"""Quant screen: per-company metrics from cleaned financials, then cross-sectional
percentile scores. Pure deterministic code — the LLM only ever sees the output."""

import json
import logging

from . import db
from .config import settings

log = logging.getLogger(__name__)

WEIGHTS = {"quality": 0.30, "growth": 0.25, "health": 0.25, "value": 0.20}
TAX_ADJ = 0.79  # rough (1 - 21% federal rate) for NOPAT in the ROIC estimate


def _sum_window(rows: list, key: str, start: int, count: int = 4) -> float | None:
    """Sum `count` quarters of `key` ending `start` quarters back from the latest."""
    window = rows[len(rows) - start - count: len(rows) - start]
    vals = [r[key] for r in window if r[key] is not None]
    return sum(vals) if len(vals) == count else None


def compute_metrics(rows: list) -> dict | None:
    """TTM metrics from quarterly rows (oldest -> newest). Needs >= 4 quarters."""
    rows = [r for r in rows if not r["suspect"]]
    if len(rows) < 4:
        return None
    latest = rows[-1]

    rev_ttm = _sum_window(rows, "revenue", 0)
    rev_prior = _sum_window(rows, "revenue", 4)
    gp_ttm = _sum_window(rows, "gross_profit", 0)
    op_ttm = _sum_window(rows, "operating_income", 0)
    ocf_ttm = _sum_window(rows, "operating_cash_flow", 0)
    capex_ttm = _sum_window(rows, "capex", 0)
    int_ttm = _sum_window(rows, "interest_expense", 0)

    fcf_ttm = (ocf_ttm - capex_ttm) if (ocf_ttm is not None and capex_ttm is not None) else ocf_ttm

    debt = latest["total_debt"] or 0.0
    equity = latest["equity"]
    invested = (debt + equity) if equity is not None else None

    shares_now = latest["shares_outstanding"]
    shares_prior = rows[-5]["shares_outstanding"] if len(rows) >= 5 else None

    def ratio(num, den):
        return (num / den) if (num is not None and den not in (None, 0)) else None

    return {
        "revenue_ttm": rev_ttm,
        "revenue_growth_1y": ratio(rev_ttm - rev_prior if rev_ttm is not None and rev_prior else None,
                                   abs(rev_prior) if rev_prior else None),
        "gross_margin": ratio(gp_ttm, rev_ttm),
        "operating_margin": ratio(op_ttm, rev_ttm),
        "fcf_ttm": fcf_ttm,
        "fcf_margin": ratio(fcf_ttm, rev_ttm),
        "roic": ratio(op_ttm * TAX_ADJ if op_ttm is not None else None,
                      invested if invested and invested > 0 else None),
        "debt_to_equity": ratio(debt, equity if equity and equity > 0 else None),
        "interest_coverage": ratio(op_ttm, int_ttm if int_ttm and int_ttm > 0 else None),
        "share_change_1y": ratio(shares_now - shares_prior if shares_now and shares_prior else None,
                                 shares_prior),
        "quarters_available": len(rows),
    }


def red_flags(m: dict) -> list[str]:
    flags = []
    if (m.get("share_change_1y") or 0) > 0.10:
        flags.append(f"heavy dilution: shares +{m['share_change_1y']:.0%} in 1y")
    ic = m.get("interest_coverage")
    if ic is not None and ic < 2:
        flags.append(f"weak interest coverage ({ic:.1f}x)")
    if (m.get("fcf_ttm") or 0) < 0 and (m.get("operating_margin") or 0) < 0:
        flags.append("burning cash: negative FCF and operating margin")
    if (m.get("revenue_growth_1y") or 0) < -0.15:
        flags.append(f"revenue declining {m['revenue_growth_1y']:.0%} y/y")
    if (m.get("debt_to_equity") or 0) > 3:
        flags.append(f"high leverage (D/E {m['debt_to_equity']:.1f})")
    return flags


