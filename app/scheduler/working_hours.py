"""Timezone-aware working-hours helpers.

All scheduled_at values are stored in UTC. Working hours/days are interpreted
in each account's local timezone, then converted to UTC for scheduling.
"""

from __future__ import annotations

import random
from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


def _tz(account) -> ZoneInfo:
    try:
        return ZoneInfo(account.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def now_local(account) -> datetime:
    return datetime.now(_tz(account))


def local_date_str(account) -> str:
    return now_local(account).strftime("%Y-%m-%d")


def is_working_day(account, dt_local: datetime | None = None) -> bool:
    dt_local = dt_local or now_local(account)
    working_days = account.working_days or [0, 1, 2, 3, 4]
    return dt_local.weekday() in working_days


def is_within_working_hours(account, dt_local: datetime | None = None) -> bool:
    dt_local = dt_local or now_local(account)
    if not is_working_day(account, dt_local):
        return False
    start = account.working_hours_start if account.working_hours_start is not None else 9
    end = account.working_hours_end if account.working_hours_end is not None else 18
    return start <= dt_local.hour < end


def working_window_today(account) -> tuple[datetime, datetime] | None:
    """Return (start_utc, end_utc) of today's working window, or None if not a working day."""
    tz = _tz(account)
    local = datetime.now(tz)
    if not is_working_day(account, local):
        return None
    start_h = account.working_hours_start if account.working_hours_start is not None else 9
    end_h = account.working_hours_end if account.working_hours_end is not None else 18
    start_local = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
    end_local = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def random_times_in_window(start_utc: datetime, end_utc: datetime, count: int,
                           not_before: datetime | None = None,
                           min_gap_seconds: int = 60) -> list[datetime]:
    """Generate `count` random, sorted, spaced UTC timestamps within the window."""
    if count <= 0:
        return []
    lower = start_utc
    if not_before and not_before > lower:
        lower = not_before
    if lower >= end_utc:
        return []

    span = int((end_utc - lower).total_seconds())
    if span <= 0:
        return []

    # Sample distinct offsets, then enforce a minimum gap.
    picks = sorted(random.sample(range(span), min(count, span)))
    times: list[datetime] = []
    last = None
    for offset in picks:
        candidate = lower + timedelta(seconds=offset)
        if last is not None and (candidate - last).total_seconds() < min_gap_seconds:
            candidate = last + timedelta(seconds=min_gap_seconds)
            if candidate >= end_utc:
                break
        times.append(candidate)
        last = candidate
    return times
