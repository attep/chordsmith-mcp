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


class Tuplet(BaseModel):
    """An exact tuplet position for one note: it lands on ``slot`` of ``count`` even divisions of
    ``in_beats`` beats, counted from ``start_beat`` (the group's start).

    Positions are computed in MIDI ticks, so they are exact — e.g. a 3:2 triplet is
    {"count": 3, "in_beats": 2, "start_beat": 138, "slot": 0..2}, and 2 against 3 works the same
    way with count 2 and in_beats 3.
    """

    count: int = Field(ge=2, le=16, description="How many slots the group divides into.")
    in_beats: float = Field(gt=0, le=64, description="The span the group fits into, in beats.")
    start_beat: float = Field(ge=0, le=100000, description="Where the group starts, in beats.")
    slot: int = Field(ge=0, le=15, description="Which slot this note takes (0-based).")

    @model_validator(mode="after")
    def _slot_within_count(self) -> Tuplet:
        if self.slot >= self.count:
            raise ValueError(f"slot {self.slot} is outside a {self.count}-slot tuplet")
        return self


class Automation(BaseModel):
    """A linear controller ramp for add_track, e.g. {"cc": 11, "from_value": 40, "to_value":
    110, "start_beat": 0, "end_beat": 12}.

    One value is written every sixteenth note (plus the exact end), so a held note can swell
    (CC11 expression) or a track can fade (CC7 channel volume).
    """

    cc: int = Field(
        ge=0, le=127, description="Controller number: 11 = expression (swell), 7 = channel volume."
    )
    from_value: int = Field(ge=0, le=127, description="Controller value at start_beat.")
    to_value: int = Field(ge=0, le=127, description="Controller value at end_beat.")
    start_beat: float = Field(ge=0, le=100000, description="Ramp start in beats.")
    end_beat: float = Field(gt=0, le=100000, description="Ramp end in beats (after the start).")

    @model_validator(mode="after")
    def _end_after_start(self) -> Automation:
        if self.end_beat <= self.start_beat:
            raise ValueError("end_beat must be after start_beat")
        return self


class NoteInput(BaseModel):
    """One note for add_track, e.g. {"pitch": "E6", "start_beat": 0, "beats": 0.5}.

    Position it one of three ways: ``start_beat`` (absolute beats from the start of the song),
    ``section`` + ``bar`` + ``beat`` (relative to a section of the song's section map — see
    set_song_sections), or ``tuplet`` (an exact slot in a tuplet group). A note may run past its
    section's end; it is still placed normally.
    """

    pitch: str | int = Field(description="Note name with octave ('E6', 'Bb3', 'C#-1') or MIDI number 0-127.")
    start_beat: float | None = Field(
        None,
        ge=0,
        le=100000,
        description="Absolute start in beats from the start; give this, section+bar+beat, or a tuplet.",
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
    tuplet: Tuplet | None = Field(
        None, description="Exact tuplet slot, e.g. {'count': 3, 'in_beats': 2, 'start_beat': 0, 'slot': 1}."
    )

    @model_validator(mode="after")
    def _exactly_one_position(self) -> NoteInput:
        modes = [self.start_beat is not None, self.section is not None, self.tuplet is not None]
        if sum(modes) != 1:
            raise ValueError("give exactly one position: start_beat, section+bar+beat, or a tuplet")
        if self.section is not None:
            if self.bar is None:
                raise ValueError("section-relative notes need a bar")
        elif self.bar is not None or self.beat is not None:
            raise ValueError("bar/beat need a section")
        return self


class VocalSegment(BaseModel):
    """One vocal piece for mix_song_with_vocals, e.g. {"file": "verse.wav", "start_seconds": 12.5}.

    Use a list of segments to mix several takes or to join a vocal that was rendered in pieces;
    each piece is placed at its start time and the pieces are summed into one vocal track. Each
    voice can also carry its own level, pan and processing chain (EQ, compressor, sends).
    """

    file: str = Field(description="Vocal file in the output folder, or a finished singing job id.")
    start_seconds: float = Field(
        0.0, ge=0.0, le=100000.0, description="Where this piece starts in the song, in seconds."
    )
    name: str | None = Field(
        None, max_length=40, description="Voice name for diagnostics, e.g. 'High' or 'Low'."
    )
    gain_db: float = Field(0.0, ge=-24.0, le=12.0, description="This voice's level in the duet.")
    pan: float = Field(0.0, ge=-1.0, le=1.0, description="Pan: -1 left, 0 centre, +1 right (equal-power).")
    chain: VocalChain | None = Field(
        None, description="This voice's processing chain; omit for a clean voice."
    )
    sends: VocalSends | None = Field(None, description="Send levels to the shared reverb/delay.")
    reverb_mix: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Insert-style reverb wet amount on this voice; null uses the sends.",
    )


