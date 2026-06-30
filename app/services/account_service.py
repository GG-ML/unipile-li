"""Account linking logic (async Unipile credential flow + checkpoint handling).

Flow:
1. POST /api/accounts/connect creates a UoAccount row, stores the password
   transiently, and returns {"status": "started", "account_id": ...} immediately.
2. A background task runs process_link_account() which talks to Unipile.
3. When Unipile returns a checkpoint, the task writes UoAuthIntent and polls the
   DB for the frontend-submitted code.
4. POST /api/accounts/{id}/checkpoint receives the code, writes it to the intent,
   and the background task picks it up.
5. GET /api/accounts/{id}/status lets the frontend poll the current state.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.db.database import session_scope
from app.db.models import AccountStatus, AccountTier, UoAccount, UoAuthIntent
from app.services.logging_service import db_log
from app.unipile.client import UnipileClient, UnipileError

logger = logging.getLogger(__name__)

_CHECKPOINT_OBJECT = "AuthenticationCheckpoint"
_ACCOUNT_OBJECT = "Account"

# Max time to wait for a checkpoint code from the frontend (seconds).
_CHECKPOINT_TIMEOUT_SECONDS = 300
# Poll interval while waiting for a checkpoint code.
_CHECKPOINT_POLL_INTERVAL = 2

# IN_APP_VALIDATION ("tap yes in the LinkedIn app") has no code to submit. We poll
# Unipile until the account reports a connected source, up to this many seconds.
_IN_APP_VALIDATION_TIMEOUT_SECONDS = 300
_IN_APP_VALIDATION_POLL_INTERVAL = 3


def get_or_create_account(db: Session, organisation_code: str, email: str, **fields) -> UoAccount:
    account = (
        db.query(UoAccount)
        .filter(UoAccount.organisation_code == organisation_code, UoAccount.email == email)
        .first()
    )
    if account is None:
        account = UoAccount(organisation_code=organisation_code, email=email)
        db.add(account)
    for key, value in fields.items():
        if value is not None and hasattr(account, key):
            setattr(account, key, value)
    db.flush()
    return account


def queue_link_account(db: Session, payload: dict) -> UoAccount:
    """Create or reset a UoAccount row for async linking and return it.

    The password is stored transiently so the background task can retry or
    continue through checkpoints. It is cleared once linked or failed.

    A new `link_attempt_id` is generated each time the user starts a link so
    that a stale background task from a previous attempt cannot overwrite the
    result of the current one.
    """
    account = get_or_create_account(
        db,
        organisation_code=payload["organisation_code"],
        email=payload["email"],
        password=payload.get("password"),
        country_code=payload.get("country_code", ""),
        timezone=payload.get("timezone", "UTC"),
        working_hours_start=payload.get("working_hours_start", 9),
        working_hours_end=payload.get("working_hours_end", 18),
        working_days=payload.get("working_days", [0, 1, 2, 3, 4]),
        daily_connect_limit=payload.get("daily_connect_limit", 25),
        min_delay_seconds=payload.get("min_delay_seconds", 180),
        max_delay_seconds=payload.get("max_delay_seconds", 600),
        status=AccountStatus.PENDING,
        last_error=None,
        link_attempt_id=str(uuid.uuid4()),
    )
    # Intents from a previous attempt are no longer relevant; mark them stale so
    # the old background task exits cleanly instead of waiting for a code forever.
    (
        db.query(UoAuthIntent)
        .filter(UoAuthIntent.account_id == account.id, UoAuthIntent.status == "pending")
        .update({"status": "stale"}, synchronize_session=False)
    )
    db.commit()
    return account


def process_link_account(account_id: int) -> None:
    """Background task that runs the Unipile credential flow.

    Updates the DB directly. When a checkpoint is hit, it creates a
    UoAuthIntent and polls the DB until the frontend submits a code.
    """
    logger.info("Starting background link for account_id=%s", account_id)
    attempt_id = None
    try:
        with session_scope() as db:
            account = db.get(UoAccount, account_id)
            if not account:
                logger.error("Account %s not found for background link", account_id)
                return
            attempt_id = account.link_attempt_id
            password = account.password
            if not password:
                _finish_account(account_id, status=AccountStatus.FAILED, error="password missing", attempt_id=attempt_id)
                return

        client = UnipileClient()
        _run_link_loop(account_id, client, attempt_id)
    except Exception as exc:
        logger.exception("Background link failed for account_id=%s", account_id)
        _finish_account(account_id, status=AccountStatus.FAILED, error=str(exc)[:1000], attempt_id=attempt_id)


def _run_link_loop(account_id: int, client: UnipileClient, attempt_id: str | None) -> None:
    """Connect to Unipile and handle any checkpoint chain."""
    # Read account data and close the session before the long Unipile call so
    # Postgres does not drop an idle connection.
    with session_scope() as db:
        account = db.get(UoAccount, account_id)
        if not account:
            return
        email = account.email
        password = account.password
        country_code = account.country_code or ""

    if not password:
        _finish_account(account_id, status=AccountStatus.FAILED, error="missing password for link", attempt_id=attempt_id)
        return

    try:
        data = client.connect_linkedin(email, password, country_code)
    except UnipileError as exc:
        _finish_account(account_id, status=AccountStatus.FAILED, error=exc.message, attempt_id=attempt_id)
        return

    result = _handle_auth_response(account_id, data, attempt_id)
    while result.get("status") == "checkpoint":
        auth_intent_db_id = result["auth_intent_db_id"]
        checkpoint_type = result["checkpoint_type"]

        # IN_APP_VALIDATION ("tap yes" in the LinkedIn app) has no code. The user
        # approves the login on their phone and Unipile connects the account on its
        # side. We must NOT call solve_checkpoint with an empty code (Unipile rejects
        # it). Instead, poll Unipile until the account reports a connected source.
        if (checkpoint_type or "").upper() == "IN_APP_VALIDATION":
            unipile_account_id = result.get("unipile_account_id") or ""
            logger.info(
                "account_id=%s IN_APP_VALIDATION: polling Unipile account %s for approval",
                account_id, unipile_account_id,
            )
            if not unipile_account_id:
                _set_intent_status(auth_intent_db_id, "failed")
                _finish_account(account_id, status=AccountStatus.FAILED,
                                error="missing Unipile account_id for in-app validation", attempt_id=attempt_id)
                return
            account_data = _poll_account_connected(client, unipile_account_id)
            if account_data is None:
                _set_intent_status(auth_intent_db_id, "failed")
                _finish_account(account_id, status=AccountStatus.FAILED,
                                error="in-app validation timed out", attempt_id=attempt_id)
                return
            result = _handle_auth_response(account_id, account_data, attempt_id)
            _set_intent_status(auth_intent_db_id, "solved")
            continue

        logger.info(
            "account_id=%s waiting for checkpoint type=%s intent_db_id=%s",
            account_id, checkpoint_type, auth_intent_db_id,
        )
        code = _wait_for_checkpoint_code(auth_intent_db_id)
        if code is None:
            _set_intent_status(auth_intent_db_id, "failed")
            _finish_account(account_id, status=AccountStatus.FAILED, error="checkpoint timed out", attempt_id=attempt_id)
            return

        with session_scope() as db:
            intent = db.get(UoAuthIntent, auth_intent_db_id)
            unipile_account_id = (
                intent.intent_id
                or (intent.detail or {}).get("account_id")
                or (intent.detail or {}).get("id")
                or ""
            )

        if not unipile_account_id:
            _set_intent_status(auth_intent_db_id, "failed")
            _finish_account(account_id, status=AccountStatus.FAILED, error="missing Unipile account_id for checkpoint", attempt_id=attempt_id)
            return

        try:
            data = client.solve_checkpoint(unipile_account_id, code, checkpoint_type)
        except UnipileError as exc:
            _set_intent_status(auth_intent_db_id, "failed")
            _finish_account(account_id, status=AccountStatus.FAILED, error=exc.message, attempt_id=attempt_id)
            return

        result = _handle_auth_response(account_id, data, attempt_id)
        # This intent has been processed. If the response is another checkpoint a
        # fresh intent was created inside _handle_auth_response; mark this one solved
        # so it no longer surfaces as a pending checkpoint to the frontend.
        _set_intent_status(auth_intent_db_id, "solved")

    if result.get("status") == "connected":
        _finish_account(account_id, status=AccountStatus.CONNECTED, error=None, attempt_id=attempt_id)
    elif result.get("status") == "failed":
        _finish_account(account_id, status=AccountStatus.FAILED, error=result.get("error", "unknown"), attempt_id=attempt_id)
    else:
        _finish_account(account_id, status=AccountStatus.FAILED, error="unexpected final response", attempt_id=attempt_id)


def _set_intent_status(auth_intent_db_id: int, status: str) -> None:
    """Update a single auth-intent row's status in a short-lived session."""
    with session_scope() as db:
        intent = db.get(UoAuthIntent, auth_intent_db_id)
        if intent:
            intent.status = status


