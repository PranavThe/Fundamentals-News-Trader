"""Long-running worker: APScheduler wiring for the full pipeline (Render entrypoint)."""

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .engine import run_news_cycle
from .etl import run_etl
from .monitor import run_monitor
from .screener import run_screen
from .universe import sync_universe

log = logging.getLogger(__name__)
ET = "America/New_York"


def _market_hours_news_cycle() -> None:
    """Poll news only during extended US market hours (Mon-Fri, 7:00-20:00 ET)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(ET))
    if now.weekday() >= 5 or not (7 <= now.hour < 20):
        return
    run_news_cycle()


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone=ET)
    # Weekly universe refresh (Sunday night).
    sched.add_job(sync_universe, CronTrigger(day_of_week="sun", hour=22, minute=0),
                  name="universe-sync", misfire_grace_time=3600)
    # Nightly fundamentals ETL + screen, done before pre-market.
    sched.add_job(run_etl, CronTrigger(hour=1, minute=30), name="fundamentals-etl",
                  misfire_grace_time=3600)
    sched.add_job(run_screen, CronTrigger(hour=4, minute=30), name="quant-screen",
                  misfire_grace_time=3600)
    # News poll during (extended) market hours.
    sched.add_job(_market_hours_news_cycle,
                  IntervalTrigger(seconds=settings.news_poll_seconds),
                  name="news-cycle", max_instances=1, coalesce=True)
    # Daily position review after the close.
    sched.add_job(run_monitor, CronTrigger(day_of_week="mon-fri", hour=16, minute=30),
                  name="portfolio-monitor", misfire_grace_time=3600)
    return sched


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting scheduler (trade_mode=%s, kill_switch=%s)",
             settings.trade_mode.value, settings.kill_switch)
    build_scheduler().start()


if __name__ == "__main__":
    main()
