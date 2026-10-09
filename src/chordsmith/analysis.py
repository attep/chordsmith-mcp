"""Simple chord analysis of MIDI files.

Works best on block chords and arpeggios that change chord on a regular grid (for example files
written by ChordSmith). It is not a general-purpose harmonic analyser.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import mido

from chordsmith.theory import SCALES, identify_chord, note_name

_FLAT_MAJOR_TONICS = {1, 3, 5, 6, 8, 10}


def _collect_notes(midi: mido.MidiFile) -> tuple[list[tuple[int, int, int]], dict]:
    info: dict = {"tempo_bpm": 120.0, "time_signature": "4/4", "markers": []}
    notes: list[tuple[int, int, int]] = []
    for track in midi.tracks:
        tick = 0
        active: dict[tuple[int, int], list[int]] = defaultdict(list)
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo":
                info["tempo_bpm"] = round(mido.tempo2bpm(msg.tempo), 2)
            elif msg.type == "time_signature":
                info["time_signature"] = f"{msg.numerator}/{msg.denominator}"
            elif msg.type == "marker":
                info["markers"].append({"beat": round(tick / midi.ticks_per_beat, 3), "text": msg.text})
            elif msg.type == "note_on" and msg.velocity > 0 and msg.channel != 9:
                active[(msg.channel, msg.note)].append(tick)
            elif msg.type in ("note_off", "note_on") and msg.channel != 9:
                starts = active.get((msg.channel, msg.note))
                if starts:
                    notes.append((starts.pop(0), tick, msg.note))
    return notes, info


def analyze_file(path: Path, window_beats: float | None = None) -> dict:
    midi = mido.MidiFile(path)
    notes, info = _collect_notes(midi)
    tpb = midi.ticks_per_beat
    if window_beats is None:
        num, den = (int(x) for x in info["time_signature"].split("/"))
        window_beats = num * 4 / den
    window = max(1, round(window_beats * tpb))
    end = max((e for _, e, _ in notes), default=0)

    totals: dict[int, int] = defaultdict(int)
    for s, e, n in notes:
        totals[n % 12] += e - s
    major = SCALES["major"]
    tonic = max(range(12), key=lambda t: (sum(totals[(t + i) % 12] for i in major), -t))
    flats = tonic in _FLAT_MAJOR_TONICS
    key_guess = f"{note_name(tonic, flats)} major / {note_name(tonic + 9, flats)} minor" if notes else None

    segments: list[dict] = []
    for w_start in range(0, end, window):
        w_end = w_start + window
        weight: dict[int, int] = defaultdict(int)
        lowest = None
        for s, e, n in notes:
            overlap = min(e, w_end) - max(s, w_start)
            if overlap > 0:
                weight[n % 12] += overlap
                lowest = n if lowest is None or n < lowest else lowest
        pcs = {pc for pc, w in weight.items() if w >= window / 16}
        if not pcs:
            label, names = "N.C.", []
        else:
            bass_pc = lowest % 12 if lowest is not None else None
            found = identify_chord(pcs, bass_pc)
            names = [note_name(pc, flats) for pc in sorted(pcs)]
            if found is None:
                label = "?"
            else:
                root, ct = found
                label = note_name(root, flats) + ct.aliases[0]
                if bass_pc is not None and bass_pc != root:
                    label += "/" + note_name(bass_pc, flats)
        if segments and segments[-1]["chord"] == label:
            segments[-1]["beats"] += window_beats
        else:
            segments.append(
                {"start_beat": w_start / tpb, "beats": window_beats, "chord": label, "notes": names}
            )

    return {
        **info,
        "ticks_per_beat": tpb,
        "total_beats": round(end / tpb, 3),
        "window_beats": window_beats,
        "key_signature_guess": key_guess,
        "progression": [s["chord"] for s in segments],
        "segments": segments,
    }
