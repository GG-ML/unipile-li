"""Campaign management: creation, account assignment, target ingestion."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import (
    AccountStatus,
    CampaignStatus,
    TaskStatus,
    UoAccount,
    UoCampaign,
    UoCampaignAccount,
    UoTask,
)

logger = logging.getLogger(__name__)


def extract_public_id(value: str) -> str:
    """Extract a LinkedIn public identifier from a URL or return as-is."""
    value = (value or "").strip()
    if not value:
        return ""
    if "/" not in value and not value.startswith("http"):
        return value
    path = urlparse(value).path
    match = re.search(r"/in/([^/]+)/?", path)
    if match:
        return match.group(1)
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else value


def create_campaign(db: Session, payload: dict) -> UoCampaign:
    campaign = UoCampaign(
        organisation_code=payload["organisation_code"],
        name=payload["name"],
        status=CampaignStatus.DRAFT,
        include_note=payload.get("include_note", True),
        connection_note_template=payload.get("connection_note_template"),
        initial_message_template=payload.get("initial_message_template"),
        followup_enabled=payload.get("followup_enabled", False),
        followup_templates=payload.get("followup_templates") or [],
        followup_interval_days=payload.get("followup_interval_days") or [],
        daily_limit_per_account=payload.get("daily_limit_per_account", 25),
        min_delay_seconds=payload.get("min_delay_seconds", 180),
        max_delay_seconds=payload.get("max_delay_seconds", 600),
    )
    db.add(campaign)
    db.flush()
    return campaign


def assign_accounts(db: Session, campaign: UoCampaign, account_ids: list[int],
                    assigned_daily_limit: int | None = None) -> list[UoCampaignAccount]:
    created = []
    for account_id in account_ids:
        account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
        if not account:
            continue
        existing = (
            db.query(UoCampaignAccount)
            .filter(
                UoCampaignAccount.campaign_id == campaign.id,
                UoCampaignAccount.account_id == account_id,
            )
            .first()
        )
        if existing:
            existing.status = "active"
            if assigned_daily_limit is not None:
                existing.assigned_daily_limit = assigned_daily_limit
            created.append(existing)
            continue
        link = UoCampaignAccount(
            campaign_id=campaign.id,
            account_id=account_id,
            assigned_daily_limit=assigned_daily_limit,
            status="active",
        )
        db.add(link)
        created.append(link)
    db.flush()
    return created


def add_targets(db: Session, campaign: UoCampaign, targets: list[dict]) -> dict:
    """Bulk add targets to a campaign, round-robin assigning across active accounts.

    Each target dict may contain: linkedin_url / public_identifier, first_name,
    last_name, headline, company.
    """
    active_account_ids = [
        ca.account_id
        for ca in db.query(UoCampaignAccount)
        .filter(UoCampaignAccount.campaign_id == campaign.id, UoCampaignAccount.status == "active")
        .all()
    ]
    # Only assign to connected accounts.
    if active_account_ids:
        connected = {
            a.id
            for a in db.query(UoAccount)
            .filter(UoAccount.id.in_(active_account_ids), UoAccount.status == AccountStatus.CONNECTED)
            .all()
        }
        active_account_ids = [aid for aid in active_account_ids if aid in connected]

    added = 0
    skipped = 0
    rr = 0
    for t in targets:
        public_id = extract_public_id(t.get("public_identifier") or t.get("linkedin_url") or "")
        if not public_id:
            skipped += 1
            continue
        exists = (
            db.query(UoTask.id)
            .filter(UoTask.campaign_id == campaign.id, UoTask.public_identifier == public_id)
            .first()
        )
        if exists:
            skipped += 1
            continue

        account_id = None
        if active_account_ids:
            account_id = active_account_ids[rr % len(active_account_ids)]
            rr += 1

        task = UoTask(
            campaign_id=campaign.id,
            account_id=account_id,
            public_identifier=public_id,
            profile_url=t.get("linkedin_url") or f"https://www.linkedin.com/in/{public_id}/",
            first_name=(t.get("first_name") or "").strip() or None,
            last_name=(t.get("last_name") or "").strip() or None,
            full_name=(t.get("full_name") or "").strip() or None,
            headline=(t.get("headline") or "").strip() or None,
            company=(t.get("company") or "").strip() or None,
            status=TaskStatus.PENDING,
        )
        db.add(task)
        added += 1

    db.flush()
    return {"added": added, "skipped": skipped}


def rebalance_unassigned(db: Session, campaign: UoCampaign) -> int:
    """Assign any tasks without an account to active connected accounts."""
    account_ids = [
        ca.account_id
        for ca in db.query(UoCampaignAccount)
        .filter(UoCampaignAccount.campaign_id == campaign.id, UoCampaignAccount.status == "active")
        .all()
    ]
    if not account_ids:
        return 0
    connected = [
        a.id
        for a in db.query(UoAccount)
        .filter(UoAccount.id.in_(account_ids), UoAccount.status == AccountStatus.CONNECTED)
        .all()
    ]
    if not connected:
        return 0
    unassigned = (
        db.query(UoTask)
        .filter(UoTask.campaign_id == campaign.id, UoTask.account_id.is_(None))
        .all()
    )
    for i, task in enumerate(unassigned):
        task.account_id = connected[i % len(connected)]
    db.flush()
    return len(unassigned)


def set_status(db: Session, campaign: UoCampaign, status: str) -> None:
    campaign.status = status
    campaign.updated_at = datetime.utcnow()
    db.flush()
