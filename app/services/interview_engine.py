"""Text interview state machine.

States: ``in_progress`` -> ``completed``. Within ``in_progress`` the session walks the stored
plan one question at a time, with at most one follow-up per question and a job-level cap on
follow-ups. Candidate inputs are idempotent via ``client_msg_id`` so retries after a network
error never create duplicate answers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Application, InterviewMessage, InterviewSession, Invitation, utcnow
from app.schemas import AnswerAssessment, Clarification
from app.services import audit
from app.services.llm_provider import LLMError, LLMProvider
from app.services.text_utils import clip

log = logging.getLogger(__name__)


class InterviewError(Exception):
    """User-facing interview problem (shown to the candidate)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


PROMPT_KINDS = ("question", "follow_up")


def _questions(session: InterviewSession) -> list[dict[str, Any]]:
    return session.plan_json.get("questions", [])


def _add(db: Session, session: InterviewSession, role: str, kind: str, content: str, *, qi: int | None, category: str = "", client_id: str | None = None) -> InterviewMessage:
    seq = (max((m.sequence for m in session.messages), default=0)) + 1
    msg = InterviewMessage(
        session_id=session.id, sequence=seq, role=role, kind=kind, content=content,
        question_index=qi, question_category=category, client_msg_id=client_id,
    )
    db.add(msg)
    session.messages.append(msg)
    return msg


def _ask(db: Session, session: InterviewSession, index: int) -> None:
    q = _questions(session)[index]
    _add(db, session, "interviewer", "question", q["text"], qi=index, category=q["category"])


def reference_code(session_id: int) -> str:
    digest = hmac.new(settings().secret_key.encode(), f"session:{session_id}".encode(), hashlib.sha256).digest()
    code = base64.b32encode(digest[:5]).decode().rstrip("=")
    return f"SW-{code[:4]}-{code[4:8]}"


def start_session(db: Session, application: Application, invitation: Invitation) -> InterviewSession:
    """Create the session (idempotent: returns the existing one for this invitation)."""
    existing = db.scalar(
        select(InterviewSession).where(InterviewSession.application_id == application.id).order_by(InterviewSession.id.desc())
    )
    if existing and existing.status == "completed":
        return existing
    if existing and existing.status == "in_progress":
        # A replacement link continues the same interview rather than starting over.
        if existing.invitation_id != invitation.id:
            existing.invitation_id = invitation.id
            db.commit()
        return existing
    plan = application.plan_json
    if not plan.get("questions"):
        raise InterviewError("This interview isn't ready yet. Please contact the recruiting team.", 409)
    job = application.job
    session = InterviewSession(
        application_id=application.id,
        invitation_id=invitation.id,
        status="in_progress",
        rubric_version=plan.get("rubric_version", job.rubric_version),
        rubric_snapshot_json={
            "essential_criteria": job.essential_criteria,
            "preferred_criteria": job.preferred_criteria,
            "anchors": job.rubric_json.get("anchors", {}),
            "behaviors": job.rubric_json.get("behaviors", []),
            "rubric_version": job.rubric_version,
        },
        plan_json=plan,
        current_index=0,
    )
    db.add(session)
    db.flush()
    session.reference_code = reference_code(session.id)
    total = len(plan["questions"])
    first_name = application.candidate.name.split()[0] if application.candidate.name else "there"
    _add(
        db, session, "interviewer", "intro",
        f"Hi {first_name}, thanks for joining. I'll ask {total} questions, one at a time, and sometimes a short follow-up. "
        "Take your time, answer from your own experience, and ask if anything is unclear.",
        qi=None,
    )
    _ask(db, session, 0)
    application.status = "interviewing"
    audit.record(db, "interview.started", "interview_session", session.id, actor_type="candidate", actor_id=application.candidate_id)
    db.commit()
    return session


def _last_prompt(session: InterviewSession) -> InterviewMessage | None:
    for m in reversed(session.messages):
        if m.role == "interviewer" and m.kind in PROMPT_KINDS:
            return m
    return None


def _existing_client_msg(db: Session, session: InterviewSession, client_id: str | None) -> bool:
    if not client_id:
        return False
    return db.scalar(
        select(InterviewMessage.id).where(InterviewMessage.session_id == session.id, InterviewMessage.client_msg_id == client_id)
    ) is not None


def _clean_client_id(client_id: str | None) -> str | None:
    if not client_id:
        return None
    cid = "".join(c for c in client_id if c.isalnum() or c in "-_")[:64]
    return cid or None


