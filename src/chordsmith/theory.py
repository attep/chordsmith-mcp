"""Music theory core: notes, chord symbols, keys, scales and Roman numerals."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


class MusicTheoryError(ValueError):
    """Raised when a chord symbol, key or numeral cannot be understood."""


_LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def _normalize(text: str) -> str:
    return (
        text.strip()
        .replace("♯", "#")
        .replace("♭", "b")
        .replace("–", "-")
        .replace("—", "-")
        .replace("Δ", "maj")
    )


def parse_note(name: str) -> int:
    """Return the pitch class (0-11) of a note name such as 'C', 'F#', 'Bb'."""
    text = _normalize(name)
    match = re.fullmatch(r"([A-Ga-g])([#b]*)", text)
    if not match:
        raise MusicTheoryError(f"'{name}' is not a note name. Use e.g. C, F#, Bb.")
    letter, accidentals = match.groups()
    return (_LETTER_PC[letter.upper()] + accidentals.count("#") - accidentals.count("b")) % 12


def note_name(pc: int, prefer_flats: bool = False) -> str:
    return (_FLAT_NAMES if prefer_flats else _SHARP_NAMES)[pc % 12]


def midi_note_name(note: int, prefer_flats: bool = False) -> str:
    """60 -> 'C4' (scientific pitch notation, middle C = C4)."""
    return f"{note_name(note % 12, prefer_flats)}{note // 12 - 1}"


def note_name_with_octave(name: str, note: int) -> str:
    """Attach the correct scientific octave to a spelled name: B#4, Cb4, Bbb4.

    The natural-letter formula (note // 12 - 1) is wrong for names that cross an octave
    boundary (MIDI 72 is C5 but also B#4; MIDI 59 is B3 but also Cb4).
    """
    match = re.fullmatch(r"([A-G])([#b]*)", name)
    if not match:
        return f"{name}{note // 12 - 1}"
    letter, accidentals = match.groups()
    accidental = accidentals.count("#") - accidentals.count("b")
    return f"{name}{(note - accidental) // 12 - 1}"


def parse_pitch(value: str | int, default_octave: int = 4) -> int:
    """Return a MIDI note number (0-127) for 'E6', 'Bb3', 'C#-1' or a plain number.

    Note names without an octave (e.g. 'C') use ``default_octave``.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise MusicTheoryError(f"'{value}' is not a pitch. Use e.g. E6 or 88.")
    if isinstance(value, int):
        if not 0 <= value <= 127:
            raise MusicTheoryError(f"MIDI note {value} is outside the valid range 0-127.")
        return value
    text = _normalize(str(value)).replace(" ", "")
    match = re.fullmatch(r"([A-Ga-g])([#b]*)(-?\d+)?", text)
    if not match:
        raise MusicTheoryError(f"'{value}' is not a pitch. Use e.g. E6, Bb3 or a MIDI number 0-127.")
    letter, accidentals, octave = match.groups()
    octave_number = int(octave) if octave is not None else default_octave
    note = parse_note(letter + accidentals) + 12 * (octave_number + 1)
    if not 0 <= note <= 127:
        raise MusicTheoryError(f"'{value}' is outside the MIDI range 0-127.")
    return note


@dataclass(frozen=True)
class ChordType:
    name: str
    intervals: tuple[int, ...]
    aliases: tuple[str, ...]
    description: str


CHORD_TYPES: tuple[ChordType, ...] = (
    ChordType("maj", (0, 4, 7), ("", "maj", "M", "major"), "Major triad"),
    ChordType("min", (0, 3, 7), ("m", "min", "-", "minor"), "Minor triad"),
    ChordType("dim", (0, 3, 6), ("dim", "°", "o"), "Diminished triad"),
    ChordType("aug", (0, 4, 8), ("aug", "+"), "Augmented triad"),
    ChordType("sus2", (0, 2, 7), ("sus2",), "Suspended 2nd"),
    ChordType("sus4", (0, 5, 7), ("sus4", "sus"), "Suspended 4th"),
    ChordType("5", (0, 7), ("5",), "Power chord (root + fifth)"),
    ChordType("6", (0, 4, 7, 9), ("6",), "Major sixth"),
    ChordType("m6", (0, 3, 7, 9), ("m6", "min6"), "Minor sixth"),
    ChordType("7", (0, 4, 7, 10), ("7", "dom7"), "Dominant seventh"),
    ChordType("maj7", (0, 4, 7, 11), ("maj7", "M7", "ma7"), "Major seventh"),
    ChordType("m7", (0, 3, 7, 10), ("m7", "min7", "-7"), "Minor seventh"),
    ChordType("mMaj7", (0, 3, 7, 11), ("mMaj7", "mM7", "m(maj7)", "minmaj7"), "Minor-major seventh"),
    ChordType("dim7", (0, 3, 6, 9), ("dim7", "°7", "o7"), "Diminished seventh"),
    ChordType("m7b5", (0, 3, 6, 10), ("m7b5", "ø", "ø7", "min7b5"), "Half-diminished seventh"),
    ChordType("aug7", (0, 4, 8, 10), ("aug7", "7#5", "+7"), "Augmented seventh"),
    ChordType("7sus4", (0, 5, 7, 10), ("7sus4", "7sus"), "Dominant seventh, suspended 4th"),
    ChordType("add9", (0, 4, 7, 14), ("add9", "add2"), "Major triad + 9th"),
    ChordType("madd9", (0, 3, 7, 14), ("madd9", "m(add9)", "madd2"), "Minor triad + 9th"),
    ChordType("6/9", (0, 4, 7, 9, 14), ("6/9", "69"), "Major sixth + 9th"),
    ChordType("9", (0, 4, 7, 10, 14), ("9", "dom9"), "Dominant ninth"),
    ChordType("maj9", (0, 4, 7, 11, 14), ("maj9", "M9"), "Major ninth"),
    ChordType("m9", (0, 3, 7, 10, 14), ("m9", "min9"), "Minor ninth"),
    ChordType("7b9", (0, 4, 7, 10, 13), ("7b9",), "Dominant seventh, flat 9"),
    ChordType("7#9", (0, 4, 7, 10, 15), ("7#9",), "Dominant seventh, sharp 9 ('Hendrix chord')"),
    ChordType("11", (0, 4, 7, 10, 14, 17), ("11",), "Dominant eleventh"),
    ChordType("m11", (0, 3, 7, 10, 14, 17), ("m11", "min11"), "Minor eleventh"),
    ChordType("13", (0, 4, 7, 10, 14, 21), ("13",), "Dominant thirteenth (no 11th)"),
    ChordType("maj13", (0, 4, 7, 11, 14, 21), ("maj13", "M13"), "Major thirteenth (no 11th)"),
    ChordType("m13", (0, 3, 7, 10, 14, 21), ("m13", "min13"), "Minor thirteenth (no 11th)"),
)

CHORD_TYPES_BY_NAME = {ct.name: ct for ct in CHORD_TYPES}
_ALIASES: dict[str, ChordType] = {}
for _ct in CHORD_TYPES:
    for _alias in _ct.aliases:
        _ALIASES[_alias] = _ct
_ALIASES_LOWER: dict[str, ChordType] = {}
for _alias, _ct in _ALIASES.items():
    if len(_alias) >= 3 and _alias.lower() not in _ALIASES_LOWER:
        _ALIASES_LOWER[_alias.lower()] = _ct


def _did_you_mean(text: str, candidates: list[str]) -> str:
    """A ' Did you mean ...?' hint for error messages, or an empty string."""
    matches = difflib.get_close_matches(text.lower(), [c.lower() for c in candidates], n=1, cutoff=0.6)
    if not matches:
        return ""
    original = next(c for c in candidates if c.lower() == matches[0])
    return f" Did you mean '{original}'?"


def lookup_chord_type(suffix: str) -> ChordType:
    ct = _ALIASES.get(suffix) or _ALIASES_LOWER.get(suffix.lower())
    if ct is None:
        aliases = [alias for alias in _ALIASES if alias]
        raise MusicTheoryError(
            f"Unknown chord quality '{suffix}'.{_did_you_mean(suffix, aliases)} "
            "Call list_chord_types to see supported qualities."
        )
    return ct


@dataclass(frozen=True)
class Chord:
    symbol: str
    root: int
    chord_type: ChordType
    bass: int | None = None
    prefer_flats: bool = False
    root_name: str | None = None

    @property
    def pitch_classes(self) -> list[int]:
        return [(self.root + i) % 12 for i in self.chord_type.intervals]

    @property
    def note_names(self) -> list[str]:
        if self.root_name:
            spelled = spell_chord(self.root_name, self.chord_type.intervals)
            if spelled is not None:
                return spelled
        return [note_name(pc, self.prefer_flats) for pc in self.pitch_classes]


_LETTERS = ("C", "D", "E", "F", "G", "A", "B")
# Semitones from the root to each interval's diatonic letter (3rd = 2 letters, 7th = 6, 9th = 1, ...).
_INTERVAL_LETTER_STEPS = {
    0: 0,
    2: 1,
    3: 2,
    4: 2,
    5: 3,
    6: 4,
    7: 4,
    8: 4,
    9: 5,
    10: 6,
    11: 6,
    13: 1,
    14: 1,
    15: 1,
    17: 3,
    21: 5,
}
_ACCIDENTALS = {0: "", 1: "#", 2: "##", 10: "bb", 11: "b"}


def spell_chord(root_name: str, intervals: tuple[int, ...]) -> list[str] | None:
    """Spell chord tones with correct letters and accidentals: C7 -> C E G Bb, D7 -> D F# A C.

    Returns None when the root or an interval cannot be spelled (caller falls back to pitch classes).
    """
    match = re.fullmatch(r"([A-G])([#b]*)", _normalize(root_name))
    if not match:
        return None
    letter = match.group(1)
    root_pc = parse_note(root_name)
    root_index = _LETTERS.index(letter)
    names = []
    for interval in intervals:
        steps = _INTERVAL_LETTER_STEPS.get(interval)
        if interval == 9 and 6 in intervals:
            steps = 6  # a diminished seventh (Cdim7 = C Eb Gb Bbb), not a sixth
        if steps is None:
            return None
        letter_index = (root_index + steps) % 7
        natural_pc = _LETTER_PC[_LETTERS[letter_index]]
        accidental = _ACCIDENTALS.get((root_pc + interval - natural_pc) % 12)
        if accidental is None:
            return None
        names.append(_LETTERS[letter_index] + accidental)
    return names


_CHORD_RE = re.compile(r"([A-G])([#b]?)(.*?)(?:/([A-Ga-g][#b]?))?")


def parse_chord(symbol: str) -> Chord:
    """Parse a chord symbol like 'Am', 'F#m7b5', 'Cmaj7/E' or 'Bb6/9'."""
    text = _normalize(symbol).replace(" ", "")
    match = _CHORD_RE.fullmatch(text)
    if not match:
        raise MusicTheoryError(
            f"'{symbol}' is not a chord symbol. Start with a capital note letter, e.g. Am, F#7, Cmaj7/E."
        )
    letter, accidental, suffix, bass = match.groups()
    root = parse_note(letter + accidental)
    chord_type = lookup_chord_type(suffix)
    return Chord(
        symbol=symbol.strip(),
        root=root,
        chord_type=chord_type,
        bass=parse_note(bass) if bass else None,
        prefer_flats=_chord_prefers_flats(letter, accidental, chord_type),
        root_name=letter.upper() + accidental,
    )


def _chord_prefers_flats(letter: str, accidental: str, chord_type: ChordType) -> bool:
    """Spelling heuristic for display only: Cm -> C Eb G, F7 -> F A C Eb, Bm -> B D F#."""
    if accidental:
        return accidental == "b"
    if letter == "F":
        return True
    has_minor_third = 3 in chord_type.intervals
    return has_minor_third and not (letter == "B" and 7 in chord_type.intervals)


SCALES: dict[str, tuple[int, ...]] = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "melodic_minor": (0, 2, 3, 5, 7, 9, 11),
    "major_pentatonic": (0, 2, 4, 7, 9),
    "minor_pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10),
}
_MODE_ALIASES = {
    "": "major",
    "maj": "major",
    "ionian": "major",
    "m": "minor",
    "min": "minor",
    "aeolian": "minor",
    "natural_minor": "minor",
}
_FLAT_MAJOR_TONICS = {5, 10, 3, 8, 1, 6}  # F Bb Eb Ab Db Gb
_MODE_OFFSET = {  # semitones from the mode's tonic down to its relative major
    "major": 0,
    "minor": 3,
    "dorian": 10,
    "phrygian": 8,
    "lydian": 7,
    "mixolydian": 5,
    "locrian": 1,
    "harmonic_minor": 3,
    "melodic_minor": 3,
    "major_pentatonic": 0,
    "minor_pentatonic": 3,
    "blues": 3,
}


@dataclass(frozen=True)
class Key:
    tonic: int
    mode: str
    tonic_name: str

    @property
    def intervals(self) -> tuple[int, ...]:
        return SCALES[self.mode]

    @property
    def is_minor_family(self) -> bool:
        return self.mode in {
            "minor",
            "harmonic_minor",
            "melodic_minor",
            "dorian",
            "phrygian",
            "locrian",
            "minor_pentatonic",
            "blues",
        }

    @property
    def prefer_flats(self) -> bool:
        if "b" in self.tonic_name[1:]:
            return True
        if "#" in self.tonic_name:
            return False
        return (self.tonic + _MODE_OFFSET[self.mode]) % 12 in _FLAT_MAJOR_TONICS

    @property
    def name(self) -> str:
        return f"{self.tonic_name} {self.mode.replace('_', ' ')}"

    def scale_notes(self) -> list[str]:
        return [note_name(self.tonic + i, self.prefer_flats) for i in self.intervals]


def parse_key(text: str) -> Key:
    """Parse 'C', 'C major', 'Am', 'A minor', 'F# dorian', 'Bb-mixolydian', 'F sharp minor'."""
    raw = _normalize(text)
    cleaned = re.sub(r"[\s\-_]+", " ", raw).strip()
    cleaned = re.sub(r"(?i)^([A-G])\s*sharp\b", r"\1#", cleaned)
    cleaned = re.sub(r"(?i)^([A-G])\s*flat\b", r"\1b", cleaned)
    match = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(.*)", cleaned)
    if not match:
        raise MusicTheoryError(f"'{text}' is not a key. Use e.g. 'C major', 'A minor', 'D dorian'.")
    letter, accidental, mode_text = match.groups()
    mode = mode_text.strip().lower().replace(" ", "_")
    mode = _MODE_ALIASES.get(mode, mode)
    if mode not in SCALES:
        raise MusicTheoryError(
            f"Unknown mode/scale '{mode_text}'.{_did_you_mean(mode_text, list(SCALES))} "
            f"Supported: {', '.join(sorted(SCALES))}."
        )
    tonic_name = letter.upper() + accidental
    return Key(tonic=parse_note(tonic_name), mode=mode, tonic_name=tonic_name)


_ROMAN_VALUES = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7}
_ROMAN_RE = re.compile(r"([#b]?)(vii|vi|v|iv|iii|ii|i)(.*)", re.IGNORECASE)
# Numeral suffix -> (quality for UPPER-case numeral, quality for lower-case numeral)
_ROMAN_SUFFIXES: dict[str, tuple[str, str]] = {
    "": ("maj", "min"),
    "7": ("7", "m7"),
    "maj7": ("maj7", "mMaj7"),
    "M7": ("maj7", "mMaj7"),
    "6": ("6", "m6"),
    "9": ("9", "m9"),
    "maj9": ("maj9", "maj9"),
    "add9": ("add9", "madd9"),
    "11": ("11", "m11"),
    "13": ("13", "m13"),
    "°": ("dim", "dim"),
    "o": ("dim", "dim"),
    "dim": ("dim", "dim"),
    "°7": ("dim7", "dim7"),
    "o7": ("dim7", "dim7"),
    "dim7": ("dim7", "dim7"),
    "ø": ("m7b5", "m7b5"),
    "ø7": ("m7b5", "m7b5"),
    "m7b5": ("m7b5", "m7b5"),
    "+": ("aug", "aug"),
    "aug": ("aug", "aug"),
    "+7": ("aug7", "aug7"),
}
_MAJOR = SCALES["major"]


