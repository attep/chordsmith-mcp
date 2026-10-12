"""Tests for the sfizz engine (sampled .sfz instruments rendered as stems).

The engine tests skip when sfizz_render is not installed (local dev); the container image
builds it, so CI runs them for real. The SFZ instrument here is generated on the fly — a
tiny sine sample and a one-line region — so no third-party samples are needed.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, server

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(audio.find_sfizz_render() is None, reason="sfizz_render is not installed"),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_sfz(tmp_path: Path) -> str:
    """A one-region SFZ instrument over a generated stereo sine (no licences involved)."""
    rate = 44100
    frames = array.array("h")
    for index in range(rate // 4):
        value = int(16000 * math.sin(2 * math.pi * 220.0 * index / rate))
        frames.append(value)
        frames.append(value)
    with wave.open(str(tmp_path / "tone.wav"), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())
    sfz = tmp_path / "tone.sfz"
    sfz.write_text("<region> sample=tone.wav pitch_keycenter=60 lokey=0 hikey=127\n", encoding="utf-8")
    return str(sfz)


async def _make_song(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F"], "filename": "song", "tempo": 120},
    )
    song = result.structuredContent["filename"]
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Melody",
            "notes": [
                {"pitch": "C5", "start_beat": 0, "beats": 1, "velocity": 90},
                {"pitch": "E5", "start_beat": 1, "beats": 1, "velocity": 90},
                {"pitch": "G5", "start_beat": 2, "beats": 1, "velocity": 90},
                {"pitch": "C5", "start_beat": 3, "beats": 1, "velocity": 90},
            ],
        },
    )
    assert not added.isError, added.content[0].text
    return added.structuredContent["filename"]


async def test_sfizz_stems_render_mixed_engines(store, tmp_path):
    song = await _make_song(store)
    sfz = _write_sfz(tmp_path)
    set_spec = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz, "gain_db": -2}}},
    )
    assert not set_spec.isError, set_spec.content[0].text
    rendered = await _call(
        "render_audio",
        {"filename": song, "stems": True, "normalize_peak_db": -1.0},
    )
    assert not rendered.isError, rendered.content[0].text
    content = rendered.structuredContent
    assert content["peak_db"] is not None and content["peak_db"] <= -0.9
    assert content["duration_seconds"] > 1.0
    assert content["failed"] is None


async def test_sfizz_single_pass_is_refused(store, tmp_path):
    song = await _make_song(store)
    sfz = _write_sfz(tmp_path)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz}}},
    )
    rendered = await _call("render_audio", {"filename": song, "format": "wav"})
    assert rendered.isError
    assert "stems=true" in rendered.content[0].text


async def test_set_track_instrument_sfizz_validation(store, tmp_path):
    song = await _make_song(store)
    sfz = _write_sfz(tmp_path)

    missing = await _call(
        "set_track_instrument", {"filename": song, "tracks": {"Melody": {"engine": "sfizz"}}}
    )
    assert missing.isError and "needs 'sfz'" in missing.content[0].text

    bad_path = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": str(tmp_path / "no.sfz")}}},
    )
    assert bad_path.isError and "does not exist" in bad_path.content[0].text

    with_program = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz, "program": 4}}},
    )
    assert with_program.isError and "fluidsynth option" in with_program.content[0].text

    with_send = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz, "reverb_send": 60}}},
    )
    assert with_send.isError and "engine effects" in with_send.content[0].text

    sfz_on_fluidsynth = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "fluidsynth", "sfz": sfz}}},
    )
    assert sfz_on_fluidsynth.isError and "sfizz option" in sfz_on_fluidsynth.content[0].text


async def test_list_instruments_reports_sfizz_tracks(store, tmp_path):
    song = await _make_song(store)
    sfz = _write_sfz(tmp_path)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz}}},
    )
    listed = (await _call("list_instruments", {"filename": song})).structuredContent
    effective = listed["file"]["effective"]["Melody"]
    assert effective["source"] == "sfz"
    assert effective["label"] == "tone.sfz"
    assert effective["bank"] is None and effective["program"] is None
    assert listed["file"]["resolved"]["Melody"] == "tone.sfz"
    engines = {engine["name"]: engine for engine in listed["engines"]}
    assert engines["sfizz"]["available"] is True


async def test_bake_instruments_with_sfizz(store, tmp_path):
    song = await _make_song(store)
    sfz = _write_sfz(tmp_path)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "sfizz", "sfz": sfz}}},
    )
    baked = await _call("bake_instruments", {"filename": song})
    assert not baked.isError, baked.content[0].text
    tracks = baked.structuredContent["tracks"]
    assert tracks["Melody"]["engine"] == "sfizz"
    assert tracks["Melody"]["sfz"] == sfz
