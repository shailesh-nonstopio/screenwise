"""Jinja2 environment shared by pages and emails."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.config import settings
from app.models import as_utc
from app.schemas import DECISION_LABELS, RECOMMENDATION_LABELS
from app.security import csrf_token
from app.services.interview_planner import CATEGORY_LABELS, SENIORITY_LABELS

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings().display_timezone)
    except Exception:  # pragma: no cover
        return ZoneInfo("UTC")


def fmt_dt(dt: datetime | None, fmt: str = "%b %d, %Y, %H:%M") -> str:
    if dt is None:
        return "—"
    local = as_utc(dt).astimezone(_tz())  # type: ignore[union-attr]
    return f"{local.strftime(fmt)} {local.tzname()}"


def fmt_date(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return as_utc(dt).astimezone(_tz()).strftime("%b %d")  # type: ignore[union-attr]


def ref_links(refs: list[str] | None) -> Markup:
    out = []
    for r in refs or []:
        r = str(escape(r))
        anchor = r.lower().replace("-", "")
        out.append(f'<a class="ref" href="#{anchor}">{r}</a>')
    return Markup(" ".join(out))


def rating_label(item: dict[str, Any]) -> str:
    if item.get("status") == "not_assessed":
        return "Not assessed"
    if item.get("status") == "insufficient_evidence" or not item.get("rating"):
        return "Insufficient evidence"
    return {1: "1 · No relevant evidence", 2: "2 · Partial", 3: "3 · Meets", 4: "4 · Strong"}[int(item["rating"])]


env = templates.env
env.filters["dt"] = fmt_dt
env.filters["date"] = fmt_date
env.filters["refs"] = ref_links
env.filters["rating_label"] = rating_label
env.globals.update(
    settings=settings,
    CATEGORY_LABELS=CATEGORY_LABELS,
    SENIORITY_LABELS=SENIORITY_LABELS,
    DECISION_LABELS=DECISION_LABELS,
    RECOMMENDATION_LABELS=RECOMMENDATION_LABELS,
)


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200):
    ctx = dict(context or {})
    ctx.setdefault("user", getattr(request.state, "user", None))
    has_session = "session" in request.scope
    ctx["csrf_token"] = csrf_token(request) if has_session else ""
    ctx["flashes"] = request.session.pop("flashes", []) if has_session else []
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, kind: str = "info") -> None:
    flashes = request.session.get("flashes", [])
    flashes.append({"kind": kind, "message": message})
    request.session["flashes"] = flashes[-5:]


def render_email(name: str, context: dict[str, Any]) -> str:
    return env.get_template(name).render(**context)
