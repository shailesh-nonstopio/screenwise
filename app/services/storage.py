"""File storage for uploaded resumes.

Two backends:
* ``database`` (default): bytes live in the ``stored_files`` table. Works on serverless hosts.
* ``local``: files live in a private directory outside the web root (good for a VM / Docker).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import StoredFile


class StorageError(RuntimeError):
    pass


def _new_key() -> str:
    return secrets.token_hex(16)


def save(db: Session, content: bytes, mime: str) -> str:
    key = _new_key()
    backend = settings().storage_backend
    if backend == "database":
        db.add(
            StoredFile(
                id=key, content=content, mime=mime, size=len(content), sha256=hashlib.sha256(content).hexdigest()
            )
        )
        return f"db:{key}"
    root = Path(settings().local_storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / key  # random name: never derived from the user's filename
    with open(path, "wb") as fh:
        fh.write(content)
    os.chmod(path, 0o600)
    return f"local:{key}"


def load(db: Session, file_key: str) -> bytes:
    backend, _, key = file_key.partition(":")
    if not key or not all(c in "0123456789abcdef" for c in key):
        raise StorageError("Invalid file key")
    if backend == "db":
        row = db.get(StoredFile, key)
        if row is None:
            raise StorageError("File not found")
        return row.content
    if backend == "local":
        path = Path(settings().local_storage_dir).resolve() / key
        if not path.is_file():
            raise StorageError("File not found")
        return path.read_bytes()
    raise StorageError("Unknown storage backend")


def delete(db: Session, file_key: str | None) -> None:
    if not file_key:
        return
    backend, _, key = file_key.partition(":")
    if backend == "db":
        row = db.get(StoredFile, key)
        if row is not None:
            db.delete(row)
    elif backend == "local" and key and all(c in "0123456789abcdef" for c in key):
        path = Path(settings().local_storage_dir).resolve() / key
        path.unlink(missing_ok=True)
