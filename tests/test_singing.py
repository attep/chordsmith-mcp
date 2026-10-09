"""Tests for the singing integration (VOICEVOX adapter stubbed)."""

from __future__ import annotations

import base64
import io
import time
import wave

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, server, singing

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _tone_wav(frames: int, rate: int = 24000, amplitude: int = 200) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little") * round(frames / singing.FRAME_RATE * rate))
    return buffer.getvalue()


class FakeVoicevox:
    def __init__(self):
        self.base_url = "http://fake"
        self.renders = 0
        self.last_query: dict | None = None

    def singers(self):
        return [{"name": "テスト", "styles": [{"id": 6000, "name": "ノーマル"}]}]

    def sing_frame_audio_query(self, score, speaker, timeout=300):
        total = sum(note["frame_length"] for note in score["notes"])
        return {
            "f0": [440.0] * total,
            "volume": [0.8] * total,
            "phonemes": [],
            "volumeScale": 1.0,
            "outputSamplingRate": 24000,
            "outputStereo": False,
        }

    def frame_synthesis(self, query, speaker, timeout=600):
        self.renders += 1
        self.last_query = query
        return _tone_wav(len(query["f0"]))


@pytest.fixture
def fake_engine(monkeypatch):
    fake = FakeVoicevox()
    monkeypatch.setattr(singing, "get_client", lambda: fake)
    return fake


async def _make_song(store):
    """A chords file plus a monophonic Melody track (notes at 0, 1, 2 and 3.5 beats)."""
    store.ensure_root()
    await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "song", "tempo": 120},
    )
    notes = [
        {"pitch": pitch, "start_beat": start, "beats": 0.5, "velocity": 80}
        for pitch, start in [("C5", 0), ("D5", 1), ("E5", 2), ("G5", 3.5)]
    ]
    result = await _call("add_track", {"filename": "song.mid", "track_name": "Melody", "notes": notes})
    return result.structuredContent["filename"]


# ------------------------------------------------------------------ score preparation


async def test_prepare_score_extracts_notes_and_rests(store, fake_engine):
    song = await _make_song(store)
    result = await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "transpose": -12})
    assert not result.isError
    data = result.structuredContent
    assert data["track"]["name"] == "Melody"
    assert [note["kind"] for note in data["notes"]] == [
        "note",
        "rest",
        "note",
        "rest",
        "note",
        "rest",
        "note",
    ]
    assert data["notes"][0]["pitch"] == 60  # C5 down an octave
    assert data["notes"][-1]["pitch"] == 67  # G5 down an octave
    assert data["notes"][0]["frames"] == 23  # 0.5 beat at 120 BPM


async def test_prepare_score_rejects_polyphony(store, fake_engine):
    store.ensure_root()
    await _call("create_chord_progression", {"chords": ["C"], "filename": "poly"})
    notes = [
        {"pitch": "C5", "start_beat": 0, "beats": 2},
        {"pitch": "E5", "start_beat": 1, "beats": 2},
    ]
    added = await _call("add_track", {"filename": "poly.mid", "track_name": "Melody", "notes": notes})
    result = await _call("prepare_vocal_score", {"filename": added.structuredContent["filename"]})
    assert result.isError
    assert "one note at a time" in result.content[0].text


async def test_prepare_score_never_changes_the_source(store, fake_engine):
    song = await _make_song(store)
    before = (store.root / song).read_bytes()
    await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})
    assert (store.root / song).read_bytes() == before


# ------------------------------------------------------------------ lyrics


async def test_wordless_mapping_uses_hum_on_every_note(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    result = await _call("map_vocal_lyrics", {"score_id": score["score_id"]})
    data = result.structuredContent
    assert data["wordless"] is True
    sung = [note for note in data["notes"] if note["kind"] == "note"]
    assert all(note["lyric"] == singing.WORDLESS_LYRIC for note in sung)
    assert data["warnings"] == []


async def test_lyric_mismatches_warn_and_never_invent(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent

    short = (
        await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "あ い"})
    ).structuredContent
    assert any("had no syllable" in warning for warning in short["warnings"])
    assert [n["lyric"] for n in short["notes"] if n["kind"] == "note"] == ["あ", "い", "", ""]

    long = (
        await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "あ い う え お か"})
    ).structuredContent
    assert any("extra syllables" in warning for warning in long["warnings"])


async def test_lyric_validation(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent

    bad_char = await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "la la"})
    assert bad_char.isError and "kana" in bad_char.content[0].text

    english = await _call(
        "map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "la", "language": "en"}
    )
    assert english.isError and "not supported" in english.content[0].text


