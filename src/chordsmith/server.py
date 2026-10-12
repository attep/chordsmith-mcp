"""ChordSmith MCP server: tools, resources and prompts."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import mido
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from chordsmith import __version__, audio, delivery, renderers, renderjobs, results, sections, singing
from chordsmith import sections as sections_module
from chordsmith.analysis import analyze_file, describe_midi
from chordsmith.auth import AuthConfig, enable_auth
from chordsmith.midi_writer import add_track as add_track_midi
from chordsmith.midi_writer import humanize_notes, swing_notes, transpose_file, write_empty, write_progression
from chordsmith.models import (
    Automation,
    ChordEvent,
    ChorusSettings,
    Humanize,
    LoopSpec,
    NoteInput,
    NumeralEvent,
    PatternRow,
    ReverbSettings,
    Rhythm,
    SongSection,
    Voicing,
)
from chordsmith.offload import offloaded
from chordsmith.storage import FileStore, StorageError
from chordsmith.theory import (
    CHORD_TYPES,
    Chord,
    MusicTheoryError,
    identify_chord,
    note_name,
    parse_chord,
    parse_key,
    parse_pitch,
    roman_to_chord,
    split_numerals,
)

INSTRUCTIONS = """ChordSmith turns musical choices into MIDI and audio files on this computer.

Workflow:
1. Create: create_chord_progression (chord symbols) or create_progression_from_roman
   (numerals + key). Read scales://{key} first to see the diatonic chords of a key.
2. Extend: add_track adds a melody/bass/drum track to a copy of a file.
3. Listen: render_audio renders a file to wav/mp3 without a music app.
4. Sing: prepare_vocal_score -> map_vocal_lyrics -> render_singing -> get_singing_job ->
   mix_song_with_vocals -> export_vocal_song (VOICEVOX for hums/kana, DiffSinger for English).
5. Share: get_midi_file returns the file itself (base64 or a signed link); export_vocal_song
   does the same for the mix, vocal and source MIDI.
6. Shape the sound: set_track_instrument stores per-track instrument specs (a soundfont per
   track, trims) and render_audio with stems=true renders every track on its own and combines
   them; list_instruments shows the engines this server can run.

Conventions:
- Files live in the output folder (see list_generated_files); '.mid'/'.wav'/'.mp3' is added to
  names automatically, and an existing name is never replaced unless overwrite=true (otherwise
  a '_2' suffix is used).
- Everything is deterministic for the same input and settings; create tools accept 'seed' for
  reproducible humanized renders, and render_singing accepts 'seed' to fix DiffSinger noise.