class EqBell(BaseModel):
    """One peaking EQ band, e.g. {"hz": 300, "gain_db": -2, "q": 1.2}."""

    hz: float = Field(ge=20.0, le=20000.0, description="Centre frequency in Hz.")
    gain_db: float = Field(ge=-24.0, le=24.0, description="Boost or cut in dB.")
    q: float = Field(1.0, gt=0.05, le=10.0, description="Bandwidth (Q).")


class EqShelf(BaseModel):
    """A shelf band, e.g. {"hz": 6000, "gain_db": 2}."""

    hz: float = Field(ge=20.0, le=20000.0, description="Shelf corner in Hz.")
    gain_db: float = Field(ge=-24.0, le=24.0, description="Boost or cut in dB.")


class VocalEq(BaseModel):
    """Corrective and tone EQ for one voice; the bands apply in this order."""

    highpass_hz: float | None = Field(
        None, ge=20.0, le=500.0, description="High-pass corner in Hz (e.g. 75 to remove rumble); null skips it."
    )
    bells: list[EqBell] = Field(default_factory=list, max_length=6, description="Peaking bands, up to 6.")
    low_shelf: EqShelf | None = Field(None, description="Low shelf (e.g. cut mud under 300 Hz).")
    high_shelf: EqShelf | None = Field(None, description="High shelf (e.g. add or remove brightness).")


class VocalCompressor(BaseModel):
    """A compressor with real settings; the defaults mirror the legacy fixed chain.

    ``gain_reduction_db`` is reported from a measurement pass, so a threshold means the same
    thing from song to song.
    """

    enabled: bool = False
    threshold_db: float = Field(-24.0, ge=-60.0, le=0.0, description="Threshold in dBFS.")
    ratio: float = Field(3.0, ge=1.0, le=20.0, description="Compression ratio (e.g. 3 = 3:1).")
    attack_ms: float = Field(10.0, ge=0.01, le=2000.0, description="Attack in ms.")
    release_ms: float = Field(250.0, ge=0.01, le=9000.0, description="Release in ms.")
    makeup_db: float = Field(2.0, ge=0.0, le=24.0, description="Makeup gain in dB.")
    knee_db: float = Field(2.83, ge=1.0, le=8.0, description="Soft-knee width in dB.")


class VocalDeesser(BaseModel):
    """De-esser (planned for the next stage: enabling it returns a clear error for now)."""

    enabled: bool = False
    intensity: float = Field(0.5, ge=0.0, le=1.0, description="De-essing intensity, 0-1.")
    frequency: float = Field(0.5, ge=0.0, le=1.0, description="Detection frequency, 0-1.")
    makeup: float = Field(0.0, ge=0.0, le=1.0, description="Makeup gain, 0-1.")


class VocalSaturation(BaseModel):
    """Soft-clip saturation (planned for the next stage: enabling it returns an error for now)."""

    enabled: bool = False
    drive_db: float = Field(6.0, ge=0.0, le=24.0, description="Drive in dB.")


class VocalSends(BaseModel):
    """Send levels to the shared returns, in dB (0 = full send, -12 = quiet)."""

    reverb_db: float | None = Field(None, ge=-60.0, le=12.0, description="Reverb send level; null = no send.")
    delay_db: float | None = Field(None, ge=-60.0, le=12.0, description="Delay send level; null = no send.")


class VocalChain(BaseModel):
    """One voice's processing chain: EQ, then compressor (de-esser/saturation come later)."""

    eq: VocalEq | None = None
    compressor: VocalCompressor | None = None
    deesser: VocalDeesser | None = None
    saturation: VocalSaturation | None = None


class LimiterSettings(BaseModel):
    """A lookahead limiter: it catches peaks, not sustained loudness."""

    enabled: bool = False
    ceiling_db: float = Field(-1.0, ge=-12.0, le=0.0, description="Ceiling in dBFS.")


