"""Signed, expiring download URLs for generated files.

The ``/files/{filename}`` route serves a file only when the request carries a valid
``expires``/``token`` pair, where the token is an HMAC of the file name and expiry. The signing
secret lives in the state directory (``url_secret``) so links survive restarts; if it cannot be
persisted, a fresh secret is generated and old links stop working.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import Field

logger = logging.getLogger(__name__)

DOWNLOAD_PATH = "/files"
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600

ReturnAs = Annotated[
    Literal["base64", "url"],
    Field(
        description="base64 returns the bytes in the response (works everywhere); url returns a "
        "signed download link that expires (needs CHORDSMITH_PUBLIC_URL)."
    ),
]

_secret: bytes | None = None


def state_dir() -> Path:
    env = os.environ.get("CHORDSMITH_STATE_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".chordsmith"


def get_secret() -> bytes:
    """Return the signing secret, creating and persisting it on first use."""
    global _secret
    if _secret is None:
        path = state_dir() / "url_secret"
        try:
            if path.is_file():
                _secret = path.read_bytes().strip() or None
            if _secret is None:
                _secret = secrets.token_bytes(32)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_secret)
                if os.name == "posix":
                    path.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not persist the download secret (%s); links expire on restart", exc)
            _secret = secrets.token_bytes(32)
    return _secret


def sign(filename: str, expires: int, secret: bytes | None = None) -> str:
    key = secret if secret is not None else get_secret()
    return hmac.new(key, f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()


def verify(filename: str, expires: str | int, token: str, secret: bytes | None = None) -> bool:
    try:
        expires_int = int(expires)
    except (TypeError, ValueError):
        return False
    if expires_int < time.time():
        return False
    return hmac.compare_digest(sign(filename, expires_int, secret), token)


def build_url(
    public_url: str, filename: str, ttl: int = DEFAULT_TTL_SECONDS, secret: bytes | None = None
) -> tuple[str, int]:
    """Return (signed URL, expires_at as a Unix timestamp)."""
    ttl = max(30, min(MAX_TTL_SECONDS, ttl))
    expires = int(time.time()) + ttl
    token = sign(filename, expires, secret)
    url = f"{public_url.rstrip('/')}{DOWNLOAD_PATH}/{quote(filename)}?expires={expires}&token={token}"
    return url, expires


def public_url() -> str | None:
    return os.environ.get("CHORDSMITH_PUBLIC_URL", "").strip().rstrip("/") or None


def deliver_file(
    filename: str, data: bytes, return_as: str, expires_in: int = DEFAULT_TTL_SECONDS
) -> dict[str, Any]:
    """Shape a file for tool-only clients: base64 bytes or a signed, expiring download URL."""
    result: dict[str, Any] = {
        "filename": filename,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if return_as == "base64":
        result["data_base64"] = base64.b64encode(data).decode()
    elif return_as == "url":
        base = public_url()
        if base is None:
            raise ValueError(
                "No public URL is configured, so download links cannot be built; use "
                "return_as='base64' or set CHORDSMITH_PUBLIC_URL."
            )
        url, expires_at = build_url(base, filename, expires_in)
        result["download_url"] = url
        result["expires_at"] = expires_at
    else:
        raise ValueError("return_as must be 'base64' or 'url'.")
    return result
