# Tools reference

Your AI assistant normally fills in these options for you. This page is for when you want to ask
for something specific ("use drop2 voicing") or understand a result.

- [create_chord_progression](#create_chord_progression)
- [create_progression_from_roman](#create_progression_from_roman)
- [Voicing options](#voicing-options)
- [Rhythm options](#rhythm-options)
- [list_chord_types](#list_chord_types)
- [transpose_midi](#transpose_midi)
- [analyze_midi](#analyze_midi)
- [list_generated_files](#list_generated_files)
- [Resources](#resources)
- [Prompts](#prompts)
- [Errors](#errors)

Files are always saved in the output folder (default `~/ChordSmith`, see
[Settings](getting-started.md#settings)). File names are cleaned up automatically
(`"my song"` becomes `my_song.mid`), and an existing file is never replaced unless you set
`overwrite: true`. Otherwise the new file gets a number, e.g. `my_song_2.mid`.

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
| `subdivision` | `0.5` | Step length in beats for patterns that repeat. `1` = quarter notes, `0.5` = eighths, `0.25` = sixteenths |
| `velocity` | `90` | Loudness 1–127 |
| `gate` | `0.95` | How long each note sounds within its step (`1.0` = fully connected) |
| `strum_spread_ms` | `25` | Delay between "strings" for `strum` |

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

**Limits:** this is a simple analyser. It works well on files with one chord per window, such as
files ChordSmith made. With melodies, drums or fast chord changes it may return `?` (unknown)
or wrong chords. `N.C.` means no notes are sounding.

## list_generated_files

No options. Returns the output folder path and its `.mid` files (newest first), with size, date
and `midi://` link.

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
