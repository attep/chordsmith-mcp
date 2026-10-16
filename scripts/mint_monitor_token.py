"""Mint an OAuth token pair for a ChordSmith server so the manager UI can monitor it.

Signs in through the server's own OAuth flow (dynamic registration, PKCE, the sign-in page's
password POST) and writes ``monitor-token.json`` with the client id the token pair belongs to.
Paste the server URL, client id and refresh token into the manager UI's Settings; the UI then
renews the short-lived access tokens on its own (the refresh token rotates on every use and
lasts about 30 days).

The password is read from ``CHORDSMITH_AUTH_PASSWORD``, ``--password``, or (by default) the
environment of a running Docker container (``--container``), so it never has to be typed or
printed. The token file is a secret: keep it out of the repository.

Usage:
    python scripts/mint_monitor_token.py --server https://your-host.example.com --out token.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

REDIRECT = "http://localhost:5173/oauth/callback"


def password_from_container(container: str) -> str:
    raw = subprocess.run(
        ["docker", "inspect", container, "--format", "{{json .Config.Env}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for entry in json.loads(raw):
        key, _, value = entry.partition("=")
        if key == "CHORDSMITH_AUTH_PASSWORD":
            return value
    raise SystemExit(f"CHORDSMITH_AUTH_PASSWORD not found in container '{container}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", required=True, help="Public server URL, e.g. https://name.trycloudflare.com")
    parser.add_argument("--out", default="monitor-token.json", help="Where to write the token file.")
    parser.add_argument("--container", default="chordsmith", help="Docker container to read the password from.")
    parser.add_argument(
        "--password",
        default=None,
        help="The sign-in password (default: CHORDSMITH_AUTH_PASSWORD env, else the container env).",
    )
    args = parser.parse_args()
    server = args.server.rstrip("/")
    import os

    password = args.password or os.environ.get("CHORDSMITH_AUTH_PASSWORD") or password_from_container(args.container)

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resource = (client.get(f"{server}/.well-known/oauth-protected-resource/mcp").json()).get("resource")
        registration = client.post(
            f"{server}/register",
            json={
                "client_name": "chordsmith-manager-monitor",
                "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "chordsmith",
            },
        )
        if registration.status_code not in (200, 201):
            print("register failed:", registration.status_code, registration.text[:300])
            return 1
        registered = registration.json()
        client_id = registered["client_id"]
        client_secret = registered.get("client_secret")

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "chordsmith",
        }
        if resource:
            params["resource"] = resource
        authorize = client.get(f"{server}/authorize", params=params, follow_redirects=False)
        location = authorize.headers.get("location", "")
        if authorize.status_code not in (302, 303, 307) or "/login" not in location:
            print("unexpected authorize response:", authorize.status_code, location or authorize.text[:300])
            return 1
        request_id = parse_qs(urlparse(location).query).get("request_id", [""])[0]

        login = client.post(
            f"{server}/login", data={"request_id": request_id, "password": password}, follow_redirects=False
        )
        location = login.headers.get("location", "")
        if login.status_code not in (302, 303, 307):
            print("sign-in failed:", login.status_code, "(wrong password or locked out?)")
            return 1
        query = parse_qs(urlparse(location).query)
        code = query.get("code", [""])[0]
        if not code or query.get("state", [""])[0] != state:
            print("no authorization code in the sign-in redirect.")
            return 1

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        if client_secret:
            form["client_secret"] = client_secret
        if resource:
            form["resource"] = resource
        tokens = client.post(f"{server}/token", data=form)
        if tokens.status_code != 200:
            print("token exchange failed:", tokens.status_code, tokens.text[:300])
            return 1
        payload = tokens.json()

        headers = {"Authorization": f"Bearer {payload['access_token']}", "Origin": REDIRECT.rsplit("/oauth", 1)[0]}
        status = client.get(f"{server}/api/status", headers=headers)
        summary = (
            f"api/status: HTTP {status.status_code}"
            + (f" (version {status.json()['version']})" if status.status_code == 200 else "")
        )

    out = Path(args.out)
    out.write_text(
        json.dumps(
            {
                "server_url": server,
                "mcp_path": "/mcp",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": payload.get("refresh_token", ""),
                "access_token": payload["access_token"],
                "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"signed in; {summary}")
    print(f"token file written: {out.resolve()}")
    print("Paste server_url, client_id and refresh_token into the manager UI's Settings.")
    print("The file is a secret - do not commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