- Errors are tool results with isError=true and a readable message that names the problem and
  the fix (for example an unknown chord with a 'Did you mean' suggestion)."""

logger = logging.getLogger(__name__)

mcp = FastMCP("ChordSmith", instructions=INSTRUCTIONS)
store = FileStore()

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
CREATES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)

MAX_TILED_NOTES = 20000  # cap after a loop pattern is tiled, so one call cannot write forever
MAX_IMPORT_BYTES = 5 * 1024 * 1024  # import_midi: keep one base64 tool call to a sane size

PRESETS: dict[str, tuple[Voicing, Rhythm]] = {
    "lofi": (
        Voicing(voice_leading=True),
        Rhythm(
            pattern="pulse",
            subdivision=0.5,
            velocity=70,
            gate=0.6,
            swing=0.33,
            humanize=Humanize(timing_ms=12, velocity_range=10, seed=7),
        ),
    ),
}
Preset = Annotated[
    Literal["lofi"] | None,
    Field(
        description="Optional style preset. 'lofi' turns on voice leading and gives a soft, "
        "swung, slightly humanized feel. Explicit voicing/rhythm objects replace the preset's."
    ),
]
MidiType = Annotated[
    Literal[0, 1],
    Field(
        description="MIDI file type: 1 (default, separate tempo/chord tracks) or 0 (everything "
        "in one track with the tempo inline, for simple players that ignore track 0)."
    ),
]
Seed = Annotated[
    int | None,
    Field(
        ge=0,
        le=2**31 - 1,
        description="Fix the humanize random seed (overrides the preset's seed). The same seed "
        "always writes the same bytes; has no effect on rhythms without humanize.",
    ),
]
VoicingOption = Annotated[
    Voicing | None,
    Field(
        description="How each chord is arranged: style (close/open/drop2), inversion, octave, "
        "voice_leading, add_bass. Omit for close root position."
    ),
]
RhythmOption = Annotated[
    Rhythm | None,
    Field(
        description="How the chords move in time: pattern (block, pulse, arpeggio_up/down/updown, "
        "alberti, strum), subdivision, velocity, gate, swing, humanize. Omit for one held block "
        "chord per chord."
    ),
]


@mcp.custom_route("/healthz", methods=["GET"])
async def health_check(_request: Request) -> Response:
    """Liveness probe for container platforms (includes the running version)."""
    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/files/{filename}", methods=["GET"])
async def download_file(request: Request) -> Response:
    """Serve a generated file for a signed, expiring URL from get_midi_file/render_audio."""
    filename = request.path_params["filename"]
    if not delivery.verify(
        filename, request.query_params.get("expires", ""), request.query_params.get("token", "")
    ):
        return JSONResponse({"error": "Invalid or expired download link."}, status_code=403)
    try:
        path = store.existing_media_path(filename)
    except StorageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    media_type = {
        ".mid": "audio/midi",
        ".midi": "audio/midi",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }[path.suffix.lower()]
    return FileResponse(path, media_type=media_type, filename=path.name)


ChordList = Annotated[
    list[str | ChordEvent],
    Field(
        min_length=1,
        max_length=256,
        description='Chord symbols in order, e.g. ["Am", "F", "C", "G"]. To give a chord its own '
        'length use an object: {"chord": "G7", "beats": 2}.',
    ),
]
FileName = Annotated[
    str | None,
    Field(description="Output file name, e.g. 'sad_song'. '.mid' is added. Default: built from the chords."),
]
Tempo = Annotated[float, Field(ge=20, le=300, description="Tempo in beats per minute.")]
TimeSignature = Annotated[str, Field(description="Time signature such as '4/4', '3/4', '6/8'.")]
BeatsPerChord = Annotated[
    float | None,
    Field(gt=0, le=64, description="Default chord length in beats. Default: one bar."),
]
Repeat = Annotated[int, Field(ge=1, le=16, description="How many times to repeat the progression.")]
Instrument = Annotated[
    int,
    Field(
        ge=0,
        le=127,
        description="General MIDI program: 0 piano, 4 electric piano, 24 nylon guitar, "
        "25 steel guitar, 48 strings, 88 pad.",
    ),
]
Overwrite = Annotated[
    bool,
    Field(description="Replace an existing file with the same name; otherwise the new file gets a number."),
]


def _parse_time_signature(text: str) -> tuple[int, int]:
    try:
        num_text, den_text = text.replace(" ", "").split("/")
        num, den = int(num_text), int(den_text)
    except ValueError:
        raise ValueError(f"Time signature '{text}' should look like '4/4' or '6/8'.") from None
    if not 1 <= num <= 32 or den not in (1, 2, 4, 8, 16):
        raise ValueError(f"Time signature '{text}' is not supported.")
    return num, den


def _apply_preset(
    preset: str | None, voicing: Voicing | None, rhythm: Rhythm | None
) -> tuple[Voicing | None, Rhythm | None]:
    if preset is None:
        return voicing, rhythm
    preset_voicing, preset_rhythm = PRESETS[preset]
    return voicing or preset_voicing, rhythm or preset_rhythm


def _apply_seed(rhythm: Rhythm | None, seed: int | None) -> Rhythm | None:
    """Override the humanize seed so renders are reproducible on request."""
    if seed is None or rhythm is None or rhythm.humanize is None:
        return rhythm
    return rhythm.model_copy(update={"humanize": rhythm.humanize.model_copy(update={"seed": seed})})


def _render(
    chords: list[tuple[Chord, float]],
    *,
    filename: str | None,
    default_stem: str,
    tempo: float,
    time_signature: tuple[int, int],
    repeat: int,
    instrument: int,
    voicing: Voicing | None,
    rhythm: Rhythm | None,
    overwrite: bool,
    midi_type: int = 1,
) -> dict[str, Any]:
    with store.claimed_path(filename, default_stem, overwrite) as path:
        result = write_progression(
            path,
            chords,
            tempo_bpm=tempo,
            time_signature=time_signature,
            voicing=voicing or Voicing(),
            rhythm=rhythm or Rhythm(),
            program=instrument,
            repeat=repeat,
            title=path.stem,
            midi_type=midi_type,
        )
    return {
        "filename": path.name,
        "path": str(path),
        "uri": f"midi://{path.name}",
        "tempo": tempo,
        "time_signature": f"{time_signature[0]}/{time_signature[1]}",
        **result,
    }


def _stem(symbols: list[str]) -> str:
    return "_".join(symbols[:8]).replace("#", "s").replace("/", "-over-") or "progression"


@mcp.tool(title="Create chord progression", annotations=CREATES)
def create_chord_progression(
    chords: ChordList,
    filename: FileName = None,
    tempo: Tempo = 100,
    time_signature: TimeSignature = "4/4",
    beats_per_chord: BeatsPerChord = None,
    repeat: Repeat = 1,
    instrument: Instrument = 0,
    voicing: VoicingOption = None,
    rhythm: RhythmOption = None,
    preset: Preset = None,
    seed: Seed = None,
    midi_type: MidiType = 1,
    overwrite: Overwrite = False,
) -> results.ProgressionResult:
    """Write chord symbols (e.g. ["Am", "F", "C", "G"]) to a new MIDI file.

    Use this to start a song from chord names; for Roman numerals use
    create_progression_from_roman instead. Slash chords (C/E) and 30 qualities are supported
    (see list_chord_types). Creates a file in the output folder and returns the file name/path,
    the notes used for every chord, tempo, length in seconds and bars. Errors name the bad chord
    (with a suggestion); on failure no file is left behind.
    """
    ts = _parse_time_signature(time_signature)
    default_beats = beats_per_chord or ts[0] * 4 / ts[1]
    parsed: list[tuple[Chord, float]] = []
    for item in chords:
        if isinstance(item, ChordEvent):
            parsed.append((parse_chord(item.chord), item.beats))
        else:
            parsed.append((parse_chord(item), default_beats))
    voicing, rhythm = _apply_preset(preset, voicing, rhythm)
    rhythm = _apply_seed(rhythm, seed)
    return _render(
        parsed,
        filename=filename,
        default_stem=_stem([c.symbol for c, _ in parsed]),
        tempo=tempo,
        time_signature=ts,
        repeat=repeat,
        instrument=instrument,
        voicing=voicing,
        rhythm=rhythm,
        overwrite=overwrite,
        midi_type=midi_type,
    )


@mcp.tool(title="Create progression from Roman numerals", annotations=CREATES)
def create_progression_from_roman(
    numerals: Annotated[
        str | list[str | NumeralEvent],
        Field(
            description="Roman numerals, as a string ('i-VI-III-VII', 'I vi IV V') or a list "
            '(["ii7", "V7", "Imaj7"] or [{"numeral": "V7", "beats": 2}]). Upper case = major, '
            "lower case = minor, ° = diminished, ø = half-diminished, b/# = borrowed chords "
            "(bVII), slash = secondary chords (V7/V)."
        ),
    ],
    key: Annotated[str, Field(description="Key, e.g. 'C major', 'A minor', 'D dorian', 'Bb'.")],
    filename: FileName = None,
    tempo: Tempo = 100,
    time_signature: TimeSignature = "4/4",
    beats_per_chord: BeatsPerChord = None,
    repeat: Repeat = 1,
    instrument: Instrument = 0,
    voicing: VoicingOption = None,
    rhythm: RhythmOption = None,
    preset: Preset = None,
    seed: Seed = None,
    midi_type: MidiType = 1,
    overwrite: Overwrite = False,
) -> results.RomanProgressionResult:
    """Write a Roman-numeral progression in a key (e.g. i-VI-III-VII in A minor) to a new MIDI file.

    Use this when the key and numerals are known; for chord names use
    create_chord_progression instead. Creates a file in the output folder and returns the key,
    the numerals, the resolved chord symbols (e.g. Am, F, C, G) and the same file details as
    create_chord_progression. Unknown modes and numerals are rejected with a suggestion.
    """
    parsed_key = parse_key(key)
    ts = _parse_time_signature(time_signature)
    default_beats = beats_per_chord or ts[0] * 4 / ts[1]
    items = split_numerals(numerals) if isinstance(numerals, str) else numerals
    if not items:
        raise ValueError("Give at least one Roman numeral.")
    if len(items) > 256:
        raise ValueError("At most 256 chords per progression.")
    parsed: list[tuple[Chord, float]] = []
    labels: list[str] = []
    for item in items:
        if isinstance(item, NumeralEvent):
            parsed.append((roman_to_chord(item.numeral, parsed_key), item.beats))
            labels.append(item.numeral)
        else:
            parsed.append((roman_to_chord(item, parsed_key), default_beats))
            labels.append(item)
    voicing, rhythm = _apply_preset(preset, voicing, rhythm)
    rhythm = _apply_seed(rhythm, seed)
    result = _render(
        parsed,
        filename=filename,
        default_stem=f"{parsed_key.name}_{'-'.join(labels[:8])}",
        tempo=tempo,
        time_signature=ts,
        repeat=repeat,
        instrument=instrument,
        voicing=voicing,
        rhythm=rhythm,
        overwrite=overwrite,
        midi_type=midi_type,
    )
    return {
        "key": parsed_key.name,
        "numerals": labels,
        "resolved_chords": [c.symbol for c, _ in parsed],
        **result,
    }


def _chord_type_rows() -> list[dict]:
    rows = []
    for ct in CHORD_TYPES:
        c_symbol = "C" + ct.aliases[0]
        rows.append(
            {
                "quality": ct.name,
                "symbols": [a for a in ct.aliases if a] or ["(none)"],
                "example": c_symbol,
                "intervals_semitones": list(ct.intervals),
                "notes_in_C": parse_chord(c_symbol).note_names,
                "description": ct.description,
            }
        )
    return rows


@mcp.tool(title="Create empty file", annotations=CREATES)
def create_empty_file(
    filename: Annotated[str, Field(description="Name for the new file, e.g. 'abyss'. '.mid' is added.")],
    sections: Annotated[
        list[SongSection] | None,
        Field(
            max_length=64,
            description="Optional section map to write straight into the conductor (like "
            "set_song_sections), so add_track can use section+bar+beat positions.",
        ),
    ] = None,
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> results.EmptyFileResult:
    """Create a blank MIDI file (a conductor track only) to build a song on from scratch.

    Use it when a song should not start from a chord progression: create the empty file, then
    add_track each part onto it. With ``sections`` the tempo/meter map is written immediately.
    Returns the file's name/path and the section map it carries.
    """
    with store.claimed_path(output_filename or filename, f"{filename}_empty", overwrite) as target:
        info = write_empty(target, target.stem)
        resolved: list[dict] = []
        if sections:
            names = [section.name for section in sections]
            if len(set(names)) != len(names):
                duplicates = sorted({name for name in names if names.count(name) > 1})
                raise ValueError(f"Duplicate section names: {', '.join(duplicates)}.")
            resolved = [section.payload() for section in sections_module.write_sections(target, sections)]
    return {
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        **info,
        "sections": resolved,
    }


@mcp.tool(title="Remove track", annotations=CREATES)
def remove_track(
    filename: Annotated[str, Field(description="Existing file to copy without the track.")],
    track_name: Annotated[str, Field(description="Name of the track to leave out, e.g. 'Chords'.")],
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> results.RemoveTrackResult:
    """Write a copy of the file without one track; the original is kept.

    Finds the track by name (names must be unique; rename first). output_filename must differ
    from the source. Returns the copy's name/path and remaining track count. The per-track
    instrument sidecar belongs to the original; the copy starts without specs.
    """
    source = store.existing_path(filename)
    midi = mido.MidiFile(source)
    names = renderers.track_names(midi)
    matches = [index for index, name in enumerate(names) if name == track_name]
    if not matches:
        available = ", ".join(names) or "(none)"
        raise ValueError(f"No track named '{track_name}'. Tracks: {available}.")
    if len(matches) > 1:
        raise ValueError(f"Several tracks are named '{track_name}'; rename them first.")
    with store.claimed_path(output_filename, f"{source.stem}_no_{track_name}", overwrite) as target:
        if target == source:
            raise ValueError("Choose a different output_filename; the original file is never modified.")
        del midi.tracks[matches[0]]
        midi.save(target)
    return {
        "source": source.name,
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "removed": track_name,
        "total_tracks": len(midi.tracks),
    }


@mcp.tool(title="List chord types", annotations=READ_ONLY)
def list_chord_types() -> list[results.ChordTypeRow]:
    """Return every supported chord quality, with the symbols you can write and its notes in C.

    Each row gives the quality name, all accepted symbols, an example spelling, intervals in
    semitones and the notes in C. Read-only; call this when a chord symbol was rejected or before
    inventing spellings.
    """
    return _chord_type_rows()


@mcp.tool(title="Transpose MIDI", annotations=CREATES)
def transpose_midi(
    filename: Annotated[str, Field(description="File in the output folder, e.g. 'Am_F_C_G.mid'.")],
    semitones: Annotated[
        int | None, Field(ge=-24, le=24, description="Semitones to shift, e.g. 2 = up a whole step.")
    ] = None,
    from_key: Annotated[
        str | None, Field(description="Current key (use with to_key), e.g. 'A minor'.")
    ] = None,
    to_key: Annotated[str | None, Field(description="Target key, e.g. 'C minor'.")] = None,
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> results.TransposeResult:
    """Transpose a MIDI file by semitones, or from one key to another. Drums (channel 10) are untouched.

    Creates a new file (the original is kept; output_filename must differ from the source) and
    returns its name/path plus how many notes moved. Give either 'semitones' or both 'from_key'
    and 'to_key'; with keys, the smallest move
    (up to 6 semitones either way) is chosen. Chord-name markers are transposed too.
    """
    source = store.existing_path(filename)
    if semitones is None:
        if not (from_key and to_key):
            raise ValueError("Give either 'semitones' or both 'from_key' and 'to_key'.")
        shift = (parse_key(to_key).tonic - parse_key(from_key).tonic) % 12
        semitones = shift - 12 if shift > 6 else shift
    direction = f"up{semitones}" if semitones >= 0 else f"down{-semitones}"
    with store.claimed_path(output_filename, f"{source.stem}_{direction}", overwrite) as target:
        if target == source:
            raise ValueError("Choose a different output_filename; the original file is never replaced.")
        info = transpose_file(source, target, semitones)
    return {
        "source": source.name,
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "semitones": semitones,
        **info,
    }


@mcp.tool(title="Analyze MIDI", annotations=READ_ONLY)
def analyze_midi(
    filename: Annotated[str, Field(description="File in the output folder to analyse.")],
    window_beats: Annotated[
        float | None,
        Field(gt=0, le=64, description="Analysis window in beats. Default: one bar."),
    ] = None,
) -> results.AnalyzeResult:
    """Guess the chords, tempo and key of a MIDI file, window by window.

    Read-only; useful to check what a file (or a transposed copy) actually contains. Chord-name
    markers written by ChordSmith are used when their notes match the window, so inversions and
    added melody notes still read back as the original chords. Works best with one chord per
    window; with fast changes it may return '?' or 'N.C.'.
    """
    return analyze_file(store.existing_path(filename), window_beats)


@mcp.tool(title="List generated files", annotations=READ_ONLY)
def list_generated_files(
    name_contains: Annotated[
        str | None, Field(description="Only files whose name contains this text (case-insensitive).")
    ] = None,
    kind: Annotated[
        Literal["midi", "wav", "mp3"] | None, Field(description="Only files of this kind.")
    ] = None,
    limit: Annotated[
        int, Field(ge=1, le=2000, description="Most files to return, newest first (default 200).")
    ] = 200,
) -> results.ListFilesResult:
    """Return the files in the output folder (MIDI and rendered audio, newest first).

    Read-only; each entry has a kind (midi/wav/mp3), a size and an ``empty`` flag, so you can
    confirm a render landed and spot 0-byte leftovers. Filter with ``name_contains`` and
    ``kind`` and cap the reply with ``limit``; ``total`` says how many files matched before the
    cap. Use it to find file names for get_midi_file, get_file, analyze_midi, transpose_midi or
    the singing tools.
    """
    root = store.ensure_root()
    files = store.list_files()
    if name_contains:
        needle = name_contains.lower()
        files = [entry for entry in files if needle in entry["filename"].lower()]
    if kind:
        files = [entry for entry in files if entry["kind"] == kind]
    total = len(files)
    return {"output_dir": str(root), "total": total, "files": files[:limit]}


ReturnAs = delivery.ReturnAs


@mcp.tool(title="Get MIDI file", annotations=READ_ONLY)
def get_midi_file(
    filename: Annotated[str, Field(description="File in the output folder, e.g. 'melancholy.mid'.")],
    return_as: ReturnAs = "base64",
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
) -> results.GetMidiFileResult:
    """Return the MIDI file itself so tool-only clients can hand it to the user.

    Read-only. Use return_as='base64' for the bytes inline (with size and sha256), or
    return_as='url' for a signed link that expires after expires_in seconds (needs
    CHORDSMITH_PUBLIC_URL). The link is a capability URL: anyone holding it can download until
    it expires, and '../' style names are rejected.
    """
    path = store.existing_path(filename)
    data = path.read_bytes()
    result = delivery.deliver_file(path.name, data, return_as, expires_in)
    result["mime_type"] = "audio/midi"
    return result


@mcp.tool(title="Get file", annotations=READ_ONLY)
def get_file(
    filename: Annotated[str, Field(description="File in the output folder: .mid, .wav or .mp3.")],
    return_as: ReturnAs = "base64",
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
) -> results.GetFileResult:
    """Return any output file — MIDI, wav or mp3 — so it can be checked or handed to the user.

    Read-only; like get_midi_file but for every kind of file: stems, mixes, vocal takes,
    imports. base64 returns the bytes inline; url a signed, expiring link (needs
    CHORDSMITH_PUBLIC_URL). Use list_generated_files to find names.
    """
    path = store.existing_media_path(filename)
    data = path.read_bytes()
    result = delivery.deliver_file(path.name, data, return_as, expires_in)
    suffix = path.suffix.lower()
    result["mime_type"] = {".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(suffix, "audio/midi")
    return result


MAX_IMPORT_AUDIO_BYTES = 25 * 1024 * 1024  # import_audio: keep one base64 call to a sane size


@mcp.tool(title="Import audio", annotations=CREATES)
def import_audio(
    filename: Annotated[str, Field(description="Name for the imported file, e.g. 'take.wav' or 'take.mp3'.")],
    data_base64: Annotated[str, Field(description="The audio bytes, base64-encoded (up to 25 MB decoded).")],
    overwrite: Overwrite = False,
) -> results.ImportAudioResult:
    """Import an audio file (wav or mp3) into the output folder, e.g. a vocal take or a render
    made elsewhere.

    The bytes are stored verbatim; the result reports size, sha256, duration, sample rate,
    channels and (for wav) the sample peak, so the file can be used with mix_song_with_vocals
    without guessing what it is. MP3 metadata needs ffprobe.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in (".wav", ".mp3"):
        raise ValueError("Import a .wav or .mp3 file (MIDI files go through import_midi).")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data_base64 is not valid base64.") from None
    if len(data) > MAX_IMPORT_AUDIO_BYTES:
        raise ValueError(
            f"The file is {len(data)} bytes; the import limit is {MAX_IMPORT_AUDIO_BYTES} bytes."
        )
    if suffix == ".wav" and data[:4] != b"RIFF":
        raise ValueError("The bytes are not a RIFF wav file.")
    with store.claimed_path(filename, "imported", overwrite, suffix) as target:
        target.write_bytes(data)
    try:
        probe = audio.probe_media(target)
    except audio.AudioError as exc:
        raise ValueError(str(exc)) from None
    peak_db: float | None = None
    if suffix == ".wav":
        try:
            peak_db = round(audio.wav_peak_db(target), 2)
        except (OSError, wave.Error):
            peak_db = None
    return {
        "filename": target.name,
        "path": str(target),
        "uri": f"audio://{target.name}",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": "audio/wav" if suffix == ".wav" else "audio/mpeg",
        "format": suffix[1:],
        **probe,
        "peak_db": peak_db,
    }


