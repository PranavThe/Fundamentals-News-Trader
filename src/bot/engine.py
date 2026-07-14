"""Orchestration: news cycle -> triggers -> LLM analysis -> risk gate -> execution.

Execution honors TRADE_MODE:
  dry_run   - log a simulated order only
  recommend - write a markdown recommendation + optional webhook notification
  auto      - review + place a limit order through the Robinhood MCP
"""

import logging

import httpx

from . import broker, db
from .analyst import Thesis, analyze, build_context, save_thesis
from .config import TradeMode, settings
from .news.stocktitan import StockTitanProvider
from .risk import PortfolioState, evaluate_buy, evaluate_sell
from .triggers import evaluate_pending_news

log = logging.getLogger(__name__)


def apply_runtime_overrides(conn) -> None:
    """DB-backed toggles (set via `bot mode` / `bot killswitch`) override env config,
    so trading can be flipped on/off without redeploying the worker."""
    mode = db.get_runtime_setting(conn, "trade_mode")
    if mode:
        try:
            settings.trade_mode = TradeMode(mode)
        except ValueError:
            log.error("ignoring invalid runtime trade_mode %r", mode)
    kill = db.get_runtime_setting(conn, "kill_switch")
    if kill is not None:
        settings.kill_switch = kill.lower() in ("1", "true", "on", "yes")


def ingest_news(conn, provider=None) -> int:
    provider = provider or StockTitanProvider()
    new_count = 0
    for item in provider.fetch():
        if db.save_news_item(conn, item.as_row()):
            new_count += 1
    conn.commit()
    if new_count:
        log.info("news ingest: %d new items", new_count)
    return new_count


def _deployed_today(conn) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity * limit_price), 0) AS spent FROM orders "
        "WHERE side = 'buy' AND status IN ('simulated', 'recommended', 'placed') "
        "AND created_at >= ?", (db.now_iso()[:10],)).fetchone()
    return float(row["spent"] or 0)


def _portfolio_state(conn, ticker: str) -> PortfolioState:
    """Live portfolio state when the broker is connected; otherwise a paper portfolio
    for dry_run/recommend (zeros in auto mode, so nothing can be bought blind)."""
    state = PortfolioState()
    try:
        pf = broker.get_portfolio() or {}
        if isinstance(pf, dict):
            state.cash = float(pf.get("cash", pf.get("buying_power", 0)) or 0)
            state.equity_value = float(pf.get("equity", pf.get("market_value", 0)) or 0)
        positions = broker.get_positions() or {}
        rows = positions.get("results", positions) if isinstance(positions, dict) else positions
        if isinstance(rows, list):
            state.open_positions = len(rows)
            for p in rows:
                if isinstance(p, dict) and str(p.get("symbol", "")).upper() == ticker.upper():
                    state.position_value_for_ticker = float(
                        p.get("market_value", p.get("equity", 0)) or 0)
    except broker.BrokerNotConfigured:
        if settings.trade_mode != TradeMode.AUTO:
            state.cash = settings.paper_cash_usd
            state.open_positions = conn.execute(
                "SELECT COUNT(DISTINCT ticker) AS n FROM theses WHERE status = 'executed'"
            ).fetchone()["n"]
            log.info("broker not configured; using paper portfolio ($%.0f cash)", state.cash)
        else:
            log.warning("broker not configured in AUTO mode; all buys will be blocked")
    except Exception as exc:
        log.warning("portfolio fetch failed: %s", exc)
    state.deployed_today_usd = max(state.deployed_today_usd, _deployed_today(conn))
    return state


def _extract_price(quote) -> float | None:
    if isinstance(quote, dict):
        for key in ("last_trade_price", "price", "last_price", "mark_price"):
            if key in quote:
                try:
                    return float(quote[key])
                except (TypeError, ValueError):
                    pass
        results = quote.get("results")
        if isinstance(results, list) and results:
            return _extract_price(results[0])
    if isinstance(quote, list) and quote:
        return _extract_price(quote[0])
    return None


