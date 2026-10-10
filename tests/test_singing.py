"""Tests for the singing integration (VOICEVOX adapter stubbed)."""

from __future__ import annotations

import array
import asyncio
import base64
import hashlib
import io
import math
import time
import wave
from pathlib import Path

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, diffsinger, renderers, server, singing

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
        self.query_speakers: list[int] = []
        self.synth_speakers: list[int] = []

    def singers(self):
        return [
            {
                "name": "テスト",
                "styles": [
                    {"id": 6000, "name": "ノーマル", "type": "sing"},
                    {"id": 3014, "name": "ノーマル", "type": "frame_decode"},
                ],
            }
        ]

    def sing_frame_audio_query(self, score, speaker, timeout=300):
        self.query_speakers.append(speaker)
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
        self.synth_speakers.append(speaker)
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
    assert data["notes"][0]["seconds"] == 0.25  # 0.5 beat at 120 BPM
    assert data["notes"][0]["frames"] == 23  # VOICEVOX preview frames
    assert data["total_seconds"] == 2.0


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
    assert [n["lyric"] for n in short["notes"] if n["kind"] == "note"] == ["あ", "い"]
    assert sum(1 for n in short["notes"] if n["kind"] == "rest") >= 2

    long = (
        await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "あ い う え お か"})
    ).structuredContent
    assert any("extra syllables" in warning for warning in long["warnings"])