@mcp.tool(title="Import MIDI", annotations=CREATES)
def import_midi(
    filename: Annotated[str, Field(description="Name for the imported file, e.g. 'sketch.mid'.")],
    data_base64: Annotated[str, Field(description="The .mid bytes, base64-encoded (up to 5 MB decoded).")],
    overwrite: Overwrite = False,
) -> results.ImportResult:
    """Import a MIDI file made elsewhere (a DAW, a generator) into the output folder.

    The bytes are stored **verbatim** — nothing is transformed or discarded — and the result is
    a structured report of what the file contains: tracks with their channels/programs/note
    counts, every tempo and time-signature change, markers, the SysEx count, and warnings for
    layouts the tools care about (type 0 files, tempo events in several tracks, missing
    tempo/meter, no notes). The imported file then works with the normal workflow: analyze_midi,
    set_song_sections, add_track, set_track_instrument, render_audio (stems) and the singing
    tools.
    """
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("data_base64 is not valid base64.") from None
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError(f"The file is {len(data)} bytes; the import limit is {MAX_IMPORT_BYTES} bytes.")
    try:
        midi = mido.MidiFile(file=io.BytesIO(data))
    except Exception:
        raise ValueError("The bytes are not a readable MIDI file.") from None
    with store.claimed_path(filename, "imported", overwrite) as target:
        target.write_bytes(data)
    return {
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mime_type": "audio/midi",
        **describe_midi(midi),
    }


