"""Daily planner — distributes invites across each account's working window.

Runs periodically. For every connected account, once per local day:
  * Computes the remaining daily connect allowance.
  * Picks that many PENDING tasks and assigns each a random scheduled_at
    inside the remaining working window (random intervals).
  * Schedules N (default 2) acceptance-poll times spread across the window.

Designed to scale to hundreds of accounts: all queries are per-account and
bounded by the daily limit, and planning is idempotent per local day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.db.database import session_scope
from app.db.models import AccountStatus, TaskStatus, UoAccount, UoTask
from app.scheduler import working_hours as wh
from app.services import counter_service
from app.services.logging_service import db_log

logger = logging.getLogger(__name__)


def plan_all_accounts() -> dict:
    """Plan today's schedule for every connected account that needs it."""
    planned_accounts = 0
    scheduled_tasks = 0
    with session_scope() as db:
        account_ids = [
            a.id
            for a in db.query(UoAccount.id)
            .filter(UoAccount.status == AccountStatus.CONNECTED)
            .all()
        ]

    for account_id in account_ids:
        try:
            result = _plan_account(account_id)
            if result["planned"]:
                planned_accounts += 1
                scheduled_tasks += result["scheduled"]
        except Exception as exc:  # pragma: no cover
            logger.exception("Planning failed for account %s: %s", account_id, exc)

    if planned_accounts:
        logger.info("Planner: %s accounts planned, %s tasks scheduled", planned_accounts, scheduled_tasks)
    return {"planned_accounts": planned_accounts, "scheduled_tasks": scheduled_tasks}


def _plan_account(account_id: int) -> dict:
    with session_scope() as db:
        account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
        if not account or account.status != AccountStatus.CONNECTED:
            return {"planned": False, "scheduled": 0}

        today = wh.local_date_str(account)
        if account.last_planned_date == today:
            return {"planned": False, "scheduled": 0}

        window = wh.working_window_today(account)
        if window is None:
            # Not a working day — still mark as planned so we don't re-check constantly.
            account.last_planned_date = today
            account.next_poll_times = []
            return {"planned": False, "scheduled": 0}

        start_utc, end_utc = window
        now_utc = datetime.now(timezone.utc)

        # Remaining daily allowance.
        daily_limit = account.daily_connect_limit or settings.DEFAULT_DAILY_CONNECT_LIMIT
        already_sent = counter_service.invites_sent_today(db, account.id, today)
        remaining = max(0, daily_limit - already_sent)

        scheduled = 0
        if remaining > 0:
            tasks = (
                db.query(UoTask)
                .filter(UoTask.account_id == account.id, UoTask.status == TaskStatus.PENDING)
                .order_by(UoTask.id.asc())
                .limit(remaining)
                .all()
            )
            if tasks:
                times = wh.random_times_in_window(
                    start_utc, end_utc, len(tasks),
                    not_before=now_utc,
                    min_gap_seconds=max(30, account.min_delay_seconds or 180),
                )
                for task, when in zip(tasks, times):
                    task.scheduled_at = when.replace(tzinfo=None)  # store naive UTC
                    task.status = TaskStatus.SCHEDULED
                    scheduled += 1

        # Schedule acceptance polls for today (default 2x).
        poll_count = settings.ACCEPTED_CHECKS_PER_DAY
        poll_times = wh.random_times_in_window(
            start_utc, end_utc, poll_count, not_before=now_utc, min_gap_seconds=3600
        )
        account.next_poll_times = [t.replace(tzinfo=None).isoformat() for t in poll_times]
        account.last_planned_date = today

        db_log("info", "planner.account_planned", account_id=account.id,
               data={"scheduled": scheduled, "remaining": remaining, "polls": len(poll_times)})
        return {"planned": True, "scheduled": scheduled}
