"""Tests for DiffSinger chunk planning and stitching (the long-score memory fix)."""

from __future__ import annotations

import wave

import pytest

from chordsmith import diffsinger
from chordsmith.diffsinger import NoteSpec


def _note(start: float, seconds: float, midi: int = 60) -> NoteSpec:
    return NoteSpec(phonemes=["s", "ow"], start_seconds=start, seconds=seconds, midi=midi)


def test_plan_chunks_prefers_the_largest_rest():
    notes = [_note(0, 1), _note(2, 1), _note(20, 1), _note(21, 1)]  # a 17 s rest before 20
    chunks = diffsinger.plan_chunks(notes, max_seconds=10)
    assert [chunk.start_seconds for chunk in chunks] == [0.0, 20.0]
    assert [[note.start_seconds for note in chunk.notes] for chunk in chunks] == [[0.0, 2.0], [0.0, 1.0]]


def test_plan_chunks_hard_splits_continuous_phrases():
    notes = [_note(float(index), 1.0) for index in range(12)]  # no rests at all
    chunks = diffsinger.plan_chunks(notes, max_seconds=5)
    assert [chunk.start_seconds for chunk in chunks] == [0.0, 5.0, 10.0]
    assert [len(chunk.notes) for chunk in chunks] == [5, 5, 2]
    # boundaries land on note starts only, never inside a note
    for chunk in chunks:
        for note in chunk.notes:
            assert 0 <= note.start_seconds < 5


def test_plan_chunks_single_long_note_is_its_own_chunk():
    notes = [_note(0, 40, midi=48), _note(41, 1)]
    chunks = diffsinger.plan_chunks(notes, max_seconds=10)
    assert [len(chunk.notes) for chunk in chunks] == [1, 1]
    assert chunks[0].seconds == 40


def test_plan_chunks_ignores_tiny_gaps_as_split_points():
    # a 0.1 s gap is below min_gap, so the split happens at the hard boundary instead
    notes = [_note(0, 2), _note(2.1, 2), _note(4.2, 2), _note(6.3, 2)]
    chunks = diffsinger.plan_chunks(notes, max_seconds=5, min_gap=0.3)
    assert [len(chunk.notes) for chunk in chunks] == [2, 2]
    assert [chunk.start_seconds for chunk in chunks] == [0.0, 4.2]


def test_plan_chunks_empty():
    assert diffsinger.plan_chunks([]) == []


def _write_tone(path, seconds: float, amplitude: int = 8000, rate: int = 44100):
    frames = round(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little", signed=True) * frames)


def test_stitch_chunks_places_pieces_at_exact_offsets(tmp_path):
    pytest.importorskip("numpy")
    first = tmp_path / "a.wav"
    second = tmp_path / "b.wav"
    _write_tone(first, 1.0)
    _write_tone(second, 0.5)
    target = tmp_path / "stitched.wav"
    duration = diffsinger.stitch_chunks([(0.0, first), (2.0, second)], target)
    assert duration == pytest.approx(2.5, abs=0.01)
    with wave.open(str(target), "rb") as handle:
        samples = handle.readframes(handle.getnframes())
    assert len(samples) // 2 == round(2.5 * 44100)
    # the gap between the pieces is silence
    gap_start = round(1.1 * 44100) * 2
    gap_end = round(1.9 * 44100) * 2
    assert samples[gap_start:gap_end] == b"\x00" * (gap_end - gap_start)
