"""Groove generators: named drum patterns and chord-aware bass lines.

Both are deterministic: the same chords and options always produce the same notes. Drum
patterns are 16th-note grids over GM drum pitches (``x`` hit, ``X`` accent, ``-`` rest); bass
lines follow the chord windows handed in by the caller (chord-name markers or analysis).
"""

from __future__ import annotations

from dataclasses import dataclass

from chordsmith.theory import Chord, MusicTheoryError

DRUM_STEP_BEATS = 0.25  # one sixteenth note
BASS_STYLES = ("root", "root_fifth", "eighth_pulse", "octave", "walking")


@dataclass(frozen=True)
class DrumPattern:
    """One named groove: ``bars`` bars of ``numerator``/4, one step string per GM voice."""

    name: str
    bars: int
    numerator: int
    description: str
    voices: dict[int, tuple[str, int]]  # GM drum pitch -> (steps, velocity)

    @property
    def beats(self) -> float:
        return self.bars * self.numerator

    def step_count(self) -> int:
        return round(self.beats / DRUM_STEP_BEATS)


def _repeat(steps: str, times: int) -> str:
    return steps * times


DRUM_PATTERNS: dict[str, DrumPattern] = {
    "four_on_floor": DrumPattern(
        "four_on_floor",
        1,
        4,
        "House kick on every beat with off-beat open hats and a clap on 2 and 4.",
        {
            36: ("x---x---x---x---", 105),
            39: ("----x-------x---", 100),
            46: ("----x-------x---", 85),
        },
    ),
    "backbeat": DrumPattern(
        "backbeat",
        1,
        4,
        "Pop/rock backbeat: kick on 1 and 3, snare on 2 and 4, straight eighth hats.",
        {
            36: ("x-------x-------", 100),
            38: ("----x-------x---", 95),
            42: ("x-x-x-x-x-x-x-x-", 75),
        },
    ),
    "rock": DrumPattern(
        "rock",
        1,
        4,
        "Driving rock: kick pickups into 3, accented eighth hats.",
        {
            36: ("x-------x---x---", 105),
            38: ("----x-------x---", 100),
            42: ("X-x-X-x-X-x-X-x-", 80),
        },
    ),
    "metal": DrumPattern(
        "metal",
        1,
        4,
        "Double-kick eighths under a 2-and-4 snare and ride.",
        {
            36: ("x-x-x-x-x-x-x-x-", 110),
            38: ("----x-------x---", 105),
            51: ("x-x-x-x-x-x-x-x-", 85),
        },
    ),
    "trap": DrumPattern(
        "trap",
        1,
        4,
        "Sparse 808 kick, half-time clap and sixteenth hats with rolls.",
        {
            36: ("x-------x---x---", 105),
            39: ("--------x-------", 95),
            42: ("x-xxx-x-x-x-xxx-", 70),
        },
    ),
    "boom_bap": DrumPattern(
        "boom_bap",
        1,
        4,
        "Classic hip-hop kick syncopation with a lazy backbeat (try swing 0.2).",
        {
            36: ("x-----x---x-----", 105),
            38: ("----x-------x---", 95),
            42: ("x-x-x-x-x-x-x-x-", 70),
        },
    ),
    "bossa_nova": DrumPattern(
        "bossa_nova",
        2,
        4,
        "Two-bar bossa nova: bass drum on 1 and 3, side-stick backbeat, shaker eighths.",
        {
            36: (_repeat("x-------x-------", 2), 90),
            37: (_repeat("----x-------x---", 2), 80),
            70: (_repeat("x-x-x-x-x-x-x-x-", 2), 65),
        },
    ),
    "shuffle": DrumPattern(
        "shuffle",
        1,
        4,
        "Blues shuffle feel (set swing 0.33): backbeat with a walking kick.",
        {
            36: ("x-------x-------", 100),
            38: ("----x-------x---", 95),
            42: ("x-x-x-x-x-x-x-x-", 75),
        },
    ),
    "waltz": DrumPattern(
        "waltz",
        1,
        3,
        "3/4 waltz: kick on 1, snare on 2, a hat for each beat.",
        {
            36: ("x-----------", 95),
            38: ("----x-------", 90),
            42: ("x---x---x---", 70),
        },
    ),
}

