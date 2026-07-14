"""Daily portfolio review: open theses vs. current state -> markdown report."""

import logging

from . import broker, db
from .config import settings

log = logging.getLogger(__name__)


def run_monitor(conn=None) -> str:
    own_conn = conn is None
    conn = conn or db.get_conn()
    lines = [f"# Portfolio monitor — {db.now_iso()}", ""]

    try:
        pf = broker.get_portfolio()
        lines.append(f"**Portfolio:** `{str(pf)[:500]}`\n")
    except broker.BrokerNotConfigured:
        lines.append("_Broker not configured; showing tracked theses only._\n")
    except Exception as exc:
        lines.append(f"_Portfolio fetch failed: {exc}_\n")

    theses = conn.execute(
        "SELECT * FROM theses WHERE status IN ('open','executed') ORDER BY created_at DESC"
    ).fetchall()
    if not theses:
        lines.append("No open theses.")
    for t in theses:
        quote = broker.get_quote(t["ticker"])
        lines.append(f"## {t['ticker']} — {t['action']} ({t['status']}, "
                     f"conviction {t['conviction']}/5, {t['created_at'][:10]})")
        lines.append(f"- Thesis: {t['thesis']}")
        lines.append(f"- Invalidation: {t['invalidation']}")
        if quote:
            lines.append(f"- Current quote: `{str(quote)[:200]}`")
        lines.append("")

    report = "\n".join(lines)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path = settings.reports_dir / f"monitor-{db.now_iso()[:10]}.md"
    path.write_text(report)
    log.info("monitor report written to %s", path)
    if own_conn:
        conn.close()
    return report
