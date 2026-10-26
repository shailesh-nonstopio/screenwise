"""Job creation and rubric versioning."""

from __future__ import annotations

import copy
import math
from typing import Any

from sqlalchemy.orm import Session

from app.models import Job, User
from app.services import audit
from app.services.interview_planner import DEFAULT_ANCHORS, DEFAULT_BANDS, DEFAULT_BEHAVIORS, draft_core_questions, slug
from app.services.llm_provider import LLMProvider

SENIORITIES = ("junior", "mid", "senior", "lead")


def new_job_defaults() -> dict[str, Any]:
    return {
        "title": "",
        "department": "",
        "location": "",
        "seniority": "mid",
        "description": "",
        "essential_criteria": [{"id": "", "name": "", "description": "", "anchors": dict(DEFAULT_ANCHORS)}],
        "preferred_criteria": [],
        "behaviors": copy.deepcopy(DEFAULT_BEHAVIORS),
        "bands": copy.deepcopy(DEFAULT_BANDS),
        "max_followups": 3,
        "estimated_minutes": 25,
        "invite_valid_days": 5,
    }


def job_to_form(job: Job) -> dict[str, Any]:
    anchors = job.rubric_json.get("anchors", {})
    return {
        "title": job.title,
        "department": job.department,
        "location": job.location,
        "seniority": job.seniority,
        "description": job.description,
        "essential_criteria": [c | {"anchors": anchors.get(c["id"], dict(DEFAULT_ANCHORS))} for c in job.essential_criteria],
        "preferred_criteria": job.preferred_criteria,
        "behaviors": job.rubric_json.get("behaviors", copy.deepcopy(DEFAULT_BEHAVIORS)),
        "bands": job.experience_bands_json or copy.deepcopy(DEFAULT_BANDS),
        "max_followups": job.max_followups,
        "estimated_minutes": job.estimated_minutes,
        "invite_valid_days": job.invite_valid_days,
    }


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float) -> float:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) and 0 <= f <= 99 else default


