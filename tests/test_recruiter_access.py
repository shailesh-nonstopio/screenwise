import pytest

from app.database import db_session
from app.models import Application, AuditEvent, Job, StoredFile
from app.demo import make_docx
from tests.conftest import PASSWORD, RESUME_LINES, csrf, login, make_user

RECRUITER_GETS = [
    "/recruiter", "/recruiter/jobs", "/recruiter/jobs/new", "/recruiter/outbox", "/recruiter/account",
    "/recruiter/applications/1", "/recruiter/applications/1/report", "/recruiter/applications/1/invite",
]


@pytest.mark.parametrize("path", RECRUITER_GETS)
def test_recruiter_pages_require_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_rejects_bad_password_and_inactive_users(client):
    make_user("off@example.com", active=False)
    make_user()
    tok = csrf(client)
    r = client.post("/login", data={"email": "recruiter@example.com", "password": "wrong", "csrf_token": tok})
    assert r.status_code == 401 and "match an active account" in r.text
    r = client.post("/login", data={"email": "off@example.com", "password": PASSWORD, "csrf_token": tok})
    assert r.status_code == 401


def test_login_requires_csrf(client):
    make_user()
    r = client.post("/login", data={"email": "recruiter@example.com", "password": PASSWORD})
    assert r.status_code == 403


