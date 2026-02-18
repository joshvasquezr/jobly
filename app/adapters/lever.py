"""
Lever ATS adapter.

Lever URLs:
  - https://jobs.lever.co/{company}/{job-id}
  - https://jobs.lever.co/{company}/{job-id}/apply

Lever uses a React-based SPA. The application form is typically
on a separate /apply page linked from the job listing.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page

from app.adapters.base import BaseAdapter, FillResult, QuestionResolver, UnknownQuestion
from app.utils.logging import get_logger

log = get_logger(__name__)

_LEVER_DOMAIN = re.compile(r"jobs\.lever\.co", re.I)


class LeverAdapter(BaseAdapter):
    ats_type = "lever"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_LEVER_DOMAIN.search(url))

    async def open_and_prepare(self, page: Page, url: str) -> None:
        log.info("lever_open", url=url)
        # If URL doesn't end with /apply, navigate to the apply page
        apply_url = url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"
        await page.goto(apply_url, wait_until="networkidle", timeout=30000)
        await self._dismiss_cookie_banner(page)
        await self._wait(page, 1500)
        try:
            await page.wait_for_selector(
                "form.application-form, #application-form, .lever-application-form,"
                " form[data-qa='application-form']",
                timeout=12000,
            )
        except Exception:
            log.warning("lever_form_not_found", url=apply_url)

    async def fill_form(
        self,
        page: Page,
        profile: dict,
        resume_path: Path,
        resolver: QuestionResolver,
    ) -> FillResult:
        result = FillResult()
        personal = profile.get("personal", {})
        work_auth = profile.get("work_authorization", {})

        # ── Full name (Lever typically uses a single name field) ──────────────
        full_name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
        await self._fill(page, result,
                         "input[name='name'], input[id='name'], input[placeholder*='name' i],"
                         " input[data-qa='name-field']",
                         full_name, "name")

        # ── Email ─────────────────────────────────────────────────────────────
        await self._fill(page, result,
                         "input[name='email'], input[id='email'], input[type='email'],"
                         " input[data-qa='email-field']",
                         personal.get("email", ""), "email")

        # ── Phone ─────────────────────────────────────────────────────────────
        await self._fill(page, result,
                         "input[name='phone'], input[id='phone'], input[type='tel'],"
                         " input[data-qa='phone-field']",
                         personal.get("phone", ""), "phone")

        # ── Organisation / Current company ────────────────────────────────────
        await self._fill(page, result,
                         "input[name='org'], input[id='org'], input[placeholder*='company' i],"
                         " input[placeholder*='organization' i], input[data-qa='org-field']",
                         "", "org", optional=True)  # intentionally blank for students

        # ── LinkedIn ──────────────────────────────────────────────────────────
        await self._fill(page, result,
                         "input[name='urls[LinkedIn]'], input[placeholder*='linkedin' i],"
                         " input[data-qa='linkedin-field']",
                         personal.get("linkedin_url", ""), "linkedin", optional=True)

        # ── GitHub ────────────────────────────────────────────────────────────
        await self._fill(page, result,
                         "input[name='urls[GitHub]'], input[placeholder*='github' i],"
                         " input[data-qa='github-field']",
                         personal.get("github_url", ""), "github", optional=True)

        # ── Portfolio / website ───────────────────────────────────────────────
        await self._fill(page, result,
                         "input[name='urls[Portfolio]'], input[placeholder*='portfolio' i],"
                         " input[placeholder*='website' i], input[data-qa='portfolio-field']",
                         personal.get("website_url", ""), "portfolio", optional=True)

        # ── Resume upload ─────────────────────────────────────────────────────
        uploaded = await self._upload_resume(page, resume_path)
        (result.filled_fields if uploaded else result.skipped_fields).append("resume")

        # ── Location ──────────────────────────────────────────────────────────
        loc = personal.get("location", {})
        await self._fill(page, result,
                         "input[name='location'], input[placeholder*='location' i]",
                         f"{loc.get('city', '')}, {loc.get('state', '')}",
                         "location", optional=True)

        # ── Custom questions ──────────────────────────────────────────────────
        await self._handle_custom_questions(page, result, resolver)

        return result

    async def reach_review_step(self, page: Page) -> None:
        """
        Lever is typically single-page — scroll to bottom for review.
        Some Lever forms have a multi-step flow via 'Continue' buttons.
        """
        for _ in range(4):
            clicked = await self._click_continue(page)
            if not clicked:
                break
            await self._wait(page, 1200)

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._wait(page, 500)
        log.info("lever_review_ready")

    async def submit(self, page: Page) -> None:
        selectors = [
            "button[type='submit']:has-text('Submit application')",
            "button[type='submit']:has-text('Submit')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "input[type='submit']",
            "[data-qa='btn-submit']",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    log.info("lever_submitted")
                    await self._wait(page, 2000)
                    return
            except Exception:
                continue
        raise RuntimeError("Could not find Lever submit button")

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _fill(
        self,
        page: Page,
        result: FillResult,
        selector: str,
        value: str,
        field_name: str,
        optional: bool = False,
    ) -> bool:
        if not value:
            if not optional:
                result.skipped_fields.append(field_name)
            return False
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=2000):
                await loc.clear()
                await loc.fill(value)
                result.filled_fields.append(field_name)
                log.debug("lever_field_filled", field=field_name)
                return True
        except Exception:
            pass
        if not optional:
            result.skipped_fields.append(field_name)
        return False

    async def _upload_resume(self, page: Page, resume_path: Path) -> bool:
        selectors = [
            "input[type='file'][name='resume']",
            "input[type='file'][accept*='pdf']",
            "input[type='file']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.set_input_files(str(resume_path))
                    await self._wait(page, 800)
                    log.debug("lever_resume_uploaded")
                    return True
            except Exception:
                continue

        # Try the drop-zone / "Upload Resume" button
        try:
            async with page.expect_file_chooser(timeout=3000) as fc_info:
                await page.locator(
                    "label:has-text('Resume'), button:has-text('Upload')"
                ).first.click()
            fc = await fc_info.value
            await fc.set_files(str(resume_path))
            await self._wait(page, 800)
            return True
        except Exception:
            pass

        return False

    async def _click_continue(self, page: Page) -> bool:
        selectors = [
            "button:has-text('Continue')",
            "button:has-text('Next')",
            "button[type='submit']:not(:has-text('Submit'))",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    return True
            except Exception:
                pass
        return False

    async def _handle_custom_questions(
        self,
        page: Page,
        result: FillResult,
        resolver: QuestionResolver,
    ) -> None:
        """Handle Lever custom application questions."""
        try:
            question_blocks = await page.locator(
                ".application-question, [data-qa='application-question'], "
                ".lever-custom-questions li"
            ).all()
        except Exception:
            question_blocks = []

        for block in question_blocks:
            try:
                # Get the question label
                label_el = block.locator("label, .question-label, p").first
                label_text = ""
                try:
                    label_text = (await label_el.inner_text(timeout=500)).strip()
                except Exception:
                    pass
                if not label_text:
                    continue

                # Find the input within this block
                inp = block.locator(
                    "input[type='text'], textarea, select"
                ).first
                try:
                    inp_type = await inp.get_attribute("type", timeout=500) or "text"
                except Exception:
                    continue

                current_val = ""
                try:
                    current_val = await inp.input_value(timeout=500)
                except Exception:
                    pass

                if current_val:
                    continue

                if inp_type == "select" or await inp.evaluate("el => el.tagName") == "SELECT":
                    options = []
                    try:
                        opt_els = await inp.locator("option").all()
                        options = [await o.inner_text() for o in opt_els]
                    except Exception:
                        pass
                    answer = resolver(label_text, self.ats_type, options, label_text)
                    if answer:
                        try:
                            await inp.select_option(label=answer)
                        except Exception:
                            await inp.select_option(value=answer)
                else:
                    answer = resolver(label_text, self.ats_type, [], label_text)
                    if answer:
                        await inp.fill(answer)

                if answer:
                    result.filled_fields.append(f"custom:{label_text[:30]}")
                result.unknown_questions.append(
                    UnknownQuestion(label=label_text, field_type=inp_type)
                )
            except Exception:
                continue
