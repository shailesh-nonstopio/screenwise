"""Small text helpers shared by the matcher, planner and mock provider."""

from __future__ import annotations

import re

STOPWORDS = {
    "a", "an", "and", "the", "of", "or", "to", "in", "on", "for", "with", "by", "at", "as", "is", "are", "be",
    "from", "into", "their", "they", "this", "that", "these", "those", "it", "its", "you", "your", "we", "our",
    "who", "what", "how", "why", "when", "can", "will", "able", "ability", "experience", "experienced",
    "strong", "good", "knowledge", "understanding", "work", "working", "using", "use", "including", "etc",
    "similar", "other", "skills", "skill", "years", "year", "plus", "related", "relevant", "e.g", "eg", "such",
    "across", "within", "level", "role", "roles", "team", "teams", "new", "make", "makes", "well",
}

SYNONYMS: dict[str, set[str]] = {
    "api": {"apis", "rest", "endpoint", "endpoints", "fastapi", "django", "flask"},
    "python": {"pandas", "django", "fastapi", "flask", "numpy"},
    "postgresql": {"postgres", "sql", "database", "schema", "schemas"},
    "relational": {"sql", "postgres", "postgresql", "mysql", "schema"},
    "debugging": {"debug", "incident", "incidents", "on-call", "observability", "troubleshooting"},
    "reliability": {"on-call", "uptime", "incident", "incidents", "monitoring", "grafana", "observability"},
    "mentoring": {"mentor", "mentored", "mentors", "coaching", "coach"},
    "review": {"reviews", "reviewing", "reviewed"},
    "dashboards": {"dashboard", "looker", "tableau", "powerbi", "bi"},
    "stakeholder": {"stakeholders", "communication", "presented", "presenting"},
    "testing": {"test", "tests", "pytest", "experiment", "experiments"},
    "leadership": {"led", "lead", "leading", "managed", "owner", "ownership"},
    "ownership": {"led", "owned", "owner", "lead", "drove"},
    "customer": {"customers", "client", "clients", "account", "accounts"},
}

_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.\-]*")


def tokens(text: str) -> list[str]:
    return [t.lower().strip(".-") for t in _WORD.findall(text or "")]


def keywords(text: str) -> set[str]:
    out = {t for t in tokens(text) if len(t) > 2 and t not in STOPWORDS}
    expanded = set(out)
    for k in out:
        expanded |= SYNONYMS.get(k, set())
    return expanded


def overlap(a: set[str], text: str) -> set[str]:
    return a & set(tokens(text))


def clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
