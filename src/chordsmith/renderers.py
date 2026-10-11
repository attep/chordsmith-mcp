"""Per-track rendering: a Renderer protocol, instrument specs and the FluidSynth engine.

Engines run as **separate processes** (today FluidSynth through its CLI) and are never imported
into this server: future GPL-licensed hosts (pedalboard, sfizz, DrumGizmo) must stay outside the
MIT codebase, and a crashing engine must not take the server down. A track's instrument is a
small JSON sidecar next to the MIDI file (``<name>.instruments.json``), so the MIDI itself is
never modified.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from chordsmith import audio, results
from chordsmith.models import ChorusSettings, ReverbSettings
from chordsmith.sections import SECTION_MARKER_PREFIX, read_sections
from chordsmith.soundfont import SoundFontError, available_labels, find_preset, read_presets
from chordsmith.storage import FileStore

SPECS_SUFFIX = ".instruments.json"
FFMPEG_TIMEOUT_SECONDS = 300
STEM_WORKERS = max(2, min(6, os.cpu_count() or 4))

GM_PROGRAMS = [
    "Acoustic Grand Piano",
    "Bright Acoustic Piano",
    "Electric Grand Piano",
    "Honky-tonk Piano",
    "Electric Piano 1",
    "Electric Piano 2",
    "Harpsichord",
    "Clavinet",
    "Celesta",
    "Glockenspiel",
    "Music Box",
    "Vibraphone",
    "Marimba",
    "Xylophone",
    "Tubular Bells",
    "Dulcimer",
    "Drawbar Organ",
    "Percussive Organ",
    "Rock Organ",
    "Church Organ",
    "Reed Organ",
    "Accordion",
    "Harmonica",
    "Tango Accordion",
    "Acoustic Guitar (nylon)",
    "Acoustic Guitar (steel)",
    "Electric Guitar (jazz)",
    "Electric Guitar (clean)",
    "Electric Guitar (muted)",
    "Overdriven Guitar",
    "Distortion Guitar",
    "Guitar Harmonics",
    "Acoustic Bass",
    "Electric Bass (finger)",
    "Electric Bass (pick)",
    "Fretless Bass",
    "Slap Bass 1",
    "Slap Bass 2",
    "Synth Bass 1",
    "Synth Bass 2",
    "Violin",
    "Viola",
    "Cello",
    "Contrabass",
    "Tremolo Strings",
    "Pizzicato Strings",
    "Orchestral Harp",
    "Timpani",
    "String Ensemble 1",
    "String Ensemble 2",
    "Synth Strings 1",
    "Synth Strings 2",
    "Choir Aahs",
    "Voice Oohs",
    "Synth Voice",
    "Orchestra Hit",
    "Trumpet",
    "Trombone",
    "Tuba",
    "Muted Trumpet",
    "French Horn",
    "Brass Section",
    "Synth Brass 1",
    "Synth Brass 2",
    "Soprano Sax",
    "Alto Sax",
    "Tenor Sax",
    "Baritone Sax",
    "Oboe",
    "English Horn",
    "Bassoon",
    "Clarinet",
    "Piccolo",
    "Flute",
    "Recorder",
    "Pan Flute",
    "Blown Bottle",
    "Shakuhachi",
    "Whistle",
    "Ocarina",
    "Lead 1 (square)",
    "Lead 2 (sawtooth)",
    "Lead 3 (calliope)",
    "Lead 4 (chiff)",
    "Lead 5 (charang)",
    "Lead 6 (voice)",
    "Lead 7 (fifths)",
    "Lead 8 (bass + lead)",
    "Pad 1 (new age)",
    "Pad 2 (warm)",
    "Pad 3 (polysynth)",
    "Pad 4 (choir)",
    "Pad 5 (bowed)",
    "Pad 6 (metallic)",
    "Pad 7 (halo)",
    "Pad 8 (sweep)",
    "FX 1 (rain)",
    "FX 2 (soundtrack)",
    "FX 3 (crystal)",
    "FX 4 (atmosphere)",
    "FX 5 (brightness)",
    "FX 6 (goblins)",
    "FX 7 (echoes)",
    "FX 8 (sci-fi)",
    "Sitar",
    "Banjo",
    "Shamisen",
    "Koto",
    "Kalimba",
    "Bagpipe",
    "Fiddle",
    "Shanai",
    "Tinkle Bell",
    "Agogo",
    "Steel Drums",
    "Woodblock",
    "Taiko Drum",
    "Melodic Tom",
    "Synth Drum",
    "Reverse Cymbal",
    "Guitar Fret Noise",
    "Breath Noise",
    "Seashore",
    "Bird Tweet",
    "Telephone Ring",
    "Helicopter",
    "Applause",
    "Gunshot",
]

GM_DRUM_KITS = {
    "Standard": 0,
    "Room": 8,
    "Power": 16,
    "Electronic": 24,
    "TR-808": 25,
    "Jazz": 32,
    "Brush": 40,
    "Orchestra": 48,
    "SFX": 56,
}

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_CREATES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


class RenderError(ValueError):
    """Raised when a track cannot be rendered (missing engine, silent stem, ...)."""


class SectionInstrument(BaseModel):
    """Per-section instrument override for one track, e.g. a lead that switches to sawtooth.

    Only the fields given are overridden; the track's own spec fills the rest. The change lands
    exactly at the section's start (bank select before the program change), and notes already
    sounding keep their original preset — FluidSynth assigns the preset per voice.
    """

    preset: str | None = Field(
        None,
        description="Must be the same .sf2 as the track's preset: per-section SoundFont "
        "switching is not supported yet.",
    )
    bank: int | None = Field(None, ge=0, le=127, description="CC0 bank select for this section.")
    program: int | None = Field(None, ge=0, le=127, description="MIDI program (0-127) for this section.")


class InstrumentSpec(BaseModel):
    """How one track should be rendered, e.g. a different soundfont for the bass."""

    engine: Literal["fluidsynth"] = Field(
        "fluidsynth", description="Which renderer to use. Only 'fluidsynth' exists today."
    )
    preset: str | None = Field(
        None,
        description="fluidsynth: path to a .sf2 soundfont for this track; omit to use the "
        "server's default soundfont.",
    )
    bank: int | None = Field(
        None,
        ge=0,
        le=127,
        description="fluidsynth: CC0 bank select (0-127); 0 is the GM melodic bank. Omit to "
        "keep the file's own bank.",
    )
    program: int | None = Field(
        None,
        ge=0,
        le=127,
        description="fluidsynth: MIDI program (0-127) — a GM instrument number minus 1 (see "
        "list_instruments for the names); on a drum track it picks the kit. Omit to keep the "
        "file's own program.",
    )
    sections: dict[str, SectionInstrument] | None = Field(
        None,
        description="Per-section overrides keyed by section name (from set_song_sections), e.g. "
        "{'Chorus': {'program': 81}}; a change lands exactly at the section start and held "
        "notes keep their original sound. Sections not listed use the track's own program/bank.",
    )
    gain_db: float = Field(
        0.0,
        ge=-24.0,
        le=12.0,
        description="Trim for this stem when the stems are combined into the mix.",
    )


@dataclass
class Stem:
    """One rendered track, ready to be combined and delivered."""

    track: str
    path: Path
    gain_db: float


class Renderer(Protocol):
    name: str

    def available(self) -> bool: ...

    def render(
        self,
        midi: Path,
        spec: InstrumentSpec,
        out: Path,
        effects: dict[str, float | int] | None = None,
    ) -> None: ...


def effect_options(
    reverb: ReverbSettings | None, chorus: ChorusSettings | None
) -> dict[str, float | int] | None:
    """FluidSynth setting map for the given effects; None when both are off (a dry render).

    Effects are engine-wide for one render instance, not per track: with ``stems`` every track
    is rendered with them, and summing wet stems is not identical to a single wet pass.
    """
    if reverb is None and chorus is None:
        return None
    options: dict[str, float | int] = {"synth.reverb.active": 0, "synth.chorus.active": 0}
    if reverb is not None:
        options.update(
            {
                "synth.reverb.active": 1,
                "synth.reverb.room-size": reverb.room_size,
                "synth.reverb.damp": reverb.damping,
                "synth.reverb.width": reverb.width,
                "synth.reverb.level": reverb.level,
            }
        )
    if chorus is not None:
        options.update(
            {
                "synth.chorus.active": 1,
                "synth.chorus.nr": chorus.voices,
                "synth.chorus.level": chorus.level,
                "synth.chorus.speed": chorus.speed,
                "synth.chorus.depth": chorus.depth,
            }
        )
    return options


def _has_audio(path: Path, threshold: int = 2) -> bool:
    """Whether a wav holds any signal above the noise floor (silent engines fail loudly)."""
    import wave

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return any(
        abs(int.from_bytes(frames[i : i + 2], "little", signed=True)) >= threshold
        for i in range(0, len(frames) - 1, 32)
    )


class FluidSynthRenderer:
    """The built-in engine: one FluidSynth pass per track, rendered as its own process."""

    name = "fluidsynth"

    def available(self) -> bool:
        return audio.find_fluidsynth() is not None and audio.find_soundfont() is not None

    def render(
        self,
        midi: Path,
        spec: InstrumentSpec,
        out: Path,
        effects: dict[str, float | int] | None = None,
    ) -> None:
        if spec.preset is not None and not Path(spec.preset).is_file():
            raise RenderError(f"Soundfont '{spec.preset}' does not exist.")
        source = override_program(midi, spec)
        try:
            audio.render(source, out, "wav", spec.preset, effects=effects)
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        if audio.wav_duration(out) <= 0.01:
            raise RenderError("The engine produced no audio for this track.")
        if not _has_audio(out):
            raise RenderError("The engine produced a silent stem; check the track's notes and instrument.")


RENDERERS: dict[str, Renderer] = {FluidSynthRenderer.name: FluidSynthRenderer()}


def get_renderer(name: str) -> Renderer:
    renderer = RENDERERS.get(name)
    if renderer is None:
        available = ", ".join(sorted(RENDERERS))
        raise RenderError(f"Unknown engine '{name}'. Available engines: {available}.")
    if not renderer.available():
        raise RenderError(
            f"Engine '{name}' is not available on this server (missing executable or soundfont)."
        )
    return renderer


# ------------------------------------------------------------------ instrument specs


def specs_path(midi_path: Path) -> Path:
    """The sidecar file that holds a MIDI file's per-track instrument specs."""
    return midi_path.with_name(midi_path.stem + SPECS_SUFFIX)


