"""Rate limiting for the MCP HTTP endpoint.

The MCP specification's security section requires servers to rate limit tool invocations. This
middleware caps POSTs to the streamable-http path per client and answers 429 (with Retry-After)
when the bucket is empty. Clients are told apart by their bearer token (hashed, never logged),
so the limit works behind tunnels and proxies where every request shares one source address;
unauthenticated requests fall back to the client address. Stdio transports are not affected.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field

DEFAULT_RATE_LIMIT = 120  # requests per minute; 0 disables the limiter


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


def client_key(scope: dict) -> str:
    """Identify the caller: a hash of the bearer token, or the address when unauthenticated."""
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            return hashlib.sha256(value).hexdigest()[:16]
    client = scope.get("client") or ("unknown", 0)
    return f"addr:{client[0]}"


class RateLimitMiddleware:
    """Raw ASGI middleware: limit POSTs to the MCP endpoint per client."""

    def __init__(self, app, limiter: TokenBucketLimiter, path: str):
        self.app = app
        self.limiter = limiter
        self.path = path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == self.path:
            allowed, retry = self.limiter.allow(client_key(scope))
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


def rate_limit_from_env() -> int:
    """Read CHORDSMITH_RATE_LIMIT (requests per minute; 0 disables)."""
    raw = os.environ.get("CHORDSMITH_RATE_LIMIT", str(DEFAULT_RATE_LIMIT)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"CHORDSMITH_RATE_LIMIT should be a number of requests per minute, got '{raw}'."
        ) from None
    if value < 0:
        raise ValueError("CHORDSMITH_RATE_LIMIT cannot be negative; use 0 to disable the limiter.")
    return value
