"""Application lifecycle: intake, parsing, planning, invitations, decisions and deletion."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Application, Candidate, EmailMessage, Invitation, Job, RecruiterDecision, User, utcnow
from app.security import hash_token, new_invitation_token
from app.services import audit, storage
from app.services.email_service import REDACTED_LINK, OutgoingEmail, get_backend
from app.services.interview_planner import build_plan
from app.services.job_matcher import match
from app.services.llm_provider import LLMProvider
from app.services.resume_parser import extract_text, number_lines, parse_resume, validate_upload

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


class DuplicateApplication(Exception):
    pass


class InvitationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_candidate_fields(name: str, email: str) -> dict[str, str]:
    errors = {}
    if len(name.strip()) < 2:
        errors["name"] = "Enter your full name."
    if len(name) > 200:
        errors["name"] = "That name is too long."
    if not EMAIL_RE.match(email.strip()):
        errors["email"] = "Enter a valid email address, like name@example.com."
    return errors


def get_or_create_candidate(db: Session, name: str, email: str) -> Candidate:
    email_n = email.strip().lower()
    cand = db.scalar(select(Candidate).where(func.lower(Candidate.email) == email_n))
    if cand is None:
        cand = Candidate(name=name.strip()[:200], email=email_n)
        db.add(cand)
        db.flush()
    return cand


def create_application(
    db: Session,
    job: Job,
    *,
    name: str,
    email: str,
    filename: str,
    content: bytes,
    provider: LLMProvider,
    source: str = "public",
    consented: bool = True,
    actor: User | None = None,
) -> Application:
    """Validate, store and parse a resume. Raises UploadError/ExtractionError/DuplicateApplication."""
    vf = validate_upload(filename, content)
    text = extract_text(content, vf.kind)  # raises ExtractionError with a user-facing message
    cand = get_or_create_candidate(db, name, email)
    exists = db.scalar(select(Application.id).where(Application.candidate_id == cand.id, Application.job_id == job.id))
    if exists:
        db.rollback()
        raise DuplicateApplication()
    key = storage.save(db, content, vf.mime)
    now = utcnow()
    app = Application(
        candidate_id=cand.id,
        job_id=job.id,
        resume_file_key=key,
        resume_filename=vf.safe_name,
        resume_mime=vf.mime,
        resume_size=len(content),
        resume_text=text,
        source=source,
        consent_privacy_at=now if consented else None,
        consent_ai_at=now if consented else None,
    )
    db.add(app)
    db.flush()
    analyse(db, app, provider)
    audit.record(
        db, "application.created", "application", app.id,
        actor_type="user" if actor else "candidate", actor_id=actor.id if actor else cand.id,
        metadata={"job_id": job.id, "source": source, "file_kind": vf.kind, "size": len(content)},
    )
    db.commit()
    return app


def analyse(db: Session, app: Application, provider: LLMProvider) -> None:
    """(Re)run parsing, matching and planning from the stored resume text."""
    job = app.job
    lines = number_lines(app.resume_text)
    extra = [c["name"] for c in job.essential_criteria] + list(job.preferred_criteria)
    parsed = parse_resume(lines, provider, extra_skills=[e for e in extra if len(e) <= 30])
    parsed["lines"] = lines
    app.parsed_resume_json = parsed
    app.match_json = match(job.essential_criteria, job.preferred_criteria, lines)
    app.parse_status = "parsed"
    app.parse_error = ""
    app.plan_json = build_plan(job, app, provider)
    app.plan_approved_at = None


def regenerate_plan(db: Session, app: Application, provider: LLMProvider, user: User) -> None:
    app.match_json = match(app.job.essential_criteria, app.job.preferred_criteria, app.parsed_resume_json.get("lines", []))
    app.plan_json = build_plan(app.job, app, provider)
    app.plan_approved_at = None
    audit.record(db, "plan.regenerated", "application", app.id, actor_type="user", actor_id=user.id, metadata={"rubric_version": app.job.rubric_version})
    db.commit()


def update_plan_from_form(db: Session, app: Application, form: Any, user: User, approve: bool) -> str | None:
    """Recruiter edits to question wording (core structure stays the same)."""
    qs = app.plan_json.get("questions", [])
    new_qs = []
    for q in qs:
        text = str(form.get(f"q-{q['index']}-text") or q["text"]).strip()
        if len(text) < 10:
            return f"Question {q['number']} is too short."
        new_qs.append(q | {"text": text[:600]})
    plan = dict(app.plan_json)
    plan["questions"] = new_qs
    plan["edited_by_recruiter"] = True
    app.plan_json = plan
    if approve:
        app.plan_approved_at = utcnow()
    audit.record(db, "plan.approved" if approve else "plan.edited", "application", app.id, actor_type="user", actor_id=user.id)
    db.commit()
    return None


def active_invitation(app: Application) -> Invitation | None:
    for inv in app.invitations:
        if inv.is_active:
            return inv
    return None


def interview_link(token: str) -> str:
    return f"{settings().base_url.rstrip('/')}/interview/{token}"


def build_invitation_email(app: Application, invitation: Invitation, link: str, note: str = "") -> OutgoingEmail:
    from app.templating import render_email

    s = settings()
    job = app.job
    ctx = {
        "candidate_first": app.candidate.name.split()[0] if app.candidate.name else "there",
        "job": job,
        "link": link,
        "expires_at": invitation.expires_at,
        "note": note.strip()[:1000],
        "s": s,
        "question_count": len(app.plan_json.get("questions", [])),
    }
    subject = f"Your written interview for {job.title}"
    return OutgoingEmail(
        to=app.candidate.email,
        subject=subject,
        text=render_email("email/invitation.txt", ctx),
        html=render_email("email/invitation.html", ctx),
    )


def issue_invitation(db: Session, app: Application, user: User, *, note: str = "", valid_days: int | None = None, replace: bool = False) -> tuple[Invitation, str, EmailMessage]:
    """Create a single-use link and email it. Returns (invitation, token, email_record).

    The token is returned to the caller only so that dry-run mode can show the link to the
    recruiter once; it is never stored or logged.
    """
    if app.parse_status != "parsed" or not app.plan_json.get("questions"):
        raise InvitationError("The resume must be parsed and an interview plan prepared before inviting.")
    if app.latest_session and app.latest_session.status == "completed":
        raise InvitationError("This candidate has already completed an interview.")
    current = active_invitation(app)
    if current and not replace:
        raise InvitationError("An active invitation already exists. Revoke it or use “Send a new link”.")
    if current and replace:
        current.revoked_at = utcnow()
        current.revoked_reason = "replaced"
        audit.record(db, "invitation.revoked", "invitation", current.id, actor_type="user", actor_id=user.id, metadata={"reason": "replaced"})

    days = valid_days or app.job.invite_valid_days or settings().invite_valid_days
    token = new_invitation_token()
    th = hash_token(token)
    inv = Invitation(
        application_id=app.id,
        token_hash=th,
        token_hint=th[-4:],
        expires_at=utcnow() + timedelta(days=days),
        created_by_id=user.id,
        delivery_status="pending",
    )
    db.add(inv)
    app.invitations.insert(0, inv)
    db.flush()

    if not app.plan_approved_at:
        app.plan_approved_at = utcnow()
    msg = build_invitation_email(app, inv, interview_link(token), note)
    backend = get_backend()
    result = backend.send(msg)
    record = EmailMessage(
        application_id=app.id,
        invitation_id=inv.id,
        to_email=msg.to,
        subject=msg.subject,
        body_text=msg.text.replace(interview_link(token), REDACTED_LINK),
        body_html=msg.html.replace(interview_link(token), REDACTED_LINK),
        backend=result.backend,
        status=result.status,
        error=result.error,
        provider_message_id=result.provider_id,
    )
    db.add(record)
    inv.delivery_status = result.status
    inv.last_error = result.error
    if result.status == "sent":
        inv.sent_at = utcnow()
    if app.status == "new":
        app.status = "invited"
    audit.record(
        db, "invitation.created", "invitation", inv.id, actor_type="user", actor_id=user.id,
        metadata={"application_id": app.id, "delivery": result.status, "backend": result.backend, "valid_days": days},
    )
    db.commit()
    return inv, token, record


def revoke_invitation(db: Session, inv: Invitation, user: User, reason: str = "revoked by recruiter") -> None:
    if inv.revoked_at is None:
        inv.revoked_at = utcnow()
        inv.revoked_reason = reason[:200]
        audit.record(db, "invitation.revoked", "invitation", inv.id, actor_type="user", actor_id=user.id, metadata={"reason": reason})
        db.commit()


def record_decision(db: Session, app: Application, decision: str, notes: str, user: User) -> RecruiterDecision:
    if decision not in ("advance", "hold", "decline", "undecided"):
        raise ValueError("Unknown decision")
    d = RecruiterDecision(application_id=app.id, decision=decision, notes=notes.strip()[:5000], recruiter_id=user.id)
    db.add(d)
    app.decisions.insert(0, d)
    if decision != "undecided":
        app.status = "decided"
    elif app.status == "decided":
        app.status = "completed" if app.latest_session and app.latest_session.status == "completed" else "new"
    audit.record(db, "decision.recorded", "application", app.id, actor_type="user", actor_id=user.id, metadata={"decision": decision})
    db.commit()
    return d


def delete_application_data(db: Session, app: Application, actor_type: str, actor_id: Any, reason: str) -> None:
    """Remove the resume file and all candidate data for this application."""
    app_id, cand = app.id, app.candidate
    storage.delete(db, app.resume_file_key)
    db.delete(app)
    db.flush()
    remaining = db.scalar(select(func.count(Application.id)).where(Application.candidate_id == cand.id))
    if not remaining:
        db.delete(cand)
    audit.record(db, "application.deleted", "application", app_id, actor_type=actor_type, actor_id=actor_id, metadata={"reason": reason})
    db.commit()


def purge_expired(db: Session) -> int:
    cutoff = utcnow() - timedelta(days=settings().retention_days)
    old = db.scalars(select(Application).where(Application.updated_at < cutoff).limit(200)).all()
    for app in old:
        delete_application_data(db, app, "system", "retention", f"retention policy ({settings().retention_days} days)")
    return len(old)
