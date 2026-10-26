"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="recruiter")  # admin | recruiter
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p]
        return ("".join(p[0] for p in parts[:2]) or self.email[:2]).upper()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    department: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    seniority: Mapped[str] = mapped_column(String(40), default="mid")
    description: Mapped[str] = mapped_column(Text, default="")
    # [{id, name, description}]
    essential_criteria: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # ["skill", ...]
    preferred_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    # {"anchors": {criterion_id: {"1": str, ...}}, "behaviors": [{id, name, anchor, enabled}]}
    rubric_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rubric_version: Mapped[int] = mapped_column(Integer, default=1)
    # [{id, category, criterion_id, text, clarification}]
    core_questions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{min_years, max_years, label, focus}]
    experience_bands_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    max_followups: Mapped[int] = mapped_column(Integer, default=3)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=25)
    invite_valid_days: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)  # draft | open | closed
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    applications: Mapped[list[Application]] = relationship(back_populates="job")


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), index=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    applications: Mapped[list[Application]] = relationship(back_populates="candidate")


class StoredFile(Base):
    """Uploaded file bytes, kept in the database (outside any public web root)."""

    __tablename__ = "stored_files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("candidate_id", "job_id", name="uq_application_candidate_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    resume_file_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resume_filename: Mapped[str] = mapped_column(String(255), default="")
    resume_mime: Mapped[str] = mapped_column(String(120), default="")
    resume_size: Mapped[int] = mapped_column(Integer, default=0)
    resume_text: Mapped[str] = mapped_column(Text, default="")
    parsed_resume_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_approved_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    parse_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | parsed | failed
    parse_error: Mapped[str] = mapped_column(String(500), default="")
    # new | invited | interviewing | completed | decided
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    source: Mapped[str] = mapped_column(String(20), default="public")  # public | recruiter
    consent_privacy_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    consent_ai_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="applications")
    job: Mapped[Job] = relationship(back_populates="applications")
    invitations: Mapped[list[Invitation]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="Invitation.created_at.desc()"
    )
    sessions: Mapped[list[InterviewSession]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="InterviewSession.id.desc()"
    )
    decisions: Mapped[list[RecruiterDecision]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="RecruiterDecision.id.desc()"
    )
    emails: Mapped[list[EmailMessage]] = relationship(
        back_populates="application", cascade="all, delete-orphan", order_by="EmailMessage.id.desc()"
    )

    @property
    def latest_session(self) -> InterviewSession | None:
        return self.sessions[0] if self.sessions else None

    @property
    def latest_invitation(self) -> Invitation | None:
        return self.invitations[0] if self.invitations else None

    @property
    def latest_decision(self) -> RecruiterDecision | None:
        return self.decisions[0] if self.decisions else None


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(8), default="")  # last chars of the hash, for display only
    expires_at: Mapped[datetime] = mapped_column(TS)
    sent_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revoked_reason: Mapped[str] = mapped_column(String(200), default="")
    # pending | logged_not_sent | sent | failed
    delivery_status: Mapped[str] = mapped_column(String(30), default="pending")
    last_error: Mapped[str] = mapped_column(String(500), default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="invitations")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        exp = as_utc(self.expires_at)
        return bool(exp and exp <= utcnow())

    @property
    def is_active(self) -> bool:
        return not self.is_revoked and not self.is_expired


class EmailMessage(Base):
    """Every email attempt. Bodies are stored with the interview link redacted."""

    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=True, index=True
    )
    invitation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invitations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str] = mapped_column(Text, default="")
    backend: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))  # logged_not_sent | sent | failed
    error: Mapped[str] = mapped_column(String(500), default="")
    provider_message_id: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    application: Mapped[Application | None] = relationship(back_populates="emails")


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    invitation_id: Mapped[int | None] = mapped_column(
        ForeignKey("invitations.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="in_progress")  # in_progress | completed
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    rubric_version: Mapped[int] = mapped_column(Integer, default=1)
    rubric_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    followups_used: Mapped[int] = mapped_column(Integer, default=0)
    ended_early: Mapped[bool] = mapped_column(Boolean, default=False)
    transcript_hash: Mapped[str] = mapped_column(String(64), default="")
    reference_code: Mapped[str] = mapped_column(String(32), default="", index=True)

    application: Mapped[Application] = relationship(back_populates="sessions")
    messages: Mapped[list[InterviewMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="InterviewMessage.sequence"
    )
    reports: Mapped[list[EvaluationReport]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="EvaluationReport.id.desc()"
    )

    @property
    def latest_report(self) -> EvaluationReport | None:
        return self.reports[0] if self.reports else None


class InterviewMessage(Base):
    __tablename__ = "interview_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_message_sequence"),
        UniqueConstraint("session_id", "client_msg_id", name="uq_message_client_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))  # interviewer | candidate
    # intro | question | follow_up | clarification | clarification_request | answer | closing
    kind: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    question_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_category: Mapped[str] = mapped_column(String(60), default="")
    client_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    session: Mapped[InterviewSession] = relationship(back_populates="messages")


class EvaluationReport(Base):
    __tablename__ = "evaluation_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|ready|failed
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    rubric_version: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)

    session: Mapped[InterviewSession] = relationship(back_populates="reports")


class RecruiterDecision(Base):
    """Human decisions, append-only. The newest row is the current decision."""

    __tablename__ = "recruiter_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(30))  # advance | hold | decline | undecided
    notes: Mapped[str] = mapped_column(Text, default="")
    recruiter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="decisions")
    recruiter: Mapped[User | None] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # user | candidate | system
    actor_id: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
