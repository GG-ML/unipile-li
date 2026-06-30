"""Executor — sends connection invites whose scheduled slot is due.

Runs frequently (EXECUTOR_TICK_SECONDS). For each account with a due task it
acquires a Redis lock and sends exactly one invite (slots are pre-spaced by the
planner), guaranteeing no account is double-operated and load is balanced
across workers.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from app.config import settings
from app.db.database import session_scope
from app.db.models import AccountStatus, AccountTier, TaskStatus, UoAccount, UoCampaign, UoTask
from app.scheduler import working_hours as wh
from app.services import counter_service
from app.services.logging_service import db_log
from app.services.profile_service import ensure_task_profile
from app.services.redis_lock import account_lock
from app.services.template_service import build_context, render
from app.unipile.client import UnipileClient, UnipileError

logger = logging.getLogger(__name__)

NOTE_MAX_LEN = 300

_executor = ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENT_WORKERS)


def run_executor_tick() -> dict:
    """Find accounts with due invites and dispatch one send per account."""
    now = datetime.utcnow()
    with session_scope() as db:
        account_ids = [
            row[0]
            for row in db.query(UoTask.account_id)
            .filter(UoTask.status == TaskStatus.SCHEDULED, UoTask.scheduled_at <= now)
            .filter(UoTask.account_id.isnot(None))
            .distinct()
            .all()
        ]

    if not account_ids:
        return {"sent": 0, "accounts": 0}

    sent = 0
    futures = {_executor.submit(_process_account_due_invite, aid): aid for aid in account_ids}
    for fut in as_completed(futures):
        try:
            if fut.result():
                sent += 1
        except Exception as exc:  # pragma: no cover
            logger.exception("Invite send failed for account %s: %s", futures[fut], exc)

    if sent:
        logger.info("Executor: sent %s invites across %s accounts", sent, len(account_ids))
    return {"sent": sent, "accounts": len(account_ids)}


def _process_account_due_invite(account_id: int) -> bool:
    with account_lock(account_id, ttl=300) as acquired:
        if not acquired:
            return False
        return _send_one_invite(account_id)


def _send_one_invite(account_id: int) -> bool:
    now = datetime.utcnow()
    with session_scope() as db:
        account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
        if not account or account.status != AccountStatus.CONNECTED or not account.unipile_account_id:
            return False

        # Respect working hours at execution time too.
        if not wh.is_within_working_hours(account):
            return False

        today = wh.local_date_str(account)
        daily_limit = account.daily_connect_limit or settings.DEFAULT_DAILY_CONNECT_LIMIT
        if counter_service.invites_sent_today(db, account.id, today) >= daily_limit:
            return False

        task = (
            db.query(UoTask)
            .filter(
                UoTask.account_id == account.id,
                UoTask.status == TaskStatus.SCHEDULED,
                UoTask.scheduled_at <= now,
            )
            .order_by(UoTask.scheduled_at.asc())
            .first()
        )
        if not task:
            return False

        campaign = db.query(UoCampaign).filter(UoCampaign.id == task.campaign_id).first()
        client = UnipileClient()

        # Ensure first_name / provider_id (scrape if missing).
        ensure_task_profile(db, task, account.unipile_account_id, client)
        if not (task.provider_id or "").strip():
            task.status = TaskStatus.FAILED
            task.last_error = "missing provider_id after profile fetch"
            db_log("error", "invite.no_provider_id", account_id=account.id, task_id=task.id)
            return False

        # Render note from template only for paid LinkedIn accounts. Basic (free)
        # accounts are limited to 200-character notes and can quickly hit the
        # LinkedIn monthly note cap, so we send without a note for them.
        note = ""
        can_use_note = account.linkedin_tier in (
            AccountTier.PREMIUM,
            AccountTier.SALES_NAVIGATOR,
        )
        if can_use_note and campaign and campaign.include_note and campaign.connection_note_template:
            note = render(campaign.connection_note_template, build_context(task), NOTE_MAX_LEN)
            task.rendered_note = note

        task.attempts = (task.attempts or 0) + 1
        try:
            result = client.send_invitation(account.unipile_account_id, task.provider_id, note)
        except UnipileError as exc:
            task.last_error = exc.message[:1000]
            # Permanent-ish failures: mark failed; otherwise leave scheduled for retry next tick.
            if exc.status_code in (400, 404, 422):
                task.status = TaskStatus.FAILED
            db_log("error", "invite.failed", exc.message[:500], account_id=account.id, task_id=task.id)
            return False

        task.invitation_id = result.get("invitation_id") or result.get("id")
        task.status = TaskStatus.INVITE_SENT
        task.invite_sent_at = now
        task.scheduled_at = None
        counter_service.increment_invites(db, account.id, today)
        db_log("info", "invite.sent", account_id=account.id, task_id=task.id,
               data={"invitation_id": task.invitation_id, "note_len": len(note)})
        return True
