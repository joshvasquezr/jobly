"""
Greenhouse ATS adapter.

Greenhouse URLs:
  - https://boards.greenhouse.io/{company}/jobs/{id}
  - https://job-boards.greenhouse.io/{company}/jobs/{id}
  - https://grnh.se/{token}  (short links — follow redirect)

Greenhouse uses standard HTML forms (not SPA), which makes it
more reliable to automate than React-based ATS systems.
The form submits to a POST endpoint; we automate the UI only.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page

from app.adapters.base import BaseAdapter, FillResult, QuestionResolver, UnknownQuestion
from app.utils.logging import get_logger

log = get_logger(__name__)

_GH_DOMAINS = re.compile(r"greenhouse\.io|grnh\.se", re.I)


class GreenhouseAdapter(BaseAdapter):
    ats_type = "greenhouse"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_GH_DOMAINS.search(url))

    async def open_and_prepare(self, page: Page, url: str) -> None:
        log.info("greenhouse_open", url=url)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await self._dismiss_cookie_banner(page)
        # If it's a short link or redirect, wait for the actual form page
        await self._wait(page, 1000)
        # Look for the application form
        try:
            await page.wait_for_selector(
                "#application-form, form#application_form, form[action*='applications'],"
                " .application-form",
                timeout=12000,
            )
        except Exception:
            log.warning("greenhouse_form_not_found", url=url)

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
        work_auth = profile.get("work_authorization", {})
        demographics = profile.get("demographics", {})

        # ── Basic personal info ───────────────────────────────────────────────
        await self._fill(page, result, "#first_name, input[id*='first_name']",
                         personal.get("first_name", ""), "first_name")
        await self._fill(page, result, "#last_name, input[id*='last_name']",
                         personal.get("last_name", ""), "last_name")
        await self._fill(page, result, "#email, input[id*='email'], input[type='email']",
                         personal.get("email", ""), "email")
        await self._fill(page, result, "#phone, input[id*='phone'], input[type='tel']",
                         personal.get("phone", ""), "phone")

        # ── Location ──────────────────────────────────────────────────────────
        loc = personal.get("location", {})
        city_state = f"{loc.get('city', '')}, {loc.get('state', '')}"
        await self._fill(
            page, result,
            "#location, input[id*='location'], input[placeholder*='city' i],"
            " input[name*='location' i]",
            city_state, "location", optional=True,
        )

        # ── Resume upload ─────────────────────────────────────────────────────
        uploaded = await self._upload_resume(page, resume_path)
        (result.filled_fields if uploaded else result.skipped_fields).append("resume")

        # ── LinkedIn / GitHub / Website ───────────────────────────────────────
        await self._fill(
            page, result,
            "input[id*='linkedin'], input[name*='linkedin'],"
            " input[placeholder*='linkedin' i]",
            personal.get("linkedin_url", ""), "linkedin", optional=True,
        )
        await self._fill(
            page, result,
            "input[id*='github'], input[name*='github']",
            personal.get("github_url", ""), "github", optional=True,
        )
        await self._fill(
            page, result,
            "input[id*='website'], input[name*='website'], input[id*='portfolio']",
            personal.get("website_url", ""), "website", optional=True,
        )

        # ── School / Education ────────────────────────────────────────────────
        await self._fill(
            page, result,
            "input[id*='school'], input[name*='school'],"
            " input[placeholder*='school' i], input[placeholder*='university' i]",
            education.get("institution", ""), "school", optional=True,
        )

        # ── Work authorisation drop-down (common Greenhouse EEO section) ──────
        await self._handle_work_auth_selects(page, result, work_auth)

        # ── Demographics (EEO) ────────────────────────────────────────────────
        await self._handle_demographics(page, result, demographics)

        # ── Custom / unknown questions ────────────────────────────────────────
        await self._handle_custom_questions(page, result, resolver)

        return result

    async def reach_review_step(self, page: Page) -> None:
        """
        Greenhouse standard forms are single-page — just scroll to bottom
        so the user can review the filled fields before we present the submit gate.
        """
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._wait(page, 500)
        log.info("greenhouse_review_ready")

    async def submit(self, page: Page) -> None:
        """Click the Submit Application button."""
        selectors = [
            "input[type='submit']",
            "button[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "#submit_app",
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    log.info("greenhouse_submitted")
                    await self._wait(page, 2000)
                    return
            except Exception:
                continue
        raise RuntimeError("Could not find Greenhouse submit button")

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
                log.debug("gh_field_filled", field=field_name)
                return True
        except Exception:
            pass
        if not optional:
            result.skipped_fields.append(field_name)
        return False

    async def _upload_resume(self, page: Page, resume_path: Path) -> bool:
        selectors = [
            "input#resume, input[id*='resume'][type='file']",
            "input[type='file'][accept*='pdf']",
            "input[type='file']",
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.set_input_files(str(resume_path))
                    await self._wait(page, 800)
                    log.debug("gh_resume_uploaded")
                    return True
            except Exception:
                continue
        return False

    async def _handle_work_auth_selects(
        self, page: Page, result: FillResult, work_auth: dict
    ) -> None:
        authorized = work_auth.get("authorized_us", True)
        sponsorship = work_auth.get("requires_sponsorship", False)

        # Greenhouse typically has select dropdowns for these
        auth_sel = "select[id*='authorized'], select[name*='authorized'], select[id*='work_auth']"
        auth_val = "Yes" if authorized else "No"
        try:
            sel_loc = page.locator(auth_sel).first
            if await sel_loc.is_visible(timeout=1500):
                try:
                    await sel_loc.select_option(label=auth_val)
                except Exception:
                    await sel_loc.select_option(value=auth_val.lower())
                result.filled_fields.append("work_authorization")
        except Exception:
            pass

        spons_sel = "select[id*='sponsor'], select[name*='sponsor']"
        spons_val = "Yes" if sponsorship else "No"
        try:
            sel_loc = page.locator(spons_sel).first
            if await sel_loc.is_visible(timeout=1500):
                try:
                    await sel_loc.select_option(label=spons_val)
                except Exception:
                    await sel_loc.select_option(value=spons_val.lower())
                result.filled_fields.append("sponsorship")
        except Exception:
            pass

    async def _handle_demographics(
        self, page: Page, result: FillResult, demographics: dict
    ) -> None:
        dem_map = {
            "select[id*='gender'], select[name*='gender']": demographics.get("gender", "Decline to state"),
            "select[id*='race'], select[id*='ethnicity'], select[name*='race']": demographics.get("race_ethnicity", "Decline to state"),
            "select[id*='veteran'], select[name*='veteran']": demographics.get("veteran_status", "I am not a protected veteran"),
            "select[id*='disability'], select[name*='disability']": demographics.get("disability_status", "I don't wish to answer"),
        }
        for sel, val in dem_map.items():
            if not val:
                continue
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    try:
                        await loc.select_option(label=val)
                    except Exception:
                        # Try partial match
                        options = await loc.locator("option").all()
                        for opt in options:
                            opt_text = await opt.inner_text()
                            if val.lower() in opt_text.lower():
                                opt_val = await opt.get_attribute("value")
                                if opt_val:
                                    await loc.select_option(value=opt_val)
                                break
                    result.filled_fields.append(f"demographics:{sel[:20]}")
            except Exception:
                pass

    async def _handle_custom_questions(
        self,
        page: Page,
        result: FillResult,
        resolver: QuestionResolver,
    ) -> None:
        """Handle custom Greenhouse questions (text inputs and textareas)."""
        # Greenhouse puts custom questions in a section after the standard form
        try:
            custom_section = page.locator(".custom-fields, #custom_fields, [id*='custom']")
            if not await custom_section.count():
                return
        except Exception:
            pass

        try:
            # Find all visible, unfilled text inputs and textareas
            inputs = await page.locator(
                "textarea:visible, input[type='text']:visible:not([id*='first']):not([id*='last'])"
                ":not([id*='email']):not([id*='phone']):not([id*='linkedin'])"
                ":not([id*='github']):not([id*='website']):not([id*='school'])"
            ).all()
        except Exception:
            return

        for inp in inputs:
            try:
                val = await inp.input_value(timeout=500)
                if val:
                    continue
                label_text = await self._get_label_text(page, inp)
                if not label_text:
                    continue
                answer = resolver(label_text, self.ats_type, [], label_text)
                if answer:
                    await inp.fill(answer)
                    result.filled_fields.append(f"custom:{label_text[:30]}")
                result.unknown_questions.append(
                    UnknownQuestion(label=label_text, field_type="text")
                )
            except Exception:
                continue

    async def _get_label_text(self, page: Page, element) -> str:
        try:
            aria = await element.get_attribute("aria-label", timeout=500)
            if aria:
                return aria.strip()
            el_id = await element.get_attribute("id", timeout=500)
            if el_id:
                label = page.locator(f"label[for='{el_id}']").first
                if await label.count():
                    return (await label.inner_text(timeout=500)).strip()
            placeholder = await element.get_attribute("placeholder", timeout=500)
            if placeholder:
                return placeholder.strip()
        except Exception:
            pass
        return ""
