"""Poller — runs a few times per day per account (default 2x) to:

  1. Detect accepted invitations (compare current relations against sent tasks).
  2. Send the templated initial message to newly accepted connections.
  3. Detect replies on initial/follow-up messages (reply => stop automation).
  4. Send the next follow-up when due and no reply was received.

Acceptance detection follows Unipile guidance: poll the relations list (recent
first) and match provider_ids against our INVITE_SENT tasks. Spacing is enforced
by the per-account poll times planned by the planner, plus a Redis poll lock.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.database import session_scope
from app.db.models import (
    AccountStatus,
    CampaignStatus,
    TaskStatus,
    UoAccount,
    UoCampaign,
    UoTask,
)
from app.scheduler import working_hours as wh
from app.services import counter_service
from app.services.logging_service import db_log
from app.services.profile_service import ensure_task_profile
from app.services.redis_lock import poll_lock, release_poll_lock
from app.services.template_service import build_context, render
from app.unipile.client import UnipileClient, UnipileError

logger = logging.getLogger(__name__)

FIRST_DEGREE = "FIRST_DEGREE"

MESSAGE_MAX_LEN = 1000
_executor = ThreadPoolExecutor(max_workers=settings.MAX_CONCURRENT_WORKERS)


def run_poller_tick() -> dict:
    """Run polls for any account whose scheduled poll time is due."""
    now = datetime.utcnow()
    due_account_ids: list[int] = []

    with session_scope() as db:
        accounts = (
            db.query(UoAccount)
            .filter(UoAccount.status == AccountStatus.CONNECTED)
            .all()
        )
        for account in accounts:
            poll_times = account.next_poll_times or []
            if any(_parse(t) <= now for t in poll_times):
                due_account_ids.append(account.id)

    if not due_account_ids:
        return {"polled": 0}

    polled = 0
    futures = {_executor.submit(_poll_account_safe, aid): aid for aid in due_account_ids}
    for fut in as_completed(futures):
        try:
            if fut.result():
                polled += 1
        except Exception as exc:  # pragma: no cover
            logger.exception("Poll failed for account %s: %s", futures[fut], exc)

    if polled:
        logger.info("Poller: polled %s accounts", polled)
    return {"polled": polled}


def _poll_account_safe(account_id: int) -> bool:
    if not poll_lock(account_id, ttl=1800):
        return False
    try:
        return _poll_account(account_id)
    finally:
        release_poll_lock(account_id)


def _poll_account(account_id: int) -> bool:
    now = datetime.utcnow()
    with session_scope() as db:
        account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
        if not account or account.status != AccountStatus.CONNECTED or not account.unipile_account_id:
            return False

        # Consume due poll times so we don't re-run this poll until next planned slot.
        remaining_polls = [t for t in (account.next_poll_times or []) if _parse(t) > now]
        account.next_poll_times = remaining_polls
        account.last_poll_at = now

        client = UnipileClient()
        unipile_id = account.unipile_account_id

        # --- 1. Detect accepted invitations -------------------------------
        connected_provider_ids = _fetch_connected_provider_ids(client, unipile_id)

        invite_sent_tasks = (
            db.query(UoTask)
            .join(UoCampaign, UoCampaign.id == UoTask.campaign_id)
            .filter(UoTask.account_id == account.id, UoTask.status == TaskStatus.INVITE_SENT)
            .filter(UoCampaign.status == CampaignStatus.RUNNING)
            .all()
        )
        for task in invite_sent_tasks:
            if task.provider_id and task.provider_id in connected_provider_ids:
                task.status = TaskStatus.ACCEPTED
                task.accepted_at = now
                db_log("info", "invite.accepted", account_id=account.id, task_id=task.id)
                continue

            # Fallback: the connection may be old and not appear in the recent
            # relations list. If the scraped profile says FIRST_DEGREE, treat it
            # as accepted so the initial message can be sent.
            try:
                if ensure_task_profile(db, task, unipile_id, client) and task.network_distance == FIRST_DEGREE:
                    task.status = TaskStatus.ACCEPTED
                    task.accepted_at = now
                    db_log("info", "invite.accepted_from_profile", account_id=account.id, task_id=task.id,
                           data={"connected_at": task.connected_at.isoformat() if task.connected_at else None})
            except Exception:
                logger.exception("Profile check failed for INVITE_SENT task %s", task.id)

        # --- 2. Send initial message to accepted (no message yet) ----------
        accepted_tasks = (
            db.query(UoTask)
            .join(UoCampaign, UoCampaign.id == UoTask.campaign_id)
            .filter(UoTask.account_id == account.id, UoTask.status == TaskStatus.ACCEPTED)
            .filter(UoCampaign.status == CampaignStatus.RUNNING)
            .all()
        )
        for task in accepted_tasks:
            _send_initial_message(db, account, task, client)

        # --- 3 & 4. Reply detection + follow-ups ---------------------------
        active_tasks = (
            db.query(UoTask)
            .join(UoCampaign, UoCampaign.id == UoTask.campaign_id)
            .filter(
                UoTask.account_id == account.id,
                UoTask.status.in_([TaskStatus.INITIAL_SENT]),
                UoCampaign.status == CampaignStatus.RUNNING,
            )
            .all()
        )
        for task in active_tasks:
            _handle_replies_and_followups(db, account, task, client, now)

        db_log("info", "poller.completed", account_id=account.id,
               data={"connected": len(connected_provider_ids),
                     "checked_invites": len(invite_sent_tasks),
                     "accepted": len(accepted_tasks)})
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fetch_connected_provider_ids(client: UnipileClient, unipile_id: str, max_pages: int = 5) -> set:
    """Fetch recent relations (paginated) and return their provider_ids."""
    provider_ids: set = set()
    cursor = ""
    for _ in range(max_pages):
        try:
            data = client.get_relations(unipile_id, limit=100, cursor=cursor)
        except UnipileError as exc:
            logger.warning("get_relations failed: %s", exc.message[:120])
            break
        items = data.get("items", []) or data.get("relations", [])
        for rel in items:
            pid = rel.get("member_id") or rel.get("provider_id") or rel.get("id")
            if pid:
                provider_ids.add(pid)
        cursor = data.get("cursor") or ""
        if not cursor or not items:
            break
    return provider_ids


def _send_initial_message(db, account: UoAccount, task: UoTask, client: UnipileClient) -> None:
    campaign = db.query(UoCampaign).filter(UoCampaign.id == task.campaign_id).first()
    if not campaign or campaign.status != CampaignStatus.RUNNING:
        return
    if not campaign.initial_message_template:
        # No initial message configured — consider the task complete on acceptance.
        task.status = TaskStatus.COMPLETED
        return

    # Ensure we have a first name before personalising.
    ensure_task_profile(db, task, account.unipile_account_id, client)
    # A cached chat can be messaged without a profile provider ID. For a new
    # chat, however, never call Unipile with a missing recipient identifier;
    # leave the task accepted so a later poll can resolve the profile safely.
    if not task.chat_id and not (task.provider_id or "").strip():
        task.last_error = "initial message deferred: missing provider_id after profile fetch"
        db_log(
            "warning",
            "initial.provider_id_deferred",
            account_id=account.id,
            task_id=task.id,
        )
        return
    text = render(campaign.initial_message_template, build_context(task), MESSAGE_MAX_LEN)
    if not text:
        return

    today = wh.local_date_str(account)
    try:
        if task.chat_id:
            result = client.send_message(task.chat_id, text)
        else:
            result = client.create_chat_with_message(account.unipile_account_id, task.provider_id, text)
            task.chat_id = result.get("chat_id") or result.get("id") or task.chat_id
    except UnipileError as exc:
        # Common when LinkedIn blocks messaging until the invite note is read.
        task.last_error = f"initial msg failed: {exc.message[:200]}"
        db_log("warning", "initial.deferred", exc.message[:300], account_id=account.id, task_id=task.id)
        return

    now = datetime.utcnow()
    task.status = TaskStatus.INITIAL_SENT
    task.initial_sent_at = now
    task.last_message_at = now
    counter_service.increment_messages(db, account.id, today)
    _schedule_next_followup(campaign, task, from_time=now, index=0)
    db_log("info", "initial.sent", account_id=account.id, task_id=task.id)


def _handle_replies_and_followups(db, account: UoAccount, task: UoTask,
                                  client: UnipileClient, now: datetime) -> None:
    campaign = db.query(UoCampaign).filter(UoCampaign.id == task.campaign_id).first()

    if not campaign or campaign.status != CampaignStatus.RUNNING:
        return

    # If a reply was already received, don't send follow-ups.
    if task.reply_received:
        if task.status != TaskStatus.REPLIED:
            task.status = TaskStatus.REPLIED
            db_log("info", "reply.already_received", account_id=account.id, task_id=task.id)
        return

    # Detect replies first.
    replied = _check_for_reply(client, task)
    if replied:
        task.status = TaskStatus.REPLIED
        task.reply_received = True
        task.last_reply_at = now
        db_log("info", "reply.received", account_id=account.id, task_id=task.id)
        return

    # No reply -> send follow-up if enabled and due.
    if not campaign.followup_enabled:
        return
    templates = campaign.followup_templates or []
    sent = task.followup_count or 0
    if sent >= len(templates):
        task.status = TaskStatus.COMPLETED
        return
    if not task.next_followup_at or task.next_followup_at > now:
        return

    text = render(templates[sent], build_context(task), MESSAGE_MAX_LEN)
    if not text:
        return

    today = wh.local_date_str(account)
    try:
        if task.chat_id:
            client.send_message(task.chat_id, text)
        else:
            result = client.create_chat_with_message(account.unipile_account_id, task.provider_id, text)
            task.chat_id = result.get("chat_id") or result.get("id") or task.chat_id
    except UnipileError as exc:
        task.last_error = f"followup failed: {exc.message[:200]}"
        db_log("warning", "followup.failed", exc.message[:300], account_id=account.id, task_id=task.id)
        return

    task.followup_count = sent + 1
    task.last_followup_at = now
    task.last_message_at = now
    counter_service.increment_messages(db, account.id, today)
    _schedule_next_followup(campaign, task, from_time=now, index=task.followup_count)
    db_log("info", "followup.sent", account_id=account.id, task_id=task.id,
           data={"followup_number": task.followup_count})

    if task.followup_count >= len(templates):
        task.status = TaskStatus.COMPLETED


def _check_for_reply(client: UnipileClient, task: UoTask) -> bool:
    """Return True if the target sent an inbound message after our initial message."""
    if not task.chat_id or not task.initial_sent_at:
        # We haven't sent an initial message yet, so nothing counts as a reply.
        return False
    try:
        messages = client.get_chat_messages(task.chat_id, limit=20)
    except UnipileError:
        return False

    for msg in messages:
        is_sender = msg.get("is_sender")
        # Any inbound message in the conversation is a hard stop. LinkedIn can
        # return conversation history with timestamps that predate our local
        # initial_sent_at (timezone/session migration), and sending a follow-up
        # in that case is unsafe.
        if is_sender is False or is_sender in (0, "0", "false", "False"):
            return True
        # Fallback: explicit direction fields.
        if msg.get("direction") == "inbound" or msg.get("from_me") is False:
            return True
    return False


def _schedule_next_followup(campaign: UoCampaign, task: UoTask, from_time: datetime, index: int) -> None:
    """Set task.next_followup_at based on the campaign interval array."""
    if not campaign.followup_enabled:
        task.next_followup_at = None
        return
    intervals = campaign.followup_interval_days or []
    templates = campaign.followup_templates or []
    if index >= len(templates):
        task.next_followup_at = None
        return
    days = intervals[index] if index < len(intervals) else (intervals[-1] if intervals else 3)
    task.next_followup_at = from_time + timedelta(days=days)


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return datetime.max