async def test_holds_merge_following_notes(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    first_note = next(note for note in score["notes"] if note["kind"] == "note")
    result = await _call(
        "map_vocal_lyrics",
        {"score_id": score["score_id"], "holds": {str(first_note["note_id"]): 1}},
    )
    data = result.structuredContent
    assert data["warnings"] == []
    assert data["notes"][0]["frames"] == first_note["frames"] * 2


# ------------------------------------------------------------------ rendering


async def _wait_for_job(job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = (await _call("get_singing_job", {"job_id": job_id})).structuredContent
        if payload["status"] in ("done", "failed"):
            return payload
        time.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_render_job_lifecycle_and_reuse(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (await _call("map_vocal_lyrics", {"score_id": score["score_id"]})).structuredContent

    started = await _call(
        "render_singing",
        {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000", "settings": {"volume_cap": 0.6}},
    )
    assert not started.isError
    assert started.structuredContent["reused"] is False

    job = await _wait_for_job(started.structuredContent["job_id"])
    assert job["status"] == "done"
    assert (store.root / job["filename"]).is_file()
    assert job["duration_seconds"] > 0
    assert job["start_offset_seconds"] == 0.0
    assert fake_engine.renders == 1
    assert all(v <= 0.6 for v in fake_engine.last_query["volume"])

    again = await _call(
        "render_singing",
        {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000", "settings": {"volume_cap": 0.6}},
    )
    assert again.structuredContent["reused"] is True
    assert again.structuredContent["job_id"] == started.structuredContent["job_id"]
    assert fake_engine.renders == 1  # a retry never renders twice


async def test_unknown_voice_and_unsupported_controls_are_rejected(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (await _call("map_vocal_lyrics", {"score_id": score["score_id"]})).structuredContent

    unknown = await _call(
        "render_singing", {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:9999"}
    )
    assert unknown.isError and "not offered" in unknown.content[0].text

    wrong_engine = await _call("render_singing", {"mapping_id": mapping["mapping_id"], "voice_id": "other:1"})
    assert wrong_engine.isError

    breathy = await _call(
        "render_singing",
        {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000", "settings": {"breathiness": 0.5}},
    )
    assert breathy.isError and "breathiness" in breathy.content[0].text


async def test_list_singing_voices(fake_engine):
    result = await _call("list_singing_voices", {})
    voices = result.structuredContent["voices"]
    assert voices[0]["voice_id"] == "voicevox:6000"
    assert voices[0]["language"] == "ja"
    assert voices[0]["soft_controls"]["energy"] is True
    assert voices[0]["soft_controls"]["breathiness"] is False


# ------------------------------------------------------------------ mixing


def test_remove_track_keeps_the_source(tmp_path):
    from chordsmith.midi_writer import add_track as add_track_midi
    from chordsmith.midi_writer import write_progression
    from chordsmith.models import Rhythm, Voicing
    from chordsmith.theory import parse_chord

    source = tmp_path / "song.mid"
    write_progression(
        source,
        [(parse_chord("C"), 4)],
        tempo_bpm=120,
        time_signature=(4, 4),
        voicing=Voicing(),
        rhythm=Rhythm(),
        program=0,
    )
    add_track_midi(source, source, track_name="Melody", program=0, channel=0, notes=[(60, 0, 1, 90)])
    before = source.read_bytes()
    target = tmp_path / "backing.mid"
    singing.remove_track(source, target, 2)  # drop the Melody track
    assert source.read_bytes() == before
    backing = mido.MidiFile(target)
    assert len(backing.tracks) == 2
    assert not any(
        msg.type == "track_name" and msg.name == "Melody" for track in backing.tracks for msg in track
    )


def _ffmpeg_ready() -> bool:
    return audio.find_ffmpeg() is not None


def _full_audio_ready() -> bool:
    return (
        audio.find_ffmpeg() is not None
        and audio.find_fluidsynth() is not None
        and audio.find_soundfont() is not None
    )


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_corrects_clipping(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    for path in (backing, vocal):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes((30000).to_bytes(2, "little") * 44100)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_volume=0.9, backing_volume=0.55, reverb=True)
    assert info["clipping"] is False
    assert info["peak_db"] <= -0.1
    assert abs(info["duration_seconds"] - 1.0) < 0.05


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_export_returns_all_files(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (await _call("map_vocal_lyrics", {"score_id": score["score_id"]})).structuredContent
    started = await _call(
        "render_singing", {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000"}
    )
    job = await _wait_for_job(started.structuredContent["job_id"])

    mixed = await _call("mix_song_with_vocals", {"source": song, "vocal": job["filename"], "overwrite": True})
    assert not mixed.isError
    assert mixed.structuredContent["clipping"] is False
    assert mixed.structuredContent["guide_removed"] == "Melody"

    exported = await _call(
        "export_vocal_song",
        {"mix": mixed.structuredContent["filename"], "vocal": job["filename"], "source": song},
    )
    assert not exported.isError
    data = exported.structuredContent
    assert set(data) == {"mix", "mix_mp3", "vocal", "midi"}
    for entry in data.values():
        assert base64.b64decode(entry["data_base64"])[:1] in (b"R", b"I", b"M")
    assert data["midi"]["filename"] == song
