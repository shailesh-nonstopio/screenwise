# Implementation status

## Done
- **Jobs:** title, description, seniority, essential criteria with editable 1–4 anchors, preferred skills, workplace behaviours, configurable experience bands, interview settings, draft/open/closed states, and an editable core question bank. Rubric versions bump automatically when these change.
- **Applications:** public careers page and application form (with consent), plus recruiter-added applications. PDF/DOCX validation and extraction. The rule-based parser marks dates as approximate and missing fields as unknown; the AI parser is used when a provider is configured. Deterministic resume-to-criteria evidence. Duplicate-application protection.
- **Recruiter review:** dashboard with search, job and stage filters and an "attention" view; resume with line references; extracted facts; criteria match; editable interview plan with approval; resume download; re-analyse.
- **Invitations:** 256-bit tokens stored as an HMAC, expiry, revoke, replace (the interview continues where it left off), duplicate-send protection, email preview, delivery history, and an email log with links redacted.
- **Candidate interview:** instructions and consent, one question at a time, clarification, one follow-up per question (capped per job), draft autosave, idempotent retries, offline banner, recovery after refresh, end-early option, no-JS fallback, mobile layout, and clear pages for expired, revoked, submitted and not-found links.
- **Evaluation:** schema-validated AI output; policy normalisation (no hidden gaps, citations required, no rating from the resume alone, recommendation guardrails); failure, retry and stale-job recovery; JSON export; print/PDF via the browser.
- **Human decision:** Advance / Hold / Decline / Not yet decided with notes and history, stored separately from the AI recommendation. The system never sets a decision itself.
- **Security:** Argon2 passwords, roles (admin/recruiter), CSRF, signed secure cookies, strict CSP, HSTS, rate limits, redacted JSON logs, audit trail, data deletion, retention purge, fail-fast checks on production config.
- **Operations:** Alembic migrations (tested on SQLite and PostgreSQL 16), CLI, Vercel config with cron and a build-time migration hook, health check.
- **Tests:** 71 automated tests, passing on both SQLite and PostgreSQL.

## Mocked or depends on your configuration
| Integration | Default | Live option |
|---|---|---|
| AI | `mock`: deterministic heuristics, reports clearly labelled as mock | `openai` (or any OpenAI-compatible endpoint), `anthropic` |
| Email | `dryrun`: logged with status "Logged (not sent)" | `smtp`, `resend` |
| File storage | database (bytes in Postgres) | `local` directory (non-serverless hosts) |

The OpenAI and Anthropic adapters are tested with mocked HTTP responses only. Run one real interview with your key before going live and review the report quality.

## Known limitations
- Rate limiting is per instance on serverless. Add Vercel Firewall rules for strict limits.
- Report generation after submission is best-effort on serverless. The **Generate now** button and the cron job are the fallback.
- Scanned (image-only) resumes are rejected with a clear message; there's no OCR.
- There's a single organisation: every recruiter sees every job.
- There's no self-service password reset email. Admins create accounts and users change their own password.
- Experience is estimated from date ranges on the resume; relevance to the job isn't judged.
- Templated core questions (mock mode) are generic. Use an AI provider, or edit them on the job page.

## Recommended next steps
1. Configure a real AI provider and email provider. Run a pilot with internal volunteers and review rating consistency.
2. Edit the privacy notice for your actual processors and jurisdictions, then get legal review.
3. Add SSO (Google or Microsoft) for recruiters and a password reset email.
4. Use object storage (S3 or Vercel Blob) for resumes if volume grows.
5. Add an adverse-impact and consistency dashboard across candidates per job.
6. Optional later: an ATS integration, and voice or video **only** with explicit consent, visible controls and an accommodation path, as the spec requires.
