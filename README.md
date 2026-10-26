# Screenwise — AI pre-screening interviews

A Python (FastAPI) web app for the first stage of hiring:

1. A candidate applies to a job with a PDF/DOCX resume.
2. The resume is parsed (uncertain facts are flagged, never guessed) and compared with the job's essential criteria.
3. The recruiter reviews a draft interview plan and clicks **Send invitation**.
4. The candidate gets an emailed link to a structured **text** interview: one question at a time, clarification allowed, drafts saved, recoverable after a refresh or a lost connection.
5. An evidence-linked report is generated. Every essential criterion is rated on its own, with citations to transcript answers (`Q3`) and resume lines (`R-5`).
6. The recruiter records the decision (**Advance / Hold / Decline / Not yet decided**), stored separately from the advisory AI recommendation.

The system never rejects anyone automatically, and never uses camera, voice, emotion, appearance or protected characteristics.

---

## Quick start (local, about 2 minutes)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                 # defaults: SQLite, mock AI, email dry-run
python -m app.cli migrate
python -m app.cli seed-demo          # synthetic jobs + candidates; prints a demo login
uvicorn app.main:app --reload
```

Open http://localhost:8000 and sign in as `demo@example.com` / `demo-password-123`.

To try the candidate side, open an application, go to **Invitations**, and click **Create link & log email (dry run)**. In dry-run mode the link is shown once so you can open it as the candidate.

Run the tests:

```bash
pytest                                            # SQLite
TEST_DATABASE_URL=postgresql://user@localhost/sw_test pytest   # same suite on PostgreSQL (wipes that DB's public schema)
```

---

## Deploying to Vercel

Vercel runs the app as a single Python function (`app/main.py`, configured in `pyproject.toml` and `vercel.json`). Its filesystem isn't persistent, so production uses **PostgreSQL**, and resumes are stored in the database.

### 1. Create a PostgreSQL database
Any hosted Postgres works: Neon (via Vercel Marketplace → Storage), Supabase, RDS, and others. Copy the **pooled** connection string. `postgres://` and `postgresql://` URLs are accepted as-is.

### 2. Import the project
Push this folder to a Git repository and import it in Vercel. You don't need to set a framework preset or build command: Vercel detects FastAPI, installs from `pyproject.toml`, and runs `python -m app.cli build`.

### 3. Set environment variables (Project → Settings → Environment Variables)

| Variable | Production value |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | 48+ random chars: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BASE_URL` | `https://your-domain` (used in invitation links; must be https) |
| `DATABASE_URL` | your pooled Postgres URL |
| `RUN_MIGRATIONS_ON_BUILD` | `1` (applies migrations during each production build) |
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | first admin; only used while there are no users. Remove after first sign-in. |
| `COMPANY_NAME`, `SUPPORT_EMAIL`, `ACCESSIBILITY_EMAIL`, `PRIVACY_EMAIL`, `RETENTION_DAYS` | shown to candidates |
| `LLM_PROVIDER` + key | `openai` + `OPENAI_API_KEY`, or `anthropic` + `ANTHROPIC_API_KEY` |
| `EMAIL_BACKEND` | `resend` + `RESEND_API_KEY` (recommended on serverless), or `smtp` + `SMTP_*` |
| `EMAIL_FROM` | a sender on a domain verified with your email provider |
| `CRON_SECRET` | random string; Vercel Cron sends it automatically |

Other settings and their defaults are in `.env.example`.

> **Preview deployments:** give the Preview environment its own `DATABASE_URL`, or leave `RUN_MIGRATIONS_ON_BUILD` unset there, so previews never migrate the production database.

### 4. Deploy, then sign in
Open `https://your-domain/login` with the bootstrap admin, change the password under **Account**, and add teammates under **Team**. Create a job, publish it, and share its public link (`/jobs/<id>`).

### Notes specific to Vercel
- **Upload size:** Vercel limits request bodies to 4.5 MB, so `MAX_UPLOAD_MB` defaults to 4.
- **Report generation:** reports start right after the candidate submits (`REPORT_GENERATION=background`). If that run is cut short, the recruiter's report page shows **Generate now**, and the cron job (`/api/cron/maintenance`) picks up anything queued. The cron in `vercel.json` runs daily, which is the Hobby-plan limit; on Pro you can change it to `*/10 * * * *`. The same job deletes data older than `RETENTION_DAYS`.
- **Rate limiting** is in-process, so it's per instance. For strict limits add Vercel Firewall rate-limit rules on `/login`, `/jobs/*/apply` and `/interview/*`.
- **Function duration:** `vercel.json` sets `maxDuration: 60` to leave room for AI report generation.

Other hosts work too (Docker, Render, a VM): run `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers` behind HTTPS with the same environment variables. `STORAGE_BACKEND=local` stores files on disk outside the web root.

---

## How it works

