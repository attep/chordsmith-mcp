"""Tests for the built-in OAuth authorization server."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.testclient import TestClient

from chordsmith import auth, server
from chordsmith.auth import (
    MAX_FAILED_LOGINS_PER_MINUTE,
    PENDING_REQUEST_TTL_SECONDS,
    AuthConfig,
    LoginFailed,
    PasswordOAuthProvider,
    enable_auth,
)

BASE_URL = "http://127.0.0.1:8000"
RESOURCE_URL = f"{BASE_URL}/mcp"
REDIRECT_URI = "http://127.0.0.1:9999/callback"
PASSWORD = "s3cret-test-password"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _provider(tmp_path) -> PasswordOAuthProvider:
    config = AuthConfig(public_url=BASE_URL, password=PASSWORD, state_dir=tmp_path)
    return PasswordOAuthProvider(config, RESOURCE_URL)


def _client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        token_endpoint_auth_method="none",
        client_name="pytest",
    )


def _params(challenge: str):
    from mcp.server.auth.provider import AuthorizationParams

    return AuthorizationParams(
        state="state-123",
        scopes=["chordsmith"],
        code_challenge=challenge,
        redirect_uri=AnyUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE_URL,
    )


async def _issue_tokens(provider: PasswordOAuthProvider, client: OAuthClientInformationFull):
    _, challenge = _pkce_pair()
    login_url = await provider.authorize(client, _params(challenge))
    request_id = login_url.split("request_id=")[1]
    redirect = await provider.complete_login(request_id, PASSWORD)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    return await provider.exchange_authorization_code(client, auth_code)


def _register_client(client) -> str:
    registration = client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "client_name": "pytest",
            "token_endpoint_auth_method": "none",
        },
    )
    assert registration.status_code == 201
    return registration.json()["client_id"]


def _authorize_and_login(client, client_id: str, challenge: str) -> str:
    response = client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "state": "state-123",
            "resource": RESOURCE_URL,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    login_url = response.headers["location"]
    assert "/login?request_id=" in login_url
    request_id = login_url.split("request_id=")[1]
    signed_in = client.post(
        "/login", data={"request_id": request_id, "password": PASSWORD}, follow_redirects=False
    )
    assert signed_in.status_code == 302
    return parse_qs(urlparse(signed_in.headers["location"]).query)["code"][0]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def oauth_app(tmp_path):
    saved_auth = server.mcp.settings.auth
    saved_provider = server.mcp._auth_server_provider
    saved_verifier = server.mcp._token_verifier
    saved_routes = list(server.mcp._custom_starlette_routes)
    server.mcp._session_manager = None
    enable_auth(server.mcp, AuthConfig(public_url=BASE_URL, password=PASSWORD, state_dir=tmp_path))
    try:
        yield server.mcp.streamable_http_app()
    finally:
        server.mcp.settings.auth = saved_auth
        server.mcp._auth_server_provider = saved_provider
        server.mcp._token_verifier = saved_verifier
        server.mcp._custom_starlette_routes[:] = saved_routes
        server.mcp._session_manager = None


# ------------------------------------------------------------------ provider unit tests


@pytest.mark.anyio
async def test_login_and_token_lifecycle(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    assert await provider.get_client("client-1") is client

    verifier, challenge = _pkce_pair()
    login_url = await provider.authorize(client, _params(challenge))
    assert login_url.startswith(f"{BASE_URL}/login?request_id=")
    request_id = login_url.split("request_id=")[1]

    with pytest.raises(LoginFailed) as exc:
        await provider.complete_login(request_id, "wrong")
    assert exc.value.status_code == 401

    redirect = await provider.complete_login(request_id, PASSWORD)
    query = parse_qs(urlparse(redirect).query)
    assert query["state"] == ["state-123"]

    with pytest.raises(LoginFailed) as exc:  # sign-in link is single use
        await provider.complete_login(request_id, PASSWORD)
    assert exc.value.status_code == 400

    auth_code = await provider.load_authorization_code(client, query["code"][0])
    assert auth_code is not None
    tokens = await provider.exchange_authorization_code(client, auth_code)
    assert tokens.access_token and tokens.refresh_token
    assert (await provider.load_access_token(tokens.access_token)) is not None

    with pytest.raises(ValueError):  # authorization codes are single use
        await provider.exchange_authorization_code(client, auth_code)


@pytest.mark.anyio
async def test_expired_login_link_and_rate_limit(tmp_path, monkeypatch):
    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(auth.asyncio, "sleep", _fast_sleep)
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    _, challenge = _pkce_pair()

    login_url = await provider.authorize(client, _params(challenge))
    request_id = login_url.split("request_id=")[1]
    provider.pending_requests[request_id].created_at -= PENDING_REQUEST_TTL_SECONDS + 1
    with pytest.raises(LoginFailed) as exc:
        await provider.complete_login(request_id, PASSWORD)
    assert exc.value.status_code == 400

    for _ in range(MAX_FAILED_LOGINS_PER_MINUTE):
        login_url = await provider.authorize(client, _params(challenge))
        request_id = login_url.split("request_id=")[1]
        with pytest.raises(LoginFailed):
            await provider.complete_login(request_id, "wrong")
    login_url = await provider.authorize(client, _params(challenge))
    request_id = login_url.split("request_id=")[1]
    with pytest.raises(LoginFailed) as exc:
        await provider.complete_login(request_id, PASSWORD)
    assert exc.value.status_code == 429


@pytest.mark.anyio
async def test_refresh_rotation_and_revoke(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    tokens = await _issue_tokens(provider, client)

    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    assert refresh is not None
    rotated = await provider.exchange_refresh_token(client, refresh, [])
    assert rotated.refresh_token != tokens.refresh_token
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None

    access = await provider.load_access_token(rotated.access_token)
    assert access is not None
    await provider.revoke_token(access)
    assert await provider.load_access_token(rotated.access_token) is None
    assert await provider.load_refresh_token(client, rotated.refresh_token) is None


@pytest.mark.anyio
async def test_state_persists_across_restart(tmp_path):
    provider = _provider(tmp_path)
    client = _client()
    await provider.register_client(client)
    tokens = await _issue_tokens(provider, client)

    restarted = _provider(tmp_path)
    assert await restarted.get_client("client-1") is not None
    assert await restarted.load_access_token(tokens.access_token) is not None
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is not None


# ------------------------------------------------------------------ HTTP tests


def test_public_url_allows_domain_host_header():
    saved = server.mcp.settings.transport_security
    try:
        server._configure_transport_security("https://chordsmith.example.com")
        settings = server.mcp.settings.transport_security
        assert "chordsmith.example.com" in settings.allowed_hosts
        assert "chordsmith.example.com:*" in settings.allowed_hosts
        assert "https://chordsmith.example.com" in settings.allowed_origins
        assert "localhost:*" in settings.allowed_hosts
    finally:
        server.mcp.settings.transport_security = saved


@pytest.mark.anyio
async def test_unauthenticated_mcp_returns_401(oauth_app):
    transport = httpx.ASGITransport(app=oauth_app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        response = await client.post(
            "/mcp", json={}, headers={"Accept": "application/json, text/event-stream"}
        )
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]

        metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"] == RESOURCE_URL
        assert [url.rstrip("/") for url in metadata.json()["authorization_servers"]] == [BASE_URL]

        server_metadata = await client.get("/.well-known/oauth-authorization-server")
        assert server_metadata.status_code == 200
        assert str(server_metadata.json()["issuer"]).rstrip("/") == BASE_URL
        assert server_metadata.json()["code_challenge_methods_supported"] == ["S256"]
        assert server_metadata.json()["client_id_metadata_document_supported"] is True
        assert "none" in server_metadata.json()["token_endpoint_auth_methods_supported"]
        assert str(server_metadata.json()["registration_endpoint"]).endswith("/register")

        # Clients also probe these alternative well-known URLs; they must not 404.
        root_metadata = await client.get("/.well-known/oauth-protected-resource")
        assert root_metadata.status_code == 200
        assert root_metadata.json()["resource"] == RESOURCE_URL
        assert root_metadata.headers["access-control-allow-origin"] == "*"

        trailing_slash = await client.get("/.well-known/oauth-protected-resource/mcp/")
        assert trailing_slash.status_code == 200

        suffixed = await client.get("/.well-known/oauth-authorization-server/mcp")
        assert suffixed.status_code == 200
        assert str(suffixed.json()["issuer"]).rstrip("/") == BASE_URL

        oidc = await client.get("/.well-known/openid-configuration")
        assert oidc.status_code == 200
        assert str(oidc.json()["issuer"]).rstrip("/") == BASE_URL


def test_full_oauth_flow_end_to_end(oauth_app):
    with TestClient(oauth_app, base_url=BASE_URL) as client:
        registration = client.post(
            "/register",
            json={
                "redirect_uris": [REDIRECT_URI],
                "client_name": "pytest",
                "token_endpoint_auth_method": "none",
            },
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        verifier, challenge = _pkce_pair()
        response = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "state": "state-123",
                "resource": RESOURCE_URL,
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        login_url = response.headers["location"]
        assert "/login?request_id=" in login_url
        request_id = login_url.split("request_id=")[1]

        page = client.get("/login", params={"request_id": request_id})
        assert page.status_code == 200
        assert 'name="password"' in page.text

        denied = client.post(
            "/login", data={"request_id": request_id, "password": "wrong"}, follow_redirects=False
        )
        assert denied.status_code == 401

        signed_in = client.post(
            "/login", data={"request_id": request_id, "password": PASSWORD}, follow_redirects=False
        )
        assert signed_in.status_code == 302
        callback = urlparse(signed_in.headers["location"])
        query = parse_qs(callback.query)
        assert f"{callback.scheme}://{callback.netloc}{callback.path}" == REDIRECT_URI
        assert query["state"] == ["state-123"]

        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "client_id": client_id,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
        )
        assert token_response.status_code == 200
        tokens = token_response.json()
        access = tokens["access_token"]

        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        }
        authenticated = client.post(
            "/mcp",
            json=initialize,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access}",
            },
        )
        assert authenticated.status_code == 200
        assert "result" in authenticated.text

        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != access

        reused = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert reused.status_code == 400


def test_authorize_requires_pkce_s256(oauth_app):
    with TestClient(oauth_app, base_url=BASE_URL) as client:
        client_id = _register_client(client)
        base_params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
        }

        missing = client.get("/authorize", params=base_params, follow_redirects=False)
        assert missing.status_code == 302  # error reported back to the client's redirect_uri
        assert parse_qs(urlparse(missing.headers["location"]).query)["error"] == ["invalid_request"]

        plain = client.get(
            "/authorize",
            params={**base_params, "code_challenge": "abc", "code_challenge_method": "plain"},
            follow_redirects=False,
        )
        assert plain.status_code == 302
        assert parse_qs(urlparse(plain.headers["location"]).query)["error"] == ["invalid_request"]


def test_token_rejects_wrong_code_verifier(oauth_app):
    with TestClient(oauth_app, base_url=BASE_URL) as client:
        client_id = _register_client(client)
        _, challenge = _pkce_pair()
        code = _authorize_and_login(client, client_id, challenge)

        response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "code_verifier": secrets.token_urlsafe(48),
                "redirect_uri": REDIRECT_URI,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"


# ------------------------------------------------------------------ CIMD (Client ID Metadata Documents)

CIMD_URL = "https://client.example.com/metadata.json"


def _cimd_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=CIMD_URL,
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        token_endpoint_auth_method="none",
        client_name="cimd-pytest",
        scope="chordsmith",
    )


def test_cimd_url_validation():
    assert auth.is_client_metadata_url(CIMD_URL)
    assert not auth.is_client_metadata_url("https://client.example.com")
    assert not auth.is_client_metadata_url("https://client.example.com/")
    assert not auth.is_client_metadata_url("http://client.example.com/metadata.json")
    assert not auth.is_client_metadata_url("https://client.example.com/metadata.json#frag")
    assert not auth.is_client_metadata_url("not-a-url")


@pytest.mark.anyio
async def test_cimd_fetch_rejects_non_public_hosts():
    assert await auth.fetch_client_metadata_document("https://127.0.0.1/metadata.json") is None
    assert await auth.fetch_client_metadata_document("https://10.0.0.5/metadata.json") is None
    assert await auth.fetch_client_metadata_document("http://example.com/metadata.json") is None


def test_cimd_client_flow_without_registration(oauth_app, monkeypatch):
    calls: list[str] = []

    async def fake_fetch(client_id: str):
        calls.append(client_id)
        return _cimd_client() if client_id == CIMD_URL else None

    monkeypatch.setattr(auth, "fetch_client_metadata_document", fake_fetch)

    with TestClient(oauth_app, base_url=BASE_URL) as client:
        verifier, challenge = _pkce_pair()
        code = _authorize_and_login(client, CIMD_URL, challenge)

        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CIMD_URL,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
        )
        assert token_response.status_code == 200
        assert token_response.json()["scope"] == "chordsmith"

        # A second authorization uses the cached document instead of fetching again.
        _, challenge = _pkce_pair()
        _authorize_and_login(client, CIMD_URL, challenge)
        assert calls == [CIMD_URL]


def test_unfetchable_cimd_client_is_rejected(oauth_app, monkeypatch):
    async def failing_fetch(client_id: str):
        return None

    monkeypatch.setattr(auth, "fetch_client_metadata_document", failing_fetch)

    with TestClient(oauth_app, base_url=BASE_URL) as client:
        response = client.get(
            "/authorize",
            params={
                "client_id": CIMD_URL,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": _pkce_pair()[1],
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
