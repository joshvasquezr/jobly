"""
Structured logging: structlog to console (Rich) + JSON file.
Secrets are never passed to log calls — but we add a processor
that redacts common patterns just in case.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import structlog
from rich.logging import RichHandler

_REDACT_PATTERNS = [
    re.compile(r"(sk-ant-[A-Za-z0-9\-_]{20,})", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9]{40,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+[A-Za-z0-9\-_\.]+)", re.IGNORECASE),
]


def _redact_secrets(logger, method, event_dict):  # noqa: ANN001
    """structlog processor: redact API keys from log values."""
    for key, val in list(event_dict.items()):
        if isinstance(val, str):
            for pat in _REDACT_PATTERNS:
                val = pat.sub("[REDACTED]", val)
            event_dict[key] = val
    return event_dict


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """
    Configure structlog with two outputs:
    - Rich console renderer (human-readable, colorised)
    - JSON file renderer (machine-readable, for troubleshooting)
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "jobly.jsonl"

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib root logger
    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        handlers=[
            RichHandler(
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            ),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_secrets,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__):  # noqa: ANN201
    return structlog.get_logger(name)
