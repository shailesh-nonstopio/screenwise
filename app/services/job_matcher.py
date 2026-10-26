"""Transparent, deterministic comparison of resume lines with job criteria.

The output is a starting point for the interview plan, never a score: a criterion that is not
found in the resume becomes something to ask about.
"""

from __future__ import annotations

from typing import Any

from app.services.text_utils import clip, keywords, overlap

STATUS_LABELS = {
    "evidence": "Evidence in resume",
    "partial": "Partial — probe in interview",
    "not_found": "Not found — ask in interview",
}


def _best_lines(kw: set[str], lines: list[dict[str, str]], top: int = 2) -> list[tuple[int, dict[str, str], set[str]]]:
    scored = []
    for ln in lines:
        hit = overlap(kw, ln["text"])
        if hit:
            scored.append((len(hit), ln, hit))
    scored.sort(key=lambda x: (-x[0], int(x[1]["ref"].split("-")[1])))
    return scored[:top]


def match(job_essentials: list[dict[str, Any]], preferred: list[str], lines: list[dict[str, str]]) -> dict[str, Any]:
    criteria = []
    for crit in job_essentials:
        kw = keywords(f"{crit.get('name', '')} {crit.get('description', '')}")
        best = _best_lines(kw, lines)
        if best and best[0][0] >= 2:
            status = "evidence"
        elif best:
            status = "partial"
        else:
            status = "not_found"
        if best:
            evidence = " ".join(f"{ln['ref']}: “{clip(ln['text'], 140)}”" for _, ln, _ in best)
        else:
            evidence = "No matching resume lines."
        criteria.append(
            {
                "criterion_id": crit["id"],
                "name": crit.get("name", ""),
                "status": status,
                "status_label": STATUS_LABELS[status],
                "refs": [ln["ref"] for _, ln, _ in best],
                "evidence": evidence,
                "matched_terms": sorted({t for _, _, hit in best for t in hit})[:8],
            }
        )
    pref = []
    for name in preferred:
        best = _best_lines(keywords(name), lines, top=1)
        pref.append({"name": name, "found": bool(best), "refs": [ln["ref"] for _, ln, _ in best]})
    return {"criteria": criteria, "preferred": pref, "method": "keyword overlap (deterministic)"}