def _initial_bpm(source: Path) -> float:
    """The file's own tempo (its first set_tempo); 120 BPM when it has none."""
    for track in mido.MidiFile(source).tracks:
        for message in track:
            if message.type == "set_tempo":
                return mido.tempo2bpm(message.tempo)
    return 120.0


def _bpm_lookup(source: Path, resolved: list[sections.ResolvedSection] | None) -> Callable[[float], float]:
    """Tempo at a beat: the section map's bpm when present, else the file's own tempo.

    Humanize converts its millisecond offsets to beats per note, so a section at another bpm
    keeps the same feel in absolute time.
    """
    if resolved:
        ordered = sorted(resolved, key=lambda section: section.start_beat)

        def from_sections(beat: float) -> float:
            bpm = 120.0
            for section in ordered:
                if section.start_beat <= beat + 1e-9:
                    bpm = section.bpm
                else:
                    break
            return bpm

        return from_sections
    bpm = _initial_bpm(source)
    return lambda beat: bpm


@mcp.tool(title="Add track", annotations=CREATES)
def add_track(
    filename: Annotated[str, Field(description="Existing file to copy and extend.")],
    track_name: Annotated[str, Field(description="Name of the new track, e.g. 'Melody'.")],
    notes: Annotated[
        list[NoteInput] | None,
        Field(
            max_length=5000,
            description='Notes, e.g. [{"pitch": "E6", "start_beat": 0, "beats": 0.5, "velocity": 80}]. '
            "Pitch is a note name with octave or a MIDI number 0-127. Position each note with "
            "start_beat (absolute) or with section+bar+beat relative to the section map "
            "(set_song_sections). Give notes, patterns, or both.",
        ),
    ] = None,
    patterns: Annotated[
        list[PatternRow] | None,
        Field(
            max_length=32,
            description="Compact step patterns, e.g. [{'pitch': 36, 'steps': 'x---x---'}, "
            "{'pitch': 38, 'steps': '----x---', 'velocity': 100}]. Each character is one step "
            "(step_beats long): 'x' hits, 'X' accents, '-' or '.' rests, '|' and spaces are bar "
            "lines. Give notes, patterns, or both.",
        ),
    ] = None,
    step_beats: Annotated[
        float,
        Field(gt=0, le=4, description="Length of one pattern step in beats (default 0.25 = a sixteenth)."),
    ] = 0.25,
    automation: Annotated[
        list[Automation] | None,
        Field(
            max_length=64,
            description="Linear controller ramps, e.g. [{'cc': 11, 'from_value': 40, "
            "'to_value': 110, 'start_beat': 0, 'end_beat': 12}] to swell a held note (CC11 "
            "expression) or fade a track (CC7 volume). One value every sixteenth note.",
        ),
    ] = None,
    instrument: Instrument = 0,
    channel: Annotated[int, Field(ge=1, le=16, description="MIDI channel, 1-16 (10 = drums).")] = 1,
    loop: Annotated[
        LoopSpec | None,
        Field(
            description="Repeat a short pattern across the song: the notes are positions inside "
            "the pattern window and repeat back to back, so a one-bar drum or bass groove is "
            "written once. Omit to place the notes as given."
        ),
    ] = None,
    swing: Annotated[
        float,
        Field(
            ge=0,
            le=0.75,
            description="Delay notes on offbeat steps of the grid (odd multiples of step_beats) "
            "by this fraction of a step: 0 = straight, 0.33 = triplet feel, 0.5 = dotted feel.",
        ),
    ] = 0,
    humanize: Annotated[
        Humanize | None,
        Field(
            description="Small, seed-controlled timing/velocity variation (the same object as "
            "the chord tools' rhythm.humanize). Applied after loop tiling, so repeats differ "
            "while a fixed seed stays reproducible."
        ),
    ] = None,
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> results.AddTrackResult:
    """Add a note-level track (melody, bass, drums, ...) to a copy of an existing MIDI file.

    Creates a new file; the original is never modified (output_filename must differ from the
    source, even with overwrite). The copy keeps all existing tracks and
    gets the new one on the chosen channel (a type 0 file is promoted to type 1). Returns the new
    file's name/path, how many notes/tracks it has and how many automation events were written.
    Pitches outside 0-127 and overlapping notes are fine; the track is as monophonic or
    polyphonic as you write it. Notes can be positioned in absolute beats, relative to a section
    (section+bar+beat, see set_song_sections), or on an exact tuplet slot ({"count": 3,
    "in_beats": 2, "start_beat": 138, "slot": 0}); a note may run past its section's end.
    ``patterns`` write drum-style grids compactly — one row per voice, 'x' hits, 'X' accents —
    from beat 0. ``automation`` writes linear controller ramps (CC11 swells, CC7 fades). With
    ``loop``, a short pattern (for example one bar of drums) is tiled across the song. With
    ``swing``, notes on offbeat steps of the grid move later (0.33 = triplet feel); ``humanize``
    adds seeded timing/velocity variation after the tiling, so repeated loops breathe.
    """
    source = store.existing_path(filename)
    with store.claimed_path(output_filename, f"{source.stem}_{track_name}", overwrite) as target:
        if target == source:
            raise ValueError("Choose a different output_filename; the original file is never modified.")
        if not notes and not patterns:
            raise ValueError("Give notes, patterns, or both.")
        ticks_per_beat = mido.MidiFile(source).ticks_per_beat
        parsed: list[tuple[int, float, float, int]] = []
        for row in patterns or []:
            try:
                row_pitch = parse_pitch(row.pitch)
            except MusicTheoryError as exc:
                raise ValueError(str(exc)) from None
            step = 0
            for char in row.steps:
                if char in "| \t":  # bar lines are separators; they do not consume a step
                    continue
                if char in "-._":
                    step += 1
                    continue
                if char not in "xXoO":
                    raise ValueError(
                        f"Unknown step '{char}' in the pattern for {row.pitch!r}; use 'x' for "
                        "a hit, 'X' for an accent, '-' or '.' for a rest, '|' as a bar line."
                    )
                velocity = min(127, row.velocity + 25) if char in "XO" else row.velocity
                parsed.append((row_pitch, step * step_beats, step_beats, velocity))
                step += 1
        resolved_sections: list[sections.ResolvedSection] | None = None
        for note in notes or []:
            try:
                pitch = parse_pitch(note.pitch)
            except MusicTheoryError as exc:
                raise ValueError(str(exc)) from None
            start = note.start_beat
            if note.section is not None:
                if resolved_sections is None:
                    resolved_sections = sections.read_sections(source)
                    if not resolved_sections:
                        raise ValueError(
                            "This file has no section map; call set_song_sections first, or "
                            "position notes with start_beat."
                        )
                section = next((item for item in resolved_sections if item.name == note.section), None)
                if section is None:
                    available = ", ".join(item.name for item in resolved_sections)
                    raise ValueError(f"Unknown section '{note.section}'. Sections: {available}.")
                bar = note.bar if note.bar is not None else 1
                if bar > section.bars:
                    raise ValueError(
                        f"Section '{section.name}' has {section.bars} bars; bar {bar} is past "
                        "its end (notes may run past the end, but not start there)."
                    )
                start = (
                    section.start_beat
                    + (bar - 1) * section.beats_per_bar
                    + ((note.beat if note.beat is not None else 1.0) - 1)
                )
            elif note.tuplet is not None:
                # exact tick math: the group start plus the slot's share of the span
                start_tick = round(note.tuplet.start_beat * ticks_per_beat) + round(
                    note.tuplet.slot * note.tuplet.in_beats * ticks_per_beat / note.tuplet.count
                )
                start = start_tick / ticks_per_beat
            parsed.append((pitch, start, note.beats, note.velocity))
        if loop is not None:
            for _, start, beats, _ in parsed:
                if start >= loop.length_beats:
                    raise ValueError(
                        f"A loop note starts at beat {start:g}, but the pattern is only "
                        f"{loop.length_beats:g} beats long."
                    )
                if start + beats > loop.length_beats + 1e-9:
                    raise ValueError(
                        f"A loop note ends at beat {start + beats:g}, past the pattern length "
                        f"{loop.length_beats:g}."
                    )
            total = len(parsed) * loop.times
            if total > MAX_TILED_NOTES:
                raise ValueError(
                    f"The loop would produce {total} notes (limit {MAX_TILED_NOTES}); "
                    "reduce the pattern or the repeat count."
                )
            parsed = [
                (pitch, start + repeat * loop.length_beats, beats, velocity)
                for repeat in range(loop.times)
                for pitch, start, beats, velocity in parsed
            ]
        parsed = swing_notes(parsed, swing, step_beats)
        if humanize is not None and (humanize.timing_ms or humanize.velocity_range):
            if resolved_sections is None:
                resolved_sections = sections.read_sections(source)
            parsed = humanize_notes(parsed, humanize, _bpm_lookup(source, resolved_sections))
        ramps = [
            (item.cc, item.from_value, item.to_value, item.start_beat, item.end_beat)
            for item in automation or []
        ]
        info = add_track_midi(
            source,
            target,
            track_name=track_name,
            program=instrument,
            channel=channel - 1,
            notes=parsed,
            automation=ramps,
        )
    return {
        "source": source.name,
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "track_name": track_name,
        "channel": channel,
        "swing": swing,
        "humanize": humanize.model_dump(exclude_none=True) if humanize is not None else None,
        **info,
    }


@mcp.tool(title="Delete MIDI file", annotations=DESTRUCTIVE_IDEMPOTENT)
def delete_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to delete.")],
) -> results.DeleteResult:
    """Delete a MIDI file from the output folder.

    Removes the file permanently (and its instrument and section sidecars, if any) and returns
    its name; use list_generated_files to confirm the name first. Fails with a clear message
    when the file does not exist.
    """
    path = store.remove(filename)
    renderers.specs_path(path).unlink(missing_ok=True)
    sections.sections_sidecar_path(path).unlink(missing_ok=True)
    return {"deleted": path.name}


