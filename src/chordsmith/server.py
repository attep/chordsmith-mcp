"""ChordSmith MCP server: tools, resources and prompts."""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from chordsmith import __version__, audio, delivery, singing
from chordsmith.analysis import analyze_file
from chordsmith.auth import AuthConfig, enable_auth
from chordsmith.midi_writer import add_track as add_track_midi
from chordsmith.midi_writer import transpose_file, write_progression
from chordsmith.models import ChordEvent, Humanize, NoteInput, NumeralEvent, Rhythm, Voicing
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

INSTRUCTIONS = """ChordSmith writes chord progressions to MIDI (.mid) files.
You choose the harmony; ChordSmith turns it into notes and saves the file.
Typical flow: (optional) read scales://{key} for the diatonic chords of a key ->
create_chord_progression or create_progression_from_roman -> tell the user the file path.
Use the 'voicing' and 'rhythm' options for inversions, voice leading, arpeggios or strumming.
get_midi_file hands the actual file to tool-only clients; add_track adds melodies and other
tracks; render_audio turns a file into wav/mp3 so it can be heard without a music app."""

logger = logging.getLogger(__name__)

mcp = FastMCP("ChordSmith", instructions=INSTRUCTIONS)
store = FileStore()

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


@mcp.custom_route("/healthz", methods=["GET"])
async def health_check(_request: Request) -> Response:
    """Liveness probe for container platforms."""
    return JSONResponse({"status": "ok"})


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
Overwrite = Annotated[bool, Field(description="Replace an existing file with the same name.")]


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
    path = store.new_path(filename, default_stem, overwrite)
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


@mcp.tool()
def create_chord_progression(
    chords: ChordList,
    filename: FileName = None,
    tempo: Tempo = 100,
    time_signature: TimeSignature = "4/4",
    beats_per_chord: BeatsPerChord = None,
    repeat: Repeat = 1,
    instrument: Instrument = 0,
    voicing: Voicing | None = None,
    rhythm: Rhythm | None = None,
    preset: Preset = None,
    midi_type: MidiType = 1,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Write chord symbols (e.g. ["Am", "F", "C", "G"]) to a MIDI file.

    Supports slash chords (C/E) and many qualities (see list_chord_types). Returns the file path,
    the notes used for every chord and the length in seconds.
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


@mcp.tool()
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
    voicing: Voicing | None = None,
    rhythm: Rhythm | None = None,
    preset: Preset = None,
    midi_type: MidiType = 1,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Write a Roman-numeral progression in a given key (e.g. i-VI-III-VII in A minor) to a MIDI file.

    The response includes the resolved chord symbols, e.g. Am, F, C, G.
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


@mcp.tool()
def list_chord_types() -> list[dict[str, Any]]:
    """List every supported chord quality with the symbols you can write and its notes in C."""
    return _chord_type_rows()


@mcp.tool()
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
) -> dict[str, Any]:
    """Transpose a MIDI file by semitones, or from one key to another. Drums (channel 10) are untouched.

    Writes a new file; the original is kept.
    """
    source = store.existing_path(filename)
    if semitones is None:
        if not (from_key and to_key):
            raise ValueError("Give either 'semitones' or both 'from_key' and 'to_key'.")
        shift = (parse_key(to_key).tonic - parse_key(from_key).tonic) % 12
        semitones = shift - 12 if shift > 6 else shift
    direction = f"up{semitones}" if semitones >= 0 else f"down{-semitones}"
    target = store.new_path(output_filename, f"{source.stem}_{direction}", overwrite)
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


@mcp.tool()
def analyze_midi(
    filename: Annotated[str, Field(description="File in the output folder to analyse.")],
    window_beats: Annotated[
        float | None,
        Field(gt=0, le=64, description="Analysis window in beats. Default: one bar."),
    ] = None,
) -> dict[str, Any]:
    """Guess the chord in each bar (or window) of a MIDI file.

    Works best on files with one chord per window (like files ChordSmith creates). Returns
    tempo, time signature, markers and the detected progression.
    """
    return analyze_file(store.existing_path(filename), window_beats)


@mcp.tool()
def list_generated_files() -> dict[str, Any]:
    """List the MIDI files in the ChordSmith output folder (newest first)."""
    root = store.ensure_root()
    return {"output_dir": str(root), "files": store.list_files()}


ReturnAs = delivery.ReturnAs


@mcp.tool()
def get_midi_file(
    filename: Annotated[str, Field(description="File in the output folder, e.g. 'melancholy.mid'.")],
    return_as: ReturnAs = "base64",
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Return the MIDI file itself so tool-only clients can hand it to the user.

    Use return_as='base64' for the bytes inline, or return_as='url' for a signed link that can
    be opened without the OAuth login and expires after expires_in seconds.
    """
    path = store.existing_path(filename)
    data = path.read_bytes()
    result = delivery.deliver_file(path.name, data, return_as, expires_in)
    result["mime_type"] = "audio/midi"
    return result


@mcp.tool()
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
    output_filename: FileName = None,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Add a note-level track (melody, bass, drums, ...) to a copy of an existing MIDI file.

    The original file is never modified. The copy keeps all existing tracks and gets the new one
    on the chosen channel; a type 0 file is promoted to type 1.
    """
    source = store.existing_path(filename)
    target = store.new_path(output_filename, f"{source.stem}_{track_name}", overwrite)
    if target == source:
        raise ValueError("Choose a different output_filename; the original file is never modified.")
    parsed: list[tuple[int, float, float, int]] = []
    for note in notes:
        try:
            pitch = parse_pitch(note.pitch)
        except MusicTheoryError as exc:
            raise ValueError(str(exc)) from None
        parsed.append((pitch, note.start_beat, note.beats, note.velocity))
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


@mcp.tool()
def delete_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to delete.")],
) -> dict[str, Any]:
    """Delete a MIDI file from the output folder."""
    path = store.remove(filename)
    return {"deleted": path.name}


@mcp.tool()
def rename_midi_file(
    filename: Annotated[str, Field(description="File in the output folder to rename.")],
    new_name: Annotated[str, Field(description="New name, e.g. 'lofi_sketch'. '.mid' is added.")],
) -> dict[str, Any]:
    """Rename a MIDI file. Refuses to overwrite an existing file."""
    target = store.rename(filename, new_name)
    return {"filename": target.name, "path": str(target), "uri": f"midi://{target.name}"}


@mcp.tool()
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
    output_filename: FileName = None,
    expires_in: Annotated[
        int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
    ] = delivery.DEFAULT_TTL_SECONDS,
    overwrite: Overwrite = False,
) -> dict[str, Any]:
    """Render a MIDI file to audio (wav or mp3) with FluidSynth so it can be heard without a music app.

    The audio file is written next to the MIDI files and returned like get_midi_file
    (base64 bytes or a signed, expiring download URL).
    """
    source = store.existing_path(filename)
    target = store.new_path(
        output_filename or source.stem, f"{source.stem}_{format}", overwrite, f".{format}"
    )
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
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
