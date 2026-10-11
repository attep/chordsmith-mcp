"""Tests for song sections: the tempo/meter map in the conductor and section-relative notes."""

from __future__ import annotations

import json

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, sections, server, singing

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


async def _make_song(store, filename="song"):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": filename, "tempo": 120},
    )
    assert not result.isError, result.content[0].text
    return result.structuredContent["filename"]


SECTIONS = [
    {"name": "Intro", "bars": 8, "bpm": 140},
    {"name": "Chorus", "bars": 16, "bpm": 140, "numerator": 7, "denominator": 8, "grouping": "2+2+3"},
]


def _conductor_events(path):
    midi = mido.MidiFile(path)
    tick = 0
    events = []
    for message in midi.tracks[0]:
        tick += message.time
        if message.type in ("set_tempo", "time_signature", "marker"):
            events.append((tick, message))
    return events, midi.ticks_per_beat


def _note_ticks(path, track_name):
    midi = mido.MidiFile(path)
    for track in midi.tracks:
        name = next((m.name for m in track if m.type == "track_name"), "")
        if name == track_name:
            tick = 0
            notes = []
            for message in track:
                tick += message.time
                if message.type == "note_on" and message.velocity > 0:
                    notes.append((message.note, tick))
            return notes, midi.ticks_per_beat
    raise AssertionError(f"track '{track_name}' not found")


async def test_set_song_sections_writes_events_and_sidecar(store):
    song = await _make_song(store)
    result = await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [section["start_beat"] for section in data["sections"]] == [0.0, 32.0]
    assert data["total_beats"] == pytest.approx(32 + 56)
    assert data["duration_seconds"] == pytest.approx(88 * 60 / 140, abs=0.01)

    events, tpb = _conductor_events(store.root / song)
    tempos = [
        (tick, mido.tempo2bpm(message.tempo)) for tick, message in events if message.type == "set_tempo"
    ]
    assert [tick for tick, _ in tempos] == [0, 32 * tpb]
    assert [bpm for _, bpm in tempos] == pytest.approx([140.0, 140.0], abs=0.01)
    meters = [
        (tick, message.numerator, message.denominator)
        for tick, message in events
        if message.type == "time_signature"
    ]
    assert meters == [(0, 4, 4), (32 * tpb, 7, 8)]
    markers = [(tick, message.text) for tick, message in events if message.type == "marker"]
    assert (0, "section: Intro") in markers and (32 * tpb, "section: Chorus") in markers
    # the chord-name markers written by create_chord_progression are kept
    assert any(text == "Am" for _, text in markers)

    sidecar = store.root / f"{song[:-4]}.sections.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["sections"][1]["grouping"] == "2+2+3"

    # set_song_sections is idempotent: writing the same map again changes nothing
    again = await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    assert not again.isError
    events_again, _ = _conductor_events(store.root / song)
    assert [
        (tick, message.type, message.text if message.type == "marker" else "") for tick, message in events
    ] == [
        (tick, message.type, message.text if message.type == "marker" else "")
        for tick, message in events_again
    ]


async def test_read_sections_round_trips(store):
    song = await _make_song(store)
    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    resolved = sections.read_sections(store.root / song)
    assert [
        (s.name, s.start_beat, s.bars, s.bpm, s.numerator, s.denominator, s.grouping) for s in resolved
    ] == [
        ("Intro", 0.0, 8, 140.0, 4, 4, None),
        ("Chorus", 32.0, 16, 140.0, 7, 8, "2+2+3"),
    ]


async def test_read_sections_derives_from_events_without_sidecar(store):
    song = await _make_song(store)
    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    (store.root / f"{song[:-4]}.sections.json").unlink()
    resolved = sections.read_sections(store.root / song)
    assert [s.name for s in resolved] == ["Intro", "Chorus"]
    assert resolved[0].start_beat == 0.0 and resolved[0].bars == 8  # from the next marker
    assert resolved[1].start_beat == 32.0
    assert resolved[1].bpm == pytest.approx(140.0, abs=0.01)  # mido tempo storage rounds to us
    assert (resolved[1].numerator, resolved[1].denominator) == (7, 8)
    assert resolved[1].grouping is None  # grouping only lives in the sidecar


async def test_set_song_sections_rejects_duplicates_and_bad_grouping(store):
    song = await _make_song(store)
    duplicate = await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "A", "bars": 4, "bpm": 120}],
        },
    )
    assert duplicate.isError and "Duplicate section names" in duplicate.content[0].text
    bad_grouping = await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120, "numerator": 7, "grouping": "2+2"}],
        },
    )
    assert bad_grouping.isError and "grouping sums to 4" in bad_grouping.content[0].text


