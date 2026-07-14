"""SQLite storage layer."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT,
    exchange TEXT,
    active INTEGER DEFAULT 1,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS financials (
    cik INTEGER NOT NULL,
    period_end TEXT NOT NULL,          -- ISO date of fiscal quarter end
    revenue REAL, gross_profit REAL, operating_income REAL, net_income REAL,
    operating_cash_flow REAL, capex REAL,
    total_assets REAL, cash REAL, total_debt REAL, equity REAL,
    shares_outstanding REAL, interest_expense REAL,
    suspect INTEGER DEFAULT 0,         -- flagged by the cleaning layer
    updated_at TEXT,
    PRIMARY KEY (cik, period_end)
);

CREATE TABLE IF NOT EXISTS metrics (
    cik INTEGER PRIMARY KEY,
    as_of TEXT,
    revenue_ttm REAL, revenue_growth_1y REAL,
    gross_margin REAL, operating_margin REAL, fcf_ttm REAL, fcf_margin REAL,
    roic REAL, debt_to_equity REAL, interest_coverage REAL,
    share_change_1y REAL,
    quarters_available INTEGER
);

CREATE TABLE IF NOT EXISTS scores (
    cik INTEGER PRIMARY KEY,
    as_of TEXT,
    quality REAL, growth REAL, health REAL, value REAL,
    composite REAL, rank INTEGER,
    in_pool INTEGER DEFAULT 0,
    red_flags TEXT                      -- JSON list of strings
);

CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,                -- provider id or content hash
    provider TEXT,
    ticker TEXT,
    title TEXT,
    url TEXT,
    published_at TEXT,
    event_type TEXT,
    raw TEXT,                           -- original JSON
    processed INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS theses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created_at TEXT,
    action TEXT,                        -- buy | sell | watch | pass
    conviction INTEGER,
    thesis TEXT,
    position_usd REAL,
    limit_price REAL,
    invalidation TEXT,
    news_id TEXT,
    status TEXT DEFAULT 'open'          -- open | executed | rejected | expired
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id INTEGER,
    ticker TEXT,
    side TEXT,
    quantity REAL,
    limit_price REAL,
    mode TEXT,                          -- dry_run | recommend | auto
    status TEXT,                        -- simulated | recommended | placed | failed | blocked
    broker_order_id TEXT,
    detail TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker);
CREATE INDEX IF NOT EXISTS idx_financials_cik ON financials (cik);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_company(conn, cik: int, ticker: str, name: str, exchange: str) -> None:
    conn.execute(
        """INSERT INTO companies (cik, ticker, name, exchange, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(cik) DO UPDATE SET ticker=excluded.ticker, name=excluded.name,
               exchange=excluded.exchange, active=1, updated_at=excluded.updated_at""",
        (cik, ticker, name, exchange, now_iso()),
    )


def upsert_financial_row(conn, cik: int, row: dict) -> None:
    cols = [
        "revenue", "gross_profit", "operating_income", "net_income",
        "operating_cash_flow", "capex", "total_assets", "cash", "total_debt",
        "equity", "shares_outstanding", "interest_expense", "suspect",
    ]
    conn.execute(
        f"""INSERT INTO financials (cik, period_end, {', '.join(cols)}, updated_at)
            VALUES (?, ?, {', '.join('?' * len(cols))}, ?)
            ON CONFLICT(cik, period_end) DO UPDATE SET
            {', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=excluded.updated_at""",
        (cik, row["period_end"], *[row.get(c) for c in cols], now_iso()),
    )


def get_runtime_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM runtime_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_runtime_setting(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()))
    conn.commit()


def save_news_item(conn, item: dict) -> bool:
    """Insert a news item; returns False when already seen (dedupe)."""
    try:
        conn.execute(
            """INSERT INTO news (id, provider, ticker, title, url, published_at,
                                 event_type, raw, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"], item.get("provider"), item.get("ticker"),
                item.get("title"), item.get("url"), item.get("published_at"),
                item.get("event_type"), json.dumps(item.get("raw", {})), now_iso(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False
