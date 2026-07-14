from bot.config import settings
from bot.risk import PortfolioState, evaluate_buy, evaluate_sell


def _portfolio(**kw):
    defaults = dict(cash=10_000, equity_value=10_000, open_positions=2,
                    deployed_today_usd=0, position_value_for_ticker=0)
    defaults.update(kw)
    return PortfolioState(**defaults)


def test_low_conviction_blocked():
    d = evaluate_buy(settings.min_conviction_to_trade - 1, 500, _portfolio())
    assert not d.allowed


def test_position_cap_applies():
    d = evaluate_buy(5, settings.max_position_usd * 10, _portfolio())
    assert d.allowed
    assert d.approved_usd <= settings.max_position_usd


def test_daily_budget_exhausted_blocks():
    d = evaluate_buy(5, 500, _portfolio(deployed_today_usd=settings.max_daily_deployment_usd))
    assert not d.allowed


def test_cash_reserve_blocks():
    d = evaluate_buy(5, 500, _portfolio(cash=settings.min_cash_reserve_usd))
    assert not d.allowed


def test_max_positions_blocks():
    d = evaluate_buy(5, 500, _portfolio(open_positions=settings.max_open_positions))
    assert not d.allowed


def test_concentration_cap():
    pf = _portfolio(position_value_for_ticker=settings.max_position_pct_of_portfolio
                    * _portfolio().total_value)
    d = evaluate_buy(5, 500, pf)
    assert not d.allowed


def test_kill_switch_blocks_everything(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    assert not evaluate_buy(5, 100, _portfolio()).allowed
    assert not evaluate_sell(_portfolio(position_value_for_ticker=100)).allowed


def test_sell_requires_position():
    assert not evaluate_sell(_portfolio(position_value_for_ticker=0)).allowed
    assert evaluate_sell(_portfolio(position_value_for_ticker=100)).allowed
