"""
LLM evaluation step using Claude.

Provides a structured recommendation (RECOMMEND_SUBMIT or RECOMMEND_SKIP)
BEFORE the final human confirmation gate. This is advisory only — the user
must still type YES to submit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class EvaluationResult:
    recommendation: str  # "RECOMMEND_SUBMIT" | "RECOMMEND_SKIP" | "NA"
    rationale: str
    red_flags: list[str]
    confidence: str  # "high" | "medium" | "low"


_SYSTEM_PROMPT = """\
You are an internship application advisor. Your job is to evaluate whether a
candidate's profile is a strong enough match for a specific job posting to be
worth submitting an application.

Be honest and direct. Flag genuine mismatches. Do NOT encourage submitting
applications where the fit is clearly poor (wrong seniority level, required
skills missing, etc.). Being selective saves the candidate's reputation.

Output JSON only — no prose outside the JSON object.
"""

_USER_TEMPLATE = """\
Evaluate this application and return JSON with this exact schema:
{{
  "recommendation": "RECOMMEND_SUBMIT" | "RECOMMEND_SKIP",
  "rationale": "1–3 sentence explanation",
  "red_flags": ["list", "of", "concerns"],
  "confidence": "high" | "medium" | "low"
}}

--- JOB ---
Company: {company}
Title: {title}
Location: {location}
ATS: {ats_type}
Fit score (rules-based): {fit_score:.2f}
Fit reason: {fit_reason}

--- CANDIDATE PROFILE ---
Name: {name}
Education: {degree} in {field} at {institution} (grad {grad_date})
GPA: {gpa}/{gpa_scale}
US Work Authorized: {authorized}
Requires Sponsorship: {sponsorship}

--- FIELDS SUBMITTED ---
{submitted_fields}

--- USER-PROVIDED ANSWERS (custom questions) ---
{custom_answers}
"""


async def evaluate_application(
    company: str,
    title: str,
    location: Optional[str],
    ats_type: Optional[str],
    fit_score: float,
    fit_reason: str,
    profile: dict,
    submitted_fields: dict,
    custom_answers: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> EvaluationResult:
    """
    Call Claude to evaluate the application and return a recommendation.
    Falls back to NA if the API call fails.
    """
    if not api_key:
        log.warning("llm_api_key_missing")
        return EvaluationResult(
            recommendation="NA",
            rationale="LLM evaluation skipped: ANTHROPIC_API_KEY not set.",
            red_flags=[],
            confidence="low",
        )

    personal = profile.get("personal", {})
    education = profile.get("education", [{}])[0]
    work_auth = profile.get("work_authorization", {})

    submitted_str = "\n".join(f"  {k}: {v}" for k, v in submitted_fields.items()) or "  (none)"
    custom_str = "\n".join(f"  Q: {k}\n  A: {v}" for k, v in custom_answers.items()) or "  (none)"

    prompt = _USER_TEMPLATE.format(
        company=company,
        title=title,
        location=location or "Not specified",
        ats_type=ats_type or "unknown",
        fit_score=fit_score,
        fit_reason=fit_reason,
        name=f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip(),
        degree=education.get("degree", ""),
        field=education.get("field_of_study", ""),
        institution=education.get("institution", ""),
        grad_date=education.get("end_date", ""),
        gpa=education.get("gpa", "N/A"),
        gpa_scale=education.get("gpa_scale", "4.0"),
        authorized=work_auth.get("authorized_us", True),
        sponsorship=work_auth.get("requires_sponsorship", False),
        submitted_fields=submitted_str,
        custom_answers=custom_str,
    )

    try:
        import anthropic  # lazy import to avoid hard dep if LLM disabled

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = raw[: raw.rfind("```")]

        data = json.loads(raw)
        result = EvaluationResult(
            recommendation=data.get("recommendation", "NA"),
            rationale=data.get("rationale", ""),
            red_flags=data.get("red_flags", []),
            confidence=data.get("confidence", "low"),
        )
        log.info(
            "llm_evaluation_complete",
            recommendation=result.recommendation,
            confidence=result.confidence,
        )
        return result

    except Exception as e:
        log.warning("llm_evaluation_failed", error=str(e))
        return EvaluationResult(
            recommendation="NA",
            rationale=f"LLM evaluation failed: {e}",
            red_flags=[],
            confidence="low",
        )
