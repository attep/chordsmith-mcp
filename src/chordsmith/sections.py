"""Song sections: the tempo and meter map written into the MIDI conductor.

A section map lives in two places that serve different readers:

* the **MIDI conductor** carries the materialized events — at each section's start a
  ``time_signature``, a ``set_tempo`` and a ``marker`` named ``section: <name>`` — so renders,
  exports, ``analyze_midi`` and the vocal score (which already integrates tempo maps) follow it
  with no extra files;
* a small JSON sidecar (``<name>.sections.json``) stores the map's structure (name, bars, bpm,
  meter, grouping). ``read_sections`` prefers it — it is exact, including the last section's
  length, which the events alone cannot express — and falls back to deriving sections from the
  markers/tempo/meter events for files that were not written here.

Tempo is always **quarter-note BPM**; ``grouping`` (e.g. "2+2+3") is accent metadata for
compound meters and never changes a bar's length.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from chordsmith import results
from chordsmith.models import SongSection
from chordsmith.storage import FileStore

SECTION_MARKER_PREFIX = "section: "
SECTIONS_SUFFIX = ".sections.json"

_UPDATES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


@dataclass(frozen=True)
class ResolvedSection:
    """A section with its cumulative start position in quarter-note beats."""

    name: str
    start_beat: float
    bars: int
    bpm: float
    numerator: int
    denominator: int
    grouping: str | None

    @property
    def beats_per_bar(self) -> float:
        return self.numerator * 4 / self.denominator

    @property
    def total_beats(self) -> float:
        return self.bars * self.beats_per_bar

    def payload(self) -> dict:
        return {
            "name": self.name,
            "start_beat": round(self.start_beat, 3),
            "bars": self.bars,
            "bpm": self.bpm,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "grouping": self.grouping,
        }


def beats_per_bar(numerator: int, denominator: int) -> float:
    """Bar length in quarter-note beats, e.g. 7/8 -> 3.5."""
    return numerator * 4 / denominator


def sections_sidecar_path(midi_path: Path) -> Path:
    return midi_path.with_name(midi_path.stem + SECTIONS_SUFFIX)


def _write_sidecar(midi_path: Path, sections: list[SongSection]) -> None:
    payload = {"sections": [section.model_dump(exclude_none=True) for section in sections]}
    sections_sidecar_path(midi_path).write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _load_sidecar(midi_path: Path) -> list[SongSection]:
    sidecar = sections_sidecar_path(midi_path)
    if not sidecar.is_file():
        return []
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    return [SongSection.model_validate(entry) for entry in data.get("sections", [])]


def _resolve(sections: list[SongSection]) -> list[ResolvedSection]:
    resolved: list[ResolvedSection] = []
    start = 0.0
    for section in sections:
        resolved.append(
            ResolvedSection(
                name=section.name,
                start_beat=start,
                bars=section.bars,
                bpm=section.bpm,
                numerator=section.numerator,
                denominator=section.denominator,
                grouping=section.grouping,
            )
        )
        start += section.bars * beats_per_bar(section.numerator, section.denominator)
    return resolved


def _derive_from_events(midi: mido.MidiFile, midi_path: Path) -> list[ResolvedSection]:
    """Fallback: rebuild sections from markers/tempo/meter events (files not written here)."""
    tempos: list[tuple[int, float]] = []
    meters: list[tuple[int, int, int]] = []
    markers: list[tuple[int, str]] = []
    end_tick = 0
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "set_tempo":
                tempos.append((tick, mido.tempo2bpm(message.tempo)))
            elif message.type == "time_signature":
                meters.append((tick, message.numerator, message.denominator))
            elif message.type == "marker" and message.text.startswith(SECTION_MARKER_PREFIX):
                markers.append((tick, message.text[len(SECTION_MARKER_PREFIX) :].strip()))
        end_tick = max(end_tick, tick)
    if not markers:
        return []
    tempos.sort()
    meters.sort()
    markers.sort()
    ticks_per_beat = midi.ticks_per_beat
    resolved: list[ResolvedSection] = []
    for index, (tick, name) in enumerate(markers):
        bpm = next((value for event_tick, value in reversed(tempos) if event_tick <= tick), 120.0)
        numerator, denominator = next(
            ((num, den) for event_tick, num, den in reversed(meters) if event_tick <= tick), (4, 4)
        )
        bar_beats = beats_per_bar(numerator, denominator)
        next_tick = markers[index + 1][0] if index + 1 < len(markers) else end_tick
        bars = max(1, round((next_tick - tick) / ticks_per_beat / bar_beats))
        resolved.append(
            ResolvedSection(
                name=name,
                start_beat=tick / ticks_per_beat,
                bars=bars,
                bpm=bpm,
                numerator=numerator,
                denominator=denominator,
                grouping=None,
            )
        )
    return resolved


def read_sections(midi_path: Path) -> list[ResolvedSection]:
    """The song's section map: from the sidecar when present, else derived from the events."""
    midi = mido.MidiFile(midi_path)
    stored = _load_sidecar(midi_path)
    if stored:
        return _resolve(stored)
    return _derive_from_events(midi, midi_path)


