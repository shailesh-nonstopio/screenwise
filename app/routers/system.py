"""Health checks and scheduled maintenance (Vercel Cron)."""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.services.applications import purge_expired
from app.services.evaluation import process_pending

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    s = settings()
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "database": ok, "llm_provider": s.llm_provider, "email_backend": s.email_backend},
        status_code=200 if ok else 503,
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> RedirectResponse:
    return RedirectResponse("/static/favicon.svg", status_code=307)


@router.get("/api/cron/maintenance", include_in_schema=False)
def maintenance(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    secret = settings().cron_secret
    supplied = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(supplied, f"Bearer {secret}"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    purged = purge_expired(db)
    reports = process_pending(db, limit=1)
    log.info("maintenance run", extra={"safe": {"reports": reports, "purged": purged}})
    return JSONResponse({"reports_processed": reports, "applications_purged": purged})
