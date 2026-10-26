"""Public careers pages: open jobs, application form, privacy notice."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Job
from app.security import rate_limit, verify_csrf
from app.services.applications import DuplicateApplication, create_application, validate_candidate_fields
from app.services.llm_provider import LLMError, get_provider
from app.services.resume_parser import ExtractionError, UploadError
from app.templating import render

router = APIRouter()


def _open_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.status != "open":
        raise HTTPException(status_code=404, detail="This job isn't open for applications.")
    return job


@router.get("/jobs")
def jobs(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Job).where(Job.status == "open").order_by(Job.created_at.desc())).all()
    return render(request, "candidate/jobs.html", {"jobs": rows})


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = _open_job(db, job_id)
    return render(request, "candidate/apply.html", {"job": job, "form": {}, "errors": {}})


async def read_upload(file: UploadFile | None) -> tuple[str, bytes]:
    if file is None or not file.filename:
        return "", b""
    limit = settings().max_upload_bytes
    content = await file.read(limit + 1)
    return file.filename, content


@router.post("/jobs/{job_id}/apply", dependencies=[Depends(verify_csrf), Depends(rate_limit("apply", 10))])
async def apply(
    request: Request,
    job_id: int,
    name: str = Form(""),
    email: str = Form(""),
    consent_privacy: str = Form(""),
    consent_ai: str = Form(""),
    resume: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    job = _open_job(db, job_id)
    form = {"name": name, "email": email, "consent_privacy": consent_privacy, "consent_ai": consent_ai}
    errors = validate_candidate_fields(name, email)
    if not consent_privacy:
        errors["consent_privacy"] = "Please confirm you have read the privacy notice."
    if not consent_ai:
        errors["consent_ai"] = "Please confirm you understand how AI is used in screening."
    filename, content = await read_upload(resume)
    if not content:
        errors["resume"] = "Choose your resume file (PDF or DOCX)."
    if errors:
        return render(request, "candidate/apply.html", {"job": job, "form": form, "errors": errors}, status_code=422)
    try:
        create_application(db, job, name=name, email=email, filename=filename, content=content, provider=get_provider(), source="public", consented=True)
    except (UploadError, ExtractionError) as exc:
        errors["resume"] = str(exc)
        form["bad_file"] = filename
    except DuplicateApplication:
        errors["email"] = "You've already applied for this job with this email address. We'll be in touch."
    except LLMError:
        errors["form"] = "We couldn't process your application just now. Please try again in a few minutes."
    if errors:
        return render(request, "candidate/apply.html", {"job": job, "form": form, "errors": errors}, status_code=422)
    return RedirectResponse(f"/jobs/{job.id}/applied", status_code=303)


@router.get("/jobs/{job_id}/applied")
def applied(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return render(request, "candidate/applied.html", {"job": job})


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "candidate/privacy.html", {})
