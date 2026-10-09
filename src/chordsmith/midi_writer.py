"""Turn chords into MIDI notes (voicing + rhythm) and write/transpose .mid files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mido

from chordsmith.models import Rhythm, Voicing
from chordsmith.theory import Chord, MusicTheoryError, midi_note_name, parse_chord

TICKS_PER_BEAT = 480
# Shifts that take C major to a flat key (Db, Eb, F, Ab, Bb); used to spell transposed chord names.
_FLAT_SHIFTS = {1, 3, 5, 8, 10}


@dataclass
class NoteEvent:
    start: int
    duration: int
    note: int
    velocity: int


def _fit_range(notes: list[int]) -> list[int]:
    while notes and min(notes) < 21:
        notes = [n + 12 for n in notes]
    while notes and max(notes) > 108:
        notes = [n - 12 for n in notes]
    return notes


def _shape(chord: Chord, octave: int, inversion: int, style: str) -> list[int]:
    base = 12 * (octave + 1) + chord.root
    notes = [base + i for i in chord.chord_type.intervals]
    for _ in range(min(inversion, len(notes) - 1)):
        notes.sort()
        notes.append(notes.pop(0) + 12)
    notes.sort()
    if len(notes) >= 3 and style == "open":
        notes[1] += 12
    elif len(notes) >= 3 and style == "drop2":
        notes[-2] -= 12
    return sorted(notes)


def _distance(a: list[int], b: list[int]) -> float:
    return sum(min(abs(x - y) for y in b) for x in a) + sum(min(abs(y - x) for x in a) for y in b)


def _add_bass(notes: list[int], bass_pc: int) -> list[int]:
    lowest = min(notes)
    gap = (lowest - bass_pc) % 12 or 12
    bass = lowest - gap
    if lowest - bass < 5:
        bass -= 12
    return [bass, *notes]


def voice_progression(chords: list[Chord], voicing: Voicing) -> list[tuple[int | None, list[int]]]:
    """Return (bass note or None, upper notes) for every chord."""
    result: list[tuple[int | None, list[int]]] = []
    previous: list[int] | None = None
    center = 12 * (voicing.octave + 1) + 6
    for chord in chords:
        if voicing.voice_leading and previous is not None:
            candidates = [
                _shape(chord, octave, inv, voicing.style)
                for octave in (voicing.octave - 1, voicing.octave, voicing.octave + 1)
                for inv in range(len(chord.chord_type.intervals))
            ]
            upper = min(
                candidates,
                key=lambda c: _distance(c, previous) + 0.5 * abs(sum(c) / len(c) - center),
            )
        else:
            upper = _shape(chord, voicing.octave, voicing.inversion, voicing.style)
        previous = upper
        bass_pc = chord.bass if chord.bass is not None else (chord.root if voicing.add_bass else None)
        if bass_pc is None:
            notes = _fit_range(upper)
            result.append((None, notes))
        else:
            notes = _fit_range(_add_bass(upper, bass_pc))
            result.append((notes[0], notes[1:]))
    return result


def _ms_to_ticks(ms: float, tempo_bpm: float) -> int:
    return round(ms / 1000 * tempo_bpm / 60 * TICKS_PER_BEAT)


def render_pattern(
    bass: int | None, upper: list[int], start: int, length: int, rhythm: Rhythm, tempo_bpm: float
) -> list[NoteEvent]:
    vel = rhythm.velocity
    step = max(1, round(rhythm.subdivision * TICKS_PER_BEAT))
    all_notes = ([bass] if bass is not None else []) + upper
    events: list[NoteEvent] = []

    def steps():
        t = start
        while t < start + length:
            yield t, min(step, start + length - t)
            t += step

    if rhythm.pattern == "block":
        dur = max(1, round(length * rhythm.gate))
        events += [NoteEvent(start, dur, n, vel) for n in all_notes]
    elif rhythm.pattern == "pulse":
        for t, span in steps():
            dur = max(1, round(span * rhythm.gate))
            events += [NoteEvent(t, dur, n, vel) for n in all_notes]
    elif rhythm.pattern == "strum":
        spread = _ms_to_ticks(rhythm.strum_spread_ms, tempo_bpm)
        for idx, (t, span) in enumerate(steps()):
            down = idx % 2 == 0
            order = all_notes if down else list(reversed(all_notes))
            hit_vel = vel if down else max(1, round(vel * 0.8))
            for k, n in enumerate(order):
                offset = min(k * spread, span - 1)
                dur = max(1, round(span * rhythm.gate) - offset)
                events.append(NoteEvent(t + offset, dur, n, hit_vel))
    else:
        if rhythm.pattern == "arpeggio_up":
            sequence = upper
        elif rhythm.pattern == "arpeggio_down":
            sequence = list(reversed(upper))
        elif rhythm.pattern == "arpeggio_updown":
            sequence = upper + list(reversed(upper))[1:-1] if len(upper) > 2 else upper
        else:  # alberti: low, high, middle, high
            sequence = [upper[0], upper[-1], upper[len(upper) // 2], upper[-1]]
        if bass is not None:
            events.append(NoteEvent(start, max(1, round(length * rhythm.gate)), bass, vel))
        for idx, (t, span) in enumerate(steps()):
            events.append(NoteEvent(t, max(1, round(span * rhythm.gate)), sequence[idx % len(sequence)], vel))
    return events


def write_progression(
    path: Path,
    chords: list[tuple[Chord, float]],
    *,
    tempo_bpm: float,
    time_signature: tuple[int, int],
    voicing: Voicing,
    rhythm: Rhythm,
    program: int,
    repeat: int = 1,
    title: str = "ChordSmith",
) -> dict:
    sequence = chords * repeat
    voiced = voice_progression([c for c, _ in sequence], voicing)

    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name=title, time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo_bpm), time=0))
    conductor.append(
        mido.MetaMessage("time_signature", numerator=time_signature[0], denominator=time_signature[1], time=0)
    )

    note_events: list[NoteEvent] = []
    markers: list[tuple[int, str]] = []
    summary = []
    tick = 0
    for (chord, beats), (bass, upper) in zip(sequence, voiced, strict=True):
        length = round(beats * TICKS_PER_BEAT)
        markers.append((tick, chord.symbol))
        note_events += render_pattern(bass, upper, tick, length, rhythm, tempo_bpm)
        summary.append(
            {
                "chord": chord.symbol,
                "beats": beats,
                "notes": [
                    midi_note_name(n, chord.prefer_flats)
                    for n in ([bass] if bass is not None else []) + upper
                ],
            }
        )
        tick += length

    last = 0
    for t, text in markers:
        conductor.append(mido.MetaMessage("marker", text=text, time=t - last))
        last = t
    conductor.append(mido.MetaMessage("end_of_track", time=max(0, tick - last)))

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name="Chords", time=0))
    track.append(mido.Message("program_change", program=program, channel=0, time=0))
    raw: list[tuple[int, int, mido.Message]] = []
    for ev in note_events:
        raw.append((ev.start, 1, mido.Message("note_on", note=ev.note, velocity=ev.velocity)))
        raw.append((ev.start + ev.duration, 0, mido.Message("note_off", note=ev.note, velocity=0)))
    raw.sort(key=lambda r: (r[0], r[1]))
    last = 0
    for t, _, msg in raw:
        track.append(msg.copy(time=t - last))
        last = t
    track.append(mido.MetaMessage("end_of_track", time=max(0, tick - last)))

    midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    midi.tracks.extend([conductor, track])
    midi.save(path)

    total_beats = tick / TICKS_PER_BEAT
    return {
        "total_beats": total_beats,
        "bars": round(total_beats / (time_signature[0] * 4 / time_signature[1]), 2),
        "duration_seconds": round(total_beats * 60 / tempo_bpm, 2),
        "chords": summary,
    }


def transpose_chord_symbol(symbol: str, semitones: int) -> str:
    """Transpose a chord symbol's root and bass; returns the input unchanged if not a chord."""
    from chordsmith.theory import note_name

    try:
        chord = parse_chord(symbol)
    except MusicTheoryError:
        return symbol
    flats = semitones % 12 in _FLAT_SHIFTS
    suffix = chord.chord_type.aliases[0]
    out = note_name(chord.root + semitones, flats) + suffix
    if chord.bass is not None:
        out += "/" + note_name(chord.bass + semitones, flats)
    return out


def transpose_file(source: Path, target: Path, semitones: int) -> dict:
    midi = mido.MidiFile(source)
    notes_changed = 0
    for track in midi.tracks:
        for i, msg in enumerate(track):
            if msg.type in ("note_on", "note_off") and msg.channel != 9:
                new_note = msg.note + semitones
                if not 0 <= new_note <= 127:
                    raise MusicTheoryError(
                        f"Transposing by {semitones} pushes note {msg.note} outside MIDI range 0-127."
                    )
                track[i] = msg.copy(note=new_note)
                notes_changed += 1
            elif msg.type == "marker":
                track[i] = msg.copy(text=transpose_chord_symbol(msg.text, semitones))
    midi.save(target)
    return {"notes_transposed": notes_changed // 2}
