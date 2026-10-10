"""ChordSmith MCP server: tools, resources and prompts."""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
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

from chordsmith import __version__, audio, delivery, renderers, results, singing
from chordsmith.analysis import analyze_file
from chordsmith.auth import AuthConfig, enable_auth
from chordsmith.midi_writer import add_track as add_track_midi
from chordsmith.midi_writer import transpose_file, write_progression
from chordsmith.models import ChordEvent, Humanize, LoopSpec, NoteInput, NumeralEvent, Rhythm, Voicing
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

    Creates a new file (the original is kept) and returns its name/path plus how many notes
    moved. Give either 'semitones' or both 'from_key' and 'to_key'; with keys, the smallest move
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
def list_generated_files() -> results.ListFilesResult:
    """Return the MIDI files in the output folder (newest first) and the folder path.

    Read-only; use it to find file names for get_midi_file, analyze_midi, transpose_midi or the
    singing tools.
    """
    root = store.ensure_root()
    return {"output_dir": str(root), "files": store.list_files()}


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


@mcp.tool(title="Add track", annotations=CREATES)
def add_track(
    filename: Annotated[str, Field(description="Existing file to copy and extend.")],
    track_name: Annotated[str, Field(description="Name of the new track, e.g. 'Melody'.")],
    notes: Annotated[
        list[NoteInput],
        Field(
            min_length=1,
            max_length=5000,
            description='Notes, e.g. [{"pitch": "E6", "start_beat": 0, "beats": 0.5, "velocity": 80}]. '
            "Pitch is a note name with octave or a MIDI number 0-127.",
        ),
    ],
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
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> results.AddTrackResult:
    """Add a note-level track (melody, bass, drums, ...) to a copy of an existing MIDI file.

    Creates a new file; the original is never modified. The copy keeps all existing tracks and
    gets the new one on the chosen channel (a type 0 file is promoted to type 1). Returns the new
    file's name/path and how many notes/tracks it has. Pitches outside 0-127 and overlapping
    notes are fine; the track is as monophonic or polyphonic as you write it. With ``loop``, a
    short pattern (for example one bar of drums) is tiled across the song in one call.
    """
    source = store.existing_path(filename)
    with store.claimed_path(output_filename, f"{source.stem}_{track_name}", overwrite) as target:
        if target == source:
            raise ValueError("Choose a different output_filename; the original file is never modified.")
        parsed: list[tuple[int, float, float, int]] = []
        for note in notes:
            try:
                pitch = parse_pitch(note.pitch)
            except MusicTheoryError as exc:
                raise ValueError(str(exc)) from None
            parsed.append((pitch, note.start_beat, note.beats, note.velocity))
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
        info = add_track_midi(
            source,
            target,
            track_name=track_name,
            program=instrument,
            channel=channel - 1,
            notes=parsed,
        )
    return {
        "source": source.name,
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "track_name": track_name,
        "channel": channel,
        **info,
    }


@mcp.tool(title="Delete MIDI file", annotations=DESTRUCTIVE_IDEMPOTENT)
def delete_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to delete.")],
) -> results.DeleteResult:
    """Delete a MIDI file from the output folder.

    Removes the file permanently (and its per-track instrument sidecar, if any) and returns its
    name; use list_generated_files to confirm the name first. Fails with a clear message when
    the file does not exist.
    """
    path = store.remove(filename)
    renderers.specs_path(path).unlink(missing_ok=True)
    return {"deleted": path.name}