```
app/
  main.py               FastAPI app, security headers, sessions, error pages (Vercel entrypoint)
  config.py             Settings from environment variables; refuses insecure production config
  database.py           SQLAlchemy engine/session (SQLite locally, PostgreSQL in production)
  security.py           Argon2 passwords, CSRF, keyed token hashing, auth deps, rate limiting
  models/               ORM models (Job, Candidate, Application, Invitation, InterviewSession, ...)
  schemas/              Pydantic schemas that validate every piece of AI output
  routers/              auth, public (careers/apply), interview (candidate), recruiter, system (health/cron)
  services/
    resume_parser.py    file validation (magic bytes, zip-bomb checks), text extraction, parsing
    job_matcher.py      deterministic resume ↔ criteria evidence (keyword overlap, transparent)
    interview_planner.py rubric defaults, core question bank, per-candidate plan, experience bands
    interview_engine.py interview state machine (follow-ups, clarifications, idempotency, locking)
    llm_provider.py     provider abstraction: OpenAI, Anthropic; mock_llm.py for tests/demo
    evaluation.py       report generation + non-negotiable policy normalisation
    email_service.py    dry-run / SMTP / Resend backends with delivery records
    applications.py     intake, invitations, decisions, deletion, retention purge
  templates/, static/   server-rendered UI (no inline scripts or styles; strict CSP)
migrations/             Alembic migrations
tests/                  71 tests: parsing, interview flow, invitation security, evaluation, access, providers
```

### Rules the AI cannot override
Model output is untrusted. It's parsed as JSON, validated against Pydantic schemas, and then normalised:

- Every essential criterion appears in the report. Any criterion the model skipped is shown as **Not assessed**.
- A rating without a valid citation (`Q<n>` or `R-<n>`) is withheld and shown as **Insufficient evidence**. Unknown citations are removed.
- A rating backed only by the resume, not by an answered question, becomes **Not assessed**.
- If any essential criterion is unmet or not assessed, "Recommend proceeding" becomes "Proceed with follow-up". If fewer than half the scored questions were answered, the recommendation becomes "Insufficient evidence".
- Any policy change is listed on the report under **Policy checks applied**.
- Candidate and resume text is wrapped in `<untrusted>` tags. The system prompt tells the model to ignore any instructions inside them.
- If the AI fails mid-interview, the candidate still gets the next planned question and the stored clarification. If report generation fails, the error is shown with a **Retry** button. Nothing is fabricated.

### Consistency and reproducibility
- Every candidate for a job gets the same core questions, plus one opening question tailored to their resume. Follow-ups are limited to one per question and capped per job.
- Each interview stores a snapshot of the plan and rubric. Editing a job's criteria, anchors, behaviours or questions bumps `rubric_version`; interviews already started keep the version they began with.
- Transcripts are locked on submission and stored with a SHA-256 hash.

### Security and privacy
- Recruiter routes require a signed-in, active user. Team management and the audit log are admin-only.
- Passwords are hashed with Argon2. Login is rate-limited, takes the same time whether or not the email exists, and resets the session.
- Session cookies are signed, HttpOnly, SameSite=Lax, and Secure in production. Every POST has a CSRF token.
- Invitation tokens carry 256 bits of randomness and are stored only as an HMAC. Opening the link swaps the token for a session cookie and redirects to `/interview`, so the token doesn't stay in the URL. `Referrer-Policy: no-referrer` is set, and tokens are redacted from logs.
- Headers: strict CSP (no inline JS or CSS), `X-Frame-Options: DENY`, HSTS in production, and a `Permissions-Policy` that blocks camera and microphone.
- Uploads are checked by magic bytes, size and zip-bomb limits, stored under random keys outside the web root, and downloaded with `Content-Disposition: attachment`.
- Logs are structured JSON and never include resumes, answers, tokens or keys. Audit metadata is filtered to safe keys.
- Recruiters can delete a candidate's data (type DELETE to confirm). Data older than `RETENTION_DAYS` is purged automatically.

---

## Commands

```bash
python -m app.cli migrate                     # apply migrations
python -m app.cli create-user --email a@b.com --name "A B" --role admin
python -m app.cli seed-demo                   # synthetic demo data (refuses in production)
python -m app.cli process-reports             # generate queued reports
python -m app.cli purge-expired               # apply the retention policy
alembic revision --autogenerate -m "change"   # new migration after model changes
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Vercel function crashes on start with "Refusing to start with insecure production configuration" | Set `SECRET_KEY` (32+ chars), an `https://` `BASE_URL`, and a Postgres `DATABASE_URL`. |
| `relation "users" does not exist` | Migrations haven't run. Set `RUN_MIGRATIONS_ON_BUILD=1` and redeploy, or run `DATABASE_URL=... python -m app.cli migrate` locally. |
| Can't sign in on first deploy | Set `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` (12+ chars) while the users table is empty, or use `create-user`. |
| Invitations show "Logged (not sent)" | `EMAIL_BACKEND` is still `dryrun`. |
| Invitation shows "Failed" | The error is shown on the Invitations tab and in the Email log. For Resend, verify the `EMAIL_FROM` domain. |
| Report stuck on "being prepared" | Click **Generate now**. Check the AI key and look in Runtime Logs for `report.failed`. |
| "Form is out of date" (403) | The session expired. Reload the page. |
| Uploads fail with 413 on Vercel | The file is over 4.5 MB. Ask for a smaller PDF. |

## Before real hiring use
Run a pilot. Check scoring consistency across reviewers, accessibility (screen readers, keyboard), false positives and negatives, and adverse impact across groups. Get legal and privacy review for your jurisdictions (e.g. GDPR, India's DPDP Act, NYC Local Law 144, EU AI Act obligations for employment systems). Update `/privacy` (`app/templates/candidate/privacy.html`) to match your actual processors and retention.

See **IMPLEMENTATION_STATUS.md** for what's done, what's mocked and next steps.
