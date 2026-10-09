# Troubleshooting

### ChordSmith doesn't show up in Claude Desktop

1. Did you **fully quit** Claude Desktop (not just close the window) and reopen it? On macOS use
   *Claude → Quit*; on Windows right-click the tray icon → *Quit*.
2. Is the config file valid JSON? A missing comma or an extra `}` breaks everything. Paste it into
   any online JSON validator to check.
3. Check the logs: **Settings → Developer**, click *chordsmith* to see its status and logs. Log files:
   - macOS: `~/Library/Logs/Claude/mcp-server-chordsmith.log`
   - Windows: `%APPDATA%\Claude\logs\mcp-server-chordsmith.log`

### "spawn uvx ENOENT" / "command not found: uvx"

The AI app can't find `uvx`. Apps started from the Dock or Start menu don't always see the same
`PATH` as your terminal. Use the full path:

1. In a terminal run `which uvx` (macOS/Linux) or `where uvx` (Windows).
2. Put that full path in the config, e.g. `"command": "/Users/you/.local/bin/uvx"`.

### "Repository not found" when uvx starts

`uvx --from git+https://github.com/attep/chordsmith-mcp` only works if the repository is public
(or your git is logged in to GitHub). Otherwise, use Option C in
[getting started](getting-started.md#option-c-from-source-for-developers).

### The first start is slow

The first time, uv downloads ChordSmith and its dependencies (a few seconds to a minute). Later
starts are fast.

### I can't find my files

Ask the assistant to run `list_generated_files`. It shows the exact folder. Defaults:
`~/ChordSmith` (uv/source) or the folder you mounted with `-v` (Docker).

**Docker:** if you didn't use `-v /your/folder:/data`, the files were saved inside the container
and disappeared when it stopped. Add the `-v` option.

### The file opens but there's no sound

A MIDI file contains notes, not audio. In your music app, make sure the track has an
**instrument** (a software synth or sampler) and isn't muted. In Ableton/FL/Reaper, drag the file
onto an instrument track rather than an audio track. Or ask the assistant to `render_audio` the
file to wav/mp3 so you can play it anywhere.

### The assistant can't attach the MIDI file

Tool-only clients (no resources) should call `get_midi_file` — it returns the file itself as
base64, or as a signed download link when `CHORDSMITH_PUBLIC_URL` is set.

### render_audio fails

Audio rendering needs FluidSynth plus a `.sf2` soundfont (and ffmpeg for mp3). The Docker image
includes all three. Locally, install `fluidsynth` and set `CHORDSMITH_SOUNDFONT=/path/to/your.sf2`
(`CHORDSMITH_FLUIDSYNTH`/`CHORDSMITH_FFMPEG` override the executable locations).

### A note or melody is rejected

Melody pitches must be note names with an octave (`E6`, `Bb3`) or MIDI numbers 0–127; anything
outside that range is refused so the file stays playable.

### The assistant used the wrong chords or ignored my options

Be specific: "use `create_chord_progression` with chords Am, F, C, G, rhythm pattern strum". You
can always check what was written with `analyze_midi`.

### A chord symbol is rejected

- Root notes must be capital letters: `Am`, not `am`.
- Use `b` for flat and `#` for sharp: `Bb`, `F#m`.
- Run `list_chord_types` to see all supported types. Unusual chords (like `C7#11`) aren't
  supported yet. Use a close alternative or ask the assistant to pick one.

### Testing the server without an AI app

Use the official MCP Inspector (needs Node.js):

```bash
npx @modelcontextprotocol/inspector uvx --from git+https://github.com/attep/chordsmith-mcp chordsmith-mcp
```

It opens a web page where you can call each tool by hand.

### Problems with the online (remote) server

Sign-in loops, `421 Invalid Host header`, `401` after connecting and similar issues are covered
in [Run it online → Troubleshooting](remote.md#troubleshooting).

Still stuck? [Open an issue](https://github.com/attep/chordsmith-mcp/issues) with the error
message and your config file (without any secrets).
