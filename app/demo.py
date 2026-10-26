"""Synthetic demo data. All names, companies and resumes are fictional."""

from __future__ import annotations

import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, Job, User
from app.services.interview_planner import DEFAULT_ANCHORS, DEFAULT_BANDS, DEFAULT_BEHAVIORS

JOBS: list[dict[str, Any]] = [
    {
        "title": "Senior Backend Engineer (Python)",
        "department": "Engineering",
        "location": "Pune · Hybrid",
        "seniority": "senior",
        "description": (
            "You will design and run the services behind our payments platform: REST APIs in FastAPI, PostgreSQL "
            "data models, background workers and the observability around them. You will review code, mentor two "
            "engineers, and work with product on scope and trade-offs."
        ),
        "criteria": [
            ("Python API development", "Designs, builds and tests production REST APIs in FastAPI or similar"),
            ("Relational data modelling with PostgreSQL", "Schemas, indexing and safe migrations on live systems"),
            ("Reliability and debugging in production", "Observability, incident investigation, performance"),
            ("Technical ownership and trade-offs", "Leads decisions and explains constraints and outcomes"),
            ("Code review and mentoring", "Gives actionable feedback and grows other engineers"),
        ],
        "preferred": ["Payments or ledger systems", "Celery", "AWS", "Kafka"],
    },
    {
        "title": "Data Analyst",
        "department": "Analytics",
        "location": "Remote (India)",
        "seniority": "mid",
        "description": (
            "Turn product and revenue data into decisions. You will write SQL, clean data in Python, build dashboards "
            "that people actually use, analyse experiments and explain findings to non-technical colleagues."
        ),
        "criteria": [
            ("SQL querying and data modelling", "Joins, aggregation, window functions"),
            ("Data cleaning in Python", "pandas, data quality checks, documented assumptions"),
            ("Dashboards and BI tools", "Looker, Tableau or similar, with clear metric definitions"),
            ("Experiment analysis", "A/B test design, sample size and statistical reasoning"),
            ("Stakeholder communication", "Explains findings in terms of decisions"),
        ],
        "preferred": ["dbt", "BigQuery", "Forecasting"],
    },
]

RESUMES: list[dict[str, Any]] = [
    {
        "name": "Priya Raman",
        "email": "priya.raman@example.com",
        "job": 0,
        "lines": [
            "PRIYA RAMAN",
            "Backend engineer · priya.raman@example.com",
            "EXPERIENCE",
            "Backend Engineer II — Northwind Pay, Mar 2021 – present",
            "Built the payment ledger service API in FastAPI and PostgreSQL, handling about 2M transactions per day.",
            "Led migration of reconciliation jobs from cron to Celery; cut failed runs by 60%.",
            "Mentor to two junior engineers; run weekly code review sessions.",
            "Software Engineer — Brightline Logistics, early 2019 – 2021",
            "Django REST APIs for shipment tracking; on-call rotation for the tracking platform.",
            "Intern — Cobalt Labs, 2018 – 2018",
            "SKILLS",
            "Python, FastAPI, Django, PostgreSQL, Redis, Celery, Docker, AWS, Grafana",
            "EDUCATION",
            "B.E. Computer Engineering, University of Pune",
        ],
    },
    {
        "name": "Mei Lin Zhou",
        "email": "meilin.zhou@example.com",
        "job": 1,
        "lines": [
            "MEI LIN ZHOU",
            "Data analyst · meilin.zhou@example.com",
            "EXPERIENCE",
            "Data Analyst — Harbor Retail, Jun 2022 – present",
            "Built weekly retention reporting in SQL with window functions for 3 product lines.",
            "Led migration of dashboards from Tableau to Looker for the growth team.",
            "Analysed checkout A/B tests and presented results to product managers.",
            "Junior Analyst — Delta Insights, Jul 2020 – May 2022",
            "Python data pipelines with pandas; reduced manual reporting time by 40%.",
            "SKILLS",
            "SQL, Python, pandas, Looker, Tableau, Excel, Statistics",
            "EDUCATION",
            "B.Sc. Statistics, Delhi University, 2020",
        ],
    },
    {
        "name": "Daniel Okafor",
        "email": "daniel.okafor@example.com",
        "job": 0,
        "lines": [
            "DANIEL OKAFOR",
            "Software engineer · daniel.okafor@example.com",
            "EXPERIENCE",
            "Senior Software Engineer — Kestrel Health, Jan 2020 – present",
            "Designed REST APIs in Flask for appointment booking; added contract tests.",
            "Owned PostgreSQL schema changes and zero-downtime migrations for a 300M-row table.",
            "Software Engineer — Orbit Media, 2016 – 2019",
            "Java services and on-call incident response.",
            "SKILLS",
            "Python, Flask, Java, PostgreSQL, Kafka, Kubernetes",
            "EDUCATION",
            "B.Tech Information Technology, 2016",
        ],
    },
]

