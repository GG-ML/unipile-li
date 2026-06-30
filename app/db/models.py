"""SQLAlchemy models for the Unipile LinkedIn outreach backend.

All tables are prefixed `uo_` so they coexist safely with the existing
Libingo `li_` tables in the same Postgres database.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


# ---------------------------------------------------------------------------
# Account status / task status constants
# ---------------------------------------------------------------------------
class AccountStatus:
    PENDING = "pending"            # auth intent started, awaiting result
    CHECKPOINT = "checkpoint"      # awaiting OTP/2FA/captcha code
    CONNECTED = "connected"        # linked & usable
    FAILED = "failed"              # auth failed
    DISCONNECTED = "disconnected"  # credentials revoked / unlinked
    PAUSED = "paused"              # manually paused


class TaskStatus:
    PENDING = "pending"            # not yet scheduled
    SCHEDULED = "scheduled"        # has a scheduled_at slot today
    INVITE_SENT = "invite_sent"    # connection request sent, awaiting acceptance
    ACCEPTED = "accepted"          # accepted, initial message not sent yet
    INITIAL_SENT = "initial_sent"  # initial message sent, awaiting reply / followups
    REPLIED = "replied"            # target replied -> handed off, no more automation
    COMPLETED = "completed"        # finished followup sequence with no reply
    FAILED = "failed"
    SKIPPED = "skipped"            # e.g. already a connection


class CampaignStatus:
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class AccountTier:
    BASIC = "basic"
    PREMIUM = "premium"
    SALES_NAVIGATOR = "sales_navigator"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
class UoAccount(Base):
    """A LinkedIn account linked through Unipile, owned by an organisation."""

    __tablename__ = "uo_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organisation_code = Column(String(100), nullable=False, index=True)

    email = Column(String(255), nullable=False, index=True)
    # Kept transiently for async checkpoint retries. Cleared once linked or failed.
    password = Column(Text)
    country_code = Column(String(8))

    unipile_account_id = Column(String(255), unique=True, index=True)
    provider = Column(String(50), default="LINKEDIN")
    name = Column(String(255))
    provider_id = Column(String(255))          # ACo... id of the account owner
    public_identifier = Column(String(255))

    status = Column(String(50), default=AccountStatus.PENDING, index=True)
    last_error = Column(Text)

    # Helps avoid stale background link tasks overwriting a newer attempt.
    link_attempt_id = Column(String(255))

    # LinkedIn subscription tier detected from Unipile's premiumFeatures.
    # basic accounts cannot send connection notes; premium/sales_navigator can.
    linkedin_tier = Column(String(50), default=AccountTier.BASIC, server_default=AccountTier.BASIC)

    # Working schedule
    timezone = Column(String(64), default="UTC")
    working_hours_start = Column(Integer, default=9)    # 0-23
    working_hours_end = Column(Integer, default=18)     # 0-23
    working_days = Column(JSONB, default=lambda: [0, 1, 2, 3, 4])  # 0=Mon .. 6=Sun

    # Rate limiting
    daily_connect_limit = Column(Integer, default=25)
    min_delay_seconds = Column(Integer, default=180)
    max_delay_seconds = Column(Integer, default=600)

    # Poll bookkeeping
    last_planned_date = Column(String(10))    # YYYY-MM-DD last day slots were planned
    next_poll_times = Column(JSONB, default=list)  # list of ISO datetimes scheduled today
    last_poll_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    auth_intents = relationship("UoAuthIntent", back_populates="account")
    tasks = relationship("UoTask", back_populates="account")

    __table_args__ = (
        UniqueConstraint("organisation_code", "email", name="uq_uo_account_org_email"),
        Index("idx_uo_account_status", "status"),
    )


class UoAuthIntent(Base):
    """Tracks an in-progress authentication / checkpoint flow."""

    __tablename__ = "uo_auth_intents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("uo_accounts.id"), nullable=False, index=True)
    intent_id = Column(String(512))
    checkpoint_type = Column(String(50))      # 2FA, OTP, CAPTCHA, IN_APP_VALIDATION...
    status = Column(String(50), default="pending")  # pending, submitted, solved, failed
    code = Column(Text)                       # user-submitted checkpoint code
    detail = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("UoAccount", back_populates="auth_intents")


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
class UoCampaign(Base):
    """A templated outreach campaign."""

    __tablename__ = "uo_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organisation_code = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(50), default=CampaignStatus.DRAFT, index=True)

    # Connection note
    include_note = Column(Boolean, default=True)
    connection_note_template = Column(Text)     # supports {first_name} {last_name} {headline} etc.

    # Initial message after acceptance
    initial_message_template = Column(Text)

    # Follow-ups
    followup_enabled = Column(Boolean, default=False)
    followup_templates = Column(JSONB, default=list)       # ["...", "..."]
    followup_interval_days = Column(JSONB, default=list)    # [3, 4] days between each

    # Per-campaign rate defaults (can be overridden per account)
    daily_limit_per_account = Column(Integer, default=25)
    min_delay_seconds = Column(Integer, default=180)
    max_delay_seconds = Column(Integer, default=600)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    accounts = relationship("UoCampaignAccount", back_populates="campaign")
    tasks = relationship("UoTask", back_populates="campaign")


class UoCampaignAccount(Base):
    """Which linked accounts participate in a campaign (load distribution)."""

    __tablename__ = "uo_campaign_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("uo_campaigns.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("uo_accounts.id"), nullable=False, index=True)
    assigned_daily_limit = Column(Integer)   # overrides account/campaign default if set
    status = Column(String(50), default="active")  # active, removed
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("UoCampaign", back_populates="accounts")

    __table_args__ = (
        UniqueConstraint("campaign_id", "account_id", name="uq_uo_campaign_account"),
    )


# ---------------------------------------------------------------------------
# Targets / tasks
# ---------------------------------------------------------------------------
class UoTask(Base):
    """A single outreach target within a campaign, executed by one account."""

    __tablename__ = "uo_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("uo_campaigns.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("uo_accounts.id"), index=True)

    # Identity of the target
    profile_url = Column(Text)
    public_identifier = Column(String(255), index=True)
    provider_id = Column(String(255))     # ACo... LinkedIn system id
    first_name = Column(String(255))
    last_name = Column(String(255))
    full_name = Column(String(255))
    headline = Column(Text)
    company = Column(String(255))
    location = Column(String(255))

    status = Column(String(50), default=TaskStatus.PENDING, index=True)

    # Unipile artefacts
    invitation_id = Column(String(255))
    chat_id = Column(String(255))

    # Scheduling
    scheduled_at = Column(DateTime, index=True)

    # Lifecycle timestamps
    invite_sent_at = Column(DateTime)
    accepted_at = Column(DateTime)
    initial_sent_at = Column(DateTime)
    last_message_at = Column(DateTime)
    last_reply_at = Column(DateTime)

    # Follow-up tracking
    followup_count = Column(Integer, default=0)
    last_followup_at = Column(DateTime)
    next_followup_at = Column(DateTime, index=True)

    reply_received = Column(Boolean, default=False)
    rendered_note = Column(Text)
    last_error = Column(Text)
    attempts = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign = relationship("UoCampaign", back_populates="tasks")
    account = relationship("UoAccount", back_populates="tasks")

    __table_args__ = (
        Index("idx_uo_task_status_account", "status", "account_id"),
        Index("idx_uo_task_sched", "status", "scheduled_at"),
        UniqueConstraint("campaign_id", "public_identifier", name="uq_uo_task_campaign_target"),
    )


# ---------------------------------------------------------------------------
# Profile cache
# ---------------------------------------------------------------------------
class UoProfile(Base):
    """Cache of scraped LinkedIn profiles, keyed by public_identifier."""

    __tablename__ = "uo_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    public_identifier = Column(String(255), unique=True, index=True)
    provider_id = Column(String(255), index=True)
    first_name = Column(String(255))
    last_name = Column(String(255))
    full_name = Column(String(255))
    headline = Column(Text)
    company = Column(String(255))
    location = Column(String(255))
    raw_json = Column(JSONB)
    scraped_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Daily counters (rate limiting per account/day)
# ---------------------------------------------------------------------------
class UoDailyCounter(Base):
    __tablename__ = "uo_daily_counters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("uo_accounts.id"), nullable=False, index=True)
    date = Column(String(10), nullable=False)   # YYYY-MM-DD
    invites_sent = Column(Integer, default=0)
    messages_sent = Column(Integer, default=0)
    search_imports = Column(Integer, default=0)  # profiles imported from LinkedIn search today
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "date", name="uq_uo_counter_account_date"),
    )


class UoSearchImport(Base):
    """Tracks a LinkedIn search URL import into a campaign."""

    __tablename__ = "uo_search_imports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("uo_accounts.id"), nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey("uo_campaigns.id"), nullable=False, index=True)

    import_type = Column(String(50), nullable=False)  # linkedin_classic_search, sales_navigator_search, sales_navigator_saved_search
    source_url = Column(Text, nullable=False)
    status = Column(String(50), default="pending")    # pending, running, completed, failed, partial

    daily_limit = Column(Integer, default=0)            # max profiles per day for this account/type
    target_count = Column(Integer, default=0)             # max profiles to fetch in this import (per-import cap)
    imported_count = Column(Integer, default=0)
    total_results = Column(Integer, default=0)          # total profiles reported by Unipile (first page)
    cursor = Column(Text)                               # last cursor consumed
    last_error = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = relationship("UoAccount")
    campaign = relationship("UoCampaign")


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
class UoLog(Base):
    __tablename__ = "uo_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, index=True)
    campaign_id = Column(Integer, index=True)
    task_id = Column(Integer, index=True)
    level = Column(String(20), default="info")   # info, warning, error
    event = Column(String(100))
    message = Column(Text)
    data = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
