"""FastAPI application entrypoint (API layer only).

The scheduler/worker runs as a separate process (app/worker.py) so the API and
the automation engine can be scaled independently.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import accounts, campaigns, search_imports, webhooks
from app.db.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Unipile LinkedIn Outreach Backend",
    description="Account linking + templated outreach campaigns with slot scheduling.",
    version="1.0.0",
)

app.include_router(accounts.router)
app.include_router(campaigns.router)
app.include_router(search_imports.router)
app.include_router(webhooks.router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("API started and database ensured")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
