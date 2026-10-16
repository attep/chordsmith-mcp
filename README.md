# ChordSmith MCP

[![CI](https://github.com/attep/chordsmith-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/attep/chordsmith-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Latest release](https://img.shields.io/github/v/release/attep/chordsmith-mcp)](https://github.com/attep/chordsmith-mcp/releases)

**Ask your AI assistant for chords, melodies and vocals — get MIDI and audio files you can open
in any music app.**

> "Make me a melancholic 8-bar progression in A minor, arpeggiated, around 80 BPM — then hum it."

The assistant chooses the music (for example `Am – F – C – G`). ChordSmith turns it into notes,
writes a `.mid` file, can add melody or drum tracks, can render it to WAV/MP3, and can even sing
it with a soft voice. You can drag the results into GarageBand, Ableton Live, FL Studio, Logic,
Reaper, MuseScore and most other music apps.

ChordSmith is an **MCP server**. MCP (Model Context Protocol) is the standard way for AI apps
such as Claude Desktop to use outside tools. You don't need to know how it works — just follow
the steps below.

---

## Quick start (about 5 minutes)

You need:

- An MCP-capable AI app. These steps use **Claude Desktop**; other apps are covered in
  [docs/getting-started.md](docs/getting-started.md#other-ai-apps).
- **uv**, a small tool that downloads and runs ChordSmith for you.

### 1. Install uv

macOS / Linux, in a terminal:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal afterwards.

### 2. Tell Claude Desktop about ChordSmith

In Claude Desktop open **Settings → Developer → Edit Config** and add the `chordsmith` entry:

```json
{
  "mcpServers": {
    "chordsmith": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/attep/chordsmith-mcp", "chordsmith-mcp"]
    }
  }
}
```

### 3. Restart Claude Desktop and make your first file

Quit it completely, reopen it, and type:

> Use ChordSmith to make a happy I–V–vi–IV progression in G major, strummed, 110 BPM.

Files are saved in a **ChordSmith** folder in your home folder (`~/ChordSmith`; Docker uses
`/data`) — use a project name like `my_song/verse` to keep one song's files together. Open the
`.mid` file in your music app and that's it.

If something doesn't work, see [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Things to try

- "Give me a jazzy ii–V–I in Bb with seventh chords and smooth voice leading."
- "Write `Am F C G` as eighth-note arpeggios, repeated 4 times, on a nylon guitar."
- "Add a simple melody on top and a basic drum beat."
- "Hum the melody down an octave with a soft voice, then mix it with the backing."
- "Render it to MP3 so I can hear it without opening a DAW."
- "Transpose my last file to C minor." / "Analyse `melancholy.mid` and tell me the chords."

## What ChordSmith can do

| Tool | What it does |
|---|---|
| `create_chord_progression` | Chord names (`["Am","F","C","G"]`) → `.mid` file |
| `create_progression_from_roman` | Roman numerals (`i–VI–III–VII`) + key → `.mid` file |
| `add_track` | Adds a note-level track (melody, bass, drums) to a copy of a file |
| `add_bassline` / `add_drums` | Derives a bass line from the file's chords (root, root-fifth, pulse, octave, walking) or lays a named drum groove (backbeat, four-on-floor, trap, metal, bossa, shuffle, …) |
| `set_song_sections` | Writes a section map (bars, tempo, meter per section) into the file's conductor |
| `get_midi_file` | Hands the actual file back (base64 or a signed download link) |
| `render_audio` | Renders a file to `.wav`/`.mp3` so it can be heard without a music app (or per-track stems with `stems: true`) |
| `set_track_instrument` / `list_instruments` / `list_plugins` | Per-track instrument specs (a soundfont, a GM program or drum kit, a sampled `.sfz` instrument, a `.vst3` plugin, trims and VST effect chains per track) and the engines + instrument/plugin catalogs this server can run |
| `list_chord_types` | Shows every chord type ChordSmith understands (30) |
| `transpose_midi` | Moves a file up/down, or from one key to another |
| `analyze_midi` | Reads a `.mid` file and guesses the chords, tempo and key — with Roman numerals per chord |
| `explain_progression` | Deterministic theory for a progression: numerals, functions, cadences, variation ideas |
| `list_generated_files` | Lists the files you've made, grouped by project folder |
| `import_midi` | Imports a MIDI file made elsewhere (base64) and reports what it contains |
| `import_audio` | Imports a vocal take or render made elsewhere (`.wav`/`.mp3`) |
| `create_empty_file` / `remove_track` | Builds a song from scratch (optionally with a section map) / drops a track in a copy |
| `import_tab` | Converts a Guitar Pro tab or MusicXML/MSCZ score to MIDI with MuseScore (local or via the host worker) and imports it like `import_midi` |
| `export_musicxml` | Converts a MIDI file to a MusicXML score for MuseScore/Finale/Sibelius (local MuseScore or the host worker) |
| `delete_midi_file` / `rename_midi_file` / `rename_track` | Tidies up files and track names |
| `prepare_vocal_score` → `map_vocal_lyrics` → `render_singing` → `get_singing_job` → `mix_song_with_vocals` → `export_vocal_song` | Sings a melody (VOICEVOX hum/kana, or DiffSinger voicebanks — English and exact phonemes; several banks with their own variants, male and female) and mixes it with the backing |

The create tools accept **voicing** options (inversions, open/drop-2, voice leading, bass note),
**rhythm** options (block, pulse, arpeggios, Alberti, strum, swing, seeded humanize), genre
**presets** (pop, rock, metal, jazz, bossa, classical, cinematic, trap, edm, blues, funk, lofi),
a `seed` for reproducible renders, and `midi_type` 0/1 for simple players. Songs can have
a **section map** with real tempo and meter changes (`set_song_sections`) — or start from a
**song-form template** (pop, rock, ballad, jazz AABA, 12-bar blues, EDM, cinematic) that lays
out Intro/Verse/Chorus/Bridge sections with bars and tempos in one call. Notes can then be
placed with section-relative bar/beat positions, and vocals, renders and exports follow the map.

Every tool also carries a human **title** and **behaviour hints** (read-only / creates /
destructive) that MCP clients display, and every result is machine-checkable (a declared output
schema, returned as structured JSON). See
[How tools appear to MCP clients](docs/tools.md#how-tools-appear-to-mcp-clients).

## Run it online

ChordSmith can run on a server so you can use it from any device, protected by OAuth 2.1
(sign-in page, dynamic client registration, PKCE):

```bash
cp .env.example .env   # set DOMAIN and CHORDSMITH_AUTH_PASSWORD
docker compose up -d --build
```

Caddy obtains HTTPS for your domain. Prefer no open ports? Use the Cloudflare Tunnel setup
(`docker-compose.tunnel.yml`). See [docs/remote.md](docs/remote.md).

## Singing

Wordless hums and Japanese kana work with the bundled VOICEVOX engine
(`docker compose --profile singing up -d`). English lyrics and exact phonemes need DiffSinger
voicebanks that you download and mount yourself — several can live side by side (`./voicebank`
is scanned; each bank brings its voice variants and languages, e.g. the male TIGER DS). They
are never bundled or committed. The mixer measures the vocal
against the band, can compress the voice or duck the band under it, and renders per-track stems
you can balance with `backing_levels`. The guide track is auto-picked (an exact
`Vocal`/`Voice`/`Melody` name first, else the first melody-like name); pass `guide_track` when
your file has several. See [docs/singing.md](docs/singing.md).

## Documentation

1. [Getting started](docs/getting-started.md): installation options (uv, Docker, from source)
   and setup for other AI apps
2. [Run it online](docs/remote.md): host it with Docker Compose, HTTPS and OAuth
3. [Tools reference](docs/tools.md): every tool, option and example
4. [Singing](docs/singing.md): VOICEVOX hums and DiffSinger English voices
5. [Music cheat sheet](docs/music-basics.md): chord symbols and Roman numerals explained simply
6. [Troubleshooting](docs/troubleshooting.md): common problems and fixes
7. [Architecture](docs/architecture.md): how the deterministic server and the LLM fit together
8. [Development](docs/development.md): running tests and the project layout

## Use it as an agent skill (Copilot, Claude)

ChordSmith ships an [agent skill](https://agentskills.io/specification) — a `SKILL.md` that teaches
Copilot (and any other skill-aware agent) the workflows, tool names and gotchas, so the assistant
prepares a score, maps lyrics and renders in the right order without being told.

- The skill lives in [`skills/chordsmith/SKILL.md`](skills/chordsmith/SKILL.md).
- **In a project**: copy that folder into your repository's `.github/skills/` directory.
- **Across projects**: copy it into `~/.copilot/skills/` (Copilot) or `~/.agents/skills/`.
- **GitHub CLI**: `gh skill install attep/chordsmith-mcp chordsmith` installs it for you.

The skill only guides the assistant — the actual work still runs through the MCP server
configured above.

## How it works

```
You → AI app (picks the music) → ChordSmith tool call → .mid / .wav / .mp3 file on your computer
```

ChordSmith never calls an AI service itself and needs no API keys. It validates the input, writes
the notes deterministically and saves the file. Everything runs on your computer (or your own
server). See [docs/architecture.md](docs/architecture.md) for the design.

## Roadmap

Contributions are welcome — these are the areas where help is wanted most:

- **Limits & hardening**: quotas (bars/tracks/file size/storage) and a storage TTL cleanup for
  long-running servers (per-client rate limiting shipped in v0.6.0)
- **Interchange**: MusicXML export for MuseScore
- **Analysis**: Roman-numeral analysis relative to the detected key, and an `explain_progression`
  tool that returns the theory (numerals, function, cadences, variations)
- **Mastering & plugin states**: a master-bus plugin chain with loudness targeting, and
  automated plugin-state generation (parameter sweeps → named states)

The [CHANGELOG](CHANGELOG.md) tracks what has shipped; releases follow semantic versioning.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the setup, checks and guidelines, and
[SECURITY.md](SECURITY.md) to report a vulnerability.

## Development

```bash
git clone https://github.com/attep/chordsmith-mcp.git
cd chordsmith-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

CI runs lint, format check, the full test suite (including golden-file, round-trip and stdio
smoke tests) on Python 3.10 and 3.12, plus a dependency audit.

## License

MIT, see [LICENSE](LICENSE).
