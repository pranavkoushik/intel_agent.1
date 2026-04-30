"""Vercel Python entrypoint — FastAPI app exposing all routes."""

from __future__ import annotations

import datetime
import logging
import os
import sys

# Make the app/ package importable when this file lives under api/.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402

from app import configure_logging  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.publishers import get_todays_publishers  # noqa: E402
from app.scheduler import run_publisher_intel  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Joveo Publisher Intelligence Agent")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "joveo-publisher-intel",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


@app.get("/api/schedule")
def schedule() -> dict:
    label, publishers, coverage_label, next_label = get_todays_publishers()
    return {
        "date": datetime.date.today().isoformat(),
        "weekday": datetime.date.today().strftime("%A"),
        "running_today": publishers is not None,
        "label": label,
        "coverage_label": coverage_label,
        "next_run": next_label,
        "publisher_count": len(publishers) if publishers else 0,
        "publishers": publishers or [],
    }


@app.api_route("/api/cron", methods=["GET", "POST"])
def cron(authorization: str | None = Header(default=None)) -> dict:
    settings = get_settings()

    # Optional bearer-token auth: only enforced if CRON_SECRET is configured.
    if settings.cron_secret:
        expected = f"Bearer {settings.cron_secret}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # Pre-flight: fail loudly with 503 if required API keys are missing, so
    # Vercel logs surface misconfiguration instead of a runtime crash mid-pipeline.
    missing: list[str] = []
    if not settings.slack_webhook_url:
        missing.append("SLACK_WEBHOOK_URL")
    if not settings.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    if not settings.tavily_api_key:
        missing.append("TAVILY_API_KEY")
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Missing required configuration: {', '.join(missing)}",
        )

    try:
        return run_publisher_intel()
    except Exception as exc:
        logger.exception("Cron run failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
