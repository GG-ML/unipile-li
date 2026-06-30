"""Thin wrapper around the Unipile v1 LinkedIn API.

Endpoint usage validated against the working test scripts in
/root/unipile-linkedin (link_linkedin_account.py, outreach_sequence.py,
periodic_checker.py, outreach_profile_fetch.py).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Transient network failures (connection resets, server disconnects, read/connect
# timeouts) are retried a few times before surfacing as a UnipileError.
_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_SECONDS = 2.0


class UnipileError(Exception):
    """Raised when the Unipile API returns an error response."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"Unipile {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


class UnipileClient:
    """Synchronous Unipile client (httpx). Safe to instantiate per call."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.UNIPILE_API_KEY
        self.base_url = (base_url or settings.UNIPILE_BASE_URL).rstrip("/")
        self._json_headers = {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }
        # multipart endpoints must NOT set content-type (httpx sets boundary)
        self._form_headers = {
            "X-API-KEY": self.api_key,
            "accept": "application/json",
        }
        self._proxy: dict[str, str] | None = None
        if settings.UNIPILE_PROXY_URL:
            self._proxy = {"all://": settings.UNIPILE_PROXY_URL}

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
        timeout: float = 60.0,
    ) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._form_headers if data is not None else self._json_headers

        last_exc: httpx.TransportError | None = None
        for attempt in range(1, _TRANSIENT_RETRIES + 1):
            try:
                resp = httpx.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout,
                    proxies=self._proxy,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # A real HTTP response with an error status is deterministic; do not retry.
                text = exc.response.text
                raise UnipileError(exc.response.status_code, text, _safe_json(exc.response)) from exc
            except httpx.TransportError as exc:
                # Connection reset / server disconnect / timeout: retry with backoff.
                last_exc = exc
                logger.warning(
                    "Unipile transport error on %s %s (attempt %s/%s): %s",
                    method, path, attempt, _TRANSIENT_RETRIES, str(exc)[:200],
                )
                if attempt < _TRANSIENT_RETRIES:
                    time.sleep(_TRANSIENT_BACKOFF_SECONDS * attempt)
                    continue
                raise UnipileError(0, f"transient network error after {attempt} attempts: {exc}") from exc
            except httpx.HTTPError as exc:
                raise UnipileError(0, str(exc)) from exc
            else:
                return _safe_json(resp) or {}

        # Unreachable, but keeps type-checkers happy.
        raise UnipileError(0, str(last_exc) if last_exc else "unknown transport error")

    # ------------------------------------------------------------------
    # Accounts / auth
    # ------------------------------------------------------------------
    def connect_linkedin(self, username: str, password: str, country: str = "") -> dict:
        """POST /api/v1/accounts — start auth intent with credentials."""
        payload: dict = {
            "provider": "LINKEDIN",
            "username": username,
            "password": password,
        }
        if country:
            payload["country"] = country.upper()
        return self._request("POST", "/api/v1/accounts", json=payload, timeout=120)

    def solve_checkpoint(
        self,
        account_id: str,
        code: str,
        checkpoint_type: str = "",
    ) -> dict:
        """POST /api/v1/accounts/checkpoint — submit OTP / 2FA / captcha code.

        Unipile requires provider and the Unipile account_id in the body.
        """
        payload: dict = {"provider": "LINKEDIN", "account_id": account_id, "code": code}
        if checkpoint_type:
            payload["checkpoint_type"] = checkpoint_type
        return self._request("POST", "/api/v1/accounts/checkpoint", json=payload, timeout=120)

    def delete_account(self, account_id: str) -> dict:
        """DELETE /api/v1/accounts/{id}."""
        return self._request("DELETE", f"/api/v1/accounts/{account_id}", timeout=30)

    def list_accounts(self) -> list[dict]:
        """GET /api/v1/accounts."""
        data = self._request("GET", "/api/v1/accounts", timeout=30)
        return data.get("items", []) or data.get("accounts", [])

    def get_account(self, account_id: str) -> dict:
        return self._request("GET", f"/api/v1/accounts/{account_id}", timeout=30)

    def search_linkedin(
        self,
        account_id: str,
        url: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        body: dict | None = None,
    ) -> dict:
        """POST /api/v1/linkedin/search — copy/paste URL or structured filters.

        The account_id is sent in the query string as required by Unipile.
        Cursor can be passed in the query string or body; we use the query string.
        """
        params: dict = {"account_id": account_id}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        json_payload: dict = {}
        if url:
            json_payload["url"] = url
        if body:
            json_payload.update(body)
        return self._request(
            "POST",
            "/api/v1/linkedin/search",
            params=params,
            json=json_payload if json_payload else None,
            timeout=120,
        )

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def fetch_profile(self, account_id: str, identifier: str) -> dict:
        """GET /api/v1/users/{identifier}?account_id=... (public id or provider id)."""
        return self._request(
            "GET",
            f"/api/v1/users/{identifier}",
            params={"account_id": account_id},
            timeout=60,
        )

    # ------------------------------------------------------------------
    # Invitations
    # ------------------------------------------------------------------
    def send_invitation(self, account_id: str, provider_id: str, note: str = "") -> dict:
        """POST /api/v1/users/invite — provider_id must be the ACo... id."""
        payload: dict = {"account_id": account_id, "provider_id": provider_id}
        if note:
            payload["message"] = note
        return self._request("POST", "/api/v1/users/invite", json=payload, timeout=60)

    def list_sent_invitations(self, account_id: str, limit: int = 100, cursor: str = "") -> dict:
        """GET /api/v1/users/invite/sent — pending sent invitations."""
        params: dict = {"account_id": account_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/users/invite/sent", params=params, timeout=30)

    def get_relations(self, account_id: str, limit: int = 100, cursor: str = "") -> dict:
        """GET /api/v1/users/relations — current connections, recent first."""
        params: dict = {"account_id": account_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/api/v1/users/relations", params=params, timeout=30)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    def list_chats(self, account_id: str, limit: int = 50) -> list[dict]:
        data = self._request(
            "GET", "/api/v1/chats", params={"account_id": account_id, "limit": limit}, timeout=30
        )
        return data.get("items", []) or data.get("chats", [])

    def get_chat_messages(self, chat_id: str, limit: int = 20) -> list[dict]:
        data = self._request(
            "GET",
            f"/api/v1/chats/{chat_id}/messages",
            params={"limit": limit},
            timeout=30,
        )
        return data.get("items", []) or data.get("messages", [])

    def send_message(self, chat_id: str, text: str) -> dict:
        """POST /api/v1/chats/{chat_id}/messages — multipart/form-data."""
        return self._request(
            "POST",
            f"/api/v1/chats/{chat_id}/messages",
            data={"text": text},
            timeout=30,
        )

    def create_chat_with_message(self, account_id: str, provider_id: str, text: str) -> dict:
        """POST /api/v1/chats — create chat + initial message (multipart)."""
        return self._request(
            "POST",
            "/api/v1/chats",
            data={"account_id": account_id, "attendees_ids": provider_id, "text": text},
            timeout=30,
        )

    # ------------------------------------------------------------------
    # Webhooks (optional real-time augmentation)
    # ------------------------------------------------------------------
    def create_webhook(self, source: str, request_url: str, name: str = "") -> dict:
        payload = {
            "source": source,
            "request_url": request_url,
            "name": name or source,
            "headers": [{"key": "Content-Type", "value": "application/json"}],
        }
        return self._request("POST", "/api/v1/webhooks", json=payload, timeout=30)


def _safe_json(resp: httpx.Response) -> Optional[dict]:
    try:
        return resp.json()
    except Exception:
        return None