def submit_answer(db: Session, session: InterviewSession, text: str, client_id: str | None, provider: LLMProvider, expected_prompt: int | None = None) -> None:
    client_id = _clean_client_id(client_id)
    if session.status != "in_progress":
        raise InterviewError("This interview has already been submitted.", 409)
    if _existing_client_msg(db, session, client_id):
        return  # duplicate submit (double click or retry) — already stored
    text = (text or "").replace("\x00", "").strip()
    if not text:
        raise InterviewError("Please type an answer before submitting, or use “Ask for clarification”.")
    limit = settings().max_answer_chars
    if len(text) > limit:
        raise InterviewError(f"Your answer is {len(text):,} characters. Please shorten it to {limit:,} characters or fewer.")
    questions = _questions(session)
    idx = session.current_index
    if idx >= len(questions):
        raise InterviewError("All questions are answered. Press “Finish and submit interview”.", 409)
    q = questions[idx]
    prompt = _last_prompt(session)
    if expected_prompt is not None and prompt is not None and prompt.sequence != expected_prompt:
        # The answer was written for a question that has since been answered (double submit, second tab).
        raise InterviewError("That question was already answered, so this copy wasn't saved. Here's the current question.", 409)
    try:
        _add(db, session, "candidate", "answer", text, qi=idx, category=q["category"], client_id=client_id)
        db.flush()
    except IntegrityError:
        db.rollback()
        raise InterviewError("Your answer couldn't be saved because the interview changed in another window. Please submit again.", 409) from None

    follow_up_text: str | None = None
    already_followed = any(m.kind == "follow_up" and m.question_index == idx for m in session.messages)
    max_fu = int(session.plan_json.get("max_followups", 3))
    if q.get("scored", True) and not already_followed and session.followups_used < max_fu:
        try:
            raw = provider.assess_answer(
                {
                    "question": prompt.content if prompt else q["text"],
                    "criterion": q.get("target"),
                    "band": f"{session.plan_json.get('band', {}).get('label', '')}: {session.plan_json.get('band', {}).get('focus', '')}",
                    "answer": text,
                }
            )
            result = AnswerAssessment.model_validate(raw)
            if result.action == "follow_up" and result.follow_up and len(result.follow_up.strip()) >= 10:
                follow_up_text = clip(result.follow_up, 400)
        except (LLMError, ValidationError) as exc:
            log.warning("answer assessment unavailable; continuing", extra={"safe": {"error": type(exc).__name__}})

    if follow_up_text:
        _add(db, session, "interviewer", "follow_up", follow_up_text, qi=idx, category=q["category"])
        session.followups_used += 1
    else:
        session.current_index = idx + 1
        if session.current_index < len(questions):
            _ask(db, session, session.current_index)
        else:
            _add(db, session, "interviewer", "closing", "Thank you, that's all the questions. Press “Finish and submit interview” to lock your answers.", qi=None)
    session.last_activity_at = utcnow()
    db.commit()


def request_clarification(db: Session, session: InterviewSession, request_text: str | None, client_id: str | None, provider: LLMProvider) -> None:
    client_id = _clean_client_id(client_id)
    if session.status != "in_progress":
        raise InterviewError("This interview has already been submitted.", 409)
    if _existing_client_msg(db, session, client_id):
        return
    questions = _questions(session)
    idx = session.current_index
    if idx >= len(questions):
        raise InterviewError("All questions are answered.", 409)
    q = questions[idx]
    prompt = _last_prompt(session)
    asked = sum(1 for m in session.messages if m.kind == "clarification_request" and m.question_index == idx)
    req = clip((request_text or "").strip(), 1000) or "Could you clarify this question?"
    _add(db, session, "candidate", "clarification_request", req, qi=idx, category=q["category"], client_id=client_id)
    if asked >= settings().max_clarifications_per_question:
        reply = "Please answer as best you can. It's fine to state any assumptions you're making; that won't count against you."
    else:
        fallback = q.get("clarification") or "Answer from your own experience. A specific example helps most."
        if prompt and prompt.kind == "follow_up":
            fallback = "This is a short follow-up to your last answer. A specific example of what you did yourself is most helpful."
        try:
            raw = provider.clarify({"question": prompt.content if prompt else q["text"], "request": request_text, "fallback": fallback})
            reply = Clarification.model_validate(raw).text.strip()
        except (LLMError, ValidationError):
            reply = fallback
    _add(db, session, "interviewer", "clarification", clip(reply, 700), qi=idx, category=q["category"])
    session.last_activity_at = utcnow()
    db.commit()


def transcript_hash(session: InterviewSession) -> str:
    h = hashlib.sha256()
    for m in session.messages:
        h.update(f"{m.sequence}|{m.role}|{m.kind}|{m.question_index}|{m.content}\n".encode())
    return h.hexdigest()


def complete(db: Session, session: InterviewSession, *, ended_early: bool = False) -> bool:
    """Lock the transcript. Returns True if this call completed it (False if already complete)."""
    if session.status == "completed":
        return False
    questions = _questions(session)
    session.ended_early = ended_early or session.current_index < len(questions)
    session.status = "completed"
    session.completed_at = utcnow()
    session.transcript_hash = transcript_hash(session)
    if session.application.status != "decided":
        session.application.status = "completed"
    audit.record(
        db, "interview.completed", "interview_session", session.id, actor_type="candidate", actor_id=session.application.candidate_id,
        metadata={"answered": session.current_index, "total": len(questions), "ended_early": session.ended_early},
    )
    from app.services.evaluation import queue_report  # avoid import cycle

    queue_report(db, session)
    db.commit()
    return True


def message_meta(session: InterviewSession, m: InterviewMessage) -> str:
    questions = _questions(session)
    total = len(questions)
    if m.role == "candidate":
        return "You · asked for clarification" if m.kind == "clarification_request" else "You · submitted"
    if m.kind == "question" and m.question_index is not None:
        label = questions[m.question_index].get("category_label", "")
        return f"Question {m.question_index + 1} of {total}" + (f" · {label}" if label else "")
    return {"follow_up": "Follow-up", "clarification": "Clarification", "intro": "Interviewer", "closing": "Interviewer"}.get(m.kind, "Interviewer")


def public_state(session: InterviewSession) -> dict[str, Any]:
    questions = _questions(session)
    total = len(questions)
    idx = min(session.current_index, total)
    return {
        "status": session.status,
        "ref": session.reference_code,
        "prompt_seq": (lambda p: p.sequence if p else None)(_last_prompt(session)),
        "messages": [
            {"id": m.sequence, "role": m.role, "kind": m.kind, "text": m.content, "meta": message_meta(session, m)}
            for m in session.messages
        ],
        "progress": {"current": min(idx + 1, total), "answered": idx, "total": total},
        "all_answered": idx >= total,
        "completed": session.status == "completed",
        "minutes_left": max(2, round((total - idx) * 3)),
    }
