"""MuseScore conversions: Guitar Pro / MusicXML tabs to MIDI, and MIDI to MusicXML scores.

The conversion runs with MuseScore — locally (``CHORDSMITH_MUSESCORE``) or on the host worker
(``scripts/vst_bridge.py`` started with ``--musescore``, reached through
``CHORDSMITH_VST_BRIDGE_URL``) — so a server without MuseScore can still import tabs and
export scores.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import mido

TAB_SUFFIXES = {".gp", ".gp3", ".gp4", ".gp5", ".gpx", ".musicxml", ".xml", ".mxl", ".mscz"}
SCORE_SUFFIXES = {".musicxml", ".mxl"}
TAB_TIMEOUT_SECONDS = 180


class TabError(ValueError):
    """Raised when a conversion fails (no converter, bad file, MuseScore failed)."""


def find_musescore() -> str | None:
    """The local MuseScore executable (CHORDSMITH_MUSESCORE), when one is configured."""
    return os.environ.get("CHORDSMITH_MUSESCORE", "").strip() or None


def convert_tab(data: bytes, suffix: str) -> tuple[bytes, str]:
    """Convert tab bytes to MIDI; returns ``(midi_bytes, converter)``."""
    return convert_document(data, suffix, ".mid")


def convert_midi_to_score(data: bytes, output_suffix: str) -> tuple[bytes, str]:
    """Convert MIDI bytes to a MusicXML score; returns ``(score_bytes, converter)``."""
    return convert_document(data, ".mid", output_suffix)


def convert_document(data: bytes, input_suffix: str, output_suffix: str) -> tuple[bytes, str]:
    """Convert document bytes with MuseScore; returns ``(output_bytes, converter)``.

    A local MuseScore (``CHORDSMITH_MUSESCORE``) wins; else the host bridge
    (``CHORDSMITH_VST_BRIDGE_URL``, worker started with ``--musescore``). Both run the same
    MuseScore CLI conversion, so a server without MuseScore can still convert.
    """
    musescore = find_musescore()
    if musescore:
        converter = f"local MuseScore ({Path(musescore).name})"
        return _convert_local(data, input_suffix, output_suffix, musescore), converter
    url = os.environ.get("CHORDSMITH_VST_BRIDGE_URL", "").strip().rstrip("/")
    if url:
        return _convert_bridge(data, input_suffix, output_suffix, url), "host bridge"
    raise TabError(
        "No MusicXML converter is configured. Install MuseScore and set CHORDSMITH_MUSESCORE "
        "to its executable, or run the host worker (scripts/vst_bridge.py) with --musescore "
        "and set CHORDSMITH_VST_BRIDGE_URL on this server."
    )


def _validate_output(data: bytes, output_suffix: str) -> None:
    if output_suffix in (".mid", ".midi"):
        try:
            mido.MidiFile(file=io.BytesIO(data))
        except Exception:
            raise TabError("The converter's output is not a readable MIDI file.") from None
        return
    if output_suffix == ".mxl":
        if data[:2] != b"PK":
            raise TabError("The converter's output is not a readable MusicXML file.")
        return
    head = data[:200]
    if b"<?xml" not in head and b"<score" not in head:
        raise TabError("The converter's output is not a readable MusicXML file.")


def _failure_detail(process: subprocess.CompletedProcess) -> str:
    return (process.stderr or process.stdout or "").strip()[-400:]


def _convert_local(data: bytes, input_suffix: str, output_suffix: str, executable: str) -> bytes:
    if not Path(executable).is_file():
        raise TabError(f"CHORDSMITH_MUSESCORE points to '{executable}', which does not exist.")
    scratch = tempfile.mkdtemp(prefix="cs-convert-")
    try:
        source = Path(scratch) / f"input{input_suffix}"
        target = Path(scratch) / f"output{output_suffix}"
        source.write_bytes(data)
        try:
            process = subprocess.run(
                [executable, "-o", str(target), str(source)],
                capture_output=True,
                text=True,
                timeout=TAB_TIMEOUT_SECONDS,
                cwd=scratch,
            )
        except subprocess.TimeoutExpired:
            raise TabError(f"MuseScore timed out after {TAB_TIMEOUT_SECONDS}s.") from None
        except OSError as exc:
            raise TabError(f"Could not run MuseScore ({executable}): {exc}") from None
        if not target.is_file():
            kind = "MIDI" if output_suffix in (".mid", ".midi") else "score"
            tail = _failure_detail(process)
            raise TabError(f"MuseScore produced no {kind}" + (f": {tail}" if tail else "."))
        output = target.read_bytes()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    _validate_output(output, output_suffix)
    return output


def _convert_bridge(data: bytes, input_suffix: str, output_suffix: str, url: str) -> bytes:
    import httpx

    token = os.environ.get("CHORDSMITH_VST_BRIDGE_TOKEN", "")
    headers = {"X-Chordsmith-Token": token} if token else {}
    payload = {
        "name": f"input{input_suffix}",
        "data": base64.b64encode(data).decode(),
        "target": output_suffix,
    }
    try:
        response = httpx.post(f"{url}/tab", json=payload, headers=headers, timeout=TAB_TIMEOUT_SECONDS + 30)
    except httpx.HTTPError as exc:
        raise TabError(f"The host bridge at {url} is unreachable: {exc}") from None
    if response.status_code != 200:
        raise TabError(
            f"The host bridge at {url} answered HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        result = response.json()
    except ValueError:
        raise TabError("The host bridge answered with non-JSON.") from None
    encoded = result.get("output") or result.get("midi")
    if not encoded:
        detail = result.get("error") or str(result.get("stderr") or "").strip()[-400:] or "no output returned"
        raise TabError(f"Conversion failed on the host bridge: {detail}")
    try:
        output = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise TabError("The host bridge returned invalid base64.") from None
    _validate_output(output, output_suffix)
    return output
