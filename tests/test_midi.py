import mido
import pytest

from chordsmith.analysis import analyze_file
from chordsmith.midi_writer import (
    transpose_chord_symbol,
    transpose_file,
    voice_progression,
    write_progression,
)
from chordsmith.models import Rhythm, Voicing
from chordsmith.theory import parse_chord

PROGRESSION = ["Am", "F", "C", "G"]


def _write(path, rhythm=None, voicing=None, chords=PROGRESSION):
    return write_progression(
        path,
        [(parse_chord(c), 4) for c in chords],
        tempo_bpm=120,
        time_signature=(4, 4),
        voicing=voicing or Voicing(),
        rhythm=rhythm or Rhythm(),
        program=0,
    )


def test_block_chords_file_structure(tmp_path):
    path = tmp_path / "x.mid"
    info = _write(path)
    assert info["total_beats"] == 16
    assert info["duration_seconds"] == 8
    assert info["chords"][0]["notes"] == ["A4", "C5", "E5"]
    midi = mido.MidiFile(path)
    markers = [m.text for m in midi.tracks[0] if m.type == "marker"]
    assert markers == PROGRESSION
    ons = [m for m in midi.tracks[1] if m.type == "note_on"]
    offs = [m for m in midi.tracks[1] if m.type == "note_off"]
    assert len(ons) == len(offs) == 12


@pytest.mark.parametrize(
    "pattern", ["block", "pulse", "arpeggio_up", "arpeggio_down", "arpeggio_updown", "alberti", "strum"]
)
def test_patterns_round_trip_through_analysis(tmp_path, pattern):
    path = tmp_path / f"{pattern}.mid"
    _write(path, rhythm=Rhythm(pattern=pattern))
    assert analyze_file(path)["progression"] == PROGRESSION


def test_slash_chord_bass_is_lowest():
    ((bass, upper),) = voice_progression([parse_chord("C/E")], Voicing())
    assert bass % 12 == 4 and bass < min(upper)


def test_inversion_and_styles():
    c = parse_chord("C")
    assert voice_progression([c], Voicing(inversion=1))[0][1] == [64, 67, 72]
    assert voice_progression([c], Voicing(style="open"))[0][1] == [60, 67, 76]
    assert voice_progression([c], Voicing(style="drop2"))[0][1] == [52, 60, 67]


def test_voice_leading_moves_less():
    chords = [parse_chord(s) for s in ["C", "F", "G", "C"]]

    def movement(voicing):
        voiced = [u for _, u in voice_progression(chords, voicing)]
        return sum(
            abs(a - b) for x, y in zip(voiced, voiced[1:], strict=False) for a, b in zip(x, y, strict=True)
        )

    assert movement(Voicing(voice_leading=True)) < movement(Voicing())


def test_add_bass_and_analysis_label(tmp_path):
    path = tmp_path / "bass.mid"
    _write(path, voicing=Voicing(add_bass=True, voice_leading=True))
    assert analyze_file(path)["progression"] == PROGRESSION


def test_transpose(tmp_path):
    src, dst = tmp_path / "a.mid", tmp_path / "b.mid"
    _write(src)
    transpose_file(src, dst, 3)
    assert analyze_file(dst)["progression"] == ["Cm", "Ab", "Eb", "Bb"]
    markers = [m.text for m in mido.MidiFile(dst).tracks[0] if m.type == "marker"]
    assert markers == ["Cm", "Ab", "Eb", "Bb"]


def test_transpose_symbol():
    assert transpose_chord_symbol("Am7/G", 2) == "Bm7/A"
    assert transpose_chord_symbol("C", -2) == "Bb"
    assert transpose_chord_symbol("not a chord", 2) == "not a chord"
