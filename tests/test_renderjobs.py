"""Tests for background render jobs: progress, partial failures and the retry path."""

from __future__ import annotations

import asyncio
import time
import wave

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import renderers, server

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
        {"chords": ["Am", "F", "C", "G"], "filename": "band", "tempo": 120},
    )
    bass = [
        {"pitch": "C2", "start_beat": 0, "beats": 1, "velocity": 80},
        {"pitch": "F2", "start_beat": 1, "beats": 1, "velocity": 80},
    ]
    with_bass = (
        await _call("add_track", {"filename": "band.mid", "track_name": "Bass", "notes": bass})
    ).structuredContent["filename"]
    melody = [{"pitch": "C5", "start_beat": 0, "beats": 0.5, "velocity": 80}]
    return (
        await _call("add_track", {"filename": with_bass, "track_name": "Melody", "notes": melody})
    ).structuredContent["filename"]


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
        data = (await _call("get_render_job", {"job_id": job_id})).structuredContent
        if data["status"] in ("done", "failed"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError("render job did not finish in time")


@pytest.fixture
def fake_engine(monkeypatch):
    fake = _FlakyRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target))
    return fake


async def test_render_job_reports_progress_and_result(store, fake_engine):
    song = await _make_band(store)
    started = await _call("render_audio_job", {"filename": song, "stems": True})
    assert not started.isError, started.content[0].text
    handle = started.structuredContent
    assert handle["status"] == "queued"
    assert handle["tracks"] == ["Chords", "Bass", "Melody"]

    job = await _wait_job(handle["job_id"])
    assert job["status"] == "done", job["error"]
    assert {entry["state"] for entry in job["progress"]} == {"done"}
    result = job["result"]
    assert [stem["track"] for stem in result["stems"]] == ["Chords", "Bass", "Melody"]
    assert result["failed"] is None
    assert sorted(fake_engine.rendered) == ["Bass", "Chords", "Melody"]


async def test_render_job_partial_failure_and_retry(store, monkeypatch):
    song = await _make_band(store)
    flaky = _FlakyRenderer(fail_for={"Bass"})
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", flaky)
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target))

    started = await _call("render_audio_job", {"filename": song, "stems": True})
    job = await _wait_job(started.structuredContent["job_id"])
    assert job["status"] == "done"  # a partial failure is still a finished job
    states = {entry["track"]: entry for entry in job["progress"]}
    assert states["Bass"]["state"] == "failed"
    assert "engine exploded on Bass" in states["Bass"]["error"]
    assert states["Chords"]["state"] == "done"
    assert [failed["track"] for failed in job["result"]["failed"]] == ["Bass"]
    assert {stem["track"] for stem in job["result"]["stems"]} == {"Chords", "Melody"}

    # retry only the failed track; the successful stems are not rendered again
    healthy = _FlakyRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", healthy)
    retry = await _call("render_audio_job", {"filename": song, "tracks": ["Bass"]})
    retried = await _wait_job(retry.structuredContent["job_id"])
    assert retried["status"] == "done", retried["error"]
    assert [entry["track"] for entry in retried["progress"]] == ["Bass"]
    assert healthy.rendered == ["Bass"]
    assert [stem["track"] for stem in retried["result"]["stems"]] == ["Bass"]


async def test_render_job_validates_tracks_and_ids(store, fake_engine):
    song = await _make_band(store)
    unknown = await _call("render_audio_job", {"filename": song, "tracks": ["Nope"]})
    assert unknown.isError and "Unknown track(s) Nope" in unknown.content[0].text
    assert "Bass" in unknown.content[0].text

    missing = await _call("get_render_job", {"job_id": "render_nope"})
    assert missing.isError and "not found" in missing.content[0].text
