# Tools reference

Your AI assistant normally fills in these options for you. This page is for when you want to ask
for something specific ("use drop2 voicing") or understand a result.

- [create_chord_progression](#create_chord_progression)
- [create_progression_from_roman](#create_progression_from_roman)
- [Voicing options](#voicing-options)
- [Rhythm options](#rhythm-options)
- [add_track](#add_track)
- [get_midi_file](#get_midi_file)
- [render_audio](#render_audio)
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
  | Creates files | `create_chord_progression`, `create_progression_from_roman`, `add_track`, `transpose_midi`, `render_audio`, `render_singing`, `mix_song_with_vocals`, `export_vocal_song` | Writes new files; an existing file is never replaced unless you pass `overwrite: true` |
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
`structuredContent` / `isError`. On the wire, ChordSmith currently negotiates protocol revision
**2025-11-25**, the latest supported by the official MCP Python SDK v1.x; the tool fields above
are common to both revisions (icons are optional and not used yet).

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
original is never modified. A type 0 file is promoted to type 1.

| Option | Description |
|---|---|
| `filename` (required) | Existing file in the output folder |
| `track_name` (required) | Name for the new track, e.g. `"Melody"` |
| `notes` (required) | List of `{"pitch": "E6", "start_beat": 0, "beats": 0.5, "velocity": 80}`. Pitch is a note name with octave (`E6`, `Bb3`, `C#-1`) or a MIDI number 0–127. Up to 5000 notes |
| `instrument` | General MIDI program for the track (0 piano, 24 nylon guitar, …) |
| `channel` | MIDI channel 1–16 (10 = drums) |
| `loop` | `{"length_beats": 4, "times": 36}`: the notes are positions inside the pattern window and repeat back to back, so a drum or bass groove is written once. The notes must fit inside `length_beats`; up to 20 000 notes after tiling |
| `output_filename` | Default: `<name>_<track_name>.mid` |
| `overwrite` | Replace an existing output file |

**Example:** a one-bar drum groove (kick on 1, snare on 2 and 4, hats on the offbeats) for a
48-bar song: write the notes once inside beats 0–4 and pass
`"loop": {"length_beats": 4, "times": 48}` instead of 192 hand-written notes.

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

## render_audio

Renders a MIDI file to audio with FluidSynth, so a sketch can be heard without a music app. The
Docker image includes FluidSynth, a General MIDI soundfont and ffmpeg; locally, install
`fluidsynth` and set `CHORDSMITH_SOUNDFONT` (and `CHORDSMITH_FLUIDSYNTH` if it is not on `PATH`).

| Option | Description |
|---|---|
| `filename` (required) | MIDI file in the output folder |
| `format` | `"wav"` (default) or `"mp3"` (mp3 needs ffmpeg) |
| `return_as` | Like [get_midi_file](#get_midi_file): `"base64"` or `"url"` |
| `soundfont` | Path to a `.sf2` file; default: `CHORDSMITH_SOUNDFONT` or a standard system path |
| `output_filename` | Default: `<name>_wav` / `<name>_mp3` |
| `expires_in`, `overwrite` | As above |

The result includes `mime_type`, `size_bytes`, `sha256`, and for wav the rendered
`duration_seconds`.

Renders use a General MIDI soundfont: no samples, sub/reese bass design, filter sweeps or
production FX. Treat them as an audition of the notes, not a finished master — open the `.mid` in
a DAW for the real sound. MIDI automation (volume curves, CC sweeps) is not written either.

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

Returns `tempo_bpm`, `time_signature`, `markers`, `key_signature_guess` (e.g.
`"C major / A minor"`), `progression` (e.g. `["Am", "F", "C", "G"]`) and `segments` with
start beat, length and notes of each chord.

If the file carries chord-name markers (ChordSmith files do), those names are used for a window
whenever their notes match the sound, so inversions, voice leading and added melody notes don't
change the reported chords.

**Limits:** this is a simple analyser. It works well on files with one chord per window, such as
files ChordSmith made. Without chord-name markers, melodies, drums or fast chord changes can
make it return `?` (unknown) or wrong chords. `N.C.` means no notes are sounding.

## list_generated_files

No options. Returns the output folder path and its `.mid` files (newest first), with size, date
and `midi://` link.

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
