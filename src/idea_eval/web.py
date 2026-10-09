"""FastAPI app: whitelist login, run API with live progress (SSE), and the one-page UI.

Start with: uvicorn idea_eval.web:create_app --factory
"""

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import DUMMY_HASH, LoginLimiter, normalize_email, valid_email, verify_password
from .config import Settings, get_settings
from .db import Database
from .llm import AnthropicLLM
from .nodes import Deps
from .report import to_markdown
from .runs import RunManager
from .search import TavilySearch

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-robots-tag", b"noindex, nofollow"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityHeaders:
    """Pure ASGI middleware, so streaming (SSE) responses are not buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].extend(SECURITY_HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class RunIn(BaseModel):
    idea: str = Field(min_length=8, max_length=2000)
    mode: Literal["cheap", "deep"] = "cheap"
    profile: str = Field(default="", max_length=1000)


def _session_secret(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret
    path = settings.data_dir / ".session_secret"
    if path.exists():
        return path.read_text().strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    path.write_text(secret)
    path.chmod(0o600)
    return secret


def _password_version(user: dict[str, Any]) -> str:
    """Changes when the password changes, which logs out existing sessions."""
    return hashlib.sha256(user["password_hash"].encode()).hexdigest()[:16]


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    deps_factory: Callable[[], Deps] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    db = db or Database(settings.db_path)
    stale = db.fail_stale_runs()
    if stale:
        log.warning("marked %d interrupted runs as failed", stale)

    check_keys = deps_factory is None
    if deps_factory is None:
        def deps_factory() -> Deps:
            return Deps(
                llm=AnthropicLLM(settings),
                search=TavilySearch(settings.tavily_api_key, db, settings.search_cache_days),
                settings=settings,
            )

    manager = RunManager(db, deps_factory, settings.max_concurrent_runs)
    email_limiter = LoginLimiter(max_failures=5, window_s=900)
    ip_limiter = LoginLimiter(max_failures=20, window_s=900)

    app = FastAPI(title="IsThisIdeaGood", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.runs = manager
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(settings),
        session_cookie="itig_session",
        max_age=settings.session_days * 86400,
        same_site="strict",
        https_only=settings.secure_cookies,
    )
    app.add_middleware(SecurityHeaders)

    def current_user(request: Request) -> dict[str, Any]:
        uid = request.session.get("uid")
        user = db.get_user_by_id(uid) if isinstance(uid, int) else None
        if user is None or request.session.get("pwv") != _password_version(user):
            raise HTTPException(status_code=401, detail="Not signed in")
        return user

    CurrentUser = Annotated[dict[str, Any], Depends(current_user)]

    def quota(user: dict[str, Any]) -> dict[str, Any]:
        if user["is_admin"]:
            return {"used": db.runs_this_month(user["id"]), "limit": None}
        limit = user["monthly_limit"]
        if limit is None:
            limit = settings.default_user_monthly_runs
        return {"used": db.runs_this_month(user["id"]), "limit": limit}

    def owned_run(run_id: str, user: dict[str, Any]) -> dict[str, Any]:
        run = db.get_run(run_id)
        if run is None or run["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    # Pages -----------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Auth ------------------------------------------------------------------

    @app.post("/api/login")
    async def login(body: LoginIn, request: Request) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        email = normalize_email(body.email)
        if ip_limiter.blocked(ip) or email_limiter.blocked(email):
            raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
        user = db.get_user(email)
        if user is None:
            await run_in_threadpool(verify_password, body.password, DUMMY_HASH)
            ip_limiter.fail(ip)
            if valid_email(email):
                db.add_to_waitlist(email)
            return {"status": "coming_soon"}
        ok = await run_in_threadpool(verify_password, body.password, user["password_hash"])
        if not ok:
            ip_limiter.fail(ip)
            email_limiter.fail(email)
            raise HTTPException(status_code=401, detail="Access denied")
        email_limiter.reset(email)
        request.session.clear()
        request.session.update(uid=user["id"], pwv=_password_version(user))
        return {"status": "ok", "user": _public_user(user)}

    @app.post("/api/logout")
    async def logout(request: Request) -> dict[str, Any]:
        request.session.clear()
        return {"status": "ok"}

    @app.get("/api/me")
    async def me(user: CurrentUser) -> dict[str, Any]:
        return {
            "user": _public_user(user),
            "quota": quota(user),
            "models": {"cheap": settings.cheap_model, "deep": settings.deep_model},
            "version": __version__,
        }

    # Runs ------------------------------------------------------------------

    @app.post("/api/runs")
    async def create_run(body: RunIn, user: CurrentUser) -> dict[str, Any]:
        if check_keys and not (settings.anthropic_api_key and settings.tavily_api_key):
            raise HTTPException(
                status_code=503, detail="Server is missing ANTHROPIC_API_KEY or TAVILY_API_KEY."
            )
        q = quota(user)
        if q["limit"] is not None and q["used"] >= q["limit"]:
            raise HTTPException(status_code=429, detail="Monthly run limit reached.")
        if (
            not user["is_admin"]
            and settings.monthly_budget_usd > 0
            and db.cost_this_month() >= settings.monthly_budget_usd
        ):
            raise HTTPException(status_code=429, detail="Monthly budget reached.")
        run_id = manager.start(user["id"], body.idea.strip(), body.mode, body.profile.strip())
        return {"id": run_id}

    @app.get("/api/runs")
    async def list_runs(user: CurrentUser) -> dict[str, Any]:
        return {"runs": db.list_runs(user["id"])}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, user: CurrentUser) -> dict[str, Any]:
        run = owned_run(run_id, user)
        run.pop("user_id", None)
        return run

    @app.get("/api/runs/{run_id}/report.md")
    async def report_md(run_id: str, user: CurrentUser) -> PlainTextResponse:
        run = owned_run(run_id, user)
        if not run.get("result"):
            raise HTTPException(status_code=409, detail="Run has no report yet")
        return PlainTextResponse(
            to_markdown(run["result"]),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="idea-{run_id[:8]}.md"'},
        )

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, user: CurrentUser) -> StreamingResponse:
        run = owned_run(run_id, user)

        async def stream():
            if run_id in manager.channels:
                async for event in manager.subscribe(run_id):
                    if event["type"] == "ping":
                        yield ": ping\n\n"
                    else:
                        yield f"data: {json.dumps(event)}\n\n"
                return
            # Finished long ago (or interrupted): answer from the database.
            if run["status"] == "done":
                event = {"type": "done", "run_id": run_id, "result": run["result"]}
            else:
                event = {"type": "error", "msg": run.get("error") or "Run did not finish."}
            yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
