# Architecture

This page explains how ChordSmith is put together and where the line sits between the AI
assistant's musical reasoning and the server's deterministic work.

```
You -> AI app (chooses chords, lyrics, options)
        |
        v  MCP tool call (JSON)
   ChordSmith server
     validate -> music theory -> MIDI/audio -> files in the output folder
        |
        v  paths, summaries, base64 or signed URLs
   You -> open the file in a DAW / music app
```

## The core rule: the LLM decides music, the server decides bytes

- **The assistant does the musical reasoning.** It picks chords, keys, voicings, rhythms,
  lyrics and transpositions, and explains them. ChordSmith never calls an AI service and needs
  no API keys.
- **The server is deterministic and boring.** For a given input (including `seed`) it always
  produces the same file, validates everything, and returns clear errors instead of guessing.
  That is what makes golden-file tests possible.

Consequences:

- Errors are written to be read by the model: they name the bad input, offer a suggestion
  ("Did you mean 'm7b5'?") and say how to proceed.
- Anything the model needs to know about the music (notes used, resolved chords, detected
  key, durations) is returned in tool results, not left for the model to compute.

## Module map

| Module | Responsibility |
|---|---|
| `server.py` | Tool/resource/prompt definitions (FastMCP), option validation, delivery shaping |
| `models.py` | Pydantic schemas for tool inputs (chords, voicing, rhythm, notes, soft settings) |
| `theory.py` | Notes, chord types, chord-symbol parsing, keys/scales, Roman numerals, spelling |
| `midi_writer.py` | Voicing, rhythm patterns (swing/humanize), writing/adding/transposing `.mid` |
| `analysis.py` | Simple chord/key detection for `analyze_midi` (marker-aware) |
| `storage.py` | The output-folder sandbox: name cleaning, path containment, listing |
| `delivery.py` | Signed, expiring download URLs for tool-only clients |
| `audio.py` | FluidSynth/ffmpeg rendering of MIDI to wav/mp3 |
| `renderers.py` | Per-track rendering: the Renderer protocol, FluidSynth engine, instrument specs |
| `singing.py` | Vocal scores, lyrics mapping, render jobs, mixing (engine dispatch) |
| `diffsinger.py` | DiffSinger ONNX adapter for English voicebanks (mounted, never bundled) |
| `auth.py` | OAuth 2.1 authorization server for HTTP transports (sign-in, DCR/CIMD, PKCE) |

## Data flow

**Create**: tool call → pydantic validation → `theory.parse_*` → `midi_writer.write_progression`
→ `storage.FileStore` (sandbox) → summary dict with notes, bars, duration and the path.

**Analyze**: file → `analysis._collect_notes` (tempo map, markers, notes) → window detection →
markers override detection when their notes match → progression, key guess, segments.

**Sing**: MIDI melody track → engine-neutral score (seconds + tempo map) → lyrics mapping
(wordless/kana/English phonemes) → background job → vocal wav → mix (backing without the guide
track, soft levels, clipping correction) → export (wav/mp3/vocal/midi via signed links).

## Determinism guarantees

- `midi_writer` output depends only on its inputs. `humanize` uses a seeded RNG, and the
  top-level `seed` parameter overrides it; the same seed writes identical bytes.
- Files carry chord-name markers; `analyze_midi` trusts them when their notes are present, so
  create → analyze round trips return the original chord names (including inversions).
- The golden tests pin notes, timing, tempo and markers for representative inputs.

## Trust boundaries

- **Output folder sandbox.** Every file operation goes through `FileStore`: names are reduced to
  `[A-Za-z0-9._-]`, resolved paths must live directly in the output folder, and the download
  route resolves through the same checks. Traversal attempts are tested.
- **Online mode is authenticated.** With an HTTP transport, ChordSmith runs its own OAuth 2.1
  authorization server (sign-in page, dynamic client registration and Client ID Metadata
  Documents, PKCE S256, audience-bound tokens). All tools sit behind it; the sign-in password is
  compared in constant time and rate-limited.
- **Downloads are capability URLs.** `get_midi_file`/`render_audio`/`export_vocal_song` can hand
  out signed, expiring links (HMAC of filename + expiry, secret in the state folder) so
  tool-only clients can fetch files without the OAuth bearer.
- **Voicebanks are user-provided.** DiffSinger voicebanks are mounted read-only and never
  committed or baked into the image; a test enforces this. Licence layers are reported by
  `list_singing_voices`.

## Engines behind one interface

`singing.py` owns scores, lyrics, jobs and mixing; engines only render notes to audio:

- `VoicevoxClient` (HTTP sidecar) for Japanese kana and wordless hums. The frame query is
  prepared by the engine's teacher style, so decode-only voices (including whisper styles) sing
  in their own timbre.
- `diffsinger.py` (in-process ONNX) for English lyrics from a mounted voicebank.

Adding an engine means implementing one render call and listing it in `list_singing_voices`.

**Track rendering** (`renderers.py`) follows the same idea with a hard isolation rule: a
`Renderer` is a **separate process** (today the FluidSynth CLI), never an imported library. That
keeps crashing hosts from taking the server down, and it keeps differently licensed engines
(GPL hosts such as pedalboard or sfizz) outside this MIT codebase — the server only ever spawns
them and reads the wav they write. Per-track instrument specs (a `.sf2` per track, a trim) live
in a JSON sidecar next to the MIDI file, and `render_audio` with `stems: true` renders each
track with its own spec, then sums the stems.

## Where to extend

- **A chord type**: add a `ChordType(...)` to `theory.CHORD_TYPES` and a case to
  `tests/test_theory.py`; it appears everywhere automatically (including spelling tests).
- **A rhythm pattern**: extend `midi_writer.render_pattern` and `models.RhythmPattern`.
- **A preset**: add an entry to `server.PRESETS` (voicing + rhythm) and a golden test.
- **A render engine**: implement `renderers.Renderer` as a subprocess host, add it to
  `RENDERERS`, and cover it with a golden test (duration, peak and loudness tolerances).
- **A tool**: define it in `server.py` (or a module with a `register(mcp, store)` function) and
  add it to the stdio smoke test so CI exercises it end to end.
