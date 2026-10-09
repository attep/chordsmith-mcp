"""Tests for the singing integration (VOICEVOX adapter stubbed)."""

from __future__ import annotations

import base64
import io
import time
import wave
from pathlib import Path

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, diffsinger, server, singing

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


def _write_constant_wav(path, amplitude: int, seconds: float = 1.0, rate: int = 44100) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little") * round(seconds * rate))


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


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_corrects_clipping(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 30000)
    _write_constant_wav(vocal, 30000)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=6.0, backing_volume=1.0, reverb=True)
    assert info["clipping"] is False
    assert info["peak_db"] <= -0.1
    assert abs(info["duration_seconds"] - 1.0) < 0.05


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_mix_balances_levels_by_measurement(tmp_path):
    backing = tmp_path / "backing.wav"
    vocal = tmp_path / "vocal.wav"
    _write_constant_wav(backing, 3000)
    _write_constant_wav(vocal, 24000)
    target = tmp_path / "mix.wav"
    info = singing.mix_tracks(backing, vocal, target, vocal_level_db=6.0, backing_volume=1.0)
    expected_gain = singing.gated_rms_db(backing) + 6.0 - singing.gated_rms_db(vocal)
    assert abs(info["vocal_gain_db"] - expected_gain) <= 0.2  # the loud vocal is turned down
    assert info["vocal_gain_db"] < 0
    assert abs(info["vocal_to_backing_db"] - 6.0) <= 0.2
    assert info["clipping"] is False


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
    assert diffsinger.split_syllables("so- di- um gold") == ["so", "di", "um", "gold"]
    phones, _ = diffsinger.phonemize_tokens(bank, ["so", "di", "um", "gold"])
    assert phones[0] == ["s", "ow"]
    assert phones[1] == ["d", "iy"]
    assert phones[2] == ["ah", "m"]
    assert phones[3] == ["g", "ow", "l", "d"]
    with pytest.raises(diffsinger.DiffSingerError) as exc:
        diffsinger.phonemize_tokens(bank, ["streetlights"])
    assert "streetlights" in str(exc.value)


@needs_voicebank
def test_english_words_phonemes():
    bank = diffsinger.get_voicebank()
    phones, _ = diffsinger.phonemize_tokens(bank, ["sodium", "gold"])
    assert phones[0] == ["s", "ow", "d", "iy", "ah", "m"]
    assert phones[1] == ["g", "ow", "l", "d"]


def test_split_syllables_controls():
    assert diffsinger.split_syllables("so- di- um + gold") == ["so", "di", "um", "+", "gold"]
    assert diffsinger.split_syllables("ah - br") == ["ah", "-", "br"]


class _StubBank:
    def is_vowel(self, phoneme):
        return phoneme in {"a", "e", "i", "o", "u", "ow", "iy", "ah"}


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
    plain = (await _call("prepare_vocal_score", {"filename": song, "track": "Melody"})).structuredContent
    assert [note["kind"] for note in plain["notes"]] == ["note", "rest", "note", "rest", "note"]

    joined = (
        await _call("prepare_vocal_score", {"filename": song, "track": "Melody", "legato": 0.25})
    ).structuredContent
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
def test_plus_carries_only_the_vowel():
    bank = diffsinger.get_voicebank()
    phones, _ = diffsinger.phonemize_tokens(bank, ["gold", "+", "+"])
    assert phones == [["g", "ow", "l", "d"], ["ow"], ["ow"]]


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
    assert sum(1 for note in mapping["notes"] if note["kind"] == "rest") >= 3


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
