"""
SQLModel database schema for jobly.
All tables, enums, and the DB initialization helper live here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select


# ─── Status enums ─────────────────────────────────────────────────────────────


class EmailStatus(str, Enum):
    raw = "raw"
    parsed = "parsed"
    failed = "failed"


class JobStatus(str, Enum):
    discovered = "discovered"
    queued = "queued"
    filtered_out = "filtered_out"
    skipped = "skipped"


class ApplicationStatus(str, Enum):
    queued = "queued"
    started = "started"
    filled = "filled"
    needs_review = "needs_review"
    submitted = "submitted"
    skipped = "skipped"
    error = "error"


class RunStatus(str, Enum):
    running = "running"
    completed = "completed"
    interrupted = "interrupted"


class ArtifactType(str, Enum):
    screenshot = "screenshot"
    html_snapshot = "html_snapshot"


class LLMRecommendation(str, Enum):
    recommend_submit = "RECOMMEND_SUBMIT"
    recommend_skip = "RECOMMEND_SKIP"
    na = "NA"


# ─── Tables ───────────────────────────────────────────────────────────────────


class Email(SQLModel, table=True):
    """Raw email metadata — audit trail of what we ingested."""

    __tablename__ = "emails"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    gmail_id: str = Field(unique=True, index=True)
    thread_id: str = Field(default="")
    subject: str = Field(default="")
    sender: str = Field(default="")
    received_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = Field(default=None)
    raw_html: Optional[str] = Field(default=None)
    status: str = Field(default=EmailStatus.raw)


class JobPost(SQLModel, table=True):
    """Extracted and deduplicated job listing."""

    __tablename__ = "job_posts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    # SHA-256 of canonical URL — primary deduplication key
    url_hash: str = Field(unique=True, index=True)
    company: str
    title: str
    location: Optional[str] = Field(default=None)
    url: str
    ats_type: Optional[str] = Field(default=None)
    fit_score: float = Field(default=0.0)
    fit_reason: str = Field(default="")
    source_email_id: Optional[str] = Field(default=None, foreign_key="emails.id")
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default=JobStatus.discovered)


class ApplicationRun(SQLModel, table=True):
    """A single invocation of `jobly run` — groups a batch of applications."""

    __tablename__ = "application_runs"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = Field(default=None)
    status: str = Field(default=RunStatus.running)
    jobs_processed: int = Field(default=0)
    jobs_submitted: int = Field(default=0)
    jobs_skipped: int = Field(default=0)
    jobs_errored: int = Field(default=0)


class Application(SQLModel, table=True):
    """Individual application attempt for a single job posting."""

    __tablename__ = "applications"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    job_post_id: str = Field(foreign_key="job_posts.id", index=True)
    run_id: Optional[str] = Field(default=None, foreign_key="application_runs.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = Field(default=ApplicationStatus.queued)
    ats_type: Optional[str] = Field(default=None)
    # JSON-serialised snapshot of all field values submitted
    answers_used: Optional[str] = Field(default=None)
    llm_recommendation: Optional[str] = Field(default=None)
    llm_rationale: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)
    screenshot_path: Optional[str] = Field(default=None)
    html_snapshot_path: Optional[str] = Field(default=None)

    def set_answers(self, answers: dict) -> None:
        self.answers_used = json.dumps(answers)

    def get_answers(self) -> dict:
        if self.answers_used:
            return json.loads(self.answers_used)
        return {}


class Artifact(SQLModel, table=True):
    """Saved screenshots and HTML snapshots for debugging."""

    __tablename__ = "artifacts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    application_id: str = Field(foreign_key="applications.id", index=True)
    artifact_type: str  # ArtifactType values
    file_path: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class QuestionAnswer(SQLModel, table=True):
    """Cached answers to ATS-specific questions so we only ask once."""

    __tablename__ = "question_answers"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    # Normalised question label (lowercase, stripped)
    question_label: str = Field(index=True)
    ats_type: str = Field(index=True)
    answer: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ─── DB helpers ───────────────────────────────────────────────────────────────

_engine = None


def get_engine(db_path: str):
    global _engine
    if _engine is None:
        url = f"sqlite:///{db_path}"
        _engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # Enable WAL mode for safer concurrent access
        with _engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return _engine


def init_db(db_path: str) -> None:
    """Create all tables if they don't exist."""
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)


def get_session(db_path: str) -> Session:
    """
    Return a Session that supports the context-manager protocol.
    Usage: `with get_session(db_path) as session: ...`
    SQLModel Session inherits from SQLAlchemy Session which implements
    __enter__/__exit__ returning self and calling close() on exit.
    """
    engine = get_engine(db_path)
    return Session(engine)


def find_cached_answer(
    session: Session, question_label: str, ats_type: str
) -> Optional[QuestionAnswer]:
    return session.exec(
        select(QuestionAnswer).where(
            QuestionAnswer.question_label == question_label.strip().lower(),
            QuestionAnswer.ats_type == ats_type,
        )
    ).first()


def upsert_answer(
    session: Session, question_label: str, ats_type: str, answer: str
) -> QuestionAnswer:
    existing = find_cached_answer(session, question_label, ats_type)
    if existing:
        existing.answer = answer
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        session.commit()
        return existing
    qa = QuestionAnswer(
        question_label=question_label.strip().lower(),
        ats_type=ats_type,
        answer=answer,
    )
    session.add(qa)
    session.commit()
    return qa
