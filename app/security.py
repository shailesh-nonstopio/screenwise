"""Authentication, CSRF, invitation tokens and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

_hasher = PasswordHasher()

# ---------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _hasher.hash("timing-equaliser")


def burn_password_check() -> None:
    """Spend the same time as a real check so unknown emails are not distinguishable."""
    verify_password(_DUMMY_HASH, "not-the-password")


def validate_new_password(password: str) -> str | None:
    if len(password) < 12:
        return "Use at least 12 characters."
    if password.lower() == password and password.isalpha():
        return "Mix in numbers, symbols or capital letters."
    return None


# ---------------------------------------------------------------------------- invitation tokens


def new_invitation_token() -> str:
    """256 bits of randomness, URL-safe. Only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Keyed hash so a database leak alone cannot be used to test guesses offline.
    return hmac.new(settings().secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------- auth


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/recruiter") -> None:
        self.next_url = next_url


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.pop("user_id", None)
        return None
    return user


def current_user(request: Request, user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        nxt = request.url.path if request.method == "GET" else "/recruiter"
        raise LoginRequired(nxt)
    request.state.user = user
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user


def login_session(request: Request, user: User) -> None:
    request.session.clear()  # prevent session fixation
    request.session["user_id"] = user.id
    request.session["csrf"] = secrets.token_urlsafe(24)
    request.session["login_at"] = int(time.time())


# ---------------------------------------------------------------------------- CSRF


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()  # cached by Starlette; endpoints can read it again
            supplied = str(form.get("csrf_token") or "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        raise HTTPException(status_code=403, detail="Your session expired or the form is out of date. Reload the page and try again.")


# ---------------------------------------------------------------------------- rate limiting


class RateLimiter:
    """Small in-process sliding-window limiter.

    On serverless platforms each instance keeps its own window, so this is a best-effort
    brake against bursts. Put a shared limiter (e.g. Vercel Firewall rules or Redis) in front
    for strict guarantees.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory
                self._hits.clear()
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def rate_limit(bucket: str, per_minute: int | None = None) -> Callable[[Request], None]:
    def _dep(request: Request) -> None:
        limit = per_minute or settings().rate_limit_per_minute
        if not limiter.hit(f"{bucket}:{client_ip(request)}", limit, 60.0):
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute and try again.")

    return _dep
