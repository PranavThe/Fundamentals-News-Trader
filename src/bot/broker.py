"""Robinhood MCP wrapper.

Talks to Robinhood's hosted Trading MCP server over streamable HTTP. Tool names
mirror the Robinhood MCP: get_equity_quotes, get_portfolio, get_equity_positions,
review_equity_order, place_equity_order.

The endpoint defaults to Robinhood's official fixed URL
(https://agent.robinhood.com/mcp/trading); ROBINHOOD_MCP_URL only needs overriding
for community/self-hosted wrappers.

Auth: the official MCP uses OAuth with auto-refreshing tokens (see broker_auth.py)
— run `bot broker login` once to create the token file. ROBINHOOD_MCP_TOKEN
remains as a static-bearer override for community wrappers. While neither is
configured, every method raises BrokerNotConfigured so callers can degrade
gracefully (dry-run / recommend modes never need the broker for quotes to work —
they just get None).
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from .config import settings

log = logging.getLogger(__name__)


class BrokerNotConfigured(RuntimeError):
    pass


class BrokerError(RuntimeError):
    pass


@asynccontextmanager
async def _session():
    if not settings.robinhood_mcp_url:
        raise BrokerNotConfigured("ROBINHOOD_MCP_URL is empty")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers: dict[str, str] = {}
    auth = None
    if settings.robinhood_mcp_token:
        # Static bearer override for community/self-hosted MCP wrappers.
        headers["Authorization"] = f"Bearer {settings.robinhood_mcp_token}"
    else:
        from .broker_auth import FileTokenStorage, build_auth
        if not FileTokenStorage().has_credentials():
            raise BrokerNotConfigured(
                "Not connected to Robinhood. Run `bot broker login` on a desktop "
                "(then `bot broker export` / `bot broker import` to move the "
                "credentials to a server)")
        auth = build_auth()  # refresh-only: no interactive handlers here
    async with streamablehttp_client(settings.robinhood_mcp_url, headers=headers,
                                     auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _call(tool: str, arguments: dict):
    from mcp.client.auth import OAuthFlowError, OAuthTokenError

    try:
        async with _session() as session:
            result = await session.call_tool(tool, arguments=arguments)
    except (OAuthFlowError, OAuthTokenError) as exc:
        # Stored tokens exist but could not be used or refreshed.
        raise BrokerNotConfigured(
            f"Robinhood OAuth session expired ({exc}); re-run `bot broker login`") from exc
    if result.isError:
        raise BrokerError(f"{tool} failed: {result.content}")
    # MCP tools return text content; Robinhood's payloads are JSON strings.
    texts = [c.text for c in result.content if getattr(c, "text", None)]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return joined


def call_tool(tool: str, arguments: dict):
    return asyncio.run(_call(tool, arguments))


# --- convenience wrappers -------------------------------------------------

def get_quote(ticker: str) -> dict | None:
    try:
        return call_tool("get_equity_quotes", {"symbols": [ticker.upper()]})
    except BrokerNotConfigured:
        return None
    except BrokerError as exc:
        log.warning("quote fetch failed for %s: %s", ticker, exc)
        return None


def get_portfolio() -> dict:
    args = {}
    if settings.robinhood_account_number:
        args["account_number"] = settings.robinhood_account_number
    return call_tool("get_portfolio", args)


def get_positions() -> dict:
    args = {}
    if settings.robinhood_account_number:
        args["account_number"] = settings.robinhood_account_number
    return call_tool("get_equity_positions", args)


def review_order(ticker: str, side: str, quantity: float, limit_price: float) -> dict:
    return call_tool("review_equity_order", _order_args(ticker, side, quantity, limit_price))


def place_order(ticker: str, side: str, quantity: float, limit_price: float) -> dict:
    return call_tool("place_equity_order", _order_args(ticker, side, quantity, limit_price))


def _order_args(ticker: str, side: str, quantity: float, limit_price: float) -> dict:
    args = {
        "symbol": ticker.upper(),
        "side": side,               # "buy" | "sell"
        "quantity": quantity,
        "order_type": "limit",
        "limit_price": limit_price,
        "time_in_force": "gfd",
    }
    if settings.robinhood_account_number:
        args["account_number"] = settings.robinhood_account_number
    return args
