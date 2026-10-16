"""Master bus on renders: loudness target, true-peak limiter and mix-wide VST chains."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, mastering, renderers, server, singing
from chordsmith.models import LimiterSettings, MasterSettings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_sine(path: Path, seconds: float = 1.0, amplitude: int = 6000) -> None:
    rate = 44100
    frames = bytearray()
    for index in range(int(seconds * rate)):
        value = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
        frames += value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


def _skip_without_ffmpeg() -> None:
    if audio.find_ffmpeg() is None:
        pytest.skip("ffmpeg is not installed")


def test_apply_master_hits_the_loudness_target_and_limits(tmp_path):
    _skip_without_ffmpeg()
    path = tmp_path / "quiet.wav"
    _write_sine(path, amplitude=2000)  # quiet on purpose
    report = mastering.apply_master(
        path,
        MasterSettings(loudness_target_lufs=-20.0, limiter=LimiterSettings(enabled=True, ceiling_db=-1.0)),
    )
    assert report["gain_correction_db"] != 0
    assert report["loudness"]["integrated_lufs"] == pytest.approx(-20.0, abs=1.0)
    assert report["limiter_reduction_db"] is not None
    assert report["peak_db"] <= -0.9
    assert report["warnings"] == []


def test_apply_master_limiter_only_guards_the_ceiling(tmp_path):
    _skip_without_ffmpeg()
    path = tmp_path / "hot.wav"
    _write_sine(path, amplitude=32000)
    report = mastering.apply_master(path, MasterSettings(limiter=LimiterSettings(enabled=True)))
    assert report["peak_db"] <= -0.9
    assert report["limiter_reduction_db"] > 0


class _FakeRenderer:
    name = "fluidsynth"
    notes = "test renderer"

    def available(self):
        return True

    def render(self, midi, spec, out, effects=None):
        _write_sine(out, amplitude=3000)


async def _make_band(store) -> str:
    await _call("create_chord_progression", {"chords": ["Am", "F"], "filename": "song"})
    added = await _call(
        "add_track",
        {
            "filename": "song.mid",
            "track_name": "Bass",
            "notes": [{"pitch": "A2", "start_beat": 0, "beats": 1.0}],
        },
    )
    return added.structuredContent["filename"]


async def test_render_master_applies_to_the_stems_mix(store, monkeypatch):
    _skip_without_ffmpeg()
    song = await _make_band(store)
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", _FakeRenderer())
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_sine(target))
    result = await _call(
        "render_audio",
        {
            "filename": song,
            "stems": True,
            "normalize_peak_db": -1.0,
            "master": {"loudness_target_lufs": -20, "limiter": {"enabled": True}},
        },
    )
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["master"]["loudness"]["integrated_lufs"] == pytest.approx(-20.0, abs=1.0)
    # the loudness target supersedes peak normalization
    assert data["gain_correction_db"] == 0.0
    assert data["peak_db"] <= -0.9


async def test_render_master_effects_run_on_the_combined_mix(store, monkeypatch):
    song = await _make_band(store)
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", _FakeRenderer())
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_sine(target))
    applied: list[tuple[str, object]] = []
    monkeypatch.setattr(
        renderers, "apply_stem_effects", lambda stem, effects: applied.append((stem.name, effects))
    )
    result = await _call(
        "render_audio",
        {
            "filename": song,
            "stems": True,
            "master_effects": [{"plugin": "TestReverb.vst3"}],
        },
    )
    assert not result.isError, result.content[0].text
    mix_calls = [(name, effects) for name, effects in applied if effects]
    assert len(mix_calls) == 1
    stem_name, effects = mix_calls[0]
    assert stem_name.endswith("_Bass.wav")  # the combined mix, not a stem
    assert effects[0].plugin == "TestReverb.vst3"
    assert result.structuredContent["master"] is None


def test_mix_master_effects_run_between_mix_and_limiter(tmp_path, monkeypatch):
    _skip_without_ffmpeg()
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    target = tmp_path / "mix.wav"
    _write_sine(backing, amplitude=4000)
    _write_sine(vocal, amplitude=9000)
    calls: list[tuple[str, list[str]]] = []

    def fake_effects(stem, effects):
        calls.append((stem.name, [effect.plugin for effect in effects]))
        _write_sine(stem, amplitude=15000)  # a plugin that adds level, like a tape emulation

    monkeypatch.setattr(renderers, "apply_stem_effects", fake_effects)
    info = singing.mix_tracks(
        backing,
        vocal,
        target,
        vocal_level_db=0.0,
        normalize_peak_db=None,
        # a loud target forces the correction gain to push peaks at the ceiling, so the built-in
        # limiter must engage *after* the plugins
        master=MasterSettings(
            loudness_target_lufs=-6.0, limiter=LimiterSettings(enabled=True, ceiling_db=-1.0)
        ),
        master_effects=[renderers.EffectPlugin(plugin="Loud.vst3")],
    )
    assert calls == [("mix_pre_plugins.wav", ["Loud.vst3"])]  # one whole-song pass, pre-master
    assert target.is_file()
    # the gain runs before ChordSmith's limiter: the ceiling holds even after the plugins added
    # ~6 dB and the loudness correction added more
    assert info["loudness"]["true_peak_db"] <= -0.9
    assert info["limiter_reduction_db"] is not None
    assert 0 < info["limiter_reduction_db"] < 3
    # the plugin pass changed the measured peak the report is based on (post-plugin file)
    assert info["peak_db"] <= -0.9
    # the balance probe predates the plugins: the check says so instead of crying mismatch
    assert info["balance_check"] == "unavailable"
    assert any("balance_check is unavailable" in warning for warning in info["warnings"])


def test_mix_without_master_effects_skips_the_plugin_pass(tmp_path, monkeypatch):
    _skip_without_ffmpeg()
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    target = tmp_path / "mix.wav"
    _write_sine(backing, amplitude=4000)
    _write_sine(vocal, amplitude=9000)

    def boom(stem, effects):
        raise AssertionError("the plugin chain must not run without master_effects")

    monkeypatch.setattr(renderers, "apply_stem_effects", boom)
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=0.0, normalize_peak_db=-1.0)
    assert target.is_file()
    assert info["peak_db"] <= -0.9
