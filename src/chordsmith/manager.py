"""Manager/inspector support for HTTP transports: health, status, sessions and CORS.

``register(mcp, store_provider)`` adds ``GET /api/status`` (version, every job in the render,
mix and singing registries, and the connected MCP sessions). ``wrap_http_app`` adds CORS for
local dev origins (the ``frontend/`` manager UI) and the session tracker that feeds the status.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from chordsmith import __version__, mixjobs, renderjobs, singing
from chordsmith.storage import FileStore

# Any port on the loopback names, so `npm run dev` (5173) or a preview build (4173) can connect.
LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
]
CORS_EXPOSE_HEADERS = ["mcp-session-id", "mcp-protocol-version", "www-authenticate"]
MAX_INITIALIZE_BODY_BYTES = 64 * 1024
# A session whose GET event stream closed (tab closed, client gone) and that sent nothing for
# this long is no longer listed, even though the SDK keeps it until it times out.
STREAM_GRACE_SECONDS = 10
ACTIVE_STATES = ("queued", "running")


def health_payload() -> dict[str, Any]:
    """The body of GET /healthz (also embedded in /api/status)."""
    return {"status": "ok", "version": __version__}


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ jobs


def _tracked_job(kind: str, tool: str, job) -> dict[str, Any]:
    with job.lock:
        progress = dict(job.progress)
        errors = dict(job.errors)
    return {
        "kind": kind,
        "tool": tool,
        "job_id": job.job_id,
        "status": job.status,
        "active": job.status in ACTIVE_STATES,
        "created_at": _iso(job.created_at),
        "progress": progress,
        "errors": errors,
        "error": job.error,
    }


_SINGING_STATE = {"queued": "pending", "running": "running", "done": "done", "failed": "failed"}


def _singing_job(job) -> dict[str, Any]:
    # Singing jobs render one vocal take, so their progress matrix has a single "vocal" cell;
    # the engine's free-text progress ("rendering phrase 3/8") goes in `detail`.
    return {
        "kind": "singing",
        "tool": "get_singing_job",
        "job_id": job.job_id,
        "status": job.status,
        "active": job.status in ACTIVE_STATES,
        "created_at": _iso(job.created_at),
        "progress": {"vocal": _SINGING_STATE.get(job.status, job.status)},
        "detail": job.progress,
        "errors": {"vocal": job.error} if job.error else {},
        "error": job.error,
        "source": job.source or None,
        "voice_id": job.voice_id,
    }


def job_summaries() -> list[dict[str, Any]]:
    """Every job in the three registries (they keep 24 hours), newest first."""
    with renderjobs._JOBS_LOCK:  # noqa: SLF001 - read-only view of the module registries
        render = list(renderjobs._JOBS.values())  # noqa: SLF001
    with mixjobs._JOBS_LOCK:  # noqa: SLF001
        mix = list(mixjobs._JOBS.values())  # noqa: SLF001
    with singing._registry.lock:  # noqa: SLF001
        sing = list(singing._registry.jobs.values())  # noqa: SLF001
    rows = [(job.created_at, _tracked_job("render", "get_render_job", job)) for job in render]
    rows += [(job.created_at, _tracked_job("mix", "get_mix_job", job)) for job in mix]
    rows += [(job.created_at, _singing_job(job)) for job in sing]
    rows.sort(key=lambda row: row[0], reverse=True)
    return [row[1] for row in rows]


# ------------------------------------------------------------------ sessions


class SessionTracker:
    """Raw ASGI middleware that remembers which MCP client opened each streamable-HTTP session.

    FastMCP does not expose its sessions' client info, so the tracker reads the (small)
    ``initialize`` request body, pairs it with the ``mcp-session-id`` response header, and
    forgets the session on DELETE. Requests that already carry a session id are not buffered.
    Open GET event streams are counted so sessions whose client went away without a DELETE
    (e.g. a closed browser tab) can be left out.
    """

    def __init__(self, app, path: str):
        self.app = app
        self.path = path
        self.sessions: dict[str, dict[str, Any]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        session_id = _header(scope, b"mcp-session-id")
        method = scope.get("method")
        if session_id:
            record = self.sessions.get(session_id)
            stream = method == "GET" and record is not None
            if record is not None:
                record["last_seen"] = time.time()
                record["requests"] += 1
                if stream:
                    record["streams"] += 1
            try:
                await self.app(scope, receive, send)
            finally:
                if stream:
                    record["streams"] -= 1
                    record["stream_closed_at"] = time.time()
            if method == "DELETE":
                self.sessions.pop(session_id, None)
            return
        if method != "POST":
            await self.app(scope, receive, send)
            return
        body, replay = await _buffer(receive, MAX_INITIALIZE_BODY_BYTES)
        client = _initialize_params(body)
        if client is None:
            await self.app(scope, replay, send)
            return

        async def capture(message) -> None:
            if message["type"] == "http.response.start" and message.get("status", 500) < 400:
                new_id = next(
                    (v.decode() for k, v in message.get("headers", []) if k.lower() == b"mcp-session-id"),
                    None,
                )
                if new_id:
                    now = time.time()
                    self.sessions[new_id] = {
                        **client,
                        "connected_at": now,
                        "last_seen": now,
                        "requests": 1,
                        "streams": 0,
                        "stream_closed_at": None,
                    }
            await send(message)

        await self.app(scope, replay, capture)

    def snapshot(self, live_ids: set[str] | None) -> list[dict[str, Any]]:
        """Tracked sessions (dropping ones the SDK has closed) plus live ones seen before tracking."""
        if live_ids is not None:
            for gone in [key for key in self.sessions if key not in live_ids]:
                del self.sessions[gone]
        now = time.time()
        lost = {key for key, record in self.sessions.items() if _stream_lost(record, now)}
        ids = (set(self.sessions) | (live_ids or set())) - lost
        rows = []
        for session_id in ids:
            record = self.sessions.get(session_id, {})
            rows.append(
                {
                    "session_id": session_id,
                    "client_name": record.get("client_name"),
                    "client_version": record.get("client_version"),
                    "protocol_version": record.get("protocol_version"),
                    "connected_at": _iso(record.get("connected_at")),
                    "last_seen": _iso(record.get("last_seen")),
                    "requests": record.get("requests"),
                }
            )
        rows.sort(key=lambda row: row["connected_at"] or "", reverse=True)
        return rows


def _stream_lost(record: dict[str, Any], now: float) -> bool:
    closed_at = record.get("stream_closed_at")
    if closed_at is None or record.get("streams", 0) > 0:
        return False
    return now - max(closed_at, record.get("last_seen") or 0) > STREAM_GRACE_SECONDS


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _buffer(receive, limit: int):
    """Read the request body (up to ``limit`` bytes to inspect) and return a replaying receive."""
    messages = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        size += len(message.get("body", b""))
        if not message.get("more_body") or size > limit:
            break
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.request")
    complete = messages[-1]["type"] != "http.request" or not messages[-1].get("more_body")
    pending = list(messages)

    async def replay():
        if pending:
            return pending.pop(0)
        return await receive()

    return (body if complete and size <= limit else b""), replay


def _initialize_params(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("method") != "initialize":
        return None
    params = payload.get("params") or {}
    info = params.get("clientInfo") or {}
    return {
        "client_name": info.get("name"),
        "client_version": info.get("version"),
        "protocol_version": params.get("protocolVersion"),
    }


_tracker: SessionTracker | None = None


def _live_session_ids(mcp: FastMCP) -> set[str] | None:
    manager = getattr(mcp, "_session_manager", None)
    instances = getattr(manager, "_server_instances", None)
    if not isinstance(instances, dict):
        return None
    return set(instances)


def session_summary(mcp: FastMCP) -> dict[str, Any]:
    stateless = bool(mcp.settings.stateless_http)
    live = _live_session_ids(mcp)
    clients = _tracker.snapshot(live) if _tracker is not None else []
    if _tracker is None and live:
        clients = SessionTracker(None, "").snapshot(live)
    return {
        "tracked": _tracker is not None and not stateless,
        "stateless": stateless,
        "count": len(clients),
        "clients": clients,
    }


# ------------------------------------------------------------------ routes and app wrapping


async def _authorized(mcp: FastMCP, request: Request) -> bool:
    """With OAuth on, /api/status needs the same bearer token as the MCP endpoint."""
    verifier = getattr(mcp, "_token_verifier", None)
    if verifier is None or mcp.settings.auth is None:
        return True
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend

    resource = mcp.settings.auth.resource_server_url if mcp.settings.auth.validate_token_resource else None
    result = await BearerAuthBackend(verifier, resource_server_url=resource).authenticate(request)
    if result is None:
        return False
    credentials, _user = result
    return all(scope in credentials.scopes for scope in mcp.settings.auth.required_scopes or [])


def register(mcp: FastMCP, store_provider: Callable[[], FileStore]) -> None:
    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> Response:
        """Server version, the render/mix/singing jobs and the connected MCP sessions."""
        if not await _authorized(mcp, request):
            return JSONResponse(
                {"error": "Sign in first: send the MCP bearer token."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        jobs = job_summaries()
        return JSONResponse(
            {
                **health_payload(),
                "server": {
                    "name": mcp.name,
                    "version": __version__,
                    "mcp_path": mcp.settings.streamable_http_path,
                    "auth": mcp.settings.auth is not None,
                    "output_dir": str(store_provider().root),
                },
                "jobs": jobs,
                "active_jobs": sum(1 for job in jobs if job["active"]),
                "sessions": session_summary(mcp),
            }
        )


def cors_origins_from_env() -> list[str]:
    """Extra allowed origins (comma-separated CHORDSMITH_CORS_ORIGINS) on top of localhost."""
    raw = os.environ.get("CHORDSMITH_CORS_ORIGINS", "")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def wrap_http_app(app, path: str, extra_origins: list[str] | None = None):
    """Add session tracking and CORS (localhost dev origins plus ``extra_origins``)."""
    global _tracker
    _tracker = SessionTracker(app, path)
    pattern = LOCAL_ORIGIN_REGEX
    if extra_origins:
        pattern = "|".join([LOCAL_ORIGIN_REGEX, *(f"^{re.escape(origin)}$" for origin in extra_origins)])
    return CORSMiddleware(
        _tracker,
        allow_origin_regex=pattern,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
        max_age=600,
    )