def load_specs(midi_path: Path) -> dict[str, InstrumentSpec]:
    sidecar = specs_path(midi_path)
    if not sidecar.is_file():
        return {}
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    return {name: InstrumentSpec.model_validate(spec) for name, spec in data.get("tracks", {}).items()}


def save_specs(midi_path: Path, specs: dict[str, InstrumentSpec]) -> Path:
    sidecar = specs_path(midi_path)
    payload = {"tracks": {name: spec.model_dump(exclude_none=True) for name, spec in specs.items()}}
    sidecar.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return sidecar


def section_markers(midi: mido.MidiFile) -> list[tuple[int, str]]:
    """Section starts ``(tick, name)`` from the conductor's ``section:`` markers, in order."""
    markers: list[tuple[int, str]] = []
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "marker" and message.text.startswith(SECTION_MARKER_PREFIX):
                markers.append((tick, message.text[len(SECTION_MARKER_PREFIX) :].strip()))
    markers.sort()
    return markers


def _program_events(
    tick: int, channels: list[int], bank: int | None, program: int | None
) -> list[tuple[int, int, mido.Message]]:
    """Bank-select + program-change events at ``tick`` (bank before program), one per channel."""
    events: list[tuple[int, int, mido.Message]] = []
    for channel in channels:
        if bank is not None:
            events.append(
                (tick, 0, mido.Message("control_change", control=0, value=bank, time=0, channel=channel))
            )
            events.append(
                (tick, 1, mido.Message("control_change", control=32, value=0, time=0, channel=channel))
            )
        if program is not None:
            events.append((tick, 2, mido.Message("program_change", program=program, time=0, channel=channel)))
    return events


