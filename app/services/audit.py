"""Audit trail. Metadata must never contain resumes, answers, tokens or secrets."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

_FORBIDDEN_KEYS = {"token", "password", "resume", "answer", "content", "api_key", "secret", "body"}


def _safe(meta: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (meta or {}).items():
        if any(f in k.lower() for f in _FORBIDDEN_KEYS):
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)[:200]
    return out


def record(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: Any,
    *,
    actor_type: str = "system",
    actor_id: Any = "",
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        actor_type=actor_type,
        actor_id=str(actor_id or ""),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        safe_metadata=_safe(metadata),
    )
    db.add(ev)
    return ev
