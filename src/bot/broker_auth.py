"""OAuth for Robinhood's Trading MCP (https://agent.robinhood.com/mcp/trading).

Robinhood's agentic trading has no API keys or long-lived tokens to paste into an
env var. Auth is the standard MCP OAuth 2.1 flow: dynamic client registration +
authorization-code + PKCE, approved once in a desktop browser with a loopback
(http://localhost:...) redirect, after which the server issues short-lived access
tokens plus a refresh token.

So the bot works like this:

  1. ``bot broker login`` — one-time, on a desktop — runs the browser flow and
     saves the tokens and the registered OAuth client to ROBINHOOD_TOKEN_PATH.
  2. Every later broker call (including headless on Render) reuses that file;
     the MCP SDK refreshes access tokens automatically and rotated refresh
     tokens are written back, so the file must live on writable storage.
  3. ``bot broker export`` / ``bot broker import`` move the file between your
     desktop and the server.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .config import settings

log = logging.getLogger(__name__)


class FileTokenStorage(TokenStorage):
    """Tokens + registered-client info in a chmod-600 JSON file.

    Refresh tokens rotate, so the SDK rewrites this file during normal operation
    — it must sit on writable storage (the persistent disk on Render).
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path or settings.robinhood_token_path)

    def has_credentials(self) -> bool:
        return bool(self._read().get("tokens"))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def _redirect_uri() -> str:
    # Robinhood only accepts loopback redirects (native-app style), so the
    # browser leg of the flow has to happen on a desktop.
    return f"http://localhost:{settings.robinhood_oauth_port}/callback"


def _client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="fundamentals-news-trader",
        redirect_uris=[_redirect_uri()],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def build_auth(redirect_handler=None, callback_handler=None) -> OAuthClientProvider:
    """httpx auth for streamablehttp_client. Without handlers it is
    refresh-only: valid/refreshable stored tokens work, anything else raises
    OAuthFlowError (which the broker turns into BrokerNotConfigured)."""
    return OAuthClientProvider(
        server_url=settings.robinhood_mcp_url,
        client_metadata=_client_metadata(),
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# --- interactive login ------------------------------------------------------

def interactive_login(manual: bool = False, timeout: float = 300.0) -> list[str]:
    """Run the one-time browser OAuth flow; returns the broker tool names.

    Default mode starts a localhost callback server and opens the browser.
    ``manual`` mode is for shells with no browser/port (e.g. SSH): it prints the
    authorization URL, you approve on any desktop browser, the redirect to
    localhost fails to load there — copy that final URL from the address bar and
    paste it back.
    """
    received: dict[str, str | None] = {}
    got_callback = threading.Event()

    async def redirect_handler(url: str) -> None:
        print("\nOpen this URL in a desktop browser and approve access:\n")
        print(f"  {url}\n")
        if not manual:
            webbrowser.open(url)

    if manual:
        async def callback_handler() -> tuple[str, str | None]:
            pasted = await asyncio.to_thread(
                input, "Paste the full redirect URL from the address bar: ")
            qs = parse_qs(urlparse(pasted.strip()).query)
            if "code" not in qs:
                raise RuntimeError("No ?code= in the pasted URL — paste the entire redirect URL")
            return qs["code"][0], qs.get("state", [None])[0]
    else:
        class _Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                qs = parse_qs(urlparse(self.path).query)
                if "code" in qs:
                    received["code"] = qs["code"][0]
                    received["state"] = qs.get("state", [None])[0]
                    got_callback.set()
                    body = b"Authorized. You can close this tab and return to the terminal."
                else:
                    body = b"Missing ?code= parameter."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # silence per-request stderr noise
                pass

        server = HTTPServer(("127.0.0.1", settings.robinhood_oauth_port), _Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        async def callback_handler() -> tuple[str, str | None]:
            try:
                ok = await asyncio.to_thread(got_callback.wait, timeout)
                if not ok:
                    raise TimeoutError(f"No OAuth callback within {timeout:.0f}s")
                return received["code"], received.get("state")
            finally:
                server.shutdown()

    async def run() -> list[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        auth = build_auth(redirect_handler, callback_handler)
        async with streamablehttp_client(settings.robinhood_mcp_url, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    return asyncio.run(run())


# --- moving credentials between machines ------------------------------------

def export_blob() -> str:
    """The token file as a base64 blob (for `bot broker import` on the server)."""
    storage = FileTokenStorage()
    if not storage.has_credentials():
        raise FileNotFoundError(f"No credentials at {storage.path} — run `bot broker login` first")
    return base64.b64encode(storage.path.read_bytes()).decode()


def import_blob(blob: str) -> Path:
    """Write a blob produced by export_blob() to ROBINHOOD_TOKEN_PATH."""
    try:
        data = json.loads(base64.b64decode(blob.strip(), validate=True))
    except (binascii.Error, json.JSONDecodeError) as exc:
        raise ValueError(f"Not a valid credentials blob: {exc}") from exc
    if "tokens" not in data:
        raise ValueError("Blob decodes but contains no tokens")
    storage = FileTokenStorage()
    storage._write(data)
    return storage.path