def _strip_program_events(track: mido.MidiTrack) -> None:
    """Remove a track's own program changes and bank selects (the spec's timeline replaces them)."""
    track[:] = [
        message
        for message in track
        if message.type != "program_change"
        and not (message.type == "control_change" and message.control in (0, 32))
    ]


def program_timeline(
    file: mido.MidiFile, spec: InstrumentSpec, channels: list[int]
) -> list[tuple[int, int, mido.Message]]:
    """The spec's resolved bank/program events at their absolute ticks, for the given channels.

    Without ``spec.sections`` that is one set at time zero. With section overrides the events
    land at each section's start: the section's override merged over the track's own
    program/bank. Sections with nothing to apply emit nothing (the channel keeps its program).
    Raises :class:`RenderError` when the file has no section map or a spec names a section the
    file does not have.
    """
    if not spec.sections:
        return _program_events(0, channels, spec.bank, spec.program)
    markers = section_markers(file)
    if not markers:
        raise RenderError(
            "This track's spec has section overrides, but the file has no section map; call "
            "set_song_sections or remove the overrides."
        )
    available = [name for _, name in markers]
    unknown = sorted(set(spec.sections) - set(available))
    if unknown:
        raise RenderError(
            f"Section(s) {', '.join(unknown)} in the instrument spec are not in this file. "
            f"Sections: {', '.join(available)}."
        )
    events: list[tuple[int, int, mido.Message]] = []
    for tick, name in markers:
        override = spec.sections.get(name)
        bank = override.bank if override is not None and override.bank is not None else spec.bank
        program = override.program if override is not None and override.program is not None else spec.program
        events += _program_events(tick, channels, bank, program)
    return events


