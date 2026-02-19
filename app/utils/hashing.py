"""
URL canonicalisation and SHA-256 hashing for deduplication.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


# UTM and tracking params to strip before hashing
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "referrer", "source", "gh_src", "lever-origin", "lever-source",
    "ashby_source", "ems", "sid", "cid", "gclid", "fbclid", "msclkid",
})

# SWEList / Simplify redirect URL patterns
_REDIRECT_PARAMS = ("url", "link", "target", "redirect", "dest", "destination")


def extract_redirect_url(url: str) -> str:
    """
    If a URL is a tracking redirect (e.g. swelist.com/click?url=...),
    extract and return the actual destination URL.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=False)
    for param in _REDIRECT_PARAMS:
        if param in qs:
            candidate = qs[param][0]
            # Must look like a real URL
            if candidate.startswith("http"):
                return candidate
    return url


def canonicalise_url(url: str) -> str:
    """
    Return a stable canonical form of a job URL:
    - Follow one level of redirect param extraction
    - Strip tracking query params
    - Lowercase scheme + host
    - Remove trailing slash from path
    - Remove fragment
    """
    url = extract_redirect_url(url.strip())
    parsed = urlparse(url)

    # Strip tracking params from query string
    qs = parse_qs(parsed.query, keep_blank_values=False)
    clean_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
    clean_query = urlencode(clean_qs, doseq=True)

    # Normalise path: remove trailing slash unless it's the root
    path = parsed.path.rstrip("/") or "/"

    canonical = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        "",           # params
        clean_query,
        "",           # fragment
    ))
    return canonical


def url_hash(url: str) -> str:
    """SHA-256 of the canonical URL, hex-encoded."""
    canon = canonicalise_url(url)
    return hashlib.sha256(canon.encode()).hexdigest()


def detect_ats_from_url(url: str) -> str:
    """
    Best-effort ATS type detection from the URL alone.
    Returns one of: ashby, greenhouse, lever, workday, unknown.
    """
    url_lower = url.lower()
    if "ashbyhq.com" in url_lower or "jobs.ashby" in url_lower:
        return "ashby"
    if "greenhouse.io" in url_lower or "grnh.se" in url_lower or "gh_jid=" in url_lower:
        return "greenhouse"
    if "lever.co" in url_lower:
        return "lever"
    if "myworkdayjobs.com" in url_lower or "workday.com" in url_lower:
        return "workday"
    if "smartrecruiters.com" in url_lower:
        return "smartrecruiters"
    if "icims.com" in url_lower or "icims=1" in url_lower:
        return "icims"
    if "taleo.net" in url_lower:
        return "taleo"
    if "workable.com" in url_lower:
        return "workable"
    if "breezy.hr" in url_lower:
        return "breezy"
    if "simplify.jobs" in url_lower:
        return "simplify"
    return "unknown"