def test_open_redirect_is_blocked(client):
    make_user()
    tok = csrf(client)
    r = client.post("/login", data={"email": "recruiter@example.com", "password": PASSWORD, "csrf_token": tok, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/recruiter"


def test_recruiter_cannot_use_admin_pages(client, recruiter):
    assert client.get("/recruiter/audit").status_code == 403
    assert client.get("/recruiter/users").status_code == 403


def test_admin_can_use_admin_pages(client):
    make_user("admin@example.com", role="admin")
    login(client, "admin@example.com")
    assert client.get("/recruiter/audit").status_code == 200
    assert client.get("/recruiter/users").status_code == 200


def test_state_changes_require_csrf(client, recruiter, job_id):
    r = client.post(f"/recruiter/jobs/{job_id}/status", data={"status": "closed"})
    assert r.status_code == 403
    r = client.post(f"/recruiter/jobs/{job_id}/status", data={"status": "closed", "csrf_token": recruiter["csrf"]}, follow_redirects=False)
    assert r.status_code == 303
    with db_session() as db:
        assert db.get(Job, job_id).status == "closed"


def test_recruiter_creates_job_with_criteria_and_rubric(client, recruiter):
    form = {
        "csrf_token": recruiter["csrf"],
        "title": "Support Lead",
        "seniority": "lead",
        "description": "Lead a team of six support specialists and own escalations for enterprise customers.",
        "criteria-0-name": "Customer escalation handling",
        "criteria-0-description": "Resolves complex customer issues",
        "criteria-0-a1": "No evidence", "criteria-0-a2": "Basic", "criteria-0-a3": "Meets", "criteria-0-a4": "Strong",
        "criteria-1-name": "Coaching support specialists",
        "behavior-collaboration-enabled": "1",
        "band-0-label": "0–3 yrs", "band-0-min": "0", "band-0-max": "3", "band-0-focus": "Basics",
        "band-1-label": "3+ yrs", "band-1-min": "3", "band-1-max": "99", "band-1-focus": "Leadership",
        "max_followups": "2", "estimated_minutes": "20", "invite_valid_days": "7",
    }
    r = client.post("/recruiter/jobs/new", data=form, follow_redirects=False)
    assert r.status_code == 303
    with db_session() as db:
        job = db.query(Job).filter_by(title="Support Lead").one()
        assert [c["name"] for c in job.essential_criteria] == ["Customer escalation handling", "Coaching support specialists"]
        assert job.rubric_json["anchors"]["customer_escalation_handling"]["4"] == "Strong"
        assert job.core_questions_json and job.status == "draft" and job.rubric_version == 1
        jid = job.id
    # Editing criteria bumps the rubric version.
    form["criteria-1-name"] = "Coaching and hiring"
    form["csrf_token"] = csrf(client, f"/recruiter/jobs/{jid}/edit")
    client.post(f"/recruiter/jobs/{jid}/edit", data=form)
    with db_session() as db:
        assert db.get(Job, jid).rubric_version == 2


def test_job_form_validation_errors(client, recruiter):
    r = client.post("/recruiter/jobs/new", data={"csrf_token": recruiter["csrf"], "title": "", "description": "short"})
    assert r.status_code == 422 and "Enter a job title" in r.text and "at least one essential criterion" in r.text


def test_public_application_and_duplicate(client, job_id):
    tok = csrf(client, f"/jobs/{job_id}")
    files = {"resume": ("cv.docx", make_docx(RESUME_LINES), "application/octet-stream")}
    data = {"csrf_token": tok, "name": "Pat Applicant", "email": "pat@example.com", "consent_privacy": "1", "consent_ai": "1"}
    r = client.post(f"/jobs/{job_id}/apply", data=data, files=files, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/jobs/{job_id}/apply", data=data, files={"resume": ("cv.docx", make_docx(RESUME_LINES), "application/octet-stream")})
    assert r.status_code == 422 and "already applied" in r.text
    r = client.post(f"/jobs/{job_id}/apply", data=data | {"email": "new@example.com"}, files={"resume": ("cv.exe", b"MZbinary", "application/octet-stream")})
    assert r.status_code == 422 and "Only PDF and DOCX" in r.text
    r = client.post(f"/jobs/{job_id}/apply", data=data | {"email": "x@example.com", "consent_ai": ""}, files={"resume": ("cv.docx", make_docx(RESUME_LINES), "application/octet-stream")})
    assert r.status_code == 422 and "AI is used" in r.text


def test_closed_job_does_not_accept_applications(client, job_id):
    with db_session() as db:
        db.get(Job, job_id).status = "closed"
        db.commit()
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_delete_candidate_data(client, recruiter, application_id):
    r = client.post(f"/recruiter/applications/{application_id}/delete", data={"csrf_token": recruiter["csrf"], "confirm": "nope"}, follow_redirects=False)
    with db_session() as db:
        assert db.get(Application, application_id) is not None
    client.post(f"/recruiter/applications/{application_id}/delete", data={"csrf_token": recruiter["csrf"], "confirm": "DELETE"})
    with db_session() as db:
        assert db.get(Application, application_id) is None
        assert db.query(StoredFile).count() == 0
        ev = db.query(AuditEvent).filter_by(action="application.deleted").one()
        assert "alex" not in str(ev.safe_metadata).lower()


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "camera=()" in r.headers["permissions-policy"]


def test_cron_requires_secret(client, test_settings):
    assert client.get("/api/cron/maintenance").status_code == 401
    test_settings.cron_secret = "s3cret"
    assert client.get("/api/cron/maintenance", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/api/cron/maintenance", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and "reports_processed" in r.json()


def test_production_refuses_insecure_config():
    from app.config import Settings

    s = Settings(_env_file=None, app_env="production", base_url="http://x.example")
    problems = s.startup_problems()
    assert any("SECRET_KEY" in p for p in problems) and any("https" in p for p in problems)


def test_vercel_without_app_env_still_enforces_secure_config(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("VERCEL", "1")
    problems = Settings(_env_file=None).startup_problems()
    assert any("SECRET_KEY" in p for p in problems) and any("PostgreSQL" in p for p in problems)


def test_non_finite_band_values_are_ignored():
    from app.services.jobs import _float

    assert _float("nan", 0) == 0 and _float("inf", 99) == 99 and _float("2.5", 0) == 2.5


def test_misconfiguration_shows_setup_page_instead_of_crashing(monkeypatch):
    from fastapi.testclient import TestClient

    from app import config
    from app.config import Settings
    from app.main import create_app

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_URL", "example.vercel.app")
    config.settings_override = Settings(_env_file=None)
    assert config.settings_override.base_url == "https://example.vercel.app"
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 503 and "SECRET_KEY" in r.text and "DATABASE_URL" in r.text
        assert c.get("/healthz").json()["status"] == "misconfigured"