def _rewrite_track(track: mido.MidiTrack, events: list[tuple[int, int, mido.Message]]) -> None:
    """Merge the given absolute-tick events into a track and rewrite its delta times."""
    existing: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []
    tick = 0
    for message in track:
        tick += message.time
        if message.type == "end_of_track":
            continue
        existing.append((tick, 3, message))
    original_end = tick
    merged = sorted(existing + list(events), key=lambda entry: (entry[0], entry[1]))
    new_track = mido.MidiTrack()
    last = 0
    for event_tick, _, message in merged:
        new_track.append(message.copy(time=max(0, event_tick - last)))
        last = event_tick
    end = max(original_end, last)
    new_track.append(mido.MetaMessage("end_of_track", time=max(0, end - last)))
    track[:] = new_track


def override_program(midi: Path, spec: InstrumentSpec) -> Path:
    """Write a copy of ``midi`` with the spec's bank/program timeline applied to every note channel.

    Returns ``midi`` itself when the spec sets nothing to apply. The copy strips the file's own
    program changes (and bank selects) and inserts the resolved timeline — one set at time zero,
    or one at each section start with ``sections`` (bank select before the program change).
    Notes already sounding keep their original preset: FluidSynth assigns the preset per voice,
    so a program change only affects notes that begin after it. The copy lives next to the
    caller's scratch MIDI.
    """
    has_overrides = any(
        override.program is not None or override.bank is not None
        for override in (spec.sections or {}).values()
    )
    if spec.bank is None and spec.program is None and not has_overrides:
        return midi
    file = mido.MidiFile(midi)
    channels = sorted(
        {
            message.channel
            for track in file.tracks
            for message in track
            if message.type in ("note_on", "note_off")
        }
    )
    for track in file.tracks:
        _strip_program_events(track)
    events = program_timeline(file, spec, channels)
    _rewrite_track(file.tracks[0], events)
    target = midi.with_name(midi.stem + ".inst.mid")
    file.save(target)
    return target


def note_tracks(midi: mido.MidiFile) -> list[int]:
    """Indices of the tracks that actually hold notes (the conductor is left out)."""
    return [
        index
        for index, track in enumerate(midi.tracks)
        if any(message.type == "note_on" and message.velocity > 0 for message in track)
    ]


