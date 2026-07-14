"""Hard risk gate. Pure code — runs after the LLM and can only shrink or block."""

from dataclasses import dataclass, field

from .config import settings


@dataclass
class PortfolioState:
    cash: float = 0.0
    equity_value: float = 0.0
    open_positions: int = 0
    deployed_today_usd: float = 0.0
    position_value_for_ticker: float = 0.0

    @property
    def total_value(self) -> float:
        return self.cash + self.equity_value


@dataclass
class RiskDecision:
    allowed: bool
    approved_usd: float
    reasons: list[str] = field(default_factory=list)


def evaluate_buy(thesis_conviction: int, requested_usd: float,
                 portfolio: PortfolioState) -> RiskDecision:
    reasons: list[str] = []

    if settings.kill_switch:
        return RiskDecision(False, 0, ["kill switch is on"])
    if thesis_conviction < settings.min_conviction_to_trade:
        return RiskDecision(False, 0,
                            [f"conviction {thesis_conviction} below minimum "
                             f"{settings.min_conviction_to_trade}"])
    if portfolio.open_positions >= settings.max_open_positions:
        return RiskDecision(False, 0, [f"already at max {settings.max_open_positions} positions"])

    approved = min(requested_usd, settings.max_position_usd)
    if approved < requested_usd:
        reasons.append(f"capped to max_position_usd ${settings.max_position_usd:,.0f}")

    # Per-name concentration (existing position + new money).
    if portfolio.total_value > 0:
        headroom = (settings.max_position_pct_of_portfolio * portfolio.total_value
                    - portfolio.position_value_for_ticker)
        if headroom <= 0:
            return RiskDecision(False, 0, ["position already at concentration limit"])
        if approved > headroom:
            approved = headroom
            reasons.append(f"capped by {settings.max_position_pct_of_portfolio:.0%} "
                           "concentration limit")

    # Daily deployment budget.
    daily_headroom = settings.max_daily_deployment_usd - portfolio.deployed_today_usd
    if daily_headroom <= 0:
        return RiskDecision(False, 0, ["daily deployment budget exhausted"])
    if approved > daily_headroom:
        approved = daily_headroom
        reasons.append("capped by daily deployment budget")

    # Cash reserve / insufficient funds. An unfunded account lands here and the
    # trade is blocked cleanly (never sent to the broker to bounce).
    spendable = portfolio.cash - settings.min_cash_reserve_usd
    if spendable <= 0:
        return RiskDecision(False, 0,
                            [f"insufficient funds: cash ${portfolio.cash:,.2f} is at/below the "
                             f"${settings.min_cash_reserve_usd:,.0f} reserve floor"])
    if approved > spendable:
        approved = spendable
        reasons.append(f"capped to spendable cash ${spendable:,.2f} (reserve floor kept)")

    if approved < settings.min_order_usd:
        return RiskDecision(False, 0, reasons
                            + [f"approved ${approved:,.2f} below ${settings.min_order_usd:,.0f} "
                               "minimum order"])
    return RiskDecision(True, round(approved, 2), reasons)


def evaluate_sell(portfolio: PortfolioState) -> RiskDecision:
    if settings.kill_switch:
        return RiskDecision(False, 0, ["kill switch is on"])
    if portfolio.position_value_for_ticker <= 0:
        return RiskDecision(False, 0, ["no position to sell"])
    return RiskDecision(True, portfolio.position_value_for_ticker, [])
