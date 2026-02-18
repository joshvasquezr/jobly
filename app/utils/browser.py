"""
Playwright browser session management.
Provides an async context manager for a single Chromium browser instance,
plus shared helpers (random waits, screenshots, HTML capture, safe clicks).
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.utils.config import BrowserConfig
from app.utils.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def browser_session(
    cfg: BrowserConfig,
    artifacts_dir: Path,
) -> AsyncGenerator[tuple[BrowserContext, Page], None]:
    """
    Yield (context, page) for a single Chromium browser session.
    Browser is closed on exit, even on exception.
    """
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=cfg.headless,
            slow_mo=cfg.slow_mo_ms,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        context.set_default_timeout(cfg.timeout_ms)
        page: Page = await context.new_page()
        try:
            yield context, page
        finally:
            await browser.close()


async def random_wait(cfg: BrowserConfig) -> None:
    """Sleep for a random duration within the configured range."""
    ms = random.randint(cfg.min_wait_ms, cfg.max_wait_ms)
    await asyncio.sleep(ms / 1000)


async def save_screenshot(
    page: Page,
    artifacts_dir: Path,
    label: str,
) -> Path:
    """Take a screenshot and return the saved path."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = artifacts_dir / f"{label}_{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.debug("screenshot_saved", path=str(path))
    except Exception as e:
        log.warning("screenshot_failed", error=str(e))
    return path


async def save_html(
    page: Page,
    artifacts_dir: Path,
    label: str,
) -> Path:
    """Save the current page HTML and return the saved path."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = artifacts_dir / f"{label}_{ts}.html"
    try:
        content = await page.content()
        path.write_text(content, encoding="utf-8")
        log.debug("html_snapshot_saved", path=str(path))
    except Exception as e:
        log.warning("html_snapshot_failed", error=str(e))
    return path


async def safe_fill(page: Page, selector: str, value: str, timeout: int = 5000) -> bool:
    """
    Fill a field by selector. Returns True on success, False if not found.
    Does NOT raise — adapters should decide what to do on False.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.clear()
        await locator.fill(value)
        return True
    except Exception:
        return False


async def safe_click(page: Page, selector: str, timeout: int = 5000) -> bool:
    """Click an element. Returns True on success, False if not found."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click()
        return True
    except Exception:
        return False


async def safe_select(page: Page, selector: str, value: str, timeout: int = 5000) -> bool:
    """Select a <select> option by value or label. Returns True on success."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        # Try by value first, then by label
        try:
            await locator.select_option(value=value)
            return True
        except Exception:
            await locator.select_option(label=value)
            return True
    except Exception:
        return False


async def wait_for_navigation(page: Page, timeout: int = 15000) -> None:
    """Wait for the page to reach a stable network-idle state."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        # Fallback — just wait for domcontentloaded
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
