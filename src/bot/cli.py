"""CLI: run any pipeline stage on demand.

  bot sync                 refresh the tradable universe from SEC
  bot etl [--limit N]      fundamentals ETL (EDGAR -> cleaned quarterly rows)
  bot screen               compute metrics + composite scores + candidate pool
  bot top [--n 25]         show the current top of the candidate pool
  bot news [--loop]        one news ingest+analyze cycle (or continuous loop)
  bot analyze TSLA         force a full analysis of one ticker (uses latest news)
  bot monitor              write the daily portfolio review report
  bot run                  start the full scheduler (what Render runs)
  bot broker login         one-time Robinhood OAuth (desktop browser)
  bot broker status        check the broker connection end to end
  bot broker export        print credentials blob to move to a server
  bot broker import BLOB   install a credentials blob (e.g. in Render's shell)
  bot broker logout        delete stored credentials
"""

import json
import logging
import time

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .config import settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def sync():
    """Refresh the tradable US-equity universe from SEC EDGAR."""
    from .universe import sync_universe
    count = sync_universe()
    console.print(f"[green]Universe synced: {count} active listings[/green]")


@app.command()
def etl(limit: int = typer.Option(None, help="Only process the first N companies"),
        ticker: list[str] = typer.Option(None, "--ticker", "-t", help="Restrict to tickers")):
    """Fetch + clean fundamentals from SEC EDGAR."""
    from .etl import run_etl
    updated = run_etl(limit=limit, tickers=ticker or None)
    console.print(f"[green]ETL complete: {updated} companies updated[/green]")


@app.command()
def screen():
    """Compute TTM metrics, composite scores, and the candidate pool."""
    from .screener import run_screen
    scored = run_screen()
    console.print(f"[green]Screen complete: {scored} companies scored[/green]")


