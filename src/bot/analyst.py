"""LLM analyst: cleaned fundamentals + news + price context -> structured Thesis."""

import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import db
from .config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined fundamentals-driven equity analyst for a small
personal account. You are given pre-cleaned TTM fundamentals (from SEC filings),
cross-sectional percentile scores, any red flags, a fresh news item, and market context.

Judge whether the news materially changes the investment case, anchored in the
fundamentals. Rules:
- Recommend "buy" only when fundamentals are solid AND the news is a genuine positive
  catalyst that the market plausibly hasn't fully priced.
- Recommend "sell" only for a currently held position whose thesis the news invalidates.
- "watch" when interesting but not actionable; "pass" when noise. Most news is noise —
  passing is the default, not a failure.
- Never recommend buying into heavy dilution, distressed balance sheets, or pure hype.
- Conviction 5 means you'd act immediately; 1 means barely worth tracking.
- suggested_position_usd must respect the stated maximum. Set a limit_price near the
  current quote — never chase.
- invalidation_conditions must be concrete and checkable (e.g. "next quarter gross
  margin < 40%"), not vague.
"""


class Thesis(BaseModel):
    action: Literal["buy", "sell", "watch", "pass"]
    conviction: int = Field(ge=1, le=5)
    thesis: str = Field(description="2-4 sentence investment thesis grounded in the data provided")
    suggested_position_usd: float = Field(ge=0, description="0 unless action is buy")
    limit_price: float | None = Field(default=None, description="Limit price for the order, if buy/sell")
    invalidation_conditions: str = Field(description="Concrete conditions that would invalidate this thesis")


def build_context(conn, ticker: str, news_row, quote: dict | None) -> dict:
    company = conn.execute(
        "SELECT * FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    metrics = score = None
    if company:
        metrics = conn.execute("SELECT * FROM metrics WHERE cik = ?", (company["cik"],)).fetchone()
        score = conn.execute("SELECT * FROM scores WHERE cik = ?", (company["cik"],)).fetchone()
    held = conn.execute(
        "SELECT action, thesis, status, created_at FROM theses WHERE ticker = ? "
        "AND status IN ('open','executed') ORDER BY created_at DESC LIMIT 3", (ticker,)).fetchall()
    return {
        "ticker": ticker,
        "company": dict(company) if company else None,
        "ttm_metrics": {k: metrics[k] for k in metrics.keys() if k not in ("cik",)} if metrics else None,
        "screen_scores": {k: score[k] for k in score.keys() if k not in ("cik",)} if score else None,
        "red_flags": json.loads(score["red_flags"]) if score and score["red_flags"] else [],
        "news": {"title": news_row["title"], "event_type": news_row["event_type"],
                 "published_at": news_row["published_at"], "url": news_row["url"]},
        "market_quote": quote,
        "existing_positions_or_theses": [dict(h) for h in held],
        "constraints": {"max_position_usd": settings.max_position_usd},
    }


def analyze(context: dict, client: anthropic.Anthropic | None = None) -> Thesis:
    client = client or anthropic.Anthropic()
    response = client.messages.parse(
        model=settings.anthropic_model,
        max_tokens=settings.analyst_max_tokens,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": "Analyze this situation and produce a thesis:\n\n"
                       + json.dumps(context, indent=2, default=str),
        }],
        output_format=Thesis,
    )
    thesis = response.parsed_output
    if thesis is None:
        raise RuntimeError(f"analyst returned unparseable output (stop_reason={response.stop_reason})")
    return thesis


def save_thesis(conn, ticker: str, thesis: Thesis, news_id: str | None) -> int:
    cur = conn.execute(
        """INSERT INTO theses (ticker, created_at, action, conviction, thesis, position_usd,
           limit_price, invalidation, news_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (ticker, db.now_iso(), thesis.action, thesis.conviction, thesis.thesis,
         thesis.suggested_position_usd, thesis.limit_price,
         thesis.invalidation_conditions, news_id))
    conn.commit()
    return cur.lastrowid
