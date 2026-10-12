"""Tests for background mix jobs: phases, per-track progress, failures and listings."""

from __future__ import annotations

import asyncio
import time
import wave

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import renderers, server, singing

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_tiny_wav(path, amplitude=1000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(amplitude.to_bytes(2, "little") * 4410)


async def _make_band(store):
    store.ensure_root()
    await _call(
        "create_chord_progression",
        {"chords": ["Am", "F"], "filename": "band", "tempo": 120},
    )
    bass = [{"pitch": "C2", "start_beat": 0, "beats": 1, "velocity": 80}]
    with_bass = (
        await _call("add_track", {"filename": "band.mid", "track_name": "Bass", "notes": bass})
    ).structuredContent["filename"]
    melody = [{"pitch": "C5", "start_beat": 0, "beats": 0.5, "velocity": 80}]
    song = (
        await _call("add_track", {"filename": with_bass, "track_name": "Melody", "notes": melody})
    ).structuredContent["filename"]
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    return song


def _fake_mix(backing, vocal, target, **kwargs):
    target.write_bytes(b"RIFF0000")
    return {
        "peak_db": -1.0,
        "clipping": False,
        "gain_correction_db": 0.0,
        "normalize_peak_db": -1.0,
        "duration_seconds": 4.0,
        "backing_rms_db": -30.0,
        "vocal_rms_db": -24.0,
        "vocal_gain_db": 0.0,
        "vocal_to_backing_db": 6.0,
        "vocal_to_original_backing_db": 6.0,
        "vocal_to_backing_measured_db": 6.0,
        "balance_check": "ok",
    }


class _FlakyRenderer:
    """Fake engine: records which tracks rendered, fails for the listed ones."""

    name = "fluidsynth"

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.rendered: list[str] = []

    def available(self):
        return True

    def render(self, midi, spec, out, effects=None):
        names = [
            next((message.name for message in track if message.type == "track_name"), "")
            for track in mido.MidiFile(midi).tracks
        ]
        name = names[-1]
        if name in self.fail_for:
            raise renderers.RenderError(f"engine exploded on {name}")
        self.rendered.append(name)
        _write_tiny_wav(out)


async def _wait_job(job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = (await _call("get_mix_job", {"job_id": job_id})).structuredContent
        if data["status"] in ("done", "failed"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("mix job did not finish in time")


@pytest.fixture
def fake_engine(monkeypatch):
    fake = _FlakyRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(singing, "_mix_stems", lambda stems, levels, target: _write_tiny_wav(target))
    monkeypatch.setattr(singing, "mix_tracks", _fake_mix)
    return fake


@pytest.fixture
def legacy_engine(monkeypatch):
    def fake_render(source, target, audio_format, soundfont=None, effects=None):
        target.write_bytes(b"RIFF0000")

    monkeypatch.setattr(singing.audio, "render", fake_render)
    monkeypatch.setattr(singing, "mix_tracks", _fake_mix)


async def test_mix_job_legacy_path_reports_phases(store, legacy_engine):
    song = await _make_band(store)
    started = await _call(
        "mix_song_with_vocals_job",
        {"source": song, "vocal": "vocal.wav", "output_filename": "job_mix"},
    )
    assert not started.isError, started.content[0].text
    handle = started.structuredContent
    assert handle["status"] == "queued"

    job = await _wait_job(handle["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["filename"] == "job_mix.wav"
    states = {entry["track"]: entry["state"] for entry in job["progress"]}
    assert states == {"backing": "done", "mix": "done"}

    listed = (await _call("list_mix_jobs", {})).structuredContent
    assert listed["jobs"][0]["job_id"] == handle["job_id"]
    assert listed["jobs"][0]["status"] == "done"


async def test_mix_job_stems_path_reports_each_track(store, fake_engine):
    song = await _make_band(store)
    set_spec = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "gain_db": -2.0}}},
    )
    assert not set_spec.isError, set_spec.content[0].text

    started = await _call(
        "mix_song_with_vocals_job",
        {"source": song, "vocal": "vocal.wav", "output_filename": "job_stems"},
    )
    job = await _wait_job(started.structuredContent["job_id"])
    assert job["status"] == "done", job["error"]
    states = {entry["track"]: entry["state"] for entry in job["progress"]}
    assert states == {"Chords": "done", "Bass": "done", "mix": "done"}
    # the guide ("Melody") stays out of the backing; the other tracks render once each
    assert sorted(fake_engine.rendered) == ["Bass", "Chords"]


async def test_mix_job_failure_names_the_track(store, monkeypatch):
    song = await _make_band(store)
    flaky = _FlakyRenderer(fail_for={"Bass"})
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", flaky)
    monkeypatch.setattr(singing, "_mix_stems", lambda stems, levels, target: _write_tiny_wav(target))
    monkeypatch.setattr(singing, "mix_tracks", _fake_mix)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth"}}},
    )

    started = await _call("mix_song_with_vocals_job", {"source": song, "vocal": "vocal.wav"})
    job = await _wait_job(started.structuredContent["job_id"])
    assert job["status"] == "failed"
    assert "Track 'Bass'" in job["error"]
    states = {entry["track"]: entry for entry in job["progress"]}
    assert states["Bass"]["state"] == "failed"
    assert "engine exploded" in states["Bass"]["error"]


async def test_mix_job_start_validations(store):
    song = await _make_band(store)

    both = await _call(
        "mix_song_with_vocals_job",
        {"source": song, "vocal": "vocal.wav", "vocals": [{"file": "vocal.wav", "start_seconds": 0.0}]},
    )
    assert both.isError and "not both" in both.content[0].text

    neither = await _call("mix_song_with_vocals_job", {"source": song})
    assert neither.isError and "vocal file" in neither.content[0].text

    missing_source = await _call("mix_song_with_vocals_job", {"source": "nope.mid", "vocal": "vocal.wav"})
    assert missing_source.isError

    missing_vocal = await _call("mix_song_with_vocals_job", {"source": song, "vocal": "gone.wav"})
    job = await _wait_job(missing_vocal.structuredContent["job_id"])
    assert job["status"] == "failed" and "gone" in job["error"]

    unknown = await _call("get_mix_job", {"job_id": "mix_nope"})
    assert unknown.isError and "not found" in unknown.content[0].text