async def test_lyric_validation(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent

    bad_char = await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "la la"})
    assert bad_char.isError and "kana" in bad_char.content[0].text

    french = await _call(
        "map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "la", "language": "fr"}
    )
    assert french.isError and "not supported" in french.content[0].text


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
    assert data["notes"][0]["seconds"] == round(first_note["seconds"] * 2, 4)


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
    assert job["settings"]["volume_cap"] == 0.6  # the job echoes what was used
    assert job["settings"]["gender"] == 0.0  # defaults are included, not just the overrides
    assert job["settings"]["steps"] == 20
    assert fake_engine.renders == 1
    assert fake_engine.query_speakers == [6000]
    assert fake_engine.synth_speakers == [6000]
    assert all(v <= 0.6 for v in fake_engine.last_query["volume"])

    again = await _call(
        "render_singing",
        {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000", "settings": {"volume_cap": 0.6}},
    )
    assert again.structuredContent["reused"] is True
    assert again.structuredContent["job_id"] == started.structuredContent["job_id"]
    assert fake_engine.renders == 1  # a retry never renders twice


async def test_decode_only_voice_is_queried_by_the_teacher(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (await _call("map_vocal_lyrics", {"score_id": score["score_id"]})).structuredContent

    started = await _call(
        "render_singing", {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:3014"}
    )
    assert not started.isError
    job = await _wait_for_job(started.structuredContent["job_id"])
    assert job["status"] == "done"
    assert job["voice_id"] == "voicevox:3014"
    assert job["query_voice_id"] == "voicevox:6000"  # teacher prepares the frame query
    assert fake_engine.query_speakers == [6000]
    assert fake_engine.synth_speakers == [3014]  # the chosen voice's timbre sings


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
    result = await _call("list_singing_voices", {"engine": "voicevox"})
    voices = result.structuredContent["voices"]
    assert voices[0]["voice_id"] == "voicevox:6000"
    assert voices[0]["language"] == "ja"
    assert voices[0]["soft_controls"]["energy"] is True
    assert voices[0]["soft_controls"]["breathiness"] is False
    assert voices[0]["query_via_teacher"] is False
    assert voices[1]["voice_id"] == "voicevox:3014"
    assert voices[1]["query_via_teacher"] is True


async def test_list_singing_voices_all_engines(fake_engine):
    result = await _call("list_singing_voices", {})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["voicevox"]["voices"][0]["voice_id"] == "voicevox:6000"
    assert "diffsinger" in data
    # the single-engine shape still carries a top-level voices list
    single = await _call("list_singing_voices", {"engine": "voicevox"})
    assert single.structuredContent["voices"][0]["voice_id"] == "voicevox:6000"


@pytest.mark.skipif(not diffsinger.available(), reason="DiffSinger voicebank not configured")
async def test_list_singing_voices_diffsinger(fake_engine):
    result = await _call("list_singing_voices", {"engine": "diffsinger"})
    data = result.structuredContent
    voices = data["voices"]
    assert [v["voice_id"] for v in voices] == [
        "diffsinger:hanami/root",
        "diffsinger:hanami/fragrance",
        "diffsinger:hanami/nectar",
    ]
    assert all(v["language"] == "en" for v in voices)
    assert "Lotte V" in voices[0]["credit"]
    assert "non-commercial" in data["commercial_status"]


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


def _write_constant_wav(
    path, amplitude: int, seconds: float = 1.0, rate: int = 44100, channels: int = 1
) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little") * round(seconds * rate) * channels)


def _write_sine_wav(
    path, frequency: float, amplitude: int, seconds: float = 1.0, rate: int = 44100, channels: int = 1
) -> None:
    samples = array.array(
        "h",
        (
            round(amplitude * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(round(seconds * rate))
            for _ in range(channels)
        ),
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


def test_gated_rms_db(tmp_path):
    loud = tmp_path / "loud.wav"
    quiet = tmp_path / "quiet.wav"
    _write_constant_wav(loud, 32767)
    _write_constant_wav(quiet, 3277)
    assert abs(singing.gated_rms_db(loud) - 0.0) < 0.1
    assert abs(singing.gated_rms_db(quiet) + 20.0) < 0.2
    silence = tmp_path / "silence.wav"
    _write_constant_wav(silence, 0)
    assert singing.gated_rms_db(silence) == -120.0


def test_active_levels_measure_only_sung_parts(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    # backing: loud first half, silent second half; vocal: silent first half, loud second half
    with wave.open(str(backing), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes((24000).to_bytes(2, "little") * 22050 + (0).to_bytes(2, "little") * 22050)
    with wave.open(str(vocal), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes((0).to_bytes(2, "little") * 22050 + (12000).to_bytes(2, "little") * 22050)
    backing_db, vocal_db = singing.active_levels(backing, vocal)
    assert backing_db == -120.0  # the loud part plays while the voice is silent: not counted
    assert abs(vocal_db - 20 * math.log10(12000 / 32768)) < 0.3


def test_active_levels_average_all_channels(tmp_path):
    backing = tmp_path / "band.wav"
    vocal = tmp_path / "vocal.wav"
    # a band that is only in the left channel: both channels must count, not just the left
    frame = (24000).to_bytes(2, "little") + (0).to_bytes(2, "little")
    with wave.open(str(backing), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(frame * 22050)
    _write_constant_wav(vocal, 12000)
    backing_db, _ = singing.active_levels(backing, vocal)
    expected = 20 * math.log10(24000 / math.sqrt(2) / 32768)  # energy averaged over both channels
    assert abs(backing_db - expected) < 0.3


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_corrects_clipping_and_normalizes(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 8000, channels=2)
    _write_constant_wav(vocal, 3000)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(
        backing, vocal, target, vocal_level_db=6.0, backing_volume=1.0, reverb=True, compress=False
    )
    assert info["clipping"] is False
    assert abs(info["peak_db"] - (-1.0)) <= 0.2  # normalized to -1 dBFS by default
    assert 1.0 <= info["duration_seconds"] <= 1.1  # the echo tail extends the mix slightly
    assert info["balance_check"] == "unavailable"  # the measurement assumes dry stems


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_balances_levels_by_measurement(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    # tones, not constants: the measured balance assumes the stems are uncorrelated, and two
    # constant signals are perfectly correlated (their amplitudes add, not their energies)
    _write_sine_wav(backing, 440.0, 3000, channels=2)
    _write_sine_wav(vocal, 660.0, 24000)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=6.0, backing_volume=1.0, compress=False)
    backing_db, vocal_db = singing.active_levels(backing, vocal)
    expected_gain = backing_db + 6.0 - vocal_db
    assert abs(info["vocal_gain_db"] - expected_gain) <= 0.2  # the loud vocal is turned down
    assert info["vocal_gain_db"] < 0
    assert abs(info["vocal_to_backing_db"] - 6.0) <= 0.2
    # the balance is also measured from the finished mix, not just calculated
    assert info["balance_check"] == "ok"
    assert info["vocal_to_backing_measured_db"] is not None
    assert abs(info["vocal_to_backing_measured_db"] - 6.0) <= 0.6
    assert info["clipping"] is False


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_vocal_level_is_exact_after_stereo_pan(tmp_path):
    # a quiet stereo backing and a much louder mono vocal: after panning the mono vocal to
    # stereo at full level, the measured vocal in the mix must match the computed gain
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 300, channels=2)
    _write_constant_wav(vocal, 12000)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(
        backing, vocal, target, vocal_level_db=6.0, backing_volume=0.0, normalize_peak_db=None, compress=False
    )
    backing_db, vocal_db = singing.active_levels(backing, vocal)
    expected_mix_vocal_db = vocal_db + info["vocal_gain_db"]
    measured = singing.gated_rms_db(target)
    assert abs(measured - expected_mix_vocal_db) <= 0.3  # no hidden ~3 dB mono-to-stereo loss


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_measures_the_vocal_at_the_mix_rate(tmp_path):
    # VOICEVOX sings at 24 kHz and the resampler's anti-alias filter costs high frequencies a
    # couple of dB on the way to the 44.1 kHz mix; the level must be measured after conversion,
    # or the finished balance lands below the requested vocal_level_db
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 3000, channels=2)
    rate = 24000
    tone = array.array("h", (round(8000 * math.sin(2 * math.pi * 11900 * i / rate)) for i in range(rate)))
    with wave.open(str(vocal), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(tone.tobytes())
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=6.0, backing_volume=1.0)
    assert info["balance_check"] == "ok"
    assert abs(info["vocal_to_backing_measured_db"] - 6.0) <= 1.0


def _window_rms_db(path, start_s, end_s, rate=44100):
    with wave.open(str(path), "rb") as handle:
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
        channels = handle.getnchannels()
    frames = len(samples) // channels
    start = max(0, round(start_s * rate))
    end = min(frames, round(end_s * rate))
    total = 0.0
    count = 0
    for frame in range(start, end, 4):
        for channel in range(channels):
            value = samples[frame * channels + channel] / 32768.0
            total += value * value
            count += 1
    return 20 * math.log10(max(math.sqrt(total / count), 1e-9)) if count else -120.0


def _write_block_vocal(path, amplitudes, block_s=0.5, rate=44100, frequency=440.0):
    samples = array.array("h")
    for amplitude in amplitudes:
        frames = round(block_s * rate)
        samples.extend(round(amplitude * math.sin(2 * math.pi * frequency * i / rate)) for i in range(frames))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_compressor_tames_the_vocal_dynamics(tmp_path):
    # DiffSinger can swing ~6 dB between notes; the default vocal chain (high-pass + gentle
    # compressor) must reduce that swing before the balance is measured
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 1500, seconds=4.0, channels=2)
    _write_block_vocal(vocal, [24000, 2400] * 4)
    plain = tmp_path / "plain.wav"
    compressed = tmp_path / "compressed.wav"
    singing.mix_tracks(backing, vocal, plain, compress=False)
    singing.mix_tracks(backing, vocal, compressed, compress=True)

    def swing(path):
        levels = [_window_rms_db(path, start, start + 0.4) for start in (0.05, 0.55, 1.05, 1.55)]
        return max(levels) - min(levels)

    assert swing(compressed) < swing(plain) - 2.0
    assert swing(compressed) < 8.0


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_ducking_dips_the_band_and_keeps_the_requested_level(tmp_path):
    # vocal_level_db is the ratio the listener hears over the dipped band: the vocal gain is
    # reduced by duck_db against the original backing, so the band steps back without the vocal
    # growing louder (against the original backing it sits duck_db lower).
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 8000, seconds=2.0, channels=2)
    _write_block_vocal(vocal, [20000, 0], block_s=1.0)
    plain = tmp_path / "plain.wav"
    ducked = tmp_path / "ducked.wav"
    plain_info = singing.mix_tracks(backing, vocal, plain, compress=False, normalize_peak_db=None)
    ducked_info = singing.mix_tracks(
        backing, vocal, ducked, compress=False, ducking=True, duck_db=4.0, normalize_peak_db=None
    )
    assert ducked_info["vocal_gain_db"] == plain_info["vocal_gain_db"] - 4.0  # the vocal steps back too
    assert ducked_info["vocal_to_backing_db"] == 6.0  # what you hear over the dipped band
    assert ducked_info["vocal_to_original_backing_db"] == 2.0
    assert ducked_info["balance_check"] == "ok"
    assert ducked_info["duck_db"] == 4.0
    assert plain_info["duck_db"] is None
    # the band is lower right after the voice stops (recovering) and back later
    assert _window_rms_db(ducked, 1.05, 1.35) < _window_rms_db(plain, 1.05, 1.35) - 1.0
    assert abs(_window_rms_db(plain, 1.8, 2.0) - _window_rms_db(ducked, 1.8, 2.0)) < 0.5


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_true_peak_guard_prevents_clipping(tmp_path):
    # hot settings: the old code measured the peak on the already-clamped 16-bit file and only
    # ever turned it down by the normalization margin; the float probe sees the true peak
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 20000, seconds=1.0, channels=2)
    _write_constant_wav(vocal, 20000)
    target = tmp_path / "hot.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=18.0, backing_volume=2.0, compress=False)
    assert info["clipping"] is False  # the export never clips
    assert info["gain_correction_db"] < -3.0  # a hot raw mix was turned down
    assert abs(info["peak_db"] - (-1.0)) <= 0.2
    with wave.open(str(target), "rb") as handle:
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    assert max(abs(value) for value in samples) < 32767  # no full-scale samples


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_clipping_guard_works_without_normalization(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 20000, seconds=1.0, channels=2)
    _write_constant_wav(vocal, 20000)
    target = tmp_path / "raw_hot.wav"
    info = singing.mix_tracks(
        backing,
        vocal,
        target,
        vocal_level_db=18.0,
        backing_volume=2.0,
        compress=False,
        normalize_peak_db=None,
    )
    assert info["clipping"] is False  # the guard still applies when normalization is off
    assert info["gain_correction_db"] < 0
    assert info["peak_db"] <= -0.05
    with wave.open(str(target), "rb") as handle:
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    assert max(abs(value) for value in samples) < 32767


async def test_mix_echoes_compress_and_ducking(store, monkeypatch):
    song = await _make_band_song(store)
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    captured = {}

    def fake_render(source, target, audio_format, soundfont=None):
        target.write_bytes(b"RIFF0000")

    def fake_mix(backing, vocal, target, **kwargs):
        captured.update(kwargs)
        target.write_bytes(b"RIFF0000")
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

    monkeypatch.setattr(singing.audio, "render", fake_render)
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)
    result = await _call(
        "mix_song_with_vocals",
        {"source": song, "vocal": "vocal.wav", "compress": False, "ducking": True},
    )
    assert not result.isError
    assert result.structuredContent["compress"] is False
    assert result.structuredContent["ducking"] is True
    assert captured["compress"] is False and captured["ducking"] is True


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


# ------------------------------------------------------------------ DiffSinger (English)


needs_voicebank = pytest.mark.skipif(not diffsinger.available(), reason="DiffSinger voicebank not configured")


@needs_voicebank
def test_english_phonemizer():
    bank = diffsinger.get_voicebank()
    groups = diffsinger.split_syllables("so- di- um gold")
    assert groups == [["so", "di", "um"], ["gold"]]
    phones, warnings, _ = diffsinger.phonemize_groups(bank, groups)
    assert phones[0] == ["s", "ow"]
    assert phones[1] == ["d", "iy"]
    assert phones[2] == ["ah", "m"]
    assert phones[3] == ["g", "ow", "l", "d"]
    assert warnings == []
    with pytest.raises(diffsinger.DiffSingerError) as exc:
        diffsinger.phonemize_groups(bank, [["streetlights"]])
    assert "streetlights" in str(exc.value)


@needs_voicebank
def test_english_words_phonemes():
    bank = diffsinger.get_voicebank()
    phones, _, _ = diffsinger.phonemize_groups(bank, [["sodium"], ["gold"]])
    assert phones[0] == ["s", "ow", "d", "iy", "ah", "m"]
    assert phones[1] == ["g", "ow", "l", "d"]


@needs_voicebank
def test_whole_word_lookup_splits_syllables():
    bank = diffsinger.get_voicebank()
    # "yellow" over two notes (yel- low): y-eh then l-ow (not "yell low" with a double l)
    phones, warnings, _ = diffsinger.phonemize_groups(bank, [["yel", "low"]])
    assert warnings == []
    assert phones[0] == ["y", "eh"]
    assert phones[1] == ["l", "ow"]
    # "moment" over two notes: m-ow then m-ah-n-t
    phones, _, _ = diffsinger.phonemize_groups(bank, [["mo", "ment"]])
    assert phones[0] == ["m", "ow"]
    assert phones[1] == ["m", "ah", "n", "t"]


@needs_voicebank
def test_vowel_mismatch_falls_back_with_warning():
    bank = diffsinger.get_voicebank()
    # "ion" has two vowels but three notes: warn, then look the pieces up individually
    phones, warnings, _ = diffsinger.phonemize_groups(bank, [["i", "o", "n"]])
    assert any("'ion'" in warning and "falling back" in warning for warning in warnings)
    assert phones[0] == ["ay"]
    assert phones[1] == ["ow"]
    assert phones[2] == ["eh", "n"]


def test_split_syllables_controls():
    assert diffsinger.split_syllables("so- di- um + gold") == [["so", "di", "um"], ["+"], ["gold"]]
    assert diffsinger.split_syllables("ah - br") == [["ah"], ["-"], ["br"]]


class _StubBank:
    def is_vowel(self, phoneme):
        return phoneme in {
            "a",
            "e",
            "i",
            "o",
            "u",
            "ae",
            "aa",
            "ao",
            "ah",
            "aw",
            "ay",
            "eh",
            "er",
            "ey",
            "ih",
            "iy",
            "ow",
            "oy",
            "uh",
            "uw",
            "SP",
            "AP",
        }


def test_word_inputs_match_the_ph_num_convention():
    # words are vowel-anchored spans: each vowel starts a span to the next vowel, the trailing
    # silence is its own span, and every span carries its note's frames (no one-frame words)
    notes = [
        diffsinger.NoteSpec(phonemes=["g", "l", "ow"], start_seconds=0.0, seconds=0.5, midi=62),
        diffsinger.NoteSpec(phonemes=["hh", "ey", "z"], start_seconds=0.5, seconds=0.5, midi=62),
    ]
    segments = [{"phoneme": "SP", "note": -1, "kind": "pad"}]
    for index, note in enumerate(notes):
        for j, phone in enumerate(note.phonemes):
            kind = "vowel" if j == len(note.phonemes) - 1 else "onset"
            segments.append({"phoneme": phone, "note": index, "kind": kind})
    segments.append({"phoneme": "SP", "note": -1, "kind": "pad"})
    word_div, word_dur = diffsinger._word_inputs(segments, notes, _StubBank())
    assert word_div == [3, 2, 2, 1]  # [SP,g,l] [ow,hh] [ey,z] [SP]
    frames = diffsinger.frame_at(0.5)
    assert word_dur == [frames, frames, frames, diffsinger.TAIL_FRAMES]


def test_plan_timeline_puts_vowels_on_notes():
    notes = [
        diffsinger.NoteSpec(phonemes=["s", "ow"], start_seconds=0.0, seconds=0.5, midi=60),
        diffsinger.NoteSpec(phonemes=["g", "ow", "l", "d"], start_seconds=0.5, seconds=0.5, midi=62),
    ]
    predicted = [[2.0, 3.0], [2.0, 3.0, 2.0, 1.0]]
    timeline = diffsinger.plan_timeline(notes, predicted, bank=_StubBank())
    frame = 0
    vowel_start: dict[int, int] = {}
    for entry in timeline:
        if entry["kind"] == "vowel":
            vowel_start.setdefault(entry["note"], frame)
        frame += entry["frames"]
    assert vowel_start[0] == diffsinger.HEAD_FRAMES
    assert vowel_start[1] == diffsinger.HEAD_FRAMES + diffsinger.frame_at(0.5)
    assert frame == sum(entry["frames"] for entry in timeline)


def test_onsets_anticipate_into_the_previous_vowel_not_the_coda():
    notes = [
        diffsinger.NoteSpec(phonemes=["hh", "ey", "z"], start_seconds=0.0, seconds=0.5, midi=62),
        diffsinger.NoteSpec(phonemes=["g", "l", "ae", "s"], start_seconds=0.5, seconds=0.5, midi=62),
    ]
    predicted = [[10.0, 25.0, 8.0], [8.0, 12.0, 25.0, 12.0]]
    timeline = diffsinger.plan_timeline(notes, predicted, bank=_StubBank())
    codas = [e for e in timeline if e["kind"] == "coda" and e["note"] == 0]
    assert sum(e["frames"] for e in codas) == 8  # the z keeps its full length
    onsets = [e for e in timeline if e["kind"] == "onset" and e["note"] == 1]
    assert sum(e["frames"] for e in onsets) == 20  # 8 + 12, taken from the previous vowel
    vowels = [e for e in timeline if e["kind"] == "vowel" and e["note"] == 0]
    assert sum(e["frames"] for e in vowels) == 35 - 20


@needs_voicebank
def test_render_sodium_gold(tmp_path):
    specs = [
        diffsinger.NoteSpec(phonemes=phones, start_seconds=index * 0.5, seconds=0.5, midi=midi)
        for index, (phones, midi) in enumerate(
            [(["s", "ow"], 60), (["d", "iy"], 62), (["ah", "m"], 64), (["g", "ow", "l", "d"], 65)]
        )
    ]
    out = tmp_path / "sodium.wav"
    info = diffsinger.render(specs, out, mode="nectar", steps=12, total_seconds=2.0)
    with wave.open(str(out), "rb") as handle:
        assert handle.getframerate() == 44100
        assert handle.getnchannels() == 1
        seconds = handle.getnframes() / handle.getframerate()
    expected = 2.0 + 2 * diffsinger.HEAD_FRAMES * diffsinger.FRAME_MS / 1000
    assert abs(seconds - expected) <= 0.05
    assert info["peak_db"] <= -1.0  # report test 3: peak below -1 dBFS
    assert out.stat().st_size > 44100  # at least a quarter second of audio


@needs_voicebank
async def test_english_mapping_via_tools(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (
        await _call(
            "map_vocal_lyrics",
            {"score_id": score["score_id"], "lyrics": "so- di- um gold", "language": "en"},
        )
    ).structuredContent
    sung = [note for note in mapping["notes"] if note["kind"] == "note"]
    assert [note["lyric"] for note in sung] == ["so", "di", "um", "gold"]
    assert sung[0]["phonemes"] == ["s", "ow"]
    assert mapping["voice_hint"] == "diffsinger"


@needs_voicebank
async def test_diffsinger_job_dispatch(store, fake_engine, monkeypatch):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (
        await _call(
            "map_vocal_lyrics",
            {"score_id": score["score_id"], "lyrics": "so- di- um gold", "language": "en"},
        )
    ).structuredContent

    def fake_render(specs, output, **kwargs):
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\x01\x00" * 4410)
        return {
            "duration_seconds": 0.1,
            "sample_rate": 44100,
            "frames": 9,
            "peak_db": -20.0,
            "mode": kwargs.get("mode"),
        }

    monkeypatch.setattr(diffsinger, "render", fake_render)
    started = await _call(
        "render_singing",
        {
            "mapping_id": mapping["mapping_id"],
            "voice_id": "diffsinger:hanami/nectar",
            "settings": {"steps": 10, "velocity": 1.1},
        },
    )
    assert not started.isError
    job = await _wait_for_job(started.structuredContent["job_id"])
    assert job["status"] == "done"
    assert job["mode"] == "nectar"
    assert (store.root / job["filename"]).is_file()


def test_voicebank_never_enters_the_repo():
    root = Path(__file__).resolve().parents[1]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "voicebank/" in ignore
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8").lower()
    assert "voicebank" not in dockerfile
    assert "dsconfig" not in dockerfile


# ------------------------------------------------------------------ audit fixes


async def _make_melody(store, notes, name="melody", tempo=120):
    store.ensure_root()
    await _call("create_chord_progression", {"chords": ["C"], "filename": name, "tempo": tempo})
    result = await _call("add_track", {"filename": f"{name}.mid", "track_name": "Melody", "notes": notes})
    return result.structuredContent["filename"]


def test_gender_defaults_to_neutral():
    assert singing.SoftSettings().gender == 0.0  # Hanami's own character, not an extreme shift


async def test_legato_closes_small_gaps(store):
    notes = [
        {"pitch": "C5", "start_beat": 0.0, "beats": 0.5},
        {"pitch": "D5", "start_beat": 0.55, "beats": 0.5},
        {"pitch": "E5", "start_beat": 1.10, "beats": 0.5},
    ]
    song = await _make_melody(store, notes, name="legato")
    plain = (
        await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "legato": 0})
    ).structuredContent
    assert [note["kind"] for note in plain["notes"]] == ["note", "rest", "note", "rest", "note"]

    # legato is on by default now: the gaps close without asking
    joined = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    assert [note["kind"] for note in joined["notes"]] == ["note", "note", "note"]
    assert joined["notes"][0]["seconds"] > plain["notes"][0]["seconds"]  # runs into the next note


async def test_score_spells_flats_in_flat_keys(store):
    notes = [
        {"pitch": pitch, "start_beat": index, "beats": 1}
        for index, pitch in enumerate(["C5", "Eb5", "F5", "G5", "Ab5", "Bb5"])
    ]
    song = await _make_melody(store, notes, name="flat_key")
    score = (
        await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "transpose": 0})
    ).structuredContent
    names = [note["name"] for note in score["notes"] if note["kind"] == "note"]
    assert names == ["C5", "Eb5", "F5", "G5", "Ab5", "Bb5"]  # not D#5/G#5/A#5


