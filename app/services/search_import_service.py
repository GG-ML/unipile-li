"""LinkedIn search URL -> campaign leads.

Handles copy/pasted URLs from Classic LinkedIn, Sales Navigator search results,
and Sales Navigator saved searches. Paginates through Unipile results, spaces
requests with human-like delays, and respects LinkedIn daily import limits
(1,000/day for Classic, 2,500/day for Sales Navigator).
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.database import session_scope
from app.db.models import (
    AccountStatus,
    TaskStatus,
    UoAccount,
    UoCampaign,
    UoProfile,
    UoSearchImport,
    UoTask,
)
from app.services import campaign_service, counter_service, profile_service
from app.services.logging_service import db_log
from app.unipile.client import UnipileClient, UnipileError

logger = logging.getLogger(__name__)

_IMPORT_TYPE_MAP = {
    "linkedin_classic_search": "linkedin_classic_search",
    "sales_navigator_search": "sales_navigator_search",
    "sales_navigator_saved_search": "sales_navigator_saved_search",
}

_DAILY_LIMITS = {
    "linkedin_classic_search": 1000,
    "sales_navigator_search": 2500,
    "sales_navigator_saved_search": 2500,
}

# Human-like delay between paginated search requests (seconds).
_MIN_PAGE_DELAY = 1.5
_MAX_PAGE_DELAY = 4.0


class SearchImportError(Exception):
    pass


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _extract_public_id_from_url(url: str) -> str:
    """Best-effort extraction of a LinkedIn public identifier from a profile URL."""
    if not url:
        return ""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    # /in/<identifier>/
    match = re.search(r"/in/([^/]+)/?", path)
    if match:
        return match.group(1).strip()
    # /sales/lead/<id>,<name>,... -> not a public id, ignore
    parts = [p for p in path.split("/") if p]
    if parts:
        return parts[-1].strip()
    return ""


def _search_item_to_fields(item: dict) -> dict:
    """Convert a Unipile search item into flat profile/task fields."""
    item_type = (item.get("type") or "").upper()
    if item_type == "PEOPLE":
        first = (item.get("first_name") or "").strip()
        last = (item.get("last_name") or "").strip()
        name = (item.get("name") or "").strip()
        full = name or (f"{first} {last}").strip()
        public_id = (item.get("public_identifier") or "").strip()
        profile_url = item.get("public_profile_url") or item.get("profile_url") or ""
        if not public_id:
            public_id = _extract_public_id_from_url(profile_url)
        # Company from current_positions if available.
        company = ""
        positions = item.get("current_positions") or []
        if positions and isinstance(positions, list):
            company = (positions[0].get("company") or "").strip()
        return {
            "type": "PEOPLE",
            "public_identifier": public_id,
            "provider_id": (item.get("id") or "").strip(),
            "first_name": first,
            "last_name": last,
            "full_name": full,
            "headline": (item.get("headline") or "").strip(),
            "company": company,
            "location": (item.get("location") or "").strip(),
            "profile_url": profile_url or (f"https://www.linkedin.com/in/{public_id}/" if public_id else ""),
            "raw": item,
        }

    if item_type == "COMPANY":
        # Not a direct outreach lead; still useful to cache.
        return {
            "type": "COMPANY",
            "public_identifier": (item.get("name") or "").strip().lower().replace(" ", "-"),
            "provider_id": (item.get("id") or "").strip(),
            "full_name": (item.get("name") or "").strip(),
            "headline": (item.get("summary") or "")[:500],
            "company": "",
            "location": (item.get("location") or "").strip(),
            "profile_url": item.get("profile_url") or "",
            "raw": item,
        }

    return {"type": "UNKNOWN", "raw": item}


def _upsert_search_profile(db: Session, fields: dict) -> UoProfile | None:
    """Cache a search result in the profile table."""
    public_id = fields.get("public_identifier")
    if not public_id:
        return None
    return profile_service.upsert_profile(db, fields, fields.get("raw") or {})


def _create_task_from_fields(
    db: Session, campaign_id: int, account_id: int, fields: dict
) -> bool:
    """Create a UoTask for a person result if it does not already exist in the campaign."""
    if fields.get("type") != "PEOPLE":
        return False
    public_id = fields.get("public_identifier")
    if not public_id:
        return False

    exists = (
        db.query(UoTask.id)
        .filter(UoTask.campaign_id == campaign_id, UoTask.public_identifier == public_id)
        .first()
    )
    if exists:
        return False

    task = UoTask(
        campaign_id=campaign_id,
        account_id=account_id,
        public_identifier=public_id,
        provider_id=fields.get("provider_id") or None,
        profile_url=fields.get("profile_url") or f"https://www.linkedin.com/in/{public_id}/",
        first_name=fields.get("first_name") or None,
        last_name=fields.get("last_name") or None,
        full_name=fields.get("full_name") or None,
        headline=fields.get("headline") or None,
        company=fields.get("company") or None,
        location=fields.get("location") or None,
        status=TaskStatus.PENDING,
    )
    db.add(task)
    return True


def queue_search_import(
    db: Session,
    campaign_id: int,
    account_id: int,
    source_url: str,
    import_type: str,
    target_count: int | None = None,
) -> UoSearchImport:
    """Create a search import record and return it."""
    normalised_type = _IMPORT_TYPE_MAP.get(import_type)
    if not normalised_type:
        raise SearchImportError(f"unsupported import_type: {import_type}")

    account = db.query(UoAccount).filter(UoAccount.id == account_id).first()
    if not account:
        raise SearchImportError("account not found")
    if account.status != AccountStatus.CONNECTED:
        raise SearchImportError("account is not connected")
    if not account.unipile_account_id:
        raise SearchImportError("account has no Unipile ID")

    campaign = db.query(UoCampaign).filter(UoCampaign.id == campaign_id).first()
    if not campaign:
        raise SearchImportError("campaign not found")

    # Ensure the account is assigned to this campaign so the scheduler can use it.
    campaign_service.assign_accounts(db, campaign, [account_id])

    daily_limit = _DAILY_LIMITS[normalised_type]
    target_count = max(0, target_count or daily_limit)
    import_record = UoSearchImport(
        account_id=account_id,
        campaign_id=campaign_id,
        import_type=normalised_type,
        source_url=source_url,
        status="pending",
        daily_limit=daily_limit,
        target_count=min(target_count, daily_limit),
    )
    db.add(import_record)
    db.flush()
    return import_record


def process_search_import(import_id: int) -> None:
    """Background task: paginate a LinkedIn search URL and import leads."""
    logger.info("Starting search import id=%s", import_id)
    try:
        with session_scope() as db:
            import_record = db.get(UoSearchImport, import_id)
            if not import_record:
                logger.error("Search import %s not found", import_id)
                return
            import_record.status = "running"

        _run_import(import_id)
    except Exception as exc:
        logger.exception("Search import %s failed", import_id)
        try:
            with session_scope() as db:
                import_record = db.get(UoSearchImport, import_id)
                if import_record:
                    import_record.status = "failed"
                    import_record.last_error = str(exc)[:1000]
        except Exception:
            pass


def _run_import(import_id: int) -> None:
    """Core pagination loop."""
    with session_scope() as db:
        import_record = db.get(UoSearchImport, import_id)
        if not import_record:
            return
        account_id = import_record.account_id
        campaign_id = import_record.campaign_id
        source_url = import_record.source_url
        import_type = import_record.import_type
        daily_limit = import_record.daily_limit or _DAILY_LIMITS[import_type]
        target_count = import_record.target_count or daily_limit

        account = db.get(UoAccount, account_id)
        if not account or not account.unipile_account_id:
            import_record.status = "failed"
            import_record.last_error = "account not connected or missing unipile id"
            return
        unipile_account_id = account.unipile_account_id

        # How many profiles can still be imported today for this account?
        today = _today()
        already_imported = counter_service.search_imports_today(db, account_id, today)
        remaining_today = max(0, daily_limit - already_imported)
        if remaining_today <= 0:
            import_record.status = "partial"
            import_record.last_error = f"daily import limit reached ({daily_limit})"
            return

        # Respect both the daily cap and the per-import cap.
        import_limit = min(remaining_today, target_count)
        if import_limit <= 0:
            import_record.status = "partial"
            import_record.last_error = "import target reached for this run"
            return

    client = UnipileClient()
    cursor: str | None = None
    total_seen = 0
    imported = 0
    page = 0
    stopped_by_limit = False
    last_error = ""

    while imported < import_limit:
        try:
            data = client.search_linkedin(
                unipile_account_id,
                url=source_url,
                cursor=cursor,
                limit=50 if import_type == "linkedin_classic_search" else 100,
            )
        except UnipileError as exc:
            last_error = exc.message[:1000]
            logger.error("Unipile search error for import %s: %s", import_id, exc.message[:200])
            break

        items = data.get("items") or []
        paging = data.get("paging") or {}
        total_count = paging.get("total_count")
        if page == 0 and total_count is not None:
            total_seen = total_count

        for item in items:
            if imported >= import_limit:
                stopped_by_limit = True
                break
            fields = _search_item_to_fields(item)
            if fields.get("type") != "PEOPLE":
                continue

            with session_scope() as db:
                import_record = db.get(UoSearchImport, import_id)
                _upsert_search_profile(db, fields)
                created = _create_task_from_fields(db, campaign_id, account_id, fields)
                if created:
                    imported += 1
                    counter_service.increment_search_imports(db, account_id, today, by=1)
                    import_record.imported_count = imported
                import_record.total_results = total_seen
                import_record.cursor = data.get("cursor")

        page += 1
        cursor = data.get("cursor")
        if not cursor or not items:
            break

        # Human-like spacing before the next page.
        time.sleep(random.uniform(_MIN_PAGE_DELAY, _MAX_PAGE_DELAY))

    with session_scope() as db:
        import_record = db.get(UoSearchImport, import_id)
        if not import_record:
            return
        import_record.imported_count = imported
        import_record.total_results = total_seen
        if last_error:
            import_record.last_error = last_error
            import_record.status = "failed" if imported == 0 else "partial"
        elif stopped_by_limit:
            # If we stopped because of the daily cap, mark partial; otherwise it
            # is the per-import target being reached, which is expected.
            if imported >= remaining_today:
                import_record.status = "partial"
                import_record.last_error = f"daily import limit reached ({daily_limit})"
            else:
                import_record.status = "completed"
                import_record.last_error = None
        else:
            import_record.status = "completed"

        db_log(
            "info",
            "search_import.finished",
            f"{imported} leads imported",
            account_id=account_id,
            campaign_id=campaign_id,
            data={"import_id": import_id, "import_type": import_type, "total_results": total_seen},
        )


def get_import(db: Session, import_id: int) -> UoSearchImport | None:
    return db.get(UoSearchImport, import_id)