def parse_job_form(form: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Turn submitted form fields into job data. Returns (data, errors)."""
    errors: dict[str, str] = {}
    get = lambda k: str(form.get(k) or "").strip()  # noqa: E731
    data = new_job_defaults()
    data["title"] = get("title")[:200]
    data["department"] = get("department")[:120]
    data["location"] = get("location")[:200]
    data["seniority"] = get("seniority") if get("seniority") in SENIORITIES else "mid"
    data["description"] = get("description")[:10000]
    if not data["title"]:
        errors["title"] = "Enter a job title."
    if len(data["description"]) < 30:
        errors["description"] = "Add a description of at least a couple of sentences; it shapes the interview."

    crits: list[dict[str, Any]] = []
    i = 0
    while f"criteria-{i}-name" in form and i < 40:
        name = get(f"criteria-{i}-name")[:160]
        if name and not form.get(f"criteria-{i}-remove"):
            crits.append(
                {
                    "id": get(f"criteria-{i}-id")[:60],
                    "name": name,
                    "description": get(f"criteria-{i}-description")[:500],
                    "anchors": {str(n): (get(f"criteria-{i}-a{n}")[:400] or DEFAULT_ANCHORS[str(n)]) for n in range(1, 5)},
                }
            )
        i += 1
    if not crits:
        errors["criteria"] = "Add at least one essential criterion."
    if len(crits) > 12:
        errors["criteria"] = "Keep essential criteria to 12 or fewer so the interview stays a reasonable length."
    used: set[str] = {c["id"] for c in crits if c["id"]}
    for c in crits:
        if not c["id"]:
            c["id"] = slug(c["name"], used)
    data["essential_criteria"] = crits

    raw_pref = get("preferred_criteria").replace("\n", ",")
    data["preferred_criteria"] = [p.strip()[:80] for p in raw_pref.split(",") if p.strip()][:20]

    behaviors = []
    for b in DEFAULT_BEHAVIORS:
        behaviors.append(
            {
                "id": b["id"],
                "name": b["name"],
                "anchor": get(f"behavior-{b['id']}-anchor")[:300] or b["anchor"],
                "enabled": bool(form.get(f"behavior-{b['id']}-enabled")),
            }
        )
    data["behaviors"] = behaviors

    bands = []
    for j in range(8):
        label = get(f"band-{j}-label")
        if not label:
            continue
        lo = _float(form.get(f"band-{j}-min"), 0)
        hi = _float(form.get(f"band-{j}-max"), 99)
        if hi <= lo:
            errors["bands"] = f"Band “{label}”: the upper bound must be above the lower bound."
        bands.append({"min_years": lo, "max_years": hi, "label": label[:30], "focus": get(f"band-{j}-focus")[:200]})
    data["bands"] = sorted(bands, key=lambda b: b["min_years"]) or copy.deepcopy(DEFAULT_BANDS)

    data["max_followups"] = _int(form.get("max_followups"), 3, 0, 10)
    data["estimated_minutes"] = _int(form.get("estimated_minutes"), 25, 5, 120)
    data["invite_valid_days"] = _int(form.get("invite_valid_days"), 5, 1, 30)
    return data, errors


def _rubric_fingerprint(job: Job) -> Any:
    return (job.essential_criteria, job.rubric_json, job.preferred_criteria)


def save_job(db: Session, job: Job | None, data: dict[str, Any], user: User, provider: LLMProvider) -> Job:
    creating = job is None
    if job is None:
        job = Job(created_by_id=user.id, status="draft", rubric_version=1)
        db.add(job)
    before = None if creating else copy.deepcopy(_rubric_fingerprint(job))
    before_questions_basis = None if creating else copy.deepcopy((job.essential_criteria, job.rubric_json.get("behaviors")))

    job.title = data["title"]
    job.department = data["department"]
    job.location = data["location"]
    job.seniority = data["seniority"]
    job.description = data["description"]
    job.essential_criteria = [{k: c[k] for k in ("id", "name", "description")} for c in data["essential_criteria"]]
    job.preferred_criteria = data["preferred_criteria"]
    job.rubric_json = {
        "anchors": {c["id"]: c["anchors"] for c in data["essential_criteria"]},
        "behaviors": data["behaviors"],
    }
    job.experience_bands_json = data["bands"]
    job.max_followups = data["max_followups"]
    job.estimated_minutes = data["estimated_minutes"]
    job.invite_valid_days = data["invite_valid_days"]

    question_basis_changed = creating or before_questions_basis != (job.essential_criteria, job.rubric_json.get("behaviors"))
    if question_basis_changed or not job.core_questions_json:
        questions, _ = draft_core_questions(job, provider)
        job.core_questions_json = questions
    if not creating and before != _rubric_fingerprint(job):
        job.rubric_version += 1
    db.flush()
    audit.record(
        db, "job.created" if creating else "job.updated", "job", job.id, actor_type="user", actor_id=user.id,
        metadata={"rubric_version": job.rubric_version},
    )
    db.commit()
    return job


def save_core_questions(db: Session, job: Job, form: Any, user: User) -> tuple[bool, str]:
    current = {q["id"]: q for q in job.core_questions_json}
    out: list[dict[str, Any]] = []
    for qid, q in current.items():
        if form.get(f"q-{qid}-remove"):
            continue
        text = str(form.get(f"q-{qid}-text") or "").strip()[:600]
        if len(text) < 10:
            return False, "Each question needs at least 10 characters."
        out.append(q | {"text": text, "clarification": str(form.get(f"q-{qid}-clarification") or "").strip()[:600]})
    new_text = str(form.get("new-text") or "").strip()
    if new_text:
        target = str(form.get("new-target") or "")
        crit = target[2:] if target.startswith("c:") else None
        beh = target[2:] if target.startswith("b:") else None
        out.append(
            {
                "id": "",
                "category": str(form.get("new-category") or "role_skills"),
                "criterion_id": crit if crit in {c["id"] for c in job.essential_criteria} else None,
                "behavior_id": beh if beh in {b["id"] for b in job.rubric_json.get("behaviors", [])} else None,
                "text": new_text[:600],
                "clarification": str(form.get("new-clarification") or "").strip()[:600],
            }
        )
    if not out:
        return False, "Keep at least one core question."
    if len(out) > 15:
        return False, "Keep the core set to 15 questions or fewer."
    job.core_questions_json = [q | {"id": f"c{i + 1}"} for i, q in enumerate(out)]
    job.rubric_version += 1
    audit.record(db, "job.questions_updated", "job", job.id, actor_type="user", actor_id=user.id, metadata={"rubric_version": job.rubric_version, "count": len(out)})
    db.commit()
    return True, "Core questions saved. New invitations will use rubric v%d." % job.rubric_version
