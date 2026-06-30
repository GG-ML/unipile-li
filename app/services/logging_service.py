"""Helper to persist structured logs to uo_logs."""

from __future__ import annotations

import logging

from app.db.database import session_scope
from app.db.models import UoLog

logger = logging.getLogger(__name__)


def db_log(
    level: str,
    event: str,
    message: str = "",
    *,
    account_id: int | None = None,
    campaign_id: int | None = None,
    task_id: int | None = None,
    data: dict | None = None,
) -> None:
    """Write a log row. Never raises (logging must not break the workflow)."""
    try:
        with session_scope() as db:
            db.add(
                UoLog(
                    level=level,
                    event=event,
                    message=message[:4000] if message else message,
                    account_id=account_id,
                    campaign_id=campaign_id,
                    task_id=task_id,
                    data=data or {},
                )
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("db_log failed (%s): %s", event, exc)