def _account_is_connected(account_data: dict) -> bool:
    """A Unipile account object is connected once a source reports status OK."""
    if not account_data or account_data.get("object") != _ACCOUNT_OBJECT:
        return False
    sources = account_data.get("sources") or []
    return any(str(s.get("status", "")).upper() == "OK" for s in sources)


def _poll_account_connected(client: UnipileClient, unipile_account_id: str) -> Optional[dict]:
    """Poll Unipile GET /accounts/{id} until the account is connected.

    Used for IN_APP_VALIDATION where the user approves the login on their phone.
    Returns the connected account object, or None on timeout.
    """
    deadline = time.time() + _IN_APP_VALIDATION_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            account_data = client.get_account(unipile_account_id)
        except UnipileError as exc:
            logger.warning(
                "IN_APP_VALIDATION poll get_account(%s) failed: %s",
                unipile_account_id, str(exc)[:200],
            )
            account_data = None
        if _account_is_connected(account_data):
            return account_data
        time.sleep(_IN_APP_VALIDATION_POLL_INTERVAL)
    return None


def _wait_for_checkpoint_code(auth_intent_db_id: int) -> Optional[str]:
    """Poll the uo_auth_intents row until the frontend submits a code.

    Returns the submitted code, or None on timeout.
    """
    deadline = time.time() + _CHECKPOINT_TIMEOUT_SECONDS
    while time.time() < deadline:
        with session_scope() as db:
            intent = db.get(UoAuthIntent, auth_intent_db_id)
            if intent and intent.status == "submitted" and intent.code is not None:
                return intent.code
        time.sleep(_CHECKPOINT_POLL_INTERVAL)
    return None


