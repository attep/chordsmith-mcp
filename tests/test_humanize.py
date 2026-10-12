"""Tests for add_track swing and seeded humanize (played-feel loops)."""

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
        {"chords": ["Am"], "filename": "song", "tempo": 120},
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


async def test_swing_delays_offbeats_only(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Hats",
            "patterns": [{"pitch": 42, "steps": "xxxx----"}],
            "swing": 0.33,
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Hats")
    ticks = [tick for _, tick, _ in notes]
    # Steps 0-3 are 16ths at 0, 0.25, 0.5, 0.75 beats; the offbeats (0.25, 0.75) move by
    # 0.33 * 0.25 beats = 0.0825 beats (~40 ticks), the straight ones do not move.
    assert ticks == [0, round(0.3325 * tpb), round(0.5 * tpb), round(0.8325 * tpb)]


async def test_swing_leaves_straight_and_free_notes_untouched(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Melody",
            "notes": [
                {"pitch": "C5", "start_beat": 0, "beats": 0.5, "velocity": 90},
                {"pitch": "D5", "start_beat": 0.5, "beats": 0.5, "velocity": 90},
            ],
            "swing": 0.33,
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Melody")
    # 8th notes sit on even multiples of the 0.25 grid, so swing does not move them.
    assert [tick for _, tick, _ in notes] == [0, round(0.5 * tpb)]


async def test_humanize_is_seeded_bounded_and_reproducible(store):
    song = await _make_song(store)
    notes = [{"pitch": "C4", "start_beat": beat, "beats": 0.5, "velocity": 90} for beat in (0, 0.5, 1.0, 1.5)]

    async def render(seed, output):
        return await _call(
            "add_track",
            {
                "filename": song,
                "track_name": "Melody",
                "notes": notes,
                "humanize": {"timing_ms": 25, "velocity_range": 10, "seed": seed},
                "output_filename": output,
            },
        )

    first = await render(7, "take_a")
    second = await render(7, "take_b")
    assert not first.isError, first.content[0].text
    a = (store.root / first.structuredContent["filename"]).read_bytes()
    b = (store.root / second.structuredContent["filename"]).read_bytes()
    assert a == b  # the same seed gives the same file

    other = await render(8, "take_c")
    c = (store.root / other.structuredContent["filename"]).read_bytes()
    assert c != a

    rendered, tpb = _notes_of(store.root / first.structuredContent["filename"], "Melody")
    expected = [0, round(0.5 * tpb), round(1.0 * tpb), round(1.5 * tpb)]
    # 25 ms at 120 BPM is 0.05 beats = 24 ticks; tick rounding can add one.
    assert all(abs(tick - want) <= 25 for (_, tick, _), want in zip(rendered, expected, strict=True))
    assert all(80 <= velocity <= 100 for _, _, velocity in rendered)


async def test_humanize_varies_repeated_loops(store):
    song = await _make_song(store)
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "patterns": [{"pitch": 40, "steps": "x-x-", "velocity": 90}],
            "step_beats": 0.5,
            "loop": {"length_beats": 2, "times": 2},
            "humanize": {"timing_ms": 30, "velocity_range": 0, "seed": 3},
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Bass")
    ticks = [tick for _, tick, _ in notes]
    assert len(ticks) == 4
    assert (ticks[2], ticks[3]) != (ticks[0], ticks[1])  # the two repeats breathe differently
    base = (0, round(tpb), round(2 * tpb), round(3 * tpb))
    # 30 ms at 120 BPM is 0.06 beats = ~29 ticks.
    assert all(abs(tick - want) <= 29 for tick, want in zip(ticks, base, strict=True))


async def test_humanize_uses_the_section_tempo(store):
    song = await _make_song(store)
    set_map = await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [
                {"name": "Slow", "bars": 1, "bpm": 60},
                {"name": "Fast", "bars": 1, "bpm": 240},
            ],
        },
    )
    assert not set_map.isError, set_map.content[0].text
    added = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Pulse",
            "notes": [
                {"pitch": "C4", "start_beat": 1, "beats": 0.25, "velocity": 90},
                {"pitch": "C4", "start_beat": 5, "beats": 0.25, "velocity": 90},
            ],
            "humanize": {"timing_ms": 50, "velocity_range": 0, "seed": 2},
        },
    )
    assert not added.isError, added.content[0].text
    notes, tpb = _notes_of(store.root / added.structuredContent["filename"], "Pulse")
    slow_tick, fast_tick = notes[0][1], notes[1][1]
    # 50 ms is 0.05 beats at 60 BPM (24 ticks) but 0.2 beats at 240 BPM (96 ticks).
    assert abs(slow_tick - tpb) <= 25
    assert abs(fast_tick - 5 * tpb) <= 97
    assert abs(fast_tick - 5 * tpb) > 25  # the fast section really uses its own tempo


async def test_add_track_echoes_swing_and_humanize(store):
    song = await _make_song(store)
    echoed = await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Shaker",
            "patterns": [{"pitch": 70, "steps": "x-x-"}],
            "swing": 0.33,
            "humanize": {"timing_ms": 10, "velocity_range": 5, "seed": 7},
        },
    )
    assert not echoed.isError, echoed.content[0].text
    content = echoed.structuredContent
    assert content["swing"] == 0.33
    assert content["humanize"]["seed"] == 7
    assert content["humanize"]["timing_ms"] == 10

    plain = await _call(
        "add_track",
        {"filename": song, "track_name": "Plain", "patterns": [{"pitch": 70, "steps": "x---"}]},
    )
    assert plain.structuredContent["swing"] == 0
    assert plain.structuredContent["humanize"] is None