@needs_voicebank
def test_plus_carries_vowel_and_moves_the_coda():
    bank = diffsinger.get_voicebank()
    phones, warnings, _ = diffsinger.phonemize_groups(bank, [["gold"], ["+"], ["+"]])
    assert warnings == []
    # "gold + +" sings g-ow, ow, ow-l-d: the closing consonants move to the last slur note
    assert phones == [["g", "ow"], ["ow"], ["ow", "l", "d"]]
    phones, _, _ = diffsinger.phonemize_groups(bank, [["still"], ["+"]])
    assert phones == [["s", "t", "ih"], ["ih", "l"]]


@needs_voicebank
async def test_english_fewer_syllables_becomes_rests_not_a_crash(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (
        await _call("map_vocal_lyrics", {"score_id": score["score_id"], "lyrics": "sodium", "language": "en"})
    ).structuredContent
    sung = [note for note in mapping["notes"] if note["kind"] == "note"]
    assert len(sung) == 1
    assert sung[0]["phonemes"] == ["s", "ow", "d", "iy", "ah", "m"]
    assert any("had no syllable" in warning for warning in mapping["warnings"])
    rests = [note for note in mapping["notes"] if note["kind"] == "rest"]
    assert len(rests) >= 3
    assert all(rest["pitch"] is None for rest in rests)  # rests carry no pitch


@needs_voicebank
async def test_holds_keep_lyrics_and_phonemes_aligned(store, fake_engine):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    first_note_id = next(note["note_id"] for note in score["notes"] if note["kind"] == "note")
    mapping = (
        await _call(
            "map_vocal_lyrics",
            {
                "score_id": score["score_id"],
                "lyrics": "so- di- um gold",
                "language": "en",
                "holds": {str(first_note_id): 1},
            },
        )
    ).structuredContent
    sung = [note for note in mapping["notes"] if note["kind"] == "note"]
    # note 0 holds over note 1; the remaining notes get so, di, um (gold is unused)
    assert [note["lyric"] for note in sung] == ["so", "di", "um"]
    assert [note["phonemes"] for note in sung] == [["s", "ow"], ["d", "iy"], ["ah", "m"]]


@needs_voicebank
async def test_stress_warning_when_stressed_syllable_gets_a_short_note(store, fake_engine):
    # "so" (stressed in "sodium") on a 0.25 s note, "di" (weak) on 0.5 s
    notes = [
        {"pitch": "C5", "start_beat": 0.0, "beats": 0.5},
        {"pitch": "D5", "start_beat": 0.5, "beats": 1.0},
        {"pitch": "E5", "start_beat": 1.5, "beats": 1.0},
    ]
    song = await _make_melody(store, notes, name="stress")
    score = (
        await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "transpose": 0})
    ).structuredContent
    mapping = (
        await _call(
            "map_vocal_lyrics",
            {"score_id": score["score_id"], "lyrics": "so- di- um", "language": "en"},
        )
    ).structuredContent
    warning = next((w for w in mapping["warnings"] if "stressed" in w), None)
    assert warning is not None
    assert "'sodium'" in warning and "'so'" in warning and "'di'" in warning


