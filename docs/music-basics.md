# Music cheat sheet

You don't need any music theory to use ChordSmith; the assistant handles that. This page helps
if you want to understand or tweak what it makes.

## Chord symbols

A chord symbol is a **root note** plus an optional **type**:

| You write | Meaning | Notes |
|---|---|---|
| `C` | C major (happy, stable) | C E G |
| `Am` | A minor (sad, soft) | A C E |
| `G7` | G dominant seventh (wants to resolve) | G B D F |
| `Fmaj7` | F major seventh (dreamy) | F A C E |
| `Bdim` | B diminished (tense) | B D F |
| `Dsus4` | D suspended 4th (open, unresolved) | D G A |
| `Cadd9` | C add 9 (bright, modern) | C E G D |
| `C/E` | C major with E in the bass ("slash chord") | E · C E G |

- Sharps and flats: `F#`, `Bb` (the symbols `♯` and `♭` also work).
- Root notes must be capital letters (`Am`, not `am`).
- See every supported type with the `list_chord_types` tool, or read `chords://types`.

## Roman numerals

Roman numerals describe chords **by their position in a key**, so the same progression works in
any key. `I–V–vi–IV` is `C–G–Am–F` in C major and `G–D–Em–C` in G major.

| Rule | Example (key of C major) |
|---|---|
| UPPER case = major chord | `I` = C, `IV` = F, `V` = G |
| lower case = minor chord | `ii` = Dm, `iii` = Em, `vi` = Am |
| `°` (or `o`, `dim`) = diminished | `vii°` = Bdim |
| `ø` = half-diminished seventh | `viiø7` = Bm7b5 |
| `+` = augmented | `III+` = E+ |
| add a number for extensions | `V7` = G7, `ii7` = Dm7, `Imaj7` = Cmaj7 |
| `b` / `#` in front = borrowed chord | `bVII` = Bb, `bVI` = Ab |
| `/` = secondary chord | `V7/V` = D7 (the "V of V") |

In **minor keys** the numerals follow the natural minor scale: `i–VI–III–VII` in A minor is
`Am–F–C–G`. `vii°` uses the raised leading note (`G#dim` in A minor), as in classical harmony.

You can separate numerals with dashes, spaces, commas or `|`: `"i–VI–III–VII"`, `"I V vi IV"`.

## Keys and modes

Write a key as a note plus a mode: `C major`, `A minor`, `D dorian`, `F# mixolydian`, `Bb`
(major is the default), `Am` (minor). Supported: major, minor, dorian, phrygian, lydian,
mixolydian, locrian, harmonic minor, melodic minor, plus major/minor pentatonic and blues
(the last three only for `scales://`, since they don't have 7 notes).

Read `scales://C-major`, `scales://A-minor` or `scales://F%23-dorian` (`%23` = `#`) to see the
notes and chords of a key.

## Famous progressions to try

| Progression | Feel | Example |
|---|---|---|
| `I–V–vi–IV` | Pop anthem | C G Am F |
| `vi–IV–I–V` | Emotional pop | Am F C G |
| `i–VI–III–VII` | Epic / melancholic | Am F C G |
| `ii7–V7–Imaj7` | Jazz | Dm7 G7 Cmaj7 |
| `I–vi–IV–V` | 50s doo-wop | C Am F G |
| `i–bVII–bVI–V` | Flamenco / dramatic ("Andalusian") | Am G F E |
| `I–IV–I–V` (12-bar style) | Blues / rock | C F C G |

## Voicing and rhythm in one minute

- **Voicing** is how a chord's notes are arranged. *Inversion* puts a note other than the root at
  the bottom. *Voice leading* picks inversions so the notes move as little as possible between
  chords (sounds smooth, like a real pianist). *Open* and *drop2* spread the notes out.
- **Rhythm** is how the chord is played over time: held (*block*), repeated (*pulse*), one note at
  a time (*arpeggio*), classical *Alberti* bass, or guitar-like *strum*.