def _handle_auth_response(account_id: int, data: dict, attempt_id: str | None = None) -> dict:
    """Persist the result of a connect_linkedin or solve_checkpoint call.

    Returns a small dict describing the next step for the background loop.
    """
    obj = data.get("object", "")
    logger.info("handle_auth_response account_id=%s object=%s data=%s", account_id, obj, str(data)[:1000])

    with session_scope() as db:
        account = db.get(UoAccount, account_id)
        if not account:
            return {"status": "failed", "error": "account not found"}

        # A newer link attempt has started; do not let this stale task mutate the DB.
        if attempt_id is not None and account.link_attempt_id != attempt_id:
            logger.warning(
                "account_id=%s link attempt_id mismatch (current=%s, task=%s) — ignoring response",
                account_id, account.link_attempt_id, attempt_id,
            )
            return {"status": "failed", "error": "stale link attempt"}

        # Unipile returns the account object on success, but sometimes just a minimal
        # response with id/account_id. Treat any response with an account identifier
        # and no checkpoint as connected.
        is_account = (
            obj == _ACCOUNT_OBJECT
            or (data.get("id") and not data.get("checkpoint"))
            or (data.get("account_id") and not data.get("checkpoint"))
            or (data.get("provider") and data.get("public_identifier"))
        )
        if is_account:
            _apply_unipile_account(account, data)
            db_log("info", "account.connected", account_id=account.id,
                   data={"unipile_account_id": account.unipile_account_id})
            return {
                "status": "connected",
                "unipile_account_id": account.unipile_account_id,
                "name": account.name,
            }

        is_checkpoint = (
            obj == _CHECKPOINT_OBJECT
            or obj == "Checkpoint"
            or data.get("checkpoint")
        )
        if is_checkpoint:
            cp = data.get("checkpoint", {}) or {}
            checkpoint_type = (
                data.get("type")
                or data.get("checkpoint_type")
                or cp.get("type")
                or cp.get("checkpoint_type")
                or cp.get("name")
                or "UNKNOWN"
            )
            unipile_account_id = data.get("account_id") or data.get("id")
            account.status = AccountStatus.CHECKPOINT
            intent = UoAuthIntent(
                account_id=account.id,
                intent_id=unipile_account_id,
                checkpoint_type=checkpoint_type,
                status="pending",
                detail=data,
            )
            db.add(intent)
            db.flush()
            db_log("info", "account.checkpoint", checkpoint_type, account_id=account.id)
            return {
                "status": "checkpoint",
                "checkpoint_type": checkpoint_type,
                "unipile_account_id": unipile_account_id,
                "auth_intent_db_id": intent.id,
            }

        account.status = AccountStatus.FAILED
        account.last_error = f"unexpected response: {str(data)[:500]}"
        return {"status": "failed", "error": "unexpected response", "raw": data}


