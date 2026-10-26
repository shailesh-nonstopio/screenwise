"""Recruiter-facing routes. Every route requires an authenticated, active user."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    Application,
    AuditEvent,
    Candidate,
    EmailMessage,
    InterviewSession,
    Invitation,
    Job,
    User,
    utcnow,
)
from app.routers.public import read_upload
from app.security import current_user, hash_password, require_admin, validate_new_password, verify_csrf, verify_password
from app.services import audit, storage
from app.services.applications import (
    DuplicateApplication,
    InvitationError,
    active_invitation,
    build_invitation_email,
    create_application,
    delete_application_data,
    issue_invitation,
    record_decision,
    regenerate_plan,
    revoke_invitation,
    update_plan_from_form,
    validate_candidate_fields,
    analyse,
)
from app.services.evaluation import generate, queue_report
from app.services.interview_engine import message_meta
from app.services.interview_planner import draft_core_questions
from app.services.jobs import job_to_form, new_job_defaults, parse_job_form, save_core_questions, save_job
from app.services.llm_provider import LLMError, get_provider
from app.services.resume_parser import ExtractionError, UploadError
from app.templating import flash, render

router = APIRouter(prefix="/recruiter", dependencies=[Depends(current_user), Depends(verify_csrf)])


def _app(db: Session, application_id: int) -> Application:
    app = db.get(Application, application_id)
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found. It may have been deleted.")
    return app


def _job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


# --------------------------------------------------------------------------- stage helpers

STAGES = {
    "review": "Needs review",
    "invited": "Invited",
    "interviewing": "Interview in progress",
    "completed": "Ready for decision",
    "decided": "Decided",
    "attention": "Needs attention",
}


def row_view(app: Application) -> dict[str, Any]:
    inv = app.latest_invitation
    sess = app.latest_session
    dec = app.latest_decision
    report = sess.latest_report if sess else None

    if app.parse_status == "failed":
        parse = ("Could not read file", "red")
    elif app.parsed_resume_json.get("unknowns"):
        parse = (f"Parsed · {len(app.parsed_resume_json['unknowns'])} unknown", "green")
    else:
        parse = ("Parsed", "green")

    if inv is None:
        invite = ("Not sent", "neutral")
    elif inv.revoked_at:
        invite = ("Revoked", "neutral")
    elif sess and sess.status == "completed":
        invite = ("Used", "neutral")
    elif inv.delivery_status == "failed":
        invite = ("Delivery failed", "red")
    elif inv.is_expired:
        invite = ("Expired", "amber")
    elif inv.delivery_status == "logged_not_sent":
        invite = ("Logged (not sent)", "amber")
    elif inv.opened_at:
        invite = ("Opened", "blue")
    else:
        invite = (f"Sent · expires {inv.expires_at.strftime('%b %d')}", "blue")

    if sess is None:
        interview = ("—", "neutral")
    elif sess.status == "completed":
        mins = max(1, round(((sess.completed_at or utcnow()) - sess.started_at).total_seconds() / 60)) if sess.completed_at else None
        interview = ("Ended early" if sess.ended_early else f"Complete · {mins} min", "amber" if sess.ended_early else "green")
    else:
        total = len(sess.plan_json.get("questions", []))
        interview = (f"In progress · Q{min(sess.current_index + 1, total)} of {total}", "amber")

    if report is None:
        ai = "—"
    elif report.status == "ready":
        ai = report.report_json.get("recommendation_label", "—")
    elif report.status == "failed":
        ai = "Report failed — retry"
    else:
        ai = "Generating…"

    decision = (dec.decision if dec else "undecided")
    attention = app.parse_status == "failed" or (inv and inv.delivery_status == "failed" and not inv.revoked_at) or (report and report.status == "failed")
    if report and report.status == "ready" or (sess and sess.status == "completed"):
        action, href = "View report", f"/recruiter/applications/{app.id}/report"
    elif inv and inv.delivery_status == "failed":
        action, href = "Resend", f"/recruiter/applications/{app.id}/invite"
    else:
        action, href = "Review", f"/recruiter/applications/{app.id}"
    return {
        "app": app, "parse": parse, "invite": invite, "interview": interview, "ai": ai,
        "decision": decision, "attention": bool(attention), "action": action, "href": href,
    }


# --------------------------------------------------------------------------- dashboard


@router.get("")
def dashboard(request: Request, q: str = "", job_id: int | None = None, stage: str = "", db: Session = Depends(get_db)):
    stmt = (
        select(Application)
        .join(Candidate)
        .options(
            selectinload(Application.candidate), selectinload(Application.job), selectinload(Application.invitations),
            selectinload(Application.sessions).selectinload(InterviewSession.reports), selectinload(Application.decisions),
        )
        .order_by(Application.created_at.desc())
    )
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Candidate.name).like(like), func.lower(Candidate.email).like(like)))
    if job_id:
        stmt = stmt.where(Application.job_id == job_id)
    if stage in ("invited", "interviewing", "completed", "decided"):
        stmt = stmt.where(Application.status == stage)
    elif stage == "review":
        stmt = stmt.where(Application.status == "new")
    apps = db.scalars(stmt.limit(500)).all()
    rows = [row_view(a) for a in apps]
    if stage == "attention":
        rows = [r for r in rows if r["attention"]]

    jobs = db.scalars(select(Job).order_by(Job.status.desc(), Job.created_at.desc())).all()
    counts = dict(db.execute(select(Application.job_id, func.count(Application.id)).group_by(Application.job_id)).all())
    all_status = dict(db.execute(select(Application.status, func.count(Application.id)).group_by(Application.status)).all())
    stats = [
        {"label": "Open jobs", "value": sum(1 for j in jobs if j.status == "open"), "note": f"{sum(counts.values())} applications"},
        {"label": "Awaiting your review", "value": all_status.get("new", 0), "note": "resumes parsed"},
        {"label": "Invited / in progress", "value": all_status.get("invited", 0) + all_status.get("interviewing", 0), "note": "interviews out"},
        {"label": "Ready for decision", "value": all_status.get("completed", 0), "note": "interviews complete"},
    ]
    attention = [r for r in (row_view(a) for a in apps) if r["attention"]] if stage != "attention" else rows
    return render(
        request, "recruiter/dashboard.html",
        {"rows": rows, "jobs": jobs, "counts": counts, "stats": stats, "q": q, "job_id": job_id, "stage": stage, "stages": STAGES, "attention_count": len(attention), "nav": "applications"},
    )


# --------------------------------------------------------------------------- jobs


@router.get("/jobs")
def jobs_list(request: Request, db: Session = Depends(get_db)):
    jobs = db.scalars(select(Job).order_by(Job.created_at.desc())).all()
    counts = dict(db.execute(select(Application.job_id, func.count(Application.id)).group_by(Application.job_id)).all())
    return render(request, "recruiter/jobs.html", {"jobs": jobs, "counts": counts, "nav": "jobs"})


@router.get("/jobs/new")
def job_new(request: Request):
    return render(request, "recruiter/job_form.html", {"form": new_job_defaults(), "errors": {}, "job": None, "nav": "jobs"})


@router.post("/jobs/new")
async def job_create(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    form = await request.form()
    data, errors = parse_job_form(form)
    if errors:
        return render(request, "recruiter/job_form.html", {"form": data, "errors": errors, "job": None, "nav": "jobs"}, status_code=422)
    job = save_job(db, None, data, user, get_provider())
    flash(request, "Job saved as a draft. Review the core questions, then publish it.", "success")
    return RedirectResponse(f"/recruiter/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    n = db.scalar(select(func.count(Application.id)).where(Application.job_id == job.id))
    return render(request, "recruiter/job_detail.html", {"job": job, "app_count": n, "nav": "jobs"})


@router.get("/jobs/{job_id}/edit")
def job_edit(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    return render(request, "recruiter/job_form.html", {"form": job_to_form(job), "errors": {}, "job": job, "nav": "jobs"})


@router.post("/jobs/{job_id}/edit")
async def job_update(request: Request, job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = _job(db, job_id)
    form = await request.form()
    data, errors = parse_job_form(form)
    if errors:
        return render(request, "recruiter/job_form.html", {"form": data, "errors": errors, "job": job, "nav": "jobs"}, status_code=422)
    before = job.rubric_version
    save_job(db, job, data, user, get_provider())
    msg = "Job updated."
    if job.rubric_version != before:
        msg += f" Rubric is now v{job.rubric_version}; interviews already sent keep the version they started with."
    flash(request, msg, "success")
    return RedirectResponse(f"/recruiter/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/status")
def job_status(request: Request, job_id: int, status: str = Form(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = _job(db, job_id)
    if status not in ("draft", "open", "closed"):
        raise HTTPException(status_code=400, detail="Unknown status.")
    if status == "open" and (not job.essential_criteria or not job.core_questions_json):
        flash(request, "Add essential criteria and core questions before publishing.", "error")
        return RedirectResponse(f"/recruiter/jobs/{job.id}", status_code=303)
    job.status = status
    audit.record(db, f"job.{status}", "job", job.id, actor_type="user", actor_id=user.id)
    db.commit()
    flash(request, {"open": "Job published. Candidates can now apply.", "closed": "Job closed to new applications.", "draft": "Job moved back to draft."}[status], "success")
    return RedirectResponse(f"/recruiter/jobs/{job.id}", status_code=303)


@router.post("/jobs/{job_id}/questions")
async def job_questions(request: Request, job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = _job(db, job_id)
    ok, msg = save_core_questions(db, job, await request.form(), user)
    flash(request, msg, "success" if ok else "error")
    return RedirectResponse(f"/recruiter/jobs/{job.id}#questions", status_code=303)


@router.post("/jobs/{job_id}/questions/regenerate")
def job_questions_regenerate(request: Request, job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = _job(db, job_id)
    questions, source = draft_core_questions(job, get_provider())
    job.core_questions_json = questions
    job.rubric_version += 1
    audit.record(db, "job.questions_regenerated", "job", job.id, actor_type="user", actor_id=user.id, metadata={"source": source})
    db.commit()
    flash(request, f"Core questions redrafted ({source}). Rubric is now v{job.rubric_version}.", "success")
    return RedirectResponse(f"/recruiter/jobs/{job.id}#questions", status_code=303)


@router.get("/jobs/{job_id}/applications")
def job_applications(job_id: int):
    return RedirectResponse(f"/recruiter?job_id={job_id}", status_code=303)


@router.get("/jobs/{job_id}/applications/new")
def application_new(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _job(db, job_id)
    return render(request, "recruiter/application_new.html", {"job": job, "form": {}, "errors": {}, "nav": "applications"})


@router.post("/jobs/{job_id}/applications")
async def application_create(
    request: Request,
    job_id: int,
    name: str = Form(""),
    email: str = Form(""),
    consent_confirmed: str = Form(""),
    resume: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    job = _job(db, job_id)
    form = {"name": name, "email": email, "consent_confirmed": consent_confirmed}
    errors = validate_candidate_fields(name, email)
    if not consent_confirmed:
        errors["consent_confirmed"] = "Confirm the candidate has been given the privacy notice."
    filename, content = await read_upload(resume)
    if not content:
        errors["resume"] = "Choose a PDF or DOCX resume."
    if not errors:
        try:
            app = create_application(db, job, name=name, email=email, filename=filename, content=content, provider=get_provider(), source="recruiter", actor=user)
            flash(request, "Application added and resume parsed.", "success")
            return RedirectResponse(f"/recruiter/applications/{app.id}", status_code=303)
        except (UploadError, ExtractionError) as exc:
            errors["resume"] = str(exc)
        except DuplicateApplication:
            errors["email"] = "This candidate has already applied for this job."
        except LLMError:
            errors["form"] = "The AI provider is unavailable. Try again shortly."
    return render(request, "recruiter/application_new.html", {"job": job, "form": form, "errors": errors, "nav": "applications"}, status_code=422)


# --------------------------------------------------------------------------- application review


@router.get("/applications/{application_id}")
def application_detail(request: Request, application_id: int, db: Session = Depends(get_db)):
    app = _app(db, application_id)
    return render(
        request, "recruiter/application.html",
        {"app": app, "row": row_view(app), "active_inv": active_invitation(app), "tab": "overview", "nav": "applications", "rubric_outdated": app.plan_json.get("rubric_version") != app.job.rubric_version},
    )


@router.get("/applications/{application_id}/resume")
def application_resume(application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    if not app.resume_file_key:
        raise HTTPException(status_code=404, detail="The original file is no longer stored.")
    try:
        content = storage.load(db, app.resume_file_key)
    except storage.StorageError as exc:
        raise HTTPException(status_code=404, detail="The original file is no longer stored.") from exc
    audit.record(db, "resume.downloaded", "application", app.id, actor_type="user", actor_id=user.id)
    db.commit()
    return Response(
        content, media_type=app.resume_mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{app.resume_filename or "resume"}"', "X-Content-Type-Options": "nosniff"},
    )


@router.post("/applications/{application_id}/plan")
async def application_plan(request: Request, application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    form = await request.form()
    approve = form.get("action") == "approve"
    err = update_plan_from_form(db, app, form, user, approve)
    if err:
        flash(request, err, "error")
        return RedirectResponse(f"/recruiter/applications/{app.id}#plan", status_code=303)
    if approve:
        return RedirectResponse(f"/recruiter/applications/{app.id}/invite", status_code=303)
    flash(request, "Interview plan saved.", "success")
    return RedirectResponse(f"/recruiter/applications/{app.id}#plan", status_code=303)


@router.post("/applications/{application_id}/plan/regenerate")
def application_plan_regenerate(request: Request, application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    regenerate_plan(db, app, get_provider(), user)
    flash(request, f"Plan rebuilt from rubric v{app.job.rubric_version}.", "success")
    return RedirectResponse(f"/recruiter/applications/{app.id}#plan", status_code=303)


@router.post("/applications/{application_id}/reanalyse")
def application_reanalyse(request: Request, application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    analyse(db, app, get_provider())
    audit.record(db, "application.reanalysed", "application", app.id, actor_type="user", actor_id=user.id)
    db.commit()
    flash(request, "Resume re-analysed and plan rebuilt.", "success")
    return RedirectResponse(f"/recruiter/applications/{app.id}", status_code=303)


# --------------------------------------------------------------------------- invitations


@router.get("/applications/{application_id}/invite")
def invite_page(request: Request, application_id: int, db: Session = Depends(get_db)):
    app = _app(db, application_id)
    return render(
        request, "recruiter/invite.html",
        {
            "app": app, "row": row_view(app), "active_inv": active_invitation(app), "tab": "invitations", "nav": "applications",
            "default_days": app.job.invite_valid_days, "dev_link": request.session.pop("dev_link", None),
        },
    )


@router.get("/applications/{application_id}/invite/preview")
def invite_preview(application_id: int, days: int | None = None, note: str = "", db: Session = Depends(get_db)):
    from datetime import timedelta

    app = _app(db, application_id)
    fake = Invitation(application_id=app.id, token_hash="", expires_at=utcnow() + timedelta(days=max(1, min(30, days or app.job.invite_valid_days))))
    msg = build_invitation_email(app, fake, f"{settings().base_url.rstrip('/')}/interview/[link-created-when-you-send]", note)
    # Email HTML relies on inline styles; serve it with a CSP that allows them but no scripts.
    return HTMLResponse(
        msg.html,
        headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'", "X-Frame-Options": "SAMEORIGIN"},
    )


@router.post("/applications/{application_id}/invite")
def invite_send(
    request: Request,
    application_id: int,
    note: str = Form(""),
    valid_days: int = Form(5),
    replace: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    app = _app(db, application_id)
    try:
        inv, token, record = issue_invitation(db, app, user, note=note, valid_days=max(1, min(30, valid_days)), replace=bool(replace))
    except InvitationError as exc:
        flash(request, exc.message, "error")
        return RedirectResponse(f"/recruiter/applications/{app.id}/invite", status_code=303)
    from app.services.applications import interview_link

    if record.status == "sent":
        flash(request, f"Invitation emailed to {app.candidate.email}.", "success")
    elif record.status == "logged_not_sent":
        request.session["dev_link"] = interview_link(token)
        flash(request, "Dry-run mode: the link was created and the email logged, but nothing was sent.", "warning")
    else:
        flash(request, f"The link was created but the email failed: {record.error} Fix the problem and use “Send a new link”.", "error")
    return RedirectResponse(f"/recruiter/applications/{app.id}/invite", status_code=303)


@router.post("/invitations/{invitation_id}/revoke")
def invite_revoke(request: Request, invitation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    inv = db.get(Invitation, invitation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    revoke_invitation(db, inv, user)
    flash(request, "Invitation revoked. The link no longer works.", "success")
    return RedirectResponse(f"/recruiter/applications/{inv.application_id}/invite", status_code=303)


# --------------------------------------------------------------------------- transcript, report, decision


def _session_or_404(app: Application) -> InterviewSession:
    sess = app.latest_session
    if sess is None:
        raise HTTPException(status_code=404, detail="This candidate hasn't started an interview yet.")
    return sess


@router.get("/applications/{application_id}/transcript")
def transcript(request: Request, application_id: int, db: Session = Depends(get_db)):
    app = _app(db, application_id)
    sess = _session_or_404(app)
    msgs = [{"m": m, "meta": message_meta(sess, m)} for m in sess.messages]
    return render(request, "recruiter/transcript.html", {"app": app, "row": row_view(app), "isession": sess, "msgs": msgs, "tab": "transcript", "nav": "applications"})


@router.get("/applications/{application_id}/report")
def report_page(request: Request, application_id: int, db: Session = Depends(get_db)):
    app = _app(db, application_id)
    sess = _session_or_404(app)
    report = sess.latest_report
    data = report.report_json if report and report.status == "ready" else None
    plan_qs = {f"Q{q['index'] + 1}": q for q in sess.plan_json.get("questions", [])}
    answers: dict[str, list[str]] = {}
    for m in sess.messages:
        if m.role == "candidate" and m.kind == "answer" and m.question_index is not None:
            answers.setdefault(f"Q{m.question_index + 1}", []).append(m.content)
    crit_names = {c["id"]: c for c in sess.rubric_snapshot_json.get("essential_criteria", [])}
    resume_lines = {ln["ref"]: ln["text"] for ln in app.parsed_resume_json.get("lines", [])}
    return render(
        request, "recruiter/report.html",
        {
            "app": app, "row": row_view(app), "isession": sess, "report": report, "data": data, "plan_qs": plan_qs,
            "answers": answers, "crit_names": crit_names, "resume_lines": resume_lines, "tab": "report", "nav": "applications",
            "decision": app.latest_decision, "print": request.query_params.get("print") == "1",
        },
    )


@router.post("/applications/{application_id}/report/generate")
def report_generate(request: Request, application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    sess = _session_or_404(app)
    if sess.status != "completed":
        flash(request, "The interview isn't finished yet.", "error")
        return RedirectResponse(f"/recruiter/applications/{app.id}/report", status_code=303)
    report = queue_report(db, sess)
    if report.status == "running":
        flash(request, "The report is already being generated. Refresh in a moment.", "info")
        return RedirectResponse(f"/recruiter/applications/{app.id}/report", status_code=303)
    db.commit()
    audit.record(db, "report.requested", "evaluation_report", report.id, actor_type="user", actor_id=user.id)
    generate(db, report)
    if report.status == "ready":
        flash(request, "Report generated.", "success")
    else:
        flash(request, f"Report generation failed: {report.error} You can retry.", "error")
    return RedirectResponse(f"/recruiter/applications/{app.id}/report", status_code=303)


@router.get("/applications/{application_id}/report.json")
def report_json(application_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    sess = _session_or_404(app)
    report = sess.latest_report
    if not report or report.status != "ready":
        raise HTTPException(status_code=404, detail="No completed report yet.")
    dec = app.latest_decision
    body = {
        "application_id": app.id,
        "candidate": {"name": app.candidate.name, "email": app.candidate.email},
        "job": {"id": app.job.id, "title": app.job.title},
        "interview": {"reference": sess.reference_code, "completed_at": sess.completed_at.isoformat() if sess.completed_at else None, "rubric_version": sess.rubric_version, "transcript_sha256": sess.transcript_hash},
        "ai_report_advisory": report.report_json,
        "human_decision": {"decision": dec.decision, "notes": dec.notes, "recorded_at": dec.created_at.isoformat()} if dec else None,
    }
    audit.record(db, "report.exported", "evaluation_report", report.id, actor_type="user", actor_id=user.id)
    db.commit()
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="screening-report-{app.id}.json"'})


@router.post("/applications/{application_id}/decision")
def decision(request: Request, application_id: int, decision: str = Form(...), notes: str = Form(""), return_to: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    try:
        record_decision(db, app, decision, notes, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Choose a decision.") from exc
    flash(request, "Decision saved and logged.", "success")
    target = f"/recruiter/applications/{app.id}/report" if return_to == "report" else f"/recruiter/applications/{app.id}"
    return RedirectResponse(target + "#decision", status_code=303)


@router.post("/applications/{application_id}/delete")
def application_delete(request: Request, application_id: int, confirm: str = Form(""), db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = _app(db, application_id)
    if confirm.strip().upper() != "DELETE":
        flash(request, "Type DELETE to confirm removal of this candidate's data.", "error")
        return RedirectResponse(f"/recruiter/applications/{app.id}#danger", status_code=303)
    delete_application_data(db, app, "user", user.id, "deleted by recruiter")
    flash(request, "Candidate data deleted: resume, transcript, report and emails.", "success")
    return RedirectResponse("/recruiter", status_code=303)


# --------------------------------------------------------------------------- outbox, audit, users, account


@router.get("/outbox")
def outbox(request: Request, db: Session = Depends(get_db)):
    emails = db.scalars(select(EmailMessage).order_by(EmailMessage.id.desc()).limit(100)).all()
    return render(request, "recruiter/outbox.html", {"emails": emails, "nav": "outbox"})


@router.get("/audit")
def audit_log(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(300)).all()
    users = {u.id: u for u in db.scalars(select(User)).all()}
    return render(request, "recruiter/audit.html", {"events": events, "users": users, "nav": "audit", "json": json})


@router.get("/users")
def users_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    users = db.scalars(select(User).order_by(User.created_at)).all()
    return render(request, "recruiter/users.html", {"users": users, "nav": "users", "form": {}, "errors": {}})


@router.post("/users")
def users_create(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    role: str = Form("recruiter"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    errors: dict[str, str] = {}
    if len(name.strip()) < 2:
        errors["name"] = "Enter a name."
    if "@" not in email:
        errors["email"] = "Enter a valid email."
    elif db.scalar(select(User).where(func.lower(User.email) == email.strip().lower())):
        errors["email"] = "A user with this email already exists."
    pw_err = validate_new_password(password)
    if pw_err:
        errors["password"] = pw_err
    if errors:
        users = db.scalars(select(User).order_by(User.created_at)).all()
        return render(request, "recruiter/users.html", {"users": users, "nav": "users", "form": {"name": name, "email": email, "role": role}, "errors": errors}, status_code=422)
    u = User(name=name.strip(), email=email.strip().lower(), password_hash=hash_password(password), role="admin" if role == "admin" else "recruiter")
    db.add(u)
    db.flush()
    audit.record(db, "user.created", "user", u.id, actor_type="user", actor_id=admin.id, metadata={"role": u.role})
    db.commit()
    flash(request, f"Account created for {u.email}. Share the password with them securely.", "success")
    return RedirectResponse("/recruiter/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def users_toggle(request: Request, user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404)
    if u.id == admin.id:
        flash(request, "You can't deactivate your own account.", "error")
    else:
        u.is_active = not u.is_active
        audit.record(db, "user.activated" if u.is_active else "user.deactivated", "user", u.id, actor_type="user", actor_id=admin.id)
        db.commit()
        flash(request, f"{u.email} is now {'active' if u.is_active else 'deactivated'}.", "success")
    return RedirectResponse("/recruiter/users", status_code=303)


@router.get("/account")
def account(request: Request):
    return render(request, "recruiter/account.html", {"nav": "account", "errors": {}})


@router.post("/account/password")
def account_password(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    errors: dict[str, str] = {}
    if not verify_password(user.password_hash, current):
        errors["current"] = "That isn't your current password."
    pw_err = validate_new_password(new)
    if pw_err:
        errors["new"] = pw_err
    elif new != confirm:
        errors["confirm"] = "The passwords don't match."
    if errors:
        return render(request, "recruiter/account.html", {"nav": "account", "errors": errors}, status_code=422)
    user.password_hash = hash_password(new)
    audit.record(db, "user.password_changed", "user", user.id, actor_type="user", actor_id=user.id)
    db.commit()
    flash(request, "Password updated.", "success")
    return RedirectResponse("/recruiter/account", status_code=303)
