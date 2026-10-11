"""Pydantic schemas shared by the MCP tools."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

VoicingStyle = Literal["close", "open", "drop2"]
RhythmPattern = Literal[
    "block", "pulse", "arpeggio_up", "arpeggio_down", "arpeggio_updown", "alberti", "strum"
]


class ChordEvent(BaseModel):
    """A chord with its own length, e.g. {"chord": "Am", "beats": 2}."""

    chord: str = Field(description="Chord symbol, e.g. 'Am', 'Cmaj7', 'G/B'.")
    beats: float = Field(gt=0, le=64, description="Length of this chord in beats (quarter notes).")


class NumeralEvent(BaseModel):
    """A Roman numeral with its own length, e.g. {"numeral": "V7", "beats": 2}."""

    numeral: str = Field(description="Roman numeral, e.g. 'i', 'bVII', 'V7', 'vii°', 'V7/V'.")
    beats: float = Field(gt=0, le=64, description="Length of this chord in beats (quarter notes).")


class NoteInput(BaseModel):
    """One note for add_track, e.g. {"pitch": "E6", "start_beat": 0, "beats": 0.5}.

    Position it either with ``start_beat`` (absolute beats from the start of the song) or with
    ``section`` + ``bar`` + ``beat`` (relative to a section of the song's section map — see
    set_song_sections). A note may run past its section's end; it is still placed normally.
    """

    pitch: str | int = Field(description="Note name with octave ('E6', 'Bb3', 'C#-1') or MIDI number 0-127.")
    start_beat: float | None = Field(
        None,
        ge=0,
        le=100000,
        description="Absolute start in beats from the start; give this, or section+bar+beat.",
    )
    beats: float = Field(gt=0, le=100000, description="Length of the note in beats (quarter notes).")
    velocity: int = Field(90, ge=1, le=127, description="Loudness, 1-127.")
    section: str | None = Field(
        None, description="Section name (from set_song_sections); positions the note relative to it."
    )
    bar: int | None = Field(None, ge=1, le=100000, description="1-based bar inside the section.")
    beat: float | None = Field(
        None, ge=1, le=100000, description="1-based beat inside the bar (floats fine: 2.5 = the 'and' of 2)."
    )

    @model_validator(mode="after")
    def _exactly_one_position(self) -> NoteInput:
        if self.section is None:
            if self.start_beat is None:
                raise ValueError("give start_beat, or section with bar/beat")
            if self.bar is not None or self.beat is not None:
                raise ValueError("bar/beat need a section")
        else:
            if self.start_beat is not None:
                raise ValueError("give either start_beat or section+bar+beat, not both")
            if self.bar is None:
                raise ValueError("section-relative notes need a bar")
        return self


class SongSection(BaseModel):
    """One song section, e.g. {"name": "Intro", "bars": 8, "bpm": 140}.

    ``bpm`` is always **quarter-note** BPM (a tempo change never silently becomes dotted-quarter
    BPM). ``grouping`` (e.g. "2+2+3") records accents for compound meters; it must sum to the
    numerator and does not change the bar's length.
    """

    name: str = Field(min_length=1, max_length=40, description="Section name, e.g. 'Intro', 'Chorus'.")
    bars: int = Field(ge=1, le=512, description="Length of the section in bars.")
    bpm: float = Field(ge=20, le=300, description="Quarter-note tempo in BPM for this section.")
    numerator: int = Field(4, ge=1, le=32, description="Time signature numerator (beats per bar).")
    denominator: Literal[1, 2, 4, 8, 16, 32] = Field(
        4, description="Time signature denominator (4 = quarter-note beat)."
    )
    grouping: str | None = Field(
        None, description="Optional accent grouping like '2+2+3'; must sum to the numerator."
    )

    @model_validator(mode="after")
    def _grouping_sums_to_numerator(self) -> SongSection:
        if self.grouping is not None:
            parts = [part.strip() for part in self.grouping.split("+")]
            if not parts or not all(part.isdigit() and int(part) > 0 for part in parts):
                raise ValueError("grouping must look like '2+2+3'")
            total = sum(int(part) for part in parts)
            if total != self.numerator:
                raise ValueError(f"grouping sums to {total}, not the numerator {self.numerator}")
        return self


class LoopSpec(BaseModel):
    """Tile a short pattern across the song, e.g. one bar of drums repeated 32 times.

    The notes are positions inside the pattern window (beat 0 up to ``length_beats``); the
    whole pattern repeats back to back ``times`` times, so a drum groove is written once.
    """

    length_beats: float = Field(
        gt=0, le=256, description="Pattern length in beats: one 4/4 bar = 4, one 2/4 bar = 2."
    )
    times: int = Field(ge=1, le=512, description="How many times the pattern repeats back to back.")


class Humanize(BaseModel):
    """Deterministic timing and velocity variation, so repeated renders sound identical."""

    timing_ms: float = Field(
        0, ge=0, le=50, description="Random timing offset of up to this many milliseconds per note."
    )
    velocity_range: int = Field(
        0, ge=0, le=60, description="Random velocity offset of up to +/- this many steps per note."
    )
    seed: int = Field(0, ge=0, le=2**31 - 1, description="Random seed; the same seed gives the same file.")


class Voicing(BaseModel):
    """How the notes of each chord are arranged (which octave, inversion, spacing)."""

    style: VoicingStyle = Field(
        "close",
        description="close = notes stacked tightly; open = middle note raised an octave; "
        "drop2 = second-highest note dropped an octave (jazz/piano sound).",
    )
    inversion: int = Field(
        0, ge=0, le=5, description="0 = root position, 1 = first inversion, 2 = second, ..."
    )
    octave: int = Field(4, ge=1, le=7, description="Octave of the chord root (4 = middle C).")
    voice_leading: bool = Field(
        False,
        description="If true, each chord after the first picks the inversion closest to the "
        "previous chord, giving smooth movement. Overrides 'inversion' after the first chord.",
    )
    add_bass: bool = Field(
        False, description="Add the root (or slash-chord bass note) one octave below the chord."
    )


class Rhythm(BaseModel):
    """How each chord is played over time."""

    pattern: RhythmPattern = Field(
        "block",
        description="block = hold the chord; pulse = repeat the chord every subdivision; "
        "arpeggio_up/down/updown = one note at a time; alberti = low-high-mid-high; "
        "strum = guitar-like strums alternating down/up.",
    )
    subdivision: float = Field(
        0.5,
        gt=0,
        le=4,
        description="Step length in beats for pulse/arpeggio/alberti/strum (0.5 = eighth notes).",
    )
    velocity: int = Field(90, ge=1, le=127, description="Loudness, 1-127.")
    gate: float = Field(0.95, gt=0, le=1, description="Fraction of each step the note sounds (1.0 = legato).")
    strum_spread_ms: float = Field(
        25, ge=0, le=200, description="Delay between strings in a strum, in milliseconds."
    )
    swing: float = Field(
        0,
        ge=0,
        le=0.75,
        description="Delays every offbeat step by this fraction of the step: 0 = straight, "
        "0.33 = triplet swing, 0.5 = dotted feel.",
    )
    humanize: Humanize | None = Field(
        None, description="Adds small, seed-controlled timing/velocity variation for a less robotic feel."
    )
    hits: list[Annotated[float, Field(ge=0)]] | None = Field(
        None,
        max_length=64,
        description="Explicit chord hit positions in beats, e.g. [0, 0.75, 1.5, 2.25] for off-beat "
        "stabs, chugs or gallops. Overrides pattern/subdivision/swing; gate, velocity and "
        "humanize still apply.",
    )
