"""End-to-end orchestration of the Publisher Intel job."""

from __future__ import annotations

import datetime
import logging

from . import configure_logging
from .config import get_settings
from .publishers import get_todays_publishers
from .services import (
    deduplicate_news,
    fetch_google_news_rss,
    fetch_news,
    filter_recent_news,
    generate_brief,
    load_sent_urls,
    post_to_slack,
    quick_filter,
    save_sent_urls,
    split_critical,
)

logger = logging.getLogger(__name__)


def run_publisher_intel() -> dict:
    """Run the full pipeline. Used by both the local CLI and the Vercel cron."""
    configure_logging()
    settings = get_settings()

    logger.info("Starting Publisher Intel run")
    label, publishers, coverage_label, _ = get_todays_publishers()

    if publishers is None:
        logger.info("Weekend — skipping run")
        return {"ok": True, "status": "skipped", "reason": "weekend"}

    logger.info("Schedule: %s (%d publishers)", label, len(publishers))

    tavily_items = fetch_news(publishers, settings)
    google_items = fetch_google_news_rss(publishers, settings)
    news = tavily_items + google_items
    logger.info(
        "Fetched %d raw items (tavily=%d, google=%d)",
        len(news), len(tavily_items), len(google_items),
    )

    news = quick_filter(news)
    logger.info("After quick_filter: %d", len(news))

    # Dedup and ledger-filter BEFORE the expensive HTTP-bound date filter so we
    # don't spend fetch_article_date() requests on items we'd drop anyway.
    news = deduplicate_news(news, settings)
    logger.info("After dedup: %d", len(news))

    sent_urls = load_sent_urls(settings)
    news = [item for item in news if item.get("url") not in sent_urls]
    logger.info("After ledger filter: %d", len(news))

    news = filter_recent_news(news, settings)
    logger.info("After date filter: %d", len(news))

    # Split last so critical items lead in Gemini's prompt context. Doing it
    # here (rather than earlier) doesn't change which items reach Gemini —
    # split_critical is purely a labeling/ordering step.
    critical, regular = split_critical(news)
    news = critical + regular
    logger.info("Split: %d critical + %d regular", len(critical), len(regular))

    if not news:
        today_str = datetime.date.today().strftime("%A, %d %B %Y")
        message = (
            f"📡 Joveo Publisher Intel - {today_str}\n\n"
            f"No impactful updates relevant to Joveo were found for "
            f"{coverage_label} within the last few days.\n\n"
            f"Researched via: Tavily\n"
            f"Coverage today: {coverage_label}"
        )
        post_to_slack(message, settings)
        return {
            "ok": True,
            "status": "no_updates",
            "coverage_label": coverage_label,
            "news_count": 0,
        }

    logger.info("Generating brief…")
    brief = generate_brief(news, coverage_label, settings)

    if not brief:
        logger.warning("No brief generated — skipping Slack post")
        return {"ok": False, "status": "no_brief", "coverage_label": coverage_label}

    logger.info("Posting to Slack…")
    success = post_to_slack(brief, settings)

    if success:
        # Persist sent URLs only after Slack confirms delivery so failed posts
        # can be retried on the next run instead of being marked as complete.
        save_sent_urls([item.get("url") for item in news if item.get("url")], settings)
        logger.info("Run complete: Slack delivery succeeded")
    else:
        logger.error("Run complete: Slack delivery failed")

    return {
        "ok": success,
        "status": "posted" if success else "slack_failed",
        "coverage_label": coverage_label,
        "news_count": len(news),
    }


def main() -> None:
    """CLI entrypoint kept for backward compatibility with brief.py."""
    run_publisher_intel()
