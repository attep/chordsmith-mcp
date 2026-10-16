# Security

## Reporting a vulnerability

Please do not open a public issue for security problems. Report privately via GitHub's
[security advisories](https://github.com/attep/chordsmith-mcp/security/advisories/new) (or the
contact on the repository profile). Include the version, the transport (stdio, HTTP), the
request or tool call, and what an attacker could do. You should get an answer within a few
days; fixes are released as patch versions and the advisory is published afterwards.

## Scope and posture

ChordSmith runs on your own computer or your own server; most deployments are single-user.
The security-relevant surfaces are:

- **OAuth 2.1 (HTTP transports)**: password sign-in with PKCE, dynamic client registration,
  single-use codes, rotating refresh tokens. Sign-in attempts are rate limited (8 failures per
  minute) and a client key is locked out for 10 minutes after 20 failures within an hour.
  `CHORDSMITH_AUTH=off` disables OAuth and warns loudly — only for fully trusted networks.
- **Rate limiting**: per-client budgets on the MCP and OAuth endpoints
  (`CHORDSMITH_RATE_LIMIT`, `CHORDSMITH_AUTH_RATE_LIMIT`). With `CHORDSMITH_TRUST_PROXY=1`,
  identities come from `CF-Connecting-IP`/`X-Forwarded-For` — only enable it when nothing but
  the trusted proxy can reach the port (the headers are spoofable otherwise).
- **Download links**: `get_midi_file`/`get_file`/`render_audio` can return signed, expiring
  URLs (HMAC over the file's relative name and expiry, secret persisted in the state
  directory). A link is a capability: anyone holding it can download until it expires.
- **File handling**: every generated file stays inside the output folder (project folders are
  one level deep); path tricks and traversal attempts are rejected, and imports verify what
  they receive.
- **Plugins and the VST bridge**: `.vst3` plugins and the bridge worker run user-supplied
  binaries outside the server process. The bridge requires a token
  (`CHORDSMITH_VST_BRIDGE_TOKEN`) — treat it like a password and keep it off public networks.
- **Secrets**: none are baked into the image or the repo; configuration is environment
  variables (see `.env.example`). Voicebanks and plugins are never bundled.

## Supported versions

Security fixes ship for the latest release. If you run an older version, upgrade first.