async def test_add_track_section_relative_positions(store):
    song = await _make_song(store)
    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    notes = [
        {"pitch": "C4", "section": "Intro", "bar": 1, "beat": 1, "beats": 1},
        {"pitch": "D4", "section": "Chorus", "bar": 1, "beat": 1, "beats": 1},
        {"pitch": "E4", "section": "Chorus", "bar": 2, "beat": 3.5, "beats": 0.5},
        {"pitch": "F4", "section": "Intro", "bar": 8, "beat": 4, "beats": 8},  # runs past the section
    ]
    added = await _call("add_track", {"filename": song, "track_name": "Bass", "notes": notes})
    assert not added.isError, added.content[0].text
    ticks, tpb = _note_ticks(store.root / added.structuredContent["filename"], "Bass")
    by_pitch = {note: tick for note, tick in ticks}
    assert by_pitch[60] == 0  # Intro bar 1 beat 1 = absolute beat 0
    assert by_pitch[62] == round(32 * tpb)  # Chorus start = 8 bars * 4 beats
    assert by_pitch[64] == round((32 + 3.5 + 2.5) * tpb)  # Chorus bar 2, beat 3.5 (7/8)
    assert by_pitch[65] == round(31 * tpb)  # Intro bar 8 beat 4 = 7*4 + 3 beats


async def test_add_track_section_validation(store):
    song = await _make_song(store)
    no_map = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [{"pitch": "C4", "section": "Intro", "bar": 1, "beats": 1}],
        },
    )
    assert no_map.isError and "no section map" in no_map.content[0].text

    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    unknown = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [{"pitch": "C4", "section": "Nope", "bar": 1, "beats": 1}],
        },
    )
    assert unknown.isError and "Unknown section 'Nope'" in unknown.content[0].text
    assert "Intro" in unknown.content[0].text

    past_end = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [{"pitch": "C4", "section": "Intro", "bar": 9, "beats": 1}],
        },
    )
    assert past_end.isError and "has 8 bars" in past_end.content[0].text

    both = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [{"pitch": "C4", "start_beat": 0, "section": "Intro", "bar": 1, "beats": 1}],
        },
    )
    assert both.isError and "not both" in both.content[0].text


async def test_conductor_ends_with_the_map(store):
    song = await _make_song(store)
    result = await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    # no track reaches the map's end, yet the file is exactly as long as the map (a stale
    # end_of_track from before the sections must not stretch it)
    assert audio.midi_duration(store.root / song) == pytest.approx(
        result.structuredContent["duration_seconds"], abs=0.05
    )


async def test_duration_and_vocals_follow_the_tempo_map(store):
    song = await _make_song(store)
    two_tempos = [
        {"name": "Slow", "bars": 4, "bpm": 60},
        {"name": "Fast", "bars": 4, "bpm": 120},
    ]
    result = await _call("set_song_sections", {"filename": song, "sections": two_tempos})
    assert result.structuredContent["duration_seconds"] == pytest.approx((16 * 60 / 60) + (16 * 60 / 120))

    notes = [
        {"pitch": "C5", "start_beat": 0, "beats": 1},
        {"pitch": "D5", "start_beat": 16, "beats": 1},
        {"pitch": "E5", "start_beat": 20, "beats": 12},  # runs to the end of the map (beat 32)
    ]
    added = await _call("add_track", {"filename": song, "track_name": "Melody", "notes": notes})
    path = store.root / added.structuredContent["filename"]

    # mido integrates the tempo map, so the audio length agrees with the map's own duration
    assert audio.midi_duration(path) == pytest.approx(16.0 + 8.0, abs=0.01)

    score = singing.build_score(path, "Melody", transpose=0)
    starts = [note.start_seconds for note in score.notes if not note.is_rest]
    # beat 16 = 16 s (Slow), beat 20 = 16 s + 4 beats at 120 BPM = 18 s
    assert starts == pytest.approx([0.0, 16.0, 18.0], abs=0.01)


async def test_analyze_reports_sections_and_changes(store):
    song = await _make_song(store)
    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    data = (await _call("analyze_midi", {"filename": song})).structuredContent
    assert [section["name"] for section in data["sections"]] == ["Intro", "Chorus"]
    assert [change["bpm"] for change in data["tempo_changes"]] == [140.0, 140.0]
    assert data["tempo_bpm"] == 140.0  # the headline tempo is the starting one
    assert [change["time_signature"] for change in data["time_signature_changes"]] == ["4/4", "7/8"]
    assert data["markers"] and all("section:" not in marker["text"] for marker in data["markers"])


async def test_section_sidecar_follows_rename_and_delete(store):
    song = await _make_song(store)
    await _call("set_song_sections", {"filename": song, "sections": SECTIONS})
    renamed = (await _call("rename_midi_file", {"filename": song, "new_name": "song_v2"})).structuredContent
    assert not (store.root / f"{song[:-4]}.sections.json").exists()
    assert (store.root / "song_v2.sections.json").is_file()
    await _call("delete_midi_file", {"filename": renamed["filename"]})
    assert not (store.root / "song_v2.sections.json").exists()