@needs_voicebank
async def test_align_stress_swaps_note_lengths(store, fake_engine):
    notes = [
        {"pitch": "C5", "start_beat": 0.0, "beats": 0.5},
        {"pitch": "D5", "start_beat": 0.5, "beats": 1.0},
        {"pitch": "E5", "start_beat": 1.5, "beats": 1.0},
    ]
    song = await _make_melody(store, notes, name="align")
    score = (
        await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "transpose": 0})
    ).structuredContent
    plain = (
        await _call(
            "map_vocal_lyrics",
            {"score_id": score["score_id"], "lyrics": "so- di- um", "language": "en"},
        )
    ).structuredContent
    aligned = (
        await _call(
            "map_vocal_lyrics",
            {
                "score_id": score["score_id"],
                "lyrics": "so- di- um",
                "language": "en",
                "align_stress": True,
            },
        )
    ).structuredContent

    plain_sung = [note for note in plain["notes"] if note["kind"] == "note"]
    aligned_sung = [note for note in aligned["notes"] if note["kind"] == "note"]
    assert [note["seconds"] for note in plain_sung] == [0.25, 0.5, 0.5]
    # "so" (stressed) takes the longest note; pitch order and total duration are unchanged
    assert [note["seconds"] for note in aligned_sung] == [0.5, 0.25, 0.5]
    assert [note["pitch"] for note in aligned_sung] == [note["pitch"] for note in plain_sung]
    assert sum(note["seconds"] for note in aligned_sung) == sum(note["seconds"] for note in plain_sung)
    assert any("swapped" in warning for warning in aligned["warnings"])


