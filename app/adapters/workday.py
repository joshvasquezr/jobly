"""
Workday ATS adapter — GUIDED MODE (best-effort).

Workday is notoriously difficult to fully automate due to:
  - Heavy JavaScript rendering (Angular/React hybrid)
  - CAPTCHA-like bot detection
  - Deeply nested shadow DOM components
  - Highly variable form structure per company

This adapter fills what it safely can (basic fields) and then
pauses to let the user take over for the complex sections.
The 'reach_review_step' and 'submit' are both MANUAL — the adapter
opens the browser and guides the user with instructions.

Workday URLs:
  - https://{company}.wd{n}.myworkdayjobs.com/...
  - https://{company}.wd{n}.myworkdayjobs.com/en-US/{company}/{job-path}
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page

from app.adapters.base import BaseAdapter, FillResult, QuestionResolver
from app.utils.logging import get_logger

log = get_logger(__name__)

_WORKDAY_DOMAIN = re.compile(r"myworkdayjobs\.com|workday\.com/[^/]+/hiring", re.I)


class WorkdayAdapter(BaseAdapter):
    ats_type = "workday"

    GUIDED_MODE_NOTICE = (
        "\n[bold yellow]⚠  Workday Guided Mode[/bold yellow]\n"
        "Workday cannot be fully automated. jobly will:\n"
        "  1. Open the job application in your browser\n"
        "  2. Pre-fill basic fields where possible\n"
        "  3. Pause and guide you through the rest\n"
        "  4. Wait for you to reach the review screen\n"
        "  5. Ask for confirmation before submitting\n"
    )

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_WORKDAY_DOMAIN.search(url))

    async def open_and_prepare(self, page: Page, url: str) -> None:
        log.info("workday_open_guided", url=url)
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await self._dismiss_cookie_banner(page)
        await self._wait(page, 3000)
        # Try to click Apply button if on listing page
        await self._click_apply(page)
        await self._wait(page, 2000)

    async def fill_form(
        self,
        page: Page,
        profile: dict,
        resume_path: Path,
        resolver: QuestionResolver,
    ) -> FillResult:
        result = FillResult()
        personal = profile.get("personal", {})

        # Workday uses complex shadow DOM — we attempt basic fields only
        # and fall through gracefully on any failure

        # ── Email ─────────────────────────────────────────────────────────────
        await self._try_fill_email(page, result, personal.get("email", ""))

        # ── Resume / file upload ──────────────────────────────────────────────
        await self._try_upload_resume(page, result, resume_path)

        # ── Guided notice ─────────────────────────────────────────────────────
        result.unknown_questions = []  # no unknown questions — it's all manual
        log.info("workday_guided_mode_active")

        return result

    async def reach_review_step(self, page: Page) -> None:
        """
        In guided mode, we cannot reliably navigate to the review step.
        We wait for the user to get there manually.
        This method returns immediately — the CLI handles the manual gate.
        """
        log.info("workday_waiting_for_manual_review")

    async def submit(self, page: Page) -> None:
        """
        Attempt to click the Workday submit button.
        Because form structure varies, we try common selectors.
        If none work, raise so the CLI can inform the user.
        """
        selectors = [
            "button[data-automation-id='bottom-navigation-next-button']",
            "button[data-automation-id='submitButton']",
            "button:has-text('Submit')",
            "button:has-text('Apply')",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    log.info("workday_submit_clicked")
                    await self._wait(page, 2000)
                    return
            except Exception:
                continue
        raise RuntimeError(
            "Could not find Workday submit button. "
            "Please submit manually in the browser."
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _click_apply(self, page: Page) -> None:
        selectors = [
            "a[data-automation-id='applyButton']",
            "button[data-automation-id='applyButton']",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await self._wait(page, 2000)
                    return
            except Exception:
                pass

    async def _try_fill_email(
        self, page: Page, result: FillResult, email: str
    ) -> None:
        if not email:
            return
        selectors = [
            "input[data-automation-id='email']",
            "input[type='email']",
            "input[name*='email' i]",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(email)
                    result.filled_fields.append("email")
                    return
            except Exception:
                pass

    async def _try_upload_resume(
        self, page: Page, result: FillResult, resume_path: Path
    ) -> None:
        try:
            file_input = page.locator("input[type='file']").first
            if await file_input.count() > 0:
                await file_input.set_input_files(str(resume_path))
                result.filled_fields.append("resume")
                await self._wait(page, 1000)
                return
        except Exception:
            pass

        # Try Workday's upload widget
        try:
            async with page.expect_file_chooser(timeout=4000) as fc_info:
                await page.locator(
                    "button[data-automation-id='file-upload-button'],"
                    " button:has-text('Upload')"
                ).first.click()
            fc = await fc_info.value
            await fc.set_files(str(resume_path))
            result.filled_fields.append("resume")
            await self._wait(page, 1000)
        except Exception:
            result.skipped_fields.append("resume")
