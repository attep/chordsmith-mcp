"""Signed, expiring download URLs for generated files.

The ``/files/{filename}`` route serves a file only when the request carries a valid
``expires``/``token`` pair, where the token is an HMAC of the file name and expiry. The signing
secret lives in the state directory (``url_secret``) so links survive restarts; if it cannot be
persisted, a fresh secret is generated and old links stop working.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

DOWNLOAD_PATH = "/files"
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600

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
