"""
Rules-based fit scoring for job postings.
All weights are configurable via FilterConfig.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.utils.config import FilterConfig


@dataclass
class ScoringResult:
    score: float
    reason: str
    should_queue: bool


# ATS score boosts
_ATS_BOOST: dict[str, float] = {
    "ashby": 0.20,
    "greenhouse": 0.10,
    "lever": 0.10,
}


def score_job(
    title: str,
    company: str,
    location: str | None,
    ats_type: str | None,
    cfg: FilterConfig,
) -> ScoringResult:
    """
    Compute a fit score in [0, 1] for a job posting.
    Returns a ScoringResult with score, human-readable reasons, and queue decision.
    """
    reasons: list[str] = []
    score = 0.0

    title_lower = title.lower()
    location_lower = (location or "").lower()
    ats = (ats_type or "unknown").lower()

    # ── Title keyword matching ────────────────────────────────────────────────
    matched_keywords: list[str] = []
    for kw in cfg.title_keywords:
        if kw.lower() in title_lower:
            matched_keywords.append(kw)

    if matched_keywords:
        # Scale keyword contribution — first match worth 0.35, each extra 0.05
        keyword_score = min(0.50, 0.35 + 0.05 * (len(matched_keywords) - 1))
        score += keyword_score
        reasons.append(f"title matches: {', '.join(matched_keywords[:3])}")
    else:
        reasons.append("no title keyword match")

    # ── ATS preference ────────────────────────────────────────────────────────
    if ats in _ATS_BOOST:
        boost = _ATS_BOOST[ats]
        score += boost
        reasons.append(f"preferred ATS ({ats} +{boost:.0%})")

    # ── Location scoring ──────────────────────────────────────────────────────
    if cfg.excluded_locations:
        for excl in cfg.excluded_locations:
            if excl.lower() in location_lower:
                score = 0.0
                reasons.append(f"excluded location: {excl}")
                return ScoringResult(
                    score=0.0,
                    reason="; ".join(reasons),
                    should_queue=False,
                )

    if cfg.preferred_locations:
        for pref in cfg.preferred_locations:
            if pref.lower() in location_lower or "remote" in location_lower:
                score += 0.05
                reasons.append(f"preferred location ({pref})")
                break
    else:
        # No location filter set — neutral bonus for remote
        if "remote" in location_lower:
            score += 0.05
            reasons.append("remote")

    # ── Cap and threshold ─────────────────────────────────────────────────────
    score = min(1.0, round(score, 3))
    should_queue = score >= cfg.min_score

    return ScoringResult(
        score=score,
        reason="; ".join(reasons),
        should_queue=should_queue,
    )