async def test_mix_accepts_a_job_id(store, fake_engine, monkeypatch):
    song = await _make_song(store)
    score = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    mapping = (await _call("map_vocal_lyrics", {"score_id": score["score_id"]})).structuredContent
    started = await _call(
        "render_singing", {"mapping_id": mapping["mapping_id"], "voice_id": "voicevox:6000"}
    )
    job = await _wait_for_job(started.structuredContent["job_id"])
    assert job["status"] == "done"

    def fake_render(source, target, audio_format, soundfont=None):
        target.write_bytes(b"RIFF0000")

    def fake_mix(backing, vocal, target, **kwargs):
        target.write_bytes(b"RIFF0000")
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

    monkeypatch.setattr(singing.audio, "render", fake_render)
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)
    # the job-id branch must resolve the .wav (it used to force .mid and fail with "not found")
    result = await _call(
        "mix_song_with_vocals", {"source": song, "vocal": started.structuredContent["job_id"]}
    )
    assert not result.isError
    assert result.structuredContent["vocal"] == job["filename"]


def test_find_track_prefers_exact_guide_names(tmp_path):
    midi = mido.MidiFile()
    for name in ("Chords", "Lead", "Vocal", "Melody"):
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        midi.tracks.append(track)
    # an exact guide name wins over earlier melody-like names (Lead comes first in the file)
    assert singing.find_track(midi, None) == 2  # 'Vocal'

    only_lead = mido.MidiFile()
    for name in ("Chords", "Lead"):
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        track.append(mido.Message("note_on", note=60, velocity=80, time=0))
        only_lead.tracks.append(track)
    assert singing.find_track(only_lead, None) == 1  # the regex fallback still finds 'Lead'
    assert singing.find_track(only_lead, "Lead") == 1
    assert singing.find_track(only_lead, 2) == 1
    with pytest.raises(singing.SingingError):
        singing.find_track(only_lead, "Nope")


