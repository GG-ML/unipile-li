"""Campaign management API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.schemas import (
    AddTargetsRequest,
    AssignAccountsRequest,
    CampaignCreate,
    CampaignOut,
)
from app.db.database import get_db
from app.db.models import CampaignStatus, TaskStatus, UoCampaign, UoTask
from app.services import campaign_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignOut)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    campaign = campaign_service.create_campaign(db, payload.model_dump())
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("", response_model=list[CampaignOut])
def list_campaigns(organisation_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(UoCampaign)
    if organisation_code:
        query = query.filter(UoCampaign.organisation_code == organisation_code)
    return query.order_by(UoCampaign.id.desc()).all()


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get(db, campaign_id)
    return campaign


@router.post("/{campaign_id}/accounts")
def assign_accounts(campaign_id: int, payload: AssignAccountsRequest, db: Session = Depends(get_db)):
    campaign = _get(db, campaign_id)
    links = campaign_service.assign_accounts(
        db, campaign, payload.account_ids, payload.assigned_daily_limit
    )
    db.commit()
    return {"assigned": len(links), "account_ids": [l.account_id for l in links]}


@router.post("/{campaign_id}/targets")
def add_targets(campaign_id: int, payload: AddTargetsRequest, db: Session = Depends(get_db)):
    campaign = _get(db, campaign_id)
    result = campaign_service.add_targets(db, campaign, [t.model_dump() for t in payload.targets])
    db.commit()
    return result


@router.post("/{campaign_id}/start")
def start_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get(db, campaign_id)
    # Ensure any unassigned tasks get an account before running.
    campaign_service.rebalance_unassigned(db, campaign)
    campaign_service.set_status(db, campaign, CampaignStatus.RUNNING)
    db.commit()
    return {"status": "running", "campaign_id": campaign_id}


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = _get(db, campaign_id)
    campaign_service.set_status(db, campaign, CampaignStatus.PAUSED)
    db.commit()
    return {"status": "paused", "campaign_id": campaign_id}


@router.get("/{campaign_id}/stats")
def campaign_stats(campaign_id: int, db: Session = Depends(get_db)):
    _get(db, campaign_id)
    rows = (
        db.query(UoTask.status, func.count(UoTask.id))
        .filter(UoTask.campaign_id == campaign_id)
        .group_by(UoTask.status)
        .all()
    )
    by_status = {status: count for status, count in rows}
    total = sum(by_status.values())
    return {
        "campaign_id": campaign_id,
        "total_targets": total,
        "by_status": by_status,
        "sent": by_status.get(TaskStatus.INVITE_SENT, 0)
        + by_status.get(TaskStatus.ACCEPTED, 0)
        + by_status.get(TaskStatus.INITIAL_SENT, 0)
        + by_status.get(TaskStatus.REPLIED, 0)
        + by_status.get(TaskStatus.COMPLETED, 0),
        "accepted": by_status.get(TaskStatus.ACCEPTED, 0)
        + by_status.get(TaskStatus.INITIAL_SENT, 0)
        + by_status.get(TaskStatus.REPLIED, 0)
        + by_status.get(TaskStatus.COMPLETED, 0),
        "replied": by_status.get(TaskStatus.REPLIED, 0),
    }


def _get(db: Session, campaign_id: int) -> UoCampaign:
    campaign = db.query(UoCampaign).filter(UoCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign
