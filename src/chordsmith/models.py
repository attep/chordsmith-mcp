"""Pydantic schemas shared by the MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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
