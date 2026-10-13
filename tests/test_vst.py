"""Tests for the VST engine (separate-process hosting).

They need the vst_host binary and the tiny ToneSynth/GainFx test plugins: the Docker image
bakes them in at /opt/vst-test (CHORDSMITH_VST_TEST_DIR), CI builds the host for smoke tests,
and local runs skip when either is missing.
"""

from __future__ import annotations

import array
import base64
import io
import math
import os
import sys
import wave
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, renderers, server

TEST_DIR = Path(os.environ.get("CHORDSMITH_VST_TEST_DIR", "/opt/vst-test"))


def _find(name: str) -> Path | None:
    if not TEST_DIR.is_dir():
        return None
    for path in sorted(TEST_DIR.rglob(name)):
        if path.is_dir():
            return path
    return None


TONE = _find("ToneSynth.vst3")
GAIN = _find("GainFx.vst3")

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        audio.find_vst_host() is None or TONE is None or GAIN is None,
        reason="vst_host or the VST test plugins are not installed",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


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


def _peak_db(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    peak = max((abs(value) for value in samples), default=0)
    return 20 * math.log10(max(peak / 32768.0, 1e-9))


def _duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


async def test_list_plugins_scans_and_describes(store, monkeypatch):
    monkeypatch.setenv("CHORDSMITH_VST_DIR", str(TEST_DIR))
    listed = (await _call("list_plugins", {})).structuredContent
    names = {entry["name"] for entry in listed["plugins"]}
    assert {"ToneSynth", "GainFx"} <= names
    assert listed["host"] is not None

    detail = (await _call("list_plugins", {"detail": "ToneSynth"})).structuredContent["detail"]
    first = detail["types"][0]
    assert first["is_instrument"] is True
    assert "Sine" in first["programs"] and "Saw" in first["programs"]

    with_params = (
        await _call("list_plugins", {"detail": "ToneSynth", "parameters": True, "parameter_search": "gain"})
    ).structuredContent
    assert any(entry["name"] == "Gain" for entry in with_params["parameters"])

    filtered = (await _call("list_plugins", {"name_contains": "tone"})).structuredContent
    assert {entry["name"] for entry in filtered["plugins"]} == {"ToneSynth"}
    assert all("ToneSynth" in entry["path"] for entry in filtered["plugins"])

    gain = (await _call("list_plugins", {"detail": str(GAIN)})).structuredContent["detail"]
    assert gain["types"][0]["is_instrument"] is False
    assert gain["types"][0]["latency_samples"] == 64


async def test_set_track_instrument_vst_validation(store):
    song = await _make_song(store)

    missing = await _call("set_track_instrument", {"filename": song, "tracks": {"Melody": {"engine": "vst"}}})
    assert missing.isError and "needs 'plugin'" in missing.content[0].text

    bad_path = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TEST_DIR / "no.vst3")}}},
    )
    assert bad_path.isError and "does not exist" in bad_path.content[0].text

    with_program = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "program": 4}}},
    )
    assert with_program.isError and "not a vst option" in with_program.content[0].text

    bad_preset = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "plugin_preset": "Nope"}},
        },
    )
    assert bad_preset.isError and "No program" in bad_preset.content[0].text

    effect_as_instrument = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(GAIN)}}},
    )
    assert effect_as_instrument.isError and "is an effect" in effect_as_instrument.content[0].text

    instrument_as_effect = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "effects": [{"plugin": str(TONE)}]}},
        },
    )
    assert instrument_as_effect.isError and "is an instrument" in instrument_as_effect.content[0].text

    ok = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "plugin_preset": "Sine"}},
        },
    )
    assert not ok.isError, ok.content[0].text
    assert ok.structuredContent["resolved"]["Melody"] == "ToneSynth.vst3 - Sine"
    effective = ok.structuredContent["effective"]["Melody"]
    assert effective["source"] == "vst"
    assert effective["label"] == "ToneSynth.vst3 - Sine"
    assert effective["bank"] is None and effective["program"] is None