@mcp.tool(title="Rename MIDI file", annotations=DESTRUCTIVE)
def rename_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to rename.")],
    new_name: Annotated[str, Field(description="New name, e.g. 'lofi_sketch'. '.mid' is added.")],
) -> results.RenameResult:
    """Rename a MIDI file. Refuses to overwrite an existing file.

    Changes the file's name in place and returns the new name/path. If the target name exists,
    the call fails and nothing changes. The per-track instrument sidecar (set_track_instrument)
    and the section sidecar (set_song_sections) move with the file.
    """
    source = store.existing_path(filename)
    target = store.rename(filename, new_name)
    for sidecar_path in (renderers.specs_path, sections.sections_sidecar_path):
        sidecar = sidecar_path(source)
        if sidecar.is_file():
            sidecar.replace(sidecar_path(target))
    return {"filename": target.name, "path": str(target), "uri": f"midi://{target.name}"}


@mcp.tool(title="Render audio", annotations=CREATES)
@offloaded
def render_audio(
    filename: Annotated[str, Field(description="MIDI file in the output folder to render.")],
    format: Annotated[Literal["wav", "mp3"], Field(description="Audio format.")] = "wav",
    return_as: ReturnAs = "base64",
    soundfont: Annotated[
        str | None,
        Field(
            description="Path to a .sf2 soundfont. Default: CHORDSMITH_SOUNDFONT or a standard system path."
        ),
    ] = None,
    stems: Annotated[
        bool,
        Field(
            description="Render each track as its own wav stem (using set_track_instrument "
            "specs) and combine them into the mix. One render per track (several at once), so "
            "it is slower; stems always get unique names. Stem entries are delivered as signed "
            "URLs when a public URL is configured, else like return_as. With reverb/chorus on, "
            "every stem is rendered wet (the engine is per render instance), so summing stems "
            "is not identical to one wet pass."
        ),
    ] = False,
    normalize_peak_db: Annotated[
        float | None,
        Field(
            ge=-6.0,
            le=0.0,
            description="Gain the rendered mix so its peak hits this level in dBFS (set -1.0 "
            "for a listening-ready file). Default null keeps FluidSynth's raw level; stems stay "
            "raw either way so their balance sums honestly.",
        ),
    ] = None,
    reverb: Annotated[
        ReverbSettings | None,
        Field(
            description="FluidSynth reverb for this render (off when omitted): room_size, "
            "damping, width, level. Engine-wide for the render instance, not per track."
        ),
    ] = None,
    chorus: Annotated[
        ChorusSettings | None,
        Field(
            description="FluidSynth chorus for this render (off when omitted): voices, level, "
            "speed, depth. Engine-wide for the render instance, not per track."
        ),
    ] = None,
    output_filename: FileName = None,
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
    overwrite: Overwrite = False,
) -> results.RenderAudioResult:
    """Render a MIDI file to audio (wav or mp3) with FluidSynth so it can be heard without a music app.

    Creates an audio file next to the MIDI files and returns it like get_midi_file (base64 bytes
    or a signed, expiring URL), plus the source name, the sample peak (wav), the gain applied by
    normalize_peak_db, whether the export clips, and the effective `effects` (reverb/chorus).
    MP3 needs ffmpeg; FluidSynth and a soundfont are required (both are in the Docker image).
    With ``stems``, each track is rendered on its own (per-track soundfont/program/gain or
    sfizz instrument from set_track_instrument) and the stems are combined into the mix; the
    result then also lists
    every stem with its own peak. Stems are never normalized (their balance must stay intact);
    only the combined mix follows normalize_peak_db. Reverb/chorus are off by default (renders
    stay byte-identical); with them on the engine's tail past the last note is kept, and note
    that they are engine-wide per render instance — with stems every stem is rendered wet, so
    summing wet stems is not identical to one wet pass. On failure a clear error explains what
    is missing and no file is left behind.
    """
    source = store.existing_path(filename)
    if not stems:
        _reject_engine_tracks_without_stems(source)
    effects = renderers.effect_options(reverb, chorus)
    effects_echo = {
        "reverb": reverb.model_dump() if reverb is not None else None,
        "chorus": chorus.model_dump() if chorus is not None else None,
    }
    if stems:
        result = _render_stems(
            source,
            format,
            return_as,
            expires_in,
            output_filename,
            overwrite,
            soundfont,
            normalize_peak_db,
            effects,
        )
        result["effects"] = effects_echo
        return result
    return _render_single(
        source,
        format,
        return_as,
        expires_in,
        output_filename,
        overwrite,
        soundfont,
        normalize_peak_db,
        effects,
        effects_echo,
    )


