"""
Adapter registry and ATS detection.
"""

from __future__ import annotations

from typing import Optional, Type

from app.adapters.base import BaseAdapter
from app.adapters.ashby import AshbyAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.workday import WorkdayAdapter
from app.utils.hashing import detect_ats_from_url
from app.utils.logging import get_logger

log = get_logger(__name__)

# Ordered by preference / reliability
_REGISTRY: list[Type[BaseAdapter]] = [
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    WorkdayAdapter,
]


def get_adapter(url: str) -> Optional[BaseAdapter]:
    """
    Return an instantiated adapter for the given URL, or None if unsupported.
    Detection is first by URL pattern (can_handle), then by heuristics.
    """
    for adapter_cls in _REGISTRY:
        if adapter_cls.can_handle(url):
            log.debug("adapter_selected", adapter=adapter_cls.ats_type, url=url)
            return adapter_cls()
    log.warning("no_adapter_found", url=url)
    return None


def detect_ats(url: str) -> str:
    """Return the ATS type string for a URL (for display/storage)."""
    adapter = get_adapter(url)
    if adapter:
        return adapter.ats_type
    return detect_ats_from_url(url)


__all__ = [
    "BaseAdapter",
    "AshbyAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "WorkdayAdapter",
    "get_adapter",
    "detect_ats",
]
