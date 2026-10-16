"""Tests for the manager support: /api/status, session tracking and CORS for the frontend."""

from __future__ import annotations

import time

import httpx
import pytest
from starlette.testclient import TestClient

from chordsmith import __version__, manager, mixjobs, renderjobs, server, singing

pytestmark = pytest.mark.anyio

BASE_URL = "http://localhost:8000"
DEV_ORIGIN = "http://localhost:5173"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http_app(store):
    server.mcp._session_manager = None
    try:
        yield server.build_http_app()
    finally:
        server.mcp._session_manager = None
        manager._tracker = None


@pytest.fixture
def registries(monkeypatch):
    monkeypatch.setattr(renderjobs, "_JOBS", {})
    monkeypatch.setattr(mixjobs, "_JOBS", {})
    monkeypatch.setattr(singing, "_registry", singing._Registry())
    now = time.time()
    render = renderjobs.RenderJob(job_id="rjob_1", status="running", created_at=now - 10)
    render.set_state("Piano", "done")
    render.set_state("Bass", "failed", "no soundfont")
    render.set_state("Melody", "running")
    renderjobs._JOBS[render.job_id] = render
    mix = mixjobs.MixJob(job_id="mjob_1", status="done", created_at=now - 20)
    mix.set_state("backing", "done")
    mixjobs._JOBS[mix.job_id] = mix
    sing = singing.SingingJob(
        job_id="job_1",
        status="running",
        mapping_id="map_1",
        voice_id="voicevox:3",
        settings={},
        cache_key="k",
        created_at=now,
        source="song.mid",
        progress="rendering phrase 2/5",
    )
    singing._registry.jobs[sing.job_id] = sing


async def test_status_lists_jobs_from_every_registry(http_app, registries):
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        health = (await client.get("/healthz")).json()
        status = (await client.get("/api/status")).json()
    assert health == {"status": "ok", "version": __version__}
    assert status["version"] == __version__
    assert status["server"]["auth"] is False
    assert [job["job_id"] for job in status["jobs"]] == ["job_1", "rjob_1", "mjob_1"]
    assert status["active_jobs"] == 2
    render = status["jobs"][1]
    assert render["kind"] == "render" and render["tool"] == "get_render_job"
    assert render["progress"] == {"Piano": "done", "Bass": "failed", "Melody": "running"}
    assert render["errors"] == {"Bass": "no soundfont"}
    assert render["created_at"].endswith("+00:00")
    vocal = status["jobs"][0]
    assert vocal["progress"] == {"vocal": "running"}
    assert vocal["detail"] == "rendering phrase 2/5"
    assert status["jobs"][2]["active"] is False


def test_cors_preflight_and_session_tracking(http_app):
    with TestClient(http_app, base_url=BASE_URL) as client:
        preflight = client.options(
            "/mcp",
            headers={
                "origin": DEV_ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type,mcp-session-id,mcp-protocol-version",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == DEV_ORIGIN

        foreign = client.options(
            "/mcp",
            headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
        )
        assert "access-control-allow-origin" not in foreign.headers

        init = client.post(
            "/mcp",
            headers={**MCP_HEADERS, "origin": DEV_ORIGIN},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "chordsmith-manager", "version": "0.1.0"},
                },
            },
        )
        assert init.status_code == 200
        assert init.headers["access-control-allow-origin"] == DEV_ORIGIN
        assert "mcp-session-id" in init.headers["access-control-expose-headers"].lower()
        session_id = init.headers["mcp-session-id"]

        sessions = client.get("/api/status", headers={"origin": DEV_ORIGIN}).json()["sessions"]
        assert sessions["tracked"] is True
        [row] = sessions["clients"]
        assert row["session_id"] == session_id
        assert row["client_name"] == "chordsmith-manager"
        assert row["protocol_version"] == "2025-06-18"

        closed = client.delete("/mcp", headers={**MCP_HEADERS, "mcp-session-id": session_id})
        assert closed.status_code == 200
        assert client.get("/api/status").json()["sessions"]["clients"] == []


def test_extra_cors_origins_from_env(monkeypatch, store):
    monkeypatch.setenv("CHORDSMITH_CORS_ORIGINS", "https://manager.example.com/, ")
    assert manager.cors_origins_from_env() == ["https://manager.example.com"]
    server.mcp._session_manager = None
    app = manager.wrap_http_app(server.mcp.streamable_http_app(), "/mcp", manager.cors_origins_from_env())
    with TestClient(app, base_url=BASE_URL) as client:
        ok = client.get("/healthz", headers={"origin": "https://manager.example.com"})
        assert ok.headers["access-control-allow-origin"] == "https://manager.example.com"
        prefix = client.get("/healthz", headers={"origin": "https://manager.example.com.evil.net"})
        assert "access-control-allow-origin" not in prefix.headers
    manager._tracker = None
    server.mcp._session_manager = None
