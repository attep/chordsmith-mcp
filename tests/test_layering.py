"""Tests for add_track copy_from/transpose_semitones (octave and grit layers)."""

from __future__ import annotations

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _notes_of(path, track_name):
    """(pitch, start_tick, duration_ticks, velocity) for a track's notes, in track order."""
    midi = mido.MidiFile(path)
    for track in midi.tracks:
        name = next((m.name for m in track if m.type == "track_name"), "")
        if name == track_name:
            tick = 0
            playing: dict[int, list[tuple[int, int]]] = {}
            notes = []
            for message in track:
                tick += message.time
                if message.type == "note_on" and message.velocity > 0:
                    playing.setdefault(message.note, []).append((tick, message.velocity))
                elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
                    queue = playing.get(message.note)
                    if queue:
                        start, velocity = queue.pop(0)
                        notes.append((message.note, start, tick - start, velocity))
            return notes, midi.ticks_per_beat
    raise AssertionError(f"track '{track_name}' not found")


async def _make_song(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am"], "filename": "song", "tempo": 120},
    )
    song = result.structuredContent["filename"]
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [
                {"pitch": "E2", "start_beat": 0, "beats": 1, "velocity": 90},
                {"pitch": "B2", "start_beat": 0, "beats": 1, "velocity": 100},
                {"pitch": "G2", "start_beat": 1, "beats": 0.5, "velocity": 80},
                {"pitch": "A2", "start_beat": 2, "beats": 2, "velocity": 90},
            ],
        },
    )
    assert not added.isError, added.content[0].text
    high = await _call(
        "add_track",
        {
            "filename": added.structuredContent["filename"],
            "track_name": "Bleep",
            "notes": [{"pitch": 120, "start_beat": 0, "beats": 1, "velocity": 90}],
        },
    )
    assert not high.isError, high.content[0].text
    return high.structuredContent["filename"]


async def test_copy_is_exact(store):
    song = await _make_song(store)
    copied = await _call(
        "add_track",
        {"filename": song, "track_name": "Bass Copy", "copy_from": "Bass", "channel": 5},
    )
    assert not copied.isError, copied.content[0].text
    content = copied.structuredContent
    assert content["copied_from"] == "Bass"
    assert content["transpose_semitones"] == 0
    path = store.root / content["filename"]
    source_notes, tpb = _notes_of(path, "Bass")
    copy_notes, _ = _notes_of(path, "Bass Copy")
    assert sorted(copy_notes) == sorted(source_notes)
    # durations survive the pairing (the 2-beat A2)
    assert max(duration for _, _, duration, _ in copy_notes) == round(2 * tpb)


async def test_copy_transposes_and_echoes(store):
    song = await _make_song(store)
    copied = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass Octave",
            "copy_from": "Bass",
            "transpose_semitones": 12,
            "channel": 5,
        },
    )
    assert not copied.isError, copied.content[0].text
    assert copied.structuredContent["transpose_semitones"] == 12
    path = store.root / copied.structuredContent["filename"]
    source_notes, _ = _notes_of(path, "Bass")
    octave_notes, _ = _notes_of(path, "Bass Octave")
    assert sorted(pitch for pitch, _, _, _ in octave_notes) == sorted(
        pitch + 12 for pitch, _, _, _ in source_notes
    )
    assert sorted((start, duration, velocity) for _, start, duration, velocity in octave_notes) == sorted(
        (start, duration, velocity) for _, start, duration, velocity in source_notes
    )


async def test_copy_validations(store):
    song = await _make_song(store)

    unknown = await _call("add_track", {"filename": song, "track_name": "X", "copy_from": "Nope"})
    assert unknown.isError and "No track named 'Nope'" in unknown.content[0].text

    both = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "copy_from": "Bass",
            "notes": [{"pitch": "C4", "start_beat": 0, "beats": 1}],
        },
    )
    assert both.isError and "not both" in both.content[0].text

    with_loop = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "copy_from": "Bass",
            "loop": {"length_beats": 4, "times": 2},
        },
    )
    assert with_loop.isError and "drop loop" in with_loop.content[0].text

    overflow = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "X",
            "copy_from": "Bleep",
            "transpose_semitones": 24,
        },
    )
    assert overflow.isError and "outside 0-127" in overflow.content[0].text
