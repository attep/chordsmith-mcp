"""Rate limiting for the MCP HTTP endpoint and the OAuth endpoints.

The MCP specification's security section requires servers to rate limit tool invocations. This
middleware caps POSTs to the configured API paths per client and answers 429 (with Retry-After)
when the bucket is empty. Clients are told apart by their bearer token (hashed, never logged),
so the limit works behind tunnels and proxies where every request shares one source address;
unauthenticated requests fall back to the client address — the real one when the server sits
behind a trusted proxy (CHORDSMITH_TRUST_PROXY=1 lets CF-Connecting-IP / X-Forwarded-For name
the caller; only enable it when nothing but that proxy can reach the port). Stdio transports
are not affected.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import time
from dataclasses import dataclass, field

DEFAULT_RATE_LIMIT = 120  # requests per minute on the MCP endpoint; 0 disables the limiter
DEFAULT_AUTH_RATE_LIMIT = 30  # requests per minute on the OAuth endpoints; 0 disables the limiter
AUTH_ENDPOINT_PATHS = ("/register", "/authorize", "/login", "/token")


@dataclass
class TokenBucketLimiter:
    """Token bucket: ``rate_per_minute`` sustained, up to ``burst`` in a moment."""

    rate_per_minute: int
    burst: int | None = None
    buckets: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, stamp)

    def __post_init__(self) -> None:
        if self.burst is None:
            self.burst = max(1, self.rate_per_minute // 2)

    def allow(self, key: str) -> tuple[bool, float]:
        """Take one token for ``key``; returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        tokens, stamp = self.buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - stamp) * self.rate_per_minute / 60.0)
        if tokens >= 1.0:
            self.buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        self.buckets[key] = (tokens, now)
        return False, (1.0 - tokens) * 60.0 / self.rate_per_minute


def trust_proxy_from_env() -> bool:
    """Read CHORDSMITH_TRUST_PROXY: honor the proxy's client-IP headers for rate-limit keys."""
    raw = os.environ.get("CHORDSMITH_TRUST_PROXY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _header_value(headers, name: bytes) -> str | None:
    for key, value in headers or []:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


def _forwarded_ip(value: str | None) -> str | None:
    """The leading address of a forwarded-for style value, when it parses as an IP."""
    if not value:
        return None
    candidate = value.split(",")[0].strip()
    if not candidate or len(candidate) > 64:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def client_address(scope: dict, trust_proxy: bool = False) -> str:
    """The caller's address: the proxy's client-IP headers when trusted, else the peer address."""
    if trust_proxy:
        headers = scope.get("headers") or []
        forwarded = _forwarded_ip(_header_value(headers, b"cf-connecting-ip")) or _forwarded_ip(
            _header_value(headers, b"x-forwarded-for")
        )
        if forwarded:
            return forwarded
    client = scope.get("client") or ("unknown", 0)
    return str(client[0])


def client_key(scope: dict, trust_proxy: bool = False) -> str:
    """Identify the caller: a hash of the bearer token, or the address when unauthenticated."""
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            return hashlib.sha256(value).hexdigest()[:16]
    return f"addr:{client_address(scope, trust_proxy)}"


def client_key_from_request(request, trust_proxy: bool = False) -> str:
    """The same key from a Starlette request (the sign-in route identifies attempts this way)."""
    client = getattr(request, "client", None)
    scope = {
        "headers": request.headers.raw,
        "client": (client.host if client else "unknown", 0),
    }
    return client_key(scope, trust_proxy)


class RateLimitMiddleware:
    """Raw ASGI middleware: limit POSTs per client on each configured path."""

    def __init__(self, app, limiters: dict[str, TokenBucketLimiter], trust_proxy: bool = False):
        self.app = app
        self.limiters = dict(limiters)
        self.trust_proxy = trust_proxy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            limiter = self.limiters.get(scope.get("path") or "")
            if limiter is not None:
                allowed, retry = limiter.allow(client_key(scope, self.trust_proxy))
                if not allowed:
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32000,
                                "message": f"Rate limit exceeded; retry in {retry:.0f} seconds.",
                            },
                            "id": None,
                        }
                    ).encode()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 429,
                            "headers": [
                                (b"content-type", b"application/json"),
                                (b"retry-after", str(int(retry) + 1).encode()),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} should be a number of requests per minute, got '{raw}'.") from None
    if value < 0:
        raise ValueError(f"{name} cannot be negative; use 0 to disable the limiter.")
    return value


def rate_limit_from_env() -> int:
    """Read CHORDSMITH_RATE_LIMIT (requests per minute on the MCP endpoint; 0 disables)."""
    return _int_from_env("CHORDSMITH_RATE_LIMIT", DEFAULT_RATE_LIMIT)


def auth_rate_limit_from_env() -> int:
    """Read CHORDSMITH_AUTH_RATE_LIMIT (requests per minute on the OAuth endpoints; 0 disables)."""
    return _int_from_env("CHORDSMITH_AUTH_RATE_LIMIT", DEFAULT_AUTH_RATE_LIMIT)
