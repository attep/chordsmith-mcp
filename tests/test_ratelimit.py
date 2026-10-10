"""Tests for the HTTP rate limiter (token bucket + ASGI middleware)."""

from __future__ import annotations

import json

import pytest

from chordsmith import ratelimit

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_limiter_burst_then_refill(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    limiter = ratelimit.TokenBucketLimiter(60, burst=2)
    assert limiter.allow("a") == (True, 0.0)
    assert limiter.allow("a") == (True, 0.0)
    allowed, retry = limiter.allow("a")
    assert not allowed and 0.5 < retry <= 1.0
    now[0] += 1.0  # 60/min = one token per second
    assert limiter.allow("a")[0]


def test_limiter_keys_are_isolated(monkeypatch):
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: 1000.0)
    limiter = ratelimit.TokenBucketLimiter(60, burst=1)
    assert limiter.allow("a")[0]
    assert not limiter.allow("a")[0]
    assert limiter.allow("b")[0]


def test_client_key_prefers_the_bearer_token():
    scope = {"headers": [(b"authorization", b"Bearer secret-one")], "client": ("1.2.3.4", 1)}
    other = {"headers": [(b"authorization", b"Bearer secret-two")], "client": ("1.2.3.4", 2)}
    anonymous = {"headers": [], "client": ("1.2.3.4", 3)}
    assert ratelimit.client_key(scope) == ratelimit.client_key(scope)
    assert ratelimit.client_key(scope) != ratelimit.client_key(other)
    assert ratelimit.client_key(anonymous).startswith("addr:")
    assert "secret" not in ratelimit.client_key(scope)  # the token itself is never the key


class _App:
    def __init__(self):
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _run(middleware, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _scope(method="POST", path="/mcp", headers=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": ("1.2.3.4", 1),
    }


async def test_middleware_rejects_with_429_when_empty():
    app = _App()
    middleware = ratelimit.RateLimitMiddleware(app, ratelimit.TokenBucketLimiter(60, burst=1), "/mcp")
    first = await _run(middleware, _scope())
    assert first[0]["status"] == 200
    second = await _run(middleware, _scope())
    assert second[0]["status"] == 429
    headers = dict(second[0]["headers"])
    assert int(headers[b"retry-after"]) >= 1
    body = json.loads(second[1]["body"])
    assert "Rate limit exceeded" in body["error"]["message"]


async def test_middleware_passes_through_other_requests():
    app = _App()
    middleware = ratelimit.RateLimitMiddleware(app, ratelimit.TokenBucketLimiter(60, burst=1), "/mcp")
    for scope in (
        _scope(method="GET"),
        _scope(path="/healthz"),
        _scope(headers=[(b"authorization", b"Bearer a")]),
        {"type": "lifespan"},
    ):
        sent = await _run(middleware, scope)
        assert sent[0]["status"] == 200
    assert app.scopes == ["http", "http", "http", "lifespan"]


async def test_middleware_limits_per_token():
    app = _App()
    middleware = ratelimit.RateLimitMiddleware(app, ratelimit.TokenBucketLimiter(60, burst=1), "/mcp")
    first = _scope(headers=[(b"authorization", b"Bearer one")])
    same = _scope(headers=[(b"authorization", b"Bearer one")])
    other = _scope(headers=[(b"authorization", b"Bearer two")])
    assert (await _run(middleware, first))[0]["status"] == 200
    assert (await _run(middleware, same))[0]["status"] == 429
    assert (await _run(middleware, other))[0]["status"] == 200


def test_rate_limit_from_env(monkeypatch):
    monkeypatch.delenv("CHORDSMITH_RATE_LIMIT", raising=False)
    assert ratelimit.rate_limit_from_env() == ratelimit.DEFAULT_RATE_LIMIT
    monkeypatch.setenv("CHORDSMITH_RATE_LIMIT", "30")
    assert ratelimit.rate_limit_from_env() == 30
    monkeypatch.setenv("CHORDSMITH_RATE_LIMIT", "0")
    assert ratelimit.rate_limit_from_env() == 0
    monkeypatch.setenv("CHORDSMITH_RATE_LIMIT", "often")
    with pytest.raises(ValueError, match="requests per minute"):
        ratelimit.rate_limit_from_env()
    monkeypatch.setenv("CHORDSMITH_RATE_LIMIT", "-5")
    with pytest.raises(ValueError, match="negative"):
        ratelimit.rate_limit_from_env()
