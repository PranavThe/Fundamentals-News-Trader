"""Stock Titan news provider.

Stock Titan has no official developer API; this polls the JSON endpoint their own
web app uses. The parsing is deliberately defensive (multiple key fallbacks, raw
payload preserved) because the shape can change without notice. Swappable via the
NewsProvider interface if it breaks or a licensed source is preferred.
"""

import hashlib
import json
import logging

import httpx

from ..config import settings
from .base import NewsItem, classify_event

log = logging.getLogger(__name__)


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _extract_tickers(entry: dict) -> list[str]:
    raw = _first(entry, "symbols", "tickers", "symbol", "ticker", default=[])
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.replace(";", ",").split(",")]
    out = []
    for item in raw if isinstance(raw, list) else []:
        sym = item if isinstance(item, str) else _first(item or {}, "symbol", "ticker", default="")
        sym = str(sym).strip().upper()
        if sym and len(sym) <= 6 and sym.isalnum():
            out.append(sym)
    return out


class StockTitanProvider:
    name = "stocktitan"

    def __init__(self, url: str | None = None):
        self.url = url or settings.stocktitan_url
        self._client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FundamentalsNewsTrader/0.1)",
                     "Accept": "application/json"},
        )

    def fetch(self) -> list[NewsItem]:
        try:
            resp = self._client.get(self.url)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            log.warning("stocktitan fetch failed: %s", exc)
            return []
        return self.parse(payload)

    def parse(self, payload) -> list[NewsItem]:
        entries = payload
        if isinstance(payload, dict):
            entries = _first(payload, "news", "data", "items", "articles", default=[])
        if not isinstance(entries, list):
            log.warning("stocktitan: unexpected payload shape %s", type(payload).__name__)
            return []

        items: list[NewsItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(_first(entry, "title", "headline", default="")).strip()
            if not title:
                continue
            url = _first(entry, "url", "link", "canonical_url")
            published = _first(entry, "date", "published_at", "created_at", "time", "timestamp")
            raw_id = _first(entry, "id", "guid", "news_id")
            item_id = f"{self.name}:{raw_id}" if raw_id else \
                "sha:" + hashlib.sha256(f"{title}|{url}".encode()).hexdigest()[:24]
            tags = _first(entry, "tags", "categories", default=[])
            tags = [str(t) for t in tags] if isinstance(tags, list) else [str(tags)]
            tickers = _extract_tickers(entry) or [None]

            for i, ticker in enumerate(tickers):
                items.append(NewsItem(
                    id=item_id if i == 0 else f"{item_id}:{ticker}",
                    provider=self.name,
                    ticker=ticker,
                    title=title,
                    url=str(url) if url else None,
                    published_at=str(published) if published else None,
                    event_type=classify_event(title, tags),
                    raw=entry,
                ))
        return items

    def close(self) -> None:
        self._client.close()
