"""Tests for restart recovery, file tools and listing filters (abyssal audit fixes)."""

from __future__ import annotations

import base64
import io
import os
import time
import wave
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, renderers, renderjobs, server, singing

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ffmpeg_ready() -> bool:
    return audio.find_ffmpeg() is not None


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_wav(path, seconds: float = 0.5, amplitude: int = 4000, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little", signed=True) * round(seconds * rate))


async def _make_song(store, filename="song"):
    created = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": filename, "tempo": 120},
    )
    song = created.structuredContent["filename"]
    notes = [{"pitch": "C5", "start_beat": index, "beats": 0.9, "velocity": 80} for index in range(4)]
    added = await _call(
        "add_track",
        {"filename": song, "track_name": "Melody", "notes": notes, "output_filename": f"{filename}_mel"},
    )
    return added.structuredContent["filename"]


async def test_singing_records_survive_a_restart(store):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    await _call("map_vocal_lyrics", {"score_id": score["score_id"]})  # wordless: no voicebank needed
    state = Path(os.environ["CHORDSMITH_STATE_DIR"]) / "singing"
    assert (state / "scores.json").is_file()
    assert (state / "mappings.json").is_file()

    # simulate the restart: wipe memory, reload from disk
    with singing._registry.lock:
        singing._registry.scores.clear()
        singing._registry.mappings.clear()
        singing._registry.jobs.clear()
    singing._load_singing()

    again = await _call("map_vocal_lyrics", {"score_id": score["score_id"]})
    assert not again.isError, again.content[0].text
    assert again.structuredContent["score_id"] == score["score_id"]


async def test_interrupted_jobs_come_back_failed(store):
    job = singing.SingingJob(
        job_id="job_restarted",
        status="running",
        mapping_id="map_x",
        voice_id="diffsinger:hanami/nectar",
        settings={},
        cache_key="k",
        created_at=time.time(),
        source="song.mid",
    )
    with singing._registry.lock:
        singing._registry.jobs[job.job_id] = job
    singing._persist_singing()
    with singing._registry.lock:
        singing._registry.jobs.clear()
    singing._load_singing()

    payload = (await _call("get_singing_job", {"job_id": "job_restarted"})).structuredContent
    assert payload["status"] == "failed"
    assert "restart" in payload["error"]
    listed = (await _call("list_singing_jobs", {})).structuredContent["jobs"]
    entry = next(item for item in listed if item["job_id"] == "job_restarted")
    assert entry["status"] == "failed" and entry["source"] == "song.mid"


async def test_render_job_records_survive_a_restart(store, monkeypatch):

    class FakeRenderer:
        name = "fluidsynth"

        def available(self):
            return True

        def render(self, midi, spec, out, effects=None):
            _write_wav(out, 0.2)

    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", FakeRenderer())
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_wav(target, 0.2))
    song = await _make_song(store)
    started = await _call("render_audio_job", {"filename": song, "stems": True})
    job_id = started.structuredContent["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        payload = (await _call("get_render_job", {"job_id": job_id})).structuredContent
        if payload["status"] in ("done", "failed"):
            break
        import asyncio

        await asyncio.sleep(0.05)
    assert payload["status"] == "done", payload.get("error")

    # simulate a restart mid-render: a running record reloads as failed with the reason
    interrupted = renderjobs.RenderJob(
        job_id="render_restarted", status="running", created_at=time.time(), progress={"Bass": "running"}
    )
    with renderjobs._JOBS_LOCK:
        renderjobs._JOBS[interrupted.job_id] = interrupted
    renderjobs._persist()
    with renderjobs._JOBS_LOCK:
        renderjobs._JOBS.clear()
    renderjobs._load()
    reloaded = (await _call("get_render_job", {"job_id": "render_restarted"})).structuredContent
    assert reloaded["status"] == "failed" and "restart" in reloaded["error"]
    listed = (await _call("list_render_jobs", {})).structuredContent["jobs"]
    assert any(entry["job_id"] == "render_restarted" for entry in listed)


async def test_empty_files_are_swept_and_flagged(store):
    store.ensure_root()
    empty = store.root / "stale.wav"
    empty.write_bytes(b"")
    real = store.root / "good.wav"
    _write_wav(real)
    listing = (await _call("list_generated_files", {"kind": "wav"})).structuredContent
    flags = {entry["filename"]: entry["empty"] for entry in listing["files"]}
    assert flags["stale.wav"] is True and flags["good.wav"] is False
    removed = store.remove_empty_media()
    assert removed == ["stale.wav"]
    assert not empty.exists() and real.is_file()


async def test_list_generated_files_filters(store):
    store.ensure_root()
    await _make_song(store, filename="alpha")
    await _make_song(store, filename="beta")
    _write_wav(store.root / "alpha_take.wav")
    by_name = (await _call("list_generated_files", {"name_contains": "alpha"})).structuredContent
    assert by_name["total"] == 3  # alpha.mid, alpha_mel.mid and alpha_take.wav
    assert all("alpha" in entry["filename"] for entry in by_name["files"])
    by_kind = (await _call("list_generated_files", {"kind": "midi"})).structuredContent
    assert all(entry["kind"] == "midi" for entry in by_kind["files"])
    capped = (await _call("list_generated_files", {"limit": 1})).structuredContent
    assert len(capped["files"]) == 1 and capped["total"] > 1


async def test_get_file_serves_audio_and_the_midi_error_points_there(store):
    store.ensure_root()
    _write_wav(store.root / "take.wav", amplitude=1234)
    fetched = (await _call("get_file", {"filename": "take.wav"})).structuredContent
    assert fetched["mime_type"] == "audio/wav"
    with wave.open(io.BytesIO(base64.b64decode(fetched["data_base64"])), "rb") as handle:
        assert handle.getnframes() > 0
    assert base64.b64decode(fetched["data_base64"])
    wrong_tool = await _call("get_midi_file", {"filename": "take.wav"})
    assert wrong_tool.isError and "get_file" in wrong_tool.content[0].text


async def test_import_audio_reports_what_it_is(store, tmp_path):
    source = tmp_path / "take.wav"
    _write_wav(source, seconds=0.4, amplitude=8000)
    data = base64.b64encode(source.read_bytes()).decode()
    result = await _call("import_audio", {"filename": "imported_take.wav", "data_base64": data})
    assert not result.isError, result.content[0].text
    report = result.structuredContent
    assert report["format"] == "wav"
    assert report["duration_seconds"] == pytest.approx(0.4, abs=0.01)
    assert report["sample_rate"] == 44100 and report["channels"] == 1
    assert report["peak_db"] == pytest.approx(-12.3, abs=0.5)

    bad_name = await _call("import_audio", {"filename": "x.mid", "data_base64": data})
    assert bad_name.isError and "import_midi" in bad_name.content[0].text
    not_wav = await _call(
        "import_audio", {"filename": "x.wav", "data_base64": base64.b64encode(b"nope").decode()}
    )
    assert not_wav.isError and "RIFF" in not_wav.content[0].text


async def test_import_audio_mp3(tmp_path, store):
    if not _ffmpeg_ready():
        pytest.skip("ffmpeg is not installed")
    import subprocess

    wav = tmp_path / "tone.wav"
    _write_wav(wav)
    mp3 = tmp_path / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(mp3)], check=True, capture_output=True
    )
    result = await _call(
        "import_audio",
        {"filename": "tone.mp3", "data_base64": base64.b64encode(mp3.read_bytes()).decode()},
    )
    assert not result.isError, result.content[0].text
    assert result.structuredContent["format"] == "mp3"
    assert result.structuredContent["duration_seconds"] == pytest.approx(0.5, abs=0.05)