DEMO_ANSWERS = [
    'I owned the posting API and the double-entry data model. The main decision I made was to make entries append-only and derive balances from them instead of updating a balance column, because it made reconciliation and audits much simpler. We added a nightly snapshot so balance reads stayed under 50 ms.',
    'On one project I wrote the API endpoints for refunds in FastAPI. I defined Pydantic models for validation, returned consistent error codes, and wrote pytest tests for the edge cases such as partial refunds. I checked it worked with contract tests in CI and by watching error rates for a week after release.',
    'We needed to add a currency column to a table with about 400M rows. I decided to add it as nullable, backfill in batches of 50k rows at night, then add the constraint, because a single ALTER would have locked the table. As a result we shipped with no downtime.',
    'First I would confirm the symptom with metrics, then check what changed in the deploy. I would compare traces before and after to find which span got slower, check the database for new slow queries or missing indexes, and roll back if users are affected while I investigate.',
    'We had a cron job that failed silently. I compared keeping cron with retries, using Celery, or a managed scheduler. I chose Celery because we already ran Redis and needed retries and visibility; failed runs dropped by 60%.',
    "I noticed a teammate's PR had business logic in the API layer. I left a comment explaining why moving it to a service would make it testable, and paired with him for an hour. He refactored it and now does the same in his reviews.",
    'On the ledger launch I worked with finance, product and support. I set up a weekly 30-minute check-in and a shared decision log, so when finance changed a reconciliation rule we caught it before release and shipped on time.',
    'My tech lead told me my design docs were too long to review. I started writing a one-page summary with the decision and trade-offs first, and review time dropped from a week to two days.',
    'A colleague wanted to add Kafka for a simple notification feature. I disagreed because the volume was low, and I wrote a short comparison of options with costs. We agreed to use the existing queue and revisit if volume grew, and I supported the decision.',
    'What does the on-call rotation look like for this team?',
]


def make_docx(lines: list[str]) -> bytes:
    import docx

    d = docx.Document()
    for ln in lines:
        d.add_paragraph(ln)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    """Minimal single-page PDF with real text (Helvetica), for tests and demos."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body = ["BT", "/F1 10 Tf", "50 780 Td", "14 TL"]
    for ln in lines:
        safe = ln.encode("latin-1", "replace").decode("latin-1")
        body.append(f"({esc(safe)}) Tj T*")
    body.append("ET")
    stream = "\n".join(body).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def job_form_data(spec: dict[str, Any]) -> dict[str, Any]:
    import copy

    return {
        "title": spec["title"],
        "department": spec["department"],
        "location": spec["location"],
        "seniority": spec["seniority"],
        "description": spec["description"],
        "essential_criteria": [{"id": "", "name": n, "description": d, "anchors": dict(DEFAULT_ANCHORS)} for n, d in spec["criteria"]],
        "preferred_criteria": spec["preferred"],
        "behaviors": copy.deepcopy(DEFAULT_BEHAVIORS),
        "bands": copy.deepcopy(DEFAULT_BANDS),
        "max_followups": 3,
        "estimated_minutes": 25,
        "invite_valid_days": 5,
    }


def seed(db: Session, user: User) -> dict[str, int]:
    """Create demo jobs, applications and one completed interview with a (mock) report."""
    from app.services.applications import create_application, issue_invitation
    from app.services.evaluation import generate
    from app.services.interview_engine import complete, start_session, submit_answer
    from app.services.jobs import save_job
    from app.services.llm_provider import get_provider
    from app.services.interview_planner import slug

    provider = get_provider()
    jobs: list[Job] = []
    for spec in JOBS:
        existing = db.scalar(select(Job).where(Job.title == spec["title"]))
        if existing:
            jobs.append(existing)
            continue
        data = job_form_data(spec)
        used: set[str] = set()
        for c in data["essential_criteria"]:
            c["id"] = slug(c["name"], used)
        job = save_job(db, None, data, user, provider)
        job.status = "open"
        db.commit()
        jobs.append(job)

    created = 0
    apps: list[Application] = []
    for r in RESUMES:
        job = jobs[r["job"]]
        try:
            app = create_application(db, job, name=r["name"], email=r["email"], filename=f"{r['name'].lower().replace(' ', '_')}_resume.docx", content=make_docx(r["lines"]), provider=provider, source="recruiter", actor=user)
            apps.append(app)
            created += 1
        except Exception:  # noqa: BLE001 - already present
            db.rollback()

    # One completed interview with a report, one pending invitation.
    if apps:
        done = next((a for a in apps if a.candidate.name == "Daniel Okafor"), None)
        if done:
            inv, _, _ = issue_invitation(db, done, user)
            sess = start_session(db, done, inv)
            i = 0
            while sess.current_index < len(sess.plan_json["questions"]) and i < 30:
                submit_answer(db, sess, DEMO_ANSWERS[min(sess.current_index, len(DEMO_ANSWERS) - 1)], f"demo-{i}", provider)
                i += 1
            complete(db, sess)
            if sess.latest_report:
                generate(db, sess.latest_report, provider)
        invited = next((a for a in apps if a.candidate.name == "Mei Lin Zhou"), None)
        if invited:
            issue_invitation(db, invited, user)
    return {"jobs": len(jobs), "applications": created}
