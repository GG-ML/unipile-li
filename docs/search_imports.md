# LinkedIn Search Import Endpoints

This document describes the three backend endpoints that turn a LinkedIn / Sales Navigator search URL into campaign leads (tasks). It also covers the frontend account-selection rules for Sales Navigator imports.

## Endpoints

| Endpoint | Method | Import type | Daily limit | Page size |
|---|---|---|---|---|
| `/api/campaigns/{campaign_id}/import/linkedin-search` | POST | `linkedin_classic_search` | 1,000 | 50 |
| `/api/campaigns/{campaign_id}/import/sales-navigator-search` | POST | `sales_navigator_search` | 2,500 | 100 |
| `/api/campaigns/{campaign_id}/import/sales-navigator-saved-search` | POST | `sales_navigator_saved_search` | 2,500 | 100 |

## Request / response

### Request body

```json
{
  "account_id": 114,
  "url": "https://www.linkedin.com/search/results/people/?keywords=...",
  "limit": 50
}
```

- `account_id` — LinkedIn account already connected in our system.
- `url` — Copy-pasted search URL from the user.
- `limit` (optional) — Per-import cap. Defaults to the type-based daily limit.

### Response

```json
{
  "status": "started",
  "import_id": 6,
  "account_id": 114,
  "campaign_id": 9,
  "import_type": "sales_navigator_search",
  "message": "Import started in the background. Poll GET /api/campaigns/{campaign_id}/search-imports/{import_id}"
}
```

### Poll status

```
GET /api/campaigns/{campaign_id}/search-imports/{import_id}
```

```json
{
  "id": 6,
  "account_id": 114,
  "campaign_id": 9,
  "import_type": "sales_navigator_search",
  "source_url": "...",
  "status": "running",
  "daily_limit": 2500,
  "target_count": 50,
  "imported_count": 23,
  "total_results": 54011,
  "cursor": "...",
  "last_error": null
}
```

Status values: `pending` → `running` → `completed` / `partial` / `failed`.

## How lead polling works

1. **Queue import**
   - The endpoint creates a `UoSearchImport` row with `status = pending`.
   - It verifies the account is connected and has a `unipile_account_id`.
   - It auto-assigns the account to the campaign (`campaign_service.assign_accounts`) so the scheduler can send invites from it later.
   - It starts a FastAPI `BackgroundTask` that runs `process_search_import(import_id)`.

2. **Background pagination**
   - The worker sets `status = running` and calls Unipile `POST /api/v1/linkedin/search` repeatedly.
   - Each page request is spaced with a random human-like delay (1.5–4 seconds).
   - The page size is `50` for Classic and `100` for Sales Navigator.
   - The cursor returned by Unipile is used to fetch the next page until:
     - the per-import `target_count` is reached,
     - the daily import limit is reached,
     - there are no more results,
     - or Unipile returns an error.

3. **Lead creation**
   - For each `PEOPLE` result, the worker:
     - Caches the profile in `uo_profiles` (provider_id, first_name, last_name, etc.).
     - Creates a `UoTask` in `pending` status for the campaign if the public identifier is not already present.
     - The task is assigned to the same account used for the import.
   - `COMPANY` results are cached but not turned into tasks.

4. **Limit handling**
   - `daily_limit` is hard-coded per type: 1,000 for Classic, 2,500 for Sales Navigator.
   - `target_count` is the per-import cap from the request `limit`.
   - The actual number fetched is `min(remaining_today, target_count)`.
   - `UoDailyCounter.search_imports` tracks how many profiles have been imported per account per day.

5. **Final status**
   - `completed` — all requested leads were imported.
   - `partial` — some leads were imported but the daily limit or the import target was reached (or an error occurred after some pages).
   - `failed` — zero leads were imported and an error occurred.

## Frontend account selection rules

### LinkedIn Classic search

- Any connected LinkedIn account can be used.
- Suggested default: accounts with `linkedin_tier` of `premium` or `basic`.

### Sales Navigator search and saved search

- **Only accounts with `linkedin_tier = "sales_navigator"` should be shown in the account select box.**
- The account must also be connected (`status = "connected"`) and have a `unipile_account_id`.
- If no `sales_navigator` account is connected, show a message such as:
  > "No Sales Navigator account is connected. Please connect a LinkedIn Sales Navigator account to use this import source."

The `linkedin_tier` value is returned by the account endpoints:

```json
{
  "id": 114,
  "email": "connectwithkarthickraj@gmail.com",
  "status": "connected",
  "linkedin_tier": "sales_navigator"
}
```

## Database tables / columns involved

- `uo_accounts` — the importing account. Relevant fields: `id`, `status`, `unipile_account_id`, `linkedin_tier`, `daily_connect_limit`, working hours.
- `uo_search_imports` — tracks each import run: `account_id`, `campaign_id`, `import_type`, `source_url`, `status`, `daily_limit`, `target_count`, `imported_count`, `total_results`, `cursor`, `last_error`.
- `uo_daily_counters` — `search_imports` column counts profiles imported per account per day.
- `uo_profiles` — cached profile data from search results.
- `uo_tasks` — the actual leads created for the campaign; these are picked up by the daily planner and executor.
- `uo_campaign_accounts` — the import endpoint auto-assigns the importing account to the campaign so the scheduler can send invites from it.