async def _make_band_song(store):
    """A song with chords, a Bass track and a Melody track (for backing-level tests)."""
    song = await _make_song(store)
    notes = [
        {"pitch": pitch, "start_beat": start, "beats": 1.0, "velocity": 80}
        for pitch, start in [("C2", 0), ("F2", 1), ("C2", 2), ("G2", 3)]
    ]
    result = await _call("add_track", {"filename": song, "track_name": "Bass", "notes": notes})
    return result.structuredContent["filename"]


async def test_mix_per_stem_levels_validate_track_names(store):
    song = await _make_band_song(store)
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    result = await _call(
        "mix_song_with_vocals",
        {"source": song, "vocal": "vocal.wav", "backing_levels": {"Nope": -3}},
    )
    assert result.isError
    assert "Nope" in result.content[0].text and "Bass" in result.content[0].text


async def test_mix_per_stem_levels_render_each_track(store, monkeypatch):
    song = await _make_band_song(store)
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    rendered = []
    mixed = {}

    class FakeRenderer:
        name = "fluidsynth"

        def available(self):
            return True

        def render(self, midi, spec, out):
            rendered.append(Path(midi).name)
            out.write_bytes(b"RIFF0000")

    def fake_stems(stems, levels, target):
        mixed["stems"] = list(stems)
        mixed["levels"] = list(levels)
        target.write_bytes(b"RIFF0000")

    def fake_mix(backing, vocal, target, **kwargs):
        target.write_bytes(b"RIFF0000")
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
    monkeypatch.setattr(singing, "_mix_stems", fake_stems)
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)
    result = await _call(
        "mix_song_with_vocals",
        {"source": song, "vocal": "vocal.wav", "backing_levels": {"Bass": -6}},
    )
    assert not result.isError
    assert result.structuredContent["backing_levels"] == {"Bass": -6.0}
    # two stems (Chords and Bass); the guide (Melody) is left out and unlisted Chords keeps 0 dB
    assert len(rendered) == 2 and len(mixed["stems"]) == 2
    assert mixed["levels"] == [0.0, -6.0]


