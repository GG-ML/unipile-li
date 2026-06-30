"""LinkedIn search URL -> campaign leads API.

Three endpoints:
- POST /api/campaigns/{campaign_id}/import/linkedin-search
- POST /api/campaigns/{campaign_id}/import/sales-navigator-search
- POST /api/campaigns/{campaign_id}/import/sales-navigator-saved-search

All accept {account_id, url, limit?} and return an import_id. The heavy lifting
(pagination, daily-limit checks) runs in a FastAPI BackgroundTask.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.schemas import (
    SearchImportOut,
    SearchImportRequest,
    SearchImportResponse,
    SearchImportStats,
)
from app.db.database import get_db
from app.db.models import UoCampaign, UoSearchImport
from app.services import search_import_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/campaigns", tags=["search-imports"])


IMPORT_TYPE_BY_PATH = {
    "linkedin-search": "linkedin_classic_search",
    "sales-navigator-search": "sales_navigator_search",
    "sales-navigator-saved-search": "sales_navigator_saved_search",
}


def _get_campaign(db: Session, campaign_id: int) -> UoCampaign:
    campaign = db.query(UoCampaign).filter(UoCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


def _queue_import(
    campaign_id: int,
    import_type: str,
    payload: SearchImportRequest,
    background_tasks: BackgroundTasks,
    db: Session,
) -> SearchImportResponse:
    campaign = _get_campaign(db, campaign_id)
    try:
        import_record = search_import_service.queue_search_import(
            db,
            campaign_id=campaign_id,
            account_id=payload.account_id,
            source_url=payload.url,
            import_type=import_type,
            target_count=payload.limit,
        )
    except search_import_service.SearchImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db.commit()
    background_tasks.add_task(search_import_service.process_search_import, import_record.id)

    return SearchImportResponse(
        status="started",
        import_id=import_record.id,
        account_id=payload.account_id,
        campaign_id=campaign_id,
        import_type=import_record.import_type,
        message="Import started in the background. Poll GET /api/campaigns/{campaign_id}/search-imports/{import_id}",
    )


@router.post("/{campaign_id}/import/linkedin-search", response_model=SearchImportResponse)
def import_linkedin_search(
    campaign_id: int,
    payload: SearchImportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _queue_import(
        campaign_id, IMPORT_TYPE_BY_PATH["linkedin-search"], payload, background_tasks, db
    )


@router.post("/{campaign_id}/import/sales-navigator-search", response_model=SearchImportResponse)
def import_sales_navigator_search(
    campaign_id: int,
    payload: SearchImportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _queue_import(
        campaign_id, IMPORT_TYPE_BY_PATH["sales-navigator-search"], payload, background_tasks, db
    )


@router.post(
    "/{campaign_id}/import/sales-navigator-saved-search",
    response_model=SearchImportResponse,
)
def import_sales_navigator_saved_search(
    campaign_id: int,
    payload: SearchImportRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _queue_import(
        campaign_id,
        IMPORT_TYPE_BY_PATH["sales-navigator-saved-search"],
        payload,
        background_tasks,
        db,
    )


@router.get("/{campaign_id}/search-imports", response_model=list[SearchImportOut])
def list_search_imports(campaign_id: int, db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    return (
        db.query(UoSearchImport)
        .filter(UoSearchImport.campaign_id == campaign_id)
        .order_by(UoSearchImport.id.desc())
        .all()
    )


@router.get("/{campaign_id}/search-imports/{import_id}", response_model=SearchImportOut)
def get_search_import(campaign_id: int, import_id: int, db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    import_record = db.get(UoSearchImport, import_id)
    if not import_record or import_record.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="import not found")
    return import_record


@router.get(
    "/{campaign_id}/search-imports/{import_id}/stats", response_model=SearchImportStats
)
def search_import_stats(campaign_id: int, import_id: int, db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    import_record = db.get(UoSearchImport, import_id)
    if not import_record or import_record.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="import not found")
    return SearchImportStats(
        imported_count=import_record.imported_count or 0,
        total_results=import_record.total_results or 0,
        status=import_record.status,
        last_error=import_record.last_error,
    )