def _reject_engine_tracks_without_stems(source: Path) -> None:
    """A single FluidSynth pass cannot play sfizz or vst tracks; stems render each own engine."""
    specs = renderers.load_specs(source)
    engines = sorted(name for name, spec in specs.items() if spec.engine in ("sfizz", "vst"))
    if engines:
        raise ValueError(
            f"This file uses per-track engines ({', '.join(engines)}); render with stems=true so "
            "each track is rendered by its own engine."
        )


def _render_single(
    source: Path,
    audio_format: str,
    return_as: str,
    expires_in: int,
    output_filename: str | None,
    overwrite: bool,
    soundfont: str | None,
    normalize_peak_db: float | None,
    effects: dict[str, float | int] | None,
    effects_echo: dict[str, Any],
) -> dict[str, Any]:
    """One FluidSynth pass over the whole file, delivered like get_midi_file."""
    with store.claimed_path(
        output_filename or source.stem, f"{source.stem}_{audio_format}", overwrite, f".{audio_format}"
    ) as target:
        try:
            correction = audio.render(source, target, audio_format, soundfont, normalize_peak_db, effects)
        except audio.AudioError as exc:
            raise ValueError(str(exc)) from None
    data = target.read_bytes()
    result = delivery.deliver_file(target.name, data, return_as, expires_in)
    result["mime_type"] = "audio/wav" if audio_format == "wav" else "audio/mpeg"
    result["source"] = source.name
    result["gain_correction_db"] = correction
    result["effects"] = effects_echo
    if audio_format == "wav":
        peak = audio.wav_peak_db(target)
        result["peak_db"] = round(peak, 2)
        result["clipping"] = peak > -0.05
        result["duration_seconds"] = round(audio.wav_duration(target), 2)
    else:
        result["peak_db"] = None
        result["clipping"] = False
    return result