async def test_mix_uses_sidecar_instrument_specs(store, monkeypatch):
    song = await _make_band_song(store)
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    captured = []

    class FakeRenderer:
        name = "fluidsynth"

        def available(self):
            return True

        def render(self, midi, spec, out):
            captured.append((Path(midi).name, spec.program, spec.gain_db))
            out.write_bytes(b"RIFF0000")

    def fake_stems(stems, levels, target):
        target.write_bytes(b"RIFF0000")

    def fake_mix(backing, vocal, target, **kwargs):
        target.write_bytes(b"RIFF0000")
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
    monkeypatch.setattr(singing, "_mix_stems", fake_stems)
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)
    await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Chords": {"engine": "fluidsynth", "program": 89},
                "Bass": {"engine": "fluidsynth", "program": 38, "gain_db": -3.0},
            },
        },
    )
    result = await _call("mix_song_with_vocals", {"source": song, "vocal": "vocal.wav"})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    # specs force the per-track path even without backing_levels, and reach the renderer
    assert len(captured) == 2
    assert {program for _, program, _ in captured} == {89, 38}
    assert data["instruments"]["Bass"]["program"] == 38
    assert data["instruments"]["Bass"]["gain_db"] == -3.0
    assert data["instruments"]["Chords"]["program"] == 89

    overridden = await _call(
        "mix_song_with_vocals",
        {"source": song, "vocal": "vocal.wav", "backing_levels": {"Bass": -6}},
    )
    # an explicit backing_levels entry overrides the spec's gain, the program still applies
    assert overridden.structuredContent["instruments"]["Bass"]["gain_db"] == -6.0
    assert overridden.structuredContent["instruments"]["Bass"]["program"] == 38


