"""Evidence-based evaluation reports.

Model output is validated against :class:`app.schemas.Report`, then normalised by
non-negotiable policy rules (see :func:`normalise`). Failures are recorded and retryable; a
report is never fabricated.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import EvaluationReport, InterviewSession, as_utc, utcnow
from app.schemas import RECOMMENDATION_LABELS, Report
from app.services import audit
from app.services.llm_provider import LLMError, LLMProvider, get_provider

log = logging.getLogger(__name__)

STALE_RUNNING = timedelta(minutes=3)  # longer than the function time limit
MAX_ATTEMPTS = 5


def queue_report(db: Session, session: InterviewSession) -> EvaluationReport:
    current = session.latest_report
    if current and current.status == "running" and as_utc(current.updated_at) < utcnow() - STALE_RUNNING:
        current.status = "queued"  # the previous run was cut off (e.g. function timeout)
        return current
    if current and current.status in ("queued", "running", "ready"):
        return current
    if current and current.status == "failed":
        current.status = "queued"
        current.error = ""
        return current
    report = EvaluationReport(session_id=session.id, status="queued", rubric_version=session.rubric_version)
    db.add(report)
    session.reports.insert(0, report)
    db.flush()
    return report


def build_context(session: InterviewSession) -> dict[str, Any]:
    plan_qs = session.plan_json.get("questions", [])
    answers: list[dict[str, Any]] = []
    for q in plan_qs:
        idx = q["index"]
        texts = [m.content for m in session.messages if m.role == "candidate" and m.kind == "answer" and m.question_index == idx]
        followups = [m.content for m in session.messages if m.kind == "follow_up" and m.question_index == idx]
        answers.append(
            {
                "q": f"Q{idx + 1}",
                "question": q["text"],
                "category": q["category"],
                "criterion_id": q.get("criterion_id"),
                "behavior_id": q.get("behavior_id"),
                "target": q.get("target", ""),
                "scored": q.get("scored", True),
                "follow_up_questions": followups,
                "texts": texts,
            }
        )
    app = session.application
    snap = session.rubric_snapshot_json
    return {
        "job": {
            "title": app.job.title,
            "seniority": app.job.seniority,
            "essential_criteria": [
                c | {"anchors": snap.get("anchors", {}).get(c["id"], {})} for c in snap.get("essential_criteria", [])
            ],
            "preferred_criteria": snap.get("preferred_criteria", []),
            "behaviors": [b for b in snap.get("behaviors", []) if b.get("enabled")],
        },
        "plan": [{"q": f"Q{q['index'] + 1}", "text": q["text"], "category": q["category"], "target": q.get("target")} for q in plan_qs],
        "answers": answers,
        "resume_lines": app.parsed_resume_json.get("lines", []),
        "match": app.match_json,
    }


def _norm_ref(ref: str) -> str:
    r = (ref or "").strip().upper()
    m = re.match(r"^R\s*-?\s*(\d+)$", r)
    if m:
        return f"R-{int(m.group(1))}"
    m = re.match(r"^Q\s*-?\s*(\d+)$", r)
    if m:
        return f"Q{int(m.group(1))}"
    return r


def normalise(report: Report, ctx: dict[str, Any]) -> dict[str, Any]:
    """Apply policy rules that the model cannot override."""
    adjustments: list[str] = []
    answered_q = {a["q"] for a in ctx["answers"] if a["texts"]}
    resume_refs = {ln["ref"] for ln in ctx["resume_lines"]}
    valid = answered_q | resume_refs

    def refs(xs: list[str]) -> list[str]:
        out = []
        for r in xs:
            r = _norm_ref(r)
            if r in valid and r not in out:
                out.append(r)
        return out

    data = report.model_dump()
    data["summary_refs"] = refs(data["summary_refs"])

    crit_questions: dict[str, set[str]] = {}
    for a in ctx["answers"]:
        if a.get("criterion_id") and a["texts"]:
            crit_questions.setdefault(a["criterion_id"], set()).add(a["q"])

    by_id = {c["criterion_id"]: c for c in data["criteria"]}
    criteria = []
    for c in ctx["job"]["essential_criteria"]:
        item = by_id.get(c["id"]) or {"criterion_id": c["id"], "rating": None, "status": "not_assessed", "evidence": "", "gap": "", "refs": []}
        if c["id"] not in by_id:
            adjustments.append(f"“{c['name']}” was missing from the AI output and is shown as not assessed.")
        item["refs"] = refs(item["refs"])
        item["name"] = c["name"]
        if item["status"] != "rated":
            item["rating"] = None
        elif item["rating"] is None:
            item["status"] = "insufficient_evidence"
        elif not item["refs"]:
            item["rating"], item["status"] = None, "insufficient_evidence"
            adjustments.append(f"Rating for “{c['name']}” withheld: no valid transcript or resume reference was cited.")
        if item["status"] == "rated" and not (set(item["refs"]) & answered_q) and c["id"] not in crit_questions:
            # A rating backed only by resume lines is not interview evidence.
            item["rating"], item["status"] = None, "not_assessed"
            adjustments.append(f"“{c['name']}” was not covered by an answered question, so it is shown as not assessed.")
        criteria.append(item)
    data["criteria"] = criteria

    all_q = {a["q"] for a in ctx["answers"]}
    data["questions"] = [q | {"q": _norm_ref(q["q"]), "refs": refs(q["refs"])} for q in data["questions"] if _norm_ref(q["q"]) in all_q]
    for q in data["questions"]:
        if q["q"] not in answered_q:
            q["rating"], q["status"] = None, "not_assessed"
        elif q["status"] != "rated":
            q["rating"] = None

    beh_ids = {b["id"]: b for b in ctx["job"]["behaviors"]}
    behaviors = {b["behavior_id"]: b for b in data["behaviors"] if b["behavior_id"] in beh_ids}
    out_b = []
    for bid, b in beh_ids.items():
        item = behaviors.get(bid) or {"behavior_id": bid, "rating": None, "status": "not_assessed", "note": "Not assessed.", "refs": []}
        item["refs"] = refs(item["refs"])
        item["name"] = b["name"]
        if item["status"] != "rated" or not item["refs"]:
            item["rating"] = None
            if item["status"] == "rated":
                item["status"] = "insufficient_evidence"
        out_b.append(item)
    data["behaviors"] = out_b

    for key in ("strengths", "gaps"):
        for it in data[key]:
            it["refs"] = refs(it["refs"])
    for key in ("supported", "needs_clarification", "not_covered"):
        for it in data["resume_crosscheck"][key]:
            it["refs"] = refs(it["refs"])
    # Strengths must be evidenced.
    before = len(data["strengths"])
    data["strengths"] = [s for s in data["strengths"] if s["refs"]]
    if len(data["strengths"]) < before:
        adjustments.append("Strengths without a supporting reference were removed.")

    scored = [a for a in ctx["answers"] if a.get("scored", True)]
    answered = [a for a in scored if a["texts"]]
    rec = data["recommendation"]
    weak = [c for c in criteria if c["status"] != "rated" or (c["rating"] or 0) <= 2]
    if len(answered) < max(1, len(scored)) / 2 and rec != "insufficient_evidence":
        adjustments.append("Recommendation set to “Insufficient evidence”: fewer than half of the scored questions were answered.")
        rec = "insufficient_evidence"
    elif rec == "recommend_proceeding" and weak:
        adjustments.append("Recommendation changed to “Proceed with follow-up”: at least one essential criterion is not yet met or not assessed.")
        rec = "proceed_with_follow_up"
    data["recommendation"] = rec
    data["recommendation_label"] = RECOMMENDATION_LABELS[rec]
    data["policy_adjustments"] = adjustments
    return data


def generate(db: Session, report: EvaluationReport, provider: LLMProvider | None = None) -> EvaluationReport:
    if report.status == "ready":
        return report
    provider = provider or get_provider()
    session = report.session
    report.status = "running"
    report.attempts += 1
    report.provider = provider.name
    report.model = provider.model
    db.commit()
    ctx = build_context(session)
    last_error = ""
    started = time.monotonic()
    for attempt in range(2):
        if attempt and time.monotonic() - started > 15:
            break  # not enough time left for a second model call
        try:
            raw = provider.evaluate(ctx)
            validated = Report.model_validate(raw)
            data = normalise(validated, ctx)
            data["meta"] = {
                "provider": provider.name,
                "model": provider.model,
                "is_mock": provider.is_mock,
                "rubric_version": session.rubric_version,
                "transcript_hash": session.transcript_hash,
                "answered": sum(1 for a in ctx["answers"] if a["texts"] and a.get("scored", True)),
                "scored_total": sum(1 for a in ctx["answers"] if a.get("scored", True)),
                "ended_early": session.ended_early,
            }
            report.report_json = data
            report.status = "ready"
            report.error = ""
            report.generated_at = utcnow()
            audit.record(db, "report.generated", "evaluation_report", report.id, metadata={"provider": provider.name, "attempt": report.attempts})
            db.commit()
            return report
        except ValidationError as exc:
            last_error = "The AI returned a report that did not match the required format."
            log.warning("report validation failed", extra={"safe": {"errors": exc.error_count()}})
        except LLMError as exc:
            last_error = str(exc)[:300]
            break
        except Exception:  # noqa: BLE001 - never leave a report stuck in "running"
            log.exception("report generation crashed")
            last_error = "Unexpected error while generating the report."
            break
    report.status = "failed"
    report.error = last_error or "Report generation failed."
    audit.record(db, "report.failed", "evaluation_report", report.id, metadata={"attempt": report.attempts, "error": report.error})
    db.commit()
    return report


def process_pending(db: Session, limit: int = 3) -> int:
    stale_before = utcnow() - STALE_RUNNING
    rows = db.scalars(
        select(EvaluationReport)
        .where(
            or_(
                EvaluationReport.status == "queued",
                (EvaluationReport.status == "running") & (EvaluationReport.updated_at < stale_before),
            ),
            EvaluationReport.attempts < MAX_ATTEMPTS,
        )
        .order_by(EvaluationReport.id)
        .limit(limit)
    ).all()
    done = 0
    for r in rows:
        if r.status == "running" and as_utc(r.updated_at) and as_utc(r.updated_at) >= stale_before:
            continue
        generate(db, r)
        done += 1
    return done


def generate_in_background(report_id: int) -> None:
    """Entry point for FastAPI BackgroundTasks; opens its own DB session."""
    from app.database import db_session

    with db_session() as db:
        report = db.get(EvaluationReport, report_id)
        if report and report.status in ("queued", "failed"):
            generate(db, report)
