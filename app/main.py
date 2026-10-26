"""FastAPI application entrypoint (also the Vercel entrypoint: ``app/main.py`` exposes ``app``)."""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.logging_config import configure_logging, redact
from app.security import LoginRequired

log = logging.getLogger("app")
CONFIG_PROBLEMS: list[str] = []

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)


def _config_error_response(request: Request):
    from html import escape

    from fastapi.responses import HTMLResponse

    if request.url.path == "/healthz":
        return JSONResponse({"status": "misconfigured", "problems": CONFIG_PROBLEMS}, status_code=503)
    items = "".join(f"<li>{escape(p)}</li>" for p in CONFIG_PROBLEMS)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Setup needed</title><link rel='stylesheet' href='/static/css/app.css'></head><body>"
        "<main class='auth-wrap'><section class='state-card container-sm'><div class='state-tag'>Setup needed</div>"
        "<h1>The app isn't configured yet</h1><p class='p'>Fix these environment variables in your hosting settings "
        "(Vercel → Project → Settings → Environment Variables), then redeploy:</p>"
        f"<ul class='p'>{items}</ul><p class='small muted'>No secret values are shown here.</p></section></main></body></html>"
    )
    return HTMLResponse(body, status_code=503)


def _bootstrap() -> None:
    from app.database import db_session
    from app.services.bootstrap import ensure_admin, run_migrations

    s = settings()
    if s.should_auto_migrate:
        run_migrations()
    if s.bootstrap_admin_email and s.bootstrap_admin_password:
        try:
            with db_session() as db:
                ensure_admin(db, s.bootstrap_admin_email, s.bootstrap_admin_password)
        except Exception:  # noqa: BLE001 - an admin bootstrap problem must not take the whole app down
            log.exception("bootstrap admin creation failed; the app keeps running")


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = settings()
    configure_logging(s.log_level)
    CONFIG_PROBLEMS.clear()
    CONFIG_PROBLEMS.extend(s.startup_problems())
    if CONFIG_PROBLEMS:
        # Don't serve the app, but explain why instead of crashing with an opaque 500.
        for p in CONFIG_PROBLEMS:
            log.error("configuration problem: %s", p)
        yield
        return
    try:
        _bootstrap()
    except Exception as exc:  # noqa: BLE001 - surface clearly instead of crashing every request
        log.exception("startup bootstrap failed")
        CONFIG_PROBLEMS.append(f"Database setup failed ({type(exc).__name__}). Check DATABASE_URL and that the database is reachable. Details are in the function logs.")
    yield


def create_app() -> FastAPI:
    s = settings()
    app = FastAPI(
        title=s.product_name,
        lifespan=lifespan,
        docs_url=None if s.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        start = time.perf_counter()
        request.state.request_id = secrets.token_hex(6)
        if CONFIG_PROBLEMS and not request.url.path.startswith("/static"):
            response = _config_error_response(request)
        else:
            response = await call_next(request)
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings().cookie_secure:
            h.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        path = request.url.path
        if path.startswith(("/recruiter", "/interview", "/login")):
            h.setdefault("Cache-Control", "no-store")
        if not path.startswith("/static"):
            log.info(
                "request",
                extra={"safe": {
                    "rid": request.state.request_id, "method": request.method, "path": redact(path),
                    "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000),
                }},
            )
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="sw_session",
        max_age=s.session_max_age_hours * 3600,
        same_site="lax",
        https_only=s.cookie_secure,
    )

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        from urllib.parse import quote

        return RedirectResponse(f"/login?next={quote(exc.next_url)}", status_code=303)

    def _wants_json(request: Request) -> bool:
        return "application/json" in request.headers.get("accept", "") or request.headers.get("content-type", "").startswith("application/json")

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        from app.templating import render

        if _wants_json(request):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        titles = {403: "Not allowed", 404: "Page not found", 429: "Slow down", 409: "Already done"}
        return render(request, "error.html", {"title": titles.get(exc.status_code, "Something went wrong"), "detail": exc.detail, "code": exc.status_code}, status_code=exc.status_code)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception):
        from app.templating import render

        if _wants_json(request):
            return JSONResponse({"error": "Not found"}, status_code=404)
        detail = getattr(exc, "detail", None)
        if not isinstance(detail, str) or detail == "Not Found":
            detail = "We couldn't find that page. Check the address, or go back and try again."
        return render(request, "error.html", {"title": "Page not found", "detail": detail, "code": 404}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        from app.templating import render

        if _wants_json(request):
            return JSONResponse({"error": "Some fields were missing or invalid."}, status_code=422)
        return render(request, "error.html", {"title": "Missing information", "detail": "Some fields were missing or invalid. Go back and check the form.", "code": 422}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        from app.templating import render

        log.exception("unhandled error", extra={"safe": {"rid": getattr(request.state, "request_id", ""), "path": redact(request.url.path)}})
        if _wants_json(request):
            return JSONResponse({"error": "Something went wrong on our side. Your work is saved; please try again."}, status_code=500)
        return render(request, "error.html", {"title": "Something went wrong", "detail": "An unexpected error occurred on our side. Please try again in a moment.", "code": 500}, status_code=500)

    from app.routers import auth, interview, public, recruiter, system

    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(public.router)
    app.include_router(interview.router)
    app.include_router(recruiter.router)

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
