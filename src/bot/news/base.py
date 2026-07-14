"""News provider interface + event classification shared by all providers."""

from dataclasses import dataclass, field
from typing import Protocol

# Ordered: first match wins. Keyword matching runs on the lowercased headline.
EVENT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("offering",  ["offering", "dilution", "shelf registration", "at-the-market", "warrant"]),
    ("earnings",  ["earnings", "quarterly results", "financial results", "q1 ", "q2 ", "q3 ", "q4 ",
                   "full year results", "eps"]),
    ("guidance",  ["guidance", "outlook", "raises forecast", "lowers forecast", "preannounce"]),
    ("m&a",       ["acquisition", "acquire", "merger", "buyout", "takeover", "to be acquired"]),
    ("fda",       ["fda", "phase 1", "phase 2", "phase 3", "clinical trial", "approval",
                   "breakthrough therapy", "nda ", "biologics license"]),
    ("contract",  ["contract", "partnership", "collaboration", "agreement", "order win", "awarded"]),
    ("management",["ceo", "cfo", "resigns", "appoints", "steps down"]),
    ("legal",     ["lawsuit", "investigation", "sec charges", "settlement", "subpoena"]),
    ("buyback",   ["buyback", "share repurchase", "dividend"]),
]

HIGH_IMPACT_EVENTS = {"earnings", "guidance", "m&a", "fda", "offering"}


def classify_event(title: str, tags: list[str] | None = None) -> str:
    haystack = " ".join([title.lower()] + [t.lower() for t in (tags or [])])
    for event_type, keywords in EVENT_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return event_type
    return "other"


@dataclass
class NewsItem:
    id: str
    provider: str
    ticker: str | None
    title: str
    url: str | None
    published_at: str | None
    event_type: str
    raw: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "id": self.id, "provider": self.provider, "ticker": self.ticker,
            "title": self.title, "url": self.url, "published_at": self.published_at,
            "event_type": self.event_type, "raw": self.raw,
        }


class NewsProvider(Protocol):
    name: str

    def fetch(self) -> list[NewsItem]:
        """Return the latest news items (provider handles its own pagination)."""
        ...