def _percentiles(values: dict[int, float | None], invert: bool = False) -> dict[int, float]:
    """cik -> percentile in [0, 1] using midranks so ties share a rank instead of
    being ordered by float noise. Missing values get the median (0.5)."""
    from bisect import bisect_left, bisect_right

    # Normalize to 10 significant digits so arithmetic noise doesn't split ties.
    cleaned = {cik: (float(f"{v:.10g}") if v is not None else None)
               for cik, v in values.items()}
    present = sorted(v for v in cleaned.values() if v is not None)
    n = len(present)
    out = {}
    for cik, v in cleaned.items():
        if v is None or n < 2:
            out[cik] = 0.5
            continue
        midrank = (bisect_left(present, v) + bisect_right(present, v) - 1) / 2
        pct = midrank / (n - 1)
        out[cik] = 1 - pct if invert else pct
    return out


def run_screen(conn=None) -> int:
    """Compute metrics + scores for the universe; mark the top candidate pool."""
    own_conn = conn is None
    conn = conn or db.get_conn()

    ciks = [r["cik"] for r in conn.execute(
        "SELECT DISTINCT c.cik FROM companies c JOIN financials f ON f.cik = c.cik "
        "WHERE c.active = 1")]

    all_metrics: dict[int, dict] = {}
    for cik in ciks:
        rows = conn.execute(
            "SELECT * FROM financials WHERE cik = ? ORDER BY period_end", (cik,)).fetchall()
        m = compute_metrics(rows)
        if m is None:
            continue
        all_metrics[cik] = m
        conn.execute(
            """INSERT OR REPLACE INTO metrics (cik, as_of, revenue_ttm, revenue_growth_1y,
               gross_margin, operating_margin, fcf_ttm, fcf_margin, roic, debt_to_equity,
               interest_coverage, share_change_1y, quarters_available)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cik, db.now_iso(), m["revenue_ttm"], m["revenue_growth_1y"], m["gross_margin"],
             m["operating_margin"], m["fcf_ttm"], m["fcf_margin"], m["roic"],
             m["debt_to_equity"], m["interest_coverage"], m["share_change_1y"],
             m["quarters_available"]))

    if not all_metrics:
        conn.commit()
        return 0

    def pct(key, invert=False):
        return _percentiles({cik: m.get(key) for cik, m in all_metrics.items()}, invert)

    p_roic, p_gm, p_om = pct("roic"), pct("gross_margin"), pct("operating_margin")
    p_growth, p_fcfm = pct("revenue_growth_1y"), pct("fcf_margin")
    p_de, p_ic = pct("debt_to_equity", invert=True), pct("interest_coverage")
    p_dilution = pct("share_change_1y", invert=True)

    scored = []
    for cik, m in all_metrics.items():
        quality = (p_roic[cik] + p_gm[cik] + p_om[cik]) / 3
        growth = (p_growth[cik] + p_fcfm[cik]) / 2
        health = (p_de[cik] + p_ic[cik] + p_dilution[cik]) / 3
        # Value proxy: FCF relative to revenue scale until we join live market caps.
        value = p_fcfm[cik]
        composite = (WEIGHTS["quality"] * quality + WEIGHTS["growth"] * growth
                     + WEIGHTS["health"] * health + WEIGHTS["value"] * value)
        scored.append((cik, quality, growth, health, value, composite, red_flags(m)))

    scored.sort(key=lambda t: t[5], reverse=True)
    pool_size = settings.candidate_pool_size
    for rank, (cik, q, g, h, v, comp, flags) in enumerate(scored, 1):
        conn.execute(
            """INSERT OR REPLACE INTO scores (cik, as_of, quality, growth, health, value,
               composite, rank, in_pool, red_flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cik, db.now_iso(), q, g, h, v, comp, rank,
             int(rank <= pool_size and not flags), json.dumps(flags)))
    conn.commit()
    log.info("screen complete: %d scored, pool size %d", len(scored), pool_size)
    if own_conn:
        conn.close()
    return len(scored)
