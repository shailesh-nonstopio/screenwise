"""Job rubric defaults, core question bank and per-application interview plans.

Every candidate for the same job (and rubric version) gets the same core questions. Only the
opening resume question is tailored, and follow-ups are added live during the interview.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError

from app.models import Application, Job, utcnow
from app.schemas import DraftQuestions, QuestionText
from app.services.llm_provider import LLMError, LLMProvider
from app.services.text_utils import clip

log = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "resume_fundamentals": "Resume fundamentals",
    "role_skills": "Role skills",
    "depth": "Depth of experience",
    "problem_solving": "Problem solving",
    "communication": "Communication",
    "workplace": "Workplace behaviours",
    "candidate_questions": "Candidate questions",
}

SENIORITY_LABELS = {"junior": "Junior", "mid": "Mid-level", "senior": "Senior", "lead": "Lead / Staff"}

DEFAULT_ANCHORS = {
    "1": "No relevant evidence in the answers.",
    "2": "Partial or basic evidence; few specifics about their own work.",
    "3": "Clear, specific evidence that meets the requirement.",
    "4": "Strong evidence beyond the requirement, with reasoning about trade-offs or impact.",
}

DEFAULT_BEHAVIORS = [
    {"id": "collaboration", "name": "Collaboration", "anchor": "Shares context and works across roles to reach outcomes.", "enabled": True},
    {"id": "feedback", "name": "Giving and receiving feedback", "anchor": "Gives specific, actionable feedback and acts on feedback received.", "enabled": True},
    {"id": "accountability", "name": "Accountability", "anchor": "Owns outcomes and follows through on mistakes.", "enabled": False},
    {"id": "disagreement", "name": "Handling disagreement", "anchor": "Disagrees with reasons and commits once a decision is made.", "enabled": True},
]

BEHAVIOR_QUESTIONS = {
    "collaboration": (
        "Tell me about a time you worked with people in other roles to deliver something. How did you keep everyone aligned, and what was the outcome?",
        "Any project where you depended on people outside your own role works, for example product, design, sales or support.",
    ),
    "feedback": (
        "Describe a time you gave or received feedback that changed how someone worked. What was said, and what happened next?",
        "It can be feedback you gave or feedback you received, formal or informal. I'm interested in what was said and what changed.",
    ),
    "accountability": (
        "Tell me about a mistake you made at work. How did you handle it, and what did you change afterwards?",
        "Any work mistake, big or small. I'm interested in what you did once you noticed it.",
    ),
    "disagreement": (
        "Tell me about a time you disagreed with a colleague about how to approach a piece of work. What did you do, and how was it resolved?",
        "Any real disagreement about how to do something at work counts. I'm interested in how you handled it.",
    ),
}

DEFAULT_BANDS = [
    {"min_years": 0, "max_years": 1, "label": "0–1 yrs", "focus": "Fundamentals and projects"},
    {"min_years": 1, "max_years": 3, "label": "1–3 yrs", "focus": "Practical application and troubleshooting"},
    {"min_years": 3, "max_years": 5, "label": "3–5 yrs", "focus": "Ownership and trade-offs"},
    {"min_years": 5, "max_years": 99, "label": "5+ yrs", "focus": "Advanced design, mentoring or strategy where relevant"},
]

CLOSING_QUESTION = {
    "category": "candidate_questions",
    "text": "Thank you. Is there anything you'd like to ask about the role, the team or the next steps? (This one isn't scored.)",
    "clarification": "Anything at all is fine: the work, the team, how decisions are made, or what happens next.",
}


def slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "criterion"
    s, n = base, 2
    while s in used:
        s, n = f"{base}_{n}", n + 1
    used.add(s)
    return s


def _lower_first(s: str) -> str:
    """Lower-case the first letter for use mid-sentence, unless it starts with a proper noun or acronym."""
    from app.services.resume_parser import KNOWN_SKILLS

    if not s or s[:2].isupper():
        return s
    first = s.split()[0].strip(",.()").lower()
    if any(first == k.lower() for k in KNOWN_SKILLS):
        return s
    return s[:1].lower() + s[1:]


def template_core_questions(job: Job) -> list[dict[str, Any]]:
    """Deterministic question bank used by the mock provider and as a fallback."""
    out: list[dict[str, Any]] = []
    cycle = ["role_skills", "depth", "problem_solving"]
    for i, crit in enumerate(job.essential_criteria):
        name = _lower_first(crit["name"])
        cat = cycle[i % len(cycle)]
        if cat == "role_skills":
            text = f"Describe a piece of work where you applied {name}. What was the goal, what did you do yourself, and how did you check it worked?"
            clar = f"Pick one concrete example involving {name}. Tell me what you were trying to achieve, the steps you personally took, and how you knew it was successful."
        elif cat == "depth":
            text = f"Tell me about an important decision you made involving {name}. What options did you consider, what constraints were you under, and what changed as a result?"
            clar = "Think of a choice that mattered, where there was more than one reasonable option. I'm interested in how you weighed them and what happened afterwards."
        else:
            text = f"Imagine a problem involving {name} that you haven't seen before. How would you approach it? Walk me through your first steps, your assumptions and the trade-offs you'd weigh."
            clar = "There's no single right answer. Describe how you'd start investigating or solving it and what you'd look at first."
        out.append({"criterion_id": crit["id"], "behavior_id": None, "category": cat, "text": text, "clarification": clar})
    for b in job.rubric_json.get("behaviors", []):
        if b.get("enabled") and b["id"] in BEHAVIOR_QUESTIONS:
            text, clar = BEHAVIOR_QUESTIONS[b["id"]]
            out.append({"criterion_id": None, "behavior_id": b["id"], "category": "workplace", "text": text, "clarification": clar})
        elif b.get("enabled"):
            out.append(
                {
                    "criterion_id": None,
                    "behavior_id": b["id"],
                    "category": "workplace",
                    "text": f"Tell me about a specific situation that shows how you approach {_lower_first(b['name'])}. What did you do and what was the result?",
                    "clarification": f"Any real work example is fine. The focus is: {b.get('anchor', '')}",
                }
            )
    return out


def draft_core_questions(job: Job, provider: LLMProvider) -> tuple[list[dict[str, Any]], str]:
    """Return (questions, source). Falls back to templates if the provider fails."""
    fallback = template_core_questions(job)
    if provider.is_mock:
        return _with_ids(fallback), "template"
    crit_ids = {c["id"] for c in job.essential_criteria}
    beh_ids = {b["id"] for b in job.rubric_json.get("behaviors", []) if b.get("enabled")}
    try:
        raw = provider.draft_core_questions(
            {
                "title": job.title,
                "seniority": job.seniority,
                "description": job.description[:4000],
                "essential_criteria": job.essential_criteria,
                "behaviors": [b for b in job.rubric_json.get("behaviors", []) if b.get("enabled")],
            }
        )
        drafted = DraftQuestions.model_validate(raw).questions
    except (LLMError, ValidationError) as exc:
        log.warning("core question drafting failed", extra={"safe": {"error": type(exc).__name__}})
        return _with_ids(fallback), "template (AI unavailable)"
    out: list[dict[str, Any]] = []
    covered_c, covered_b = set(), set()
    for q in drafted:
        if q.criterion_id in crit_ids and q.criterion_id not in covered_c:
            covered_c.add(q.criterion_id)
            out.append(q.model_dump() | {"behavior_id": None})
        elif q.behavior_id in beh_ids and q.behavior_id not in covered_b:
            covered_b.add(q.behavior_id)
            out.append(q.model_dump() | {"criterion_id": None, "category": "workplace"})
    # Guarantee coverage of every criterion and behaviour.
    for q in fallback:
        if (q["criterion_id"] and q["criterion_id"] not in covered_c) or (q["behavior_id"] and q["behavior_id"] not in covered_b):
            out.append(q)
    return _with_ids(out), "ai"


def _with_ids(qs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [q | {"id": f"c{i + 1}"} for i, q in enumerate(qs)]


def experience_band(job: Job, parsed: dict[str, Any]) -> dict[str, Any]:
    bands = job.experience_bands_json or DEFAULT_BANDS
    exp = parsed.get("experience_years")
    if not exp:
        return {"label": "Unknown", "focus": f"Experience not determined; using role seniority ({SENIORITY_LABELS.get(job.seniority, job.seniority)})", "years": None}
    mid = (float(exp["min"]) + float(exp["max"])) / 2
    for b in bands:
        if float(b["min_years"]) <= mid < float(b["max_years"]):
            years = f"{exp['min']:g}–{exp['max']:g}" if exp["min"] != exp["max"] else f"{exp['max']:g}"
            return {"label": b["label"], "focus": b["focus"], "years": years, "certain": bool(exp.get("certain"))}
    return {"label": "Unknown", "focus": "No matching band", "years": None}


def _resume_question(job: Job, application: Application, band: dict[str, Any], provider: LLMProvider) -> dict[str, Any]:
    lines = {ln["ref"]: ln["text"] for ln in application.parsed_resume_json.get("lines", [])}
    match = application.match_json.get("criteria", [])
    pick = next((m for m in match if m["status"] == "evidence" and m["refs"]), None) or next(
        (m for m in match if m["refs"]), None
    )
    ref = pick["refs"][0] if pick else None
    line = lines.get(ref) if ref else None
    crit_name = pick["name"] if pick else (job.essential_criteria[0]["name"] if job.essential_criteria else job.title)
    if line:
        text = f"Your resume mentions: “{clip(line, 160).rstrip('.')}”. Walk me through what you personally did there, and one decision you made along the way."
        clar = "Pick the part you were most hands-on with. Tell me what you built or did yourself, and one choice you made (for example an approach or tool) and why."
    else:
        text = "Walk me through a recent piece of work you're proud of. What was your role, what did you personally contribute, and what was the result?"
        clar = "Any recent project or piece of work is fine. Focus on the parts you did yourself."
    q = {"text": text, "clarification": clar}
    if not provider.is_mock:
        try:
            raw = provider.resume_question(job.title, crit_name, line, f"{band['label']}: {band['focus']}")
            q = QuestionText.model_validate(raw).model_dump()
        except (LLMError, ValidationError):
            log.info("resume question fallback to template")
    return {
        "category": "resume_fundamentals",
        "criterion_id": pick["criterion_id"] if pick else None,
        "behavior_id": None,
        "source_ref": ref,
        **q,
    }


def build_plan(job: Job, application: Application, provider: LLMProvider) -> dict[str, Any]:
    band = experience_band(job, application.parsed_resume_json)
    crit_names = {c["id"]: c["name"] for c in job.essential_criteria}
    beh_names = {b["id"]: b["name"] for b in job.rubric_json.get("behaviors", [])}
    questions: list[dict[str, Any]] = [_resume_question(job, application, band, provider)]
    for q in job.core_questions_json:
        questions.append({k: q.get(k) for k in ("category", "criterion_id", "behavior_id", "text", "clarification")} | {"core_id": q.get("id")})
    questions.append(dict(CLOSING_QUESTION, criterion_id=None, behavior_id=None))
    for i, q in enumerate(questions):
        q["index"] = i
        q["number"] = i + 1
        q["scored"] = q["category"] != "candidate_questions"
        target = crit_names.get(q.get("criterion_id") or "") or beh_names.get(q.get("behavior_id") or "") or ("Not scored" if not q["scored"] else "General")
        q["target"] = target
        q["category_label"] = CATEGORY_LABELS.get(q["category"], q["category"])
    return {
        "questions": questions,
        "band": band,
        "rubric_version": job.rubric_version,
        "max_followups": job.max_followups,
        "generated_at": utcnow().isoformat(),
        "provider": provider.name,
    }