def _detect_tier(data: dict) -> str:
    """Map Unipile's premiumFeatures to our account tier.

    Basic (free) accounts have no premiumFeatures. Premium or Sales Navigator
    accounts expose a list of feature identifiers; we inspect them to choose the
    most specific tier.
    """
    im = (data.get("connection_params") or {}).get("im") or {}
    features = data.get("premiumFeatures") or im.get("premiumFeatures") or []
    if not features:
        return AccountTier.BASIC

    # Normalise everything to a string so we can do case-insensitive matching.
    tokens = []
    for f in features:
        if isinstance(f, dict):
            for key in ("name", "type", "id", "value"):
                val = f.get(key)
                if val is not None:
                    tokens.append(str(val).lower())
        else:
            tokens.append(str(f).lower())
    joined = " ".join(tokens)

    if any(term in joined for term in ("sales_navigator", "salesnavigator", "sales_nav")):
        return AccountTier.SALES_NAVIGATOR
    if any(
        term in joined
        for term in (
            "premium",
            "recruiter",
            "business",
            "career",
            "learning",
            "job_posting",
        )
    ):
        return AccountTier.PREMIUM

    # If Unipile reports a non-empty premiumFeatures list but we cannot map it,
    # treat it as a paid tier (better to allow notes than block them for basic).
    return AccountTier.PREMIUM


def _apply_unipile_account(account: UoAccount, data: dict) -> None:
    # Full account objects nest the LinkedIn identifiers under connection_params.im;
    # checkpoint-solve responses expose them at the top level. Support both shapes.
    im = (data.get("connection_params") or {}).get("im") or {}
    account.unipile_account_id = data.get("id") or account.unipile_account_id
    account.name = data.get("name") or data.get("username") or im.get("username") or account.name
    account.provider = data.get("type") or data.get("provider") or account.provider
    account.provider_id = data.get("provider_id") or im.get("id") or account.provider_id
    account.public_identifier = (
        data.get("public_identifier") or im.get("publicIdentifier") or account.public_identifier
    )
    account.linkedin_tier = _detect_tier(data)
    account.status = AccountStatus.CONNECTED
    account.last_error = None


def _finish_account(account_id: int, status: str, error: Optional[str], attempt_id: str | None = None) -> None:
    """Set final status and clear the transient password.

    If `attempt_id` is provided and the account has a newer link attempt, this
    is a stale task and must not overwrite the current status or clear the
    password that belongs to the newer attempt.
    """
    with session_scope() as db:
        account = db.get(UoAccount, account_id)
        if not account:
            return
        if attempt_id is not None and account.link_attempt_id != attempt_id:
            logger.warning(
                "account_id=%s stale link attempt not finishing (current=%s, task=%s)",
                account_id, account.link_attempt_id, attempt_id,
            )
            return
        account.status = status
        account.last_error = error[:1000] if error else None
        account.password = None
        db_log("info", f"account.{status}", error or "", account_id=account.id)


def submit_checkpoint_code(db: Session, account_id: int, code: str) -> dict:
    """Receive a checkpoint code from the frontend and store it for the background task."""
    account = db.get(UoAccount, account_id)
    if not account:
        return {"status": "failed", "error": "account not found"}

    # The link flow has already reached a terminal state. Report it idempotently so
    # repeated frontend submits return the final result instead of a 400 error.
    if account.status == AccountStatus.CONNECTED:
        return {
            "status": "connected",
            "account_id": account_id,
            "unipile_account_id": account.unipile_account_id,
            "name": account.name,
            "done": True,
        }
    if account.status == AccountStatus.FAILED:
        return {
            "status": "failed",
            "account_id": account_id,
            "error": account.last_error or "link failed",
            "done": True,
        }

    intent = (
        db.query(UoAuthIntent)
        .filter(UoAuthIntent.account_id == account_id)
        .order_by(UoAuthIntent.id.desc())
        .first()
    )
    if intent is None:
        return {"status": "failed", "error": "no pending checkpoint"}

    unipile_account_id = (
        intent.intent_id
        or (intent.detail or {}).get("account_id")
        or (intent.detail or {}).get("id")
    )

    # A code was already submitted and the background task is still processing it.
    # Return idempotently so the frontend keeps polling status instead of erroring.
    if intent.status == "submitted":
        return {
            "status": "processing",
            "account_id": account_id,
            "intent_id": unipile_account_id,
            "checkpoint_type": intent.checkpoint_type,
        }
    if intent.status != "pending":
        return {"status": "failed", "error": "no pending checkpoint"}

    intent.code = code
    intent.status = "submitted"
    db.commit()
    db_log("info", "account.checkpoint_submitted", intent.checkpoint_type, account_id=account_id)
    return {
        "status": "submitted",
        "account_id": account_id,
        "intent_id": unipile_account_id,
        "checkpoint_type": intent.checkpoint_type,
    }


