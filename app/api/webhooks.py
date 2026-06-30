"""Optional Unipile webhook receiver.

Polling (the poller) is the primary mechanism per the user's requirement, but
this endpoint lets Unipile push `new_relation` events to mark acceptances
faster. It is purely additive — the poller remains the source of truth.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from app.db.database import get_db, session_scope
from app.db.models import AccountStatus, TaskStatus, UoAccount, UoTask
from app.services.logging_service import db_log

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/unipile")
async def unipile_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event")
    unipile_account_id = payload.get("account_id")

    if event == "new_relation" and unipile_account_id:
        provider_id = payload.get("user_provider_id")
        public_id = payload.get("user_public_identifier")
        _mark_accepted(unipile_account_id, provider_id, public_id)

    db_log("info", "webhook.received", event or "unknown",
           data={"account": unipile_account_id})
    return {"ok": True}


def _mark_accepted(unipile_account_id: str, provider_id: str | None, public_id: str | None) -> None:
    with session_scope() as db:
        account = (
            db.query(UoAccount)
            .filter(UoAccount.unipile_account_id == unipile_account_id)
            .first()
        )
        if not account:
            return
        query = db.query(UoTask).filter(
            UoTask.account_id == account.id,
            UoTask.status == TaskStatus.INVITE_SENT,
        )
        task = None
        if provider_id:
            task = query.filter(UoTask.provider_id == provider_id).first()
        if task is None and public_id:
            task = query.filter(UoTask.public_identifier == public_id).first()
        if task:
            task.status = TaskStatus.ACCEPTED
            task.accepted_at = datetime.utcnow()
            db_log("info", "webhook.accepted", account_id=account.id, task_id=task.id)
