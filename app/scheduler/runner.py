"""APScheduler-based runner that drives the planner, executor and poller.

Run as a standalone worker process (see app/worker.py) separately from the
FastAPI API so the two can be scaled independently. Multiple worker replicas
are safe because every state-changing operation is guarded by Redis locks.
"""

from __future__ import annotations

import logging

from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db.database import init_db
from app.scheduler.executor import run_executor_tick
from app.scheduler.planner import plan_all_accounts
from app.scheduler.poller import run_poller_tick

logger = logging.getLogger(__name__)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        executors={"default": APThreadPoolExecutor(max_workers=4)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
        timezone="UTC",
    )
    scheduler.add_job(
        plan_all_accounts, "interval", seconds=settings.PLANNER_TICK_SECONDS,
        id="planner", next_run_time=_now(),
    )
    scheduler.add_job(
        run_executor_tick, "interval", seconds=settings.EXECUTOR_TICK_SECONDS,
        id="executor",
    )
    scheduler.add_job(
        run_poller_tick, "interval", seconds=settings.POLLER_TICK_SECONDS,
        id="poller",
    )
    return scheduler


def _now():
    from datetime import datetime
    return datetime.utcnow()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_db()
    scheduler = build_scheduler()
    logger.info(
        "Scheduler starting (planner=%ss, executor=%ss, poller=%ss, workers=%s)",
        settings.PLANNER_TICK_SECONDS, settings.EXECUTOR_TICK_SECONDS,
        settings.POLLER_TICK_SECONDS, settings.MAX_CONCURRENT_WORKERS,
    )
    scheduler.start()
    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown(wait=True)


if __name__ == "__main__":
    main()