def parallel_map(worker, items: list) -> list:
    """Run ``worker`` over ``items`` in a bounded thread pool; results stay in input order.

    Each stem render is an independent engine process, so rendering several tracks at once
    divides the wall time; the pool is bounded because every engine loads its own soundfont.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=STEM_WORKERS) as pool:
        return list(pool.map(worker, items))


def track_names(midi: mido.MidiFile) -> list[str]:
    names = []
    for track in midi.tracks:
        name = next((message.name for message in track if message.type == "track_name"), "")
        names.append(name or "(unnamed)")
    return names


def track_channels(midi: mido.MidiFile) -> dict[str, int]:
    """Track name -> the MIDI channel its notes play on (channel 9 is percussion)."""
    channels: dict[str, int] = {}
    for track in midi.tracks:
        name = next((message.name for message in track if message.type == "track_name"), "")
        if not name:
            continue
        channel = next((message.channel for message in track if message.type == "note_on"), None)
        if channel is not None:
            channels[name] = channel
    return channels


def preset_label(spec: InstrumentSpec, channel: int) -> str | None:
    """The preset a spec will actually play, checked against the SoundFont it targets.

    Returns the preset's ``bank-program name`` label, or None when there is nothing to resolve
    (no program/bank, or no SoundFont file on this server). Raises :class:`RenderError` when the
    requested bank/program is missing from the SoundFont, so a silent fallback is impossible.
    """
    if spec.bank is None and spec.program is None and spec.preset is None:
        return None
    path = spec.preset or audio.find_soundfont()
    if path is None:
        return None  # engine availability already gates rendering; nothing to verify against
    if spec.preset is not None and not Path(path).is_file():
        raise RenderError(f"Soundfont '{path}' does not exist.")
    try:
        presets = read_presets(path)
    except SoundFontError as exc:
        if spec.preset is not None:
            raise RenderError(str(exc)) from None
        return None
    if spec.bank is None and spec.program is None:
        return None
    bank = spec.bank if spec.bank is not None else (128 if channel == 9 else 0)
    if spec.program is None:
        if not any(preset.bank == bank for preset in presets):
            banks = ", ".join(str(value) for value in sorted({preset.bank for preset in presets}))
            raise RenderError(f"Bank {bank} is not in '{Path(path).name}'. Banks: {banks}.")
        return None
    preset = find_preset(presets, bank, spec.program)
    if preset is None:
        in_bank = [entry for entry in presets if entry.bank == bank]
        raise RenderError(
            f"Preset {bank:03d}-{spec.program:03d} is not in '{Path(path).name}'. "
            f"Bank {bank}: {available_labels(in_bank) or '(none)'}"
        )
    return preset.label()


def _validate_sections(source: Path, spec: InstrumentSpec, channel: int) -> None:
    """Check a spec's section overrides against the file's section map and the target SoundFont."""
    if not spec.sections:
        return
    resolved = read_sections(source)
    if not resolved:
        raise RenderError(
            "The file has no section map; call set_song_sections before adding section overrides."
        )
    available = [section.name for section in resolved]
    unknown = sorted(set(spec.sections) - set(available))
    if unknown:
        raise RenderError(f"Unknown section(s) {', '.join(unknown)}. Sections: {', '.join(available)}.")
    target = spec.preset or audio.find_soundfont()
    for override in spec.sections.values():
        if override.preset is not None and (
            target is None or Path(override.preset).resolve() != Path(target).resolve()
        ):
            raise RenderError(
                "Per-section SoundFont switching is not supported yet: a section preset must be "
                "the same .sf2 as the track's preset."
            )
        merged = InstrumentSpec(
            preset=spec.preset,
            bank=override.bank if override.bank is not None else spec.bank,
            program=override.program if override.program is not None else spec.program,
        )
        preset_label(merged, channel)


# ------------------------------------------------------------------ stem combination


def _ffmpeg(args: list[str]) -> None:
    ffmpeg = audio.find_ffmpeg()
    if ffmpeg is None:
        raise RenderError("ffmpeg is not installed (it is in the Docker image); needed for stems.")
    try:
        result = subprocess.run(
            [ffmpeg, *args], capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"ffmpeg could not run: {exc}") from exc
    if result.returncode != 0:
        raise RenderError(f"ffmpeg failed: {(result.stderr or result.stdout).strip()[-300:]}")


def combine_stems(stems: list[Stem], target: Path) -> None:
    """Sum the rendered stems into one wav, applying each stem's trim."""
    args = ["-y"]
    for stem in stems:
        args += ["-i", str(stem.path)]
    filters = ";".join(
        f"[{index}:a]volume={10.0 ** (stem.gain_db / 20.0):.4f}[s{index}]" for index, stem in enumerate(stems)
    )
    inputs = "".join(f"[s{index}]" for index in range(len(stems)))
    filters += f";{inputs}amix=inputs={len(stems)}:duration=longest:normalize=0[m]"
    _ffmpeg([*args, "-filter_complex", filters, "-map", "[m]", "-ar", "44100", str(target)])


