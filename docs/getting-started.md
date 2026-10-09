# Getting started

This guide starts from zero. Pick **one** installation option, then connect your AI app.

- [What you need](#what-you-need)
- [Option A: uv (recommended)](#option-a-uv-recommended)
- [Option B: Docker](#option-b-docker)
- [Option C: from source (for developers)](#option-c-from-source-for-developers)
- [Connect Claude Desktop](#connect-claude-desktop)
- [Other AI apps](#other-ai-apps)
- [Check that it works](#check-that-it-works)
- [Settings](#settings)
- [Opening the MIDI files](#opening-the-midi-files)

## What you need

- A computer running macOS, Windows or Linux.
- An AI app that supports MCP, such as Claude Desktop, Claude Code, Cursor or VS Code with Copilot.
- Either **uv** (Option A), **Docker** (Option B), or **Python 3.10+** (Option C).

Not sure which option to pick? Use **Option A**.

## Option A: uv (recommended)

[uv](https://docs.astral.sh/uv/) downloads ChordSmith and its dependencies, including Python
if needed, and runs it.

1. Install uv:
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Close and reopen your terminal.
3. Check it works:

   ```bash
   uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp --version
   ```

   You should see `chordsmith-mcp 0.1.0`.

The command your AI app will run is:

```
uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp
```

## Option B: Docker

Use this if you already have [Docker Desktop](https://www.docker.com/products/docker-desktop/).

1. Download the code and build the image:

   ```bash
   git clone https://github.com/attep/chordsmith-mcp.git
   cd chordsmith-mcp
   docker build -t chordsmith-mcp .
   ```

2. Create the folder where files should appear, e.g. `~/ChordSmith`.

The container writes files to `/data`. You **must** connect a folder on your computer to `/data`
with `-v`, or the files stay hidden inside the container:

```bash
docker run -i --rm -v "$HOME/ChordSmith:/data" chordsmith-mcp
```

(On Windows use e.g. `-v "C:\Users\you\ChordSmith:/data"`.)

## Option C: from source (for developers)

```bash
git clone https://github.com/attep/chordsmith-mcp.git
cd chordsmith-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
chordsmith-mcp --version
```

The command your AI app will run is the full path to `.venv/bin/chordsmith-mcp`
(Windows: `.venv\Scripts\chordsmith-mcp.exe`).

## Connect Claude Desktop

1. Open Claude Desktop → **Settings → Developer → Edit Config**.
   This opens `claude_desktop_config.json`:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add ChordSmith under `mcpServers`. Use the block for the option you picked.

   **Option A (uv):**

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

   **Option B (Docker)**, replace `/Users/you` with your home folder:

   ```json
   {
     "mcpServers": {
       "chordsmith": {
         "command": "docker",
         "args": ["run", "-i", "--rm", "-v", "/Users/you/ChordSmith:/data", "chordsmith-mcp"]
       }
     }
   }
   ```

   **Option C (source):**

   ```json
   {
     "mcpServers": {
       "chordsmith": {
         "command": "/full/path/to/chordsmith-mcp/.venv/bin/chordsmith-mcp"
       }
     }
   }
   ```

3. Save, then **quit Claude Desktop completely** and reopen it.

If you already have other servers in the file, add `"chordsmith": {...}` next to them, separated
by a comma. Don't create a second `mcpServers` section.

## Other AI apps

Most apps use the same `command` + `args` as above.

**Claude Code** (terminal):

```bash
claude mcp add chordsmith -- uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp
```

**Cursor**: create `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects)
with the same `mcpServers` block as for Claude Desktop.

**VS Code (Copilot agent mode)**: create `.vscode/mcp.json`:

```json
{
  "servers": {
    "chordsmith": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/attep/chordsmith-mcp", "chordsmith-mcp"]
    }
  }
}
```

**Apps that connect over HTTP** instead of starting a program: run

```bash
uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp --transport streamable-http --port 8000
```

and point the app to `http://127.0.0.1:8000/mcp`.

To host ChordSmith on a server so you can reach it from anywhere (with HTTPS and an OAuth
sign-in page), see [Run it online](remote.md).

## Check that it works

Ask your assistant:

> Which ChordSmith chord types are there?

It should call `list_chord_types` and show a list. Then try:

> Make an i–VI–III–VII progression in A minor and save it as first_song.

You'll get a reply with the chords (`Am, F, C, G`) and a path like
`/Users/you/ChordSmith/first_song.mid`.

## Settings

ChordSmith reads these optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CHORDSMITH_OUTPUT_DIR` | `~/ChordSmith` | Folder where `.mid` files are saved (Docker: `/data`) |
| `CHORDSMITH_TRANSPORT` | `stdio` | `stdio`, `streamable-http` or `sse` |
| `CHORDSMITH_HOST` | `127.0.0.1` | Host for HTTP transports |
| `CHORDSMITH_PORT` | `8000` | Port for HTTP transports |
| `CHORDSMITH_PUBLIC_URL` | local URL | Public `https://` URL when running online, e.g. `https://chordsmith.example.com` |
| `CHORDSMITH_AUTH_PASSWORD` | random, printed in logs | Password for the OAuth sign-in page (HTTP transports) |
| `CHORDSMITH_AUTH` | on for HTTP | `off` disables OAuth |
| `CHORDSMITH_STATE_DIR` | `~/.chordsmith` | Where OAuth clients and tokens are stored (Docker: `/state`) |

The online-related variables are explained in [Run it online](remote.md#settings).

To change the output folder in Claude Desktop, add an `env` block:

```json
"chordsmith": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/attep/chordsmith-mcp", "chordsmith-mcp"],
  "env": { "CHORDSMITH_OUTPUT_DIR": "/Users/you/Music/Chords" }
}
```

## Opening the MIDI files

A `.mid` file contains notes, not sound. Your music app plays those notes with an instrument.

- **GarageBand / Logic**: drag the file onto the track area.
- **Ableton Live / FL Studio / Reaper / Cubase**: drag the file onto a MIDI or instrument track.
- **MuseScore** (free): *File → Open* shows the chords as sheet music.
- **Windows**: Windows 11's built-in Media Player no longer plays `.mid` files; use MuseScore, a
  DAW, or a MIDI-capable player.

Each chord's name is stored as a *marker*, so many apps show the chord names on the timeline.
