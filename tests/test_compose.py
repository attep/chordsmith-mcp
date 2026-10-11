"""Tests for the composition additions: automation, tuplets, empty files, mute, section gains."""

from __future__ import annotations

import math
import os
import wave

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, diffsinger, renderers, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _ffmpeg_ready() -> bool:
    return audio.find_ffmpeg() is not None


def _voicebank_ready() -> bool:
    return bool(os.environ.get("CHORDSMITH_DIFFSINGER_VOICE")) and diffsinger.available()


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


async def _make_song(store, filename="song"):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": filename, "tempo": 120},
    )
    return result.structuredContent["filename"]


def _track_messages(path, track_name):
    midi = mido.MidiFile(path)
    for track in midi.tracks:
        if next((m.name for m in track if m.type == "track_name"), "") == track_name:
            tick = 0
            out = []
            for message in track:
                tick += message.time
                out.append((tick, message))
            return out, midi.ticks_per_beat
    raise AssertionError(f"track '{track_name}' not found")


async def test_add_track_automation_ramps(store):
    song = await _make_song(store)
    result = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Timpani",
            "notes": [{"pitch": "C2", "start_beat": 0, "beats": 4, "velocity": 100}],
            "automation": [{"cc": 11, "from_value": 40, "to_value": 110, "start_beat": 0, "end_beat": 4}],
        },
    )
    assert not result.isError, result.content[0].text
    assert result.structuredContent["automation_events"] > 8
    messages, tpb = _track_messages(store.root / result.structuredContent["filename"], "Timpani")
    values = [m.value for _, m in messages if m.type == "control_change" and m.control == 11]
    assert values[0] == 40 and values[-1] == 110
    assert values == sorted(values)  # a linear rise
    # the end lands exactly on beat 4
    last_tick = max(t for t, m in messages if m.type == "control_change")
    assert last_tick == 4 * tpb

    bad = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "notes": [{"pitch": "C2", "start_beat": 0, "beats": 1}],
            "automation": [{"cc": 11, "from_value": 0, "to_value": 1, "start_beat": 2, "end_beat": 1}],
        },
    )
    assert bad.isError and "after start_beat" in bad.content[0].text


async def test_add_track_tuplets_are_tick_exact(store):
    song = await _make_song(store)
    notes = [
        {"pitch": "C5", "beats": 0.667, "tuplet": {"count": 3, "in_beats": 2, "start_beat": 0, "slot": slot}}
        for slot in range(3)
    ]
    notes += [
        {"pitch": "E5", "beats": 1.0, "tuplet": {"count": 2, "in_beats": 3, "start_beat": 8, "slot": slot}}
        for slot in range(2)
    ]
    result = await _call("add_track", {"filename": song, "track_name": "Harp", "notes": notes})
    assert not result.isError, result.content[0].text
    messages, tpb = _track_messages(store.root / result.structuredContent["filename"], "Harp")
    starts = [tick for tick, m in messages if m.type == "note_on"]
    # 3:2 over 2 beats = 320 ticks each (480 tpb); 2:3 over 3 beats = 720 each, from beat 8
    assert starts == [0, 320, 640, 8 * tpb, 8 * tpb + 720]

    bad_slot = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "notes": [
                {"pitch": "C5", "beats": 1, "tuplet": {"count": 3, "in_beats": 2, "start_beat": 0, "slot": 3}}
            ],
        },
    )
    assert bad_slot.isError and "outside a 3-slot" in bad_slot.content[0].text
    two_modes = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "notes": [
                {
                    "pitch": "C5",
                    "beats": 1,
                    "start_beat": 0,
                    "tuplet": {"count": 3, "in_beats": 2, "start_beat": 0, "slot": 0},
                }
            ],
        },
    )
    assert two_modes.isError and "exactly one" in two_modes.content[0].text


async def test_create_empty_file_with_sections(store):
    result = await _call(
        "create_empty_file",
        {
            "filename": "blank",
            "sections": [{"name": "Intro", "bars": 4, "bpm": 90}, {"name": "Climax", "bars": 4, "bpm": 140}],
        },
    )
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [section["name"] for section in data["sections"]] == ["Intro", "Climax"]
    midi = mido.MidiFile(store.root / data["filename"])
    assert len(midi.tracks) == 1  # the conductor only
    # the file is immediately usable with section-relative notes
    added = await _call(
        "add_track",
        {
            "filename": data["filename"],
            "track_name": "Pad",
            "notes": [{"pitch": "C4", "section": "Climax", "bar": 1, "beats": 4}],
        },
    )
    assert not added.isError, added.content[0].text
    messages, tpb = _track_messages(store.root / added.structuredContent["filename"], "Pad")
    assert [tick for tick, m in messages if m.type == "note_on"] == [16 * tpb]


async def test_remove_track_writes_a_copy(store):
    song = await _make_song(store)
    notes = [{"pitch": "C5", "start_beat": 0, "beats": 1}]
    with_bass = (
        await _call("add_track", {"filename": song, "track_name": "Bass", "notes": notes})
    ).structuredContent["filename"]
    result = await _call("remove_track", {"filename": with_bass, "track_name": "Bass"})
    assert not result.isError, result.content[0].text
    copy = store.root / result.structuredContent["filename"]
    names = [
        next((m.name for m in track if m.type == "track_name"), "") for track in mido.MidiFile(copy).tracks
    ]
    assert "Bass" not in names
    # the original still has it
    original = store.root / with_bass
    assert any(
        next((m.name for m in track if m.type == "track_name"), "") == "Bass"
        for track in mido.MidiFile(original).tracks
    )
    missing = await _call("remove_track", {"filename": with_bass, "track_name": "Nope"})
    assert missing.isError and "No track named 'Nope'" in missing.content[0].text


