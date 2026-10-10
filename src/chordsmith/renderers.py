"""Per-track rendering: a Renderer protocol, instrument specs and the FluidSynth engine.

Engines run as **separate processes** (today FluidSynth through its CLI) and are never imported
into this server: future GPL-licensed hosts (pedalboard, sfizz, DrumGizmo) must stay outside the
MIT codebase, and a crashing engine must not take the server down. A track's instrument is a
small JSON sidecar next to the MIDI file (``<name>.instruments.json``), so the MIDI itself is
never modified.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from chordsmith import audio, results
from chordsmith.storage import FileStore

SPECS_SUFFIX = ".instruments.json"
FFMPEG_TIMEOUT_SECONDS = 300

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

    def render(self, midi: Path, spec: InstrumentSpec, out: Path) -> None: ...


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

    def render(self, midi: Path, spec: InstrumentSpec, out: Path) -> None:
        if spec.preset is not None and not Path(spec.preset).is_file():
            raise RenderError(f"Soundfont '{spec.preset}' does not exist.")
        source = override_program(midi, spec)
        try:
            audio.render(source, out, "wav", spec.preset)
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


def override_program(midi: Path, spec: InstrumentSpec) -> Path:
    """Write a copy of ``midi`` with the spec's bank/program applied to every note channel.

    Returns ``midi`` itself when the spec sets neither. The copy strips the file's own program
    changes (and bank selects) and inserts the spec's at time zero, so the track plays the chosen
    GM instrument (or drum kit) from the first note. The sidecar MIDI of a render lives in the
    caller's scratch directory, so the copy is temporary too.
    """
    if spec.bank is None and spec.program is None:
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
        track[:] = [
            message
            for message in track
            if message.type != "program_change"
            and not (message.type == "control_change" and message.control in (0, 32))
        ]
    head: list[mido.Message] = []
    for channel in channels:
        if spec.bank is not None:
            head.append(mido.Message("control_change", control=0, value=spec.bank, time=0, channel=channel))
            head.append(mido.Message("control_change", control=32, value=0, time=0, channel=channel))
        if spec.program is not None:
            head.append(mido.Message("program_change", program=spec.program, time=0, channel=channel))
    file.tracks[0][:0] = head
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


def track_names(midi: mido.MidiFile) -> list[str]:
    names = []
    for track in midi.tracks:
        name = next((message.name for message in track if message.type == "track_name"), "")
        names.append(name or "(unnamed)")
    return names


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
        0-127, or a drum kit on a drum track; ``bank`` selects the bank), and a ``gain_db`` trim
        for the combined mix. Track names must exist in the file and a soundfont preset must
        exist on this server. Returns the full spec map after the update.
        """
        store: FileStore = store_provider()
        source = store.existing_path(filename)
        midi = mido.MidiFile(source)
        names = track_names(midi)
        known = {names[index] for index in note_tracks(midi)}
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
            specs[name] = spec
        if specs:
            save_specs(source, specs)
        else:
            specs_path(source).unlink(missing_ok=True)
        return {
            "filename": source.name,
            "path": str(source),
            "tracks": {name: spec.model_dump() for name, spec in specs.items()},
        }

    @mcp.tool(title="List instruments", annotations=_READ_ONLY)
    def list_instruments(
        filename: Annotated[
            str | None, Field(description="Optional MIDI file whose per-track specs to report.")
        ] = None,
    ) -> results.InstrumentsResult:
        """List the rendering engines available on this server and a file's per-track specs.

        Read-only. Engines are invoked as separate processes (so a crashing or GPL-licensed
        host can never take the server down); today only FluidSynth is built in. Also returns the
        instrument catalog: the 128 GM melodic program names and the standard drum kits (the
        numbers set_track_instrument takes). With a filename, also returns each track's stored
        instrument spec and the tracks that can carry one.
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
        result: dict = {
            "engines": engines,
            "programs": {"melodic": list(GM_PROGRAMS), "drums": dict(GM_DRUM_KITS)},
            "file": None,
        }
        if filename is not None:
            source = store.existing_path(filename)
            midi = mido.MidiFile(source)
            names = track_names(midi)
            specs = load_specs(source)
            result["file"] = {
                "filename": source.name,
                "tracks": [names[index] for index in note_tracks(midi)],
                "specs": {name: spec.model_dump() for name, spec in specs.items()},
            }
        return result
