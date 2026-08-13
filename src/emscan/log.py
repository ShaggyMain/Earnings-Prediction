"""Logowanie strukturalne — SPEC §Jakość kodu.

Każdy fetch loguje źródło i liczbę rekordów. Nazwa modułu to `log`, a nie `logging`,
żeby nie mylić go ze stdlib wewnątrz pakietu.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Konfiguruje structlog. `json_output=True` w GitHub Actions, tekst lokalnie."""
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Logger związany z modułem."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
