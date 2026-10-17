# Development

## Setup

```bash
git clone https://github.com/attep/chordsmith-mcp.git
cd chordsmith-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Everyday commands

```bash
pytest -q                   # run all tests
ruff check src tests        # lint
ruff format src tests       # format
mypy                        # typing for the curated module set (see pyproject.toml)
docker compose -f docker-compose.dev.yml up -d                        # manager UI dev server (:5173)
docker compose -f docker-compose.dev.yml exec frontend npm test       # frontend unit tests
docker compose -f docker-compose.dev.yml exec frontend npm run build  # typecheck + build
chordsmith-mcp              # run the server on stdio (waits for a client; Ctrl+C to stop)
npx @modelcontextprotocol/inspector .venv/bin/chordsmith-mcp   # try tools in a browser
docker build -t chordsmith-mcp .                                # build the Docker image
```

Optional: `pip install pre-commit && pre-commit install` runs ruff and mypy on every commit
(see `.pre-commit-config.yaml`).

## Dev stack (Docker)

The manager UI's Vite dev server runs in a container so it can be managed with Docker commands
instead of host processes (`docker compose -f docker-compose.dev.yml up -d`, `restart frontend`,
`logs -f frontend`; `--build frontend` after `package-lock.json` changes). Two details make it
work on Windows:

- `CHOKIDAR_USEPOLLING=1`: Docker Desktop does not forward host file-change events into the
  container, so HMR polls the filesystem.
- `node_modules` lives in a named volume (native esbuild/rollup binaries do not survive
  Windows bind mounts); the source is bind-mounted over it.

Server-side work targets the hosted service (its public URL or `127.0.0.1:18000`); there is no
local server process. Python tests run in the host `.venv`, frontend tests inside the container.
The Windows-only VST bridge is started with `scripts/start-bridge.cmd` (Ollama stays a host app).


CI (GitHub Actions) runs lint, format check and tests on Python 3.10 and 3.12 on tags and pull
requests (and manually via workflow dispatch).

## Project layout

```
src/chordsmith/
  server.py           MCP tools, resources and prompts (FastMCP); command-line entry point
  models.py           Pydantic schemas: ChordEvent, NumeralEvent, NoteInput, Humanize, Voicing, Rhythm
  theory.py           Notes, chord types, chord-symbol parser, keys/scales, Roman numerals, pitches
  harmony.py          Deterministic analysis for explain_progression (key detection, cadences)
  midi_writer.py      Voicing, rhythm patterns (swing/humanize), writing, adding tracks, transposing
  grooves.py          Named drum patterns and chord-driven bass lines (add_drums/add_bassline)
  forms.py            Song-form templates (pop, rock, metal, ballad, jazz AABA, blues, edm, cinematic)
  analysis.py         Simple chord detection and Roman numerals for analyze_midi
  sections.py         Song sections: tempo/meter map in the conductor plus the sidecar
  renderers.py        Render engines and per-track instrument specs (FluidSynth, sfizz, VST)
  soundfont.py        .sf2 preset-table reader (pdta/phdr)
  audio.py            FluidSynth/ffmpeg rendering of MIDI to wav/mp3
  singing.py          Singing pipeline (Sodium Gold): engines, scores, jobs, mixing
  diffsinger.py       DiffSinger ONNX adapter (English voicebanks, mounted read-only, never bundled)
  tabs.py             MuseScore conversions: Guitar Pro/MusicXML to MIDI, MIDI to MusicXML scores
  storage.py          Output folder and project folders; keeps every file inside them
  delivery.py         Signed, expiring download URLs for get_midi_file/render_audio
  mixjobs.py          Background mix jobs (mix_song_with_vocals_job)
  renderjobs.py       Background render jobs (render_audio_job)
  ratelimit.py        Per-client rate limiting for the MCP and OAuth endpoints
  auth.py             OAuth 2.1 authorization server (sign-in page, tokens) for HTTP transports
  offload.py          Runs long tool bodies on worker threads
frontend/             The manager UI (Vite + React): jobs dashboard, tool runner, files, Ollama
tests/                pytest suite, including end-to-end tests through a real MCP client session
```

## Design notes

- **The LLM does the music, the server does the writing.** The server makes no AI calls. Its
  output for a given input is always the same, which keeps it easy to test.
- **Voicing and rhythm are options on the create tools**, not separate tools, so one call makes
  a finished file and no information is lost by re-reading MIDI.
- **Files stay in the output folder.** A name may carry one project folder (`"my_song/verse"`);
  segments are reduced to `[A-Za-z0-9._-]` and resolved paths must live inside the output
  folder (one level deep).
- MIDI files are type 1, 480 ticks per beat: track 0 holds tempo, time signature and one marker
  per chord, and track 1 holds the notes on channel 1.
- **OAuth is opt-in at startup.** `main()` turns it on for HTTP transports via `auth.py`; the
  stdio server (and the in-memory tests) run without auth. `tests/test_auth.py` covers the
  provider and drives the full browser flow through the ASGI app.

## Adding a chord type

Add a `ChordType(...)` line to `CHORD_TYPES` in `theory.py` (name, intervals in semitones,
accepted symbols, description) and add a case to `tests/test_theory.py`. It then appears
automatically in `list_chord_types` and `chords://types`.
