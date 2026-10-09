"""Tests for file delivery, extra tracks, feel options and file management."""

from __future__ import annotations

import base64
import hashlib
import time
import wave
from urllib.parse import parse_qs, urlparse

import httpx
import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, delivery, server
from chordsmith.analysis import analyze_file
from chordsmith.midi_writer import write_progression
from chordsmith.models import Humanize, Rhythm, Voicing
from chordsmith.theory import MusicTheoryError, parse_chord, parse_pitch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write(path, chords=("Am", "F", "C", "G"), **kwargs):
    return write_progression(
        path,
        [(parse_chord(c), 4) for c in chords],
        tempo_bpm=kwargs.pop("tempo_bpm", 120),
        time_signature=(4, 4),
        voicing=kwargs.pop("voicing", Voicing()),
        rhythm=kwargs.pop("rhythm", Rhythm()),
        program=kwargs.pop("program", 0),
        **kwargs,
    )


def _note_on_ticks(path):
    midi = mido.MidiFile(path)
    track = next(t for t in midi.tracks if any(m.type == "note_on" for m in t))
    tick = 0
    ons = []
    for msg in track:
        tick += msg.time
        if msg.type == "note_on":
            ons.append(tick)
    return sorted(set(ons))


# ------------------------------------------------------------------ parse_pitch


def test_parse_pitch():
    assert parse_pitch("C4") == 60
    assert parse_pitch("E6") == 88
    assert parse_pitch("Bb3") == 58
    assert parse_pitch("C-1") == 0
    assert parse_pitch("C") == 60  # default octave
    assert parse_pitch(88) == 88
    for bad in (128, -1, "C10", "H4", "", True):
        with pytest.raises(MusicTheoryError):
            parse_pitch(bad)


# ------------------------------------------------------------------ file delivery


async def test_get_midi_file_base64_matches_disk(store):
    store.ensure_root()
    _write(store.root / "delivery.mid")
    result = await _call("get_midi_file", {"filename": "delivery.mid"})
    assert not result.isError
    data = result.structuredContent
    raw = (store.root / "delivery.mid").read_bytes()
    assert data["mime_type"] == "audio/midi"
    assert data["size_bytes"] == len(raw)
    assert data["sha256"] == hashlib.sha256(raw).hexdigest()
    assert base64.b64decode(data["data_base64"]) == raw