PATTERN_NAMES = tuple(DRUM_PATTERNS)


def drum_notes(pattern: DrumPattern) -> list[tuple[int, float, float, int]]:
    """The pattern's notes as (pitch, start_beat, beats, velocity), relative to its start."""
    notes: list[tuple[int, float, float, int]] = []
    steps_per_voice = pattern.step_count()
    for pitch, (steps, velocity) in pattern.voices.items():
        step = 0
        for char in steps:
            if char in "| \t":
                continue
            if char in "-._":
                step += 1
                continue
            if char in "xXoO":
                hit_velocity = min(127, velocity + 25) if char in "XO" else velocity
                notes.append((pitch, step * DRUM_STEP_BEATS, DRUM_STEP_BEATS, hit_velocity))
                step += 1
                continue
            raise MusicTheoryError(
                f"Unknown step '{char}' in the '{pattern.name}' pattern for pitch {pitch}."
            )
        if step != steps_per_voice:
            raise MusicTheoryError(
                f"The '{pattern.name}' pattern for pitch {pitch} has {step} steps; "
                f"{steps_per_voice} are needed for {pattern.bars} bar(s) of {pattern.numerator}/4."
            )
    return notes


def _pitch_of(pitch_class: int, octave: int) -> int:
    return 12 * (octave + 1) + pitch_class % 12


def _third_semitones(chord: Chord) -> int:
    intervals = chord.chord_type.intervals
    return 4 if len(intervals) > 1 and intervals[1] == 4 else 3


def bassline_notes(
    style: str,
    chords: list[tuple[float, float, Chord]],
    *,
    octave: int = 2,
    subdivision: float = 0.5,
    gate: float = 0.9,
    velocity: int = 90,
) -> list[tuple[int, float, float, int]]:
    """Generate one bass note per slot as (pitch, start_beat, beats, velocity).

    ``chords`` are ``(start_beat, length_beats, chord)`` windows in order; the last window may
    end before the song does (a tail rest is fine).
    """
    if style not in BASS_STYLES:
        raise MusicTheoryError(f"Unknown bass style '{style}'. Styles: {', '.join(BASS_STYLES)}.")
    if not chords:
        raise MusicTheoryError("No chords to build a bass line from.")
    notes: list[tuple[int, float, float, int]] = []
    for index, (start, length, chord) in enumerate(chords):
        root = _pitch_of(chord.root, octave)
        next_root = _pitch_of(chords[index + 1][2].root, octave) if index + 1 < len(chords) else root + 7
        approach = next_root - 1 if next_root - 1 != root else next_root - 2
        if style == "root":
            notes.append((root, start, max(0.25, length * gate), velocity))
        elif style in ("root_fifth", "eighth_pulse", "octave"):
            step = subdivision
            slot = 0
            position = 0.0
            while position + 1e-9 < length:
                slot_length = min(step, length - position)
                pitch = root
                if style == "root_fifth" and slot % 2:
                    pitch = root + 7
                elif style == "octave" and slot % 2:
                    pitch = root + 12
                notes.append((pitch, start + position, max(0.1, slot_length * gate), velocity))
                slot += 1
                position += step
        elif style == "walking":
            sequence = [root, root + 7, root + _third_semitones(chord), approach]
            slot = 0
            position = 0.0
            while position + 1e-9 < length:
                slot_length = min(1.0, length - position)
                notes.append(
                    (
                        sequence[slot % len(sequence)],
                        start + position,
                        max(0.2, slot_length * gate),
                        velocity,
                    )
                )
                slot += 1
                position += 1.0
    return [(max(0, min(127, pitch)), start, beats, velocity) for pitch, start, beats, velocity in notes]
