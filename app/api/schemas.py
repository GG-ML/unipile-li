"""Pydantic request/response schemas for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class AccountConnectRequest(BaseModel):
    organisation_code: str = Field(..., description="Owner organisation code")
    email: str = Field(..., description="LinkedIn login email/username")
    password: str = Field(..., description="LinkedIn password")
    country_code: str = Field("", description="Proxy country code e.g. IN, US, GB")

    # Optional schedule / limit overrides at link time
    timezone: Optional[str] = "UTC"
    working_hours_start: Optional[int] = 9
    working_hours_end: Optional[int] = 18
    working_days: Optional[list[int]] = Field(default=None, description="0=Mon..6=Sun")
    daily_connect_limit: Optional[int] = None
    min_delay_seconds: Optional[int] = None
    max_delay_seconds: Optional[int] = None


class CheckpointRequest(BaseModel):
    code: Optional[str] = Field(
        default="",
        description="OTP / 2FA / captcha code. For IN_APP_VALIDATION (tap-yes), send an empty string.",
    )


class AccountConnectResponse(BaseModel):
    status: str
    account_id: int
    message: str = "Account linking started in the background. Poll GET /api/accounts/{account_id}/status."


class AccountStatusResponse(BaseModel):
    account_id: int
    status: str
    done: bool = False
    email: str
    unipile_account_id: Optional[str] = None
    name: Optional[str] = None
    linkedin_tier: Optional[str] = "basic"
    last_error: Optional[str] = None
    checkpoint: Optional[dict] = None


class CheckpointSubmittedResponse(BaseModel):
    status: str
    account_id: int
    intent_id: Optional[str] = None
    checkpoint_type: Optional[str] = None
    unipile_account_id: Optional[str] = None
    name: Optional[str] = None
    error: Optional[str] = None
    done: bool = False


class AccountScheduleUpdate(BaseModel):
    timezone: Optional[str] = None
    working_hours_start: Optional[int] = None
    working_hours_end: Optional[int] = None
    working_days: Optional[list[int]] = None
    daily_connect_limit: Optional[int] = None
    min_delay_seconds: Optional[int] = None
    max_delay_seconds: Optional[int] = None
    status: Optional[str] = None


class AccountOut(BaseModel):
    id: int
    organisation_code: str
    email: str
    unipile_account_id: Optional[str] = None
    name: Optional[str] = None
    status: str
    linkedin_tier: Optional[str] = "basic"
    country_code: Optional[str] = None
    timezone: Optional[str] = None
    working_hours_start: Optional[int] = None
    working_hours_end: Optional[int] = None
    working_days: Optional[list[int]] = None
    daily_connect_limit: Optional[int] = None
    last_error: Optional[str] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
class CampaignCreate(BaseModel):
    organisation_code: str
    name: str
    include_note: bool = True
    connection_note_template: Optional[str] = None
    initial_message_template: Optional[str] = None
    followup_enabled: bool = False
    followup_templates: list[str] = Field(default_factory=list)
    followup_interval_days: list[int] = Field(default_factory=list)
    daily_limit_per_account: int = 25
    min_delay_seconds: int = 180
    max_delay_seconds: int = 600


class AssignAccountsRequest(BaseModel):
    account_ids: list[int]
    assigned_daily_limit: Optional[int] = None


class TargetIn(BaseModel):
    linkedin_url: Optional[str] = None
    public_identifier: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    headline: Optional[str] = None
    company: Optional[str] = None


class AddTargetsRequest(BaseModel):
    targets: list[TargetIn]


class CampaignOut(BaseModel):
    id: int
    organisation_code: str
    name: str
    status: str
    include_note: bool
    followup_enabled: bool
    daily_limit_per_account: int

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Search imports
# ---------------------------------------------------------------------------
class SearchImportRequest(BaseModel):
    account_id: int = Field(..., description="LinkedIn account ID in our system")
    url: str = Field(..., description="Copy-pasted LinkedIn / Sales Navigator search URL")
    limit: int | None = Field(None, description="Optional per-import cap; defaults to type-based daily limit")


class SearchImportResponse(BaseModel):
    status: str
    import_id: int
    account_id: int
    campaign_id: int
    import_type: str
    message: str


class SearchImportOut(BaseModel):
    id: int
    account_id: int
    campaign_id: int
    import_type: str
    source_url: str
    status: str
    daily_limit: int
    target_count: int
    imported_count: int
    total_results: int
    cursor: str | None
    last_error: str | None
    created_at: datetime | None
    updated_at: datetime | None

    class Config:
        from_attributes = True


class SearchImportStats(BaseModel):
    imported_count: int
    total_results: int
    status: str
    last_error: str | None