def _run_audio_job(args: dict[str, Any], set_state) -> dict[str, Any]:
    """The render pipeline for background jobs (renderjobs.py runs this on a worker thread)."""
    source = store.existing_path(args["filename"])
    effects = renderers.effect_options(args.get("reverb"), args.get("chorus"))
    effects_echo = {
        "reverb": args["reverb"].model_dump() if args.get("reverb") is not None else None,
        "chorus": args["chorus"].model_dump() if args.get("chorus") is not None else None,
    }
    only_tracks = args.get("tracks")
    if not (args.get("stems") or only_tracks):
        _reject_engine_tracks_without_stems(source)
    if args.get("stems") or only_tracks:
        result = _render_stems(
            source,
            args.get("format", "wav"),
            args.get("return_as", "base64"),
            args.get("expires_in", delivery.DEFAULT_TTL_SECONDS),
            args.get("output_filename"),
            bool(args.get("overwrite")),
            args.get("soundfont"),
            args.get("normalize_peak_db"),
            effects,
            only_tracks=only_tracks,
            progress=set_state,
            continue_on_error=True,
        )
        result["effects"] = effects_echo
        return result
    return _render_single(
        source,
        args.get("format", "wav"),
        args.get("return_as", "base64"),
        args.get("expires_in", delivery.DEFAULT_TTL_SECONDS),
        args.get("output_filename"),
        bool(args.get("overwrite")),
        args.get("soundfont"),
        args.get("normalize_peak_db"),
        effects,
        effects_echo,
    )