def get_account_status(db: Session, account_id: int) -> dict:
    """Return the current async link status and any pending checkpoint."""
    account = db.get(UoAccount, account_id)
    if not account:
        return {"status": "failed", "error": "account not found"}

    intent = (
        db.query(UoAuthIntent)
        .filter(UoAuthIntent.account_id == account_id)
        .order_by(UoAuthIntent.id.desc())
        .first()
    )

    # Terminal: the frontend should stop polling once `done` is true.
    done = account.status in (AccountStatus.CONNECTED, AccountStatus.FAILED)
    result = {
        "account_id": account_id,
        "status": account.status,
        "done": done,
        "email": account.email,
        "unipile_account_id": account.unipile_account_id,
        "name": account.name,
        "linkedin_tier": account.linkedin_tier,
        "last_error": account.last_error,
    }

    # Only surface a checkpoint while it is genuinely awaiting user action. Once the
    # code has been submitted/solved or the account reached a terminal state, the
    # checkpoint block is omitted so the frontend knows the prompt is gone.
    if intent and not done and intent.status == "pending":
        unipile_account_id = (
            intent.intent_id
            or (intent.detail or {}).get("account_id")
            or (intent.detail or {}).get("id")
        )
        result["checkpoint"] = {
            "intent_id": unipile_account_id,
            "checkpoint_type": intent.checkpoint_type,
            "status": intent.status,
            "detail": intent.detail,
        }
    return result


# Backward-compatible helpers kept for direct imports/tests.
def _set_account_failed(account_id: int, error: str) -> None:
    """Mark an account as failed using a short-lived DB session."""
    _finish_account(account_id, status=AccountStatus.FAILED, error=error)


def link_account(
    db: Session,
    account: UoAccount,
    password: str,
    country_code: str = "",
    client: Optional[UnipileClient] = None,
) -> dict:
    """Synchronous wrapper for ad-hoc or test use.

    Prefer queue_link_account + process_link_account for production API calls.
    """
    client = client or UnipileClient()
    account.country_code = country_code or account.country_code
    account.status = AccountStatus.PENDING
    account.password = password
    db.flush()
    db.commit()
    account_id = account.id

    try:
        data = client.connect_linkedin(account.email, password, country_code)
    except UnipileError as exc:
        _finish_account(account_id, status=AccountStatus.FAILED, error=exc.message)
        db_log("error", "account.link_failed", exc.message[:500], account_id=account_id)
        return {"status": "failed", "error": exc.message}

    return _handle_auth_response(account_id, data)


def solve_checkpoint(
    db: Session,
    account: UoAccount,
    code: str,
    client: Optional[UnipileClient] = None,
) -> dict:
    """Synchronous wrapper for ad-hoc or test use.

    Prefer submit_checkpoint_code for production API calls.
    """
    intent = (
        db.query(UoAuthIntent)
        .filter(UoAuthIntent.account_id == account.id, UoAuthIntent.status == "pending")
        .order_by(UoAuthIntent.id.desc())
        .first()
    )
    if intent is None:
        return {"status": "failed", "error": "no pending checkpoint"}

    client = client or UnipileClient()
    try:
        data = client.solve_checkpoint(intent.intent_id or "", code, intent.checkpoint_type or "")
    except UnipileError as exc:
        intent.status = "failed"
        account.last_error = exc.message[:1000]
        db_log("error", "account.checkpoint_failed", exc.message[:500], account_id=account.id)
        return {"status": "failed", "error": exc.message}

    result = _handle_auth_response(account.id, data)
    if result["status"] == "connected":
        intent.status = "solved"
    return result
