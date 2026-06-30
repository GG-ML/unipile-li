"""Redis-backed distributed locks + atomic daily counters.

Used to load-balance work across many concurrent workers so that a single
LinkedIn account is never operated on by two workers at once, and so daily
send limits are enforced atomically even under concurrency.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from typing import Optional

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


@contextlib.contextmanager
def account_lock(account_id: int, ttl: int = 600):
    """Acquire a per-account lock. Yields True if acquired, else False.

    Non-blocking: if another worker holds the lock we immediately yield False
    so the caller can skip this account on this tick.
    """
    r = get_redis()
    key = f"uo:lock:account:{account_id}"
    token = str(uuid.uuid4())
    acquired = False
    try:
        acquired = bool(r.set(key, token, nx=True, ex=ttl))
        yield acquired
    finally:
        if acquired:
            # Release only if we still own it (avoid releasing someone else's lock)
            try:
                lua = (
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end"
                )
                r.eval(lua, 1, key, token)
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed releasing lock %s: %s", key, exc)


def poll_lock(account_id: int, ttl: int = 1800) -> bool:
    """Best-effort lock to ensure only one poll per account runs at a time."""
    r = get_redis()
    key = f"uo:lock:poll:{account_id}"
    return bool(r.set(key, "1", nx=True, ex=ttl))


def release_poll_lock(account_id: int) -> None:
    get_redis().delete(f"uo:lock:poll:{account_id}")
