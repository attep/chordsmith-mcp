"""Tests for import_tab: tab conversion routing (local MuseScore stub / fake host bridge)."""

from __future__ import annotations

import base64
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import server, tabs

pytestmark = pytest.mark.anyio

TOKEN = "tab-token"


def _fixture_midi() -> bytes:
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name="Guitar", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    for index, pitch in enumerate((60, 64, 67)):
        track.append(mido.Message("note_on", note=pitch, velocity=80, time=0 if index == 0 else 480))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    buffer = io.BytesIO()
    mid.save(file=buffer)
    return buffer.getvalue()


FIXTURE = _fixture_midi()


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_stub(tmp_path: Path, name: str, body_posix: str, body_windows: str) -> str:
    """A fake 'MuseScore' executable: args are -o <out.mid> <in>; $2 / %2 is the output."""
    if os.name == "nt":
        stub = tmp_path / f"{name}.bat"
        stub.write_text("@echo off\r\n" + body_windows + "\r\n")
    else:
        stub = tmp_path / f"{name}.sh"
        stub.write_text("#!/bin/sh\n" + body_posix + "\n")
        stub.chmod(0o755)
    return str(stub)


@pytest.fixture
def no_converter(monkeypatch):
    monkeypatch.delenv("CHORDSMITH_MUSESCORE", raising=False)
    monkeypatch.delenv("CHORDSMITH_VST_BRIDGE_URL", raising=False)
    monkeypatch.delenv("CHORDSMITH_VST_BRIDGE_TOKEN", raising=False)


async def test_wrong_extension_is_rejected(store, no_converter):
    result = await _call("import_tab", {"filename": "song.mid", "data_base64": "AAAA"})
    assert result.isError and "import_midi" in result.content[0].text


async def test_no_converter_configured_names_both_setups(store, no_converter):
    result = await _call("import_tab", {"filename": "riff.gpx", "data_base64": "AAAA"})
    assert result.isError
    text = result.content[0].text
    assert "CHORDSMITH_MUSESCORE" in text and "vst_bridge" in text


async def test_local_converter_end_to_end(store, monkeypatch, tmp_path):
    (tmp_path / "fixture.mid").write_bytes(FIXTURE)
    stub = _write_stub(
        tmp_path,
        "stub",
        'cp "$(dirname "$0")/fixture.mid" "$2"',
        'copy /y "%~dp0fixture.mid" "%~2" >nul',
    )
    monkeypatch.setenv("CHORDSMITH_MUSESCORE", stub)
    monkeypatch.delenv("CHORDSMITH_VST_BRIDGE_URL", raising=False)

    result = await _call(
        "import_tab", {"filename": "shattered.gpx", "data_base64": base64.b64encode(b"BCFZtab").decode()}
    )
    assert not result.isError, result.content[0].text
    payload = result.structuredContent
    assert payload["filename"] == "shattered.mid"
    assert payload["converted_from"] == "shattered.gpx"
    assert payload["converted_format"] == ".gpx"
    assert payload["converter"].startswith("local MuseScore")
    assert payload["total_notes"] == 3
    assert payload["tracks"][0]["name"] == "Guitar"

    analyzed = await _call("analyze_midi", {"filename": "shattered.mid"})
    assert not analyzed.isError


async def test_local_converter_without_output_fails_clearly(store, monkeypatch, tmp_path):
    stub = _write_stub(tmp_path, "silent", "exit 0", "exit /b 0")
    monkeypatch.setenv("CHORDSMITH_MUSESCORE", stub)
    monkeypatch.delenv("CHORDSMITH_VST_BRIDGE_URL", raising=False)
    result = await _call("import_tab", {"filename": "riff.gp5", "data_base64": "AAAA"})
    assert result.isError and "produced no MIDI" in result.content[0].text


async def test_local_converter_garbage_output_is_rejected(store, monkeypatch, tmp_path):
    stub = _write_stub(
        tmp_path,
        "garbage",
        'printf "not a midi" > "$2"',
        'echo not a midi > "%~2"',
    )
    monkeypatch.setenv("CHORDSMITH_MUSESCORE", stub)
    monkeypatch.delenv("CHORDSMITH_VST_BRIDGE_URL", raising=False)
    result = await _call("import_tab", {"filename": "riff.gpx", "data_base64": "AAAA"})
    assert result.isError and "not a readable MIDI" in result.content[0].text


class _FakeTabBridge(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.headers.get("X-Chordsmith-Token") != TOKEN:
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/tab":
            if payload.get("name") == "input.gpx":
                self._json(200, {"exit": -1, "midi": None, "error": "no MuseScore configured on this bridge"})
            else:
                self._json(
                    200,
                    {
                        "exit": 0,
                        "midi": base64.b64encode(FIXTURE).decode(),
                        "stdout": "",
                        "stderr": "",
                    },
                )
        else:
            self._json(404, {"error": "not found"})


@pytest.fixture
def fake_tab_bridge(monkeypatch):
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), _FakeTabBridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_URL", f"http://127.0.0.1:{bridge.server_address[1]}")
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_TOKEN", TOKEN)
    monkeypatch.delenv("CHORDSMITH_MUSESCORE", raising=False)
    yield
    bridge.shutdown()


async def test_bridge_conversion(store, fake_tab_bridge):
    result = await _call("import_tab", {"filename": "riff.gp5", "data_base64": "AAAA"})
    assert not result.isError, result.content[0].text
    payload = result.structuredContent
    assert payload["converter"] == "host bridge"
    assert payload["filename"] == "riff.mid"
    assert payload["total_notes"] == 3


async def test_bridge_failure_is_reported(store, fake_tab_bridge):
    result = await _call("import_tab", {"filename": "fail.gpx", "data_base64": "AAAA"})
    assert result.isError and "no MuseScore configured on this bridge" in result.content[0].text


async def test_bridge_token_rejected(store, fake_tab_bridge, monkeypatch):
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_TOKEN", "wrong")
    result = await _call("import_tab", {"filename": "riff.gpx", "data_base64": "AAAA"})
    assert result.isError and "401" in result.content[0].text


def test_tab_suffixes_are_the_documented_set():
    assert {".gp", ".gp3", ".gp4", ".gp5", ".gpx"} <= tabs.TAB_SUFFIXES
    assert {".musicxml", ".xml", ".mxl", ".mscz"} <= tabs.TAB_SUFFIXES
    assert ".mid" not in tabs.TAB_SUFFIXES
