"""Deterministic mock provider for tests and local demos.

It never pretends to be a real model: reports it produces are labelled as mock output, and its
"ratings" come from transparent heuristics (answer length, keyword overlap, specificity).
"""

from __future__ import annotations

import re
from typing import Any

from app.services.llm_provider import LLMError, LLMProvider
from app.services.text_utils import clip, keywords, overlap

MOCK_NOTICE = (
    "Mock evaluation (development mode): ratings below come from simple heuristics such as answer length, "
    "keyword overlap and specificity, not from an AI model. Configure LLM_PROVIDER for real evaluations."
)


def _words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def _specificity(text: str) -> int:
    score = 0
    if re.search(r"\b\d+(\.\d+)?\s*(%|ms|s|x|k|m|users|rows|people|days|weeks|hours)?\b", text or ""):
        score += 1
    if re.search(r"\b(I|my|me)\b", text or ""):
        score += 1
    if re.search(r"\b(because|so that|trade-?off|instead|decided|chose|result|outcome)\b", text or "", re.I):
        score += 1
    return score


def heuristic_rating(text: str, kw: set[str]) -> tuple[int | None, str]:
    n = _words(text)
    if n == 0:
        return None, "not_assessed"
    if n < 20:
        return None, "insufficient_evidence"
    hits = len(overlap(kw, text)) if kw else 1
    spec = _specificity(text)
    rating = 1 if hits == 0 and spec == 0 else 2
    if n >= 50 and (hits >= 1 or spec >= 2):
        rating = 3
    if n >= 90 and spec >= 2 and hits >= 2:
        rating = 4
    return rating, "rated"