def execute_thesis(conn, thesis_id: int, ticker: str, thesis: Thesis) -> str:
    """Apply the risk gate and act according to TRADE_MODE. Returns a status string."""
    apply_runtime_overrides(conn)
    mode = settings.trade_mode

    if thesis.action in ("watch", "pass"):
        return f"{thesis.action}: no order"

    portfolio = _portfolio_state(conn, ticker)
    if thesis.action == "buy":
        decision = evaluate_buy(thesis.conviction, thesis.suggested_position_usd, portfolio)
    else:
        decision = evaluate_sell(portfolio)

    if not decision.allowed:
        conn.execute(
            "INSERT INTO orders (thesis_id, ticker, side, mode, status, detail, created_at) "
            "VALUES (?, ?, ?, ?, 'blocked', ?, ?)",
            (thesis_id, ticker, thesis.action, mode.value, "; ".join(decision.reasons), db.now_iso()))
        conn.execute("UPDATE theses SET status = 'rejected' WHERE id = ?", (thesis_id,))
        conn.commit()
        return f"blocked by risk gate: {'; '.join(decision.reasons)}"

    limit_price = thesis.limit_price or _extract_price(broker.get_quote(ticker))
    quantity = round(decision.approved_usd / limit_price, 4) if limit_price else None
    detail = (f"{thesis.action} ${decision.approved_usd:,.2f}"
              + (f" (~{quantity} sh @ ${limit_price:,.2f} limit)" if limit_price else " (no quote)"))

    status = "simulated"
    if mode == TradeMode.AUTO and limit_price and quantity:
        try:
            broker.review_order(ticker, thesis.action, quantity, limit_price)
            result = broker.place_order(ticker, thesis.action, quantity, limit_price)
            status = "placed"
            detail += f" | broker: {str(result)[:300]}"
            conn.execute("UPDATE theses SET status = 'executed' WHERE id = ?", (thesis_id,))
        except (broker.BrokerNotConfigured, broker.BrokerError) as exc:
            # Broker rejections (e.g. insufficient buying power despite our checks)
            # are recorded and surfaced, never raised — the worker keeps running.
            status = "failed"
            detail += f" | error: {exc}"
            _notify(f"[auto] order FAILED for {ticker}: {exc}")
    elif mode == TradeMode.AUTO:
        status = "blocked"
        detail += " | no quote available; refusing to place an unpriced order"
    elif mode == TradeMode.RECOMMEND:
        status = "recommended"

    conn.execute(
        "INSERT INTO orders (thesis_id, ticker, side, quantity, limit_price, mode, status, "
        "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (thesis_id, ticker, thesis.action, quantity, limit_price, mode.value, status,
         detail, db.now_iso()))
    conn.commit()
    return f"{status}: {detail}"


def _notify(text: str) -> None:
    if not settings.notify_webhook_url:
        return
    try:
        httpx.post(settings.notify_webhook_url, json={"text": text}, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("notification failed: %s", exc)


def _write_report(ticker: str, thesis: Thesis, news_title: str, outcome: str) -> None:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / f"{db.now_iso()[:10]}-{ticker}.md"
    body = (f"# {ticker} — {thesis.action.upper()} (conviction {thesis.conviction}/5)\n\n"
            f"**News:** {news_title}\n\n**Thesis:** {thesis.thesis}\n\n"
            f"**Size:** ${thesis.suggested_position_usd:,.0f}  "
            f"**Limit:** {thesis.limit_price}\n\n"
            f"**Invalidation:** {thesis.invalidation_conditions}\n\n"
            f"**Outcome:** {outcome}\n")
    with open(path, "a") as f:
        f.write(body + "\n---\n")


def run_news_cycle(conn=None) -> list[str]:
    """One full pass: ingest -> trigger -> analyze -> execute. Returns summaries."""
    own_conn = conn is None
    conn = conn or db.get_conn()
    summaries: list[str] = []
    try:
        apply_runtime_overrides(conn)
        ingest_news(conn)

        # Bankroll pre-check: in AUTO mode with a (near-)empty account, don't spend
        # LLM calls analyzing buys we could never place. News on held names still
        # gets analyzed because it may produce a sell.
        can_buy = True
        if settings.trade_mode == TradeMode.AUTO:
            state = _portfolio_state(conn, "")
            can_buy = (state.cash - settings.min_cash_reserve_usd) >= settings.min_order_usd
            if not can_buy:
                log.info("insufficient funds for new buys (cash $%.2f); "
                         "only held positions will be analyzed", state.cash)
        held = {r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM theses WHERE status = 'executed'")}

        for trig in evaluate_pending_news(conn):
            if not can_buy and trig.ticker not in held:
                log.info("skipping %s (%s): insufficient funds for new positions",
                         trig.ticker, trig.reason)
                continue
            news_row = conn.execute("SELECT * FROM news WHERE id = ?", (trig.news_id,)).fetchone()
            quote = broker.get_quote(trig.ticker)
            context = build_context(conn, trig.ticker, news_row, quote)
            try:
                thesis = analyze(context)
            except Exception as exc:
                log.error("analysis failed for %s: %s", trig.ticker, exc)
                continue
            thesis_id = save_thesis(conn, trig.ticker, thesis, trig.news_id)
            outcome = execute_thesis(conn, thesis_id, trig.ticker, thesis)
            summary = (f"{trig.ticker} [{trig.reason}] -> {thesis.action} "
                       f"(conviction {thesis.conviction}) -> {outcome}")
            summaries.append(summary)
            log.info(summary)
            if thesis.action in ("buy", "sell"):
                _write_report(trig.ticker, thesis, news_row["title"], outcome)
                _notify(f"[{settings.trade_mode.value}] {summary}\n{thesis.thesis}")
    finally:
        if own_conn:
            conn.close()
    return summaries
