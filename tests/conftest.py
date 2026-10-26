"""Shared fixtures. Every test runs against a fresh SQLite database built by the real migrations."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.database import db_session, reset_engine
from app.demo import JOBS, make_docx, job_form_data
from app.models import Application, Job, User
from app.security import hash_password, limiter
from app.services.interview_planner import slug

PASSWORD = "correct-horse-battery-staple"

RESUME_LINES = [
    "ALEX TESTER",
    "Backend Engineer — Northwind Pay, Mar 2021 – present",
    "Built the payment ledger service API in FastAPI and PostgreSQL.",
    "Mentor to two junior engineers; run weekly code review sessions.",
    "Software Engineer — Brightline Logistics, early 2019 – 2021",
    "Django REST APIs; on-call rotation.",
    "SKILLS",
    "Python, FastAPI, PostgreSQL, Celery, AWS",
    "EDUCATION",
    "B.E. Computer Engineering, University of Pune",
]


TEST_PG = os.environ.get("TEST_DATABASE_URL")  # e.g. postgresql://postgres@localhost:5433/sw


def _reset_postgres(url: str) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(Settings(_env_file=None, database_url=url).database_url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()


@pytest.fixture(autouse=True)
def test_settings(tmp_path) -> Iterator[Settings]:
    if TEST_PG:
        _reset_postgres(TEST_PG)
    s = Settings(
        _env_file=None,
        app_env="test",
        secret_key="test-secret-key-that-is-long-enough-123456",
        database_url=TEST_PG or f"sqlite:///{tmp_path / 'test.db'}",
        base_url="http://testserver",
        llm_provider="mock",
        email_backend="dryrun",
        report_generation="inline",
        storage_backend="database",
        rate_limit_per_minute=1000,
    )
    config.settings_override = s
    reset_engine()
    limiter.reset()
    from app.services.bootstrap import run_migrations

    run_migrations()
    yield s
    config.settings_override = None
    reset_engine()


@pytest.fixture
def client(test_settings) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c


def make_user(email: str = "recruiter@example.com", role: str = "recruiter", active: bool = True) -> int:
    with db_session() as db:
        u = User(email=email, name="Rae Cruiter", password_hash=hash_password(PASSWORD), role=role, is_active=active)
        db.add(u)
        db.commit()
        return u.id


def csrf(client: TestClient, path: str = "/login") -> str:
    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def login(client: TestClient, email: str = "recruiter@example.com") -> str:
    token = csrf(client, "/login")
    r = client.post("/login", data={"email": email, "password": PASSWORD, "csrf_token": token, "next": "/recruiter"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return csrf(client, "/recruiter")


@pytest.fixture
def recruiter(client) -> dict:
    uid = make_user()
    token = login(client)
    return {"id": uid, "csrf": token}


@pytest.fixture
def admin_user(client) -> dict:
    uid = make_user("admin@example.com", role="admin")
    return {"id": uid}


def create_job(status: str = "open", spec_index: int = 0) -> int:
    from app.services.jobs import save_job
    from app.services.llm_provider import get_provider

    data = job_form_data(JOBS[spec_index])
    used: set[str] = set()
    for c in data["essential_criteria"]:
        c["id"] = slug(c["name"], used)
    with db_session() as db:
        user = db.query(User).first()
        if user is None:
            user = User(email="owner@example.com", name="Owner", password_hash=hash_password(PASSWORD), role="admin")
            db.add(user)
            db.commit()
        job = save_job(db, None, data, user, get_provider())
        job.status = status
        db.commit()
        return job.id


@pytest.fixture
def job_id(client) -> int:
    return create_job()


def create_application(job_id: int, name: str = "Alex Tester", email: str = "alex@example.com", lines=None) -> int:
    from app.services.applications import create_application as _create
    from app.services.llm_provider import get_provider

    with db_session() as db:
        job = db.get(Job, job_id)
        app = _create(db, job, name=name, email=email, filename="resume.docx", content=make_docx(lines or RESUME_LINES), provider=get_provider())
        return app.id


@pytest.fixture
def application_id(job_id) -> int:
    return create_application(job_id)


def get_app(app_id: int) -> Application:
    with db_session() as db:
        a = db.get(Application, app_id)
        db.expunge_all()
        return a
