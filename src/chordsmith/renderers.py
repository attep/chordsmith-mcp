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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, Protocol

import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from chordsmith import audio, results
from chordsmith.models import ChorusSettings, ReverbSettings, VocalChain
from chordsmith.sections import SECTION_MARKER_PREFIX, read_sections
from chordsmith.soundfont import SoundFontError, available_labels, find_preset, read_presets
from chordsmith.storage import FileStore

SPECS_SUFFIX = ".instruments.json"
FFMPEG_TIMEOUT_SECONDS = 300
STEM_WORKERS = max(2, min(6, os.cpu_count() or 4))
MAX_SFZ_LIBRARIES = 200
MAX_VST_PLUGINS = 500

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
    gain_db: float | None = Field(
        None,
        ge=-24.0,
        le=12.0,
        description="Level for this track during this section, replacing its gain_db; applied "
        "exactly to the rendered stem (and baked as CC11, which cannot boost above 0 dB).",
    )


class EffectPlugin(BaseModel):
    """One VST3 effect in a track's insert chain (applied to the rendered stem)."""

    plugin: str = Field(description="Path to a .vst3 effect plugin (see list_plugins).")
    preset: str | None = Field(
        None, description="Program name or index inside the plugin; omit for its default state."
    )
    state: str | None = Field(
        None,
        description="Path to a captured plugin state file (see open_plugin_state_editor), "
        "applied before params — how an effect with nothing settable (TONEX) loads its tone.",
    )
    params: dict[str, float | str] | None = Field(
        None,
        description="Parameter values applied after the preset and state, keyed by parameter name "
        "(or index) — a number (e.g. {'Mix': 0.4}), or a value name for a choice parameter (e.g. "
        "{'Mode': 'Bright'}, see list_plugins parameter_values).",
    )


