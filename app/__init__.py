"""Joveo Publisher Intelligence application package."""

from __future__ import annotations

import logging

from .config import get_settings

_LOGGING_CONFIGURED = False


def configure_logging() -> None:
    """Initialize root logger from settings. Safe to call multiple times."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _LOGGING_CONFIGURED = True
