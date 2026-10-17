# Agent guidance

Notes for AI agents (and humans) working in this repository.

## Verification policy

Run on **every commit** (fast and reliable):

- `pytest -q` (Python, host venv)
- `ruff check src tests` and `ruff format --check src tests`
- `mypy`
- frontend: `docker compose -f docker-compose.dev.yml exec frontend npm test`
  and `docker compose -f docker-compose.dev.yml exec frontend npm run build`

Only on **explicit request** (slow, or it changes a live system):

- `scripts/assistant_rehearsal.py` (model-driven, minutes per run)
- rebuilding or recreating the hosted containers (the Caddy or tunnel stack)
- redeploys, tags/releases, browser/UI checks

Never start long-running services with PowerShell `Start-Process` — services belong in Docker
(the dev frontend) or are already running (hosted ChordSmith, Windows VST bridge, Ollama).

## Dev environment

- The manager UI's Vite dev server runs in a container:
  `docker compose -f docker-compose.dev.yml up -d` (port 5173; `--build frontend` after
  `package-lock.json` changes). HMR polls the filesystem because Docker Desktop on Windows
  does not forward file-change events from the host.
- The ChordSmith server under test is the **hosted** service (its public URL or
  `127.0.0.1:18000`); do not start a local server process unless the user explicitly asks.
- Frontend tests and builds run inside the frontend container; Python tests/lint/mypy run in
  the host `.venv`.
- The Windows VST bridge (`scripts/start-bridge.cmd`; Windows-only) and Ollama stay on the host.

## Notes

- Changelog and docs entries describe behaviour generically; never name specific songs,
  projects or user files.
- `frontend/src/lib/toolSchemas.fixture.ts` is generated from a running server's `tools/list`;
  regenerate it when tool schemas change (see `frontend/README.md`).