# ------------------------------------------------------------------ MCP tools


def register(mcp: FastMCP, store_provider) -> None:
    """Register the instrument tools on the ChordSmith server."""

    @mcp.tool(title="Set track instrument", annotations=_CREATES)
    def set_track_instrument(
        filename: Annotated[str, Field(description="MIDI file in the output folder.")],
        tracks: Annotated[
            dict[str, InstrumentSpec | None],
            Field(
                description="Per-track instrument specs keyed by track name, e.g. "
                "{'Bass': {'engine': 'fluidsynth', 'program': 38, 'gain_db': -3}} or "
                "{'Drums': {'program': 16}} for a drum kit. Entries merge into the existing map; "
                "a null value removes a track's spec. The specs live in a sidecar next to the "
                "MIDI and are used by render_audio with stems=true."
            ),
        ],
    ) -> results.InstrumentMapResult:
        """Store per-track instrument specs for a MIDI file (they are used when rendering stems).

        Writes a small JSON sidecar next to the file; the MIDI itself is never modified. Each
        track can get its own soundfont (``preset``), a different GM instrument (``program``
        0-127, or a drum kit on a drum track; ``bank`` selects the bank), a ``gain_db`` trim for
        the combined mix, and ``sections`` overrides that change the program/bank at a section's
        start (see set_song_sections; held notes keep their original sound). Track names must
        exist in the file, a preset file must be readable, section names must exist in the file's
        section map, and every requested bank/program must exist in the SoundFont it targets — a
        miss is an error that lists what the bank has, so nothing falls back silently. Returns
        the full spec map and the resolved preset label per track.
        """
        store: FileStore = store_provider()
        source = store.existing_path(filename)
        midi = mido.MidiFile(source)
        names = track_names(midi)
        known = {names[index] for index in note_tracks(midi)}
        channels = track_channels(midi)
        specs = load_specs(source)
        for name, spec in tracks.items():
            if name not in known:
                available = ", ".join(sorted(known)) or "(none)"
                raise RenderError(f"Unknown track '{name}'. Tracks: {available}.")
            if spec is None:
                specs.pop(name, None)
                continue
            get_renderer(spec.engine)  # validate engine availability now, not at render time
            if spec.preset is not None and not Path(spec.preset).is_file():
                raise RenderError(f"Soundfont '{spec.preset}' does not exist.")
            preset_label(spec, channels.get(name, 0))  # validate bank/program against the sf2
            _validate_sections(source, spec, channels.get(name, 0))
            specs[name] = spec
        if specs:
            save_specs(source, specs)
        else:
            specs_path(source).unlink(missing_ok=True)
        resolved = {}
        for name, spec in specs.items():
            try:
                resolved[name] = preset_label(spec, channels.get(name, 0))
            except RenderError:
                resolved[name] = None
        return {
            "filename": source.name,
            "path": str(source),
            "tracks": {name: spec.model_dump() for name, spec in specs.items()},
            "resolved": resolved,
        }

    @mcp.tool(title="List instruments", annotations=_READ_ONLY)
    def list_instruments(
        filename: Annotated[
            str | None, Field(description="Optional MIDI file whose per-track specs to report.")
        ] = None,
        soundfont: Annotated[
            str | None,
            Field(description="Optional .sf2 path to inspect; default: the server's soundfont."),
        ] = None,
    ) -> results.InstrumentsResult:
        """List the rendering engines, the SoundFont's real preset inventory, and a file's specs.

        Read-only. Engines are invoked as separate processes (so a crashing or GPL-licensed
        host can never take the server down); today only FluidSynth is built in. The ``soundfont``
        block is the actual preset table of the loaded (or given) .sf2 — name, bank and program,
        both 0-based — which is what set_track_instrument validates against; when no soundfont
        file is readable, a static GM reference list is returned instead. With a filename, also
        returns each track's stored spec, the tracks that can carry one, and the preset label
        each spec resolves to.
        """
        store: FileStore = store_provider()
        engines = [
            {
                "name": renderer.name,
                "available": renderer.available(),
                "default_soundfont": audio.find_soundfont(),
                "notes": "Runs as a separate process; renders General MIDI with a .sf2 soundfont.",
            }
            for renderer in sorted(RENDERERS.values(), key=lambda item: item.name)
        ]
        inventory_path = soundfont or audio.find_soundfont()
        inventory = None
        if inventory_path is not None:
            try:
                presets = read_presets(inventory_path)
            except SoundFontError as exc:
                if soundfont is not None:
                    raise RenderError(str(exc)) from None
                presets = []
            if presets:
                inventory = {
                    "path": str(inventory_path),
                    "presets": [
                        {"name": preset.name, "bank": preset.bank, "program": preset.program}
                        for preset in presets
                    ],
                }
        result: dict = {
            "engines": engines,
            "soundfont": inventory,
            "programs": None if inventory else {"melodic": list(GM_PROGRAMS), "drums": dict(GM_DRUM_KITS)},
            "file": None,
        }
        if filename is not None:
            source = store.existing_path(filename)
            midi = mido.MidiFile(source)
            names = track_names(midi)
            channels = track_channels(midi)
            specs = load_specs(source)
            resolved: dict[str, str | None] = {}
            for name, spec in specs.items():
                try:
                    resolved[name] = preset_label(spec, channels.get(name, 0))
                except RenderError:
                    resolved[name] = None
            result["file"] = {
                "filename": source.name,
                "tracks": [names[index] for index in note_tracks(midi)],
                "specs": {name: spec.model_dump() for name, spec in specs.items()},
                "resolved": resolved,
            }
        return result

    @mcp.tool(title="Bake instruments into MIDI", annotations=_CREATES)
    def bake_instruments(
        filename: Annotated[str, Field(description="MIDI file in the output folder.")],
        output_filename: Annotated[
            str | None, Field(description="Name for the baked copy (default: <name>_instruments).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing copy.")] = False,
    ) -> results.BakeResult:
        """Write a copy of the file with the instrument specs' bank/program events baked in.

        The original is never modified. Every track with a set_track_instrument spec gets its
        resolved bank/program timeline as real MIDI events in the copy — including per-section
        overrides at their exact boundaries — so other players and DAWs hear the intended
        instruments without ChordSmith. A MIDI file cannot carry a SoundFont, so the result also
        returns the required .sf2 per track as companion metadata. Tracks that share a MIDI
        channel share one program timeline (a standard MIDI limitation; a warning says so).
        """
        store: FileStore = store_provider()
        source = store.existing_path(filename)
        specs = load_specs(source)
        if not specs:
            raise RenderError("This file has no instrument specs; call set_track_instrument first.")
        midi = mido.MidiFile(source)
        names = track_names(midi)
        channels = track_channels(midi)
        warnings: list[str] = []
        channel_settings: dict[int, tuple[int | None, int | None]] = {}
        baked: dict[str, InstrumentSpec] = {}
        for index in note_tracks(midi):
            name = names[index]
            spec = specs.get(name)
            if spec is None:
                continue
            has_overrides = any(
                override.program is not None or override.bank is not None
                for override in (spec.sections or {}).values()
            )
            if spec.bank is None and spec.program is None and not has_overrides:
                baked[name] = spec  # only a trim (gain_db): nothing representable in MIDI
                continue
            channel = channels.get(name)
            if channel is not None:
                settings = (spec.bank, spec.program)
                if channel in channel_settings and channel_settings[channel] != settings:
                    warnings.append(
                        f"Tracks share channel {channel + 1}; the MIDI channel keeps one program timeline."
                    )
                channel_settings[channel] = settings
            track_channels_here = [channel] if channel is not None else []
            _strip_program_events(midi.tracks[index])
            _rewrite_track(midi.tracks[index], program_timeline(midi, spec, track_channels_here))
            baked[name] = spec
        with store.claimed_path(output_filename, f"{source.stem}_instruments", overwrite) as target:
            if target == source:
                raise ValueError("Choose a different output_filename; the original file is never modified.")
            midi.save(target)
        return {
            "source": source.name,
            "filename": target.name,
            "path": str(target),
            "uri": f"midi://{target.name}",
            "tracks": {name: spec.model_dump() for name, spec in baked.items()},
            "warnings": warnings,
        }
