from bot.cleaning import clean_company_facts


def _fact(val, start=None, end=None, filed="2026-01-01"):
    f = {"val": val, "end": end, "filed": filed, "form": "10-Q"}
    if start:
        f["start"] = start
    return f


def _facts_payload():
    """Company reporting under the modern revenue tag, with FY needing Q4 derivation."""
    quarters = [
        ("2025-01-01", "2025-03-31", 100.0),
        ("2025-04-01", "2025-06-30", 110.0),
        ("2025-07-01", "2025-09-30", 120.0),
    ]
    rev_facts = [_fact(v, s, e) for s, e, v in quarters]
    # Annual fact covering the fiscal year; Q4 = 500 - (100+110+120) = 170
    rev_facts.append(_fact(500.0, "2025-01-01", "2025-12-31", filed="2026-02-01"))
    # A restated Q1 filed later should win over the original.
    rev_facts.append(_fact(105.0, "2025-01-01", "2025-03-31", filed="2026-03-01"))

    ni_facts = [_fact(v / 10, s, e) for s, e, v in quarters] + [
        _fact(50.0, "2025-01-01", "2025-12-31")]

    assets = [_fact(1000.0, end=e) for _, e, _ in quarters] + [_fact(1100.0, end="2025-12-31")]

    return {
        "facts": {
            "us-gaap": {
                # Only the fallback tag is present — primary "Revenues" missing.
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": rev_facts}},
                "NetIncomeLoss": {"units": {"USD": ni_facts}},
                "Assets": {"units": {"USD": assets}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {"shares": [_fact(1_000_000, end="2025-09-30")]}},
            },
        }
    }


def test_tag_fallback_and_quarter_grid():
    rows = clean_company_facts(_facts_payload())
    ends = [r["period_end"] for r in rows]
    assert ends == ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]


def test_restatement_wins():
    rows = clean_company_facts(_facts_payload())
    q1 = next(r for r in rows if r["period_end"] == "2025-03-31")
    assert q1["revenue"] == 105.0


def test_q4_derived_from_annual():
    rows = clean_company_facts(_facts_payload())
    q4 = next(r for r in rows if r["period_end"] == "2025-12-31")
    # FY 500 minus restated Q1 (105) + Q2 (110) + Q3 (120) = 165
    assert q4["revenue"] == 165.0


def test_instant_values_align_with_tolerance():
    rows = clean_company_facts(_facts_payload())
    q3 = next(r for r in rows if r["period_end"] == "2025-09-30")
    assert q3["total_assets"] == 1000.0
    assert q3["shares_outstanding"] == 1_000_000


def test_missing_everything_is_empty():
    assert clean_company_facts({"facts": {}}) == []
