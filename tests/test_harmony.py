"""Roman-numeral analysis (theory/harmony) and the explain_progression tool."""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import harmony, server
from chordsmith.theory import chord_to_numeral, parse_chord, parse_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


# ------------------------------------------------------------------ numerals


def test_numerals_in_major_and_minor():
    major = parse_key("C major")
    assert chord_to_numeral(parse_chord("C"), major) == "I"
    assert chord_to_numeral(parse_chord("Am"), major) == "vi"
    assert chord_to_numeral(parse_chord("F"), major) == "IV"
    assert chord_to_numeral(parse_chord("G7"), major) == "V7"
    assert chord_to_numeral(parse_chord("Cmaj7"), major) == "Imaj7"
    assert chord_to_numeral(parse_chord("Dm7"), major) == "ii7"
    assert chord_to_numeral(parse_chord("Bm7b5"), major) == "viiø7"
    assert chord_to_numeral(parse_chord("Bb"), major) == "bVII"
    assert chord_to_numeral(parse_chord("F#dim"), major) == "#iv°"

    minor = parse_key("A minor")
    assert chord_to_numeral(parse_chord("Am"), minor) == "i"
    assert chord_to_numeral(parse_chord("C"), minor) == "III"
    assert chord_to_numeral(parse_chord("G"), minor) == "VII"
    assert chord_to_numeral(parse_chord("E7"), minor) == "V7"  # the raised leading tone stays V
    assert chord_to_numeral(parse_chord("G#dim"), minor) == "vii°"  # harmonic-minor spelling


def test_secondary_dominants():
    key = parse_key("C major")
    assert chord_to_numeral(parse_chord("D7"), key) == "V7/V"
    assert chord_to_numeral(parse_chord("A7"), key) == "V7/ii"
    assert chord_to_numeral(parse_chord("C7"), key) == "V7/IV"
    assert chord_to_numeral(parse_chord("E7"), key) == "V7/vi"
    assert chord_to_numeral(parse_chord("D"), key) == "V/V"
    assert chord_to_numeral(parse_chord("F7"), key) == "IV7"  # no V of a diminished degree


def test_detect_key_reads_the_chord_set():
    assert harmony.detect_key([parse_chord(c) for c in ("Am", "F", "C", "G")]).name == "A minor"
    assert harmony.detect_key([parse_chord(c) for c in ("Dm7", "G7", "Cmaj7")]).name == "C major"
    assert harmony.detect_key([parse_chord(c) for c in ("Am", "Dm", "E7", "Am")]).name == "A minor"


# ------------------------------------------------------------------ tool


async def test_explain_progression_with_a_key(store):
    result = await _call("explain_progression", {"chords": "Am F C G", "key": "A minor"})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["key"] == "A minor" and data["key_source"] == "given"
    assert data["numerals"] == ["i", "VI", "III", "VII"]
    assert [entry["function"] for entry in data["analysis"]] == [
        "tonic",
        "subdominant",
        "tonic",
        "dominant",
    ]
    assert data["borrowed"] == [] and data["secondary_dominants"] == []
    assert 1 <= len(data["suggestions"]) <= 3


async def test_explain_progression_detects_key_and_cadences(store):
    result = await _call("explain_progression", {"chords": ["Dm7", "G7", "Cmaj7"]})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["key"] == "C major" and data["key_source"] == "detected"
    assert data["numerals"] == ["ii7", "V7", "Imaj7"]
    assert [cadence["kind"] for cadence in data["cadences"]] == ["ii-V-I"]

    half = await _call("explain_progression", {"chords": "C Am Dm G7"})
    assert not half.isError, half.content[0].text
    assert any(cadence["kind"] == "half" for cadence in half.structuredContent["cadences"])


async def test_explain_progression_flags_borrowed_and_secondary_chords(store):
    result = await _call("explain_progression", {"chords": "C Bb F G7", "key": "C major"})
    data = result.structuredContent
    assert data["borrowed"] == ["Bb"]
    assert data["numerals"] == ["I", "bVII", "IV", "V7"]
    secondary = await _call("explain_progression", {"chords": "C A7 Dm G7", "key": "C major"})
    assert secondary.structuredContent["secondary_dominants"] == ["A7"]
    assert "V7/ii" in secondary.structuredContent["numerals"]


async def test_explain_progression_rejects_bad_input(store):
    empty = await _call("explain_progression", {"chords": "   "})
    assert empty.isError and "at least one chord" in empty.content[0].text
    bad = await _call("explain_progression", {"chords": "C H7"})
    assert bad.isError and "H7" in bad.content[0].text


async def test_analyze_reports_numerals(store):
    await _call("create_chord_progression", {"chords": ["Am", "F", "C", "G"], "filename": "song"})
    detected = (await _call("analyze_midi", {"filename": "song.mid"})).structuredContent
    assert detected["key_used"] == "A minor" and detected["key_source"] == "detected"
    assert detected["roman_numerals"] == ["i", "VI", "III", "VII"]
    override = (await _call("analyze_midi", {"filename": "song.mid", "key": "C major"})).structuredContent
    assert override["key_source"] == "given"
    assert override["roman_numerals"] == ["vi", "IV", "I", "V"]
