# Tools reference

Your AI assistant normally fills in these options for you. This page is for when you want to ask
for something specific ("use drop2 voicing") or understand a result.

- [create_chord_progression](#create_chord_progression)
- [create_progression_from_roman](#create_progression_from_roman)
- [Voicing options](#voicing-options)
- [Rhythm options](#rhythm-options)
- [add_track](#add_track)
- [create_empty_file](#create_empty_file)
- [remove_track](#remove_track)
- [set_song_sections](#set_song_sections)
- [bake_instruments](#bake_instruments)
- [get_midi_file](#get_midi_file)
- [get_file](#get_file)
- [import_midi](#import_midi)
- [import_audio](#import_audio)
- [render_audio](#render_audio)
- [render_audio_job and get_render_job](#render_audio_job-and-get_render_job)
- [mix_song_with_vocals_job and get_mix_job](#mix_song_with_vocals_job-and-get_mix_job)
- [list_chord_types](#list_chord_types)
- [transpose_midi](#transpose_midi)
- [analyze_midi](#analyze_midi)
- [list_generated_files](#list_generated_files)
- [delete_midi_file and rename_midi_file](#delete_midi_file-and-rename_midi_file)
- [Resources](#resources)
- [Prompts](#prompts)
- [Errors](#errors)

Files are always saved in the output folder (default `~/ChordSmith`, see
[Settings](getting-started.md#settings)). File names are cleaned up automatically
(`"my song"` becomes `my_song.mid`), and an existing file is never replaced unless you set
`overwrite: true`. Otherwise the new file gets a number, e.g. `my_song_2.mid`.

## How tools appear to MCP clients

Everything on this page is also machine-readable. MCP clients (and the assistants inside them)
see each tool with:

- **A friendly title** — e.g. "Create chord progression" or "Mix song with vocals" — shown in
  tool lists and permission prompts.
- **Behaviour hints**, so apps can treat calls appropriately:

  | Hint | Tools | Meaning |
  |---|---|---|
  | Read-only | `list_chord_types`, `analyze_midi`, `list_generated_files`, `get_midi_file`, `list_singing_voices`, `prepare_vocal_score`, `map_vocal_lyrics`, `get_singing_job` | Nothing on disk changes (the singing steps only keep temporary state in memory) |
  | Creates files | `create_chord_progression`, `create_progression_from_roman`, `add_track`, `transpose_midi`, `render_audio`, `render_singing`, `mix_song_with_vocals`, `export_vocal_song` | Writes new files; an existing file is never replaced unless you pass `overwrite: true`. A copy tool's `output_filename` must differ from its source |
  | Destructive | `delete_midi_file`, `rename_midi_file` | Changes files that already exist; some clients ask you to confirm first |

- **A fully described input schema** — every option has a description and its default, so the
  assistant can fill in calls without guessing.
- **A declared output schema** — results come back as typed `structuredContent` (and as JSON
  text too, for older clients), so apps can validate what they receive instead of parsing prose.

When something is wrong, the call comes back as an **error the assistant can act on** (see
[Errors](#errors)) — never as a crash.

The tool definitions follow the MCP
[tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools):
`name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`, and results with
`structuredContent` / `isError`. Tool calls are rate limited per client (`CHORDSMITH_RATE_LIMIT`,
default 120 requests per minute; over-limit requests get `429` with `Retry-After`). On the wire,
ChordSmith currently negotiates protocol revision **2025-11-25**, the latest supported by the
official MCP Python SDK v1.x; the tool fields above are common to both revisions (icons are
optional and not used yet).

---

## create_chord_progression

Turns chord symbols into a MIDI file.

| Option | Type | Default | Description |
|---|---|---|---|
| `chords` | list (required) | | Chord symbols, e.g. `["Am", "F", "C", "G"]`. Give a chord its own length with `{"chord": "G7", "beats": 2}`. Up to 256 chords. |
| `filename` | text | built from chords | e.g. `"sad_song"` → `sad_song.mid` |
| `tempo` | number 20–300 | `100` | Beats per minute |
| `time_signature` | text | `"4/4"` | e.g. `"3/4"`, `"6/8"` |
| `beats_per_chord` | number | one bar | Default chord length in beats (quarter notes) |
| `repeat` | 1–16 | `1` | Repeat the whole progression |
| `instrument` | 0–127 | `0` | General MIDI sound: 0 piano, 4 electric piano, 24 nylon guitar, 25 steel guitar, 48 strings, 88 pad |
| `voicing` | object | close, root position | See [Voicing options](#voicing-options) |
| `rhythm` | object | block chords | See [Rhythm options](#rhythm-options) |
| `preset` | text | none | `"lofi"`: voice leading on, soft swung repeated chord hits (pulse pattern) and light humanization |
| `seed` | number | none | Fix the humanization randomness so the same call writes identical bytes (used with a preset or a `rhythm.humanize`; overrides its `seed`) |
| `midi_type` | 0 or 1 | `1` | `1` = tempo/chords in track 0, notes in track 1 (standard; the first track carries the file's title, tempo and chord-name markers). `0` = everything in one track with the tempo inline, for simple players that ignore track 0 |
| `overwrite` | true/false | `false` | Replace a file with the same name |

**Example call**

```json
{
  "chords": ["Am", "F", {"chord": "C", "beats": 2}, {"chord": "G", "beats": 2}],
  "filename": "melancholy",
  "tempo": 80,
  "voicing": {"voice_leading": true, "add_bass": true},
  "rhythm": {"pattern": "arpeggio_up", "subdivision": 0.5}
}
```

**Example result (shortened)**

```json
{
  "filename": "melancholy.mid",
  "path": "/Users/you/ChordSmith/melancholy.mid",
  "uri": "midi://melancholy.mid",
  "tempo": 80,
  "time_signature": "4/4",
  "total_beats": 12.0,
  "bars": 3.0,
  "duration_seconds": 9.0,
  "chords": [
    {"chord": "Am", "beats": 4, "notes": ["A3", "A4", "C5", "E5"]},
    {"chord": "F", "beats": 4, "notes": ["F3", "A4", "C5", "F5"]}
  ]
}
```

Note names use scientific pitch: `C4` is middle C.

## create_progression_from_roman

Same as above, but you give **Roman numerals and a key** instead of chord names. The rules are in
the [music cheat sheet](music-basics.md#roman-numerals).

| Option | Type | Description |
|---|---|---|
| `numerals` | text or list (required) | `"i–VI–III–VII"`, `"I V vi IV"`, `["ii7", "V7", "Imaj7"]` or `[{"numeral": "V7", "beats": 2}]` |
| `key` | text (required) | `"C major"`, `"A minor"`, `"D dorian"`, `"Bb"`, `"F#m"` |
| everything else | | Same as `create_chord_progression` |

The result also contains `key`, `numerals` and `resolved_chords`, e.g. `["Am", "F", "C", "G"]`.

## Voicing options

Pass these inside `"voicing": {...}`. All are optional.

| Option | Default | Description |
|---|---|---|
| `style` | `"close"` | `close` = notes stacked tightly · `open` = middle note raised an octave (wider, airy) · `drop2` = second-highest note dropped an octave (jazz piano) |
| `inversion` | `0` | `0` root position, `1` first inversion, `2` second inversion, … |
| `octave` | `4` | Octave of the root (4 = around middle C) |
| `voice_leading` | `false` | Picks the inversion of each chord that is closest to the previous chord, so the chords connect smoothly. Overrides `inversion` after the first chord. |
| `add_bass` | `false` | Adds the root one octave below. Slash chords (`C/E`) always get their bass note. |

## Rhythm options

Pass these inside `"rhythm": {...}`. All are optional.

| Option | Default | Description |
|---|---|---|
| `pattern` | `"block"` | See below |
| `hits` | off | `[0, 0.75, 1.5, 2.25]`: play the chord at exactly these beats inside each chord — off-beat stabs, chugs and gallops. Overrides `pattern`/`subdivision`/`swing`; `gate`, `velocity` and `humanize` still apply |
| `subdivision` | `0.5` | Step length in beats for patterns that repeat. `1` = quarter notes, `0.5` = eighths, `0.25` = sixteenths |
| `velocity` | `90` | Loudness 1–127 |
| `gate` | `0.95` | How long each note sounds within its step (`1.0` = fully connected) |
| `strum_spread_ms` | `25` | Delay between "strings" for `strum` |
| `swing` | `0` | 0–0.75. Delays every offbeat step by this fraction of the step: `0.33` ≈ triplet swing, `0.5` = dotted feel |
| `humanize` | off | `{"timing_ms": 12, "velocity_range": 10, "seed": 7}`: small, seed-controlled timing/velocity variation. The same seed always produces the same bytes |

| Pattern | Sounds like |
|---|---|
| `block` | Whole chord held for its full length |
| `pulse` | Whole chord repeated every step (driving pop/rock piano) |
| `arpeggio_up` | One note at a time, low to high |
| `arpeggio_down` | One note at a time, high to low |
| `arpeggio_updown` | Up then down |
| `alberti` | Low–high–middle–high (classical piano) |
| `strum` | Guitar-like strums that alternate down and up, quieter on the up-strums |

With arpeggio and Alberti patterns, a bass note (from `add_bass` or a slash chord) is held under
the moving notes.

## add_track

Adds a note-level track (melody, bass, drums, …) to a **copy** of an existing MIDI file. The
original is never modified (so `output_filename` must differ from the source). A type 0 file is
promoted to type 1.

| Option | Description |
|---|---|
| `filename` (required) | Existing file in the output folder |
| `track_name` (required) | Name for the new track, e.g. `"Melody"` |
| `notes` | List of `{"pitch": "E6", "start_beat": 0, "beats": 0.5, "velocity": 80}`. Pitch is a note name with octave (`E6`, `Bb3`, `C#-1`) or a MIDI number 0–127. Up to 5000 notes. Position a note with `start_beat` (absolute), `section` + `bar` + `beat` (1-based, relative to the section map), or `tuplet` — an exact slot in a tuplet group, e.g. `{"count": 3, "in_beats": 2, "start_beat": 138, "slot": 0}` for the first note of a 3:2 triplet (positions are computed in MIDI ticks, so they are exact); a note may run past its section's end |
| `patterns` | Compact step grids, e.g. `[{"pitch": 36, "steps": "x---x---"}, {"pitch": 38, "steps": "----x---", "velocity": 100}]`. One row per voice: `x`/`o` hit, `X`/`O` accent (+25), `-`/`.`/`_` rest, `|` and spaces are bar lines that do not consume a step. Starts at beat 0 and tiles with `loop`. Give `notes`, `patterns`, or both |
| `step_beats` | Length of one pattern step in beats (default `0.25` = a sixteenth) |
| `automation` | Linear controller ramps, e.g. `[{"cc": 11, "from_value": 40, "to_value": 110, "start_beat": 0, "end_beat": 12}]` — a held note can swell (CC11 expression) or a track can fade (CC7 volume); one value every sixteenth note |
| `instrument` | General MIDI program for the track (0 piano, 24 nylon guitar, …) |
| `channel` | MIDI channel 1–16 (10 = drums) |
| `loop` | `{"length_beats": 4, "times": 36}`: the notes are positions inside the pattern window and repeat back to back, so a drum or bass groove is written once. The notes must fit inside `length_beats`; up to 20 000 notes after tiling |
| `swing` | `0.33`: notes on offbeat steps of the grid (odd multiples of `step_beats`) move later by this fraction of a step — 0 straight, 0.33 triplet feel, 0.5 dotted feel |
| `humanize` | `{"timing_ms": 12, "velocity_range": 10, "seed": 7}`: seeded timing/velocity variation (the chord tools' `rhythm.humanize`) applied **after** `loop` tiling, so repeated loops differ while the same seed reproduces the same bytes |
| `copy_from` | Copy an existing track's notes into the new one — a layer in one call, e.g. `"copy_from": "Sub Weight", "transpose_semitones": 12` for an octave-up sub. Give `notes`/`patterns` or `copy_from`, not both; `loop` does not apply |
| `transpose_semitones` | With `copy_from`: shift the copied notes by ±24 semitones |
| `output_filename` | Default: `<name>_<track_name>.mid` |
| `overwrite` | Replace an existing output file |

**Example:** a one-bar drum groove (kick on 1, snare on 2 and 4, hats on the offbeats) for a
48-bar song: write the notes once inside beats 0–4 and pass
`"loop": {"length_beats": 4, "times": 48}` instead of 192 hand-written notes. The same groove
with `patterns` is three short strings:

```json
{"filename": "song.mid", "track_name": "Drums", "channel": 10, "loop": {"length_beats": 4, "times": 48},
 "patterns": [
   {"pitch": 36, "steps": "x---x---|x---x---", "velocity": 120},
   {"pitch": 38, "steps": "----x---|----x---", "velocity": 115},
   {"pitch": 42, "steps": "x-x-x-x-|x-x-x-x-", "velocity": 70}
 ]}
```

### Layered subs that survive small speakers

A 30–50 Hz sine is inaudible on most phones and earbuds, no matter its gain — deep bass needs
company an octave up. With `copy_from` the layers take one call each:

1. **Octave layer** — `add_track(filename, track_name="Sub Octave", copy_from="Sub Weight",
   transpose_semitones=12, channel=4)` (on the file the previous call produced), then a
   harmonically rich preset via `set_track_instrument` (`"program": 80` square or `81` saw, with
   `gain_db` around −8). This carries the fundamental where phones can play it.
2. **Grit layer** — the same call again (`track_name="Sub Grit"`, `transpose_semitones=12`,
   `channel=5`, program 81/80) with a chain: `{"eq": {"highpass_hz": 120}, "saturation":
   {"enabled": true, "drive_db": 9}, "compressor": {"enabled": true, "threshold_db": -18}}` —
   saturation adds harmonics up to 300–800 Hz, the highpass keeps the layer thin.
3. Trim both with `gain_db` and keep the original sine under them; the mix's balance
   measurement ends up carrying the low end on any speaker.

## create_empty_file

Creates a **blank MIDI file** (a conductor track only) to build a song on from scratch — for
songs that should not start from a chord progression. With `sections`, the tempo/meter map is
written immediately, so [add_track](#add_track) can use `section` + `bar` + `beat` positions.

| Option | Description |
|---|---|
| `filename` (required) | Name for the new file, e.g. `"abyss"` (`.mid` is added) |
| `sections` | Optional section map, like [set_song_sections](#set_song_sections) |
| `output_filename`, `overwrite` | As above |

## remove_track

Writes a **copy** of a file without one track (the original is kept). The track is found by name
(names must be unique; rename first if not). The per-track instrument sidecar belongs to the
original, so the copy starts without specs.

| Option | Description |
|---|---|
| `filename` (required) | Existing file in the output folder |
| `track_name` (required) | Track to leave out, e.g. `"Chords"` |
| `output_filename` | Default: `<name>_no_<track_name>` |
| `overwrite` | Replace an existing copy |

## set_song_sections

Writes the song's **section map** into the MIDI: at each section's start the conductor gets a
time signature, a tempo and a `section: <name>` marker, so renders, exports and the vocal score
follow the map. This is the one tool that edits a file's timing **in place** — notes are never
touched. Call it before [add_track](#add_track) to place notes with section-relative positions.

| Option | Description |
|---|---|
| `filename` (required) | MIDI file in the output folder |
| `sections` (required) | Sections in order, up to 64 |

Each section has:

| Field | Description |
|---|---|
| `name` | Section name, e.g. `"Intro"`, `"Chorus"` (unique within the map) |
| `bars` | Length in bars |
| `bpm` | **Quarter-note** tempo for this section (a change is a real tempo event, never a hidden meter trick) |
| `numerator`, `denominator` | Time signature (default 4/4); e.g. `7` and `8` for 7/8 |
| `grouping` | Optional accent grouping like `"2+2+3"` (must sum to the numerator); stored in a sidecar, accents only — it does not change the bar's length |

Example — a 140 BPM intro in 4/4, then a chorus in 7/8:

```json
{"filename": "song.mid", "sections": [
  {"name": "Intro", "bars": 8, "bpm": 140},
  {"name": "Chorus", "bars": 16, "bpm": 140, "numerator": 7, "denominator": 8, "grouping": "2+2+3"}
]}
```

The result reports each section's `start_beat`, the total beats and the `duration_seconds`
integrated over the whole map (not the initial BPM applied to the song). Replaces any existing
section map; the sidecar (`<name>.sections.json`) follows the file through rename and delete.
Old single-tempo songs simply have no sections and keep working unchanged.

## bake_instruments

Writes a **copy** of a MIDI file with the [set_track_instrument](#set_track_instrument) specs'
bank/program events baked in as real MIDI events — including per-section overrides at their
exact boundaries — so other players and DAWs hear the intended instruments without ChordSmith.
The original is never modified (`output_filename` must differ from the source).

| Option | Description |
|---|---|
| `filename` (required) | MIDI file in the output folder |
| `output_filename` | Default: `<name>_instruments` |
| `overwrite` | Replace an existing copy |

A MIDI file cannot carry a SoundFont, so the result also returns the required `.sf2` per track
as **companion metadata** (the `tracks` map). Tracks that share a MIDI channel share one program
timeline — a standard MIDI limitation, reported as a warning when it applies. Tracks whose spec
is only a `gain_db` trim are listed but have nothing representable to bake.

## get_midi_file

Returns the MIDI file itself, so clients that only expose tools (no resources) can hand the file
to you.

| Option | Description |
|---|---|
| `filename` (required) | File in the output folder |
| `return_as` | `"base64"` (default): the bytes inline as `data_base64`, with `size_bytes` and `sha256`. `"url"`: a signed `download_url` plus `expires_at` (needs `CHORDSMITH_PUBLIC_URL`) |
| `expires_in` | Link lifetime in seconds, 30–3600 (default 300) |

The signed URL works without the OAuth login and stops working when it expires; the signing
secret lives in the state folder.

## get_file

Returns **any** output file — MIDI, wav or mp3 — so a render, stem, mix or imported take can be
checked or handed over. Same options as [get_midi_file](#get_midi_file) (`filename`,
`return_as`, `expires_in`); `mime_type` follows the file kind. Asking `get_midi_file` for an
audio file now fails with a pointer here instead of a bare "not found".

## import_midi

Imports a MIDI file made elsewhere (a DAW, a generator, another assistant) into the output
folder. The bytes are stored **verbatim** — nothing is transformed or discarded — and the
result reports what the file contains.

| Option | Description |
|---|---|
| `filename` (required) | Name for the imported file, e.g. `"sketch.mid"` |
| `data_base64` (required) | The `.mid` bytes, base64-encoded (up to 5 MB decoded) |
| `overwrite` | Replace an existing file of that name |

The report has: `midi_type`, `ticks_per_beat`, `duration_seconds`, `total_notes`, per-track
`index`/`name`/`channel`/`program`/`notes` (channels 1-based, 10 = drums), every
`tempo_changes` and `time_signature_changes` entry, `markers`, the `sysex_events` count, and
`warnings` for layouts the tools care about: type 0 files (per-track stems need type 1), tempo
events in several tracks, missing tempo/meter events, SysEx events (stored but ignored by
rendering), no notes. The imported file then works with the whole workflow — analyze,
sections, per-track instruments, stems and the singing tools.

## import_audio

Imports an audio file (`.wav` or `.mp3`) into the output folder — a vocal take, a render made
elsewhere, or a piece of a vocal rendered in chunks. The bytes are stored verbatim and the
result reports `size_bytes`, `sha256`, `duration_seconds`, `sample_rate`, `channels` and (for
wav) `peak_db`, so the file can be handed to [mix_song_with_vocals](singing.md#mixing-and-export)
as a `vocal` or a `vocals` segment without guessing what it is.

| Option | Description |
|---|---|
| `filename` (required) | Name for the imported file, e.g. `"take.wav"` |
| `data_base64` (required) | The audio bytes, base64-encoded (up to 25 MB decoded) |
| `overwrite` | Replace an existing file of that name |

## render_audio

Renders a MIDI file to audio with FluidSynth, so a sketch can be heard without a music app. The
Docker image includes FluidSynth, a General MIDI soundfont and ffmpeg; locally, install
`fluidsynth` and set `CHORDSMITH_SOUNDFONT` (and `CHORDSMITH_FLUIDSYNTH` if it is not on `PATH`).
For sampled `.sfz` instruments (see [set_track_instrument](#set_track_instrument)), install
sfizz — the Docker image builds it — and set `CHORDSMITH_SFIZZ_RENDER` if `sfizz_render` is not
on `PATH`.

| Option | Description |
|---|---|
| `filename` (required) | MIDI file in the output folder |
| `format` | `"wav"` (default) or `"mp3"` (mp3 needs ffmpeg) |
| `return_as` | Like [get_midi_file](#get_midi_file): `"base64"` or `"url"` |
| `soundfont` | Path to a `.sf2` file; default: `CHORDSMITH_SOUNDFONT` or a standard system path |
| `stems` | `true`: render every track on its own (using [set_track_instrument](#set_track_instrument) specs — soundfont, GM program, gain, or a sfizz `.sfz` instrument) and combine the stems into the mix. **Files with sfizz tracks must use `stems: true`** — a single FluidSynth pass cannot play them. Several renders run at once, but it is still slower than one pass; the result then also lists every stem (name, track, gain, sha256, peak). Stem entries use signed URLs whenever a public URL is configured (they are megabytes each), otherwise they follow `return_as`; the mix always follows `return_as` |
| `normalize_peak_db` | Gain the rendered mix so its peak hits this level in dBFS (set `-1.0` for a listening-ready file). Default `null` keeps FluidSynth's raw level; **stems stay raw either way**, so their balance sums back to the unnormalized mix |
| `reverb` | FluidSynth's built-in reverb (off when omitted): `{"room_size": 0.5, "damping": 0.3, "width": 0.8, "level": 0.7}` — those are the engine's own defaults, ranges are the engine's accepted values (room 0–1.2, damping 0–1, width 0–100, level 0–1). `{}` turns it on with the engine defaults |
| `chorus` | FluidSynth's built-in chorus (off when omitted): `{"voices": 3, "level": 0.6, "speed": 0.2, "depth": 4.25}` — again the engine's defaults and accepted ranges (voices 0–99, level 0–10, speed 0.1–5 Hz, depth 0–21 ms; this engine build has no waveform setting) |
| `output_filename` | Default: `<name>_wav` / `<name>_mp3` |
| `expires_in`, `overwrite` | As above |

The result includes `mime_type`, `size_bytes`, `sha256`, for wav the rendered `duration_seconds`
and `peak_db`, the `gain_correction_db` that normalization applied, a `clipping` flag, and the
effective `effects` (reverb/chorus settings or nulls). Every stem entry carries its own
`peak_db`. Raw renders intentionally keep headroom (a busy arrangement can peak around
−10 dBFS) — pass `normalize_peak_db: -1.0` when the file is for listening rather than for
balancing.

**Instrumental effects (`reverb`/`chorus`) are off by default**, so renders stay byte-identical
until you opt in; with them on, the engine's natural tail past the last note is kept (about
2.7 s). They are **engine-wide for one render instance, not per-track effects**: with
`stems: true` every stem is rendered wet, so summing the wet stems is not identical to one wet
pass (chorus/reverb do not add linearly across stems). How wet each track is comes from its
spec's `reverb_send`/`chorus_send` (see [set_track_instrument](#set_track_instrument)) — with no
send a track gets the GM default (a subtle reverb; send 0 is fully dry).
[`mix_song_with_vocals`](singing.md#mixing-and-export) takes the same `reverb`/`chorus` settings
for the backing, so a mix can be wet too; the vocal's own `vocal_reverb` is a separate effect and
is never applied to instruments.

## render_audio_job and get_render_job

For renders that may outrun a client's tool timeout — a three-minute, nine-track stems render
writes hundreds of megabytes and can take minutes — start the work in the background and poll it.

`render_audio_job` takes the same options as [render_audio](#render_audio) (`format`,
`return_as`, `soundfont`, `stems`, `normalize_peak_db`, `reverb`, `chorus`, `output_filename`,
`expires_in`, `overwrite`) plus:

| Option | Description |
|---|---|
| `tracks` | Render **only these tracks** and combine them — the retry path after a partial failure, so successful stems are never redone |

It returns immediately with `{job_id, status, tracks}`. `get_render_job(job_id)` (read-only)
reports:

- `status`: `queued`, `running`, `done` or `failed`;
- `progress`: every track with its `state` (`pending`/`running`/`done`/`failed`) and, for failed
  tracks, the error message;
- `result`: the full [render_audio](#render_audio) result once done — including a `failed` list
  when some tracks failed, while the mix still combines the successful stems.

Jobs live 24 hours. A failed track can be retried with
`render_audio_job(filename, tracks: ["Drums"])`.

Renders use a General MIDI soundfont: no samples, sub/reese bass design, filter sweeps or
production FX. Treat them as an audition of the notes, not a finished master — open the `.mid` in
a DAW for the real sound. MIDI automation (volume curves, CC sweeps) is not written either.

## mix_song_with_vocals_job and get_mix_job

Mixes re-render the backing, process every voice and master the result, and can take minutes on
a stems session. `mix_song_with_vocals_job` takes **the same options as
[mix_song_with_vocals](singing.md#mixing-and-export)** and returns `{job_id, status}` immediately;
poll `get_mix_job(job_id)` (read-only) for `status`, `progress` (each backing track as it renders,
then the `mix` phase — with error text on a failed track) and the full mix result when done.
`list_mix_jobs` (read-only, newest first) finds a job whose reply was lost; records survive
server restarts and a job interrupted by one comes back failed with the reason. Jobs live
24 hours. Use the synchronous tool for short mixes and this one when a client's tool timeout is
in play.

## set_track_instrument

Stores per-track instrument specs for a MIDI file in a small JSON sidecar next to it
(`<name>.instruments.json`); the MIDI itself is never modified. Use it to give a track its own
soundfont, to pick a different General MIDI instrument (or drum kit), to swap in a sampled `.sfz`
instrument, and to trim its level in the stem mix — then render with
[`render_audio`](#render_audio) and `stems: true` and audition the stems.

| Option | Description |
|---|---|
| `filename` (required) | MIDI file in the output folder |
| `tracks` (required) | Specs keyed by track name; entries merge into the existing map, a `null` value removes a track's spec |

Each spec has:

| Field | Description |
|---|---|
| `engine` | `"fluidsynth"` (General MIDI via a `.sf2` soundfont), `"sfizz"` (a sampled `.sfz` instrument) or `"vst"` (a `.vst3` plugin); sfizz and vst need `stems: true` renders |
| `preset` | fluidsynth: path to a `.sf2` soundfont for this track; omit for the server's default (FluidR3_GM) |
| `sfz` | sfizz: path to a `.sfz` instrument file (with its samples) the server can read; required when `engine` is `"sfizz"` |
| `plugin` | vst: path to a `.vst3` instrument plugin the server can read (see [list_plugins](#list_plugins)); required when `engine` is `"vst"` |
| `plugin_preset` | vst: program name or index inside the plugin (from `list_plugins` `detail`); omit for its default state |
| `plugin_state` | vst: a captured plugin state — **by name from the state library** or by path (see [open_plugin_state_editor](#open_plugin_state_editor)) — a TONEX tone, Guitar Rig rig or SampleTank/Syntronik patch; omit it to use the plugin's **default state** (`"none"` for the bare plugin). Applied before `plugin_preset`/`plugin_params`, so those can still tweak the captured sound |
| `plugin_params` | vst: parameter values applied after the preset, keyed by parameter name (or index) — a number (`{"Gain": 0.5}`) or a value name for a choice parameter (`{"Osc 1 Type": "Classic"}`). How plugins **without** VST3 programs pick a patch: find the selector with `list_plugins` `parameter_search`, list its values with `parameter_values`, set one by name |
| `bank` | CC0 bank select, 0–127 (`0` is the melodic bank, `128` the percussion bank — FluidR3's kits); omit to keep the file's bank (a track on MIDI channel 10 always plays the percussion bank) |
| `program` | MIDI program 0–127: a GM instrument number minus 1, or a drum kit on a drum track; omit to keep the file's program. [`list_instruments`](#list_instruments) returns the real preset table of the soundfont |
| `sections` | Per-section overrides keyed by section name (from [set_song_sections](#set_song_sections)), e.g. `{"Chorus": {"program": 81}}`. The change lands exactly at the section's start (bank select before the program change); sections not listed use the track's own program/bank, and notes already sounding keep their original preset. A section `preset` must be the **same .sf2** as the track's — per-section SoundFont switching is not supported yet. A section `gain_db` replaces the track's gain for that section: it is applied **exactly** to the rendered stem for every engine (baked as CC11 for fluidsynth, which cannot boost above 0 dB; sfizz/vst tracks take gain-only overrides) |
| `reverb_send`, `chorus_send` | CC91/CC93 effect sends (0–127; 0 = fully dry) — how much of FluidSynth's engine reverb/chorus this track gets, since the engine applies them by send amount. Omit to keep the file's own send (GM defaults: reverb 40, chorus 0). A track with no send gets a subtle reverb; raise it for a wetter part. FluidSynth tracks only — sfizz/vst tracks use the chain's `reverb` stage or a VST effect |
| `gain_db` | Trim for this stem when the stems are combined, −24 to +12 |
| `mute` | Muted: left out of stem renders and vocal mixes entirely, and baked as CC7=0 so external players hear it silent too |
| `effects` | VST3 effect plugins applied to this track's rendered stem, in order, e.g. `[{"plugin": "/vst/Comp.vst3"}, {"plugin": "/vst/Tape.vst3", "preset": "Warm", "params": {"Mix": 0.4}}, {"plugin": "/vst/Tonex.vst3", "state": "tonex_metal"}]`. An effect's `state` is a captured plugin state, by name or path (the plugin's default applies when omitted) — how TONEX gets its tone. Works for any engine; the host compensates each plugin's reported latency, so stems stay aligned. Needs the VST host |
| `chain` | Built-in processing chain for the rendered stem — the same stages as the vocal chain (`eq` → `de-esser` → `saturation` → `compressor`) plus an optional convolution `reverb` (no plugin host needed; `{"reverb": {"decay_s": 2.5, "damping": 0.4, "wet": 0.18}}`), applied before VST effects. How instrument grit, filtering, glue and space work without plugins: e.g. `{"eq": {"highpass_hz": 120}, "saturation": {"enabled": true, "drive_db": 9}}` |

Example — a synth bass, a power drum kit, and the keys pulled back:

```json
{"filename": "song.mid", "tracks": {
  "Bass":   {"engine": "fluidsynth", "program": 38, "gain_db": -3},
  "Drums":  {"engine": "fluidsynth", "program": 16},
  "Melody": {"engine": "sfizz", "sfz": "/sfz/keys.sfz", "gain_db": -2}
}}
```

Then `render_audio` with `stems: true` renders each track through its spec and sums the stems
(each with its `gain_db`) into the mix; the result lists every stem so you can listen one by one.
[`mix_song_with_vocals`](singing.md#mixing-and-export) honors the same specs for the backing
(with `backing_levels` overriding a spec's gain). Unknown track names, missing soundfonts and
unknown engines are rejected with a message that lists what is available, and a requested
bank/program is checked against the soundfont it will play from — a miss is an error that lists
what that bank actually has, so nothing falls back silently. The result echoes the **resolved
preset** per track (e.g. `"128-016 Power"`; bank and program are 0-based) and the same
**`effective`** block as [`list_instruments`](#list_instruments) — what every track will
actually play, with its source and any warnings. The specs follow the file through rename and
delete.

**Section overrides** switch a track's program/bank mid-song without splitting it into separate
tracks: `{"sections": {"Intro": {"program": 24}, "Climax": {"program": 30}}}`. The events land at
the section boundaries with held notes untouched; per-section `.sf2` switching (different
SoundFont files) is not supported yet and is rejected with a clear message.

**sfizz tracks** (`engine: "sfizz"`) play a sampled `.sfz` instrument instead of General MIDI:
pass `sfz` and leave `preset`/`bank`/`program`/`reverb_send`/`chorus_send` unset — those are
FluidSynth options and are rejected; section overrides may set `gain_db` only. sfizz has no
engine reverb/chorus (use the [mix tools'](singing.md#mixing-and-export) returns for shared
effects), and sfizz tracks render only with `render_audio` `stems: true` (or
[`mix_song_with_vocals`](singing.md#mixing-and-export) for the backing). `effective` labels them
`source: "sfz"` with a null bank/program. **Sustain comes from the instrument**: a sample with
no loop plays once per note and then falls silent — loop points and `ampeg_release` in the
`.sfz` decide how notes hold. Stems are trimmed and padded to the song's own length, like
FluidSynth stems.

**vst tracks** (`engine: "vst"`) render a `.vst3` instrument offline through the separate VST
host: the Docker image builds the Linux host, and Windows servers use the released
`vst_host.exe` (see [VST plugins](vst.md)). Pass `plugin` — a path to a `.vst3` under
`CHORDSMITH_VST_DIR` (`list_plugins` lists what is there) — and optionally `plugin_preset`, a
program name or index from `list_plugins` with `detail`. vst tracks render only with `stems:
true` (or [`mix_song_with_vocals`](singing.md#mixing-and-export) for the backing), reject
FluidSynth options and section overrides, and cannot be baked into a MIDI (a `.mid` cannot
carry a plugin). Effect plugins go in a spec's `effects` chain (see above), which applies to
any track's stem.

To use your own `.sf2` files, put them where the server can read them and pass that path as
`preset`: drop them into the output folder (visible to the server as `/data/…` in the Docker
image) or mount a folder into the container and use its container path, e.g. `/sf2/MyBass.sf2`.
`.sfz` instruments work the same way with `sfz`; the Docker image mounts a folder at `/sfz`
(`CHORDSMITH_SFZ_DIR`) and [`list_instruments`](#list_instruments) lists the `.sfz` files found
there, so the paths are easy to pick.

## list_instruments

No options, or a `filename` and/or a `soundfont` path. Read-only. Returns the rendering engines
this server can run — their availability, the default soundfont and notes — the **actual
preset table** of the loaded (or given) `.sf2`: every preset's name, bank and program, both
0-based, so the numbers `set_track_instrument` takes are verifiable, not assumed (FluidR3 reports
189 presets, its drum kits in bank 128 — e.g. `128-016 Power`), `sfz_libraries`: the `.sfz`
files found under `CHORDSMITH_SFZ_DIR` (name + path) when it is set, null otherwise — the
pickable list for sfizz specs — and `vst_plugins`: the `.vst3` plugins found under
`CHORDSMITH_VST_DIR`, the pickable list for vst specs. When no soundfont file is readable, a
static GM reference list is returned instead. With a filename, also returns:

- each track's **stored spec** and the preset label each spec resolves to (`resolved` — null
  when a spec sets no bank/program, or the `.sfz` file name for sfizz specs);
- an **`effective`** block: what every note track will actually play — a spec's program/bank
  first, then the file's own `program_change`/CC0 on that channel (scanned across all tracks),
  then the default (program 0; **bank 128 on channel 10**). Each entry carries the 1-based
  `channel`, `bank`, `program`, the resolved `label`, a `source` (`spec`/`file`/`default`/`sfz`),
  the `soundfont` it was looked up in (null for sfizz), and a `warning` for shared channels, a
  later program change on the channel, or a preset missing from the SoundFont. It shows what is
  *meant* to play, not proof of how the audio sounds.

Engines run as **separate processes**, so a crashing or differently licensed engine can never
take the server down.

## list_plugins

No options, or a `detail` value; `name_contains` filters the scan (case-insensitive). Read-only.
Lists the `.vst3` plugins found under `CHORDSMITH_VST_DIR` (Docker: `/vst`) as name + path — the
values `set_track_instrument` takes as `plugin` — plus the resolved `host` binary. With `detail`
(a path, or a name from the scan) the VST host loads that plugin and returns each of its types:
vendor, category, whether it is an instrument, its `programs` (the values `plugin_preset` takes),
parameter count and `latency_samples`, plus its captured `states` and `default_state`. `parameters: true` (optionally with `parameter_search`)
returns the parameter list — index, name, steps, current text — the keys `plugin_params` takes;
`parameter_values: <index>` dumps a parameter's value strings (a patch selector's list, for
example) so a value can be picked by name; for parameters with no discrete steps — many VST3
choice parameters — the host scans the range and lists the distinct texts it finds. Plugins are
user-supplied and never bundled; the host is a separate process, so a crashing plugin cannot take
the server down. With a VST bridge configured the scan, the renders and the state captures come
from the bridge's machine too (see [VST plugins](vst.md)); otherwise see that page for setup on
Windows and Docker, and free plugin suggestions.

## open_plugin_state_editor

`plugin` (a path, or a name from `list_plugins`) and an optional `state_name` (stored under
`states/` in the output folder, `.state` added). Returns immediately with the path the state
will be written to: a window opens on the **server's desktop** showing the plugin's own UI —
load the tone/rig/patch in it, then close the window, and the plugin state is saved to that
path. Reference the file from a vst spec's `plugin_state` or an effect's `state`, and every
render loads it headlessly. This is how GUI-only plugins join a session: TONEX tones, Guitar Rig
rigs, SampleTank/Syntronik patches. Needs a desktop session — the headless Linux Docker server
refuses it (capture on a native/desktop server; the state file then renders anywhere). The shell
equivalent: `vst_host --editor --plugin X.vst3 --save-state out.state` (and `--dump-state FILE`
for a GUI-free snapshot).

## list_plugin_states

No options, or a `plugin` filter (a path or a name from `list_plugins`). Read-only. Lists the
state library: each captured `.state` with its name, path, plugin, size and saved time, plus a
plugin-name → default-state map. A spec's `plugin_state`/effect `state` takes a state by name
from here; the default is used when a spec names the plugin but no state.

## set_plugin_default_state

`plugin` (a path or a name from `list_plugins`) and `state` (a name from `list_plugin_states`
or a path); pass `null` (or `"none"`) to clear. Marks the state as the plugin's default, used
whenever a spec names the plugin but gives no state. Returns the plugin and the state now used
by default. See [open_plugin_state_editor](#open_plugin_state_editor).

## list_chord_types

No options. Returns every chord quality: its name, the symbols you can type, intervals, the notes
of the C version and a description. Includes maj, m, dim, aug, sus2, sus4, 5, 6, m6, 7, maj7,
m7, mMaj7, dim7, m7b5, aug7, 7sus4, add9, madd9, 6/9, 9, maj9, m9, 7b9, 7#9, 11, m11, 13,
maj13 and m13.

## transpose_midi

Creates a transposed copy of a file. The original is kept.

| Option | Description |
|---|---|
| `filename` (required) | A file in the output folder, e.g. `"melancholy.mid"` |
| `semitones` | −24 to 24. `2` = up a whole step, `-12` = down an octave |
| `from_key` + `to_key` | Instead of semitones, e.g. `"A minor"` → `"C minor"`. Picks the smallest move (up to 6 semitones up or down) |
| `output_filename` | Default: `<name>_up3.mid` / `<name>_down2.mid` |
| `overwrite` | Replace an existing output file |

Drum notes (MIDI channel 10) are not changed. The chord-name markers are renamed too.

## analyze_midi

Reads a MIDI file and guesses the chord in each bar.

| Option | Description |
|---|---|
| `filename` (required) | A file in the output folder |
| `window_beats` | Size of each analysis window in beats. Default: one bar. Use `2` if chords change every half bar. |

Returns `tempo_bpm` and `time_signature` (the values the file *starts* with), the full
`tempo_changes` and `time_signature_changes` lists (beat + value), the `sections` map
([set_song_sections](#set_song_sections)), `markers`, `key_signature_guess` (e.g.
`"C major / A minor"`), `progression` (e.g. `["Am", "F", "C", "G"]`) and `segments` with
start beat, length and notes of each chord.

If the file carries chord-name markers (ChordSmith files do), those names are used for a window
whenever their notes match the sound, so inversions, voice leading and added melody notes don't
change the reported chords.

**Limits:** this is a simple analyser. It works well on files with one chord per window, such as
files ChordSmith made. Without chord-name markers, melodies, drums or fast chord changes can
make it return `?` (unknown) or wrong chords. `N.C.` means no notes are sounding.

## list_generated_files

Returns the output folder path, a `total` count and its files (newest first), each with size,
date, a `kind` (`midi`/`wav`/`mp3`), an `empty` flag (0-byte crash leftovers) and a link
scheme (`midi://` or `audio://`). Rendered audio is listed too, so you can confirm a render,
stem or mix actually landed on disk.

| Option | Description |
|---|---|
| `name_contains` | Only files whose name contains this text (case-insensitive) |
| `kind` | Only `"midi"`, `"wav"` or `"mp3"` files |
| `limit` | Most files to return, newest first (default 200) |

## delete_midi_file and rename_midi_file

Tidy up the output folder. Both stay inside the folder; path tricks are cleaned up or rejected.

- `delete_midi_file(filename)` removes a file.
- `rename_midi_file(filename, new_name)` renames a file and refuses to overwrite an existing one.

---

## Resources

Resources are read-only data the assistant (or you, in apps that show them) can open.

| URI | Content |
|---|---|
| `chords://types` | Markdown table of all chord types and their formulas |
| `scales://{key}` | Notes, triads and seventh chords of a key, with Roman numerals. Examples: `scales://C-major`, `scales://A-minor`, `scales://Bb-mixolydian`, `scales://F%23-dorian` (`%23` = `#`; `F-sharp-dorian` also works) |
| `midi://{filename}` | The raw MIDI file (`audio/midi`), e.g. `midi://melancholy.mid` |

Example `scales://A-minor` result:

```json
{
  "key": "A minor",
  "scale_notes": ["A", "B", "C", "D", "E", "F", "G"],
  "triads": [
    {"numeral": "i", "chord": "Am"}, {"numeral": "ii°", "chord": "Bdim"},
    {"numeral": "III", "chord": "C"}, {"numeral": "iv", "chord": "Dm"},
    {"numeral": "v", "chord": "Em"}, {"numeral": "VI", "chord": "F"},
    {"numeral": "VII", "chord": "G"}
  ],
  "seventh_chords": ["..."]
}
```

## Prompts

Prompts are ready-made instructions. In Claude Desktop they appear under the **+** / attachment
menu.

| Prompt | Arguments | What it does |
|---|---|---|
| `compose_progression` | `mood` (required), `key`, `style`, `bars` | Asks the assistant to look up the key, choose and justify a progression, save it with sensible voicing/rhythm, and suggest a variation |
| `explain_progression` | `chords` (required), `key` | Asks for a plain-language explanation: numerals, function, why it sounds that way, variations |

## Errors

When something is wrong, the tool returns a clear message that the assistant can act on, for
example:

- `Unknown chord quality 'xyz'. Call list_chord_types to see supported qualities.`
- `'am' is not a chord symbol. Start with a capital note letter, e.g. Am, F#7, Cmaj7/E.`
- `File 'song.mid' not found. Available files: melancholy.mid, ...`
- Out-of-range values (tempo 500, inversion 9, ...) are rejected with the allowed range.
