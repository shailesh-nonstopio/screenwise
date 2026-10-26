"""Recruiter sign-in and sign-out."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, utcnow
from app.security import (
    burn_password_check,
    current_user_optional,
    limiter,
    client_ip,
    login_session,
    verify_csrf,
    verify_password,
)
from app.services import audit
from app.templating import render

router = APIRouter()


def _safe_next(nxt: str | None) -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return "/recruiter"


@router.get("/")
def home(user: User | None = Depends(current_user_optional)):
    return RedirectResponse("/recruiter" if user else "/login", status_code=303)


@router.get("/login")
def login_page(request: Request, next: str | None = None, user: User | None = Depends(current_user_optional)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next), "email": ""})


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/recruiter"),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    email_n = email.strip().lower()
    if not limiter.hit(f"login:{ip}", 10, 60) or not limiter.hit(f"login-email:{email_n}", 10, 300):
        return render(request, "login.html", {"next": _safe_next(next), "email": email, "error": "Too many attempts. Wait a few minutes and try again."}, status_code=429)
    user = db.scalar(select(User).where(func.lower(User.email) == email_n))
    if user is None:
        burn_password_check()
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        audit.record(db, "auth.login_failed", "user", user.id if user else "-", actor_type="anonymous", metadata={"ip": ip})
        db.commit()
        return render(request, "login.html", {"next": _safe_next(next), "email": email, "error": "That email and password don't match an active account."}, status_code=401)
    login_session(request, user)
    user.last_login_at = utcnow()
    audit.record(db, "auth.login", "user", user.id, actor_type="user", actor_id=user.id)
    db.commit()
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
