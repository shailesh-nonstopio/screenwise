"""Structured JSON logging with redaction of sensitive values."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_REDACTIONS = [
    (re.compile(r"(/interview/)[A-Za-z0-9_\-]{16,}"), r"\1[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(api[_-]?key|password|secret|token)(\"?\s*[:=]\s*\"?)[^\s\",]+"), r"\1\2[redacted]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "[redacted-key]"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "safe", None)
        if isinstance(extra, dict):
            payload.update({k: redact(str(v)) for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Covers third-party loggers (e.g. uvicorn access logs that include URL paths).
        if record.args:
            try:
                record.msg = redact(record.getMessage())
                record.args = ()
            except Exception:  # pragma: no cover
                pass
        else:
            record.msg = redact(str(record.msg))
        return True


_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.addFilter(RedactingFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _configured = True