def _degree_root(key: Key, accidental: str, degree: int) -> int:
    if len(key.intervals) != 7:
        raise MusicTheoryError(
            f"Roman numerals need a 7-note key; '{key.name}' has {len(key.intervals)} notes."
        )
    if accidental:
        # With an accidental, the numeral is measured against the major scale (bVII = flat 7th).
        shift = 1 if accidental == "#" else -1
        return (key.tonic + _MAJOR[degree - 1] + shift) % 12
    return (key.tonic + key.intervals[degree - 1]) % 12


def _numeral_quality(numeral: str, suffix: str) -> str:
    if numeral != numeral.upper() and numeral != numeral.lower():
        raise MusicTheoryError(f"Numeral '{numeral}' mixes upper and lower case.")
    upper = numeral.isupper()
    if suffix in _ROMAN_SUFFIXES:
        return _ROMAN_SUFFIXES[suffix][0 if upper else 1]
    if not upper and ("m" + suffix) in _ALIASES:
        return _ALIASES["m" + suffix].name
    return lookup_chord_type(suffix).name


def roman_to_chord(numeral_text: str, key: Key) -> Chord:
    """Resolve a Roman numeral ('i', 'bVII', 'V7', 'vii°', 'V7/V') in a key to a Chord."""
    text = _normalize(numeral_text).replace(" ", "")
    target_key = key
    if "/" in text:
        text, target = text.split("/", 1)
        target_chord = roman_to_chord(target, key)
        target_mode = "minor" if target_chord.chord_type.intervals[1:2] == (3,) else "major"
        target_key = Key(
            tonic=target_chord.root,
            mode=target_mode,
            tonic_name=note_name(target_chord.root, key.prefer_flats),
        )
    match = _ROMAN_RE.fullmatch(text)
    if not match:
        raise MusicTheoryError(
            f"'{numeral_text}' is not a Roman numeral. Use e.g. I, ii, V7, bVII, vii°, V7/V."
        )
    accidental, numeral, suffix = match.groups()
    degree = _ROMAN_VALUES[numeral.lower()]
    quality = _numeral_quality(numeral, suffix)
    root = _degree_root(target_key, accidental, degree)
    # Leading-tone chords in minor keys use the raised 7th (harmonic minor), e.g. vii° in A minor = G#°.
    if degree == 7 and not accidental and target_key.mode == "minor" and quality in {"dim", "dim7", "m7b5"}:
        root = (root + 1) % 12
    raised_leading_tone = degree == 7 and root != _degree_root(target_key, accidental, degree)
    prefer_flats = accidental == "b" or (key.prefer_flats and not raised_leading_tone)
    symbol_suffix = CHORD_TYPES_BY_NAME[quality].aliases[0]
    symbol = note_name(root, prefer_flats) + symbol_suffix
    return Chord(
        symbol=symbol,
        root=root,
        chord_type=CHORD_TYPES_BY_NAME[quality],
        prefer_flats=prefer_flats,
        root_name=note_name(root, prefer_flats),
    )


def split_numerals(text: str) -> list[str]:
    """Split 'i–VI–III–VII' / 'I IV V' / 'ii, V7, I' into individual numerals."""
    parts = re.split(r"[\s,|\-–—]+", text.strip())
    return [p for p in parts if p]


def identify_chord(pitch_classes: set[int], bass_pc: int | None = None) -> tuple[int, ChordType] | None:
    """Find a (root, chord type) whose pitch classes equal the given set. Prefers the bass as root."""
    candidates = []
    for ct in CHORD_TYPES:
        pcs_template = {i % 12 for i in ct.intervals}
        if len(pcs_template) != len(pitch_classes):
            continue
        for root in range(12):
            if {(root + i) % 12 for i in pcs_template} == pitch_classes:
                candidates.append((root, ct))
    if not candidates:
        return None
    order = {ct.name: idx for idx, ct in enumerate(CHORD_TYPES)}
    candidates.sort(key=lambda c: (c[0] != bass_pc, order[c[1].name]))
    return candidates[0]
