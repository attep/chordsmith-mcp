"""OAuth 2.1 authorization server for ChordSmith when it runs over HTTP.

Remote MCP clients (Claude, ChatGPT, ...) authenticate with OAuth: they identify themselves
with dynamic client registration (DCR, RFC 7591) or a Client ID Metadata Document (CIMD, a
client_id that is an https URL), open the authorization endpoint in a browser, sign in, and
receive an authorization code (PKCE with S256, RFC 7636) that is exchanged for bearer tokens.
This module implements the authorization-server side of that flow behind a single shared
password, using the auth hooks built into the MCP SDK.

State is kept in memory and mirrored to a JSON file (``state_dir/oauth_state.json``) so that
registered clients and tokens survive a restart. Everything is single-user: the password grants
access to the whole server.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    ProviderTokenVerifier,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

SCOPE = "chordsmith"
SUBJECT = "chordsmith"
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 300
PENDING_REQUEST_TTL_SECONDS = 600
MAX_FAILED_LOGINS_PER_MINUTE = 8
CIMD_CACHE_TTL_SECONDS = 3600
CIMD_FETCH_TIMEOUT_SECONDS = 5.0
CIMD_MAX_DOCUMENT_BYTES = 64 * 1024


class LoginFailed(Exception):
    """Raised when the sign-in form cannot be completed."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AuthConfig:
    """Settings for the built-in OAuth authorization server."""

    public_url: str
    password: str
    state_dir: Path | None = None

    @classmethod
    def from_env(cls, *, host: str, port: int) -> AuthConfig | None:
        """Build the config from environment variables. Returns None when auth is disabled."""
        if os.environ.get("CHORDSMITH_AUTH", "").strip().lower() in {"off", "disabled", "0", "false", "no"}:
            return None
        public_url = os.environ.get("CHORDSMITH_PUBLIC_URL", "").strip().rstrip("/")
        if not public_url:
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError(
                    "CHORDSMITH_PUBLIC_URL must be set to the public https:// URL when listening on a "
                    "non-local address, e.g. CHORDSMITH_PUBLIC_URL=https://chordsmith.example.com. "
                    "Set CHORDSMITH_AUTH=off to run without authentication (not recommended)."
                )
            public_url = f"http://localhost:{port}"
        password = os.environ.get("CHORDSMITH_AUTH_PASSWORD", "")
        if not password:
            password = secrets.token_urlsafe(16)
            logger.warning(
                "CHORDSMITH_AUTH_PASSWORD is not set; generated a temporary password: %s "
                "(set the variable to keep it stable across restarts)",
                password,
            )
        state_dir_env = os.environ.get("CHORDSMITH_STATE_DIR", "").strip()
        state_dir = Path(state_dir_env).expanduser() if state_dir_env else Path.home() / ".chordsmith"
        return cls(public_url=public_url, password=password, state_dir=state_dir)


@dataclass
class _PendingAuthorization:
    created_at: float
    client: OAuthClientInformationFull
    params: AuthorizationParams


def is_client_metadata_url(client_id: str) -> bool:
    """True when a client_id is a Client ID Metadata Document URL (CIMD)."""
    try:
        parsed = urlsplit(client_id)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.path not in ("", "/") and not parsed.fragment


