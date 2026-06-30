# Frontend Integration Guide — Unipile LinkedIn Outreach API

This guide is for the frontend team consuming the **Unipile LinkedIn Outreach Backend**.

The backend is a FastAPI service that links LinkedIn accounts via Unipile, creates templated outreach campaigns, and runs a slot-based scheduler to send connection requests, initial messages, and follow-ups automatically within configured working hours.

---

## Base URL

| Environment | URL | Notes |
|---|---|---|
| Local dev | `http://localhost:8000` | Run `uvicorn app.main:app --port 8000` |
| Docker | `http://localhost:8000` | `docker compose up` exposes port `8000` |
| Production | `https://linkedin-server.libingo.io` | Nginx reverse proxy + SSL (see deployment guide) |

**Live deployment status:** The production stack is running. Verified endpoints:

```bash
curl https://linkedin-server.libingo.io/health
curl https://linkedin-server.libingo.io/api/campaigns/1/stats
```

All endpoints are prefixed with `/api/` except `/health`, `/docs`, and `/openapi.json`.

---

## Authentication

The API currently does **not** require API keys or JWT tokens. Access is network-based (VPN / internal IP / Nginx). If auth is added later, this document will be updated.

---

## Quick health check

```bash
curl https://linkedin-server.libingo.io/health
```

Response:
```json
{"status": "ok"}
```

Interactive docs (Swagger UI) are available at:
```
https://linkedin-server.libingo.io/docs
https://linkedin-server.libingo.io/openapi.json
```

---

## End-to-end setup flow

### 1. Link a LinkedIn account (async)

`POST /api/accounts/connect`

