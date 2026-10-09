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
chordsmith-mcp              # run the server on stdio (waits for a client; Ctrl+C to stop)
npx @modelcontextprotocol/inspector .venv/bin/chordsmith-mcp   # try tools in a browser
docker build -t chordsmith-mcp .                                # build the Docker image
```

CI (GitHub Actions) runs lint, format check and tests on Python 3.10 and 3.12 for every push and
pull request.

## Project layout

```
src/chordsmith/
  server.py       MCP tools, resources and prompts (FastMCP); command-line entry point
  models.py       Pydantic schemas: ChordEvent, NumeralEvent, NoteInput, Humanize, Voicing, Rhythm
  theory.py       Notes, chord types, chord-symbol parser, keys/scales, Roman numerals, pitches
  midi_writer.py  Voicing, rhythm patterns (swing/humanize), writing, adding tracks, transposing
  analysis.py     Simple chord detection for analyze_midi
  storage.py      Output folder; keeps every file inside it and cleans up names
  delivery.py     Signed, expiring download URLs for get_midi_file/render_audio
  audio.py        FluidSynth/ffmpeg rendering of MIDI to wav/mp3
  singing.py      Singing pipeline (Sodium Gold): VOICEVOX adapter, scores, jobs, mixing
  auth.py         OAuth 2.1 authorization server (sign-in page, tokens) for HTTP transports
tests/            pytest suite, including end-to-end tests through a real MCP client session
```

## Design notes

- **The LLM does the music, the server does the writing.** The server makes no AI calls. Its
  output for a given input is always the same, which keeps it easy to test.
- **Voicing and rhythm are options on the create tools**, not separate tools, so one call makes
  a finished file and no information is lost by re-reading MIDI.
- **Files stay in one folder.** Names are reduced to `[A-Za-z0-9._-]` and resolved paths must
  live directly inside the output folder.
- MIDI files are type 1, 480 ticks per beat: track 0 holds tempo, time signature and one marker
  per chord, and track 1 holds the notes on channel 1.
- **OAuth is opt-in at startup.** `main()` turns it on for HTTP transports via `auth.py`; the
  stdio server (and the in-memory tests) run without auth. `tests/test_auth.py` covers the
  provider and drives the full browser flow through the ASGI app.

## Adding a chord type

Add a `ChordType(...)` line to `CHORD_TYPES` in `theory.py` (name, intervals in semitones,
accepted symbols, description) and add a case to `tests/test_theory.py`. It then appears
automatically in `list_chord_types` and `chords://types`.