class MockProvider(LLMProvider):
    name = "mock"
    model = "mock-heuristic-v1"
    is_mock = True

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict[str, Any]:
        raise LLMError("The mock provider does not accept free-form prompts")

    def assess_answer(self, ctx: dict[str, Any]) -> dict[str, Any]:
        if _words(ctx["answer"]) < 15:
            return {
                "action": "follow_up",
                "follow_up": "Thanks. Could you give a specific example from your own work: what you personally did, and what the result was?",
                "reason": "short answer",
            }
        return {"action": "next", "follow_up": None, "reason": "answer has enough detail"}

    def clarify(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"text": ctx.get("fallback") or "Of course. Answer from your own experience; a specific example helps most."}

    def evaluate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        job = ctx["job"]
        answers: list[dict[str, Any]] = ctx["answers"]
        match = {m["criterion_id"]: m for m in ctx.get("match", {}).get("criteria", [])}
        by_crit: dict[str, list[dict[str, Any]]] = {}
        by_beh: dict[str, list[dict[str, Any]]] = {}
        for a in answers:
            if a.get("criterion_id"):
                by_crit.setdefault(a["criterion_id"], []).append(a)
            if a.get("behavior_id"):
                by_beh.setdefault(a["behavior_id"], []).append(a)

        def combined(items: list[dict[str, Any]]) -> str:
            return " ".join(" ".join(i["texts"]) for i in items)

        criteria, strengths, gaps, follow_ups = [], [], [], []
        supported, clarify_list, not_covered = [], [], []
        for c in job["essential_criteria"]:
            items = by_crit.get(c["id"], [])
            text = combined(items)
            kw = keywords(f"{c['name']} {c.get('description', '')}")
            rating, status = heuristic_rating(text, kw) if items else (None, "not_assessed")
            refs = [i["q"] for i in items if i["texts"]]
            res_refs = match.get(c["id"], {}).get("refs", [])[:2]
            if status == "rated":
                quote = clip(text, 220)
                evidence = f"Answer: “{quote}”"
            elif status == "insufficient_evidence":
                evidence = "Answer was too brief to rate."
            else:
                evidence = "Not covered in the interview."
            gap = ""
            if status != "rated" or (rating or 0) <= 2:
                gap = f"More evidence needed on {c['name'].lower()}."
            criteria.append({"criterion_id": c["id"], "rating": rating, "status": status, "evidence": evidence, "gap": gap, "refs": refs + res_refs})
            if rating and rating >= 3:
                strengths.append({"text": f"Specific answer on {c['name'].lower()}.", "refs": refs})
                if res_refs:
                    supported.append({"text": f"Resume evidence for {c['name'].lower()} is consistent with the interview answer.", "refs": res_refs + refs})
            elif status == "not_assessed":
                gaps.append({"kind": "not_asked", "text": f"{c['name']} was not covered.", "refs": []})
                if res_refs:
                    not_covered.append({"text": f"Resume lines about {c['name'].lower()} were not discussed.", "refs": res_refs})
            else:
                gaps.append({"kind": "not_demonstrated", "text": f"{c['name']}: limited detail in the answer.", "refs": refs})
                follow_ups.append({"kind": "recruiter_question", "text": f"Ask for a concrete, recent example of {c['name'].lower()} and the candidate's own part in it."})
                if res_refs:
                    clarify_list.append({"text": f"Resume suggests experience with {c['name'].lower()}; the interview answer gave limited detail. Worth asking for an example.", "refs": res_refs + refs})

        questions = []
        for a in answers:
            if not a.get("scored", True):
                continue
            text = " ".join(a["texts"])
            r, st = heuristic_rating(text, keywords(a.get("target", "")))
            questions.append({"q": a["q"], "rating": r, "status": st, "explanation": f"{_words(text)} words; specificity score {_specificity(text)}/3.", "refs": [a["q"]] if text else []})

        behaviors = []
        for b in job.get("behaviors", []):
            if not b.get("enabled"):
                continue
            items = by_beh.get(b["id"], [])
            text = combined(items)
            r, st = heuristic_rating(text, keywords(b.get("anchor", ""))) if items else (None, "not_assessed")
            behaviors.append({"behavior_id": b["id"], "rating": r, "status": st, "note": clip(text, 160) if text else "Not assessed.", "refs": [i["q"] for i in items if i["texts"]]})

        for p in job.get("preferred_criteria", [])[:3]:
            gaps.append({"kind": "unverified", "text": f"Preferred: {p} — not specifically assessed.", "refs": []})
        if any(c["status"] != "rated" or (c["rating"] or 0) <= 2 for c in criteria):
            follow_ups.append({"kind": "technical_interview", "text": "Use the next interview to cover the criteria marked as gaps above."})

        answered = sum(1 for a in answers if a.get("scored", True) and a["texts"])
        scored_total = sum(1 for a in answers if a.get("scored", True)) or 1
        weak = [c for c in criteria if c["status"] != "rated" or (c["rating"] or 0) <= 2]
        if answered < scored_total / 2:
            rec = "insufficient_evidence"
        elif not weak:
            rec = "recommend_proceeding"
        elif len(weak) == 1:
            rec = "proceed_with_follow_up"
        else:
            rec = "additional_assessment"
        rated = [c for c in criteria if c["status"] == "rated" and (c["rating"] or 0) >= 3]
        summary = (
            f"{MOCK_NOTICE} The candidate answered {answered} of {scored_total} scored questions. "
            f"{len(rated)} of {len(criteria)} essential criteria show answers with enough detail to meet the bar under these heuristics."
        )
        return {
            "executive_summary": summary,
            "summary_refs": [a["q"] for a in answers if a["texts"]][:4],
            "criteria": criteria,
            "questions": questions,
            "strengths": strengths,
            "gaps": gaps,
            "resume_crosscheck": {"supported": supported, "needs_clarification": clarify_list, "not_covered": not_covered},
            "behaviors": behaviors,
            "follow_ups": follow_ups,
            "recommendation": rec,
            "recommendation_rationale": f"Mock heuristic: {len(weak)} essential criteria with gaps; {answered}/{scored_total} questions answered.",
        }