The API returns immediately with an `account_id`. The actual Unipile credential flow runs in the background so the HTTP request never times out (important for Vercel's 60-second limit).

```json
{
  "organisation_code": "ACME",
  "email": "linkedinmail@gmail.com",
  "password": "••••••",
  "country_code": "IN",
  "timezone": "Asia/Kolkata",
  "working_hours_start": 9,
  "working_hours_end": 18,
  "working_days": [0, 1, 2, 3, 4],
  "daily_connect_limit": 10,
  "min_delay_seconds": 180,
  "max_delay_seconds": 600
}
```

Response:
```json
{
  "status": "started",
  "account_id": 1,
  "message": "Account linking started in the background. Poll GET /api/accounts/{account_id}/status."
}
```

**Frontend flow:**
1. Call `POST /api/accounts/connect` and save the `account_id`.
2. Poll `GET /api/accounts/{account_id}/status` every 3–5 seconds.
3. When `status` becomes `connected`, linking is done.
4. If `status` is `checkpoint`, look at `checkpoint.checkpoint_type` and ask the user for input.
5. Submit the input via `POST /api/accounts/{account_id}/checkpoint` and continue polling.
6. If `status` is `failed`, show `last_error`.

**Important:** `working_hours_*`, `working_days`, and `daily_connect_limit` control when and how many connection requests are sent per day.

### 2. Poll link status

`GET /api/accounts/{account_id}/status`

Example responses:

```json
// Linking in progress
{
  "account_id": 1,
  "status": "pending",
  "done": false,
  "email": "linkedinmail@gmail.com",
  "unipile_account_id": null,
  "name": null,
  "last_error": null,
  "checkpoint": null
}

// Checkpoint waiting for user input
{
  "account_id": 1,
  "status": "checkpoint",
  "done": false,
  "email": "linkedinmail@gmail.com",
  "unipile_account_id": null,
  "name": null,
  "last_error": null,
  "checkpoint": {
    "intent_id": "...",
    "checkpoint_type": "IN_APP_VALIDATION",
    "status": "pending",
    "detail": { ... }
  }
}

// Linked successfully
{
  "account_id": 1,
  "status": "connected",
  "done": true,
  "email": "linkedinmail@gmail.com",
  "unipile_account_id": "LuHJQE7MRtObdVhw-BCT0A",
  "name": "...",
  "last_error": null,
  "checkpoint": null
}
```

**Stop polling when `done` is `true`.** The final `status` is then either `connected` or `failed` (see `last_error`). Do not treat the intermediate `submitted`/`processing` checkpoint responses as terminal.

### 3. Solve a checkpoint (if needed)

`POST /api/accounts/{account_id}/checkpoint`

#### Checkpoint types

| Type | Frontend action | Payload |
|---|---|---|
| `OTP` / `2FA` / `EMAIL_VALIDATION` / `PHONE_REGISTER` | Ask user for the code | `{"code": "123456"}` |
| `CAPTCHA` | Show the `captcha_url` image and ask for the text | `{"code": "abc123"}` |
| `IN_APP_VALIDATION` (tap **Yes** in LinkedIn app) | Prompt user to open the LinkedIn mobile app and tap **Yes, it's me**. **Do NOT submit a checkpoint.** Just keep polling status. | _(no checkpoint call needed)_ |
| Unknown | Log the raw response and ask the user | `{"code": ""}` (fallback) |

For `IN_APP_VALIDATION` there is **no code to submit**. The user approves the login in the LinkedIn mobile app, and the backend automatically polls Unipile until the account connects. The frontend should simply keep polling `GET /api/accounts/{account_id}/status` until `done` is `true` (status becomes `connected`, or `failed` if the user never approves within the timeout).

Response after submitting a checkpoint:
```json
{
  "status": "submitted",
  "account_id": 1,
  "intent_id": "...",
  "checkpoint_type": "IN_APP_VALIDATION"
}
```

### 4. List / inspect accounts

`GET /api/accounts?organisation_code=ACME`

`GET /api/accounts/{account_id}`

### 5. Create a campaign

`POST /api/campaigns`

```json
{
  "organisation_code": "ACME",
  "name": "Gowtham AI Engineer Outreach - sample test",
  "include_note": true,
  "connection_note_template": "Hi {first_name}, I came across your profile and your work at {company} really stood out to me. I'm an AI engineer focused on GenAI and production ML systems, and I'd love to connect with professionals building ambitious tech like yours.",
  "initial_message_template": "Thanks for connecting, {first_name}.",
  "followup_enabled": true,
  "followup_templates": [
    "Hi {first_name}, I wanted to follow up. I'm curious whether {company}"
  ],
  "followup_interval_days": [3],
  "daily_limit_per_account": 10,
  "min_delay_seconds": 180,
  "max_delay_seconds": 600
}
```

Response:
```json
{
  "id": 1,
  "organisation_code": "ACME",
  "name": "Gowtham AI Engineer Outreach - sample test",
  "status": "draft",
  "include_note": true,
  "followup_enabled": true,
  "daily_limit_per_account": 10
}
```

### 6. Assign accounts to the campaign

`POST /api/campaigns/{campaign_id}/accounts`

```json
{
  "account_ids": [1]
}
```

### 7. Add targets

`POST /api/campaigns/{campaign_id}/targets`

```json
{
  "targets": [
    {"linkedin_url": "https://www.linkedin.com/in/niclas-stoltenberg-43990b58/"},
    {"linkedin_url": "https://www.linkedin.com/in/pooryaparsa/"},
    {"linkedin_url": "https://www.linkedin.com/in/saravanasankar-m-79231099/"}
  ]
}
```

Each target creates a `uo_task` row in status `pending`.

### 8. Start the campaign

`POST /api/campaigns/{campaign_id}/start`

The scheduler will:
- Pick up to `daily_connect_limit` pending tasks per account per day
- Assign random `scheduled_at` times inside the account's working window
- Send connection requests when the slot time arrives
- Poll twice per day for accepted invites and send the initial message
- Send follow-ups if no reply is received after the configured interval

### 9. Get campaign stats

`GET /api/campaigns/{campaign_id}/stats`

```json
{
  "campaign_id": 1,
  "total_targets": 28,
  "by_status": {
    "invite_sent": 1,
    "scheduled": 9,
    "pending": 18
  },
  "sent": 1,
  "accepted": 0,
  "replied": 0
}
```

### 10. Pause / resume

- `POST /api/campaigns/{campaign_id}/pause` — stops scheduling new tasks
- `POST /api/campaigns/{campaign_id}/start` — resumes

Pausing does **not** cancel already-scheduled tasks.

---

## Template placeholders

Use these in `connection_note_template`, `initial_message_template`, and `followup_templates`:

| Placeholder | Source |
|---|---|
| `{first_name}` | Target profile (scraped from LinkedIn via Unipile) |
| `{last_name}` | Target profile |
| `{full_name}` | Target profile |
| `{headline}` | Target profile headline |
| `{company}` | Target profile current company |
| `{location}` | Target profile location |

If a value is missing, the placeholder is replaced with an empty string. Connection notes are limited to 300 characters.

---

## Task statuses

| Status | Meaning |
|---|---|
| `pending` | Not yet scheduled |
| `scheduled` | Has a `scheduled_at` slot today |
| `invite_sent` | Connection request sent, waiting for acceptance |
| `accepted` | Accepted, initial message not yet sent |
| `initial_sent` | Initial message sent, waiting for reply or follow-up |
| `replied` | Target replied — manual handoff |
| `completed` | Follow-up sequence finished with no reply |
| `failed` | Send failed after retry |
| `skipped` | Already a connection or not sendable |

---

## Database

The backend uses the **shared Libingo PostgreSQL database**. New tables are prefixed with `uo_` so they coexist with existing `li_` and `lc_` tables.

### Tables created

- `uo_accounts` — LinkedIn accounts linked via Unipile
- `uo_auth_intents` — In-progress OTP/2FA/captcha checkpoints
- `uo_campaigns` — Outreach campaigns and templates
- `uo_campaign_accounts` — Many-to-many campaign ↔ account assignments
- `uo_tasks` — One row per target; holds status, scheduling, and lifecycle timestamps
- `uo_profiles` — Cache of scraped LinkedIn profile data
- `uo_daily_counters` — Per-account daily send counters
- `uo_logs` — Structured application logs

Key point for the frontend: campaign progress is read from `uo_tasks.status` via `GET /api/campaigns/{id}/stats`.

---

## Architecture & flow

```
Frontend (your app)
   │
   │  HTTPS
   ▼
Nginx ──▶ FastAPI (port 8000)
           │
           ├─ POST /api/accounts/connect
           ├─ POST /api/campaigns
           ├─ POST /api/campaigns/{id}/targets
           ├─ POST /api/campaigns/{id}/start
           └─ GET  /api/campaigns/{id}/stats
           │
           ▼
    PostgreSQL (uo_* tables)
           ▲
           │
    Worker (python -m app.worker)
      ├─ Planner  ── schedules invites inside working hours
      ├─ Executor ── sends due connection requests via Redis lock
      ├─ Poller   ── detects acceptances, sends initial message & follow-ups
      └─ uses Redis for distributed locks + daily counters
           │
           ▼
      Unipile API (LinkedIn proxy)
```

### Worker jobs

| Job | Frequency | Purpose |
|---|---|---|
| `plan_all_accounts` | every 15 minutes | Assigns today’s slots and acceptance-poll times |
| `run_executor_tick` | every 30 seconds | Sends due connection invites |
| `run_poller_tick` | every 10 minutes | Runs any due acceptance polls |

### Acceptance detection

LinkedIn does not push a real-time "accepted" event. The primary detection is the **relations list** polled a few times per day. You can optionally register `POST /api/webhooks/unipile` with Unipile for `new_relation` events to speed this up.

---

## Webhooks

`POST /api/webhooks/unipile`

Unipile can push events to this endpoint. Currently handled:

- `new_relation` — marks a sent invite as accepted and triggers the initial message

Example payload from Unipile:
```json
{
  "event": "new_relation",
  "account_id": "LuHJQE7MRtObdVhw-BCT0A",
  "user_provider_id": "ACoAAAA..."
}
```

---

## Domain / HTTPS

Yes, you need a domain for production HTTPS. The project includes:

- `nginx.conf` — reverse proxy with CORS for frontend origins
- `docker-compose.yml` — `nginx` + `certbot` services (production profile)
- `setup-ssl.sh` — interactive SSL setup (self-signed or Let's Encrypt)

See `DEPLOYMENT.md` for full instructions.

Default domain configured: `linkedin-server.libingo.io`. Change the `server_name` in `nginx.conf` and the domain prompt in `setup-ssl.sh` if you want a different one.

---

## Common curl snippets

```bash
# Health
curl https://linkedin-server.libingo.io/health

# Connect account (returns immediately)
curl -X POST https://linkedin-server.libingo.io/api/accounts/connect \
  -H 'Content-Type: application/json' \
  -d '{"organisation_code":"ACME","email":"user@example.com","password":"secret","country_code":"IN","timezone":"Asia/Kolkata","working_hours_start":9,"working_hours_end":18,"working_days":[0,1,2,3,4],"daily_connect_limit":10}'

# Poll link status
watch -n 5 'curl -s https://linkedin-server.libingo.io/api/accounts/1/status | python3 -m json.tool'

# Submit checkpoint (OTP / 2FA / captcha / tap-yes)
curl -X POST https://linkedin-server.libingo.io/api/accounts/1/checkpoint \
  -H 'Content-Type: application/json' \
  -d '{"code":"123456"}'

# Create campaign
curl -X POST https://linkedin-server.libingo.io/api/campaigns \
  -H 'Content-Type: application/json' \
  -d '{"organisation_code":"ACME","name":"Founders","include_note":true,"connection_note_template":"Hi {first_name}, would love to connect.","initial_message_template":"Thanks for connecting!","followup_enabled":false,"daily_limit_per_account":10}'

# Assign account, add target, start
curl -X POST https://linkedin-server.libingo.io/api/campaigns/1/accounts \
  -H 'Content-Type: application/json' \
  -d '{"account_ids":[1]}'

curl -X POST https://linkedin-server.libingo.io/api/campaigns/1/targets \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"linkedin_url":"https://www.linkedin.com/in/example/"}]}'

curl -X POST https://linkedin-server.libingo.io/api/campaigns/1/start

# Stats
curl https://linkedin-server.libingo.io/api/campaigns/1/stats
```

---

## Support / debugging

- API logs: `docker compose logs -f api`
- Worker logs: `docker compose logs -f worker`
- Scheduler logs: `tail -f /tmp/uo_worker.log` (local)
- Nginx logs: `docker compose logs -f nginx`
- Database: shared Libingo Postgres at `145.223.20.160:25577/libingo`

For any issues, capture the `last_error` field on accounts/tasks and the `uo_logs` table.
