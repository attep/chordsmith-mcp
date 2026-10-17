"""Named drum grooves and chord-driven bass lines (add_drums / add_bassline)."""

from __future__ import annotations

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import grooves, server
from chordsmith.grooves import DrumPattern, bassline_notes, drum_notes
from chordsmith.midi_writer import add_track as add_track_midi
from chordsmith.midi_writer import write_empty
from chordsmith.theory import MusicTheoryError, parse_chord, parse_pitch

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


# ------------------------------------------------------------------ unit: patterns


def test_backbeat_hits_the_right_steps():
    notes = drum_notes(grooves.DRUM_PATTERNS["backbeat"])
    kick = sorted(start for pitch, start, _, _ in notes if pitch == 36)
    snare = sorted(start for pitch, start, _, _ in notes if pitch == 38)
    hats = sorted(start for pitch, start, _, _ in notes if pitch == 42)
    assert kick == [0.0, 2.0]
    assert snare == [1.0, 3.0]
    assert len(hats) == 8


def test_bossa_is_two_bars_and_waltz_is_three_beats():
    bossa = grooves.DRUM_PATTERNS["bossa_nova"]
    waltz = grooves.DRUM_PATTERNS["waltz"]
    assert bossa.bars == 2 and bossa.beats == 8
    assert waltz.numerator == 3 and waltz.beats == 3
    assert max(start for _, start, _, _ in drum_notes(bossa)) < bossa.beats


def test_accents_are_louder_and_bad_steps_are_rejected():
    notes = drum_notes(grooves.DRUM_PATTERNS["rock"])
    hat_velocity = sorted({velocity for pitch, _, _, velocity in notes if pitch == 42})
    assert hat_velocity == [80, 105]  # plain eighths and accented beats
    broken = DrumPattern("broken", 1, 4, "test", {36: ("x---", 90)})
    with pytest.raises(MusicTheoryError, match="16 are needed"):
        drum_notes(broken)
    odd = DrumPattern("odd", 1, 4, "test", {36: ("x?--------------", 90)})
    with pytest.raises(MusicTheoryError, match="Unknown step"):
        drum_notes(odd)


# ------------------------------------------------------------------ unit: bass


class _FakeChord:
    def __init__(self, root):
        self.root = root
        self.chord_type = parse_chord("C").chord_type


def test_bass_styles_alternate_the_right_notes():
    chord = parse_chord("Am")
    windows = [(0.0, 4.0, chord)]
    root = 12 * 3 + chord.root  # A2 = 45
    root_fifth = bassline_notes("root_fifth", windows)
    assert [pitch for pitch, _, _, _ in root_fifth[:4]] == [root, root + 7, root, root + 7]
    octave = bassline_notes("octave", windows)
    assert [pitch for pitch, _, _, _ in octave[:4]] == [root, root + 12, root, root + 12]
    held = bassline_notes("root", windows)
    assert len(held) == 1 and held[0][0] == root and held[0][2] == pytest.approx(4 * 0.9)
    walking = bassline_notes("walking", [(0.0, 4.0, chord), (4.0, 4.0, parse_chord("F"))])
    assert [pitch for pitch, _, _, _ in walking[:4]] == [root, root + 7, root + 3, 40]  # E2 approach


def test_bass_rejects_unknown_styles():
    with pytest.raises(MusicTheoryError, match="Unknown bass style"):
        bassline_notes("slap", [(0.0, 4.0, parse_chord("C"))])


def test_follow_style_locks_to_onsets():
    windows = [(0.0, 4.0, parse_chord("Am"))]
    notes = bassline_notes("follow", windows, onsets=[(0.0, 100), (0.75, 70), (2.5, 110)])
    assert [(pitch, start) for pitch, start, _, _ in notes] == [(45, 0.0), (45, 0.75), (45, 2.5)]
    assert [velocity for *_, velocity in notes] == [100, 70, 110]
    assert notes[0][2] == pytest.approx(0.75 * 0.9)  # length to the next onset x gate
    assert notes[2][2] == pytest.approx(1.5 * 0.9)  # last onset stretches to the chord end


def test_follow_style_holds_the_root_without_onsets():
    windows = [(0.0, 4.0, parse_chord("C")), (4.0, 4.0, parse_chord("G"))]
    notes = bassline_notes("follow", windows, onsets=[(0.0, 90)])
    assert [(pitch, start) for pitch, start, _, _ in notes] == [(36, 0.0), (43, 4.0)]


def test_follow_style_needs_onsets():
    with pytest.raises(MusicTheoryError, match="onsets"):
        bassline_notes("follow", [(0.0, 4.0, parse_chord("C"))])


# ------------------------------------------------------------------ tools