async def test_vst_stems_render_and_single_pass_refusal(store):
    song = await _make_song(store)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE)}}},
    )
    refused = await _call("render_audio", {"filename": song, "format": "wav"})
    assert refused.isError and "stems=true" in refused.content[0].text

    rendered = await _call("render_audio", {"filename": song, "stems": True, "normalize_peak_db": -1.0})
    assert not rendered.isError, rendered.content[0].text
    content = rendered.structuredContent
    assert content["failed"] is None
    melody = next(stem for stem in content["stems"] if stem["track"] == "Melody")
    data = base64.b64decode(melody["data_base64"])
    assert _duration(data) == pytest.approx(content["duration_seconds"], abs=0.1)
    assert _peak_db(data) > -60


async def test_vst_effects_chain_is_applied_and_latency_compensated(store):
    song = await _make_song(store)
    plain = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE)}}},
    )
    assert not plain.isError, plain.content[0].text
    without = await _call(
        "render_audio",
        {"filename": song, "stems": True, "output_filename": "plain_mix", "overwrite": True},
    )
    assert not without.isError, without.content[0].text
    plain_stem = next(stem for stem in without.structuredContent["stems"] if stem["track"] == "Melody")
    plain_data = base64.b64decode(plain_stem["data_base64"])

    chained = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "effects": [{"plugin": str(GAIN)}]}},
        },
    )
    assert not chained.isError, chained.content[0].text
    with_fx = await _call(
        "render_audio",
        {"filename": song, "stems": True, "output_filename": "fx_mix", "overwrite": True},
    )
    assert not with_fx.isError, with_fx.content[0].text
    fx_stem = next(stem for stem in with_fx.structuredContent["stems"] if stem["track"] == "Melody")
    fx_data = base64.b64decode(fx_stem["data_base64"])

    # GainFx halves the signal and reports 64 samples of latency; the host compensates, so the
    # stem keeps the song's length and the peak drops by about 6 dB.
    assert _duration(fx_data) == pytest.approx(_duration(plain_data), abs=0.02)
    assert _peak_db(fx_data) == pytest.approx(_peak_db(plain_data) - 6.0, abs=1.5)


async def test_vst_plugin_params_change_the_render(store):
    song = await _make_song(store)
    bad = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "plugin_params": {"Nope": 0.5}}},
        },
    )
    assert bad.isError and "no parameter named" in bad.content[0].text

    async def render_with(params, output):
        spec = {"engine": "vst", "plugin": str(TONE)}
        if params is not None:
            spec["plugin_params"] = params
        set_spec = await _call("set_track_instrument", {"filename": song, "tracks": {"Melody": spec}})
        assert not set_spec.isError, set_spec.content[0].text
        rendered = await _call(
            "render_audio",
            {"filename": song, "stems": True, "output_filename": output, "overwrite": True},
        )
        assert not rendered.isError, rendered.content[0].text
        stem = next(s for s in rendered.structuredContent["stems"] if s["track"] == "Melody")
        return _peak_db(base64.b64decode(stem["data_base64"]))

    plain_peak = await render_with(None, "pp_plain")
    quiet_peak = await render_with({"Gain": 0.2}, "pp_quiet")
    # the test synth's Gain defaults to 0.8; 0.2 is about -12 dB
    expected = plain_peak + 20 * math.log10(0.2 / 0.8)
    assert quiet_peak == pytest.approx(expected, abs=1.5)


