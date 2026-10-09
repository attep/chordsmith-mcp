"""Turn chords into MIDI notes (voicing + rhythm) and write/transpose .mid files."""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import mido

from chordsmith.models import Humanize, Rhythm, Voicing
from chordsmith.theory import Chord, MusicTheoryError, midi_note_name, note_name_with_octave, parse_chord

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


def _note_label(note: int, chord: Chord) -> str:
    """Name a MIDI note using the chord's spelling (C7's seventh is Bb, not A#)."""
    spelled = dict(zip(chord.pitch_classes, chord.note_names, strict=True))
    name = spelled.get(note % 12)
    if name is None:
        return midi_note_name(note, chord.prefer_flats)
    return note_name_with_octave(name, note)


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
        index = 0
        t = start
        while t < start + length:
            shifted = t + (round(rhythm.swing * step) if index % 2 else 0)
            span = min(step, start + length - shifted)
            if span > 0:
                yield shifted, span
            t += step
            index += 1

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


def _humanize_events(
    events: list[NoteEvent],
    humanize: Humanize,
    tempo_bpm: float,
    rng: random.Random,
    window_start: int,
    window_end: int,
) -> None:
    """Apply deterministic timing/velocity variation (same seed -> same bytes).

    Timing shifts are clamped to the chord's own window so a humanized note never drifts into
    the next chord (which would confuse bar-by-bar analysis).
    """
    timing_ticks = _ms_to_ticks(humanize.timing_ms, tempo_bpm)
    for ev in events:
        if timing_ticks:
            shifted = ev.start + rng.randint(-timing_ticks, timing_ticks)
            ev.start = min(max(shifted, window_start), window_end - 1)
        if humanize.velocity_range:
            offset = rng.randint(-humanize.velocity_range, humanize.velocity_range)
            ev.velocity = min(127, max(1, ev.velocity + offset))


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
    midi_type: int = 1,
) -> dict:
    if midi_type not in (0, 1):
        raise ValueError("midi_type must be 0 or 1.")
    sequence = chords * repeat
    voiced = voice_progression([c for c, _ in sequence], voicing)

    note_events: list[NoteEvent] = []
    markers: list[tuple[int, str]] = []
    summary = []
    humanize_rng = random.Random(rhythm.humanize.seed) if rhythm.humanize else None
    tick = 0
    for (chord, beats), (bass, upper) in zip(sequence, voiced, strict=True):
        length = round(beats * TICKS_PER_BEAT)
        markers.append((tick, chord.symbol))
        chord_events = render_pattern(bass, upper, tick, length, rhythm, tempo_bpm)
        if humanize_rng is not None:
            _humanize_events(chord_events, rhythm.humanize, tempo_bpm, humanize_rng, tick, tick + length)
        note_events += chord_events
        summary.append(
            {
                "chord": chord.symbol,
                "beats": beats,
                "notes": [_note_label(n, chord) for n in ([bass] if bass is not None else []) + upper],
            }
        )
        tick += length

    raw: list[tuple[int, int, mido.Message]] = []
    for ev in note_events:
        raw.append((ev.start, 1, mido.Message("note_on", note=ev.note, velocity=ev.velocity)))
        raw.append((ev.start + ev.duration, 0, mido.Message("note_off", note=ev.note, velocity=0)))

    if midi_type == 0:
        merged: list[tuple[int, int, mido.Message | mido.MetaMessage]] = [
            (0, 0, mido.MetaMessage("track_name", name=title)),
            (0, 0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo_bpm))),
            (
                0,
                0,
                mido.MetaMessage(
                    "time_signature", numerator=time_signature[0], denominator=time_signature[1]
                ),
            ),
        ]
        merged += [(t, 0, mido.MetaMessage("marker", text=text)) for t, text in markers]
        merged.append((0, 1, mido.Message("program_change", program=program, channel=0)))
        merged += [(t, 2 if kind == 0 else 3, msg) for t, kind, msg in raw]
        merged.sort(key=lambda r: (r[0], r[1]))
        track = mido.MidiTrack()
        last = 0
        for t, _, msg in merged:
            track.append(msg.copy(time=max(0, t - last)))
            last = t
        track.append(mido.MetaMessage("end_of_track", time=max(0, tick - last)))
        midi = mido.MidiFile(type=0, ticks_per_beat=TICKS_PER_BEAT)
        midi.tracks.append(track)
    else:
        conductor = mido.MidiTrack()
        conductor.append(mido.MetaMessage("track_name", name=title, time=0))
        conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo_bpm), time=0))
        conductor.append(
            mido.MetaMessage(
                "time_signature", numerator=time_signature[0], denominator=time_signature[1], time=0
            )
        )
        last = 0
        for t, text in markers:
            conductor.append(mido.MetaMessage("marker", text=text, time=t - last))
            last = t
        conductor.append(mido.MetaMessage("end_of_track", time=max(0, tick - last)))

        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name="Chords", time=0))
        track.append(mido.Message("program_change", program=program, channel=0, time=0))
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
        "midi_type": midi_type,
        "total_beats": total_beats,
        "bars": round(total_beats / (time_signature[0] * 4 / time_signature[1]), 2),
        "duration_seconds": round(total_beats * 60 / tempo_bpm, 2),
        "chords": summary,
    }


def add_track(
    source: Path,
    target: Path,
    *,
    track_name: str,
    program: int,
    channel: int,
    notes: list[tuple[int, float, float, int]],
) -> dict:
    """Append a note-level track to a copy of ``source``.

    ``notes`` are ``(pitch, start_beat, beats, velocity)`` tuples. The original file is never
    modified; a type 0 file is promoted to type 1 so it can hold several tracks.
    """
    midi = mido.MidiFile(source)
    if midi.type == 0:
        midi.type = 1
    tpb = midi.ticks_per_beat
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=track_name, time=0))
    track.append(mido.Message("program_change", program=program, channel=channel, time=0))
    raw: list[tuple[int, int, mido.Message]] = []
    end = 0
    for pitch, start_beat, beats, velocity in notes:
        start = round(start_beat * tpb)
        duration = max(1, round(beats * tpb))
        raw.append((start, 1, mido.Message("note_on", note=pitch, velocity=velocity, channel=channel)))
        raw.append((start + duration, 0, mido.Message("note_off", note=pitch, velocity=0, channel=channel)))
        end = max(end, start + duration)
    raw.sort(key=lambda r: (r[0], r[1]))
    last = 0
    for t, _, msg in raw:
        track.append(msg.copy(time=max(0, t - last)))
        last = t
    track.append(mido.MetaMessage("end_of_track", time=max(0, end - last)))
    midi.tracks.append(track)
    midi.save(target)
    return {"notes_added": len(notes), "total_tracks": len(midi.tracks), "track_index": len(midi.tracks) - 1}


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
