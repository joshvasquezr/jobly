"""
Configuration loading: .env → config.yaml → profile.json.
All paths are resolved and expanded. Resume files are validated.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load .env from CWD or home, but never fail if missing
load_dotenv(override=False)

# ─── Default paths ────────────────────────────────────────────────────────────

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "jobly"
_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "jobly"


def _expand(p: str | Path | None) -> Optional[Path]:
    if not p:
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


# ─── Sub-config dataclasses ───────────────────────────────────────────────────


@dataclass
class GmailConfig:
    sender_filter: str = "noreply@swelist.com"
    subject_filter: str = ""
    lookback_days: int = 2
    max_results: int = 10


@dataclass
class FilterConfig:
    min_score: float = 0.30
    title_keywords: list[str] = field(default_factory=lambda: [
        "intern", "internship", "swe", "software engineer",
        "backend", "platform", "infra", "infrastructure",
        "data", "distributed", "database", "systems",
    ])
    preferred_ats: list[str] = field(default_factory=lambda: ["ashby", "greenhouse", "lever"])
    preferred_locations: list[str] = field(default_factory=list)
    excluded_locations: list[str] = field(default_factory=list)


@dataclass
class BrowserConfig:
    headless: bool = False
    slow_mo_ms: int = 100
    timeout_ms: int = 30000
    min_wait_ms: int = 300
    max_wait_ms: int = 1200


@dataclass
class LLMConfig:
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024


@dataclass
class AppConfig:
    gmail: GmailConfig = field(default_factory=GmailConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    # Resolved paths
    config_dir: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR)
    data_dir: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR)
    credentials_path: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR / "credentials.json")
    token_path: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR / "token.json")
    profile_path: Path = field(default_factory=lambda: _DEFAULT_CONFIG_DIR / "profile.json")
    db_path: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR / "jobly.db")
    artifacts_dir: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR / "artifacts")
    log_dir: Path = field(default_factory=lambda: _DEFAULT_DATA_DIR / "logs")
    resume_default_path: Optional[Path] = None
    resume_variants: dict[str, Path] = field(default_factory=dict)

    # Anthropic key — never logged
    anthropic_api_key: str = field(default="", repr=False)

    def ensure_dirs(self) -> None:
        for d in [self.data_dir, self.artifacts_dir, self.log_dir, self.config_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def validate_resume(self) -> None:
        if not self.resume_default_path:
            raise ValueError(
                "resume_default_path is not set. "
                "Add it to config.yaml or set RESUME_DEFAULT_PATH in .env"
            )
        if not self.resume_default_path.exists():
            raise FileNotFoundError(
                f"Resume file not found: {self.resume_default_path}\n"
                "Update resume_default_path in config.yaml."
            )

    def get_resume(self, variant: Optional[str] = None) -> Path:
        if variant and variant in self.resume_variants:
            p = self.resume_variants[variant]
            if not p.exists():
                raise FileNotFoundError(f"Resume variant '{variant}' not found: {p}")
            return p
        self.validate_resume()
        return self.resume_default_path  # type: ignore[return-value]


# ─── Loader ───────────────────────────────────────────────────────────────────


def _merge_dict(base: dict, override: dict) -> dict:
    """Deep-merge override into base."""
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge_dict(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """
    Load configuration in precedence order:
    1. Hardcoded defaults
    2. config.yaml
    3. Environment variables
    """
    cfg = AppConfig()

    # ── Determine config file path ────────────────────────────────────────────
    if config_path is None:
        env_path = os.getenv("JOBLY_CONFIG_PATH")
        config_path = _expand(env_path) if env_path else cfg.config_dir / "config.yaml"

    raw: dict = {}
    if config_path and config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    # ── Gmail ─────────────────────────────────────────────────────────────────
    gm = raw.get("gmail", {})
    cfg.gmail = GmailConfig(
        sender_filter=gm.get("sender_filter", cfg.gmail.sender_filter),
        subject_filter=gm.get("subject_filter", cfg.gmail.subject_filter),
        lookback_days=int(gm.get("lookback_days", cfg.gmail.lookback_days)),
        max_results=int(gm.get("max_results", cfg.gmail.max_results)),
    )

    # ── Filter ────────────────────────────────────────────────────────────────
    fl = raw.get("filter", {})
    cfg.filter = FilterConfig(
        min_score=float(fl.get("min_score", cfg.filter.min_score)),
        title_keywords=fl.get("title_keywords", cfg.filter.title_keywords),
        preferred_ats=fl.get("preferred_ats", cfg.filter.preferred_ats),
        preferred_locations=fl.get("preferred_locations", cfg.filter.preferred_locations),
        excluded_locations=fl.get("excluded_locations", cfg.filter.excluded_locations),
    )

    # ── Browser ───────────────────────────────────────────────────────────────
    br = raw.get("browser", {})
    cfg.browser = BrowserConfig(
        headless=bool(br.get("headless", cfg.browser.headless)),
        slow_mo_ms=int(br.get("slow_mo_ms", cfg.browser.slow_mo_ms)),
        timeout_ms=int(br.get("timeout_ms", cfg.browser.timeout_ms)),
        min_wait_ms=int(br.get("min_wait_ms", cfg.browser.min_wait_ms)),
        max_wait_ms=int(br.get("max_wait_ms", cfg.browser.max_wait_ms)),
    )

    # ── LLM ───────────────────────────────────────────────────────────────────
    ll = raw.get("llm", {})
    cfg.llm = LLMConfig(
        enabled=bool(ll.get("enabled", cfg.llm.enabled)),
        model=ll.get("model", cfg.llm.model),
        max_tokens=int(ll.get("max_tokens", cfg.llm.max_tokens)),
    )

    # ── Paths — env vars override yaml ────────────────────────────────────────
    def _path_from(env_key: str, yaml_key: str, default: Path) -> Path:
        v = os.getenv(env_key) or raw.get(yaml_key)
        return _expand(v) if v else default

    cfg.credentials_path = _path_from(
        "GOOGLE_CREDENTIALS_PATH", "credentials_path", cfg.credentials_path
    )
    cfg.token_path = _path_from("GOOGLE_TOKEN_PATH", "token_path", cfg.token_path)
    cfg.profile_path = _path_from("JOBLY_PROFILE_PATH", "profile_path", cfg.profile_path)
    cfg.db_path = _path_from("JOBLY_DB_PATH", "db_path", cfg.db_path)
    cfg.artifacts_dir = _path_from("JOBLY_ARTIFACTS_DIR", "artifacts_dir", cfg.artifacts_dir)
    cfg.log_dir = _path_from("JOBLY_LOG_DIR", "log_dir", cfg.log_dir)

    # Resume paths
    resume_raw = os.getenv("RESUME_DEFAULT_PATH") or raw.get("resume_default_path")
    if resume_raw:
        cfg.resume_default_path = _expand(resume_raw)

    variants_raw = raw.get("resume_variants", {})
    cfg.resume_variants = {k: _expand(v) for k, v in variants_raw.items() if v}  # type: ignore[misc]

    # ── Secrets — never stored in plain config ────────────────────────────────
    cfg.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")

    return cfg


def load_profile(profile_path: Path) -> dict:
    """Load and return the user profile JSON. Raises if not found."""
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profile not found: {profile_path}\n"
            f"Copy profile_template.json to {profile_path} and fill in your details."
        )
    with open(profile_path) as f:
        profile = json.load(f)
    # Strip internal comment key
    profile.pop("_comment", None)
    return profile
