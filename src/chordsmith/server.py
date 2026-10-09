"""ChordSmith MCP server: tools, resources and prompts."""

from __future__ import annotations

import argparse
import json
import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from chordsmith import __version__
from chordsmith.analysis import analyze_file
from chordsmith.midi_writer import transpose_file, write_progression
from chordsmith.models import ChordEvent, NumeralEvent, Rhythm, Voicing
from chordsmith.storage import FileStore
from chordsmith.theory import (
    CHORD_TYPES,
    Chord,
    identify_chord,
    note_name,
    parse_chord,
    parse_key,
    roman_to_chord,
    split_numerals,
)

INSTRUCTIONS = """ChordSmith writes chord progressions to MIDI (.mid) files.
You choose the harmony; ChordSmith turns it into notes and saves the file.
Typical flow: (optional) read scales://{key} for the diatonic chords of a key ->
create_chord_progression or create_progression_from_roman -> tell the user the file path.
Use the 'voicing' and 'rhythm' options for inversions, voice leading, arpeggios or strumming."""

mcp = FastMCP("ChordSmith", instructions=INSTRUCTIONS)
store = FileStore()

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
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