async def test_concurrent_mixes_get_distinct_backings(store, monkeypatch):
    song = await _make_band_song(store)
    (store.root / "vocal.wav").write_bytes(b"RIFF0000")
    backings = []

    def fake_render(source, target, audio_format, soundfont=None):
        target.write_bytes(b"RIFF0000")

    def fake_mix(backing, vocal, target, **kwargs):
        backings.append(backing.name)
        target.write_bytes(b"RIFF0000")
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

    monkeypatch.setattr(singing.audio, "render", fake_render)
    monkeypatch.setattr(singing, "mix_tracks", fake_mix)

    async def mix_once(name):
        return await _call(
            "mix_song_with_vocals",
            {"source": song, "vocal": "vocal.wav", "output_filename": name, "overwrite": True},
        )

    first, second = await asyncio.gather(mix_once("mix_a"), mix_once("mix_b"))
    assert not first.isError and not second.isError
    # even with overwrite=true, each mix renders its own backing (no shared intermediate)
    assert first.structuredContent["backing"] != second.structuredContent["backing"]
    assert len(set(backings)) == 2
    assert (store.root / first.structuredContent["backing"]).is_file()


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_stems_applies_levels(tmp_path):
    loud = tmp_path / "a.wav"
    quiet = tmp_path / "b.wav"
    _write_constant_wav(loud, 8000, channels=2)
    _write_constant_wav(quiet, 8000, channels=2)
    full = tmp_path / "full.wav"
    trimmed = tmp_path / "trimmed.wav"
    singing._mix_stems([loud, quiet], [0.0, 0.0], full)
    singing._mix_stems([loud, quiet], [0.0, -20.0], trimmed)
    assert singing.gated_rms_db(trimmed) < singing.gated_rms_db(full) - 2.0


def test_patch_graph_seeds_random_nodes():
    onnx = pytest.importorskip("onnx")
    node = onnx.helper.make_node("RandomNormal", [], ["out"], shape=[2, 2])
    graph = onnx.helper.make_graph(
        [node], "g", [], [onnx.helper.make_tensor_value_info("out", onnx.TensorProto.FLOAT, [2, 2])]
    )
    diffsinger._patch_graph(graph, 42, [0])
    seeds = [attribute.f for attribute in graph.node[0].attribute if attribute.name == "seed"]
    assert len(seeds) == 1 and seeds[0] > 0
    diffsinger._patch_graph(graph, 42, [0])  # patching again replaces, never duplicates
    seeds = [attribute.f for attribute in graph.node[0].attribute if attribute.name == "seed"]
    assert len(seeds) == 1


@needs_voicebank
def test_diffsinger_seed_is_deterministic(tmp_path):
    pytest.importorskip("onnx")
    specs = [
        diffsinger.NoteSpec(phonemes=["s", "ow"], start_seconds=0.0, seconds=0.5, midi=60),
        diffsinger.NoteSpec(phonemes=["g", "ow", "l", "d"], start_seconds=0.5, seconds=0.5, midi=62),
    ]
    first = tmp_path / "seed_a.wav"
    second = tmp_path / "seed_b.wav"
    third = tmp_path / "seed_c.wav"
    diffsinger.render(specs, first, mode="nectar", steps=4, seed=7)
    diffsinger.render(specs, second, mode="nectar", steps=4, seed=7)
    diffsinger.render(specs, third, mode="nectar", steps=4, seed=8)
    digest_first = hashlib.sha256(first.read_bytes()).digest()
    assert digest_first == hashlib.sha256(second.read_bytes()).digest()  # same seed, same bytes
    assert digest_first != hashlib.sha256(third.read_bytes()).digest()  # different seed, different