def write_sections(midi_path: Path, sections: list[SongSection]) -> list[ResolvedSection]:
    """Write the section map into the file's conductor (in place) and its sidecar.

    Replaces any existing tempo/time-signature/``section:`` marker events in the conductor;
    other events (track name, chord markers, notes) are kept. Returns the resolved map.
    """
    resolved = _resolve(sections)
    midi = mido.MidiFile(midi_path)
    conductor = midi.tracks[0]
    kept: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []
    tick = 0
    for message in conductor:
        tick += message.time
        if message.type in ("set_tempo", "time_signature", "end_of_track"):
            continue
        if message.type == "marker" and message.text.startswith(SECTION_MARKER_PREFIX):
            continue
        kept.append((tick, 3, message))
    ticks_per_beat = midi.ticks_per_beat
    added: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []
    for section in resolved:
        start_tick = round(section.start_beat * ticks_per_beat)
        added.append(
            (
                start_tick,
                0,
                mido.MetaMessage(
                    "time_signature",
                    numerator=section.numerator,
                    denominator=section.denominator,
                    time=0,
                ),
            )
        )
        added.append(
            (start_tick, 1, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(section.bpm), time=0))
        )
        added.append(
            (start_tick, 2, mido.MetaMessage("marker", text=f"{SECTION_MARKER_PREFIX}{section.name}", time=0))
        )
    events = sorted(kept + added, key=lambda entry: (entry[0], entry[1]))
    new_conductor = mido.MidiTrack()
    last = 0
    for event_tick, _, message in events:
        new_conductor.append(message.copy(time=max(0, event_tick - last)))
        last = event_tick
    # The conductor ends where the map ends (a stale end_of_track from before the sections must
    # not stretch the song); kept content and note tracks past that point still set the length.
    map_end = round(sum(section.total_beats for section in resolved) * ticks_per_beat)
    end = max(last, map_end)
    new_conductor.append(mido.MetaMessage("end_of_track", time=max(0, end - last)))
    midi.tracks[0] = new_conductor
    midi.save(midi_path)
    _write_sidecar(midi_path, sections)
    return resolved


def section_duration_seconds(sections: list[ResolvedSection]) -> float:
    """Seconds covered by the map, integrating each section's tempo (no initial-BPM shortcut)."""
    total = 0.0
    for section in sections:
        total += section.total_beats * 60.0 / section.bpm
    return total


def register(mcp: FastMCP, store_provider) -> None:
    """Register the section tools on the ChordSmith server."""

    @mcp.tool(title="Set song sections", annotations=_UPDATES)
    def set_song_sections(
        filename: Annotated[str, Field(description="MIDI file in the output folder.")],
        sections: Annotated[
            list[SongSection],
            Field(
                min_length=1,
                max_length=64,
                description="Sections in order, e.g. [{'name': 'Intro', 'bars': 8, 'bpm': 140}, "
                "{'name': 'Chorus', 'bars': 16, 'bpm': 140, 'numerator': 7, 'denominator': 8, "
                "'grouping': '2+2+3'}].",
            ),
        ],
    ) -> results.SectionsResult:
        """Write the song's section map into the MIDI: tempo, meter and section markers (step 0 of composing).

        The file's conductor is updated **in place** (the one tool that edits a file's timing
        map; notes are untouched): each section's start gets a time signature, a tempo and a
        'section: <name>' marker, so renders, exports and the vocal score follow the map, and
        add_track can place notes with section-relative bar/beat positions. Replaces any existing
        section map. Tempo is always quarter-note BPM; a section's meter can differ from its
        neighbours. Returns each section's start beat, the total beats and the duration in
        seconds integrated over the whole map.
        """
        store: FileStore = store_provider()
        source = store.existing_path(filename)
        names = [section.name for section in sections]
        if len(set(names)) != len(names):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"Duplicate section names: {', '.join(duplicates)}.")
        resolved = write_sections(source, sections)
        return {
            "filename": source.name,
            "path": str(source),
            "sections": [section.payload() for section in resolved],
            "total_beats": round(sum(section.total_beats for section in resolved), 3),
            "duration_seconds": round(section_duration_seconds(resolved), 2),
        }
