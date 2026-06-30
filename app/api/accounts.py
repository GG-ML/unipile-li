"""Account linking API.

POST /connect returns immediately with an account_id. The long-running Unipile
credential flow runs as a FastAPI BackgroundTask, so the HTTP response never waits
for Unipile (which can take >60 seconds). The frontend polls
GET /{account_id}/status and submits checkpoint codes via
POST /{account_id}/checkpoint.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.schemas import (
    AccountConnectRequest,
    AccountConnectResponse,
    AccountOut,
    AccountScheduleUpdate,
    AccountStatusResponse,
    CheckpointRequest,
    CheckpointSubmittedResponse,
)
from app.db.database import get_db
from app.db.models import UoAccount
from app.services import account_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.post("/connect", response_model=AccountConnectResponse)
async def connect_account(
    payload: AccountConnectRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue a LinkedIn account link and return an ID immediately.

    The actual Unipile credential flow runs in the background. Poll
    GET /api/accounts/{account_id}/status for progress and submit any checkpoint
    codes via POST /api/accounts/{account_id}/checkpoint.
    """
    account = account_service.queue_link_account(db, payload.model_dump())
    background_tasks.add_task(account_service.process_link_account, account.id)
    return {
        "status": "started",
        "account_id": account.id,
        "message": "Account linking started in the background. Poll GET /api/accounts/{account_id}/status.",
    }


@router.post("/{account_id}/checkpoint", response_model=CheckpointSubmittedResponse)
async def submit_checkpoint(account_id: int, payload: CheckpointRequest, db: Session = Depends(get_db)):
    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="account not found")

    result = account_service.submit_checkpoint_code(db, account_id, payload.code or "")
    # A failed result that is not terminal (e.g. no pending checkpoint at all) is a
    # client error. Terminal failures (done=True) are returned with 200 so the
    # frontend can read the final outcome instead of treating it as a request error.
    if result["status"] == "failed" and not result.get("done"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/{account_id}/status", response_model=AccountStatusResponse)
def get_account_status(account_id: int, db: Session = Depends(get_db)):
    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return account_service.get_account_status(db, account_id)


@router.get("", response_model=list[AccountOut])
def list_accounts(organisation_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(UoAccount)
    if organisation_code:
        query = query.filter(UoAccount.organisation_code == organisation_code)
    return query.order_by(UoAccount.id.desc()).all()


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db)):
    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, payload: AccountScheduleUpdate, db: Session = Depends(get_db)):
    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(account, key, value)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    if account.unipile_account_id:
        from app.unipile.client import UnipileClient, UnipileError
        try:
            await run_in_threadpool(UnipileClient().delete_account, account.unipile_account_id)
        except UnipileError as exc:
            logger.warning("Unipile delete failed: %s", exc.message[:120])
    db.delete(account)
    db.commit()
    return {"status": "deleted", "account_id": account_id}
