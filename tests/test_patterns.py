"""Tests for add_track step patterns (compact drum/rhythm entry)."""

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


async def _make_song(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "song", "tempo": 120},
    )
    return result.structuredContent["filename"]


def _notes_of(path, track_name):
    midi = mido.MidiFile(path)
    for track in midi.tracks:
        name = next((m.name for m in track if m.type == "track_name"), "")
        if name == track_name:
            tick = 0
            notes = []
            for message in track:
                tick += message.time
                if message.type == "note_on" and message.velocity > 0:
                    notes.append((message.note, tick, message.velocity))
            return notes, midi.ticks_per_beat
    raise AssertionError(f"track '{track_name}' not found")


async def test_add_track_pattern_rows(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Drums",
            "channel": 10,
            "patterns": [
                {"pitch": 36, "steps": "x---x---"},
                {"pitch": 38, "steps": "----x---", "velocity": 100},
                {"pitch": 42, "steps": "X-x-X-x-"},
            ],
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Drums")
    kicks = [(tick, velocity) for note, tick, velocity in notes if note == 36]
    snares = [(tick, velocity) for note, tick, velocity in notes if note == 38]
    hats = [(tick, velocity) for note, tick, velocity in notes if note == 42]
    assert kicks == [(0, 90), (tpb, 90)]  # steps 0 and 4 at a sixteenth each
    assert snares == [(tpb, 100)]
    assert hats == [(0, 115), (tpb // 2, 90), (tpb, 115), (3 * tpb // 2, 90)]  # X accents


async def test_add_track_pattern_bar_lines_do_not_consume_steps(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Kick",
            "channel": 10,
            "patterns": [{"pitch": 36, "steps": "x---|--x-"}],
        },
    )
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Kick")
    # 8 real steps in two bars: the hit after '|' is the 7th sixteenth (beat 1.5), not shifted
    assert [(tick, velocity) for _, tick, velocity in notes] == [(0, 90), (round(1.5 * tpb), 90)]


async def test_add_track_patterns_combine_with_notes_and_step_beats(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "patterns": [{"pitch": "C2", "steps": "x-x-", "velocity": 80}],
            "step_beats": 0.5,
            "notes": [{"pitch": "G2", "start_beat": 4, "beats": 1}],
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Bass")
    assert sorted(notes) == [(36, 0, 80), (36, tpb, 80), (43, 4 * tpb, 90)]


async def test_add_track_patterns_tile_with_loop(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Kick",
            "channel": 10,
            "patterns": [{"pitch": 36, "steps": "x---"}],
            "loop": {"length_beats": 1, "times": 4},
        },
    )
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Kick")
    assert [(tick, velocity) for _, tick, velocity in notes] == [
        (0, 90),
        (tpb, 90),
        (2 * tpb, 90),
        (3 * tpb, 90),
    ]


async def test_add_track_requires_notes_or_patterns_and_valid_steps(store):
    song = await _make_song(store)
    empty = await _call("add_track", {"filename": song, "track_name": "X"})
    assert empty.isError and "Give notes, patterns, or both" in empty.content[0].text
    bad_step = await _call(
        "add_track", {"filename": song, "track_name": "X", "patterns": [{"pitch": 36, "steps": "x?x-"}]}
    )
    assert bad_step.isError and "Unknown step '?'" in bad_step.content[0].text
