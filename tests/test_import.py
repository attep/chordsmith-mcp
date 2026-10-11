"""Tests for import_midi: verbatim storage and the structured report."""

from __future__ import annotations

import base64
import hashlib
import io

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _make_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=6, denominator=8, time=0))
    conductor.append(mido.MetaMessage("marker", text="A", time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=480 * 8))
    lead = mido.MidiTrack()
    lead.append(mido.MetaMessage("track_name", name="Lead", time=0))
    lead.append(mido.Message("program_change", program=81, channel=0, time=0))
    lead.append(mido.Message("note_on", note=60, velocity=90, channel=0, time=0))
    lead.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
    lead.append(mido.Message("note_on", note=62, velocity=90, channel=0, time=480))
    lead.append(mido.Message("note_off", note=62, velocity=0, channel=0, time=480))
    lead.append(mido.MetaMessage("end_of_track", time=0))
    drums = mido.MidiTrack()
    drums.append(mido.MetaMessage("track_name", name="Drums", time=0))
    drums.append(mido.Message("sysex", data=(1, 2, 3), time=0))
    drums.append(mido.Message("note_on", note=36, velocity=100, channel=9, time=0))
    drums.append(mido.Message("note_off", note=36, velocity=0, channel=9, time=120))
    drums.append(mido.MetaMessage("end_of_track", time=480 * 8))
    midi.tracks.extend([conductor, lead, drums])
    buffer = io.BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def _make_type0_bytes() -> bytes:
    midi = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Everything", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    track.append(mido.Message("note_on", note=60, velocity=80, channel=0, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.tracks.append(track)
    buffer = io.BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


async def test_import_midi_stores_verbatim_and_reports(store):
    data = _make_midi_bytes()
    result = await _call(
        "import_midi", {"filename": "sketch.mid", "data_base64": base64.b64encode(data).decode()}
    )
    assert not result.isError, result.content[0].text
    report = result.structuredContent
    assert report["filename"] == "sketch.mid"
    assert report["sha256"] == hashlib.sha256(data).hexdigest()
    assert report["size_bytes"] == len(data)
    assert report["midi_type"] == 1
    assert report["total_notes"] == 3
    tracks = {entry["name"]: entry for entry in report["tracks"]}
    assert tracks["Lead"]["channel"] == 1
    assert tracks["Lead"]["program"] == 81
    assert tracks["Lead"]["notes"] == 2
    assert tracks["Drums"]["channel"] == 10 and tracks["Drums"]["notes"] == 1
    assert report["tempo_changes"] == [{"beat": 0.0, "bpm": 90.0}]
    assert report["time_signature_changes"] == [{"beat": 0.0, "time_signature": "6/8"}]
    assert report["markers"] == [{"beat": 0.0, "text": "A"}]
    assert report["sysex_events"] == 1
    assert any("SysEx" in warning for warning in report["warnings"])

    # the stored bytes are identical (a true round trip)
    fetched = (await _call("get_midi_file", {"filename": "sketch.mid"})).structuredContent
    assert base64.b64decode(fetched["data_base64"]) == data
    # and the file works with the normal workflow
    analysis = await _call("analyze_midi", {"filename": "sketch.mid"})
    assert not analysis.isError
    assert analysis.structuredContent["tempo_bpm"] == 90.0


async def test_import_type0_warns_about_the_layout(store):
    data = _make_type0_bytes()
    report = (
        await _call("import_midi", {"filename": "flat.mid", "data_base64": base64.b64encode(data).decode()})
    ).structuredContent
    assert report["midi_type"] == 0
    assert any("Type 0" in warning for warning in report["warnings"])


async def test_import_midi_errors(store):
    bad_base64 = await _call("import_midi", {"filename": "x.mid", "data_base64": "not base64!!"})
    assert bad_base64.isError and "not valid base64" in bad_base64.content[0].text
    not_midi = await _call(
        "import_midi", {"filename": "x.mid", "data_base64": base64.b64encode(b"hello world").decode()}
    )
    assert not_midi.isError and "not a readable MIDI" in not_midi.content[0].text


async def test_import_midi_numbers_duplicate_names(store):
    data = base64.b64encode(_make_midi_bytes()).decode()
    first = await _call("import_midi", {"filename": "same.mid", "data_base64": data})
    second = await _call("import_midi", {"filename": "same.mid", "data_base64": data})
    assert first.structuredContent["filename"] == "same.mid"
    assert second.structuredContent["filename"] == "same_2.mid"
