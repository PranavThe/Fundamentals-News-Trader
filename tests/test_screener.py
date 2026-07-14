from bot.screener import compute_metrics, red_flags


def _rows(revs, ni=10.0, shares=100.0, shares_last=None):
    rows = []
    for i, rev in enumerate(revs):
        rows.append({
            "period_end": f"{2020 + i // 4}-{(i % 4) * 3 + 1:02d}-01",
            "revenue": rev, "gross_profit": rev * 0.5, "operating_income": rev * 0.2,
            "net_income": ni, "operating_cash_flow": rev * 0.25, "capex": rev * 0.05,
            "total_assets": 1000.0, "cash": 100.0, "total_debt": 200.0, "equity": 400.0,
            "shares_outstanding": shares_last if (shares_last and i == len(revs) - 1) else shares,
            "interest_expense": 5.0, "suspect": 0,
        })
    return rows


def test_needs_four_quarters():
    assert compute_metrics(_rows([100, 100, 100])) is None


def test_ttm_and_growth():
    m = compute_metrics(_rows([100] * 4 + [120] * 4))
    assert m["revenue_ttm"] == 480
    assert abs(m["revenue_growth_1y"] - 0.2) < 1e-9
    assert abs(m["gross_margin"] - 0.5) < 1e-9
    # FCF = OCF (0.25) - capex (0.05) = 0.20 of revenue
    assert abs(m["fcf_margin"] - 0.20) < 1e-9


def test_suspect_rows_excluded():
    rows = _rows([100] * 4)
    for r in rows:
        r["suspect"] = 1
    assert compute_metrics(rows) is None


def test_red_flags_dilution_and_coverage():
    m = {"share_change_1y": 0.25, "interest_coverage": 1.0, "fcf_ttm": 10,
         "operating_margin": 0.1, "revenue_growth_1y": 0.05, "debt_to_equity": 0.5}
    flags = red_flags(m)
    assert any("dilution" in f for f in flags)
    assert any("interest coverage" in f for f in flags)
    assert len(flags) == 2


def test_no_flags_for_healthy_company():
    m = compute_metrics(_rows([100] * 8))
    assert red_flags(m) == []


def test_percentiles_ties_share_midrank():
    from bot.screener import _percentiles
    # Two identical values must both land at 0.5, not be split by float noise.
    p = _percentiles({1: 3.0, 2: 3.0 + 1e-14, 3: 9.0})
    assert p[1] == p[2] == 0.25
    assert p[3] == 1.0
    inv = _percentiles({1: 1.0, 2: 2.0}, invert=True)
    assert inv[1] == 1.0 and inv[2] == 0.0
