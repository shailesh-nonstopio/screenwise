"""Candidate interview routes.

The emailed link ``/interview/{token}`` is exchanged for a session cookie on first use and the
browser is redirected to ``/interview``, so the token does not stay in the address bar, browser
history or later request logs. Every request re-checks that the invitation is still valid.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Application, InterviewSession, Invitation, utcnow
from app.security import hash_token, rate_limit, verify_csrf
from app.services import audit
from app.services.evaluation import generate_in_background
from app.services.interview_engine import (
    InterviewError,
    complete,
    public_state,
    request_clarification,
    start_session,
    submit_answer,
)
from app.services.llm_provider import get_provider
from app.templating import render

router = APIRouter(prefix="/interview", dependencies=[Depends(rate_limit("interview", 60))])

SESSION_KEY = "interview"


class LinkState(Exception):
    def __init__(self, state: str, app: Application | None = None, session: InterviewSession | None = None) -> None:
        self.state = state
        self.app = app
        self.session = session


def _state_page(request: Request, state: LinkState) -> Response:
    status = {"not_found": 404, "revoked": 410, "expired": 410, "submitted": 200}.get(state.state, 400)
    return render(request, "candidate/link_state.html", {"state": state.state, "app": state.app, "isession": state.session}, status_code=status)


def _is_json(request: Request) -> bool:
    return request.headers.get("content-type", "").startswith("application/json") or "application/json" in request.headers.get("accept", "")


def _context(request: Request, db: Session) -> tuple[Invitation, Application, InterviewSession | None]:
    data = request.session.get(SESSION_KEY) or {}
    inv = db.get(Invitation, data.get("id")) if data.get("id") else None
    if inv is None or inv.token_hash != data.get("h"):
        raise LinkState("not_found")
    app = inv.application
    session = app.latest_session
    if session and session.status == "completed":
        raise LinkState("submitted", app, session)
    if inv.is_revoked:
        raise LinkState("revoked", app)
    if inv.is_expired:
        raise LinkState("expired", app)
    return inv, app, session


def _guard(request: Request, db: Session):
    try:
        return _context(request, db), None
    except LinkState as ls:
        if _is_json(request):
            msg = {
                "not_found": "Your interview session wasn't found. Reopen the link from your email.",
                "revoked": "This interview link is no longer valid.",
                "expired": "This interview link has expired.",
                "submitted": "This interview has already been submitted.",
            }[ls.state]
            return None, JSONResponse({"error": msg, "state": ls.state}, status_code=409 if ls.state == "submitted" else 410)
        return None, _state_page(request, ls)


@router.get("")
def welcome(request: Request, db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    inv, app, session = ctx
    if session and session.status == "in_progress":
        return RedirectResponse("/interview/chat", status_code=303)
    return render(request, "candidate/welcome.html", {"app": app, "inv": inv, "question_count": len(app.plan_json.get("questions", []))})


@router.post("/start", dependencies=[Depends(verify_csrf)])
def start(request: Request, acknowledge: str = Form(""), db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    inv, app, session = ctx
    if session and session.status == "in_progress":
        return RedirectResponse("/interview/chat", status_code=303)
    if not acknowledge:
        return render(
            request, "candidate/welcome.html",
            {"app": app, "inv": inv, "question_count": len(app.plan_json.get("questions", [])), "error": "Please tick the box to confirm you've read the instructions and privacy information."},
            status_code=422,
        )
    try:
        start_session(db, app, inv)
    except InterviewError as exc:
        return render(request, "error.html", {"title": "Interview not ready", "detail": exc.message, "code": exc.status}, status_code=exc.status)
    return RedirectResponse("/interview/chat", status_code=303)


@router.get("/chat")
def chat(request: Request, db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    inv, app, session = ctx
    if not session:
        return RedirectResponse("/interview", status_code=303)
    state = public_state(session)
    return render(
        request, "candidate/chat.html",
        {
            "app": app, "inv": inv, "isession": session, "state": state, "state_json": json.dumps(state).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
            "max_chars": settings().max_answer_chars, "error": request.session.pop("interview_error", None),
        },
    )


@router.get("/state")
def state(request: Request, db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    _, _, session = ctx
    if not session:
        return JSONResponse({"error": "Interview not started."}, status_code=409)
    return JSONResponse(public_state(session))


async def _payload(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}
    form = await request.form()
    return {k: form.get(k) for k in ("text", "client_msg_id", "csrf_token", "prompt_seq")}


def _respond(request: Request, session: InterviewSession, error: InterviewError | None = None) -> Response:
    if _is_json(request):
        body = public_state(session)
        if error:
            body["error"] = error.message
        return JSONResponse(body, status_code=error.status if error else 200)
    if error:
        request.session["interview_error"] = error.message
    return RedirectResponse("/interview/chat", status_code=303)


@router.post("/answer", dependencies=[Depends(verify_csrf)])
async def answer(request: Request, db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    _, _, session = ctx
    if not session:
        return RedirectResponse("/interview", status_code=303)
    data = await _payload(request)
    try:
        try:
            expected = int(data["prompt_seq"]) if data.get("prompt_seq") not in (None, "") else None
        except (TypeError, ValueError):
            expected = None
        submit_answer(db, session, str(data.get("text") or ""), str(data.get("client_msg_id") or "") or None, get_provider(), expected)
    except InterviewError as exc:
        db.refresh(session)
        return _respond(request, session, exc)
    return _respond(request, session)


@router.post("/clarify", dependencies=[Depends(verify_csrf)])
async def clarify(request: Request, db: Session = Depends(get_db)):
    ctx, err = _guard(request, db)
    if err:
        return err
    _, _, session = ctx
    if not session:
        return RedirectResponse("/interview", status_code=303)
    data = await _payload(request)
    try:
        # The draft answer box is never sent as a clarification request.
        request_clarification(db, session, None, str(data.get("client_msg_id") or "") or None, get_provider())
    except InterviewError as exc:
        db.refresh(session)
        return _respond(request, session, exc)
    return _respond(request, session)


@router.post("/complete", dependencies=[Depends(verify_csrf)])
def finish(request: Request, background: BackgroundTasks, end_early: str = Form(""), db: Session = Depends(get_db)):
    try:
        inv, app, session = _context(request, db)
    except LinkState as ls:
        if ls.state == "submitted":
            return RedirectResponse("/interview/done", status_code=303)
        return _state_page(request, ls)
    if not session:
        return RedirectResponse("/interview", status_code=303)
    all_answered = session.current_index >= len(session.plan_json.get("questions", []))
    if not all_answered and not end_early:
        request.session["interview_error"] = "Please answer the remaining questions, or choose “End interview early”."
        return RedirectResponse("/interview/chat", status_code=303)
    if complete(db, session, ended_early=not all_answered):
        report = session.latest_report
        mode = settings().report_generation
        if report and mode == "background":
            background.add_task(generate_in_background, report.id)
        elif report and mode == "inline":
            generate_in_background(report.id)
    return RedirectResponse("/interview/done", status_code=303)


@router.get("/done")
def done(request: Request, db: Session = Depends(get_db)):
    data = request.session.get(SESSION_KEY) or {}
    inv = db.get(Invitation, data.get("id")) if data.get("id") else None
    if inv is None or inv.token_hash != data.get("h"):
        return _state_page(request, LinkState("not_found"))
    session = inv.application.latest_session
    if not session or session.status != "completed":
        return RedirectResponse("/interview", status_code=303)
    return render(request, "candidate/done.html", {"app": inv.application, "isession": session})


@router.get("/{token}")
def open_link(request: Request, token: str, db: Session = Depends(get_db)):
    """Exchange the emailed token for a session cookie, then redirect to a token-free URL."""
    if len(token) < 20 or len(token) > 100:
        return _state_page(request, LinkState("not_found"))
    th = hash_token(token)
    inv = db.scalar(select(Invitation).where(Invitation.token_hash == th))
    if inv is None:
        return _state_page(request, LinkState("not_found"))
    request.session[SESSION_KEY] = {"id": inv.id, "h": th}
    if inv.opened_at is None and inv.is_active:
        inv.opened_at = utcnow()
        audit.record(db, "invitation.opened", "invitation", inv.id, actor_type="candidate", actor_id=inv.application.candidate_id)
        db.commit()
    return RedirectResponse("/interview", status_code=303)
