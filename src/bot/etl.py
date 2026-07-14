"""Nightly fundamentals ETL: EDGAR companyfacts -> cleaning -> SQLite."""

import logging

from . import db
from .cleaning import clean_company_facts
from .edgar import EdgarClient

log = logging.getLogger(__name__)


def run_etl(conn=None, limit: int | None = None, tickers: list[str] | None = None) -> int:
    """Refresh financials for the active universe. Returns companies updated."""
    own_conn = conn is None
    conn = conn or db.get_conn()

    query = "SELECT cik, ticker FROM companies WHERE active = 1"
    params: tuple = ()
    if tickers:
        placeholders = ",".join("?" * len(tickers))
        query += f" AND ticker IN ({placeholders})"
        params = tuple(t.upper() for t in tickers)
    query += " ORDER BY cik"
    if limit:
        query += f" LIMIT {int(limit)}"

    companies = conn.execute(query, params).fetchall()
    client = EdgarClient()
    updated = 0
    try:
        for i, company in enumerate(companies, 1):
            try:
                facts = client.company_facts(company["cik"])
            except Exception as exc:  # network hiccup on one company shouldn't kill the run
                log.warning("companyfacts failed for %s: %s", company["ticker"], exc)
                continue
            if not facts:
                continue
            rows = clean_company_facts(facts)
            for row in rows:
                db.upsert_financial_row(conn, company["cik"], row)
            if rows:
                updated += 1
            if i % 50 == 0:
                conn.commit()
                log.info("etl progress: %d/%d companies", i, len(companies))
        conn.commit()
    finally:
        client.close()
        if own_conn:
            conn.close()
    log.info("etl complete: %d companies updated", updated)
    return updated
