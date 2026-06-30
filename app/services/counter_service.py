"""Per-account daily send counters (DB-backed, used for rate limiting)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import UoDailyCounter


def get_counter(db: Session, account_id: int, date_str: str) -> UoDailyCounter:
    counter = (
        db.query(UoDailyCounter)
        .filter(UoDailyCounter.account_id == account_id, UoDailyCounter.date == date_str)
        .first()
    )
    if counter is None:
        counter = UoDailyCounter(account_id=account_id, date=date_str, invites_sent=0, messages_sent=0)
        db.add(counter)
        db.flush()
    return counter


def invites_sent_today(db: Session, account_id: int, date_str: str) -> int:
    counter = (
        db.query(UoDailyCounter)
        .filter(UoDailyCounter.account_id == account_id, UoDailyCounter.date == date_str)
        .first()
    )
    return counter.invites_sent if counter else 0


def increment_invites(db: Session, account_id: int, date_str: str, by: int = 1) -> None:
    counter = get_counter(db, account_id, date_str)
    counter.invites_sent = (counter.invites_sent or 0) + by


def increment_messages(db: Session, account_id: int, date_str: str, by: int = 1) -> None:
    counter = get_counter(db, account_id, date_str)
    counter.messages_sent = (counter.messages_sent or 0) + by


def search_imports_today(db: Session, account_id: int, date_str: str) -> int:
    counter = (
        db.query(UoDailyCounter)
        .filter(UoDailyCounter.account_id == account_id, UoDailyCounter.date == date_str)
        .first()
    )
    return counter.search_imports if counter else 0


def increment_search_imports(db: Session, account_id: int, date_str: str, by: int = 1) -> None:
    counter = get_counter(db, account_id, date_str)
    counter.search_imports = (counter.search_imports or 0) + by