async def test_mute_skips_renders_and_bakes_cc7(store, monkeypatch):
    class FakeRenderer:
        name = "fluidsynth"

        def __init__(self):
            self.rendered = []

        def available(self):
            return True

        def render(self, midi, spec, out, effects=None):
            names = [
                next((m.name for m in t if m.type == "track_name"), "") for t in mido.MidiFile(midi).tracks
            ]
            self.rendered.append(names[-1])
            with wave.open(str(out), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(44100)
                handle.writeframes(b"\x64\x00" * 4410)

    fake = FakeRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_stub(target))

    song = await _make_song(store)
    notes = [{"pitch": "C5", "start_beat": 0, "beats": 1}]
    with_melody = (
        await _call("add_track", {"filename": song, "track_name": "Melody", "notes": notes})
    ).structuredContent["filename"]
    stored = await _call(
        "set_track_instrument",
        {"filename": with_melody, "tracks": {"Melody": {"engine": "fluidsynth", "mute": True}}},
    )
    assert not stored.isError, stored.content[0].text
    assert stored.structuredContent["tracks"]["Melody"]["mute"] is True

    result = await _call("render_audio", {"filename": with_melody, "stems": True})
    assert not result.isError, result.content[0].text
    assert "Melody" not in fake.rendered  # muted: left out of the stems render
    assert {stem["track"] for stem in result.structuredContent["stems"]} == {"Chords"}

    baked = await _call("bake_instruments", {"filename": with_melody, "output_filename": "baked_mute"})
    assert not baked.isError, baked.content[0].text
    messages, _ = _track_messages(store.root / baked.structuredContent["filename"], "Melody")
    cc7 = [(m.control, m.value) for _, m in messages if m.type == "control_change" and m.control == 7]
    assert cc7 == [(7, 0)]


def _write_stub(path):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(b"\x64\x00" * 4410)


def test_section_gain_windows_follow_the_map():
    spec = renderers.InstrumentSpec(sections={"Climax": renderers.SectionInstrument(gain_db=-6.0)})
    timeline = [("Intro", 0.0, 19.2), ("Climax", 19.2, 46.6)]
    windows = renderers.section_gain_windows(spec, timeline)
    assert windows == [(19.2, 46.6, -6.0)]
    assert renderers.section_gain_windows(renderers.InstrumentSpec(), timeline) == []


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
def test_apply_section_gains_changes_only_its_window(tmp_path):
    stem = tmp_path / "stem.wav"
    rate = 44100
    with wave.open(str(stem), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((8000).to_bytes(2, "little", signed=True) * rate * 2)  # 2 s flat

    assert renderers.apply_section_gains(stem, [(0.0, 1.0, -6.0)], baseline_db=0.0) is True

    def rms_db(start, end):
        with wave.open(str(stem), "rb") as handle:
            handle.setpos(round(start * rate))
            frames = handle.readframes(round((end - start) * rate))
        values = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)]
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        return 20 * math.log10(rms / 32768.0)

    assert rms_db(0.1, 0.9) == pytest.approx(rms_db(1.1, 1.9) - 6.0, abs=0.3)
    # a no-op when the delta is negligible
    assert renderers.apply_section_gains(stem, [(0.0, 1.0, 0.0)], baseline_db=0.0) is False


@pytest.mark.skipif(not _voicebank_ready(), reason="DiffSinger voicebank not configured")
async def test_lyrics_phoneme_mode_and_custom_words(store):
    song = await _make_song(store)
    notes = [{"pitch": "C5", "start_beat": index, "beats": 0.9} for index in range(3)]
    added = (
        await _call("add_track", {"filename": song, "track_name": "Melody", "notes": notes})
    ).structuredContent["filename"]
    score = (await _call("prepare_vocal_score", {"filename": added, "track": "Melody"})).structuredContent

    phonemes = await _call(
        "map_vocal_lyrics",
        {"score_id": score["score_id"], "lyrics": "l-aa v-ey n-ah", "language": "phonemes"},
    )
    assert not phonemes.isError, phonemes.content[0].text
    sung = [note for note in phonemes.structuredContent["notes"] if note["kind"] == "note"]
    assert [note["phonemes"] for note in sung] == [["l", "aa"], ["v", "ey"], ["n", "ah"]]

    unknown = await _call(
        "map_vocal_lyrics",
        {"score_id": score["score_id"], "lyrics": "l-zz", "language": "phonemes"},
    )
    assert unknown.isError and "Unknown phoneme(s): zz" in unknown.content[0].text

    custom = await _call(
        "map_vocal_lyrics",
        {
            "score_id": score["score_id"],
            "lyrics": "lah vey nah",
            "language": "en",
            "words": {"lah": "l aa", "vey": "v ey", "nah": "n aa"},
        },
    )
    assert not custom.isError, custom.content[0].text
    sung = [note for note in custom.structuredContent["notes"] if note["kind"] == "note"]
    assert [note["phonemes"] for note in sung] == [["l", "aa"], ["v", "ey"], ["n", "aa"]]

    bad_word = await _call(
        "map_vocal_lyrics",
        {
            "score_id": score["score_id"],
            "lyrics": "lah",
            "language": "en",
            "words": {"lah": "l zz"},
        },
    )
    assert bad_word.isError and "not in this voicebank" in bad_word.content[0].text