class VocalBus(BaseModel):
    """The duet bus: one gain (and optional glue compression/limiting) for all voices together."""

    gain_db: float = Field(0.0, ge=-24.0, le=12.0, description="Bus level for the summed voices.")
    compressor: VocalCompressor | None = None
    limiter: LimiterSettings | None = None


class ReverbReturn(BaseModel):
    """The shared reverb: a convolution room with decay, pre-delay and damping."""

    decay_s: float = Field(2.0, ge=0.1, le=8.0, description="Decay length in seconds.")
    predelay_ms: float = Field(20.0, ge=0.0, le=200.0, description="Pre-delay in ms (space before the room).")
    damping: float = Field(0.5, ge=0.0, le=1.0, description="High-frequency damping: 0 bright, 1 dark.")


class DelayReturn(BaseModel):
    """The shared delay: explicit milliseconds, or synced to the song's starting tempo."""

    delay_ms: float | None = Field(None, ge=1.0, le=2000.0, description="Delay time in ms.")
    tempo_sync: Literal["1/4", "1/8", "1/8d", "1/16"] | None = Field(
        None, description="Note value synced to the song's starting tempo."
    )
    feedback: float = Field(0.2, ge=0.0, le=0.9, description="Feedback amount, 0-0.9.")

    @model_validator(mode="after")
    def _one_time(self) -> DelayReturn:
        if (self.delay_ms is None) == (self.tempo_sync is None):
            raise ValueError("give either delay_ms or tempo_sync, not both")
        return self


class Returns(BaseModel):
    """The shared effect returns; voices reach them through their send levels."""

    reverb: ReverbReturn | None = None
    delay: DelayReturn | None = None


class MasterSettings(BaseModel):
    """The master stage: an optional true-peak limiter and an optional loudness target."""

    limiter: LimiterSettings | None = None
    loudness_target_lufs: float | None = Field(
        None, ge=-30.0, le=-6.0, description="Target integrated loudness; applied as one static gain."
    )


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


class PatternRow(BaseModel):
    """One voice of a step pattern for add_track, e.g. {"pitch": 36, "steps": "x---x---"}.

    Each character is one step (``step_beats`` long, a sixteenth by default): ``x``/``o`` hits,
    ``X``/``O`` hits harder (accent), ``-``/``.``/``_`` are rests, and ``|`` and spaces are bar
    lines (ignored). Drum grids are written as one row per voice.
    """

    pitch: str | int = Field(description="Note name with octave or MIDI number 0-127.")
    steps: str = Field(
        min_length=1,
        max_length=4096,
        description="Step string, e.g. 'x---x---' (kick on 1 and 3) or 'x-x-|x-x-'.",
    )
    velocity: int = Field(90, ge=1, le=127, description="Velocity of a plain hit; accents add 25.")


class ReverbSettings(BaseModel):
    """FluidSynth's built-in reverb for one render (engine-wide, not per track).

    Defaults and ranges are the installed engine's own (FluidSynth 2.4.4): room size 0-1.2,
    damping 0-1, width 0-100, level 0-1. Effects are off unless the tool gets this object.
    """

    room_size: float = Field(0.5, ge=0.0, le=1.2, description="Room size 0-1.2 (bigger = longer tail).")
    damping: float = Field(0.3, ge=0.0, le=1.0, description="High-frequency damping 0-1.")
    width: float = Field(0.8, ge=0.0, le=100.0, description="Stereo width 0-100.")
    level: float = Field(0.7, ge=0.0, le=1.0, description="Reverb output level 0-1.")


class ChorusSettings(BaseModel):
    """FluidSynth's built-in chorus for one render (engine-wide, not per track).

    Defaults and ranges are the installed engine's own (FluidSynth 2.4.4): 3 delay lines,
    level 0-10, speed 0.1-5 Hz, depth 0-21 ms. This engine build has no waveform setting.
    """

    voices: int = Field(3, ge=0, le=99, description="Delay lines 0-99 (the engine's default is 3).")
    level: float = Field(0.6, ge=0.0, le=10.0, description="Chorus output level 0-10.")
    speed: float = Field(0.2, ge=0.1, le=5.0, description="Modulation speed in Hz, 0.1-5.")
    depth: float = Field(4.25, ge=0.0, le=21.0, description="Modulation depth in ms, 0-21.")


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
