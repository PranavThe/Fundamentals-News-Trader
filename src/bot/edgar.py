"""SEC EDGAR client: company tickers + XBRL companyfacts, politely rate-limited.

SEC fair-access rules: max 10 req/s and a User-Agent identifying the caller.
https://www.sec.gov/os/accessing-edgar-data
"""

import threading
import time

import httpx

from .config import settings

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class _RateLimiter:
    def __init__(self, max_rps: float):
        self.min_interval = 1.0 / max_rps
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last = time.monotonic()


class EdgarClient:
    def __init__(self, user_agent: str | None = None, max_rps: float | None = None):
        self._limiter = _RateLimiter(max_rps or settings.edgar_max_rps)
        self._client = httpx.Client(
            headers={"User-Agent": user_agent or settings.edgar_user_agent,
                     "Accept-Encoding": "gzip, deflate"},
            timeout=30.0,
            follow_redirects=True,
        )

    def _get_json(self, url: str, retries: int = 3) -> dict | None:
        for attempt in range(retries + 1):
            self._limiter.wait()
            try:
                resp = self._client.get(url)
                if resp.status_code == 404:
                    return None
                if resp.status_code in (429, 500, 502, 503):
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)
        return None

    def company_tickers(self) -> list[dict]:
        """All SEC-registered tickers with exchange: [{cik, ticker, name, exchange}]."""
        data = self._get_json(TICKERS_URL)
        fields = data["fields"]  # ["cik", "name", "ticker", "exchange"]
        return [dict(zip(fields, row)) for row in data["data"]]

    def company_facts(self, cik: int) -> dict | None:
        """Full XBRL fact history for one company, or None if the CIK has no facts."""
        return self._get_json(COMPANYFACTS_URL.format(cik=cik))

    def close(self) -> None:
        self._client.close()
