"""
BaseAdapter abstract class and supporting types.
All ATS adapters must implement this interface.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import Page

from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class UnknownQuestion:
    """A form field the adapter could not fill automatically."""
    label: str
    field_type: str = "text"     # text | select | radio | checkbox | textarea
    options: list[str] = field(default_factory=list)  # for select/radio
    context: str = ""             # surrounding page text for user context
    selector: Optional[str] = None  # CSS selector to fill after user answers


@dataclass
class FillResult:
    """Summary of what the adapter was able to fill."""
    filled_fields: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    unknown_questions: list[UnknownQuestion] = field(default_factory=list)


# Type alias for the question resolver callback
# Signature: (question_label, ats_type, options, context) -> answer_string
QuestionResolver = Callable[[str, str, list[str], str], str]


class BaseAdapter(ABC):
    """
    Abstract base for all ATS adapters.

    Subclasses implement the five abstract methods. The orchestrator
    (ApplicationRunner) calls them in order:
        1. open_and_prepare(page, url)
        2. fill_form(page, profile, resume_path, resolver) -> FillResult
        3. reach_review_step(page)
        4. [user gate — submit only if confirmed]
        5. submit(page)
    """

    ats_type: str = "unknown"

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Return True if this adapter can handle the given URL."""
        ...

    @abstractmethod
    async def open_and_prepare(self, page: Page, url: str) -> None:
        """
        Navigate to the application URL and wait for it to be ready.
        Accept cookie banners, handle popups, etc.
        """
        ...

    @abstractmethod
    async def fill_form(
        self,
        page: Page,
        profile: dict,
        resume_path: Path,
        resolver: QuestionResolver,
    ) -> FillResult:
        """
        Fill all form fields from `profile`.
        For unknown questions, call resolver(label, ats_type, options, context)
        which will either return a cached answer or ask the user interactively.
        Upload resume from resume_path.
        Returns a FillResult summarising what was filled.
        """
        ...

    @abstractmethod
    async def reach_review_step(self, page: Page) -> None:
        """
        Click through to the final review / confirm page without submitting.
        After this returns, the application should be one submit-click away.
        """
        ...

    @abstractmethod
    async def submit(self, page: Page) -> None:
        """
        Click the final submit button.
        MUST NOT be called unless the user has explicitly confirmed.
        """
        ...

    # ── Shared helpers available to all adapters ──────────────────────────────

    async def _wait(self, page: Page, ms: int = 800) -> None:
        await asyncio.sleep(ms / 1000)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        """Best-effort attempt to dismiss cookie/GDPR banners."""
        selectors = [
            "button:has-text('Accept')",
            "button:has-text('Accept All')",
            "button:has-text('I Agree')",
            "button:has-text('Got it')",
            "[id*='cookie'] button",
            "[class*='cookie'] button",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    log.debug("cookie_banner_dismissed", selector=sel)
                    return
            except Exception:
                pass

    async def _upload_file(self, page: Page, selector: str, path: Path) -> bool:
        """Upload a file to a file input."""
        try:
            await page.locator(selector).set_input_files(str(path))
            log.debug("file_uploaded", path=str(path))
            return True
        except Exception as e:
            log.warning("file_upload_failed", selector=selector, error=str(e))
            return False

    async def _find_label_input(
        self, page: Page, label_text: str, timeout: int = 3000
    ) -> Optional[str]:
        """
        Find an input associated with a label containing label_text.
        Returns a CSS selector or None.
        """
        try:
            # Strategy 1: for/id association
            label = page.locator(f"label:has-text('{label_text}')").first
            for_attr = await label.get_attribute("for", timeout=timeout)
            if for_attr:
                return f"#{for_attr}"
        except Exception:
            pass
        try:
            # Strategy 2: label wrapping an input
            inp = page.locator(f"label:has-text('{label_text}') input").first
            if await inp.is_visible(timeout=timeout):
                return f"label:has-text('{label_text}') input"
        except Exception:
            pass
        return None
