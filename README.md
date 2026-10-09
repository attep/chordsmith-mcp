# ChordSmith MCP

**Ask your AI assistant for chords and get a MIDI file you can open in any music app.**

> "Make me a melancholic 8-bar progression in A minor, arpeggiated, around 80 BPM."

The assistant chooses the chords (for example `Am – F – C – G`). ChordSmith turns them into
notes and saves a `.mid` file. You can drag that file into GarageBand, Ableton Live, FL Studio,
Logic, Reaper, MuseScore and most other music apps.

ChordSmith is an **MCP server**. MCP (Model Context Protocol) is the standard way for AI apps
such as Claude Desktop to use outside tools. You don't need to know how it works, just follow the
steps below.

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

In Claude Desktop open **Settings → Developer → Edit Config**. A file called
`claude_desktop_config.json` opens. Replace its contents with this (or, if it already has an
`mcpServers` section, add the `chordsmith` entry inside it):

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

Save the file.

### 3. Restart Claude Desktop

Quit it completely and open it again. **chordsmith** should now be listed in the chat's
tools/connectors menu.

### 4. Make your first file

Type into the chat:

> Use ChordSmith to make a happy I–V–vi–IV progression in G major, strummed, 110 BPM.

Claude replies with the chords and the file location. By default files are saved in a folder
called **ChordSmith** in your home folder:

| System  | Folder                     |
|---------|----------------------------|
| macOS   | `/Users/<you>/ChordSmith`  |
| Windows | `C:\Users\<you>\ChordSmith` |
| Linux   | `/home/<you>/ChordSmith`   |

Open the `.mid` file in your music app, and that's it.

If something doesn't work, see [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Things to try

- "Give me a jazzy ii–V–I in Bb with seventh chords and smooth voice leading."
- "Write `Am F C G` as eighth-note arpeggios, repeated 4 times, on a nylon guitar."
- "Transpose my last file to C minor."
- "What chords are in D dorian?"
- "Analyse `melancholy.mid` and tell me what the chords are."
- "Explain why `C – Am – F – G` sounds so familiar."

## What ChordSmith can do

| Tool | What it does |
|---|---|
| `create_chord_progression` | Chord names (`["Am","F","C","G"]`) → `.mid` file |
| `create_progression_from_roman` | Roman numerals (`i–VI–III–VII`) + key → `.mid` file |
| `add_track` | Adds a note-level track (melody, bass, drums) to a copy of a file |
| `get_midi_file` | Hands the actual file back (base64 or a signed download link) |
| `render_audio` | Renders a file to `.wav`/`.mp3` so it can be heard without a music app |
| `list_chord_types` | Shows every chord type ChordSmith understands |
| `transpose_midi` | Moves a file up/down, or from one key to another |
| `analyze_midi` | Reads a `.mid` file and guesses the chords |
| `list_generated_files` | Lists the files you've made |
| `delete_midi_file` / `rename_midi_file` | Tidies up the output folder |
| `prepare_vocal_score` → `render_singing` → `mix_song_with_vocals` | Sings a melody track with a soft voice (VOICEVOX) and mixes it with the backing |

Both `create_*` tools accept **voicing** options (inversions, open/drop-2 voicings, voice leading,
bass note), **rhythm** options (block chords, pulses, arpeggios, Alberti bass, strumming, swing
and humanize) and a `lofi` preset for a soft, swung, humanized feel.

There is also a **singing pipeline** (slice 1, "Sodium Gold"): wordless hums with a soft VOICEVOX
voice, mixed and exported. See [docs/singing.md](docs/singing.md).

There are also **resources** (`chords://types`, `scales://{key}`, `midi://{filename}`) and
**prompts** (`compose_progression`, `explain_progression`).

Full details are in [docs/tools.md](docs/tools.md).

## Documentation

1. [Getting started](docs/getting-started.md): installation options (uv, Docker, from source)
   and setup for other AI apps
2. [Run it online](docs/remote.md): host it on a server with Docker Compose, HTTPS and OAuth
3. [Tools reference](docs/tools.md): every tool, option and example
4. [Singing](docs/singing.md): sing a melody track with a soft voice and mix it in (VOICEVOX)
5. [Music cheat sheet](docs/music-basics.md): chord symbols and Roman numerals explained simply
6. [Troubleshooting](docs/troubleshooting.md): common problems and fixes
7. [Development](docs/development.md): running tests and the project layout

## How it works

```
You → AI app (picks the chords) → ChordSmith tool call → .mid file on your computer
```

ChordSmith never calls an AI service itself and needs no API keys. It checks the input, writes
the notes and saves the file. Everything runs on your computer.

## Run it online

ChordSmith can also run on a server so you can use it from any device:

```bash
cp .env.example .env   # set DOMAIN and CHORDSMITH_AUTH_PASSWORD
docker compose up -d --build
```

Caddy obtains HTTPS for your domain and ChordSmith protects itself with an OAuth 2.1 sign-in
page. Prefer no open ports? Use the Cloudflare Tunnel setup
(`docker-compose.tunnel.yml`) instead. See [docs/remote.md](docs/remote.md) for the full
walkthrough, client setup and security notes.

## License

MIT, see [LICENSE](LICENSE).
