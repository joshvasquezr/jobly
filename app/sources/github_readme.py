"""
GitHub README job source — scrapes SimplifyJobs/Summer2026-Internships.

The README is an HTML document containing a <table> with columns:
  Company | Role | Location | Application | Age

The Application cell contains:
  - <a href="ATS_URL"><img alt="Apply"></a>  — direct ATS link  (what we want)
  - <a href="simplify.jobs/..."><img alt="Simplify"></a>  — Simplify link (skip)
  - 🔒  — closed role (skip)

Exports:
    fetch_github_jobs(filter_cfg) -> list[ParsedJob]
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from app.gmail.parser import ParsedJob
from app.utils.hashing import canonicalise_url, detect_ats_from_url, url_hash
from app.utils.logging import get_logger

log = get_logger(__name__)

_README_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
)

# ATS types that require account creation — skip them
_SKIPPED_ATS = {"workday", "taleo"}

# Unicode emoji ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def _get_apply_url(application_td) -> Optional[str]:
    """
    Return the direct ATS URL from the Apply button in the Application cell.
    The Apply button is <a href="ATS_URL"><img alt="Apply"></a>.
    Returns None if the cell is closed (🔒) or only has a Simplify link.
    """
    # Closed role
    if "🔒" in application_td.get_text():
        return None
    # Find <a> tag whose child <img> has alt="Apply"
    for a in application_td.find_all("a", href=True):
        img = a.find("img", alt=re.compile(r"^Apply$", re.I))
        if img:
            return a["href"]
    return None


def fetch_github_jobs(filter_cfg) -> list[ParsedJob]:
    """
    Fetch the SimplifyJobs Summer 2026 Internships README and return
    a deduplicated list of ParsedJob filtered by title keywords.

    Skips:
    - Closed roles (🔒)
    - Rows with no direct Apply link (Simplify-only)
    - Workday and Taleo ATS
    - Roles whose title doesn't match any filter_cfg.title_keywords
    """
    log.info("github_fetch_start", url=_README_URL)
    try:
        resp = httpx.get(_README_URL, timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise RuntimeError(f"Failed to fetch GitHub README: {e}") from e

    soup = BeautifulSoup(resp.text, "lxml")
    jobs: list[ParsedJob] = []
    seen_hashes: set[str] = set()
    keywords_lower = [k.lower() for k in filter_cfg.title_keywords]

    # Find the first table with the expected header columns
    table = None
    for t in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in t.find_all("th")]
        if "Company" in headers and "Role" in headers and "Application" in headers:
            table = t
            break

    if not table:
        log.warning("github_no_table_found")
        return []

    current_company: Optional[str] = None

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue  # skip header or malformed rows

        company_td = cells[0]
        role_td = cells[1]
        location_td = cells[2]
        application_td = cells[3]

        # Track current company; continuation rows have just "↳" in company cell
        company_text = company_td.get_text(strip=True)
        if "↳" not in company_text:
            a = company_td.find("a")
            current_company = _strip_emoji(a.get_text(strip=True) if a else company_text)

        if not current_company:
            continue

        # Get direct Apply URL — skip if none
        url = _get_apply_url(application_td)
        if not url:
            continue

        # Clean role title
        role = _strip_emoji(role_td.get_text(strip=True))
        if not role:
            continue

        # Filter by title keywords
        if not any(kw in role.lower() for kw in keywords_lower):
            log.debug("github_skip_role", role=role, company=current_company)
            continue

        # Detect ATS and skip unsupported ones
        ats = detect_ats_from_url(url)
        if ats in _SKIPPED_ATS:
            log.debug("github_skip_ats", company=current_company, ats=ats)
            continue

        # Canonicalise and deduplicate
        canon = canonicalise_url(url)
        h = url_hash(canon)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        location = _strip_emoji(location_td.get_text(strip=True)) or None

        jobs.append(ParsedJob(
            company=current_company,
            title=role,
            url=canon,
            url_hash=h,
            ats_type=ats,
            location=location,
            source_email_id=None,
            discovered_at=datetime.utcnow(),
        ))

    log.info("github_fetch_done", count=len(jobs))
    return jobs
