"""
Ashby ATS adapter.

Ashby URLs:
  - https://jobs.ashbyhq.com/{company}/{job-id}
  - https://app.ashbyhq.com/jobs/{company}/{job-id}

Ashby uses a React SPA. Forms are rendered with role="form" or
labelled inputs. Multi-step forms use a Next/Continue button flow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from app.adapters.base import BaseAdapter, FillResult, QuestionResolver, UnknownQuestion
from app.utils.logging import get_logger

log = get_logger(__name__)

_ASHBY_DOMAINS = re.compile(r"ashbyhq\.com|ashby\.com", re.I)


class AshbyAdapter(BaseAdapter):
    ats_type = "ashby"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_ASHBY_DOMAINS.search(url))

    async def open_and_prepare(self, page: Page, url: str) -> None:
        log.info("ashby_open", url=url)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._wait(page, 2000)
        await self._dismiss_cookie_banner(page)

        # Wait for the application form or job listing to appear
        try:
            await page.wait_for_selector(
                "[data-ashby-application-form], form, [role='form'], .ashby-job-posting-brief-description",
                timeout=15000,
            )
        except Exception:
            log.warning("ashby_form_not_found", url=url)

    async def fill_form(
        self,
        page: Page,
        profile: dict,
        resume_path: Path,
        resolver: QuestionResolver,
    ) -> FillResult:
        result = FillResult()
        personal = profile.get("personal", {})
        education = profile.get("education", [{}])[0]

        # ── Click "Apply" button if we're on the job listing page ─────────────
        await self._click_apply_button(page)
        await self._wait(page, 1500)

        # ── Name ──────────────────────────────────────────────────────────────
        await self._fill_field(
            page, result,
            selectors=["input[name*='name'][name*='first' i]", "input[placeholder*='first' i]",
                       "input[id*='first' i]", "input[aria-label*='first name' i]"],
            value=personal.get("first_name", ""),
            field_name="first_name",
        )
        await self._fill_field(
            page, result,
            selectors=["input[name*='name'][name*='last' i]", "input[placeholder*='last' i]",
                       "input[id*='last' i]", "input[aria-label*='last name' i]"],
            value=personal.get("last_name", ""),
            field_name="last_name",
        )
        # Some Ashby forms have a combined "Full Name" field
        full_name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
        await self._fill_field(
            page, result,
            selectors=["input[name*='fullName' i]", "input[placeholder*='full name' i]",
                       "input[aria-label*='full name' i]"],
            value=full_name,
            field_name="full_name",
            optional=True,
        )

        # ── Email ─────────────────────────────────────────────────────────────
        await self._fill_field(
            page, result,
            selectors=["input[type='email']", "input[name*='email' i]", "input[id*='email' i]"],
            value=personal.get("email", ""),
            field_name="email",
        )

        # ── Phone ─────────────────────────────────────────────────────────────
        await self._fill_field(
            page, result,
            selectors=["input[type='tel']", "input[name*='phone' i]", "input[id*='phone' i]"],
            value=personal.get("phone", ""),
            field_name="phone",
        )

        # ── Location / City ───────────────────────────────────────────────────
        loc = personal.get("location", {})
        location_str = f"{loc.get('city', '')}, {loc.get('state', '')}"
        await self._fill_field(
            page, result,
            selectors=["input[name*='location' i]", "input[placeholder*='location' i]",
                       "input[id*='location' i]", "input[placeholder*='city' i]"],
            value=location_str,
            field_name="location",
            optional=True,
        )

        # ── LinkedIn ──────────────────────────────────────────────────────────
        await self._fill_field(
            page, result,
            selectors=["input[name*='linkedin' i]", "input[placeholder*='linkedin' i]",
                       "input[id*='linkedin' i]", "input[aria-label*='linkedin' i]"],
            value=personal.get("linkedin_url", ""),
            field_name="linkedin",
            optional=True,
        )

        # ── GitHub / Portfolio ────────────────────────────────────────────────
        await self._fill_field(
            page, result,
            selectors=["input[name*='github' i]", "input[placeholder*='github' i]",
                       "input[id*='github' i]"],
            value=personal.get("github_url", ""),
            field_name="github",
            optional=True,
        )
        await self._fill_field(
            page, result,
            selectors=["input[name*='website' i]", "input[name*='portfolio' i]",
                       "input[placeholder*='website' i]"],
            value=personal.get("website_url", ""),
            field_name="website",
            optional=True,
        )

        # ── Resume upload ─────────────────────────────────────────────────────
        uploaded = await self._upload_resume(page, resume_path)
        if uploaded:
            result.filled_fields.append("resume")
        else:
            result.skipped_fields.append("resume")

        # ── Work authorisation ────────────────────────────────────────────────
        work_auth = profile.get("work_authorization", {})
        await self._handle_work_auth(page, result, work_auth)

        # ── Custom / unknown questions ────────────────────────────────────────
        await self._handle_custom_questions(page, result, resolver)

        return result

    async def reach_review_step(self, page: Page) -> None:
        """
        Click through multi-step form to the final review page.
        Ashby typically has Next/Continue buttons between sections.
        """
        max_clicks = 8
        for i in range(max_clicks):
            clicked = await self._click_next_or_continue(page)
            if not clicked:
                break
            await self._wait(page, 1000)
            # Check if we're on a review/submit page
            if await self._is_review_page(page):
                log.info("ashby_review_page_reached")
                return
        log.info("ashby_form_navigation_complete")

    async def submit(self, page: Page) -> None:
        """Click the final submit button. Only called after user YES."""
        selectors = [
            "button[type='submit']:has-text('Submit')",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "[data-ashby-application-form-submit]",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    log.info("ashby_submitted")
                    await self._wait(page, 2000)
                    return
            except Exception:
                continue
        raise RuntimeError("Could not find Ashby submit button")

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _click_apply_button(self, page: Page) -> None:
        selectors = [
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await self._wait(page, 1500)
                    return
            except Exception:
                pass

    async def _fill_field(
        self,
        page: Page,
        result: FillResult,
        selectors: list[str],
        value: str,
        field_name: str,
        optional: bool = False,
    ) -> bool:
        if not value:
            if not optional:
                result.skipped_fields.append(field_name)
            return False
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.clear()
                    await loc.fill(value)
                    result.filled_fields.append(field_name)
                    log.debug("ashby_field_filled", field=field_name)
                    return True
            except Exception:
                continue
        if not optional:
            result.skipped_fields.append(field_name)
        return False

    async def _upload_resume(self, page: Page, resume_path: Path) -> bool:
        selectors = [
            "input[type='file'][accept*='pdf']",
            "input[type='file'][accept*='.pdf']",
            "input[type='file']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.set_input_files(str(resume_path))
                    await self._wait(page, 1000)
                    log.debug("ashby_resume_uploaded", path=str(resume_path))
                    return True
            except Exception:
                continue
        # Try drag-and-drop zone with file chooser
        try:
            drop_zone = page.locator(
                "[data-ashby-upload], [class*='upload'], [class*='dropzone']"
            ).first
            if await drop_zone.is_visible(timeout=2000):
                async with page.expect_file_chooser() as fc_info:
                    await drop_zone.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(str(resume_path))
                await self._wait(page, 1000)
                return True
        except Exception:
            pass
        return False

    async def _handle_work_auth(
        self, page: Page, result: FillResult, work_auth: dict
    ) -> None:
        authorized = work_auth.get("authorized_us", True)
        sponsorship = work_auth.get("requires_sponsorship", False)

        # Authorization radio buttons
        auth_text = "Yes" if authorized else "No"
        auth_selectors = [
            f"input[type='radio'][value*='yes' i]",
            f"label:has-text('authorized') input[type='radio']",
        ]
        for sel in auth_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    await loc.click()
                    result.filled_fields.append("work_authorization")
                    break
            except Exception:
                pass

        # Sponsorship
        spons_val = "Yes" if sponsorship else "No"
        spons_selectors = [
            f"label:has-text('sponsor') input[type='radio'][value*='{'yes' if sponsorship else 'no'}' i]",
        ]
        for sel in spons_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    await loc.click()
                    result.filled_fields.append("sponsorship")
                    break
            except Exception:
                pass

    async def _handle_custom_questions(
        self,
        page: Page,
        result: FillResult,
        resolver: QuestionResolver,
    ) -> None:
        """Find unfilled required fields and ask user via resolver."""
        # Look for visible, empty required inputs/textareas not already handled
        try:
            inputs = await page.locator(
                "input:visible:not([type='hidden']):not([type='file'])"
                ":not([type='radio']):not([type='checkbox'])"
                ", textarea:visible"
            ).all()
        except Exception:
            return

        for inp in inputs:
            try:
                current_val = await inp.input_value(timeout=1000)
                if current_val:
                    continue  # already filled
                # Find the label
                label_text = await self._get_label_for_element(page, inp)
                if not label_text:
                    continue
                # Skip if it looks like a field we already handled
                label_lower = label_text.lower()
                if any(kw in label_lower for kw in [
                    "first name", "last name", "email", "phone",
                    "linkedin", "github", "website", "location",
                ]):
                    continue

                q = UnknownQuestion(
                    label=label_text,
                    field_type="text",
                    context=label_text,
                )
                answer = resolver(label_text, self.ats_type, [], label_text)
                if answer:
                    await inp.fill(answer)
                    result.filled_fields.append(f"custom:{label_text}")
                result.unknown_questions.append(q)
            except Exception:
                continue

    async def _get_label_for_element(self, page: Page, element) -> str:
        """Find the label text for a given input element."""
        try:
            # Try aria-label first
            aria = await element.get_attribute("aria-label", timeout=500)
            if aria:
                return aria.strip()
            # Try placeholder
            placeholder = await element.get_attribute("placeholder", timeout=500)
            if placeholder:
                return placeholder.strip()
            # Try id -> label[for]
            el_id = await element.get_attribute("id", timeout=500)
            if el_id:
                label = page.locator(f"label[for='{el_id}']").first
                if await label.count() > 0:
                    return (await label.inner_text(timeout=500)).strip()
        except Exception:
            pass
        return ""

    async def _click_next_or_continue(self, page: Page) -> bool:
        selectors = [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button[type='submit']:not(:has-text('Submit'))",
            "button:has-text('Next step')",
            "button:has-text('Save & Continue')",
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

    async def _is_review_page(self, page: Page) -> bool:
        review_texts = ["review", "confirm", "almost done", "check your application"]
        try:
            content = (await page.content()).lower()
            return any(t in content for t in review_texts)
        except Exception:
            return False