async def test_mix_accepts_vocal_segments(store, monkeypatch):
    if not _ffmpeg_ready():
        pytest.skip("ffmpeg is not installed")
    song = await _make_song(store)
    _write_wav(store.root / "verse1.wav", seconds=0.5)
    _write_wav(store.root / "verse2.wav", seconds=0.5)

    class FakeRenderer:
        name = "fluidsynth"

        def available(self):
            return True

        def render(self, midi, spec, out, effects=None):
            _write_wav(out, 0.2)

    captured = {}

    def fake_mix(backing, vocal, target, **kwargs):
        captured["vocal"] = vocal
        _write_wav(target, 0.2)
        return {
            "peak_db": -1.0,
            "clipping": False,
            "gain_correction_db": 0.0,
            "normalize_peak_db": -1.0,
            "duration_seconds": 1.0,
            "backing_rms_db": -30.0,
            "vocal_rms_db": -24.0,
            "vocal_gain_db": 0.0,
            "vocal_to_backing_db": 6.0,
            "vocal_to_original_backing_db": 6.0,
            "vocal_to_backing_measured_db": 6.0,
            "balance_check": "ok",
        }

    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", FakeRenderer())
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_wav(target, 0.2))
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)
    result = await _call(
        "mix_song_with_vocals",
        {
            "source": song,
            "vocals": [
                {"file": "verse1.wav", "start_seconds": 0.0},
                {"file": "verse2.wav", "start_seconds": 1.0},
            ],
            "output_filename": "segmented_mix",
            "overwrite": True,
        },
    )
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [segment["file"] for segment in data["vocal_segments"]] == ["verse1.wav", "verse2.wav"]
    # the combined vocal was placed at the offsets: 1.0 s + 0.5 s piece = 1.5 s
    with wave.open(str(captured["vocal"]), "rb") as handle:
        combined = handle.getnframes() / handle.getframerate()
    assert combined == pytest.approx(1.5, abs=0.05)

    both = await _call(
        "mix_song_with_vocals",
        {"source": song, "vocal": "verse1.wav", "vocals": [{"file": "verse1.wav"}], "output_filename": "x"},
    )
    assert both.isError and "not both" in both.content[0].text


async def test_score_reports_sections_and_tempo_changes(store):
    song = await _make_song(store)
    await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "Slow", "bars": 2, "bpm": 60}, {"name": "Fast", "bars": 2, "bpm": 120}],
        },
    )
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    assert [section["name"] for section in score["sections"]] == ["Slow", "Fast"]
    assert [change["bpm"] for change in score["tempo_changes"]] == pytest.approx([60.0, 120.0], abs=0.01)
    assert score["tempo_bpm"] == pytest.approx(60.0, abs=0.01)  # the starting tempo
