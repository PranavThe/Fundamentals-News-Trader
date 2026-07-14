"""XBRL cleaning/normalization: raw EDGAR companyfacts -> tidy quarterly rows.

This is the "nicely clean up the data before the agent uses it" layer:
- concept tag fallbacks (companies report revenue under different us-gaap tags)
- fiscal alignment: classify facts as quarterly vs annual by duration, derive the
  missing Q4 as FY minus the other three quarters
- dedupe restatements by preferring the most recently filed value
- unit selection (USD / shares) and basic sanity flags on the output rows
"""

from datetime import date, timedelta

# Flow (duration) concepts, in fallback priority order.
FLOW_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"],
}

# Instant (point-in-time) concepts.
INSTANT_CONCEPTS: dict[str, list[str]] = {
    "total_assets": ["Assets"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "total_debt": [
        "LongTermDebt",
        "LongTermDebtNoncurrent",
        "DebtLongtermAndShorttermCombinedAmount",
    ],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "shares_outstanding": ["CommonStockSharesOutstanding", "CommonStockSharesIssued"],
}

QUARTER_DAYS = (60, 120)   # accept 2-4 month durations as "a quarter"
ANNUAL_DAYS = (330, 400)


def _iso(d: str) -> date:
    return date.fromisoformat(d)


def _facts_for(facts: dict, taxonomy: str, tag: str, unit_keys: tuple[str, ...]) -> list[dict]:
    tag_data = facts.get("facts", {}).get(taxonomy, {}).get(tag)
    if not tag_data:
        return []
    units = tag_data.get("units", {})
    for key in unit_keys:
        if key in units:
            return units[key]
    return []


def _first_available(facts: dict, tags: list[str], unit_keys: tuple[str, ...],
                     taxonomies: tuple[str, ...] = ("us-gaap",)) -> list[dict]:
    for taxonomy in taxonomies:
        for tag in tags:
            found = _facts_for(facts, taxonomy, tag, unit_keys)
            if found:
                return found
    return []


def _dedupe_latest_filed(entries: list[tuple[str, dict]]) -> dict[str, float]:
    """{key: value}, keeping the most recently *filed* fact per key (restatements win)."""
    best: dict[str, dict] = {}
    for key, fact in entries:
        prev = best.get(key)
        if prev is None or fact.get("filed", "") >= prev.get("filed", ""):
            best[key] = fact
    return {k: f["val"] for k, f in best.items() if isinstance(f.get("val"), (int, float))}


def _flow_series(raw: list[dict]) -> dict[str, float]:
    """Quarterly values keyed by period-end date, deriving Q4 from annual facts."""
    quarterly: list[tuple[str, dict]] = []
    annual: list[tuple[str, dict]] = []
    for fact in raw:
        start, end = fact.get("start"), fact.get("end")
        if not start or not end:
            continue
        days = (_iso(end) - _iso(start)).days
        if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
            quarterly.append((end, fact))
        elif ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
            annual.append((end, fact))

    q = _dedupe_latest_filed(quarterly)
    fy = _dedupe_latest_filed(annual)

    # Derive missing Q4: FY value minus the three quarters inside the same fiscal year.
    for end, fy_val in fy.items():
        if end in q:
            continue
        fy_end = _iso(end)
        fy_start = fy_end - timedelta(days=370)
        inside = [v for e, v in q.items() if fy_start < _iso(e) < fy_end]
        if len(inside) == 3:
            q[end] = fy_val - sum(inside)
    return q


def _instant_series(raw: list[dict]) -> dict[str, float]:
    return _dedupe_latest_filed([(f["end"], f) for f in raw if f.get("end")])


def _nearest(series: dict[str, float], target: str, tolerance_days: int = 14) -> float | None:
    """Value at target date, tolerating small period-end mismatches across statements."""
    if target in series:
        return series[target]
    t = _iso(target)
    best_key, best_gap = None, tolerance_days + 1
    for key in series:
        gap = abs((_iso(key) - t).days)
        if gap < best_gap:
            best_key, best_gap = key, gap
    return series.get(best_key) if best_key else None


def clean_company_facts(facts: dict, max_quarters: int = 12) -> list[dict]:
    """Turn one EDGAR companyfacts payload into tidy quarterly rows (newest last)."""
    flow = {name: _flow_series(_first_available(facts, tags, ("USD",)))
            for name, tags in FLOW_CONCEPTS.items()}
    instant = {name: _instant_series(_first_available(facts, tags, ("USD",)))
               for name, tags in INSTANT_CONCEPTS.items()}
    # Shares: prefer the dei cover-page tag, fall back to us-gaap.
    dei_shares = _instant_series(
        _first_available(facts, ["EntityCommonStockSharesOutstanding"], ("shares",), ("dei",)))
    if dei_shares:
        instant["shares_outstanding"] = dei_shares

    # The quarter grid is driven by periods where we actually have revenue or net income.
    period_ends = sorted(set(flow["revenue"]) | set(flow["net_income"]))[-max_quarters:]

    rows = []
    for end in period_ends:
        row: dict = {"period_end": end}
        for name, series in flow.items():
            row[name] = series.get(end)
        for name, series in instant.items():
            row[name] = _nearest(series, end)
        row["suspect"] = int(
            (row.get("revenue") is not None and row["revenue"] < 0)
            or (row.get("shares_outstanding") or 0) < 0
            or (row.get("total_assets") or 0) < 0
        )
        rows.append(row)
    return rows