def _host_is_public(host: str | None) -> bool:
    """True when every address the host resolves to is public (SSRF guard for CIMD fetches)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address.is_global:
            return False
    return True


async def fetch_client_metadata_document(client_id: str) -> OAuthClientInformationFull | None:
    """Fetch and validate a Client ID Metadata Document (CIMD).

    The document is a JSON object with the same fields as OAuth client metadata, hosted at an
    https URL that is used as the client_id. Fetching is guarded against SSRF: https only,
    public addresses only, no redirects, short timeout, small size limit. (The address is
    resolved once for the check and again by the HTTP client, so a rebinding DNS server could
    still race this; acceptable for a personal server, revisit if ever multi-tenant.)
    """
    if not is_client_metadata_url(client_id):
        return None
    parsed = urlsplit(client_id)
    if not _host_is_public(parsed.hostname):
        logger.warning("Refusing CIMD client %s: host is not a public address", client_id)
        return None
    try:
        async with httpx.AsyncClient(timeout=CIMD_FETCH_TIMEOUT_SECONDS, follow_redirects=False) as http:
            response = await http.get(client_id, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch CIMD for %s (%s)", client_id, exc)
        return None
    if response.status_code != 200:
        logger.warning("CIMD fetch for %s returned HTTP %s", client_id, response.status_code)
        return None
    if len(response.content) > CIMD_MAX_DOCUMENT_BYTES:
        logger.warning("CIMD document for %s is larger than %d bytes", client_id, CIMD_MAX_DOCUMENT_BYTES)
        return None
    try:
        document = response.json()
    except ValueError as exc:
        logger.warning("CIMD document for %s is not valid JSON (%s)", client_id, exc)
        return None
    if not isinstance(document, dict):
        logger.warning("CIMD document for %s is not a JSON object", client_id)
        return None
    if document.get("client_id") not in (None, client_id):
        logger.warning("CIMD document for %s declares a different client_id", client_id)
        return None
    document.pop("client_id", None)
    try:
        metadata = OAuthClientMetadata.model_validate(document)
    except ValidationError as exc:
        logger.warning("CIMD document for %s is not valid client metadata (%s)", client_id, exc)
        return None
    if not metadata.redirect_uris:
        logger.warning("CIMD document for %s has no redirect_uris", client_id)
        return None
    # CIMD clients are public: no secret is ever issued, so the token endpoint gets no auth.
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=metadata.redirect_uris,
        token_endpoint_auth_method="none",
        grant_types=metadata.grant_types,
        response_types=metadata.response_types,
        client_name=metadata.client_name,
        client_uri=metadata.client_uri,
        logo_uri=metadata.logo_uri,
        scope=metadata.scope or SCOPE,
        contacts=metadata.contacts,
        tos_uri=metadata.tos_uri,
        policy_uri=metadata.policy_uri,
    )


class PasswordOAuthProvider:
    """In-memory OAuth authorization server gated by one shared password.

    Implements the ``OAuthAuthorizationServerProvider`` protocol from the MCP SDK.
    """

    def __init__(self, config: AuthConfig, resource_server_url: str, cimd_fetcher=None):
        self.config = config
        self.resource_server_url = resource_server_url
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.access_tokens: dict[str, AccessToken] = {}
        self.refresh_tokens: dict[str, RefreshToken] = {}
        self.token_pairs: dict[str, str] = {}
        self.pending_requests: dict[str, _PendingAuthorization] = {}
        self._failed_logins: list[float] = []
        self._cimd_fetcher = cimd_fetcher
        self._cimd_cache: dict[str, tuple[float, OAuthClientInformationFull]] = {}
        self._load_state()

    # ---------------------------------------------------------------- state file

    @property
    def state_path(self) -> Path | None:
        return self.config.state_dir / "oauth_state.json" if self.config.state_dir else None

    def _load_state(self) -> None:
        path = self.state_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.clients = {
                key: OAuthClientInformationFull.model_validate(value)
                for key, value in raw.get("clients", {}).items()
            }
            self.access_tokens = {
                key: AccessToken.model_validate(value) for key, value in raw.get("access_tokens", {}).items()
            }
            self.refresh_tokens = {
                key: RefreshToken.model_validate(value)
                for key, value in raw.get("refresh_tokens", {}).items()
            }
            self.token_pairs = {str(key): str(value) for key, value in raw.get("token_pairs", {}).items()}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read OAuth state from %s (%s); starting fresh", path, exc)

    def _save_state(self) -> None:
        path = self.state_path
        if path is None:
            return
        data = {
            "clients": {key: c.model_dump(mode="json") for key, c in self.clients.items()},
            "access_tokens": {key: t.model_dump(mode="json") for key, t in self.access_tokens.items()},
            "refresh_tokens": {key: t.model_dump(mode="json") for key, t in self.refresh_tokens.items()},
            "token_pairs": self.token_pairs,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            if os.name == "posix":
                path.chmod(0o600)
        except OSError as exc:
            logger.warning("Could not save OAuth state to %s (%s)", path, exc)

    # ---------------------------------------------------------------- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a client: registered (DCR) first, then a CIMD URL."""
        client = self.clients.get(client_id)
        if client is not None:
            return client
        if not is_client_metadata_url(client_id):
            return None
        cached = self._cimd_cache.get(client_id)
        now = time.time()
        if cached is not None and cached[0] > now:
            return cached[1]
        fetcher = self._cimd_fetcher or fetch_client_metadata_document
        client = await fetcher(client_id)
        if client is None:
            self._cimd_cache.pop(client_id, None)
            return None
        logger.info("Resolved CIMD client %s (%s)", client_id, client.client_name)
        self._cimd_cache[client_id] = (now + CIMD_CACHE_TTL_SECONDS, client)
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        self.clients[client_info.client_id] = client_info
        logger.info("Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)
        self._save_state()

    # ---------------------------------------------------------------- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        self.pending_requests[request_id] = _PendingAuthorization(
            created_at=time.time(), client=client, params=params
        )
        return f"{self.config.public_url}/login?request_id={request_id}"

    def pending_request(self, request_id: str) -> _PendingAuthorization | None:
        pending = self.pending_requests.get(request_id)
        if pending is None:
            return None
        if pending.created_at + PENDING_REQUEST_TTL_SECONDS < time.time():
            del self.pending_requests[request_id]
            return None
        return pending

    async def complete_login(self, request_id: str, password: str) -> str:
        """Check the password, create an authorization code and return the client redirect URL."""
        pending = self.pending_request(request_id)
        if pending is None:
            raise LoginFailed(
                "This sign-in link has expired. Start the connection again from your AI app.", 400
            )
        if self._recent_failures() >= MAX_FAILED_LOGINS_PER_MINUTE:
            raise LoginFailed("Too many failed attempts. Wait a minute and try again.", 429)
        if not hmac.compare_digest(password.encode(), self.config.password.encode()):
            self._failed_logins.append(time.time())
            logger.warning("Failed sign-in attempt (client %s)", pending.client.client_id)
            await asyncio.sleep(0.5)
            raise LoginFailed("Wrong password. Try again.", 401)
        self._failed_logins.clear()
        del self.pending_requests[request_id]
        code = secrets.token_urlsafe(32)
        params = pending.params
        # This server has exactly one scope; ignore any others a client may have asked for.
        scopes = [s for s in (params.scopes or [SCOPE]) if s == SCOPE] or [SCOPE]
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=pending.client.client_id or "",
            scopes=scopes,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource or self.resource_server_url,
            subject=SUBJECT,
        )
        logger.info("Sign-in succeeded (client %s)", pending.client.client_name or pending.client.client_id)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def _recent_failures(self) -> int:
        cutoff = time.time() - 60
        self._failed_logins = [t for t in self._failed_logins if t >= cutoff]
        return len(self._failed_logins)

    # ---------------------------------------------------------------- codes and tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.expires_at < time.time():
            del self.auth_codes[authorization_code]
            return None
        return code

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
            resource=resource or self.resource_server_url,
            subject=subject,
        )
        self.refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + REFRESH_TOKEN_TTL_SECONDS,
            resource=resource or self.resource_server_url,
            subject=subject,
        )
        self.token_pairs[access] = refresh
        self.token_pairs[refresh] = access
        self._save_state()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        stored = self.auth_codes.pop(authorization_code.code, None)
        if stored is None or stored.client_id != client.client_id:
            raise ValueError("Invalid authorization code")
        if stored.expires_at < time.time():
            raise ValueError("Authorization code has expired")
        return self._issue_tokens(stored.client_id, stored.scopes, stored.resource, stored.subject)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self.refresh_tokens.get(refresh_token)
        if token is None:
            return None
        if token.expires_at and token.expires_at < time.time():
            del self.refresh_tokens[refresh_token]
            return None
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if refresh_token.token not in self.refresh_tokens:
            raise ValueError("Invalid refresh token")
        del self.refresh_tokens[refresh_token.token]
        partner = self.token_pairs.pop(refresh_token.token, None)
        if partner:
            self.token_pairs.pop(partner, None)
        return self._issue_tokens(
            refresh_token.client_id,
            scopes or refresh_token.scopes,
            refresh_token.resource,
            refresh_token.subject,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < time.time():
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        partner = self.token_pairs.pop(token.token, None)
        self.access_tokens.pop(token.token, None)
        self.refresh_tokens.pop(token.token, None)
        if partner:
            self.token_pairs.pop(partner, None)
            self.access_tokens.pop(partner, None)
            self.refresh_tokens.pop(partner, None)
        self._save_state()


def _login_page(request_id: str | None, error: str | None = None) -> str:
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    if request_id is None:
        form = ""
    else:
        form = f"""
      <form method="post" action="/login">
        <input type="hidden" name="request_id" value="{escape(request_id, quote=True)}">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" autocomplete="current-password"
               autofocus required>
        <button type="submit">Sign in</button>
      </form>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChordSmith sign in</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #16181d; color: #e8e8ea;
         display: flex; justify-content: center; padding: 8vh 1rem; }}
  main {{ width: 100%; max-width: 22rem; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .3rem; }}
  p.sub {{ color: #9aa0aa; margin: 0 0 1.5rem; }}
  .error {{ color: #ff8a8a; }}
  label {{ display: block; font-size: .85rem; margin-bottom: .3rem; color: #c6c9cf; }}
  input[type=password] {{ width: 100%; padding: .6rem .7rem; box-sizing: border-box;
         border: 1px solid #3a3f47; border-radius: 6px; background: #1f2228; color: inherit; }}
  button {{ width: 100%; margin-top: 1rem; padding: .65rem; border: 0; border-radius: 6px;
         background: #4c8bf5; color: white; font-size: 1rem; cursor: pointer; }}
  button:hover {{ background: #3d7ae0; }}
</style>
</head>
<body>
  <main>
    <h1>ChordSmith</h1>
    <p class="sub">Sign in to connect your AI assistant to this ChordSmith server.</p>
    {error_html}
    {form}
  </main>
</body>
</html>
"""


def _patch_sdk_metadata() -> None:
    """Fix up the SDK's authorization server metadata for MCP clients.

    Two things clients look for are missing from the SDK's builder (1.30):
    - ``client_id_metadata_document_supported``: without it, clients never use CIMD.
    - ``none`` in ``token_endpoint_auth_methods_supported``: MCP clients are public clients
      (they register with token_endpoint_auth_method=none, which this server supports), but the
      SDK only advertises client_secret_post/basic, so strict clients conclude OAuth is
      unsupported.

    Wrapping the SDK's metadata builder is idempotent and both values are asserted in the tests,
    so an SDK change cannot go unnoticed.
    """
    from mcp.server.auth import routes as sdk_auth_routes

    original = sdk_auth_routes.build_metadata
    if getattr(original, "_chordsmith_patched", False):
        return

    def build_metadata_patched(*args, **kwargs):
        metadata = original(*args, **kwargs)
        metadata.client_id_metadata_document_supported = True
        methods = list(metadata.token_endpoint_auth_methods_supported or [])
        if "none" not in methods:
            metadata.token_endpoint_auth_methods_supported = ["none", *methods]
        revocation_methods = list(metadata.revocation_endpoint_auth_methods_supported or [])
        if revocation_methods and "none" not in revocation_methods:
            metadata.revocation_endpoint_auth_methods_supported = ["none", *revocation_methods]
        return metadata

    build_metadata_patched._chordsmith_patched = True  # type: ignore[attr-defined]
    sdk_auth_routes.build_metadata = build_metadata_patched


def _register_discovery_aliases(mcp: FastMCP, config: AuthConfig, resource_server_url: str) -> None:
    """Serve the OAuth discovery documents at the extra well-known URLs clients try.

    The MCP SDK only registers the canonical RFC 9728/8414 paths
    (``/.well-known/oauth-protected-resource/mcp`` and ``/.well-known/oauth-authorization-server``).
    Several MCP clients probe the root protected-resource URL, the path-suffixed
    authorization-server URL, or OpenID Connect discovery first and report "OAuth unsupported"
    on a 404, so serve the same documents (with CORS headers) there too.
    """
    from mcp.server.auth import routes as sdk_auth_routes

    resource_document = {
        "resource": resource_server_url,
        "authorization_servers": [config.public_url],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
    }
    auth_settings = mcp.settings.auth
    if auth_settings is None:  # pragma: no cover - enable_auth sets it just before
        return
    auth_document = sdk_auth_routes.build_metadata(
        auth_settings.issuer_url,
        None,
        ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        RevocationOptions(enabled=True),
    ).model_dump(mode="json", exclude_none=True)

    def with_cors(response: Response) -> Response:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

    async def protected_resource(_request: Request) -> Response:
        return with_cors(JSONResponse(resource_document))

    async def authorization_server(_request: Request) -> Response:
        return with_cors(JSONResponse(auth_document))

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp/"):
        mcp.custom_route(path, methods=["GET", "OPTIONS"])(protected_resource)
    for path in ("/.well-known/oauth-authorization-server/mcp", "/.well-known/openid-configuration"):
        mcp.custom_route(path, methods=["GET", "OPTIONS"])(authorization_server)


def enable_auth(mcp: FastMCP, config: AuthConfig, cimd_fetcher=None) -> PasswordOAuthProvider:
    """Turn on OAuth 2.1 for an HTTP transport and register the sign-in page.

    Clients identify themselves with dynamic client registration (DCR) or a Client ID Metadata
    Document (CIMD); PKCE with S256 is required for the authorization code flow. Must be called
    before the server starts (or before the ASGI app is built).
    """
    _patch_sdk_metadata()
    resource_server_url = f"{config.public_url}{mcp.settings.streamable_http_path}"
    provider = PasswordOAuthProvider(config, resource_server_url, cimd_fetcher=cimd_fetcher)
    mcp.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(config.public_url),
        resource_server_url=AnyHttpUrl(resource_server_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=True,
    )
    mcp._auth_server_provider = provider  # noqa: SLF001 - no public setter in the SDK
    mcp._token_verifier = ProviderTokenVerifier(provider)  # noqa: SLF001

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:  # pragma: no cover - exercised via ASGI tests
        if request.method == "POST":
            form = await request.form()
            request_id = str(form.get("request_id") or request.query_params.get("request_id") or "")
            password = str(form.get("password") or "")
            try:
                redirect_url = await provider.complete_login(request_id, password)
            except LoginFailed as exc:
                page = _login_page(request_id or None, exc.message)
                return HTMLResponse(page, status_code=exc.status_code)
            return RedirectResponse(redirect_url, status_code=302)
        request_id = request.query_params.get("request_id", "")
        if provider.pending_request(request_id) is None:
            return HTMLResponse(
                _login_page(None, "This sign-in link has expired. Start again from your AI app."),
                status_code=400,
            )
        return HTMLResponse(_login_page(request_id))

    _register_discovery_aliases(mcp, config, resource_server_url)

    logger.info(
        "OAuth 2.1 enabled: issuer %s, resource %s, state file %s",
        config.public_url,
        resource_server_url,
        provider.state_path or "(in memory only)",
    )
    return provider
