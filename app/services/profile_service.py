"""Profile resolution: check DB cache first, otherwise scrape via Unipile.

Mirrors the working fetch logic in /root/unipile-linkedin/outreach_profile_fetch.py
but without any AI. Used before sending a connection note or initial message to
ensure first_name / last_name are populated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import UoProfile, UoTask
from app.unipile.client import UnipileClient, UnipileError

logger = logging.getLogger(__name__)


def _extract_fields(profile: dict) -> dict:
    """Normalise a Unipile profile payload to our flat fields."""
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    full = (profile.get("name") or profile.get("full_name") or (first + " " + last)).strip()
    headline = (profile.get("headline") or "").strip()
    location = profile.get("location") or ""
    if isinstance(location, dict):
        location = location.get("name") or location.get("city") or ""

    company = ""
    experience = profile.get("experience") or profile.get("linkedin_experience") or []
    if experience and isinstance(experience, list):
        first_exp = experience[0] or {}
        company = first_exp.get("company") or first_exp.get("company_name") or ""

    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "headline": headline,
        "company": (company or "").strip(),
        "location": (location or "").strip(),
        "provider_id": profile.get("provider_id") or "",
        "public_identifier": profile.get("public_identifier") or "",
    }


def get_cached_profile(db: Session, public_identifier: str) -> Optional[UoProfile]:
    if not public_identifier:
        return None
    return (
        db.query(UoProfile)
        .filter(UoProfile.public_identifier == public_identifier)
        .first()
    )


def upsert_profile(db: Session, fields: dict, raw: dict) -> UoProfile:
    public_id = fields.get("public_identifier")
    existing = get_cached_profile(db, public_id) if public_id else None
    if existing:
        for key in ("first_name", "last_name", "full_name", "headline", "company", "location", "provider_id"):
            if fields.get(key):
                setattr(existing, key, fields[key])
        existing.raw_json = raw
        existing.updated_at = datetime.utcnow()
        return existing

    profile = UoProfile(
        public_identifier=public_id,
        provider_id=fields.get("provider_id"),
        first_name=fields.get("first_name"),
        last_name=fields.get("last_name"),
        full_name=fields.get("full_name"),
        headline=fields.get("headline"),
        company=fields.get("company"),
        location=fields.get("location"),
        raw_json=raw,
    )
    db.add(profile)
    return profile


def ensure_task_profile(
    db: Session,
    task: UoTask,
    unipile_account_id: str,
    client: UnipileClient | None = None,
) -> bool:
    """Ensure `task` has first_name/provider_id, scraping via Unipile if needed.

    Returns True if the task has a usable provider_id after this call.
    """
    # 1. Already have what we need on the task?
    if (task.first_name or "").strip() and (task.provider_id or "").strip():
        return True

    # 2. Try DB profile cache.
    cached = get_cached_profile(db, task.public_identifier)
    if cached and (cached.first_name or "").strip() and (cached.provider_id or "").strip():
        _apply_to_task(task, cached)
        return True

    # 3. Scrape via Unipile.
    identifier = task.public_identifier or task.provider_id
    if not identifier:
        task.last_error = "no identifier to scrape"
        return False

    client = client or UnipileClient()
    try:
        raw = client.fetch_profile(unipile_account_id, identifier)
    except UnipileError as exc:
        task.last_error = f"profile fetch failed: {exc.message[:200]}"
        logger.warning("Profile fetch failed for %s: %s", identifier, exc.message[:120])
        return bool((task.provider_id or "").strip())

    fields = _extract_fields(raw)
    profile = upsert_profile(db, fields, raw)
    _apply_to_task(task, profile)
    return bool((task.provider_id or "").strip())


def _apply_to_task(task: UoTask, profile: UoProfile) -> None:
    if profile.first_name and not (task.first_name or "").strip():
        task.first_name = profile.first_name
    if profile.last_name and not (task.last_name or "").strip():
        task.last_name = profile.last_name
    if profile.full_name and not (task.full_name or "").strip():
        task.full_name = profile.full_name
    if profile.headline and not (task.headline or "").strip():
        task.headline = profile.headline
    if profile.company and not (task.company or "").strip():
        task.company = profile.company
    if profile.location and not (task.location or "").strip():
        task.location = profile.location
    if profile.provider_id and not (task.provider_id or "").strip():
        task.provider_id = profile.provider_id