async def test_add_bassline_from_markers(store):
    await _call("create_chord_progression", {"chords": ["Am", "F", "C", "G"], "filename": "song"})
    result = await _call("add_bassline", {"filename": "song.mid", "style": "root"})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["chord_source"] == "markers"
    assert data["chords"] == ["Am", "F", "C", "G"]
    assert data["filename"] == "song_Bass.mid"
    midi = mido.MidiFile(store.root / "song_Bass.mid")
    bass = [track for track in midi.tracks if any(m.type == "track_name" and m.name == "Bass" for m in track)]
    assert len(bass) == 1
    pitches = [m.note for m in bass[0] if m.type == "note_on"]
    assert pitches == [45, 41, 36, 43]  # A2, F2, C2, G2
    assert data["notes_added"] == 4


async def test_add_bassline_falls_back_to_analysis(store):
    source = store.ensure_root() / "no_markers.mid"
    write_empty(source, "no_markers")
    notes = []
    for bar, pitches in enumerate((("C4", "E4", "G4"), ("F4", "A4", "C5"))):
        for pitch in pitches:
            notes.append((parse_pitch(pitch), bar * 4.0, 3.9, 90))
    add_track_midi(source, source, track_name="Chords", program=0, channel=0, notes=notes)
    result = await _call("add_bassline", {"filename": "no_markers.mid", "style": "root"})
    assert not result.isError, result.content[0].text
    assert result.structuredContent["chord_source"] == "analysis"
    assert result.structuredContent["chords"][:2] == ["C", "F"]


async def test_add_bassline_follow_style(store):
    await _call("create_chord_progression", {"chords": ["Am", "F"], "filename": "song"})
    await _call(
        "add_track",
        {
            "filename": "song.mid",
            "track_name": "Gtr",
            "notes": [
                {"pitch": "A2", "start_beat": 0, "beats": 0.5, "velocity": 100},
                {"pitch": "A2", "start_beat": 1.5, "beats": 0.5, "velocity": 80},
            ],
        },
    )
    result = await _call(
        "add_bassline", {"filename": "song_Gtr.mid", "style": "follow", "rhythm_track": "Gtr"}
    )
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["rhythm_track"] == "Gtr"
    assert data["notes_added"] == 3  # two onsets in Am, then F holds its root
    midi = mido.MidiFile(store.root / "song_Gtr_Bass.mid")
    bass = [t for t in midi.tracks if any(m.type == "track_name" and m.name == "Bass" for m in t)]
    ons = [(m.note, m.velocity) for m in bass[0] if m.type == "note_on"]
    assert ons == [(45, 100), (45, 80), (41, 90)]  # A2 twice at the Gtr velocities, F2 default


async def test_add_bassline_follow_without_rhythm_track_is_rejected(store):
    await _call("create_chord_progression", {"chords": ["C"], "filename": "song"})
    result = await _call("add_bassline", {"filename": "song.mid", "style": "follow"})
    assert result.isError and "rhythm_track" in result.content[0].text


async def test_add_drums_covers_the_bars(store):
    await _call("create_chord_progression", {"chords": ["Am", "F", "C", "G"], "filename": "song"})
    result = await _call("add_drums", {"filename": "song.mid", "pattern": "backbeat"})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["total_bars"] == 4 and data["repeat"] == 4
    assert data["channel"] == 10 and data["notes_added"] == 48  # 12 hits per bar
    assert data["filename"] == "song_Drums.mid"
    midi = mido.MidiFile(store.root / "song_Drums.mid")
    channels = {message.channel for track in midi.tracks for message in track if message.type == "note_on"}
    assert 9 in channels


async def test_add_drums_meter_must_match(store):
    await _call(
        "create_chord_progression",
        {"chords": ["Am", "F"], "filename": "waltz_song", "time_signature": "3/4"},
    )
    result = await _call("add_drums", {"filename": "waltz_song.mid", "pattern": "backbeat"})
    assert result.isError
    assert "written in 4/4" in result.content[0].text and "waltz" in result.content[0].text
    ok = await _call("add_drums", {"filename": "waltz_song.mid", "pattern": "waltz"})
    assert not ok.isError, ok.content[0].text
    assert ok.structuredContent["total_bars"] == 2 and ok.structuredContent["repeat"] == 2


async def test_add_drums_follows_the_section_map(store):
    await _call(
        "create_empty_file",
        {
            "filename": "structured",
            "sections": [
                {"name": "Intro", "bars": 2, "bpm": 100},
                {"name": "Chorus", "bars": 6, "bpm": 120},
            ],
        },
    )
    result = await _call("add_drums", {"filename": "structured.mid", "pattern": "four_on_floor"})
    assert not result.isError, result.content[0].text
    assert result.structuredContent["total_bars"] == 8
    assert result.structuredContent["repeat"] == 8
    meter_change = await _call(
        "set_song_sections",
        {
            "filename": "structured.mid",
            "sections": [
                {"name": "A", "bars": 2, "bpm": 100},
                {"name": "B", "bars": 2, "bpm": 100, "numerator": 7, "denominator": 8},
            ],
        },
    )
    assert not meter_change.isError
    rejected = await _call("add_drums", {"filename": "structured.mid", "pattern": "backbeat"})
    assert rejected.isError and "change meter" in rejected.content[0].text
