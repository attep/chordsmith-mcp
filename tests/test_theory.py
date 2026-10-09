import pytest

from chordsmith.theory import (
    MusicTheoryError,
    identify_chord,
    parse_chord,
    parse_key,
    roman_to_chord,
    split_numerals,
)


@pytest.mark.parametrize(
    ("symbol", "quality", "notes"),
    [
        ("Am", "min", ["A", "C", "E"]),
        ("C", "maj", ["C", "E", "G"]),
        ("F#m7b5", "m7b5", ["F#", "A", "C", "E"]),
        ("Bbmaj7", "maj7", ["Bb", "D", "F", "A"]),
        ("Gsus", "sus4", ["G", "C", "D"]),
        ("C°", "dim", ["C", "Eb", "Gb"]),
        ("E♭7", "7", ["Eb", "G", "Bb", "Db"]),
        ("D6/9", "6/9", ["D", "F#", "A", "B", "E"]),
        ("CΔ7", "maj7", ["C", "E", "G", "B"]),
    ],
)
def test_parse_chord(symbol, quality, notes):
    chord = parse_chord(symbol)
    assert chord.chord_type.name == quality
    assert chord.note_names == notes


def test_slash_chord_bass():
    assert parse_chord("C/E").bass == 4


@pytest.mark.parametrize(
    ("symbol", "notes"),
    [
        ("C7", ["C", "E", "G", "Bb"]),
        ("D7", ["D", "F#", "A", "C"]),
        ("B7", ["B", "D#", "F#", "A"]),
        ("Eb7", ["Eb", "G", "Bb", "Db"]),
        ("F#m7b5", ["F#", "A", "C", "E"]),
        ("Cdim7", ["C", "Eb", "Gb", "Bbb"]),
        ("C7#9", ["C", "E", "G", "Bb", "D#"]),
        ("Abm", ["Ab", "Cb", "Eb"]),
        ("Bmaj7", ["B", "D#", "F#", "A#"]),
        ("D6/9", ["D", "F#", "A", "B", "E"]),
    ],
)
def test_chord_spelling(symbol, notes):
    assert parse_chord(symbol).note_names == notes


@pytest.mark.parametrize("bad", ["", "H7", "am", "Cxyz", "C/Q"])
def test_bad_chords_raise(bad):
    with pytest.raises(MusicTheoryError):
        parse_chord(bad)


def test_key_parsing():
    assert parse_key("Am").name == "A minor"
    assert parse_key("F sharp minor").scale_notes() == ["F#", "G#", "A", "B", "C#", "D", "E"]
    assert parse_key("Bb-dorian").scale_notes()[2] == "Db"
    assert parse_key("d minor").scale_notes()[5] == "Bb"
    with pytest.raises(MusicTheoryError):
        parse_key("C wobbly")


def _resolve(numerals, key):
    k = parse_key(key)
    return [roman_to_chord(n, k).symbol for n in split_numerals(numerals)]


def test_roman_minor():
    assert _resolve("i–VI–III–VII", "A minor") == ["Am", "F", "C", "G"]
    assert _resolve("iv V7 vii°", "A minor") == ["Dm", "E7", "G#dim"]


def test_roman_major_borrowed_and_secondary():
    assert _resolve("I vi IV V7", "C") == ["C", "Am", "F", "G7"]
    assert _resolve("bVII bVI V7/V viiø7", "C major") == ["Bb", "Ab", "D7", "Bm7b5"]


def test_roman_errors():
    with pytest.raises(MusicTheoryError):
        roman_to_chord("VIII", parse_key("C"))
    with pytest.raises(MusicTheoryError):
        roman_to_chord("Vi", parse_key("C"))
    with pytest.raises(MusicTheoryError):
        roman_to_chord("I", parse_key("C blues"))


def test_identify_chord_prefers_bass_root():
    root, ct = identify_chord({9, 0, 4, 7}, bass_pc=9)
    assert (root, ct.name) == (9, "m7")
    root, ct = identify_chord({9, 0, 4, 7}, bass_pc=0)
    assert (root, ct.name) == (0, "6")