@mcp.tool(title="Rename MIDI file", annotations=DESTRUCTIVE)
def rename_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to rename.")],
    new_name: Annotated[str, Field(description="New name, e.g. 'lofi_sketch'. '.mid' is added.")],
) -> results.RenameResult:
    """Rename a MIDI file. Refuses to overwrite an existing file.

    Changes the file's name in place and returns the new name/path. If the target name exists,
    the call fails and nothing changes. A per-track instrument sidecar (set_track_instrument)
    moves with the file.
    """
    source = store.existing_path(filename)
    target = store.rename(filename, new_name)
    sidecar = renderers.specs_path(source)
    if sidecar.is_file():
        sidecar.replace(renderers.specs_path(target))
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
            "URLs when a public URL is configured, else like return_as."
        ),
    ] = False,
    output_filename: FileName = None,
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
    overwrite: Overwrite = False,
) -> results.RenderAudioResult:
    """Render a MIDI file to audio (wav or mp3) with FluidSynth so it can be heard without a music app.

    Creates an audio file next to the MIDI files and returns it like get_midi_file (base64 bytes
    or a signed, expiring URL), plus the source name and, for wav, the duration in seconds. MP3
    needs ffmpeg; FluidSynth and a soundfont are required (both are in the Docker image). With
    ``stems``, each track is rendered on its own (per-track soundfont/gain from
    set_track_instrument) and the stems are combined into the mix; the result then also lists
    every stem. On failure a clear error explains what is missing and no file is left behind.
    """
    source = store.existing_path(filename)
    if stems:
        return _render_stems(source, format, return_as, expires_in, output_filename, overwrite, soundfont)
    with store.claimed_path(
        output_filename or source.stem, f"{source.stem}_{format}", overwrite, f".{format}"
    ) as target:
        try:
            audio.render(source, target, format, soundfont)
        except audio.AudioError as exc:
            raise ValueError(str(exc)) from None
    data = target.read_bytes()
    result = delivery.deliver_file(target.name, data, return_as, expires_in)
    result["mime_type"] = "audio/wav" if format == "wav" else "audio/mpeg"
    result["source"] = source.name
    if format == "wav":
        result["duration_seconds"] = round(audio.wav_duration(target), 2)
    return result


def _render_stems(
    source: Path,
    audio_format: str,
    return_as: str,
    expires_in: int,
    output_filename: str | None,
    overwrite: bool,
    soundfont: str | None,
) -> dict[str, Any]:
    """Render every note track on its own and combine the stems into the mix."""
    midi = mido.MidiFile(source)
    specs = renderers.load_specs(source)
    names = renderers.track_names(midi)
    tracks = renderers.note_tracks(midi)
    if not tracks:
        raise ValueError("This file has no note tracks to render.")
    with tempfile.TemporaryDirectory() as scratch:

        def render_track(index: int) -> renderers.Stem:
            name = names[index]
            stem_midi = Path(scratch) / f"stem_{index}.mid"
            singing.extract_track(source, stem_midi, index)
            spec = specs.get(name) or renderers.InstrumentSpec(preset=soundfont)
            renderer = renderers.get_renderer(spec.engine)
            with store.claimed_path(None, f"{source.stem}_{name}", extension=".wav") as stem_wav:
                try:
                    renderer.render(stem_midi, spec, stem_wav)
                except renderers.RenderError as exc:
                    raise ValueError(f"Track '{name}': {exc}") from None
                return renderers.Stem(track=name, path=stem_wav, gain_db=spec.gain_db)

        stems = renderers.parallel_map(render_track, tracks)
        with store.claimed_path(
            output_filename or source.stem, f"{source.stem}_{audio_format}", overwrite, f".{audio_format}"
        ) as target:
            combined = target if audio_format == "wav" else Path(scratch) / "mix.wav"
            try:
                renderers.combine_stems(stems, combined)
                if audio_format == "mp3":
                    singing.to_mp3(combined, target)
            except renderers.RenderError as exc:
                raise ValueError(str(exc)) from None
    data = target.read_bytes()
    result = delivery.deliver_file(target.name, data, return_as, expires_in)
    result["mime_type"] = "audio/wav" if audio_format == "wav" else "audio/mpeg"
    result["source"] = source.name
    if audio_format == "wav":
        result["duration_seconds"] = round(audio.wav_duration(target), 2)
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
