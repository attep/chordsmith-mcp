# Starting the local stack

The manager UI and the MCP server run as Docker containers here, with the server behind a
Cloudflare **quick tunnel** (a new random hostname on every start). After a reboot, one command
brings everything back and repairs the tunnel URL and manager token.

## After a reboot

1. Make sure Docker Desktop is running (see [One-time setup](#one-time-setup) to make that
   automatic).
2. Double-click **`scripts\start-stack.cmd`** (or run
   `powershell -ExecutionPolicy Bypass -File scripts\start-stack.ps1`).
3. If it says the tunnel URL changed, open **`monitor-token.ui.json`** and paste `server_url`,
   `client_id` and `refresh_token` into the UI's **Settings** — leave the Bearer token empty —
   then **Save & reconnect**. If it says the URL is unchanged, nothing needs pasting.
4. If you use Windows VST plugins, double-click **`scripts\start-bridge.cmd`** (a console window
   stays open while the bridge runs).

Open the manager at <http://localhost:5173>.

## What should be running

| Container | Role | Compose file |
|---|---|---|
| `chordsmith` | MCP server on `127.0.0.1:18000` | `docker-compose.stack.yml` |
| `chordsmith-cloudflared` | public HTTPS for outside clients | `docker-compose.stack.yml` |
| `voicevox` | Japanese singing engine | `docker-compose.stack.yml` |
| `chordsmith-frontend-1` | manager UI on `localhost:5173` | `docker-compose.dev.yml` |

On the host: `scripts\start-bridge.cmd` (VST bridge, port `18002`) and optionally Ollama for the
Assistant tab. Other containers on the machine are unrelated to ChordSmith.

## Why the token sometimes changes

A quick tunnel asks Cloudflare for a new random hostname every time it starts. The server's
`CHORDSMITH_PUBLIC_URL` — used for OAuth metadata and the token audience — must match it, so
`start-stack` rewrites `.env` and recreates the server; tokens minted for the old hostname stop
working. `start-stack` mints a new pair automatically in that case.

**Permanent fix:** use a named Cloudflare tunnel (stable hostname) — see `docs/remote.md`. With a
stable URL, tokens survive reboots, external clients keep the same connector URL, and
`start-stack` never has to mint; only the containers start.

## One-time setup

- Docker Desktop must start at sign-in, otherwise containers with `restart: unless-stopped` only
  start when Docker does. This setup adds a **`Docker Desktop.lnk`** shortcut to the Windows
  Startup folder; Docker's own **Settings → General → Start Docker Desktop when you sign in**
  toggle does the same (keep one of the two).
- The first `start-stack` run creates `.env` from the existing `chordsmith` container (sign-in
  password, VST bridge token, rate limits). `.env` is gitignored — treat it as a secret and keep
  a backup.
- `start-stack` converts the old hand-started `chordsmith`, `chordsmith-cloudflared` and
  `voicevox` containers once so that `docker compose` manages them from then on.

## Troubleshooting

```powershell
docker ps                                        # what is running
docker logs --tail 100 chordsmith                # server log
docker logs --tail 50 chordsmith-cloudflared     # current tunnel URL
Invoke-RestMethod http://127.0.0.1:18000/healthz
```

If minting fails while the server is healthy, run it by hand:

```powershell
.venv\Scripts\python.exe scripts\mint_monitor_token.py --server <the tunnel URL> --container chordsmith --out monitor-token.ui.json
```
