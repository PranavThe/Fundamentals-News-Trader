"""Sync the tradable US-equity universe from SEC EDGAR's ticker/exchange file."""

import logging

from . import db
from .edgar import EdgarClient

log = logging.getLogger(__name__)

# Listed US exchanges as labeled in the SEC file; excludes OTC (blank/"OTC").
LISTED_EXCHANGES = {"NYSE", "Nasdaq", "NYSE American", "NYSE Arca", "CBOE", "NYSE MKT"}


def sync_universe(conn=None) -> int:
    """Refresh the companies table. Returns the number of active listings."""
    own_conn = conn is None
    conn = conn or db.get_conn()
    client = EdgarClient()
    try:
        rows = client.company_tickers()
    finally:
        client.close()

    conn.execute("UPDATE companies SET active = 0")
    count = 0
    for row in rows:
        exchange = (row.get("exchange") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        if exchange not in LISTED_EXCHANGES or not ticker:
            continue
        # Skip warrant/unit/preferred share classes like XYZ-WT, XYZ-U (keep BRK-B style? SEC
        # uses '-' for share classes too; keep single-letter suffixes, drop known non-common).
        if any(ticker.endswith(suf) for suf in ("-WT", "-U", "-R", "-P")):
            continue
        db.upsert_company(conn, int(row["cik"]), ticker, row.get("name", ""), exchange)
        count += 1
    conn.commit()
    log.info("universe sync: %d active listings", count)
    if own_conn:
        conn.close()
    return count
