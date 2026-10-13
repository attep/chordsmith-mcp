"""Guitar Pro / MusicXML tab conversion to MIDI (MuseScore: local or on the host bridge)."""

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
TAB_TIMEOUT_SECONDS = 180


class TabError(ValueError):
    """Raised when a tab cannot be converted (no converter, bad file, MuseScore failed)."""


def find_musescore() -> str | None:
    """The local MuseScore executable (CHORDSMITH_MUSESCORE), when one is configured."""
    return os.environ.get("CHORDSMITH_MUSESCORE", "").strip() or None


def convert_tab(data: bytes, suffix: str) -> tuple[bytes, str]:
    """Convert tab bytes to MIDI; returns ``(midi_bytes, converter)``.

    A local MuseScore (``CHORDSMITH_MUSESCORE``) wins; else the host bridge
    (``CHORDSMITH_VST_BRIDGE_URL``, worker started with ``--musescore``). Both run the same
    MuseScore CLI conversion, so a server without MuseScore can still import tabs.
    """
    musescore = find_musescore()
    if musescore:
        return _convert_local(data, suffix, musescore), f"local MuseScore ({Path(musescore).name})"
    url = os.environ.get("CHORDSMITH_VST_BRIDGE_URL", "").strip().rstrip("/")
    if url:
        return _convert_bridge(data, suffix, url), "host bridge"
    raise TabError(
        "No tab converter is configured. Install MuseScore and set CHORDSMITH_MUSESCORE to its "
        "executable, or run the host worker (scripts/vst_bridge.py) with --musescore and set "
        "CHORDSMITH_VST_BRIDGE_URL on this server."
    )


def _validate_midi(data: bytes) -> None:
    try:
        mido.MidiFile(file=io.BytesIO(data))
    except Exception:
        raise TabError("The converter's output is not a readable MIDI file.") from None


def _convert_local(data: bytes, suffix: str, executable: str) -> bytes:
    if not Path(executable).is_file():
        raise TabError(f"CHORDSMITH_MUSESCORE points to '{executable}', which does not exist.")
    scratch = tempfile.mkdtemp(prefix="cs-tab-")
    try:
        source = Path(scratch) / f"input{suffix}"
        target = Path(scratch) / "output.mid"
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
            tail = (process.stderr or process.stdout or "").strip()[-400:]
            raise TabError("MuseScore produced no MIDI" + (f": {tail}" if tail else "."))
        midi = target.read_bytes()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    _validate_midi(midi)
    return midi


def _convert_bridge(data: bytes, suffix: str, url: str) -> bytes:
    import httpx

    token = os.environ.get("CHORDSMITH_VST_BRIDGE_TOKEN", "")
    headers = {"X-Chordsmith-Token": token} if token else {}
    payload = {"name": f"input{suffix}", "data": base64.b64encode(data).decode()}
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
    encoded = result.get("midi")
    if not encoded:
        detail = result.get("error") or str(result.get("stderr") or "").strip()[-400:] or "no MIDI returned"
        raise TabError(f"Tab conversion failed on the host bridge: {detail}")
    try:
        midi = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise TabError("The host bridge returned invalid base64.") from None
    _validate_midi(midi)
    return midi
