"""
Google OAuth2 authentication for Gmail API.
Handles first-run browser flow, token persistence, and automatic refresh.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.utils.logging import get_logger

log = get_logger(__name__)

# Read-only Gmail scope — we only need to read emails, never send or modify
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def authenticate(
    credentials_path: Path,
    token_path: Path,
) -> Credentials:
    """
    Return valid Gmail API credentials.

    Flow:
    1. If a saved token exists and is valid (or can be refreshed), use it.
    2. Otherwise, run the browser-based OAuth2 flow and persist the token.

    credentials_path: Path to the credentials.json downloaded from Google Cloud Console.
    token_path:       Where to save/load the OAuth token.
    """
    creds: Optional[Credentials] = None

    # ── Load existing token ───────────────────────────────────────────────────
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            log.debug("token_loaded", path=str(token_path))
        except Exception as e:
            log.warning("token_load_failed", error=str(e))
            creds = None

    # ── Refresh or re-authorise ───────────────────────────────────────────────
    if creds and creds.valid:
        log.debug("credentials_valid")
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            log.info("token_refreshed")
            _save_token(creds, token_path)
            return creds
        except Exception as e:
            log.warning("token_refresh_failed", error=str(e))
            creds = None

    # ── Full OAuth2 browser flow ──────────────────────────────────────────────
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Google OAuth credentials not found: {credentials_path}\n\n"
            "Setup steps:\n"
            "  1. Go to https://console.cloud.google.com/\n"
            "  2. Create a project → Enable Gmail API\n"
            "  3. OAuth consent screen → Desktop app\n"
            "  4. Credentials → Download JSON → save as credentials.json\n"
            f"  5. Place at: {credentials_path}\n"
            "  6. Run: jobly auth"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    log.info("starting_oauth_flow", msg="A browser window will open for Google authorisation")
    creds = flow.run_local_server(port=0, open_browser=True)

    _save_token(creds, token_path)
    log.info("auth_complete", token_path=str(token_path))
    return creds


def _save_token(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    # Restrict permissions — token grants read access to Gmail
    token_path.chmod(0o600)
    log.debug("token_saved", path=str(token_path))


def check_credentials_file(credentials_path: Path) -> bool:
    """Return True if credentials.json exists and looks valid."""
    if not credentials_path.exists():
        return False
    try:
        data = json.loads(credentials_path.read_text())
        return "installed" in data or "web" in data
    except Exception:
        return False