def _render_stems(
    source: Path,
    audio_format: str,
    return_as: str,
    expires_in: int,
    output_filename: str | None,
    overwrite: bool,
    soundfont: str | None,
    normalize_peak_db: float | None = None,
    effects: dict[str, float | int] | None = None,
    only_tracks: list[str] | None = None,
    progress=None,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    """Render every note track on its own and combine the stems into the mix.

    ``only_tracks`` renders (and combines) just those tracks — the retry path for background
    jobs; ``progress`` is called as ``(track, state, error=None)`` per track; with
    ``continue_on_error`` a failed track is reported instead of aborting the whole render, and
    the mix is combined from the successful stems.
    """
    midi = mido.MidiFile(source)
    specs = renderers.load_specs(source)
    names = renderers.track_names(midi)
    tracks = renderers.note_tracks(midi)
    section_timeline = sections.timeline_seconds(sections.read_sections(source))
    if only_tracks is not None:
        available = [names[index] for index in tracks]
        unknown = sorted(set(only_tracks) - set(available))
        if unknown:
            raise ValueError(f"Unknown track(s) {', '.join(unknown)}. Tracks: {', '.join(available)}.")
        wanted = set(only_tracks)
        tracks = [index for index in tracks if names[index] in wanted]
    muted = {name for name, spec in specs.items() if spec.mute}
    if muted:
        tracks = [index for index in tracks if names[index] not in muted]
    if not tracks:
        raise ValueError("This file has no unmuted note tracks to render.")
    with tempfile.TemporaryDirectory() as scratch:

        def render_track(index: int):
            name = names[index]
            if progress is not None:
                progress(name, "running")
            stem_midi = Path(scratch) / f"stem_{index}.mid"
            singing.extract_track(source, stem_midi, index)
            spec = specs.get(name) or renderers.InstrumentSpec(preset=soundfont)
            renderer = renderers.get_renderer(spec.engine)
            with store.claimed_path(None, f"{source.stem}_{name}", extension=".wav") as stem_wav:
                try:
                    renderer.render(stem_midi, spec, stem_wav, effects)
                    windows = renderers.section_gain_windows(spec, section_timeline)
                    if windows:
                        renderers.apply_section_gains(stem_wav, windows, spec.gain_db)
                    renderers.apply_stem_effects(stem_wav, spec.effects)
                except renderers.RenderError as exc:
                    message = f"Track '{name}': {exc}"
                    if not continue_on_error:
                        raise ValueError(message) from None
                    if progress is not None:
                        progress(name, "failed", message)
                    return ("failed", name, message)
                if progress is not None:
                    progress(name, "done")
                return ("ok", renderers.Stem(track=name, path=stem_wav, gain_db=spec.gain_db), None)

        outcomes = renderers.parallel_map(render_track, tracks)
        stems = [outcome[1] for outcome in outcomes if outcome[0] == "ok"]
        failures = [
            {"track": outcome[1], "error": outcome[2]} for outcome in outcomes if outcome[0] == "failed"
        ]
        if not stems:
            detail = failures[0]["error"] if failures else "unknown error"
            raise ValueError(f"Every track failed to render: {detail}")
        correction = 0.0
        with store.claimed_path(
            output_filename or source.stem, f"{source.stem}_{audio_format}", overwrite, f".{audio_format}"
        ) as target:
            combined = target if audio_format == "wav" else Path(scratch) / "mix.wav"
            try:
                renderers.combine_stems(stems, combined)
                if normalize_peak_db is not None:
                    correction = audio.normalize_wav(combined, normalize_peak_db)
                if audio_format == "mp3":
                    singing.to_mp3(combined, target)
            except renderers.RenderError as exc:
                raise ValueError(str(exc)) from None
            except audio.AudioError as exc:
                raise ValueError(str(exc)) from None
    data = target.read_bytes()
    result = delivery.deliver_file(target.name, data, return_as, expires_in)
    result["mime_type"] = "audio/wav" if audio_format == "wav" else "audio/mpeg"
    result["source"] = source.name
    result["gain_correction_db"] = correction
    result["failed"] = failures or None
    if audio_format == "wav":
        peak = audio.wav_peak_db(target)
        result["peak_db"] = round(peak, 2)
        result["clipping"] = peak > -0.05
        result["duration_seconds"] = round(audio.wav_duration(target), 2)
    else:
        result["peak_db"] = None
        result["clipping"] = False
    # Stems are big (a minute of stereo wav is ~10 MB each), so with a public URL configured
    # they are always delivered as signed links: inlining ten of them can outsize any client's
    # tool timeout. The mix above still follows return_as.
    stem_return = "url" if delivery.public_url() else return_as
    result["stems"] = [
        {
            **delivery.deliver_file(stem.path.name, stem.path.read_bytes(), stem_return, expires_in),
            "mime_type": "audio/wav",
            "track": stem.track,
            "gain_db": stem.gain_db,
            "peak_db": round(audio.wav_peak_db(stem.path), 2),
        }
        for stem in stems
    ]
    return result


@mcp.resource("chords://types", mime_type="text/markdown")
def chord_types_resource() -> str:
    """Table of supported chord qualities and their formulas."""
    lines = [
        "| Quality | Symbols | Intervals (semitones) | Example | Notes in C |",
        "|---|---|---|---|---|",
    ]
    for row in _chord_type_rows():
        lines.append(
            f"| {row['quality']} | {', '.join(row['symbols'])} | {row['intervals_semitones']} | "
            f"{row['example']} | {' '.join(row['notes_in_C'])} |"
        )
    return "\n".join(lines)


def _diatonic_chords(key_text: str) -> dict[str, Any]:
    key = parse_key(key_text)
    notes = key.scale_notes()
    result: dict = {"key": key.name, "scale_notes": notes}
    if len(key.intervals) != 7:
        return result
    numerals = ["i", "ii", "iii", "iv", "v", "vi", "vii"]
    triads, sevenths = [], []
    for degree in range(7):
        for size, bucket in ((3, triads), (4, sevenths)):
            pcs = [(key.tonic + key.intervals[(degree + 2 * k) % 7]) % 12 for k in range(size)]
            found = identify_chord(set(pcs), pcs[0])
            if found is None:
                continue
            root, ct = found
            numeral = numerals[degree]
            if ct.intervals[1] == 4 or ct.name in ("aug", "aug7"):
                numeral = numeral.upper()
            suffix = {"dim": "°", "aug": "+", "dim7": "°7", "m7b5": "ø7", "aug7": "+7"}.get(ct.name)
            if suffix is None:
                suffix = {"maj": "", "min": "", "7": "7", "m7": "7", "maj7": "maj7", "mMaj7": "maj7"}.get(
                    ct.name, ct.aliases[0]
                )
            bucket.append(
                {
                    "numeral": numeral + suffix,
                    "chord": note_name(root, key.prefer_flats) + ct.aliases[0],
                }
            )
    result["triads"] = triads
    result["seventh_chords"] = sevenths
    return result


@mcp.resource("scales://{key}", mime_type="application/json")
def scale_resource(key: str) -> str:
    """Notes and diatonic chords of a key, e.g. scales://C-major, scales://A-minor, scales://F%23-dorian."""
    from urllib.parse import unquote

    return json.dumps(_diatonic_chords(unquote(key)), indent=2)


@mcp.resource("midi://{filename}", mime_type="audio/midi")
def midi_resource(filename: str) -> bytes:
    """The raw bytes of a generated MIDI file."""
    from urllib.parse import unquote

    return store.existing_path(unquote(filename)).read_bytes()


@mcp.prompt()
def compose_progression(mood: str, key: str = "", style: str = "", bars: str = "8") -> str:
    """Guide the assistant through composing a progression and saving it as MIDI."""
    key_line = f"Use the key {key}." if key else "Pick a key that suits the mood."
    style_line = f"Style/genre: {style}." if style else ""
    return f"""Compose a chord progression with this mood: {mood}. {key_line} {style_line}
Length: about {bars} bars.

Steps:
1. Read the resource scales://<key> (for example scales://A-minor) to see the chords that belong
   to the key.
2. Choose the progression. Explain in one or two sentences why it fits the mood.
3. Call create_progression_from_roman (or create_chord_progression with chord symbols).
   Choose a tempo, and pick 'voicing' (e.g. voice_leading: true) and 'rhythm'
   (block, arpeggio_up, strum, ...) that match the style.
4. Reply with the chord names, the file path, and one idea for a variation."""


@mcp.prompt()
def explain_progression(chords: str, key: str = "") -> str:
    """Ask the assistant to explain how a chord progression works."""
    key_line = f"in the key of {key}" if key else "(work out the most likely key first)"
    return f"""Explain the chord progression {chords} {key_line}.

Cover, in plain language:
1. The Roman numeral of each chord and its function (tonic, subdominant, dominant, borrowed...).
2. Why the progression sounds the way it does (tension and release, mood).
3. Well-known songs or styles that use something similar, if you are confident.
4. One or two variations to try. Offer to save them with create_chord_progression."""


def _configure_transport_security(public_url: str) -> None:
    """Allow the public URL's host in the DNS-rebinding check (localhost stays allowed)."""
    netloc = urlsplit(public_url).netloc
    if not netloc:
        return
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[netloc, f"{netloc}:*", "localhost:*", "127.0.0.1:*", "[::1]:*"],
        allowed_origins=[
            public_url,
            f"{public_url}:*",
            "http://localhost:*",
            "http://127.0.0.1:*",
            "http://[::1]:*",
        ],
    )


singing.register(mcp, lambda: store)
renderers.register(mcp, lambda: store)
sections.register(mcp, lambda: store)
renderjobs.register(mcp, lambda: store, _run_audio_job)


def main() -> None:
    parser = argparse.ArgumentParser(prog="chordsmith-mcp", description="ChordSmith MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.environ.get("CHORDSMITH_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("CHORDSMITH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CHORDSMITH_PORT", "8000")))
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    removed = store.remove_empty_media()
    if removed:
        logger.warning("Removed %d empty file(s) left by a crash: %s", len(removed), ", ".join(removed))
    if args.transport != "stdio":
        public_url = os.environ.get("CHORDSMITH_PUBLIC_URL", "").strip().rstrip("/")
        if public_url:
            _configure_transport_security(public_url)
        try:
            auth_config = AuthConfig.from_env(host=args.host, port=args.port)
        except ValueError as exc:
            parser.error(str(exc))
        if auth_config is None:
            logger.warning(
                "OAuth is disabled (CHORDSMITH_AUTH=off); anyone who can reach this server can use it."
            )
        else:
            enable_auth(mcp, auth_config)
    if args.transport == "streamable-http":
        import uvicorn

        from chordsmith.ratelimit import RateLimitMiddleware, TokenBucketLimiter, rate_limit_from_env

        try:
            limit = rate_limit_from_env()
        except ValueError as exc:
            parser.error(str(exc))
        app = mcp.streamable_http_app()
        if limit > 0:
            logger.info("Rate limit: %d requests per minute per client.", limit)
            app = RateLimitMiddleware(app, TokenBucketLimiter(limit), mcp.settings.streamable_http_path)
        uvicorn.run(app, host=args.host, port=args.port, log_level=mcp.settings.log_level.lower())
        return
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
