"""Central configuration, loaded from environment variables / .env."""

from enum import Enum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class TradeMode(str, Enum):
    DRY_RUN = "dry_run"      # log hypothetical trades only
    RECOMMEND = "recommend"  # write recommendation reports + notify; no orders
    AUTO = "auto"            # place orders via Robinhood MCP (within risk limits)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage ---
    db_path: Path = Path("data/bot.db")
    reports_dir: Path = Path("reports")

    # --- SEC EDGAR (they require a descriptive User-Agent with contact info) ---
    edgar_user_agent: str = "FundamentalsNewsTrader/0.1 (contact: set EDGAR_USER_AGENT)"
    edgar_max_rps: float = 8.0

    # --- Anthropic / analyst ---
    anthropic_model: str = "claude-opus-4-8"
    analyst_max_tokens: int = 16000

    # --- Stock Titan news ---
    stocktitan_url: str = "https://wpapi.stocktitan.net/api/news/json"
    news_poll_seconds: int = 90

    # --- screening ---
    candidate_pool_size: int = 200
    min_market_cap_usd: float = 300_000_000  # skip micro caps by default

    # --- trading ---
    trade_mode: TradeMode = TradeMode.DRY_RUN
    kill_switch: bool = False  # True = never place orders, regardless of mode

    # Paper portfolio used by dry_run/recommend when the broker is not connected.
    paper_cash_usd: float = 10_000.0

    # --- Robinhood MCP connection ---
    # Official Trading MCP endpoint; override only for community/self-hosted wrappers.
    robinhood_mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    # OAuth token cache written by `bot broker login`. Must be writable at runtime:
    # refresh tokens rotate. On Render, keep it on the persistent disk.
    robinhood_token_path: Path = Path("data/robinhood_tokens.json")
    # Loopback port for the one-time OAuth browser redirect during `bot broker login`.
    robinhood_oauth_port: int = 8917
    # Optional static bearer override (community wrappers only — the official MCP
    # has no static tokens; leave empty to use the OAuth flow above).
    robinhood_mcp_token: str = ""
    robinhood_account_number: str = ""

    # --- hard risk limits (enforced in code; the LLM cannot override these) ---
    max_position_usd: float = 1_000.0
    max_daily_deployment_usd: float = 3_000.0
    max_open_positions: int = 20
    min_cash_reserve_usd: float = 500.0
    max_position_pct_of_portfolio: float = 0.10
    min_conviction_to_trade: int = 4  # thesis conviction 1-5
    min_order_usd: float = 10.0       # skip orders smaller than this (fractional shares ok)

    # --- notifications (optional; POSTs {"text": ...} JSON) ---
    notify_webhook_url: str = ""


settings = Settings()
