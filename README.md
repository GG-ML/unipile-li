# Unipile LinkedIn Outreach Backend

Templated (no-AI) LinkedIn outreach automation built on the
[Unipile API](https://developer.unipile.com/docs/getting-started). Two services:

- **API** (`app.main`) — link LinkedIn accounts, create campaigns, add targets.
- **Worker** (`app.worker`) — slot scheduler that sends invites within each
  account's working hours/days/daily-limit, polls twice a day for accepted
  connections, sends the initial templated message, and sends templated
  follow-ups when no reply is received.

Designed to scale to **100–500 accounts** via per-account Redis locks, DB-backed
daily counters, bounded thread pools, and horizontally scalable worker replicas.

## Production URL

The live API is served behind Nginx at:

```
https://linkedin-server.libingo.io
https://linkedin-server.libingo.io/docs
```

Use this URL in the frontend and all API calls below. For local development, replace it with `http://localhost:8000`.

## Architecture

```
                +-------------------+
   HTTP  ---->  |   FastAPI (api)   |   link accounts / campaigns / targets
                +-------------------+
                          |
                          v
                 PostgreSQL (uo_* tables)  <----+
                          ^                      |
                          |                      |
                +-------------------+            |
                |  Worker (APSched) |            |
                |  planner  -------------> assigns scheduled_at slots
                |  executor -------------> sends invites (Redis-locked)
                |  poller   -------------> 2x/day: accepted-check, initial msg,
                |                          reply detection, follow-ups
                +-------------------+            |
                          |                      |
                          v                      |
                      Unipile API ---------------+
```

### Tables (prefixed `uo_`, shared Libingo Postgres)
`uo_accounts`, `uo_auth_intents`, `uo_campaigns`, `uo_campaign_accounts`,
`uo_tasks`, `uo_profiles`, `uo_daily_counters`, `uo_search_imports`, `uo_logs`.

> **Note:** `init_db()` only creates missing tables. If you are updating an
> existing database, run the manual DDL in `migrations/manual/` (or use Alembic)
> to add new columns/tables. For this release the required change is:
> `ALTER TABLE uo_daily_counters ADD COLUMN search_imports INTEGER NOT NULL DEFAULT 0;`

## Flow

1. **Link account** — `POST /api/accounts/connect` with org code, email,
   password, country code. Handles checkpoints via
   `POST /api/accounts/{id}/checkpoint`. Supported checkpoint types: OTP/2FA,
   captcha, and `IN_APP_VALIDATION` (tap **Yes** in the LinkedIn mobile app —
   submit an empty `code` to confirm).
2. **Create campaign** — templated connection note + initial message +
   optional follow-ups (`{first_name}`, `{last_name}`, `{headline}`,
   `{company}`, `{full_name}` placeholders).
3. **Assign accounts & targets** — targets are round-robin distributed across
   the campaign's connected accounts.
4. **Start campaign** — the worker plans each account's day: it spreads invites
   at **random intervals inside the working window**, never exceeding the daily
   limit, and schedules 2 acceptance polls.
5. **Polling** — twice/day per account the poller compares the relations list
   against sent invites to detect acceptances, sends the initial message, and —
   if no reply — sends the next follow-up (when enabled).
6. **Names** — before sending any note/message, the system checks the DB for
   `first_name`/`last_name`; if missing it scrapes the profile via Unipile
   (`/api/v1/users/{id}`) and caches it in `uo_profiles`.

## Quick start (Docker)

```bash
cp .env .env   # edit UNIPILE_*, DATABASE_URL, REDIS_URL if needed
docker compose up --build
# scale the automation engine:
docker compose up --scale worker=3
```

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — API
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
# Terminal 2 — automation worker
python -m app.worker
```

## API examples

Link an account (async — returns `account_id` immediately):
```bash
curl -X POST https://linkedin-server.libingo.io/api/accounts/connect -H 'Content-Type: application/json' -d '{
  "organisation_code": "ACME",
  "email": "user@example.com",
  "password": "••••••",
  "country_code": "IN",
  "timezone": "Asia/Kolkata",
  "working_hours_start": 9,
  "working_hours_end": 18,
  "working_days": [0,1,2,3,4],
  "daily_connect_limit": 25
}'
# {"status": "started", "account_id": 1}
```

Poll status until `connected`:
```bash
watch -n 5 'curl -s https://linkedin-server.libingo.io/api/accounts/1/status | python3 -m json.tool'
```

Submit a checkpoint code (OTP / 2FA / captcha / tap-yes):
```bash
curl -X POST https://linkedin-server.libingo.io/api/accounts/1/checkpoint -H 'Content-Type: application/json' -d '{"code":"123456"}'
# For tap-yes (IN_APP_VALIDATION) send: {"code":""}
```

Create a campaign:
```bash
curl -X POST https://linkedin-server.libingo.io/api/campaigns -H 'Content-Type: application/json' -d '{
  "organisation_code": "ACME",
  "name": "Q3 Founders",
  "include_note": true,
  "connection_note_template": "Hi {first_name}, loved your work as {headline}. Would be great to connect!",
  "initial_message_template": "Thanks for connecting, {first_name}! What are you focused on this quarter?",
  "followup_enabled": true,
  "followup_templates": ["Just floating this back up, {first_name} — keen to hear your thoughts."],
  "followup_interval_days": [3],
  "daily_limit_per_account": 25
}'
```

Assign accounts, add targets, start:
```bash
curl -X POST https://linkedin-server.libingo.io/api/campaigns/1/accounts -d '{"account_ids":[1,2]}' -H 'Content-Type: application/json'

curl -X POST https://linkedin-server.libingo.io/api/campaigns/1/start
curl https://linkedin-server.libingo.io/api/campaigns/1/stats
```

## Scheduler tuning (.env)

| Var | Meaning | Default |
|-----|---------|---------|
| `MAX_CONCURRENT_WORKERS` | Thread-pool size for sends/polls | 20 |
| `EXECUTOR_TICK_SECONDS` | How often to send due invites | 30 |
| `PLANNER_TICK_SECONDS` | How often to (re)plan the day | 900 |
| `POLLER_TICK_SECONDS` | How often to check for due polls | 600 |
| `DEFAULT_DAILY_CONNECT_LIMIT` | Per-account daily cap | 25 |
| `ACCEPTED_CHECKS_PER_DAY` | Acceptance polls per account/day | 2 |

## Acceptance detection

Per [Unipile docs](https://developer.unipile.com/docs/detecting-accepted-invitations),
LinkedIn has no real-time accept event. The poller uses the **relations list**
(recent first) compared against sent-invite tasks, run a few times a day with
random spacing. An optional `POST /api/webhooks/unipile` endpoint also accepts
`new_relation` events to mark acceptances faster.

## Frontend integration

See `FRONTEND_INTEGRATION.md` for:
- All API endpoints and payload schemas
- Step-by-step campaign setup
- Database overview
- Template placeholders and task statuses

## Production HTTPS / domain setup

See `DEPLOYMENT.md` for:
- Nginx reverse proxy configuration
- Let's Encrypt SSL setup
- Docker Compose production profile
- Migration notes from `hiringday_linkedin_login2`
# unipile-li
