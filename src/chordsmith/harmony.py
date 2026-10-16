"""Deterministic harmonic analysis for explain_progression.

Everything here is rule-based: key detection scores the chords against every major/minor
scale, numerals come from theory.chord_to_numeral, functions from the scale degree, and the
cadences from the numeral pairs (with the ii-V-I triple recognised). No AI, same bytes for
the same input.
"""

from __future__ import annotations

import re

from chordsmith.theory import (
    SCALES,
    Chord,
    Key,
    MusicTheoryError,
    chord_degree,
    chord_to_numeral,
    note_name,
    parse_key,
    roman_to_chord,
)

_FLAT_TONICS = {1, 3, 5, 6, 8, 10}

# Degree (0-based) -> harmonic function, per mode family. In major, iii and vi are weak
# tonics (vi is the relative minor) and vii a leading-tone dominant; in minor, III is the
# relative major (tonic group), VI leans subdominant (plagal) and VII is the subtonic dominant.
_FUNCTIONS_MAJOR = {
    0: "tonic",
    1: "subdominant",
    2: "tonic",
    3: "subdominant",
    4: "dominant",
    5: "tonic",
    6: "dominant",
}
_FUNCTIONS_MINOR = {
    0: "tonic",
    1: "subdominant",
    2: "tonic",
    3: "subdominant",
    4: "dominant",
    5: "subdominant",
    6: "dominant",
}


def detect_key(chords: list[Chord]) -> Key:
    """The best-fitting major or minor key: roots and chord tones score against every scale."""
    scores: list[tuple[float, int, str]] = []
    for tonic in range(12):
        for mode in ("major", "minor"):
            scale = {(tonic + interval) % 12 for interval in SCALES[mode]}
            score = 0.0
            for chord in chords:
                if chord.root in scale:
                    score += 2.0
                tones = chord.pitch_classes
                score += sum(1.0 for pc in tones if pc in scale) / max(1, len(tones))
            if chords:
                if chords[0].root == tonic:
                    score += 0.5  # the opening chord usually states the key
                if chords[-1].root == tonic:
                    score += 1.0  # so does the final one, more strongly
            scores.append((score, tonic, mode))
    _, tonic, mode = max(scores, key=lambda entry: (entry[0], entry[2] == "major", -entry[1]))
    return parse_key(f"{note_name(tonic, tonic in _FLAT_TONICS)} {mode}")


def _bare(numeral: str) -> str:
    """The numeral without its suffix or secondary root: 'bVII7' -> 'bVII', 'V7/ii' -> 'V'."""
    match = re.match(r"^([b#]*[IViv]+)", numeral)
    return match.group(1) if match else numeral


def _is(numeral: str, base: str) -> bool:
    return _bare(numeral) == base


def _cadences(entries: list[dict]) -> list[dict]:
    """Adjacent-pair cadence detection over the numerals, with ii-V-I recognised as a triple."""
    numerals = [entry["numeral"] for entry in entries]
    cadences: list[dict] = []
    covered: set[int] = set()

    def add(index: int, kind: str, description: str) -> None:
        cadences.append(
            {
                "kind": kind,
                "from_chord": entries[index - 1]["chord"],
                "to_chord": entries[index]["chord"],
                "description": description,
            }
        )

    for index in range(2, len(numerals)):
        if (
            numerals[index - 2]
            and numerals[index - 1]
            and numerals[index]
            and _is(numerals[index - 2], "ii")
            and _is(numerals[index - 1], "V")
            and (_is(numerals[index], "I") or _is(numerals[index], "i"))
        ):
            add(index, "ii-V-I", "the strongest pull home: ii to V to the tonic")
            covered.add(index)
    for index in range(1, len(numerals)):
        if index in covered or not numerals[index - 1] or not numerals[index]:
            continue
        previous, current = numerals[index - 1], numerals[index]
        if _is(previous, "V") and (_is(current, "I") or _is(current, "i")):
            add(index, "authentic", "V resolving to the tonic: the classic close")
        elif _is(previous, "IV") or _is(previous, "iv"):
            if _is(current, "I") or _is(current, "i"):
                add(index, "plagal", "IV to I: the 'amen' close")
        elif _is(previous, "V") and (_is(current, "vi") or _is(current, "VI")):
            add(index, "deceptive", "V sidesteps to vi instead of the tonic")
    if numerals and numerals[-1] and _is(numerals[-1], "V"):
        cadences.append(
            {
                "kind": "half",
                "from_chord": entries[-1]["chord"],
                "to_chord": "",
                "description": "ends on the dominant: unresolved, wanting a return to the tonic",
            }
        )
    return cadences


def _suggestions(chords: list[Chord], key: Key, entries: list[dict]) -> list[str]:
    """A few deterministic variation ideas that reference the actual chords."""
    ideas: list[str] = []
    numerals = [entry["numeral"] for entry in entries]

    def chord_symbol(numeral: str) -> str | None:
        try:
            return roman_to_chord(numeral, key).symbol
        except MusicTheoryError:
            return None

    has_dominant = any(numeral and _is(numeral, "V") for numeral in numerals)
    has_two_before = any(
        numerals[index]
        and _is(numerals[index], "ii")
        and index + 1 < len(numerals)
        and numerals[index + 1]
        and _is(numerals[index + 1], "V")
        for index in range(len(numerals))
    )
    if has_dominant and not has_two_before:
        two = chord_symbol("ii7")
        if two:
            ideas.append(f"Insert {two} before the V for a stronger ii-V-I pull.")
    if any(numerals) and numerals[-1] and (_is(numerals[-1], "I") or _is(numerals[-1], "i")):
        six = chord_symbol("vi" if not key.is_minor_family else "VI")
        if six:
            ideas.append(f"Swap the final tonic for {six} for a softer, deceptive ending.")
    if not any("7" in numeral for numeral in numerals if numeral):
        first = chords[0]
        if first.chord_type.name == "maj":
            ideas.append(f"Try {first.symbol}maj7 for a warmer color on the first chord.")
        elif first.chord_type.name == "min":
            ideas.append(f"Try {first.symbol}7 for a warmer color on the first chord.")
    if not any(entry["function"] == "borrowed" for entry in entries):
        flat_seven = chord_symbol("bVII")
        if flat_seven:
            ideas.append(f"Borrow {flat_seven} (bVII) once for a lift toward the chorus.")
    return ideas[:3]


def explain(chords: list[Chord], key: Key, key_source: str = "given") -> dict:
    """The full analysis: per-chord entries, cadences, borrowed/secondary chords, ideas."""
    functions = _FUNCTIONS_MINOR if key.is_minor_family else _FUNCTIONS_MAJOR
    entries: list[dict] = []
    for chord in chords:
        numeral = chord_to_numeral(chord, key)
        placement = chord_degree(chord, key)
        if numeral and "/" in numeral:
            function = "secondary dominant"
        elif placement is None or placement[1]:
            function = "borrowed" if numeral else "outside the key"
        else:
            function = functions[placement[0]]
        entries.append({"chord": chord.symbol, "numeral": numeral, "function": function})
    return {
        "key": key.name,
        "key_source": key_source,
        "chords": [chord.symbol for chord in chords],
        "numerals": [entry["numeral"] for entry in entries],
        "analysis": entries,
        "cadences": _cadences(entries),
        "borrowed": [entry["chord"] for entry in entries if entry["function"] == "borrowed"],
        "secondary_dominants": [
            entry["chord"] for entry in entries if entry["function"] == "secondary dominant"
        ],
        "suggestions": _suggestions(chords, key, entries),
    }
