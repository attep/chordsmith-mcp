# Run ChordSmith online (remote MCP + OAuth)

This guide runs ChordSmith on a server so your AI app can reach it over the internet at
`https://your-domain/mcp`, protected by a sign-in password.

- [How it works](#how-it-works)
- [Quick start: VPS with Docker Compose (recommended)](#quick-start-vps-with-docker-compose-recommended)
- [Local test without a domain](#local-test-without-a-domain)
- [Cloudflare Tunnel](#cloudflare-tunnel)
- [Platforms that terminate TLS for you](#platforms-that-terminate-tls-for-you)
- [Connecting your AI app](#connecting-your-ai-app)
- [Settings](#settings)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)

## How it works

When ChordSmith runs with an HTTP transport it speaks **OAuth 2.1** the way MCP clients expect:

1. The client discovers the endpoints (`/.well-known/oauth-protected-resource/mcp` and
   `/.well-known/oauth-authorization-server`).
2. The client identifies itself with **dynamic client registration** (DCR, RFC 7591) or a
   **Client ID Metadata Document** (CIMD: the client_id is an `https://` URL hosting its client
   metadata). ChordSmith advertises and accepts both, and clients pick whichever they support.
3. The client opens `https://your-domain/authorize` in your browser; ChordSmith shows a
   **sign-in page** that asks for `CHORDSMITH_AUTH_PASSWORD`.
4. After sign-in the client receives an authorization code and exchanges it for an access token
   (1 hour) and a refresh token (30 days). **PKCE with S256 is required** for that exchange:
   requests without a `code_challenge` (or with another method) are rejected, and the
   `code_verifier` is checked against the stored challenge.

Registered clients and tokens are saved to `CHORDSMITH_STATE_DIR` (`/state` in Docker), so you
only sign in once per client, and restarts don't log you out. ChordSmith stays a single-user
server: the one password grants access to everything.

## Quick start: VPS with Docker Compose (recommended)

You need a small server (any VPS) with Docker installed, and a domain name whose DNS **A/AAAA
record points at the server's IP**. [Caddy](https://caddyserver.com/) obtains and renews the
HTTPS certificate automatically.

1. Point your domain at the server, e.g. `chordsmith.example.com → 203.0.113.10`.

2. On the server:

   ```bash
   git clone https://github.com/attep/chordsmith-mcp.git
   cd chordsmith-mcp
   cp .env.example .env
   ```

3. Edit `.env` and set:

   ```ini
   DOMAIN=chordsmith.example.com
   CHORDSMITH_AUTH_PASSWORD=<a long random passphrase>
   ```

   Generate a password with:
   `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`

4. Start it:

   ```bash
   docker compose up -d --build
   docker compose logs -f chordsmith
   ```

5. Check it's alive: open `https://chordsmith.example.com/healthz` — you should see
   `{"status": "ok", "version": "..."}` reporting the version the image was built from (the
   Docker build bakes it in, so rebuild after an upgrade). Then connect your AI app (see below)
   and enter the password when the sign-in page opens.

The generated `.mid`/`.wav`/`.mp3` files land in `./data` next to the compose file, and the
OAuth state in `./state`, so they survive upgrades and are easy to copy out. To update:
`git pull && docker compose up -d --build`.

If port 80/443 are already used on the server, remove the `ports` from the `caddy` service in
`docker-compose.yml` or put Caddy behind whatever is already there.

## Local test without a domain

You can try the whole flow on your own machine with plain HTTP:

```bash
docker build -t chordsmith-mcp .
docker run --rm -p 8000:8000 \
  -e CHORDSMITH_HOST=0.0.0.0 \
  -e CHORDSMITH_PUBLIC_URL=http://localhost:8000 \
  -e CHORDSMITH_AUTH_PASSWORD=test-pass \
  chordsmith-mcp --transport streamable-http
```

The MCP endpoint is `http://localhost:8000/mcp`. HTTP is only allowed for `localhost`; anything
public must be HTTPS.

Without Docker, the same thing from a source checkout:

```bash
chordsmith-mcp --transport streamable-http --host 127.0.0.1 --port 8000
# CHORDSMITH_PUBLIC_URL defaults to http://localhost:8000 on local addresses
```

## Cloudflare Tunnel

A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
gives you HTTPS without opening any inbound ports and without running Caddy. ChordSmith works
with both kinds of tunnel.

### Named tunnel (stable URL, needs a Cloudflare account + domain)

1. In the Cloudflare dashboard go to **Zero Trust → Networks → Tunnels**, create a tunnel
   (e.g. `chordsmith`) and copy its **token**.
2. Add a **Public Hostname**: `chordsmith.example.com`, service type `HTTP`, URL
   `chordsmith:8000`.
3. In `.env` set:

   ```ini
   CLOUDFLARE_TUNNEL_TOKEN=<the token>
   CHORDSMITH_PUBLIC_URL=https://chordsmith.example.com
   CHORDSMITH_AUTH_PASSWORD=<passphrase>
   ```

4. Start it:

   ```bash
   docker compose -f docker-compose.tunnel.yml up -d --build
   ```

`CHORDSMITH_PUBLIC_URL` must be exactly the public hostname: it is used in the OAuth metadata,
the sign-in redirect, and to bind tokens to this server. The endpoint for your AI app is
`https://chordsmith.example.com/mcp`.

### Quick tunnel (no account, random URL, for testing)

A quick tunnel gets a temporary `https://<random>.trycloudflare.com` URL. Because the URL is
random, start the tunnel first, read the URL from its logs, then start ChordSmith with that URL:

```bash
docker network create chordsmith-tunnel
docker run -d --name chordsmith-cloudflared --network chordsmith-tunnel \
  cloudflare/cloudflared:latest tunnel --url http://chordsmith:8000
docker logs chordsmith-cloudflared 2>&1 | grep trycloudflare   # copy the https://... URL

docker run -d --name chordsmith --network chordsmith-tunnel \
  -e CHORDSMITH_HOST=0.0.0.0 \
  -e CHORDSMITH_PUBLIC_URL=https://<random>.trycloudflare.com \
  -e CHORDSMITH_AUTH_PASSWORD=<passphrase> \
  -v "$PWD/data:/data" -v "$PWD/state:/state" \
  chordsmith-mcp --transport streamable-http
```

Add the connector URL `https://<random>.trycloudflare.com/mcp`. Quick tunnels are rate-limited,
the URL changes every time the `chordsmith-cloudflared` container restarts, and anyone who knows
the URL reaches your sign-in page — fine for testing, use a named tunnel or Caddy for a
long-lived server.

## Platforms that terminate TLS for you

On Fly.io, Railway, Render, a Cloudflare Tunnel, or behind your own reverse proxy, the platform
provides HTTPS. Run the container with the streamable HTTP transport and set
`CHORDSMITH_PUBLIC_URL` to the exact public URL clients will use:

```bash
docker run -d -p 8000:8000 \
  -e CHORDSMITH_HOST=0.0.0.0 \
  -e CHORDSMITH_PUBLIC_URL=https://chordsmith.example.com \
  -e CHORDSMITH_AUTH_PASSWORD=<passphrase> \
  -v "$PWD/data:/data" -v "$PWD/state:/state" \
  chordsmith-mcp --transport streamable-http
```

The URL must match exactly (scheme and host). It is used in the OAuth metadata, in the sign-in
redirect, and to bind tokens to this server ("audience" checking).

## Connecting your AI app

**Claude Desktop / Claude.ai**: Settings → Connectors → *Add custom connector*, URL
`https://chordsmith.example.com/mcp`. A browser window opens the ChordSmith sign-in page; enter
the password.

**Claude Code**:

```bash
claude mcp add --transport http chordsmith https://chordsmith.example.com/mcp
```

**ChatGPT**: Settings → Connectors → *Add* → custom MCP server, URL
`https://chordsmith.example.com/mcp` (needs a plan/developer mode that allows custom connectors).

**MCP Inspector** (no AI app needed):

```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable HTTP, URL: https://chordsmith.example.com/mcp
```

The first request from any client returns `401` with a `WWW-Authenticate` header pointing at the
metadata; compliant clients then walk the OAuth flow automatically.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHORDSMITH_PUBLIC_URL` | `http://localhost:<port>` on local addresses | The exact public URL, e.g. `https://chordsmith.example.com`. Required when listening on a public address. |
| `CHORDSMITH_AUTH_PASSWORD` | random, printed in the logs | Password for the sign-in page. Set it so it stays stable. |
| `CHORDSMITH_AUTH` | on for HTTP transports | Set to `off` to disable OAuth (only do this behind your own auth layer). |
| `CHORDSMITH_STATE_DIR` | `~/.chordsmith` (Docker: `/state`) | Where registered clients and tokens are stored (`oauth_state.json`). |
| `CHORDSMITH_OUTPUT_DIR` | `~/ChordSmith` (Docker: `/data`) | Where `.mid` files are written. |
| `CHORDSMITH_HOST` | `127.0.0.1` (Docker: set `0.0.0.0`) | Bind address. |
| `CHORDSMITH_PORT` | `8000` | Bind port. |
| `CHORDSMITH_TRANSPORT` | `stdio` | `streamable-http` for remote use (or pass `--transport`). |

## Security notes

- **Use HTTPS.** For anything non-local, ChordSmith refuses to start without an `https://`
  public URL, because OAuth metadata must not travel in the clear.
- The password is compared in constant time and rate-limited (8 failures per minute per
  provider). Failed and successful sign-ins are logged (without the password).
- Access tokens last 1 hour, refresh tokens 30 days, and refresh tokens rotate on use.
  Authorization codes are single-use and expire after 5 minutes.
- PKCE with S256 is enforced; the token exchange fails without a matching `code_verifier`.
- CIMD lookups only fetch `https://` URLs with a path that resolve to public addresses (no
  redirects, 5-second timeout, 64 KB size limit, cached for an hour), which blocks the obvious
  SSRF tricks; DCR registrations never expire but can be wiped with the state file.
- Tokens are bound to your public URL (RFC 8707 resource indicator): a token issued for one
  deployment is rejected by another.
- The state file is written atomically and chmod `0600` where the OS supports it. Treat it like
  a password: it contains bearer tokens.
- Anyone with the password can use every tool, including reading and writing files in the
  output folder. Run one deployment per person, or use `CHORDSMITH_AUTH=off` behind an
  authenticating proxy if you need more.
- To force everyone to sign in again, delete `oauth_state.json` in the state volume (or
  `docker compose down -v` to wipe everything).

## Troubleshooting

### `421 Invalid Host header`

The request's `Host` header doesn't match `CHORDSMITH_PUBLIC_URL`. Set it to exactly the URL
you use in the browser/client (including `https://` and the domain, without a trailing slash).

### The app connects but every call returns `401`

The client is holding a token issued for a different URL (for example `http://localhost` vs
`https://your-domain`). Remove the connector and add it again with the exact public URL, or
delete `oauth_state.json` and sign in fresh.

### The sign-in link has expired

Sign-in links are valid for 10 minutes. Start the connection again from the AI app.

### `CHORDSMITH_PUBLIC_URL must be set ...` on startup

You are listening on a non-local address (e.g. `0.0.0.0`) with OAuth enabled. Set
`CHORDSMITH_PUBLIC_URL=https://...` (recommended), or run behind an auth proxy with
`CHORDSMITH_AUTH=off`.

### I forgot the password

Change `CHORDSMITH_AUTH_PASSWORD` and restart. Existing tokens keep working until they expire;
delete `oauth_state.json` if you also want to revoke them.

### The client doesn't ask for a password

Some clients only start the OAuth flow when the server returns `401` with OAuth metadata — make
sure you added the `/mcp` URL (not the domain root) and that the client supports remote MCP
servers with OAuth.

### The app shows an error like `mcp_oauth_unsupported`

That is the client giving up on OAuth discovery. ChordSmith serves the discovery documents at
every well-known URL clients try — `/.well-known/oauth-protected-resource` (with and without
the `/mcp` suffix), `/.well-known/oauth-authorization-server` (with and without the suffix) and
`/.well-known/openid-configuration` — and advertises PKCE S256, DCR and CIMD. If it still
fails:

- Check the server is reachable: open `https://your-domain/healthz`, then
  `https://your-domain/.well-known/oauth-protected-resource` (must be JSON, not an error page).
- Make sure the connector URL is exactly `https://your-domain/mcp` (scheme, host and path).
- Update ChordSmith (`git pull && docker compose up -d --build`): older builds only served the
  `/mcp`-suffixed metadata URLs, which some clients don't probe.
- A few clients only allow OAuth connectors on certain plans or connector types; check the
  client's own logs/error details.