class InstrumentSpec(BaseModel):
    """How one track should be rendered, e.g. a different soundfont for the bass."""

    engine: Literal["fluidsynth", "sfizz", "vst"] = Field(
        "fluidsynth",
        description="Which renderer to use: 'fluidsynth' (General MIDI via a .sf2 soundfont), "
        "'sfizz' (a sampled .sfz instrument) or 'vst' (a .vst3 plugin). sfizz and vst need "
        "render_audio with stems=true.",
    )
    preset: str | None = Field(
        None,
        description="fluidsynth: path to a .sf2 soundfont for this track; omit to use the "
        "server's default soundfont.",
    )
    sfz: str | None = Field(
        None,
        description="sfizz: path to a .sfz instrument file on this server (with its samples); "
        "required when engine='sfizz'.",
    )
    plugin: str | None = Field(
        None,
        description="vst: path to a .vst3 instrument plugin on this server (see list_plugins); "
        "required when engine='vst'.",
    )
    plugin_preset: str | None = Field(
        None,
        description="vst: program name or index inside the plugin (see list_plugins detail); "
        "omit for the plugin's default state.",
    )
    plugin_state: str | None = Field(
        None,
        description="vst: path to a captured plugin state file (see open_plugin_state_editor) — "
        "how GUI-only plugins keep their sound (a TONEX tone, a Guitar Rig rig, a "
        "SampleTank/Syntronik patch). Applied before plugin_preset/plugin_params, so those can "
        "still tweak the captured sound.",
    )
    plugin_params: dict[str, float | str] | None = Field(
        None,
        description="vst: parameter values applied after the preset and state, keyed by parameter "
        "name (or index) — a number (e.g. {'Gain': 0.5}) or a value name for a choice parameter "
        "(e.g. {'Osc 1 Type': 'Classic'}); list names with list_plugins parameter_search and "
        "values with parameter_values. This is how plugins without VST3 programs (IK's "
        "SampleTank, Syntronik) pick a patch: find the patch parameter, list its values and set "
        "one by name.",
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
    reverb_send: int | None = Field(
        None,
        ge=0,
        le=127,
        description="CC91 reverb send for this track (0-127; 0 = fully dry). Omit to keep the "
        "file's own send (GM default 40) — FluidSynth's engine reverb only reaches a track as "
        "far as its send.",
    )
    chorus_send: int | None = Field(
        None,
        ge=0,
        le=127,
        description="CC93 chorus send for this track (0-127; 0 = fully dry). Omit to keep the "
        "file's own send (GM default 0).",
    )
    effects: list[EffectPlugin] | None = Field(
        None,
        description="VST3 effects applied to this track's rendered stem, in order, e.g. "
        "[{'plugin': '/vst/Comp.vst3'}, {'plugin': '/vst/Tape.vst3', 'preset': 'Warm'}]; "
        "works for any engine and needs the VST host.",
    )
    chain: VocalChain | None = Field(
        None,
        description="Built-in chain for this track's rendered stem — the same stages as the "
        "vocal chain (EQ, de-esser, saturation, compressor) plus an optional convolution "
        "'reverb' (no plugin needed), applied before VST effects. How instrument grit, "
        "filtering, glue and space work without a plugin host (e.g. saturate a copied sub "
        "layer for grit).",
    )
    gain_db: float = Field(
        0.0,
        ge=-24.0,
        le=12.0,
        description="Trim for this stem when the stems are combined into the mix.",
    )
    mute: bool = Field(
        False,
        description="Muted: left out of stem renders and vocal mixes, and baked as CC7=0 so "
        "external players hear it silent too.",
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


def _has_audio(path: Path) -> bool:
    """Whether a wav holds any signal at all (silent engines fail loudly).

    The scan is dense (every second sample) and short-circuits on the first nonzero value: a
    deliberately quiet guide track (velocity 1) still counts as audio, while a broken engine —
    which produces pure zeros — is caught. The old sparse scan sampled every 16th sample and
    mistook such guides for silence.
    """
    import wave

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    import array

    samples = array.array("h")
    samples.frombytes(frames)
    return any(samples[::2])


class FluidSynthRenderer:
    """The built-in engine: one FluidSynth pass per track, rendered as its own process."""

    name = "fluidsynth"
    notes = "Runs as a separate process; renders General MIDI with a .sf2 soundfont."

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


class SfizzRenderer:
    """Sample-based SFZ instruments through the sfizz_render CLI, as its own process.

    Engine reverb/chorus do not apply (they are FluidSynth settings); use the mix tools'
    returns/master for shared effects.
    """

    name = "sfizz"
    notes = "Runs as a separate process; renders a sampled .sfz instrument at 44.1 kHz."

    def available(self) -> bool:
        return audio.find_sfizz_render() is not None

    def render(
        self,
        midi: Path,
        spec: InstrumentSpec,
        out: Path,
        effects: dict[str, float | int] | None = None,
    ) -> None:
        if spec.sfz is None or not Path(spec.sfz).is_file():
            raise RenderError(f"SFZ instrument '{spec.sfz}' does not exist.")
        try:
            audio.render_sfizz(spec.sfz, midi, out)
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        if audio.wav_duration(out) <= 0.01:
            raise RenderError("The engine produced no audio for this track.")
        if not _has_audio(out):
            raise RenderError("The engine produced a silent stem; check the track's notes and instrument.")


class VstRenderer:
    """Offline VST3 rendering through the separate vst_host process (GPL; see host/).

    Instruments play a MIDI file; a spec's ``effects`` chain is applied to the rendered stem by
    :func:`apply_stem_effects`, whichever engine produced it.
    """

    name = "vst"
    notes = "Runs as a separate process; renders a .vst3 plugin (instruments) at 44.1 kHz."

    def available(self) -> bool:
        return audio.find_vst_host() is not None

    def render(
        self,
        midi: Path,
        spec: InstrumentSpec,
        out: Path,
        effects: dict[str, float | int] | None = None,
    ) -> None:
        host = audio.host_for_plugin(spec.plugin) if spec.plugin else None
        if host is None:
            raise RenderError(
                "The VST host is not installed; set CHORDSMITH_VST_HOST or put vst_host on PATH."
            )
        if spec.plugin is None or not _plugin_exists(spec.plugin):
            raise RenderError(f"VST plugin '{spec.plugin}' does not exist.")
        try:
            audio.render_vst(
                host,
                spec.plugin,
                spec.plugin_preset,
                _resolve_sets(spec.plugin, spec.plugin_params),
                midi,
                out,
                state=spec.plugin_state,
            )
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        duration = audio.midi_duration(midi)
        audio.trim_wav(out, duration)
        audio.pad_wav(out, duration)
        if audio.wav_duration(out) <= 0.01:
            raise RenderError("The engine produced no audio for this track.")
        if not _has_audio(out):
            raise RenderError("The engine produced a silent stem; check the track's notes and instrument.")


def apply_stem_effects(stem: Path, effects: list[EffectPlugin] | None) -> None:
    """Run a track's VST3 effect chain over its rendered stem (the host compensates latency).

    A chain may mix this server's plugins with the bridge's; consecutive effects that share a
    host run in one pass, and the stem moves between passes.
    """
    if not effects:
        return
    groups: list[tuple[str, list[tuple[str, str | None, list[tuple[int, float | str]], str | None]]]] = []
    for effect in effects:
        if not _plugin_exists(effect.plugin):
            raise RenderError(f"VST plugin '{effect.plugin}' does not exist.")
        host = audio.host_for_plugin(effect.plugin)
        if host is None:
            raise RenderError("This track has VST effects, but the VST host is not installed.")
        entry = (effect.plugin, effect.preset, _resolve_sets(effect.plugin, effect.params), effect.state)
        if groups and groups[-1][0] == host:
            groups[-1][1].append(entry)
        else:
            groups.append((host, [entry]))
    for host, chain in groups:
        temp = stem.with_name(stem.stem + ".fx.wav")
        try:
            audio.process_vst(host, chain, stem, temp)
            temp.replace(stem)
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        finally:
            temp.unlink(missing_ok=True)


RENDERERS: dict[str, Renderer] = {
    FluidSynthRenderer.name: FluidSynthRenderer(),
    SfizzRenderer.name: SfizzRenderer(),
    VstRenderer.name: VstRenderer(),
}


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
    tick: int,
    channels: list[int],
    bank: int | None,
    program: int | None,
    reverb_send: int | None = None,
    chorus_send: int | None = None,
) -> list[tuple[int, int, mido.Message]]:
    """Channel events at ``tick``: bank select, program change and effect sends, in order."""
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
        if reverb_send is not None:
            events.append(
                (
                    tick,
                    3,
                    mido.Message("control_change", control=91, value=reverb_send, time=0, channel=channel),
                )
            )
        if chorus_send is not None:
            events.append(
                (
                    tick,
                    4,
                    mido.Message("control_change", control=93, value=chorus_send, time=0, channel=channel),
                )
            )
    return events


def _strip_program_events(track: mido.MidiTrack, sends: bool = False) -> None:
    """Remove a track's own program changes and bank selects (and effect sends when replaced)."""
    controls = {0, 32} | ({91, 93} if sends else set())
    track[:] = [
        message
        for message in track
        if message.type != "program_change"
        and not (message.type == "control_change" and message.control in controls)
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
    """Write a copy of ``midi`` with the spec's bank/program timeline and sends applied.

    Returns ``midi`` itself when the spec sets nothing to apply. The copy strips the file's own
    program changes (and bank selects — and effect sends when the spec sets its own) and inserts
    the resolved timeline: programs/banks at time zero or at each section start with
    ``sections`` (bank select before the program change), plus CC91/CC93 sends at time zero when
    the spec sets them. Notes already sounding keep their original preset: FluidSynth assigns
    the preset per voice, so a program change only affects notes that begin after it. The copy
    lives next to the caller's scratch MIDI.
    """
    has_overrides = any(
        override.program is not None or override.bank is not None
        for override in (spec.sections or {}).values()
    )
    has_sends = spec.reverb_send is not None or spec.chorus_send is not None
    if spec.bank is None and spec.program is None and not has_overrides and not has_sends:
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
        _strip_program_events(track, sends=has_sends)
    events = program_timeline(file, spec, channels)
    if has_sends:
        events += _program_events(0, channels, None, None, spec.reverb_send, spec.chorus_send)
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


def _has_section_gains(spec: InstrumentSpec) -> bool:
    return any(override.gain_db is not None for override in (spec.sections or {}).values())


def effective_instruments(
    midi: mido.MidiFile, specs: dict[str, InstrumentSpec], soundfont: str | None = None
) -> dict[str, dict]:
    """What each note track will actually play, resolved through the spec, the file and defaults.

    Resolution order per track: a spec's ``program``/``bank`` first, then the file's own events
    on that channel (the first CC0 and the first ``program_change``, scanned across **all**
    tracks — override_program writes into the conductor), then the default (program 0; bank 128
    on channel 10). ``source`` names where the program came from; the bank resolves independently
    through the same order. Labels are looked up in the spec's preset or the server SoundFont —
    a missing preset returns a null label with the reason in ``warning``, never an error.
    Channels are 1-based (10 = drums), matching add_track and the import report. This says what
    is *meant* to play, not proof of how the audio sounds.
    """
    programs: dict[int, list[tuple[int, int]]] = {}
    banks: dict[int, list[tuple[int, int]]] = {}
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "program_change":
                programs.setdefault(message.channel, []).append((tick, message.program))
            elif message.type == "control_change" and message.control == 0:
                banks.setdefault(message.channel, []).append((tick, message.value))
    ticks_per_beat = midi.ticks_per_beat
    names = track_names(midi)
    channels = track_channels(midi)
    note_names = [names[index] for index in note_tracks(midi)]
    shared: dict[int, list[str]] = {}
    for name in note_names:
        channel = channels.get(name)
        if channel is not None:
            shared.setdefault(channel, []).append(name)
    result: dict[str, dict] = {}
    for name in note_names:
        channel = channels.get(name)
        channel0 = channel if channel is not None else 0
        spec = specs.get(name)
        warnings: list[str] = []
        if spec is not None and spec.program is not None:
            program, source = spec.program, "spec"
        elif channel is not None and programs.get(channel0):
            program, source = programs[channel0][0][1], "file"
        else:
            program, source = 0, "default"
        if spec is not None and spec.bank is not None:
            bank = spec.bank
        elif channel is not None and banks.get(channel0):
            bank = banks[channel0][0][1]
        else:
            bank = 128 if channel0 == 9 else 0
        if channel is not None:
            if len(shared.get(channel0, [])) > 1:
                others = ", ".join(entry for entry in shared[channel0] if entry != name)
                warnings.append(f"shares channel {channel0 + 1} with {others}")
            channel_programs = programs.get(channel0, [])
            if len(channel_programs) > 1:
                beat = channel_programs[1][0] / ticks_per_beat
                warnings.append(
                    f"the channel changes program again at beat {beat:g}; reporting the starting value"
                )
        if spec is not None and spec.engine == "sfizz":
            if spec.sfz is None or not Path(spec.sfz).is_file():
                warnings.append(f"SFZ instrument '{spec.sfz}' does not exist")
            result[name] = {
                "channel": channel0 + 1,
                "bank": None,
                "program": None,
                "label": Path(spec.sfz).name if spec.sfz else None,
                "source": "sfz",
                "soundfont": None,
                "warning": "; ".join(warnings) if warnings else None,
            }
            continue
        if spec is not None and spec.engine == "vst":
            if spec.plugin is None or not _plugin_exists(spec.plugin):
                warnings.append(f"VST plugin '{spec.plugin}' does not exist")
            result[name] = {
                "channel": channel0 + 1,
                "bank": None,
                "program": None,
                "label": _vst_label(spec),
                "source": "vst",
                "soundfont": None,
                "warning": "; ".join(warnings) if warnings else None,
            }
            continue
        target = spec.preset if spec is not None and spec.preset is not None else None
        if target is None:
            target = soundfont or audio.find_soundfont()
        label: str | None = None
        if target is None:
            warnings.append("no SoundFont is readable on this server")
        else:
            try:
                presets = read_presets(target)
            except SoundFontError as exc:
                warnings.append(str(exc))
                presets = []
            if presets:
                preset = find_preset(presets, bank, program)
                if preset is None:
                    warnings.append(f"preset {bank:03d}-{program:03d} is not in '{Path(target).name}'")
                else:
                    label = preset.label()
        result[name] = {
            "channel": channel0 + 1,
            "bank": bank,
            "program": program,
            "label": label,
            "source": source,
            "soundfont": str(target) if target is not None else None,
            "warning": "; ".join(warnings) if warnings else None,
        }
    return result


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


def _validate_sfizz(source: Path, spec: InstrumentSpec) -> None:
    """Check a sfizz spec: a readable .sfz, no FluidSynth-only options, section names exist."""
    if spec.sfz is None:
        raise RenderError("engine='sfizz' needs 'sfz': the path to a .sfz instrument file.")
    if not Path(spec.sfz).is_file():
        raise RenderError(f"SFZ instrument '{spec.sfz}' does not exist.")
    for option, value in (
        ("preset", spec.preset),
        ("bank", spec.bank),
        ("program", spec.program),
        ("plugin", spec.plugin),
        ("plugin_preset", spec.plugin_preset),
        ("plugin_state", spec.plugin_state),
    ):
        if value is not None:
            raise RenderError(f"'{option}' is a fluidsynth option; an SFZ instrument maps its own notes.")
    if spec.reverb_send is not None or spec.chorus_send is not None:
        raise RenderError(
            "'reverb_send'/'chorus_send' drive FluidSynth's engine effects; sfizz has none — "
            "use the chain's 'reverb' stage or a VST reverb effect."
        )
    if spec.sections:
        resolved = read_sections(source)
        if not resolved:
            raise RenderError(
                "The file has no section map; call set_song_sections before adding section overrides."
            )
        available = [section.name for section in resolved]
        unknown = sorted(set(spec.sections) - set(available))
        if unknown:
            raise RenderError(f"Unknown section(s) {', '.join(unknown)}. Sections: {', '.join(available)}.")
        for section_name, override in spec.sections.items():
            if override.program is not None or override.bank is not None or override.preset is not None:
                raise RenderError(
                    f"Section '{section_name}' changes program/bank/preset; an SFZ instrument maps "
                    "its own notes (a gain_db override is fine)."
                )


def _vst_label(spec: InstrumentSpec) -> str | None:
    """The label a vst spec resolves to: the plugin's file name plus its preset when set."""
    if spec.plugin is None:
        return None
    label = _basename(spec.plugin)
    return f"{label} - {spec.plugin_preset}" if spec.plugin_preset else label


def _plugin_exists(path: str) -> bool:
    """Whether a plugin path exists here (a local .vst3) or on the bridge (a Windows path)."""
    if Path(path).exists():
        return True
    if audio.bridge_url() and audio.is_windows_path(path):
        try:
            return bool(audio.bridge_paths_exist([path]).get(path))
        except audio.AudioError:
            return False
    return False


def _basename(path: str) -> str:
    """The file name of a path that may be a Windows path on a Linux server."""
    return PureWindowsPath(path).name if len(path) > 1 and path[1] == ":" else Path(path).name


def _resolve_plugin(plugin: str) -> str | None:
    """A plugin path or a scanned name -> its path; None when neither matches."""
    if _plugin_exists(plugin):
        return plugin
    scanned = _scan_vst_plugins(os.environ.get("CHORDSMITH_VST_DIR"))
    match = next((entry for entry in scanned if entry["name"] == plugin), None)
    return match["path"] if match else None


def _scan_vst_plugins(directory: str | None) -> list[dict[str, str]]:
    """The .vst3 plugins under a directory; with a bridge, its machine's plugins too."""
    local = _scan_vst_plugins_local(directory)
    if not audio.bridge_url():
        return local
    remote = audio.bridge_plugin_scan()
    combined = local + [entry for entry in remote if entry["path"] not in {e["path"] for e in local}]
    return combined[:MAX_VST_PLUGINS]


def _scan_vst_plugins_local(directory: str | None) -> list[dict[str, str]]:
    """The .vst3 plugins under this server's own folder (Windows bundles listed once)."""
    if not directory or not Path(directory).is_dir():
        return []
    kept: list[Path] = []
    for path in sorted(Path(directory).rglob("*.vst3")):
        # A Windows VST3 bundle is a directory holding a same-named .vst3 file inside it
        # (Contents\x86_64-win\X.vst3): keep the bundle, drop the file inside it.
        if any(entry in path.parents for entry in kept):
            continue
        kept.append(path)
        if len(kept) >= MAX_VST_PLUGINS:
            break
    return [{"name": path.stem, "path": str(path)} for path in kept]


_PARAMETER_CACHE: dict[tuple[str, str], dict[str, int]] = {}


def _parameter_indices(host: str, plugin: str) -> dict[str, int]:
    """Parameter name -> index for a plugin (cached; the host loads the plugin once)."""
    key = (host, plugin)
    names = _PARAMETER_CACHE.get(key)
    if names is None:
        info = audio.vst_plugin_params(host, plugin)
        names = {str(entry.get("name", "")): int(entry["index"]) for entry in info.get("parameters") or []}
        _PARAMETER_CACHE[key] = names
    return names


def _parameter_value(value: float | str) -> float | str:
    """A parameter value as the host takes it: a float, or a choice name the host resolves."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RenderError("Parameter values cannot be an empty string.")
        if ";" in text:
            raise RenderError(f"Parameter value '{value}' cannot contain ';'.")
        return text
    return float(value)


def _resolve_sets(plugin: str, params: dict[str, float | str] | None) -> list[tuple[int, float | str]]:
    """Turn a spec's parameter map into the (index, value) pairs the host takes.

    A string value is a choice name the host resolves against the plugin (VST3 plugins often
    expose their choice parameters as continuous values, so names are the sane way in).
    """
    if not params:
        return []
    host = audio.host_for_plugin(plugin)
    if host is None:
        raise RenderError(
            "VST parameters need the VST host; set CHORDSMITH_VST_HOST or put vst_host on PATH."
        )
    names = _parameter_indices(host, plugin)
    sets: list[tuple[int, float | str]] = []
    for key, value in params.items():
        if key.isdigit():
            sets.append((int(key), _parameter_value(value)))
            continue
        index = names.get(key)
        if index is None:
            close = next((name for name in names if key.lower() in name.lower()), None)
            hint = f" Did you mean '{close}'?" if close else ""
            raise RenderError(
                f"'{_basename(plugin)}' has no parameter named '{key}'.{hint} "
                "(list them with list_plugins parameter_search.)"
            )
        sets.append((index, _parameter_value(value)))
    return sets


def _validate_vst(source: Path, spec: InstrumentSpec) -> None:
    """Check a vst spec: a readable .vst3 instrument that loads, and no other engine's options."""
    if spec.plugin is None:
        raise RenderError("engine='vst' needs 'plugin': the path to a .vst3 plugin.")
    if not _plugin_exists(spec.plugin):
        raise RenderError(f"VST plugin '{spec.plugin}' does not exist.")
    for option, value in (
        ("preset", spec.preset),
        ("sfz", spec.sfz),
        ("bank", spec.bank),
        ("program", spec.program),
    ):
        if value is not None:
            raise RenderError(
                f"'{option}' is not a vst option; use 'plugin', 'plugin_preset' and 'plugin_state'."
            )
    if spec.reverb_send is not None or spec.chorus_send is not None:
        raise RenderError(
            "'reverb_send'/'chorus_send' drive FluidSynth's engine effects; a vst track has "
            "none — use a VST reverb effect with a dry/wet (e.g. Fame Studio Reverb) or the "
            "chain's 'reverb' stage."
        )
    if spec.sections:
        resolved = read_sections(source)
        if not resolved:
            raise RenderError(
                "The file has no section map; call set_song_sections before adding section overrides."
            )
        available = [section.name for section in resolved]
        unknown = sorted(set(spec.sections) - set(available))
        if unknown:
            raise RenderError(f"Unknown section(s) {', '.join(unknown)}. Sections: {', '.join(available)}.")
        for section_name, override in spec.sections.items():
            if override.program is not None or override.bank is not None or override.preset is not None:
                raise RenderError(
                    f"Section '{section_name}' changes program/bank/preset; a vst track only takes "
                    "a gain_db override (the gain is applied to the rendered stem)."
                )
    host = audio.host_for_plugin(spec.plugin)
    if host is None:
        raise RenderError("The VST host is not installed; set CHORDSMITH_VST_HOST or put vst_host on PATH.")
    try:
        info = audio.vst_plugin_info(host, spec.plugin)
    except audio.AudioError as exc:
        raise RenderError(str(exc)) from None
    types = info.get("types") or []
    if not types:
        raise RenderError(f"No VST3 types found in '{spec.plugin}'.")
    if not types[0].get("is_instrument"):
        raise RenderError(
            f"'{_basename(spec.plugin)}' is an effect; engine='vst' takes an instrument "
            "(effects go in 'effects')."
        )
    if spec.plugin_preset is not None:
        programs = [str(name) for name in types[0].get("programs") or []]
        if not programs:
            raise RenderError(
                "This plugin exposes no programs; drop 'plugin_preset' (its default state is used)."
            )
        if spec.plugin_preset not in programs and not spec.plugin_preset.isdigit():
            listed = ", ".join(programs[:12]) + ("..." if len(programs) > 12 else "")
            raise RenderError(
                f"No program '{spec.plugin_preset}' in '{_basename(spec.plugin)}'. Programs: {listed}"
            )
    if spec.plugin_state is not None and not Path(spec.plugin_state).is_file():
        raise RenderError(
            f"Plugin state '{spec.plugin_state}' does not exist; capture it with "
            "open_plugin_state_editor (or the host's --editor / --dump-state)."
        )
    _resolve_sets(spec.plugin, spec.plugin_params)  # parameter names must exist


def _validate_effects(effects: list[EffectPlugin] | None) -> None:
    """Check a track's effect chain: readable plugins that load, effects only, real programs."""
    if not effects:
        return
    for index, effect in enumerate(effects, start=1):
        if not _plugin_exists(effect.plugin):
            raise RenderError(f"Effect {index}: VST plugin '{effect.plugin}' does not exist.")
        host = audio.host_for_plugin(effect.plugin)
        if host is None:
            raise RenderError(
                f"Effect {index}: the VST host is not installed; set CHORDSMITH_VST_HOST or "
                "put vst_host on PATH."
            )
        try:
            info = audio.vst_plugin_info(host, effect.plugin)
        except audio.AudioError as exc:
            raise RenderError(f"Effect {index}: {exc}") from None
        types = info.get("types") or []
        if types and types[0].get("is_instrument"):
            raise RenderError(
                f"Effect {index}: '{_basename(effect.plugin)}' is an instrument; 'effects' takes "
                "effect plugins."
            )
        if effect.state is not None and not Path(effect.state).is_file():
            raise RenderError(
                f"Effect {index}: state file '{effect.state}' does not exist; capture it with "
                "open_plugin_state_editor (or the host's --editor / --dump-state)."
            )
        if effect.preset is not None:
            programs = [str(name) for name in types[0].get("programs") or []] if types else []
            if not programs:
                raise RenderError(f"Effect {index}: this plugin exposes no programs; drop its preset.")
            if effect.preset not in programs and not effect.preset.isdigit():
                listed = ", ".join(programs[:12]) + ("..." if len(programs) > 12 else "")
                raise RenderError(
                    f"Effect {index}: no program '{effect.preset}' in '{_basename(effect.plugin)}'. "
                    f"Programs: {listed}"
                )
        _resolve_sets(effect.plugin, effect.params)  # parameter names must exist


STATE_SUFFIX = ".state"


def _states_dir(store: FileStore) -> Path:
    """The captured plugin states live in 'states/' inside the output folder (the library)."""
    return store.root / "states"


def _state_library(store: FileStore) -> dict[str, dict]:
    """Every captured state with its metadata: {name: {name, path, plugin, ...}}."""
    directory = _states_dir(store)
    library: dict[str, dict] = {}
    if not directory.is_dir():
        return library
    for path in sorted(directory.glob(f"*{STATE_SUFFIX}")):
        name = path.stem
        metadata: dict = {}
        sidecar = directory / f"{name}.json"
        if sidecar.is_file():
            try:
                loaded = json.loads(sidecar.read_text("utf-8"))
                if isinstance(loaded, dict):
                    metadata = loaded
            except (OSError, json.JSONDecodeError):
                metadata = {}
        plugin = str(metadata.get("plugin") or "") or None
        plugin_name = metadata.get("plugin_name")
        if not plugin_name and plugin:
            plugin_name = _basename(plugin).removesuffix(".vst3")
        library[name] = {
            "name": name,
            "path": str(path),
            "plugin": plugin,
            "plugin_name": plugin_name,
            "saved_at": metadata.get("saved_at"),
            "default": bool(metadata.get("default")),
            "size_bytes": path.stat().st_size,
        }
    return library


def _plugin_key(plugin: str) -> str:
    return _basename(plugin).removesuffix(".vst3").casefold()


def _default_state(store: FileStore, plugin: str) -> str | None:
    """The plugin's registered default state path, or None."""
    key = _plugin_key(plugin)
    for entry in _state_library(store).values():
        if entry["default"] and entry.get("plugin_name") and str(entry["plugin_name"]).casefold() == key:
            return entry["path"]
    return None


def _resolve_state_reference(store: FileStore, plugin: str, value: str | None) -> str | None:
    """Resolve plugin_state/state: a path, a library name, 'none', or None (-> the default)."""
    if value is None:
        return _default_state(store, plugin)
    text = str(value).strip()
    if text == "" or text.casefold() == "none":
        return None
    candidate = Path(text)
    if candidate.is_file():
        return str(candidate)
    library = _state_library(store)
    name = text[: -len(STATE_SUFFIX)] if text.endswith(STATE_SUFFIX) else text
    entry = library.get(name)
    if entry is not None:
        return entry["path"]
    available = ", ".join(sorted(library)) or "(none captured yet)"
    raise RenderError(
        f"Plugin state '{value}' is not a file, and no captured state is named '{name}'. "
        f"States: {available}. Capture one with open_plugin_state_editor."
    )


def _write_state_metadata(directory: Path, name: str, plugin: str, default: bool) -> None:
    """Write/refresh a state's metadata sidecar; 'default' also clears other defaults."""
    plugin_name = _basename(plugin).removesuffix(".vst3")
    sidecar = directory / f"{name}.json"
    previous: dict = {}
    if sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text("utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            previous = {}
    if default:
        for other in directory.glob("*.json"):
            if other == sidecar:
                continue
            try:
                data = json.loads(other.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or not data.get("default"):
                continue
            other_name = data.get("plugin_name") or _basename(str(data.get("plugin") or "")).removesuffix(
                ".vst3"
            )
            if str(other_name).casefold() == plugin_name.casefold():
                data["default"] = False
                other.write_text(json.dumps(data, indent=1) + "\n", "utf-8")
    payload = {
        "plugin": plugin,
        "plugin_name": plugin_name,
        "saved_at": previous.get("saved_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "default": bool(default) or bool(previous.get("default")),
    }
    sidecar.write_text(json.dumps(payload, indent=1) + "\n", "utf-8")


def _materialize_states(store: FileStore, spec: InstrumentSpec) -> None:
    """Resolve state references (names, defaults) into concrete paths before saving a spec."""
    if spec.engine == "vst" and spec.plugin is not None:
        spec.plugin_state = _resolve_state_reference(store, spec.plugin, spec.plugin_state)
    for effect in spec.effects or []:
        effect.state = _resolve_state_reference(store, effect.plugin, effect.state)


def section_gain_windows(
    spec: InstrumentSpec, timeline: list[tuple[str, float, float]]
) -> list[tuple[float, float, float]]:
    """``(start_seconds, end_seconds, gain_db)`` for each section the spec gives a gain to."""
    if not spec.sections:
        return []
    windows: list[tuple[float, float, float]] = []
    for name, start, end in timeline:
        override = spec.sections.get(name)
        if override is not None and override.gain_db is not None:
            windows.append((start, end, override.gain_db))
    return windows


SECTION_RAMP_SECONDS = 0.015  # linear ramp at every section-gain boundary: no click from a step


def apply_section_gains(stem: Path, windows: list[tuple[float, float, float]], baseline_db: float) -> bool:
    """Apply per-section gain deltas to a rendered stem with an ffmpeg volume envelope.

    Only the *difference* from the track's baseline is applied, so the stem keeps the baseline
    for the mix and the combined result is exact. Every boundary ramps over
    ``SECTION_RAMP_SECONDS`` instead of stepping, so section changes never click. Returns True
    when a pass ran.
    """
    deltas = [
        (start, end, gain - baseline_db) for start, end, gain in windows if abs(gain - baseline_db) > 0.05
    ]
    if not deltas:
        return False
    ffmpeg = audio.find_ffmpeg()
    if ffmpeg is None:
        raise RenderError("ffmpeg is not installed (it is in the Docker image); needed for section gains.")
    # The envelope in dB: a list of (time, change) boundaries, each changing the level over the
    # next ramp window; summing the ramp terms gives the piecewise-linear envelope.
    boundaries: list[tuple[float, float]] = []
    current = 0.0
    for start, end, delta in sorted(deltas):
        if abs(delta - current) > 1e-9:
            boundaries.append((start, delta - current))
            current = delta
        boundaries.append((end, -current))
        current = 0.0
    merged: list[list[float]] = []
    for time, change in boundaries:
        if merged and abs(merged[-1][0] - time) < 1e-6:
            merged[-1][1] += change
        else:
            merged.append([time, change])
    terms = [
        f"{change:.4f}*min(max((t-{time:.3f})/{SECTION_RAMP_SECONDS:g},0),1)"
        for time, change in merged
        if abs(change) > 1e-9
    ]
    if not terms:
        return False
    # volume's eval=frame evaluates once per input frame; feeding 16-sample frames (0.36 ms)
    # before it makes the ramps smooth instead of stepping at the default ~23 ms granularity.
    # p=0 keeps the last partial frame as-is (no silent padding at the end).
    gain_filter = f"asetnsamples=n=16:p=0,volume=volume='pow(10,(({'+'.join(terms)}))/20)':eval=frame"
    temp = stem.with_name(stem.stem + ".sec.wav")
    try:
        _ffmpeg(
            [
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(stem),
                "-af",
                gain_filter,
                str(temp),
            ]
        )
        temp.replace(stem)
    finally:
        temp.unlink(missing_ok=True)
    return True


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
        the combined mix, ``sections`` overrides that change the program/bank at a section's
        start (see set_song_sections; held notes keep their original sound), and CC91/CC93
        effect sends (``reverb_send``/``chorus_send``) that decide how much of FluidSynth's
        engine reverb/chorus each track gets. Track names must exist in the file, a preset file
        must be readable, section names must exist in the file's section map, and every
        requested bank/program must exist in the SoundFont it targets — a miss is an error that
        lists what the bank has, so nothing falls back silently. Returns the full spec map, the
        resolved preset label per spec, and an ``effective`` block: what each track actually
        plays, labelled with a source and warnings.
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
            _materialize_states(store, spec)  # names/defaults become concrete state paths
            if spec.engine == "sfizz":
                _validate_sfizz(source, spec)
            elif spec.engine == "vst":
                _validate_vst(source, spec)
            else:
                if (
                    spec.sfz is not None
                    or spec.plugin is not None
                    or spec.plugin_preset is not None
                    or spec.plugin_state is not None
                ):
                    raise RenderError(
                        "'sfz', 'plugin', 'plugin_preset' and 'plugin_state' are not fluidsynth options."
                    )
                if spec.preset is not None and not Path(spec.preset).is_file():
                    raise RenderError(f"Soundfont '{spec.preset}' does not exist.")
                preset_label(spec, channels.get(name, 0))  # validate bank/program against the sf2
                _validate_sections(source, spec, channels.get(name, 0))
            _validate_effects(spec.effects)
            specs[name] = spec
        if specs:
            save_specs(source, specs)
        else:
            specs_path(source).unlink(missing_ok=True)
        resolved = {}
        for name, spec in specs.items():
            if spec.engine == "sfizz":
                resolved[name] = Path(spec.sfz).name if spec.sfz else None
                continue
            if spec.engine == "vst":
                resolved[name] = _vst_label(spec)
                continue
            try:
                resolved[name] = preset_label(spec, channels.get(name, 0))
            except RenderError:
                resolved[name] = None
        return {
            "filename": source.name,
            "path": str(source),
            "tracks": {name: spec.model_dump() for name, spec in specs.items()},
            "resolved": resolved,
            "effective": effective_instruments(midi, specs),
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
        returns each track's stored spec, the tracks that can carry one, the preset label each
        spec resolves to, and an ``effective`` block: what each track actually plays, labelled
        with a source and warnings (1-based channels; intent, not proof of the sound).
        """
        store: FileStore = store_provider()
        engines = [
            {
                "name": renderer.name,
                "available": renderer.available(),
                "default_soundfont": audio.find_soundfont() if renderer.name == "fluidsynth" else None,
                "notes": renderer.notes,
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
        sfz_dir = os.environ.get("CHORDSMITH_SFZ_DIR")
        libraries = None
        if sfz_dir and Path(sfz_dir).is_dir():
            libraries = [
                {"name": path.stem, "path": str(path)}
                for path in sorted(Path(sfz_dir).rglob("*.sfz"))[:MAX_SFZ_LIBRARIES]
            ]
        vst_dir = os.environ.get("CHORDSMITH_VST_DIR")
        vst_plugins = None
        if vst_dir and Path(vst_dir).is_dir():
            vst_plugins = [
                {"name": path.stem, "path": str(path)}
                for path in sorted(Path(vst_dir).rglob("*.vst3"))[:MAX_VST_PLUGINS]
            ]
        result: dict = {
            "engines": engines,
            "soundfont": inventory,
            "programs": None if inventory else {"melodic": list(GM_PROGRAMS), "drums": dict(GM_DRUM_KITS)},
            "sfz_libraries": libraries,
            "vst_plugins": vst_plugins,
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
                if spec.engine == "sfizz":
                    resolved[name] = Path(spec.sfz).name if spec.sfz else None
                    continue
                if spec.engine == "vst":
                    resolved[name] = _vst_label(spec)
                    continue
                try:
                    resolved[name] = preset_label(spec, channels.get(name, 0))
                except RenderError:
                    resolved[name] = None
            result["file"] = {
                "filename": source.name,
                "tracks": [names[index] for index in note_tracks(midi)],
                "specs": {name: spec.model_dump() for name, spec in specs.items()},
                "resolved": resolved,
                "effective": effective_instruments(midi, specs, soundfont),
            }
        return result

    @mcp.tool(title="List VST plugins", annotations=_READ_ONLY)
    def list_plugins(
        detail: Annotated[
            str | None,
            Field(
                description="Optional plugin path (or a name from the scan) to describe in full: "
                "vendor, category, programs, parameters, latency. Loads the plugin in the VST host."
            ),
        ] = None,
        name_contains: Annotated[
            str | None,
            Field(
                description="Only list plugins whose file name contains this text "
                "(case-insensitive), e.g. 'modo' or 'surge'."
            ),
        ] = None,
        parameters: Annotated[
            bool,
            Field(
                description="With detail: also return the plugin's parameters (index, name, steps, "
                "current text; up to 500) — the keys plugin_params takes."
            ),
        ] = False,
        parameter_search: Annotated[
            str | None,
            Field(
                description="With detail: only parameters whose name contains this text "
                "(case-insensitive), e.g. 'patch' or 'gain'."
            ),
        ] = None,
        parameter_values: Annotated[
            int | None,
            Field(
                description="With detail: dump the value strings of this parameter index (up to "
                "200) — for discrete selectors like patch lists; the value names are what "
                "plugin_params takes, or the plain values where no names exist."
            ),
        ] = None,
    ) -> results.PluginsResult:
        """List the .vst3 plugins found in CHORDSMITH_VST_DIR (Docker: /vst); read-only.

        Each entry has the plugin's file name and path — the values set_track_instrument takes as
        'plugin'. With ``detail`` the VST host loads one plugin and returns its types: vendor,
        category, whether it is an instrument, its programs (the values 'plugin_preset' takes),
        parameter count and latency. Plugins are user-supplied and never bundled; the host is a
        separate process, so a crashing plugin cannot take the server down.
        """
        local_dir = os.environ.get("CHORDSMITH_VST_DIR")
        scanned = _scan_vst_plugins(local_dir)
        plugins = scanned
        directory = local_dir
        if audio.bridge_url():
            remote_dir = os.environ.get("CHORDSMITH_VST_BRIDGE_VST_DIR", "")
            directory = " + ".join(part for part in (local_dir, remote_dir) if part) or None
        if name_contains:
            needle = name_contains.lower()
            plugins = [entry for entry in scanned if needle in entry["name"].lower()]
        host = audio.find_vst_host()
        payload = None
        parameter_list = None
        value_list = None
        detail_states = None
        detail_default = None
        if detail is not None:
            resolved = _resolve_plugin(detail)
            if resolved is None:
                raise RenderError(f"Plugin '{detail}' is not a path or a scanned name.")
            target = resolved
            plugin_host = audio.host_for_plugin(target)
            if plugin_host is None:
                raise RenderError(
                    "The VST host is not installed; set CHORDSMITH_VST_HOST or put vst_host on PATH."
                )
            payload = audio.vst_plugin_info(plugin_host, target)
            key = _plugin_key(target)
            store: FileStore = store_provider()
            detail_states = [
                {
                    "name": entry["name"],
                    "path": entry["path"],
                    "plugin": entry["plugin"],
                    "plugin_name": entry["plugin_name"],
                    "saved_at": entry["saved_at"],
                    "default": entry["default"],
                    "size_bytes": entry["size_bytes"],
                }
                for entry in sorted(_state_library(store).values(), key=lambda item: item["name"])
                if entry.get("plugin_name") and str(entry["plugin_name"]).casefold() == key
            ]
            detail_default = next((entry["name"] for entry in detail_states if entry["default"]), None)
            if parameters or parameter_search is not None:
                info = audio.vst_plugin_params(plugin_host, target)
                entries = info.get("parameters") or []
                if parameter_search:
                    needle = parameter_search.lower()
                    entries = [entry for entry in entries if needle in str(entry.get("name", "")).lower()]
                parameter_list = entries[:500]
            if parameter_values is not None:
                value_list = audio.vst_param_values(plugin_host, target, parameter_values)
        return {
            "host": host,
            "directory": directory,
            "plugins": plugins,
            "detail": payload,
            "parameters": parameter_list,
            "parameter_values": value_list,
            "states": detail_states,
            "default_state": detail_default,
        }

    @mcp.tool(title="Capture a plugin's state", annotations=_CREATES)
    def open_plugin_state_editor(
        plugin: Annotated[
            str,
            Field(description="Plugin path or a name from list_plugins, e.g. 'Guitar Rig 7' or 'TONEX'."),
        ],
        state_name: Annotated[
            str | None,
            Field(
                description="Name for the saved state file (default: the plugin's name); '.state' "
                "is added, stored under 'states/' in the output folder."
            ),
        ] = None,
        make_default: Annotated[
            bool,
            Field(
                description="Make this capture the plugin's default state: specs that give no "
                "plugin_state/state use it automatically."
            ),
        ] = False,
    ) -> results.PluginEditorResult:
        """Open a plugin's own UI on the server's desktop to capture its state once.

        Returns immediately with the path the state will be written to: a window opens on the
        server machine, the tone/rig/patch is loaded in it, and closing the window saves the
        plugin state there. Reference that path from set_track_instrument's 'plugin_state' (a vst
        instrument) or an effect's 'state' — every render then loads the captured sound headless,
        which is the only way in for GUI-only plugins (TONEX tones, Guitar Rig rigs, SampleTank/
        Syntronik patches). Needs a desktop session: native/desktop servers show the window
        directly, and with a VST bridge configured it opens on the bridge's desktop. The same
        capture from a shell is: vst_host --editor --plugin X --save-state out (with --state to
        load an existing capture, and --dump-state for a GUI-free snapshot). With
        ``make_default`` the capture becomes the plugin's default: specs that name no state use
        it; later default changes go through set_plugin_default_state.
        """
        resolved = _resolve_plugin(plugin)
        if resolved is None:
            raise RenderError(f"Plugin '{plugin}' is not a path or a scanned name.")
        target = resolved
        host = audio.host_for_plugin(target)
        if host is None:
            raise RenderError(
                "The VST host is not installed; set CHORDSMITH_VST_HOST or put vst_host on PATH."
            )
        if (
            not host.startswith("http")
            and sys.platform not in ("win32", "darwin")
            and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")
        ):
            raise RenderError(
                "The plugin editor needs a desktop session and this server has none; run the "
                "server natively on a desktop machine, or configure the VST bridge "
                "(scripts/vst_bridge.py) and the window opens on the bridge's desktop instead."
            )
        try:
            audio.vst_plugin_info(host, target)  # fail early when the plugin cannot load at all
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        store: FileStore = store_provider()
        states_dir = _states_dir(store)
        states_dir.mkdir(parents=True, exist_ok=True)
        filename = store.safe_name(
            (state_name or Path(_basename(target)).stem).removesuffix(".state"), ".state"
        )
        state_path = states_dir / filename
        _write_state_metadata(states_dir, state_path.stem, target, default=make_default)
        try:
            audio.start_vst_editor(host, target, state_path)
        except audio.AudioError as exc:
            raise RenderError(str(exc)) from None
        note = (
            "A plugin window opened on the server's desktop. Load the tone/rig/patch in it, then "
            "close the window: the state is written to state_path. Specs reference it as "
            "'plugin_state' (a vst instrument) or 'state' (an effect) — by that path or by name."
        )
        if make_default:
            note += " It is now the plugin's default state: specs that name no state use it."
        return {
            "plugin": Path(target).name,
            "state_path": str(state_path),
            "started": True,
            "make_default": make_default,
            "note": note,
        }

    @mcp.tool(title="List plugin states", annotations=_READ_ONLY)
    def list_plugin_states(
        plugin: Annotated[
            str | None,
            Field(
                description="Only states captured for this plugin (a path or a name from "
                "list_plugins), e.g. 'TONEX'."
            ),
        ] = None,
    ) -> results.PluginStatesResult:
        """List the captured plugin states (the state library) and each plugin's default; read-only.

        States are captured with open_plugin_state_editor and stored in 'states/' in the output
        folder. A spec's plugin_state/state takes one by name (or path), and a plugin's default
        is used whenever a spec names no state at all. Returns each state's name, path, plugin,
        size and saved time, plus a plugin-name -> default-state map.
        """
        store: FileStore = store_provider()
        library = _state_library(store)
        entries = [
            {
                "name": entry["name"],
                "path": entry["path"],
                "plugin": entry["plugin"],
                "plugin_name": entry["plugin_name"],
                "saved_at": entry["saved_at"],
                "default": entry["default"],
                "size_bytes": entry["size_bytes"],
            }
            for entry in sorted(library.values(), key=lambda item: item["name"])
        ]
        defaults = {
            str(entry["plugin_name"]): entry["name"]
            for entry in library.values()
            if entry["default"] and entry.get("plugin_name")
        }
        if plugin is not None:
            key = _plugin_key(plugin)
            entries = [entry for entry in entries if str(entry.get("plugin_name") or "").casefold() == key]
        return {"directory": str(_states_dir(store)), "states": entries, "defaults": defaults}

    @mcp.tool(title="Set a plugin's default state", annotations=_CREATES)
    def set_plugin_default_state(
        plugin: Annotated[str, Field(description="Plugin path or a name from list_plugins, e.g. 'TONEX'.")],
        state: Annotated[
            str | None,
            Field(
                description="State name or path to make this plugin's default; null (or 'none') "
                "clears the default."
            ),
        ] = None,
    ) -> results.PluginDefaultResult:
        """Mark a captured state as the plugin's default (or clear it).

        The default is used whenever a spec names this plugin but gives no plugin_state/state
        ('none' in a spec opts out). The state must already exist in the library. Returns the
        plugin and the state now used by default (null when cleared).
        """
        store: FileStore = store_provider()
        target = _resolve_plugin(plugin) or plugin
        directory = _states_dir(store)
        clearing = state is None or str(state).strip().casefold() == "none"
        if clearing:
            for other in directory.glob("*.json"):
                try:
                    data = json.loads(other.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or not data.get("default"):
                    continue
                other_name = data.get("plugin_name") or _basename(str(data.get("plugin") or "")).removesuffix(
                    ".vst3"
                )
                if str(other_name).casefold() == _plugin_key(target):
                    data["default"] = False
                    other.write_text(json.dumps(data, indent=1) + "\n", "utf-8")
            return {"plugin": _basename(target), "default_state": None}
        resolved = _resolve_state_reference(store, target, str(state))
        if resolved is None:
            return {"plugin": _basename(target), "default_state": None}
        _write_state_metadata(directory, Path(resolved).stem, target, default=True)
        return {"plugin": _basename(target), "default_state": Path(resolved).stem}

    @mcp.tool(title="Bake instruments into MIDI", annotations=_CREATES)
    def bake_instruments(
        filename: Annotated[str, Field(description="MIDI file in the output folder.")],
        output_filename: Annotated[
            str | None, Field(description="Name for the baked copy (default: <name>_instruments).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing copy.")] = False,
    ) -> results.BakeResult:
        """Write a copy of the file with the instrument specs' bank/program events baked in.

        The original is never modified; output_filename must differ from the source. Every track
        with a set_track_instrument spec gets its
        resolved bank/program timeline as real MIDI events in the copy — including per-section
        overrides at their exact boundaries and the track's CC91/CC93 effect sends — so other
        players and DAWs hear the intended instruments without ChordSmith. A MIDI file cannot
        carry a SoundFont, so the result also returns the required .sf2 per track as companion
        metadata. Tracks that share a MIDI channel share one program timeline (a standard MIDI
        limitation; a warning says so).
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
            has_sends = spec.reverb_send is not None or spec.chorus_send is not None
            if (
                spec.bank is None
                and spec.program is None
                and not has_overrides
                and not has_sends
                and not spec.mute
                and not _has_section_gains(spec)
            ):
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
            _strip_program_events(midi.tracks[index], sends=has_sends)
            events = program_timeline(midi, spec, track_channels_here)
            if has_sends:
                events += _program_events(
                    0, track_channels_here, None, None, spec.reverb_send, spec.chorus_send
                )
            if spec.mute:
                # a muted track is silenced for external players too (CC7 = channel volume)
                events += [
                    (0, 5, mido.Message("control_change", control=7, value=0, time=0, channel=entry))
                    for entry in track_channels_here
                ]
            if spec.sections:
                # per-section gains bake as CC11 expression at the section starts; the encoding
                # cannot boost above 0 dB (CC11 127), which the docs call out
                for section_tick, section_name in section_markers(midi):
                    override = spec.sections.get(section_name)
                    if override is None or override.gain_db is None:
                        continue
                    value = max(0, min(127, round(127 * 10 ** (override.gain_db / 20.0))))
                    events += [
                        (
                            section_tick,
                            6,
                            mido.Message("control_change", control=11, value=value, time=0, channel=entry),
                        )
                        for entry in track_channels_here
                    ]
            _rewrite_track(midi.tracks[index], events)
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