async def test_vst_plugin_params_by_name(store):
    song = await _make_song(store)

    async def render_with(params, output):
        spec = {"engine": "vst", "plugin": str(TONE)}
        if params is not None:
            spec["plugin_params"] = params
        set_spec = await _call("set_track_instrument", {"filename": song, "tracks": {"Melody": spec}})
        assert not set_spec.isError, set_spec.content[0].text
        rendered = await _call(
            "render_audio",
            {"filename": song, "stems": True, "output_filename": output, "overwrite": True},
        )
        assert not rendered.isError, rendered.content[0].text
        stem = next(s for s in rendered.structuredContent["stems"] if s["track"] == "Melody")
        return base64.b64decode(stem["data_base64"])

    sine = await render_with(None, "wave_sine")
    saw = await render_with({"Wave": "Saw"}, "wave_saw")
    assert saw != sine, "setting Wave by name did not change the render"

    indexed = await render_with({"1": "Saw"}, "wave_indexed")
    assert indexed == saw, "the index key and the name key should reach the same value"

    bad = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Melody": {
                    "engine": "vst",
                    "plugin": str(TONE),
                    "plugin_params": {"Wave": "Triangle"},
                }
            },
        },
    )
    assert not bad.isError  # names are validated on set; values resolve in the host at render time
    failed = await _call(
        "render_audio", {"filename": song, "stems": True, "output_filename": "wave_bad", "overwrite": True}
    )
    assert failed.isError and "no value matches" in failed.content[0].text, failed.content[0].text
    assert "Sine" in failed.content[0].text  # the error lists the available value names


async def test_vst_plugin_state_round_trip(store, tmp_path):
    song = await _make_song(store)
    host = audio.find_vst_host()
    state = tmp_path / "tone.state"
    dumped = audio.dump_vst_state(host, str(TONE), state, sets=[(1, "Saw")])
    assert state.is_file() and state.stat().st_size > 0 and dumped["bytes"] > 0, dumped

    async def render_with(extra, output):
        spec = {"engine": "vst", "plugin": str(TONE), **extra}
        set_spec = await _call("set_track_instrument", {"filename": song, "tracks": {"Melody": spec}})
        assert not set_spec.isError, set_spec.content[0].text
        rendered = await _call(
            "render_audio",
            {"filename": song, "stems": True, "output_filename": output, "overwrite": True},
        )
        assert not rendered.isError, rendered.content[0].text
        stem = next(s for s in rendered.structuredContent["stems"] if s["track"] == "Melody")
        return base64.b64decode(stem["data_base64"])

    by_set = await render_with({"plugin_params": {"Wave": "Saw"}}, "state_set")
    by_state = await render_with({"plugin_state": str(state)}, "state_file")
    assert by_state == by_set, "the captured state did not reproduce the parameter-set render"

    missing_state = {
        "engine": "vst",
        "plugin": str(TONE),
        "plugin_state": str(tmp_path / "no.state"),
    }
    missing = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": missing_state}},
    )
    assert missing.isError and "does not exist" in missing.content[0].text


async def test_vst_default_state_applies_unless_opted_out(store):
    song = await _make_song(store)
    host = audio.find_vst_host()
    state = store.root / "states" / "tone_saw.state"
    audio.dump_vst_state(host, str(TONE), state, sets=[(1, "Saw")])
    renderers._write_state_metadata(state.parent, "tone_saw", str(TONE), default=True)

    implied = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE)}}},
    )
    assert not implied.isError, implied.content[0].text
    assert implied.structuredContent["tracks"]["Melody"]["plugin_state"] == str(state)

    named = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "plugin_state": "tone_saw"}},
        },
    )
    assert not named.isError, named.content[0].text
    assert named.structuredContent["tracks"]["Melody"]["plugin_state"] == str(state)

    opted_out = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE), "plugin_state": "none"}},
        },
    )
    assert not opted_out.isError, opted_out.content[0].text
    assert opted_out.structuredContent["tracks"]["Melody"]["plugin_state"] is None


@pytest.mark.skipif(sys.platform != "linux", reason="the desktop-session guard is Linux-specific")
async def test_editor_tool_needs_a_desktop(store, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    result = await _call("open_plugin_state_editor", {"plugin": str(TONE)})
    assert result.isError and "desktop" in result.content[0].text, result.content[0].text


async def test_bake_instruments_with_vst(store):
    song = await _make_song(store)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Melody": {"engine": "vst", "plugin": str(TONE)}}},
    )
    baked = await _call("bake_instruments", {"filename": song})
    assert not baked.isError, baked.content[0].text
    assert baked.structuredContent["tracks"]["Melody"]["plugin"] == str(TONE)
