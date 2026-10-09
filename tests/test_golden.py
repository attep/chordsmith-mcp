"""Golden-file tests: exact notes, timing, tempo and markers, plus edge cases.

These tests pin the deterministic output of the main tools (the "golden" values live right here
in the test), covering slash chords, extended chords, enharmonic spelling, time signatures and
the create -> analyze round trip for every chord type.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx
import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import delivery, server
from chordsmith.theory import CHORD_TYPES, MusicTheoryError, parse_chord, parse_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _summary(path: Path) -> dict:
    """Tempo, time signature, markers and note spans, straight from the written file."""
    midi = mido.MidiFile(path)
    tempo = None
    time_signature = None
    markers: list[tuple[int, str]] = []
    tick = 0
    for msg in midi.tracks[0]:
        tick += msg.time
        if msg.type == "set_tempo":
            tempo = round(mido.tempo2bpm(msg.tempo), 3)
        elif msg.type == "time_signature":
            time_signature = f"{msg.numerator}/{msg.denominator}"
        elif msg.type == "marker":
            markers.append((tick, msg.text))
    note_spans: list[list[tuple[int, int, int]]] = []
    for track in midi.tracks:
        spans: list[tuple[int, int, int]] = []
        tick = 0
        active: dict[int, list[int]] = {}
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                active.setdefault(msg.note, []).append(tick)
            elif msg.type in ("note_off", "note_on"):
                starts = active.get(msg.note)
                if starts:
                    spans.append((msg.note, starts.pop(0), tick))
        if spans:
            note_spans.append(sorted(spans))
    return {
        "tempo": tempo,
        "time_signature": time_signature,
        "markers": markers,
        "tracks": note_spans,
        "ticks_per_beat": midi.ticks_per_beat,
    }


# ------------------------------------------------------------------ golden values


async def test_golden_block_progression(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "golden_block", "tempo": 100},
    )
    assert not result.isError
    data = result.structuredContent
    assert data["total_beats"] == 16
    assert data["duration_seconds"] == 9.6
    assert [chord["notes"] for chord in data["chords"]] == [
        ["A4", "C5", "E5"],
        ["F4", "A4", "C5"],
        ["C4", "E4", "G4"],
        ["G4", "B4", "D5"],
    ]

    summary = _summary(store.root / "golden_block.mid")
    assert summary["tempo"] == 100
    assert summary["time_signature"] == "4/4"
    assert summary["ticks_per_beat"] == 480
    assert summary["markers"] == [(0, "Am"), (1920, "F"), (3840, "C"), (5760, "G")]
    chord_track = summary["tracks"][0]
    assert len(chord_track) == 12
    starts = sorted({start for _, start, _ in chord_track})
    assert starts == [0, 1920, 3840, 5760]
    # block chords sound for gate (0.95) of the bar
    assert all(end - start == round(1920 * 0.95) for _, start, end in chord_track)
    assert sorted({note for note, _, _ in chord_track}) == [60, 64, 65, 67, 69, 71, 72, 74, 76]


async def test_golden_slash_chord(store):
    result = await _call(
        "create_chord_progression", {"chords": ["C/E"], "filename": "golden_slash", "tempo": 120}
    )
    assert result.structuredContent["chords"][0]["notes"] == ["E3", "C4", "E4", "G4"]
    summary = _summary(store.root / "golden_slash.mid")
    assert summary["markers"] == [(0, "C/E")]
    assert summary["tracks"][0][0] == (52, 0, 1824)  # E3 bass, held for the bar


async def test_golden_extended_chords(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["C9", "F#m11", "Bbmaj13"], "filename": "golden_ext", "tempo": 120},
    )
    assert [chord["notes"] for chord in result.structuredContent["chords"]] == [
        ["C4", "E4", "G4", "Bb4", "D5"],
        ["F#4", "A4", "C#5", "E5", "G#5", "B5"],
        ["Bb4", "D5", "F5", "A5", "C6", "G6"],
    ]


async def test_golden_enharmonic_spelling(store):
    result = await _call(
        "create_chord_progression",
        {"chords": ["Dbmaj7", "C#maj7"], "filename": "golden_enharmonic", "tempo": 120},
    )
    assert [chord["notes"] for chord in result.structuredContent["chords"]] == [
        ["Db4", "F4", "Ab4", "C5"],
        ["C#4", "E#4", "G#4", "B#4"],  # B#4 is MIDI 72, one letter above B4
    ]
    assert parse_chord("Cdim7").note_names == ["C", "Eb", "Gb", "Bbb"]


@pytest.mark.parametrize(
    ("time_signature", "beats", "total_beats", "bars"),
    [("3/4", 3, 9, 3), ("6/8", 3, 9, 3), ("7/8", 3.5, 10.5, 3)],
)
async def test_golden_time_signatures(store, time_signature, beats, total_beats, bars):
    name = f"golden_{time_signature.replace('/', '_')}"
    result = await _call(
        "create_chord_progression",
        {"chords": ["C", "G", "Am"], "filename": name, "time_signature": time_signature, "tempo": 120},
    )
    data = result.structuredContent
    assert data["total_beats"] == total_beats
    assert data["bars"] == bars
    assert data["chords"][0]["beats"] == beats

    analysis = await _call("analyze_midi", {"filename": f"{name}.mid"})
    assert analysis.structuredContent["time_signature"] == time_signature
    assert analysis.structuredContent["progression"] == ["C", "G", "Am"]


async def test_golden_analysis(store):
    await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "golden_analysis", "tempo": 90},
    )
    analysis = (await _call("analyze_midi", {"filename": "golden_analysis.mid"})).structuredContent
    assert analysis["tempo_bpm"] == 90
    assert analysis["time_signature"] == "4/4"
    # the analyzer measures up to the last note-off (gate 0.95 of the final bar)
    assert analysis["total_beats"] == 15.8
    assert analysis["progression"] == ["Am", "F", "C", "G"]
    assert [marker["text"] for marker in analysis["markers"]] == ["Am", "F", "C", "G"]


async def test_golden_roman_progression(store):
    result = await _call(
        "create_progression_from_roman",
        {"numerals": "i-VI-III-VII", "key": "A minor", "filename": "golden_roman", "tempo": 120},
    )
    data = result.structuredContent
    assert data["resolved_chords"] == ["Am", "F", "C", "G"]
    summary = _summary(store.root / "golden_roman.mid")
    assert [text for _, text in summary["markers"]] == ["Am", "F", "C", "G"]


async def test_golden_add_track_timing(store):
    store.ensure_root()
    await _call("create_chord_progression", {"chords": ["C"], "filename": "golden_track", "tempo": 120})
    melody = [
        {"pitch": "C5", "start_beat": 0, "beats": 1, "velocity": 80},
        {"pitch": "E5", "start_beat": 1.5, "beats": 0.5, "velocity": 90},
    ]
    result = await _call(
        "add_track",
        {"filename": "golden_track.mid", "track_name": "Melody", "notes": melody},
    )
    assert result.structuredContent["notes_added"] == 2
    summary = _summary(store.root / result.structuredContent["filename"])
    melody_spans = summary["tracks"][-1]
    assert melody_spans == [(72, 0, 480), (76, 720, 960)]  # beats * 480 ticks


# ------------------------------------------------------------------ determinism


async def test_golden_seed_determinism(store):
    args = {
        "chords": ["Am", "F", "C", "G"],
        "tempo": 120,
        "rhythm": {
            "pattern": "pulse",
            "subdivision": 0.5,
            "humanize": {"timing_ms": 10, "velocity_range": 10, "seed": 1},
        },
        "seed": 42,
    }
    first = await _call("create_chord_progression", {**args, "filename": "golden_seed", "overwrite": True})
    assert not first.isError
    hash_a = hashlib.sha256((store.root / "golden_seed.mid").read_bytes()).hexdigest()
    second = await _call("create_chord_progression", {**args, "filename": "golden_seed", "overwrite": True})
    assert not second.isError
    hash_b = hashlib.sha256((store.root / "golden_seed.mid").read_bytes()).hexdigest()
    assert hash_a == hash_b  # same seed -> same bytes

    other = await _call(
        "create_chord_progression",
        {**args, "filename": "golden_seed", "overwrite": True, "seed": 43},
    )
    assert not other.isError
    hash_c = hashlib.sha256((store.root / "golden_seed.mid").read_bytes()).hexdigest()
    assert hash_c != hash_a  # a different seed changes the humanization


# ------------------------------------------------------------------ round trips


@pytest.mark.parametrize("chord_type", CHORD_TYPES, ids=lambda ct: ct.name)
async def test_round_trip_every_chord_type(store, chord_type):
    symbol = "C" + chord_type.aliases[0]
    name = f"rt_{chord_type.name}".replace("/", "-")
    created = await _call("create_chord_progression", {"chords": [symbol], "filename": name, "tempo": 120})
    assert not created.isError
    analysis = await _call("analyze_midi", {"filename": f"{name}.mid"})
    assert not analysis.isError
    assert analysis.structuredContent["progression"] == [symbol]


async def test_round_trip_sevenths_and_transpose(store):
    created = await _call(
        "create_chord_progression",
        {"chords": ["Am7", "Dm7", "G7", "Cmaj7"], "filename": "rt_sevenths", "tempo": 72},
    )
    assert created.structuredContent["chords"][0]["notes"] == ["A4", "C5", "E5", "G5"]
    analysis = await _call("analyze_midi", {"filename": "rt_sevenths.mid"})
    assert analysis.structuredContent["progression"] == ["Am7", "Dm7", "G7", "Cmaj7"]

    transposed = await _call("transpose_midi", {"filename": "rt_sevenths.mid", "semitones": 2})
    analysis = await _call("analyze_midi", {"filename": transposed.structuredContent["filename"]})
    assert analysis.structuredContent["progression"] == ["Bm7", "Em7", "A7", "Dmaj7"]


# ------------------------------------------------------------------ error suggestions


def test_suggestions_in_messages():
    with pytest.raises(MusicTheoryError) as exc:
        parse_chord("Cm7b6")
    assert "Did you mean 'm7b5'?" in str(exc.value)

    with pytest.raises(MusicTheoryError) as exc:
        parse_key("C doriann")
    assert "Did you mean 'dorian'?" in str(exc.value)

    with pytest.raises(MusicTheoryError) as exc:
        parse_chord("Cxyz")
    assert "Call list_chord_types" in str(exc.value)


async def test_suggestions_through_tools(store):
    result = await _call("create_chord_progression", {"chords": ["Cm7b6"], "filename": "bad"})
    assert result.isError
    assert "Did you mean 'm7b5'?" in result.content[0].text


# ------------------------------------------------------------------ path tricks


async def test_path_tricks_are_contained(store):
    store.ensure_root()
    outside = store.root.parent / "secret.mid"
    outside.write_bytes(b"x")
    for bad in ("../secret.mid", "..\\secret.mid", str(outside), "sub/secret.mid"):
        assert (await _call("get_midi_file", {"filename": bad})).isError
        assert (await _call("delete_midi_file", {"filename": bad})).isError
        assert (await _call("rename_midi_file", {"filename": bad, "new_name": "x"})).isError
    assert outside.read_bytes() == b"x"  # never touched


async def test_download_route_rejects_traversal(store, monkeypatch):
    monkeypatch.setattr(delivery, "_secret", b"path-test")
    store.ensure_root()
    outside = store.root.parent / "secret.mid"
    outside.write_bytes(b"secret")
    expires = int(time.time()) + 60
    token = delivery.sign("../secret.mid", expires)
    transport = httpx.ASGITransport(app=server.mcp.streamable_http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.get("/files/..%2Fsecret.mid", params={"expires": expires, "token": token})
    assert response.status_code in (403, 404)  # a valid signature still cannot escape the folder
    assert outside.read_bytes() == b"secret"
