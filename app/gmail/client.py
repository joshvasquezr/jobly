"""
Gmail API client — queries SWEList digest emails and returns raw messages.
"""

from __future__ import annotations

import base64
import email
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from app.utils.config import GmailConfig
from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class RawEmail:
    gmail_id: str
    thread_id: str
    subject: str
    sender: str
    received_at: datetime
    html_body: str
    snippet: str


def build_query(cfg: GmailConfig) -> str:
    """
    Construct the Gmail search query string.
    Example: from:noreply@swelist.com newer_than:2d
    """
    parts = [f"from:{cfg.sender_filter}"]
    if cfg.subject_filter:
        parts.append(f'subject:"{cfg.subject_filter}"')
    if cfg.lookback_days > 0:
        parts.append(f"newer_than:{cfg.lookback_days}d")
    query = " ".join(parts)
    log.debug("gmail_query", query=query)
    return query


def fetch_digest_emails(
    creds: Credentials,
    cfg: GmailConfig,
    already_seen: Optional[set[str]] = None,
) -> list[RawEmail]:
    """
    Fetch SWEList digest emails from Gmail.

    Returns a list of RawEmail objects, skipping any already in `already_seen`
    (set of gmail_ids) to avoid re-processing.
    """
    already_seen = already_seen or set()

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        raise RuntimeError(f"Failed to build Gmail service: {e}") from e

    query = build_query(cfg)

    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=cfg.max_results)
            .execute()
        )
    except HttpError as e:
        raise RuntimeError(f"Gmail API error: {e}") from e

    messages = result.get("messages", [])
    if not messages:
        log.info("no_new_emails", query=query)
        return []

    log.info("emails_found", count=len(messages), query=query)

    emails: list[RawEmail] = []
    for msg_ref in messages:
        gmail_id = msg_ref["id"]
        if gmail_id in already_seen:
            log.debug("email_already_processed", gmail_id=gmail_id)
            continue

        try:
            raw = _fetch_message(service, gmail_id)
            if raw:
                emails.append(raw)
        except Exception as e:
            log.warning("email_fetch_failed", gmail_id=gmail_id, error=str(e))

    return emails


def _fetch_message(service, gmail_id: str) -> Optional[RawEmail]:
    """Fetch a single message and extract its HTML body."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=gmail_id, format="full")
        .execute()
    )

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    date_str = headers.get("date", "")
    snippet = msg.get("snippet", "")

    received_at = _parse_date(date_str)

    html_body = _extract_html(msg.get("payload", {}))
    if not html_body:
        log.warning("no_html_body", gmail_id=gmail_id, subject=subject)
        return None

    return RawEmail(
        gmail_id=gmail_id,
        thread_id=msg.get("threadId", ""),
        subject=subject,
        sender=sender,
        received_at=received_at,
        html_body=html_body,
        snippet=snippet,
    )


def _extract_html(payload: dict) -> str:
    """
    Recursively extract the HTML body from a MIME message payload.
    Handles multipart/alternative and nested multipart structures.
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    parts = payload.get("parts", [])
    # Prefer text/html over text/plain in multipart
    html_candidate = ""
    for part in parts:
        result = _extract_html(part)
        if result and part.get("mimeType") == "text/html":
            return result
        if result:
            html_candidate = result

    return html_candidate


def _parse_date(date_str: str) -> datetime:
    """Parse an RFC 2822 email date string into a UTC datetime."""
    if not date_str:
        return datetime.utcnow()
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()