async def test_get_midi_file_signed_url_roundtrip(store, monkeypatch):
    monkeypatch.setattr(delivery, "_secret", b"test-secret")
    monkeypatch.setenv("CHORDSMITH_PUBLIC_URL", "http://127.0.0.1:8000")
    store.ensure_root()
    _write(store.root / "link.mid")
    raw = (store.root / "link.mid").read_bytes()

    result = await _call("get_midi_file", {"filename": "link.mid", "return_as": "url", "expires_in": 60})
    data = result.structuredContent
    assert data["expires_at"] > time.time()
    parsed = urlparse(data["download_url"])
    query = parse_qs(parsed.query)

    transport = httpx.ASGITransport(app=server.mcp.streamable_http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        ok = await client.get(
            parsed.path, params={"expires": query["expires"][0], "token": query["token"][0]}
        )
        assert ok.status_code == 200
        assert ok.content == raw
        assert ok.headers["content-type"].startswith("audio/midi")

        tampered = await client.get(parsed.path, params={"expires": query["expires"][0], "token": "0" * 64})
        assert tampered.status_code == 403

        wrong_name = await client.get(
            "/files/other.mid", params={"expires": query["expires"][0], "token": query["token"][0]}
        )
        assert wrong_name.status_code == 403


async def test_signed_url_expires(store, monkeypatch):
    monkeypatch.setattr(delivery, "_secret", b"s")
    expired = int(time.time()) - 5
    assert not delivery.verify("link.mid", expired, delivery.sign("link.mid", expired))
    fresh = int(time.time()) + 60
    assert delivery.verify("link.mid", fresh, delivery.sign("link.mid", fresh))
    assert not delivery.verify("link.mid", fresh, "bad-token")
    assert not delivery.verify("link.mid", "not-a-number", "bad-token")


async def test_get_midi_file_url_needs_public_url(store, monkeypatch):
    monkeypatch.setattr(delivery, "_secret", b"x")
    monkeypatch.delenv("CHORDSMITH_PUBLIC_URL", raising=False)
    store.ensure_root()
    _write(store.root / "x.mid")
    result = await _call("get_midi_file", {"filename": "x.mid", "return_as": "url"})
    assert result.isError
    assert "CHORDSMITH_PUBLIC_URL" in result.content[0].text


async def test_get_midi_file_rejects_path_tricks(store):
    store.ensure_root()
    (store.root.parent / "secret.mid").write_bytes(b"secret")
    result = await _call("get_midi_file", {"filename": "../secret.mid"})
    assert result.isError
    assert (store.root.parent / "secret.mid").read_bytes() == b"secret"


# ------------------------------------------------------------------ add_track


MELODY = [
    "E5",
    "A5",
    "C6",
    "E6",
    "A5",
    "F5",
    "A5",
    "C6",
    "F6",
    "A5",
    "E5",
    "G5",
    "C6",
    "E6",
    "G5",
    "D5",
    "G5",
    "B5",
    "D6",
]


async def test_add_track_melody(store):
    store.ensure_root()
    source = store.root / "sketch.mid"
    _write(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    original_progression = analyze_file(source)["progression"]

    notes = [
        {"pitch": pitch, "start_beat": round(i * 0.8, 2), "beats": 0.5, "velocity": 80}
        for i, pitch in enumerate(MELODY)
    ]
    result = await _call("add_track", {"filename": "sketch.mid", "track_name": "Melody", "notes": notes})
    assert not result.isError
    data = result.structuredContent
    assert data["notes_added"] == 19
    assert data["total_tracks"] == 3

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    copy = store.root / data["filename"]
    note_tracks = [t for t in mido.MidiFile(copy).tracks if any(m.type == "note_on" for m in t)]
    assert len(note_tracks) == 2
    assert analyze_file(copy)["progression"] == original_progression


async def test_add_track_rejects_pitch_above_127(store):
    store.ensure_root()
    _write(store.root / "p.mid")
    result = await _call(
        "add_track",
        {
            "filename": "p.mid",
            "track_name": "Too high",
            "notes": [{"pitch": 128, "start_beat": 0, "beats": 1}],
        },
    )
    assert result.isError


async def test_add_track_drums_channel(store):
    store.ensure_root()
    _write(store.root / "beat.mid")
    notes = [{"pitch": 36, "start_beat": i, "beats": 0.25, "velocity": 100} for i in range(4)]
    result = await _call(
        "add_track",
        {"filename": "beat.mid", "track_name": "Drums", "notes": notes, "channel": 10},
    )
    assert not result.isError
    copy = store.root / result.structuredContent["filename"]
    channels = {m.channel for t in mido.MidiFile(copy).tracks for m in t if m.type == "note_on"}
    assert 9 in channels


# ------------------------------------------------------------------ midi_type


def test_midi_type_0_and_1_tempo(tmp_path):
    for midi_type in (0, 1):
        path = tmp_path / f"t{midi_type}.mid"
        info = _write(path, chords=("Am",) * 8, midi_type=midi_type, tempo_bpm=72)
        assert info["duration_seconds"] == 26.67
        midi = mido.MidiFile(path)
        assert midi.type == midi_type
        tempo = next(m.tempo for t in midi.tracks for m in t if m.type == "set_tempo")
        assert round(mido.tempo2bpm(tempo), 2) == 72.0
        assert abs(midi.length - 26.67) < 0.05
    assert len(mido.MidiFile(tmp_path / "t0.mid").tracks) == 1
    assert len(mido.MidiFile(tmp_path / "t1.mid").tracks) == 2


# ------------------------------------------------------------------ feel


def test_swing_moves_only_offbeats(tmp_path):
    def on_beats(swing):
        path = tmp_path / f"swing{swing}.mid"
        _write(
            path,
            chords=("C",),
            rhythm=Rhythm(pattern="pulse", subdivision=0.5, gate=0.5, swing=swing),
        )
        return _note_on_ticks(path)

    assert on_beats(0.0) == [0, 240, 480, 720, 960, 1200, 1440, 1680]
    assert on_beats(0.5) == [0, 360, 480, 840, 960, 1320, 1440, 1800]


def test_humanize_is_deterministic_and_in_range(tmp_path):
    def render(seed):
        path = tmp_path / f"h{seed}.mid"
        _write(
            path,
            chords=("C",) * 4,
            rhythm=Rhythm(pattern="pulse", humanize=Humanize(timing_ms=10, velocity_range=20, seed=seed)),
        )
        return path.read_bytes()

    assert render(42) == render(42)
    midi = mido.MidiFile(tmp_path / "h42.mid")
    velocities = [m.velocity for t in midi.tracks for m in t if m.type == "note_on"]
    assert velocities and all(1 <= v <= 127 for v in velocities)


async def test_lofi_preset_is_soft_and_swung(store):
    store.ensure_root()
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am7", "Dm7", "G7", "Cmaj7"], "filename": "lofi", "preset": "lofi"},
    )
    assert not result.isError
    midi = mido.MidiFile(store.root / "lofi.mid")
    velocities = [m.velocity for t in midi.tracks for m in t if m.type == "note_on"]
    assert max(velocities) <= 80  # 70 + humanize
    ticks = _note_on_ticks(store.root / "lofi.mid")
    assert any(tick % 240 for tick in ticks)  # swung/humanized offbeats


# ------------------------------------------------------------------ file management


async def test_delete_and_rename(store):
    store.ensure_root()
    _write(store.root / "old.mid")
    renamed = await _call("rename_midi_file", {"filename": "old.mid", "new_name": "new_song"})
    assert renamed.structuredContent["filename"] == "new_song.mid"
    assert not (store.root / "old.mid").exists()

    _write(store.root / "other.mid")
    conflict = await _call("rename_midi_file", {"filename": "other.mid", "new_name": "new_song"})
    assert conflict.isError
    assert "already exists" in conflict.content[0].text

    trick = await _call("rename_midi_file", {"filename": "new_song.mid", "new_name": "../escape"})
    assert not trick.isError
    assert (store.root / "escape.mid").is_file()
    assert not (store.root.parent / "escape.mid").exists()

    deleted = await _call("delete_midi_file", {"filename": "escape.mid"})
    assert deleted.structuredContent["deleted"] == "escape.mid"
    assert not (store.root / "escape.mid").exists()
    assert (await _call("delete_midi_file", {"filename": "escape.mid"})).isError


# ------------------------------------------------------------------ audio


def test_wav_duration(tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x00\x00" * 44100)
    assert audio.wav_duration(path) == 1.0
    audio.trim_wav(path, 0.25)
    assert audio.wav_duration(path) == 0.25


@pytest.mark.skipif(
    audio.find_fluidsynth() is None or audio.find_soundfont() is None,
    reason="FluidSynth or a soundfont is not installed",
)
async def test_render_audio_wav(store):
    store.ensure_root()
    info = _write(store.root / "song.mid")
    result = await _call("render_audio", {"filename": "song.mid", "format": "wav"})
    assert not result.isError
    data = result.structuredContent
    raw = base64.b64decode(data["data_base64"])
    assert raw[:4] == b"RIFF"
    assert data["mime_type"] == "audio/wav"
    assert data["sha256"] == hashlib.sha256(raw).hexdigest()
    assert abs(data["duration_seconds"] - info["duration_seconds"]) <= 0.5
