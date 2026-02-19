"""
SWEList HTML email parser.

Strategy (multiple passes, most specific first):
  1. Look for structured job cards with data attributes or known class patterns.
  2. Find all <a> tags pointing to known ATS domains.
  3. Fallback: any <a> with href containing job-related path fragments.

For each candidate link, we try to find the associated company name and
job title from surrounding DOM elements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from app.utils.hashing import canonicalise_url, detect_ats_from_url, url_hash
from app.utils.logging import get_logger

log = get_logger(__name__)

# ─── ATS URL patterns ─────────────────────────────────────────────────────────

_ATS_DOMAINS = re.compile(
    r"(ashbyhq\.com|greenhouse\.io|lever\.co|myworkdayjobs\.com|"
    r"smartrecruiters\.com|icims\.com|taleo\.net|grnh\.se|"
    r"breezy\.hr|jobvite\.com|bamboohr\.com|workable\.com|simplify\.jobs)",
    re.IGNORECASE,
)

_JOB_PATH_FRAGMENTS = re.compile(
    r"/(job|jobs|career|careers|apply|application|position|opening)/",
    re.IGNORECASE,
)

# Text fragments to skip (unsubscribe links, image links, etc.)
_SKIP_TEXTS = frozenset({
    "unsubscribe", "view in browser", "privacy policy", "terms", "help",
    "manage preferences", "opt out", "click here", "", "apply",
})


@dataclass
class ParsedJob:
    company: str
    title: str
    url: str
    url_hash: str
    ats_type: str
    location: Optional[str] = None
    source_email_id: Optional[str] = None
    discovered_at: datetime = field(default_factory=datetime.utcnow)


def parse_email_html(
    html: str,
    source_email_id: Optional[str] = None,
) -> list[ParsedJob]:
    """
    Parse SWEList digest email HTML and return a deduplicated list of ParsedJob.
    """
    soup = BeautifulSoup(html, "lxml")
    _remove_boilerplate(soup)

    jobs: list[ParsedJob] = []
    seen_hashes: set[str] = set()

    candidates = _extract_candidates(soup)
    log.debug("parser_candidates", count=len(candidates))

    for company, title, url, location in candidates:
        if not url or not title:
            continue

        canon = canonicalise_url(url)
        h = url_hash(canon)
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        ats = detect_ats_from_url(canon)
        jobs.append(ParsedJob(
            company=company,
            title=title,
            url=canon,
            url_hash=h,
            ats_type=ats,
            location=location,
            source_email_id=source_email_id,
        ))

    log.info("parser_jobs_found", count=len(jobs), email_id=source_email_id)
    return jobs


# ─── Private helpers ──────────────────────────────────────────────────────────


def _remove_boilerplate(soup: BeautifulSoup) -> None:
    """Remove common email boilerplate sections."""
    for tag in soup.find_all(["style", "script", "meta", "head"]):
        tag.decompose()
    # Remove footer-ish divs (common class names)
    footer_patterns = re.compile(r"footer|unsubscribe|legal|disclaimer", re.I)
    for tag in soup.find_all(class_=footer_patterns):
        tag.decompose()
    for tag in soup.find_all(id=footer_patterns):
        tag.decompose()


def _extract_candidates(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, Optional[str]]]:
    """
    Return list of (company, title, url, location) tuples.
    Multiple strategies tried in order.
    """
    candidates: list[tuple[str, str, str, Optional[str]]] = []

    # Strategy 0: SWEList <p class="internship"> format
    candidates.extend(_strategy_swelist_paragraphs(soup))

    # Strategy 1: structured job rows/cards
    if not candidates:
        candidates.extend(_strategy_structured_cards(soup))

    # Strategy 2: ATS links with context inference
    if not candidates:
        candidates.extend(_strategy_ats_links(soup))

    # Strategy 3: any job-path link as fallback
    if not candidates:
        candidates.extend(_strategy_job_path_links(soup))

    return candidates


def _strategy_swelist_paragraphs(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, Optional[str]]]:
    """
    Handle SWEList's native email format:
      <p class="internship">
        <strong>Company Name:</strong>
        <a href="https://simplify.jobs/p/...">Job Title</a>
      </p>
    """
    results = []
    for p in soup.find_all("p", class_="internship"):
        link = p.find("a", href=True)
        if not link:
            continue
        title = _clean_text(link.get_text())
        if not title or title.lower() in _SKIP_TEXTS:
            continue
        strong = p.find("strong")
        company = _clean_text(strong.get_text()).rstrip(":") if strong else "Unknown Company"
        location = _infer_location(p)
        results.append((company, title, link["href"], location))
    return results


def _strategy_structured_cards(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, Optional[str]]]:
    """
    Look for rows or divs that contain both a company name and a titled link.
    SWEList emails tend to use table rows with 2–4 cells per job.
    """
    results = []

    # Try table rows first
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        links = row.find_all("a", href=True)
        if not links:
            continue

        job_link = None
        for link in links:
            href = link.get("href", "")
            if _is_job_url(href):
                job_link = link
                break

        if not job_link:
            continue

        title = _clean_text(job_link.get_text())
        if not title or title.lower() in _SKIP_TEXTS:
            continue

        # Company: look in cells before the link cell
        company = _find_company_in_row(row, job_link)
        location = _find_location_in_row(row, job_link)
        url = job_link["href"]

        if title and url:
            results.append((company, title, url, location))

    # Try div-based cards
    if not results:
        for card in soup.find_all(class_=re.compile(r"job|card|listing|position|role", re.I)):
            links = card.find_all("a", href=True)
            for link in links:
                href = link.get("href", "")
                if not _is_job_url(href):
                    continue
                title = _clean_text(link.get_text())
                if not title or title.lower() in _SKIP_TEXTS:
                    continue
                company = _infer_company(card, link)
                location = _infer_location(card)
                results.append((company, title, href, location))

    return results


def _strategy_ats_links(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, Optional[str]]]:
    """Find all links pointing to known ATS domains and infer context."""
    results = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if not _ATS_DOMAINS.search(href):
            continue
        title = _clean_text(link.get_text())
        if not title or title.lower() in _SKIP_TEXTS:
            # Use URL-derived title as fallback
            title = _title_from_url(href)
        if not title:
            continue
        parent = link.parent or link
        company = _infer_company(parent, link)
        location = _infer_location(parent)
        results.append((company, title, href, location))
    return results


def _strategy_job_path_links(
    soup: BeautifulSoup,
) -> list[tuple[str, str, str, Optional[str]]]:
    """Fallback: any anchor whose href contains /job/ /jobs/ /careers/ etc."""
    results = []
    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        if not _JOB_PATH_FRAGMENTS.search(href):
            continue
        title = _clean_text(link.get_text())
        if not title or title.lower() in _SKIP_TEXTS:
            continue
        parent = link.parent or link
        company = _infer_company(parent, link)
        location = _infer_location(parent)
        results.append((company, title, href, location))
    return results


# ─── Context inference helpers ────────────────────────────────────────────────


def _find_company_in_row(row: Tag, job_link: Tag) -> str:
    """Look for company name in table cells before the link."""
    cells = row.find_all("td")
    for cell in cells:
        if job_link in cell.descendants:
            continue
        text = _clean_text(cell.get_text())
        if text and len(text) < 60 and text.lower() not in _SKIP_TEXTS:
            return text
    return "Unknown Company"


def _find_location_in_row(row: Tag, job_link: Tag) -> Optional[str]:
    """Look for location hints in the row."""
    text = _clean_text(row.get_text())
    return _extract_location_from_text(text)


def _infer_company(container: Tag, link: Tag) -> str:
    """Try several heuristics to find the company name near a link."""
    # 1. sibling or parent text nodes
    parent = link.parent
    if parent:
        prev_sib = link.find_previous_sibling()
        if prev_sib:
            text = _clean_text(prev_sib.get_text())
            if text and len(text) < 80 and text.lower() not in _SKIP_TEXTS:
                return text

    # 2. strong/b/span tags in container
    for tag in container.find_all(["strong", "b", "span", "p"]):
        if tag is link or link in tag.parents:
            continue
        text = _clean_text(tag.get_text())
        if text and 2 < len(text) < 60 and text.lower() not in _SKIP_TEXTS:
            return text

    # 3. Alt text of images (company logos)
    for img in container.find_all("img", alt=True):
        alt = _clean_text(img.get("alt", ""))
        if alt and len(alt) < 60:
            return alt

    return "Unknown Company"


def _infer_location(container: Tag) -> Optional[str]:
    text = _clean_text(container.get_text(" ", strip=True))
    return _extract_location_from_text(text)


def _extract_location_from_text(text: str) -> Optional[str]:
    """Heuristic extraction of location from free text."""
    patterns = [
        re.compile(r"\b(remote|hybrid|on-site|onsite)\b", re.I),
        re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?,\s*[A-Z]{2})\b"),  # City, ST
        re.compile(r"\b(New York|San Francisco|Seattle|Austin|Boston|Chicago)\b", re.I),
    ]
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(0).strip()
    return None


def _title_from_url(url: str) -> str:
    """Derive a rough title from the URL path."""
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p and not p.isdigit() and len(p) > 3]
    if parts:
        return parts[-1].replace("-", " ").replace("_", " ").title()
    return ""


def _is_job_url(url: str) -> bool:
    return bool(_ATS_DOMAINS.search(url) or _JOB_PATH_FRAGMENTS.search(url))


def _clean_text(text: str) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", text or "").strip()
