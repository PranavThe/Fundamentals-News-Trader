"""Trigger engine: decide which stored news items deserve an LLM deep-dive."""

import json
from dataclasses import dataclass

from .news.base import HIGH_IMPACT_EVENTS


@dataclass
class Trigger:
    news_id: str
    ticker: str
    reason: str


def _held_tickers(conn) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM theses WHERE status IN ('open', 'executed')").fetchall()
    return {r["ticker"] for r in rows}


def evaluate_pending_news(conn, max_triggers: int = 10) -> list[Trigger]:
    """Scan unprocessed news; return triggers and mark everything scanned as processed.

    Fires when: (a) the ticker is in the fundamentals candidate pool,
    (b) the event type is high-impact, or (c) we hold/track the name.
    """
    pool = {r["ticker"] for r in conn.execute(
        "SELECT c.ticker FROM scores s JOIN companies c ON c.cik = s.cik WHERE s.in_pool = 1")}
    red_flagged = {r["ticker"]: json.loads(r["red_flags"] or "[]") for r in conn.execute(
        "SELECT c.ticker, s.red_flags FROM scores s JOIN companies c ON c.cik = s.cik "
        "WHERE s.red_flags != '[]'")}
    held = _held_tickers(conn)
    known = {r["ticker"] for r in conn.execute("SELECT ticker FROM companies WHERE active = 1")}

    pending = conn.execute(
        "SELECT * FROM news WHERE processed = 0 ORDER BY created_at DESC LIMIT 200").fetchall()

    triggers: list[Trigger] = []
    for item in pending:
        conn.execute("UPDATE news SET processed = 1 WHERE id = ?", (item["id"],))
        ticker = item["ticker"]
        if not ticker or ticker not in known or len(triggers) >= max_triggers:
            continue
        if ticker in red_flagged and item["event_type"] not in ("m&a",):
            # Bullish news on a red-flagged balance sheet is usually a trap; skip
            # unless it's a takeover, which changes the calculus entirely.
            continue
        if ticker in held:
            triggers.append(Trigger(item["id"], ticker, f"news on held position ({item['event_type']})"))
        elif ticker in pool and item["event_type"] != "other":
            triggers.append(Trigger(item["id"], ticker, f"candidate-pool stock with {item['event_type']} news"))
        elif item["event_type"] in HIGH_IMPACT_EVENTS:
            triggers.append(Trigger(item["id"], ticker, f"high-impact event: {item['event_type']}"))
    conn.commit()
    return triggers