@app.command()
def top(n: int = 25):
    """Show the top of the candidate pool."""
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT s.rank, c.ticker, c.name, s.composite, s.quality, s.growth, s.health,
                  s.in_pool, s.red_flags, m.revenue_ttm, m.revenue_growth_1y
           FROM scores s JOIN companies c ON c.cik = s.cik
           LEFT JOIN metrics m ON m.cik = s.cik
           ORDER BY s.rank LIMIT ?""", (n,)).fetchall()
    table = Table(title=f"Top {n} by composite fundamental score")
    for col in ("Rank", "Ticker", "Name", "Composite", "Rev TTM", "Rev growth", "Flags"):
        table.add_column(col)
    for r in rows:
        flags = ", ".join(json.loads(r["red_flags"] or "[]")) or "-"
        rev = f"${r['revenue_ttm'] / 1e9:.1f}B" if r["revenue_ttm"] else "-"
        growth = f"{r['revenue_growth_1y']:.0%}" if r["revenue_growth_1y"] is not None else "-"
        table.add_row(str(r["rank"]), r["ticker"], (r["name"] or "")[:38],
                      f"{r['composite']:.3f}", rev, growth, flags)
    console.print(table)
    conn.close()


@app.command()
def news(loop: bool = typer.Option(False, help="Keep polling on the configured interval")):
    """Ingest Stock Titan news and analyze anything that triggers."""
    from .engine import run_news_cycle
    while True:
        summaries = run_news_cycle()
        if summaries:
            for s in summaries:
                console.print(f"[bold]{s}[/bold]")
        else:
            console.print("[dim]No triggers this cycle.[/dim]")
        if not loop:
            break
        time.sleep(settings.news_poll_seconds)


@app.command()
def analyze(ticker: str):
    """Force a full analysis of one ticker using its most recent stored news item."""
    from . import broker
    from .analyst import analyze as run_analysis, build_context, save_thesis
    from .engine import execute_thesis, ingest_news

    conn = db.get_conn()
    ticker = ticker.upper()
    ingest_news(conn)
    news_row = conn.execute(
        "SELECT * FROM news WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
        (ticker,)).fetchone()
    if news_row is None:
        # Synthesize a neutral prompt so pure-fundamentals analysis still works.
        db.save_news_item(conn, {"id": f"manual:{ticker}:{db.now_iso()}", "provider": "manual",
                                 "ticker": ticker, "title": "Manual review (no fresh news)",
                                 "url": None, "published_at": db.now_iso(),
                                 "event_type": "other", "raw": {}})
        conn.commit()
        news_row = conn.execute(
            "SELECT * FROM news WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
            (ticker,)).fetchone()

    context = build_context(conn, ticker, news_row, broker.get_quote(ticker))
    console.print_json(json.dumps(context, default=str))
    thesis = run_analysis(context)
    console.print(f"\n[bold]{thesis.action.upper()}[/bold] conviction {thesis.conviction}/5")
    console.print(thesis.thesis)
    console.print(f"Size: ${thesis.suggested_position_usd:,.0f}  Limit: {thesis.limit_price}")
    console.print(f"Invalidation: {thesis.invalidation_conditions}")
    thesis_id = save_thesis(conn, ticker, thesis, news_row["id"])
    outcome = execute_thesis(conn, thesis_id, ticker, thesis)
    console.print(f"[cyan]{outcome}[/cyan]")
    conn.close()


@app.command()
def mode(value: str = typer.Argument(None, help="dry_run | recommend | auto (omit to show)")):
    """Show or flip the trading mode. THE toggle: `bot mode auto` goes live,
    `bot mode dry_run` goes back to paper. Takes effect on the next cycle —
    no redeploy needed (stored in the DB, overrides the TRADE_MODE env var)."""
    from .config import TradeMode
    conn = db.get_conn()
    if value is None:
        override = db.get_runtime_setting(conn, "trade_mode")
        kill = db.get_runtime_setting(conn, "kill_switch")
        effective = override or settings.trade_mode.value
        console.print(f"Effective mode: [bold]{effective}[/bold]"
                      f"  (env: {settings.trade_mode.value}, db override: {override or '-'})")
        console.print(f"Kill switch: {kill or ('on' if settings.kill_switch else 'off')}")
    else:
        try:
            m = TradeMode(value)
        except ValueError:
            console.print(f"[red]Invalid mode {value!r}. Use: dry_run | recommend | auto[/red]")
            raise typer.Exit(1)
        if m == TradeMode.AUTO:
            typer.confirm("AUTO places REAL orders with REAL money. Continue?", abort=True)
        db.set_runtime_setting(conn, "trade_mode", m.value)
        console.print(f"[green]Trading mode set to {m.value}[/green]")
    conn.close()


@app.command()
def killswitch(state: str = typer.Argument(..., help="on | off")):
    """Emergency stop: `bot killswitch on` blocks ALL orders in every mode."""
    if state not in ("on", "off"):
        console.print("[red]Use: bot killswitch on|off[/red]")
        raise typer.Exit(1)
    conn = db.get_conn()
    db.set_runtime_setting(conn, "kill_switch", state)
    conn.close()
    color = "red" if state == "on" else "green"
    console.print(f"[{color}]Kill switch {state.upper()}[/{color}]")


broker_app = typer.Typer(no_args_is_help=True,
                         help="Robinhood Trading MCP connection (OAuth login, status, key transfer).")
app.add_typer(broker_app, name="broker")


@broker_app.command()
def login(manual: bool = typer.Option(
        False, help="No local browser/port (e.g. SSH): approve on any desktop, "
                    "then paste the redirect URL back")):
    """One-time OAuth login to Robinhood's Trading MCP.

    Needs a desktop browser (Robinhood only allows loopback redirects). Saves
    auto-refreshing tokens to ROBINHOOD_TOKEN_PATH; run once, works headless after.
    """
    from .broker_auth import interactive_login
    tools = interactive_login(manual=manual)
    console.print(f"[green]Connected to Robinhood. {len(tools)} broker tools available:[/green]")
    console.print(", ".join(tools))


@broker_app.command()
def status():
    """Check the broker connection end to end (config, credentials, live call)."""
    from . import broker
    from .broker_auth import FileTokenStorage

    storage = FileTokenStorage()
    console.print(f"Endpoint: {settings.robinhood_mcp_url}")
    console.print(f"Static token override: {'set' if settings.robinhood_mcp_token else 'no'}")
    console.print(f"OAuth credentials: "
                  f"{storage.path if storage.has_credentials() else 'none (run `bot broker login`)'}")
    console.print(f"Account number: {settings.robinhood_account_number or '(not set)'}")
    try:
        quote = broker.call_tool("get_equity_quotes", {"symbols": ["AAPL"]})
        console.print(f"[green]Live check OK — AAPL quote: {quote}[/green]")
    except broker.BrokerNotConfigured as exc:
        console.print(f"[yellow]Not connected: {exc}[/yellow]")
    except broker.BrokerError as exc:
        console.print(f"[red]Connected but the call failed: {exc}[/red]")


@broker_app.command(name="export")
def broker_export():
    """Print the credentials blob (paste into `bot broker import` on the server)."""
    from .broker_auth import export_blob
    console.print(export_blob(), soft_wrap=True)


@broker_app.command(name="import")
def broker_import(blob: str = typer.Argument(None, help="Blob from `bot broker export` "
                                                        "(omit to read from stdin)")):
    """Install a credentials blob produced by `bot broker export`."""
    import sys
    from .broker_auth import import_blob
    path = import_blob(blob or sys.stdin.read())
    console.print(f"[green]Credentials installed at {path}[/green]")


@broker_app.command()
def logout():
    """Delete the stored Robinhood credentials."""
    from .broker_auth import FileTokenStorage
    FileTokenStorage().clear()
    console.print("[green]Credentials deleted.[/green]")


@app.command()
def monitor():
    """Write the daily portfolio review report."""
    from .monitor import run_monitor
    console.print(run_monitor())


@app.command()
def run():
    """Start the full scheduler (long-running worker; Render entrypoint)."""
    from .scheduler import main
    main()


if __name__ == "__main__":
    app()
