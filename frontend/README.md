# ChordSmith Manager UI

A small web app for looking inside a running ChordSmith server and driving it by hand:

- **Dashboard**: server health, the MCP session, active render/mix/singing jobs with a
  per-track status matrix (refreshed every 1.5 s), and the MCP clients connected right now
  (a client that disconnects without saying goodbye drops off the list after about a minute).
  **Click a client row** to see what it is doing: its recent requests, tool calls with a
  compacted argument summary, the response status and **how long each call took** (amber from
  3 s, red from 10 s; keepalive pings are filtered).
- **Tools**: every ChordSmith tool with a form built from its input schema. Run it and see
  the JSON result (errors are shown in red). Job IDs from results are kept in your browser,
  so they're still there after a reload.
- **Files**: everything in the output folder, with download links and an audio player for
  `.wav`/`.mp3`.
- **Assistant**: chat with a local [Ollama](https://ollama.com) model that can call
  ChordSmith tools. You choose which tools it may use. Every tool call and its result appear in
  the chat; small models sometimes emit valid JSON with the wrong shape, so calls are repaired
  against the tool's schema first (stringified lists, numbers as text, `file`/`source` renamed
  to `filename`; the chat shows `repaired: <fields>`), and an unsalvageable call gets a precise
  retry message instead of a server validation dump. `scripts/assistant_rehearsal.py` replays a
  whole song request through your server and Ollama and reports every call.

It's a separate app with its own `package.json`. It doesn't change how the Python package
is built or installed.

## What you need

- [Node.js](https://nodejs.org) 20 or newer (`node --version` to check)
- [uv](https://docs.astral.sh/uv/) for `uvx` (see [Getting started](../docs/getting-started.md))
- Optional: [Ollama](https://ollama.com/download) for the Assistant tab

## 1. Start ChordSmith over HTTP

The manager talks to the server over the streamable-HTTP transport (not stdio). In a
terminal:

```bash
CHORDSMITH_AUTH=off CHORDSMITH_PUBLIC_URL=http://localhost:8000 \
  uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp \
  --transport streamable-http --port 8000
```

Windows PowerShell:

```powershell
$env:CHORDSMITH_AUTH="off"; $env:CHORDSMITH_PUBLIC_URL="http://localhost:8000"
uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp --transport streamable-http --port 8000
```

- `CHORDSMITH_AUTH=off` turns off the sign-in page. Only do this on your own computer.
  With auth on, paste an access token into **Settings → Bearer token** instead.
- `CHORDSMITH_PUBLIC_URL` makes tools return download links (`/files/...`). Without it the
  Files tab still works; it fetches the file contents through MCP instead.

Check it's running: open <http://localhost:8000/healthz>. You should see
`{"status":"ok","version":"..."}`.

## 2. Start the manager

In a second terminal, from this `frontend/` folder:

```bash
npm install
npm run dev
```

Open <http://localhost:5173>. The dot in the top right turns green when it's connected.
If your server isn't on `http://localhost:8000`, change it under **Settings**.

## 3. Optional: Ollama assistant

Ollama only accepts requests from web pages it trusts. Allow the manager's address, then
start Ollama and pull a model that supports tool calling:

```bash
OLLAMA_ORIGINS=http://localhost:5173 ollama serve
ollama pull qwen3        # in another terminal; llama3.1 and mistral also work
```

On Windows/macOS with the Ollama app: set `OLLAMA_ORIGINS` as a user environment variable
to `http://localhost:5173`, then quit and restart Ollama.

In the **Assistant** tab, pick the model, then use **Allowed tools** to limit what it may
run (for example only the read-only tools). Every tool call and its result appear in the chat.

## Settings

All settings are saved in your browser (localStorage).

| Setting | Default | What it is |
|---|---|---|
| ChordSmith URL | `http://localhost:8000` | Where the server runs |
| MCP path | `/mcp` | The streamable-HTTP endpoint |
| Bearer token | empty | A fixed access token (e.g. from a client that signed in); used as-is when set |
| OAuth client ID | empty | With a refresh token, the UI renews access tokens by itself (hosted servers) |
| OAuth refresh token | empty | Rotated automatically on every renewal; lasts about 30 days |
| Ollama URL | `http://localhost:11434` | Where Ollama runs |
| Poll interval | `1500` ms | How often the dashboard refreshes |

## Monitoring a hosted (OAuth) server

The dashboard can look inside a ChordSmith server that runs with OAuth (e.g. behind a
Cloudflare tunnel). The server must run v0.31.0 or newer (it needs `GET /api/status`), and you
need a token pair once:

```bash
# the sign-in password comes from CHORDSMITH_AUTH_PASSWORD or the container env
python scripts/mint_monitor_token.py --server https://your-host.example.com --out monitor-token.json
```

Then paste the file's `server_url`, `client_id` and `refresh_token` into **Settings**. The UI
exchanges the refresh token for short-lived access tokens in the background (the refresh token
rotates on every use and lasts about 30 days), so monitoring keeps working without re-pasting.
The token file is a secret — don't commit it. The server allows browser requests from
`localhost`/`127.0.0.1` origins by default; to open the UI from another address, start the
server with `CHORDSMITH_CORS_ORIGINS=<that origin>`.

**One refresh-token chain per consumer.** Every refresh rotates the pair, so a second client
that refreshes the same token logs the first one out; mint separate pairs (`--out` files) for
the UI and for tools. `scripts/assistant_rehearsal.py` keeps its own
`monitor-token.rehearsal.json` (auto-minted) and never touches the UI's file. A stale manual
**Bearer token** in Settings beats everything — clear it to use the refresh flow — and if an
access token dies with a server restart the UI renews it on the 401 by itself.

## Opening the manager from another address

The server allows browser requests from `localhost` and `127.0.0.1` on any port. To use the
manager from another address (e.g. `http://192.168.1.20:5173`), start the server with:

```bash
CHORDSMITH_CORS_ORIGINS=http://192.168.1.20:5173
```

(comma-separated for several).

## Troubleshooting

| You see | Fix |
|---|---|
| Red dot, `Failed to fetch` | The server isn't running on that URL, or isn't started with `--transport streamable-http` |
| `401` / `invalid_token` | Auth is on: set `CHORDSMITH_AUTH=off` locally, or paste a token in Settings |
| `421 Misdirected Request` | Use the same host name the server was started for (e.g. `localhost`, not your machine's name) |
| Assistant: `Failed to fetch` | Ollama isn't running, or `OLLAMA_ORIGINS` doesn't include `http://localhost:5173` |
| Assistant answers but never calls tools | The model doesn't support tool calling; try `qwen3` or `llama3.1` |
| Files have no download link | Set `CHORDSMITH_PUBLIC_URL` (the file is still downloadable via the fallback) |

## Development

```bash
npm run typecheck   # TypeScript
npm test            # unit tests (Vitest)
npm run build       # production build in dist/
```

`src/lib/toolSchemas.fixture.ts` is generated from a running server's `tools/list` and drives
the all-tools repair sweep in `arguments.tools.test.ts` — regenerate it whenever tools or their
schemas change.

`GET /api/status` (server side, in `src/chordsmith/manager.py`) returns the version, every
job in the render/mix/singing registries with its progress, and the connected MCP sessions.
`GET /api/sessions/{session_id}` returns one session in full: its row plus the recent
requests and tool calls (with arguments and response status; pings are filtered). Both need
the same bearer token as `/mcp` when the server has OAuth on.
