"""Singing integration (Sodium Gold, slice 1): a soft wordless hum via VOICEVOX.

Flow: prepare_vocal_score extracts a monophonic melody from a MIDI file (optionally transposed),
map_vocal_lyrics attaches one lyric per note (wordless mode sings "う" on every note),
render_singing runs VOICEVOX in a background job, and mix_song_with_vocals/export_vocal_song
render the backing without the guide track and mix everything together.

The engine sits behind a small adapter so other engines (DiffSinger, ...) can be added later.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from chordsmith import audio, delivery, diffsinger, results
from chordsmith.storage import FileStore
from chordsmith.theory import midi_note_name

logger = logging.getLogger(__name__)

FRAME_RATE = 93.75  # VOICEVOX sing frames per second
WORDLESS_LYRIC = "う"
MIN_FRAME_LENGTH = 8
DEFAULT_TRANSPOSE = -12
VOICEVOX_DEFAULT_URL = "http://127.0.0.1:50021"
REGISTRY_TTL_SECONDS = 24 * 3600
VOICEVOX_LICENCE = (
    "VOICEVOX engine is LGPL-3.0; every voice character has its own terms of use — "
    "check the character's terms at https://voicevox.hiroshiba.jp/ before publishing audio."
)
_KANA_RE = re.compile(r"^[\u3041-\u3096\u30a1-\u30fa\u30fc]+$")
_MELODY_NAME_RE = re.compile(r"melody|vocal|lead|voice|sing", re.IGNORECASE)

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_CREATES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


class SingingError(ValueError):
    """Raised when a singing request cannot be served."""


class VoicevoxError(SingingError):
    """Raised when the VOICEVOX engine cannot serve a request."""


# ------------------------------------------------------------------ VOICEVOX adapter


class VoicevoxClient:
    def __init__(self, base_url: str | None = None):
        configured = base_url or os.environ.get("CHORDSMITH_VOICEVOX_URL", "") or VOICEVOX_DEFAULT_URL
        self.base_url = configured.rstrip("/")

    def _post(self, path: str, params: dict, body: dict, timeout: float) -> httpx.Response:
        try:
            response = httpx.post(f"{self.base_url}{path}", params=params, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise VoicevoxError(
                f"VOICEVOX engine not reachable at {self.base_url} ({exc}). Start it with "
                "docker compose --profile singing up, or set CHORDSMITH_VOICEVOX_URL."
            ) from exc
        if response.status_code != 200:
            raise VoicevoxError(f"VOICEVOX {path} failed ({response.status_code}): {response.text[:300]}")
        return response

    def singers(self) -> list[dict]:
        try:
            response = httpx.get(f"{self.base_url}/singers", timeout=30)
        except httpx.HTTPError as exc:
            raise VoicevoxError(
                f"VOICEVOX engine not reachable at {self.base_url} ({exc}). Start it with "
                "docker compose --profile singing up, or set CHORDSMITH_VOICEVOX_URL."
            ) from exc
        if response.status_code != 200:
            raise VoicevoxError(f"VOICEVOX /singers failed ({response.status_code})")
        return response.json()

    def sing_frame_audio_query(self, score: dict, speaker: int, timeout: float = 300) -> dict:
        return self._post("/sing_frame_audio_query", {"speaker": speaker}, score, timeout).json()

    def frame_synthesis(self, query: dict, speaker: int, timeout: float = 600) -> bytes:
        return self._post("/frame_synthesis", {"speaker": speaker}, query, timeout).content


def get_client() -> VoicevoxClient:
    """Return the singing engine client (patched in tests)."""
    return VoicevoxClient()


# ------------------------------------------------------------------ registries


@dataclass
class ScoreNote:
    note_id: int
    key: int | None  # None for rests
    start_beat: float
    beats: float
    start_seconds: float
    seconds: float
    is_rest: bool


@dataclass
class VocalScore:
    score_id: str
    source: str
    track_index: int
    track_name: str
    tempo_bpm: float
    ticks_per_beat: int
    transpose: int
    notes: list[ScoreNote]
    warnings: list[str]
    created_at: float
    total_seconds: float = 0.0


@dataclass
class MappingNote:
    note_id: int
    key: int | None
    start_seconds: float
    seconds: float
    lyric: str
    is_rest: bool
    phonemes: list[str] | None = None


@dataclass
class VocalMapping:
    mapping_id: str
    score_id: str
    version: int
    voice_hint: str
    notes: list[MappingNote]
    total_seconds: float
    wordless: bool
    warnings: list[str]
    created_at: float


@dataclass
class SingingJob:
    job_id: str
    status: str  # queued | running | done | failed
    mapping_id: str
    voice_id: str
    settings: dict
    cache_key: str
    created_at: float
    echo_settings: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None


@dataclass
class _Registry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    scores: dict[str, VocalScore] = field(default_factory=dict)
    mappings: dict[str, VocalMapping] = field(default_factory=dict)
    jobs: dict[str, SingingJob] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)

    def prune(self) -> None:
        cutoff = time.time() - REGISTRY_TTL_SECONDS
        with self.lock:
            for store_dict in (self.scores, self.mappings, self.jobs):
                for key in [k for k, v in store_dict.items() if v.created_at < cutoff]:
                    del store_dict[key]


_registry = _Registry()
_get_store: Callable[[], FileStore] | None = None


# ------------------------------------------------------------------ score preparation


def _track_names(midi: mido.MidiFile) -> list[str]:
    names = []
    for track in midi.tracks:
        name = next((msg.name for msg in track if msg.type == "track_name"), "")
        names.append(name or "(unnamed)")
    return names


def find_track(midi: mido.MidiFile, track: str | int | None) -> int:
    """Resolve a track reference: 1-based index, name, or a melody-like name when None."""
    names = _track_names(midi)
    if track is None:
        for index, name in enumerate(names):
            if _MELODY_NAME_RE.search(name):
                return index
        raise SingingError(
            "No melody track found. Give 'track' as a name or 1-based index. "
            f"Tracks: {', '.join(f'{i + 1}={n}' for i, n in enumerate(names))}"
        )
    if isinstance(track, int):
        index = track - 1
        if not 0 <= index < len(midi.tracks):
            raise SingingError(f"Track {track} does not exist; the file has {len(midi.tracks)} tracks.")
        return index
    for index, name in enumerate(names):
        if name == track:
            return index
    for index, name in enumerate(names):
        if name.lower() == track.lower():
            return index
    raise SingingError(
        f"No track named '{track}'. Tracks: {', '.join(f'{i + 1}={n}' for i, n in enumerate(names))}"
    )


def _melody_notes(track: mido.MidiTrack, ticks_per_beat: int) -> list[tuple[int, int, int]]:
    """Return (start_tick, end_tick, note) for one track."""
    notes: list[tuple[int, int, int]] = []
    tick = 0
    active: dict[int, int] = {}
    for msg in track:
        tick += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            active[msg.note] = tick
        elif msg.type in ("note_off", "note_on"):
            start = active.pop(msg.note, None)
            if start is not None and tick > start:
                notes.append((start, tick, msg.note))
    notes.sort()
    return notes


def _tempo_map(midi: mido.MidiFile) -> list[tuple[int, float]]:
    """All tempo changes (tick, bpm), sorted; every tempo change is honoured."""
    events: dict[int, float] = {}
    for track in midi.tracks:
        tick = 0
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo" and tick not in events:
                events[tick] = mido.tempo2bpm(msg.tempo)
    points = sorted(events.items())
    if not points:
        return [(0, 120.0)]
    if points[0][0] != 0:
        points.insert(0, (0, points[0][1]))
    return points


def _tick_to_seconds(points: list[tuple[int, float]], tick: int, ticks_per_beat: int) -> float:
    seconds = 0.0
    for index, (start_tick, bpm) in enumerate(points):
        if tick <= start_tick:
            break
        end_tick = points[index + 1][0] if index + 1 < len(points) else tick
        seconds += (min(tick, end_tick) - start_tick) / ticks_per_beat * 60.0 / bpm
        if tick <= end_tick:
            break
    return seconds


def build_score(path: Path, track: str | int | None, transpose: int, legato: float = 0.0) -> VocalScore:
    midi = mido.MidiFile(path)
    track_index = find_track(midi, track)
    track_name = _track_names(midi)[track_index]
    tempos = _tempo_map(midi)
    tempo = tempos[0][1]
    raw = _melody_notes(midi.tracks[track_index], midi.ticks_per_beat)
    if not raw:
        raise SingingError(f"Track '{track_name}' has no notes.")
    last_end = raw[0][1]
    for start, end, _ in raw[1:]:
        if start < last_end - 1:
            raise SingingError(
                "The melody track is not one note at a time (notes overlap around tick "
                f"{start}). Pick a monophonic track."
            )
        last_end = max(last_end, end)

    warnings: list[str] = []
    notes: list[ScoreNote] = []
    note_id = 0
    cursor_tick = 0
    for start, end, pitch in raw:
        if start > cursor_tick:
            gap_beats = (start - cursor_tick) / midi.ticks_per_beat
            if legato and gap_beats <= legato and notes and not notes[-1].is_rest:
                # legato: the previous note runs on into this one instead of a silent gap
                previous = notes[-1]
                previous.beats += gap_beats
                previous.seconds = (
                    _tick_to_seconds(tempos, start, midi.ticks_per_beat) - previous.start_seconds
                )
            else:
                rest_seconds = _tick_to_seconds(tempos, start, midi.ticks_per_beat) - _tick_to_seconds(
                    tempos, cursor_tick, midi.ticks_per_beat
                )
                if rest_seconds > 0:
                    notes.append(
                        ScoreNote(
                            note_id,
                            None,
                            cursor_tick / midi.ticks_per_beat,
                            (start - cursor_tick) / midi.ticks_per_beat,
                            _tick_to_seconds(tempos, cursor_tick, midi.ticks_per_beat),
                            rest_seconds,
                            True,
                        )
                    )
                    note_id += 1
        key = pitch + transpose
        if not 0 <= key <= 127:
            raise SingingError(
                f"Transposing {midi_note_name(pitch)} by {transpose:+d} leaves the MIDI range 0-127."
            )
        seconds = _tick_to_seconds(tempos, end, midi.ticks_per_beat) - _tick_to_seconds(
            tempos, start, midi.ticks_per_beat
        )
        if seconds * FRAME_RATE < MIN_FRAME_LENGTH:
            warnings.append(f"Note at tick {start} is very short; engines may lengthen it slightly.")
        notes.append(
            ScoreNote(
                note_id,
                key,
                start / midi.ticks_per_beat,
                (end - start) / midi.ticks_per_beat,
                _tick_to_seconds(tempos, start, midi.ticks_per_beat),
                seconds,
                False,
            )
        )
        note_id += 1
        cursor_tick = end
    return VocalScore(
        score_id=f"score_{uuid.uuid4().hex[:10]}",
        source=path.name,
        track_index=track_index,
        track_name=track_name,
        tempo_bpm=round(tempo, 3),
        ticks_per_beat=midi.ticks_per_beat,
        transpose=transpose,
        notes=notes,
        warnings=warnings,
        created_at=time.time(),
        total_seconds=_tick_to_seconds(tempos, last_end, midi.ticks_per_beat),
    )


_FLAT_MAJOR_TONICS = {1, 3, 5, 6, 8, 10}  # Db Eb F Ab Bb Gb


def _score_prefers_flats(score: VocalScore) -> bool:
    """Spell the score like the chords: a flat key (e.g. C minor -> Eb major) reads Eb, not D#."""
    from collections import Counter

    from chordsmith.theory import SCALES

    totals: Counter[int] = Counter(note.key % 12 for note in score.notes if note.key is not None)
    major = SCALES["major"]
    tonic = max(range(12), key=lambda t: (sum(totals[(t + i) % 12] for i in major), -t))
    return tonic in _FLAT_MAJOR_TONICS


def _score_payload(score: VocalScore) -> dict:
    flats = _score_prefers_flats(score)
    return {
        "score_id": score.score_id,
        "source": score.source,
        "track": {"index": score.track_index + 1, "name": score.track_name},
        "tempo_bpm": score.tempo_bpm,
        "transpose": score.transpose,
        "total_seconds": round(score.total_seconds, 3),
        "warnings": score.warnings,
        "notes": [
            {
                "note_id": note.note_id,
                "kind": "rest" if note.is_rest else "note",
                "pitch": note.key,
                "name": midi_note_name(note.key, flats) if note.key is not None else None,
                "start_beat": round(note.start_beat, 4),
                "beats": round(note.beats, 4),
                "start_seconds": round(note.start_seconds, 4),
                "seconds": round(note.seconds, 4),
                "frames": round(note.seconds * FRAME_RATE) if not note.is_rest else None,
            }
            for note in score.notes
        ],
    }


# ------------------------------------------------------------------ lyrics


def _parse_syllables(lyrics: str, language: str) -> list[str]:
    if language not in ("ja", "oo", "wordless"):
        raise SingingError(
            f"Language '{language}' is not supported by the VOICEVOX adapter (Japanese kana only). "
            "Use language='ja' with kana lyrics, or omit lyrics for a wordless hum."
        )
    tokens = lyrics.split() if re.search(r"\s", lyrics) else list(lyrics)
    invalid = [token for token in tokens if not _KANA_RE.match(token)]
    if invalid:
        raise SingingError(
            f"These syllables are not Japanese kana: {', '.join(invalid)}. VOICEVOX sings one kana "
            "per note; use e.g. 'う う う' or omit lyrics for a wordless hum."
        )
    return tokens


def _truncate_groups(groups: list[list[str]], limit: int) -> list[list[str]]:
    """Keep the first `limit` flat tokens, preserving word groups as far as possible."""
    result: list[list[str]] = []
    count = 0
    for group in groups:
        if count >= limit:
            break
        take = group[: limit - count]
        result.append(take)
        count += len(take)
    return result


def _stress_warnings(
    groups: list[list[str]],
    remaining: list[ScoreNote],
    stress_per_note: dict[int, float | None],
    aligned: set[int] | None = None,
) -> list[str]:
    """Warn when a stressed syllable lands on a much shorter note than a weak one in its word."""
    warnings: list[str] = []
    index = 0
    for group in groups:
        if len(group) == 1 and group[0] in ("+", "-", "br"):
            index += 1
            continue
        if len(group) > 1:
            entries = []
            for offset, token in enumerate(group):
                position = index + offset
                if position >= len(remaining):
                    break
                entries.append(
                    (token, remaining[position].seconds, stress_per_note.get(remaining[position].note_id))
                )
            stressed = [(token, seconds) for token, seconds, level in entries if level == 1]
            weak = [(token, seconds) for token, seconds, level in entries if level == 0]
            if stressed and weak:
                stressed_token, stressed_seconds = min(stressed, key=lambda item: item[1])
                weak_token, weak_seconds = max(weak, key=lambda item: item[1])
                if stressed_seconds < 0.6 * weak_seconds:
                    suffix = (
                        " The note lengths were swapped (align_stress)."
                        if aligned
                        and any(note.note_id in aligned for note in remaining[index : index + len(group)])
                        else " Consider swapping the syllables, or pass align_stress to swap the "
                        "note lengths automatically."
                    )
                    warnings.append(
                        f"'{''.join(group)}': the stressed syllable '{stressed_token}' sits on a "
                        f"shorter note ({stressed_seconds:.2f}s) than the weak syllable "
                        f"'{weak_token}' ({weak_seconds:.2f}s).{suffix}"
                    )
        index += len(group)
    return warnings


def _align_stress(
    groups: list[list[str]], remaining: list[ScoreNote], stress_per_note: dict[int, float | None]
) -> dict[int, tuple[float, float]]:
    """Swap note lengths inside a word so stressed syllables get the longest notes.

    Returns {note_id: (start_seconds, seconds)} for the affected notes; pitch order and the
    total duration stay unchanged, and no gaps are introduced.
    """
    retimed: dict[int, tuple[float, float]] = {}
    index = 0
    for group in groups:
        if len(group) == 1 and group[0] in ("+", "-", "br"):
            index += 1
            continue
        positions = [index + offset for offset in range(len(group)) if index + offset < len(remaining)]
        index += len(group)
        if len(positions) < 2:
            continue
        levels = [stress_per_note.get(remaining[position].note_id) for position in positions]
        if 1 not in levels or 0 not in levels:
            continue
        stressed_positions = [p for p, level in zip(positions, levels, strict=True) if level == 1]
        weak_positions = [p for p, level in zip(positions, levels, strict=True) if level == 0]
        shortest_stressed = min(stressed_positions, key=lambda p: remaining[p].seconds)
        longest_weak = max(weak_positions, key=lambda p: remaining[p].seconds)
        if remaining[shortest_stressed].seconds >= 0.6 * remaining[longest_weak].seconds:
            continue
        durations = {p: remaining[p].seconds for p in positions}
        durations[shortest_stressed], durations[longest_weak] = (
            durations[longest_weak],
            durations[shortest_stressed],
        )
        cursor = remaining[positions[0]].start_seconds
        for position in positions:
            retimed[remaining[position].note_id] = (cursor, durations[position])
            cursor += durations[position]
    return retimed


def build_mapping(
    score: VocalScore,
    lyrics: str | None,
    language: str,
    holds: dict[int, int] | None,
    align_stress: bool = False,
) -> VocalMapping:
    wordless = lyrics is None or lyrics.strip() == ""
    warnings: list[str] = []
    retimed: dict[int, tuple[float, float]] = {}
    sung = [note for note in score.notes if not note.is_rest]

    # Holds are applied first: the notes a hold consumes never get their own text or phonemes,
    # so syllables and sounds stay aligned on the notes that remain.
    holds = holds or {}
    held_seconds: dict[int, float] = {}
    skipped: set[int] = set()
    if holds:
        sung_ids = [note.note_id for note in sung]
        for key, count in holds.items():
            if key not in sung_ids:
                warnings.append(f"Hold on note {key} ignored: that note id does not exist.")
                continue
            position = sung_ids.index(key)
            for offset in range(1, count + 1):
                if position + offset < len(sung_ids):
                    target = sung_ids[position + offset]
                    held_seconds[key] = held_seconds.get(key, 0.0) + next(
                        note.seconds for note in sung if note.note_id == target
                    )
                    skipped.add(target)
    remaining = [note for note in sung if note.note_id not in skipped]

    syllables: list[str] = []
    groups: list[list[str]] = []
    phonemes_per_note: dict[int, list[str]] = {}
    stress_per_note: dict[int, float | None] = {}
    english = False
    if not wordless:
        if language == "en":
            if not diffsinger.available():
                raise SingingError(
                    "English lyrics need the DiffSinger voicebank (see docs/singing.md) and "
                    "CHORDSMITH_DIFFSINGER_VOICE. Use wordless mode or Japanese kana for VOICEVOX."
                )
            english = True
            groups = diffsinger.split_syllables(lyrics)
            syllables = [token for group in groups for token in group]
        else:
            syllables = _parse_syllables(lyrics, language)
        if len(syllables) > len(remaining):
            warnings.append(
                f"{len(syllables) - len(remaining)} extra syllables were not used "
                f"(the melody has {len(remaining)} singable notes)."
            )
            syllables = syllables[: len(remaining)]
            groups = _truncate_groups(groups, len(remaining))
        if len(syllables) < len(remaining):
            warnings.append(
                f"{len(remaining) - len(syllables)} notes had no syllable and were turned into rests "
                "(no words were invented)."
            )
        if english and syllables:
            bank = diffsinger.get_voicebank()
            phones_per_token, group_warnings, stress = diffsinger.phonemize_groups(bank, groups)
            warnings.extend(group_warnings)
            for note, phones, level in zip(remaining, phones_per_token, stress, strict=False):
                phonemes_per_note[note.note_id] = phones
                stress_per_note[note.note_id] = level
            retimed = _align_stress(groups, remaining, stress_per_note) if align_stress else {}
            aligned = set(retimed)
            warnings.extend(_stress_warnings(groups, remaining, stress_per_note, aligned))
    syllable_by_note = {
        note.note_id: syllables[index] for index, note in enumerate(remaining) if index < len(syllables)
    }

    mapping_notes: list[MappingNote] = []
    for note in score.notes:
        if note.is_rest:
            mapping_notes.append(MappingNote(note.note_id, None, note.start_seconds, note.seconds, "", True))
            continue
        if note.note_id in skipped:
            continue
        if wordless:
            lyric = WORDLESS_LYRIC
        else:
            lyric = syllable_by_note.get(note.note_id, "")
        phonemes = phonemes_per_note.get(note.note_id)
        if not wordless and lyric == "":
            phonemes = None  # no syllable: this note becomes a rest
        is_rest = not wordless and lyric == ""
        start_seconds, base_seconds = retimed.get(note.note_id, (note.start_seconds, note.seconds))
        seconds = base_seconds + held_seconds.get(note.note_id, 0.0)
        mapping_notes.append(
            MappingNote(
                note.note_id,
                None if is_rest else note.key,
                start_seconds,
                seconds,
                lyric,
                is_rest,
                phonemes,
            )
        )
    if not wordless and not syllables:
        warnings.append("No syllables were mapped; every note is a rest.")
    return VocalMapping(
        mapping_id=f"map_{uuid.uuid4().hex[:10]}",
        score_id=score.score_id,
        version=1,
        voice_hint="diffsinger" if english else "voicevox",
        notes=mapping_notes,
        total_seconds=score.total_seconds,
        wordless=wordless,
        warnings=warnings,
        created_at=time.time(),
    )


def _mapping_payload(mapping: VocalMapping) -> dict:
    return {
        "mapping_id": mapping.mapping_id,
        "score_id": mapping.score_id,
        "version": mapping.version,
        "wordless": mapping.wordless,
        "voice_hint": mapping.voice_hint,
        "duration_seconds": round(mapping.total_seconds, 3),
        "warnings": mapping.warnings,
        "notes": [
            {
                "note_id": note.note_id,
                "kind": "rest" if note.is_rest else "note",
                "pitch": note.key,
                "lyric": note.lyric,
                "phonemes": note.phonemes,
                "start_seconds": round(note.start_seconds, 4),
                "seconds": round(note.seconds, 4),
                "frames": round(note.seconds * FRAME_RATE) if not note.is_rest else None,
            }
            for note in mapping.notes
        ],
    }


# ------------------------------------------------------------------ render jobs


class SoftSettings(BaseModel):
    """Soft-voice controls.

    VOICEVOX uses energy and volume_cap. DiffSinger voices use velocity, gender, expr, steps and
    depth; breathiness is refused (this voicebank has none).
    """

    energy: float = Field(
        1.0, ge=0.1, le=2.0, description="Overall vocal level (scales the engine's output)."
    )
    volume_cap: float | None = Field(
        None,
        ge=0.05,
        le=1.0,
        description="VOICEVOX only: clamp every frame's volume so loud notes stay soft (e.g. 0.6).",
    )
    breathiness: float | None = Field(
        None, ge=0.0, le=1.0, description="Not supported: this voicebank has no breathiness control."
    )
    vibrato: float | None = Field(
        None, ge=0.0, le=1.0, description="Not supported yet (DiffSinger vibrato needs curve support)."
    )
    velocity: float = Field(
        1.0, ge=0.5, le=2.0, description="DiffSinger only: singing speed factor (1.0 = original)."
    )
    gender: float = Field(
        0.0,
        ge=-1.0,
        le=1.0,
        description="DiffSinger only: formant/gender shift, 0 = the voicebank's own character "
        "(positive shifts up, negative down; large values sound unnatural).",
    )
    expr: float = Field(
        1.0, ge=0.0, le=1.0, description="DiffSinger only: pitch expressiveness (1.0 = natural)."
    )
    steps: int = Field(
        20, ge=1, le=100, description="DiffSinger only: diffusion sampling steps (higher = slower)."
    )
    depth: float = Field(
        0.6, ge=0.05, le=0.6, description="DiffSinger only: diffusion depth (voicebank max 0.6)."
    )
    seed: int | None = Field(
        None,
        ge=0,
        le=2**31 - 1,
        description="DiffSinger only: fix the engine's sampling noise so the same input renders "
        "identical audio (fair A/B tests). None draws fresh noise on every render.",
    )


def parse_voice_id(voice_id: str, singers: list[dict] | None = None) -> int:
    engine, _, style = voice_id.partition(":")
    if engine != "voicevox" or not style.isdigit():
        raise SingingError(f"Unknown voice '{voice_id}'. Use list_singing_voices to pick one.")
    speaker = int(style)
    if singers is not None:
        known = {style_info["id"] for singer in singers for style_info in singer["styles"]}
        if speaker not in known:
            closest = min(known, key=lambda candidate: abs(candidate - speaker)) if known else None
            hint = f" Did you mean 'voicevox:{closest}'?" if closest is not None else ""
            raise SingingError(
                f"Voice '{voice_id}' is not offered by the engine (no swapping in another voice).{hint}"
            )
    return speaker


def _style_types(singers: list[dict]) -> dict[int, str]:
    return {style["id"]: style.get("type", "sing") for singer in singers for style in singer["styles"]}


def resolve_speakers(voice_id: str, singers: list[dict]) -> tuple[int, int]:
    """Return (query speaker, synthesis speaker) for a voice.

    VOICEVOX splits singing styles: only ``sing``/``singing_teacher`` styles can prepare the
    frame query, while ``frame_decode`` styles (most voices, including the whisper styles) can
    only synthesize. For a decode-only voice the query is prepared by the engine's teacher style
    and the audio is synthesized with the chosen voice's timbre.
    """
    speaker = parse_voice_id(voice_id, singers)
    types = _style_types(singers)
    if types.get(speaker, "sing") in ("sing", "singing_teacher"):
        return speaker, speaker
    teacher = next(
        (sid for sid, style_type in types.items() if style_type in ("sing", "singing_teacher")), None
    )
    if teacher is None:
        raise SingingError("The engine offers no singing_teacher style to prepare the score.")
    return teacher, speaker


def _settings_dict(settings: SoftSettings | None, engine: str) -> dict:
    data = (settings or SoftSettings()).model_dump(exclude_none=True, exclude_defaults=True)
    for unsupported in ("breathiness", "vibrato"):
        if unsupported in data:
            raise SingingError(
                f"'{unsupported}' is not supported by the {engine} voices. "
                "Use energy/volume_cap (VOICEVOX) or velocity/gender/expr (DiffSinger)."
            )
    if engine == "voicevox":
        for diffsinger_only in ("velocity", "gender", "expr", "steps", "depth", "seed"):
            if diffsinger_only in data:
                raise SingingError(
                    f"'{diffsinger_only}' is a DiffSinger setting; VOICEVOX uses energy/volume_cap."
                )
    return data


def parse_voice_engine(voice_id: str) -> tuple[str, str]:
    """Split a voice id into (engine, rest): 'voicevox:6000' or 'diffsinger:hanami/nectar'."""
    engine, _, rest = voice_id.partition(":")
    if engine not in ("voicevox", "diffsinger") or not rest:
        raise SingingError(f"Unknown voice '{voice_id}'. Use list_singing_voices to pick one.")
    return engine, rest


def _voicevox_payload(mapping: VocalMapping) -> dict:
    """Build the VOICEVOX score from the engine-neutral mapping (absolute frame rounding)."""
    notes = []
    for note in mapping.notes:
        start_frame = round(note.start_seconds * FRAME_RATE)
        end_frame = round((note.start_seconds + note.seconds) * FRAME_RATE)
        notes.append({"key": note.key, "frame_length": max(1, end_frame - start_frame), "lyric": note.lyric})
    return {"notes": notes}


def _run_voicevox(job: SingingJob, mapping: VocalMapping, score: VocalScore, query: int, synth: int) -> dict:
    client = get_client()
    query_data = client.sing_frame_audio_query(_voicevox_payload(mapping), query)
    energy = float(job.settings.get("energy", 1.0))
    if energy != 1.0:
        query_data["volumeScale"] = float(query_data.get("volumeScale", 1.0)) * energy
    cap = job.settings.get("volume_cap")
    if cap is not None:
        query_data["volume"] = [min(float(v), float(cap)) for v in query_data.get("volume", [])]
    data = client.frame_synthesis(query_data, synth)
    return {"data": data, "query_voice_id": f"voicevox:{query}"}


def _run_diffsinger(job: SingingJob, mapping: VocalMapping, score: VocalScore, path: Path) -> dict:
    _, voice = parse_voice_engine(job.voice_id)
    mode = voice.split("/", 1)[-1]
    if mode not in diffsinger.MODES:
        raise SingingError(f"Unknown DiffSinger mode '{mode}'; use one of {', '.join(diffsinger.MODES)}.")
    specs = [
        diffsinger.NoteSpec(
            phonemes=note.phonemes or [],
            start_seconds=note.start_seconds,
            seconds=note.seconds,
            midi=note.key or 60,
        )
        for note in mapping.notes
        if not note.is_rest
    ]
    if not specs:
        raise SingingError("Nothing to sing: every note is a rest.")
    if any(not spec.phonemes for spec in specs):
        raise SingingError("English notes need phonemes; re-run map_vocal_lyrics with language='en'.")
    info = diffsinger.render(
        specs,
        path,
        mode=mode,
        steps=int(job.settings.get("steps", 20)),
        depth=float(job.settings.get("depth", 0.6)),
        velocity=float(job.settings.get("velocity", 1.0)),
        gender=float(job.settings.get("gender", 0.0)),
        expr=float(job.settings.get("expr", 1.0)),
        energy=float(job.settings.get("energy", 1.0)),
        seed=job.settings.get("seed"),
        total_seconds=mapping.total_seconds,
    )
    return {"info": info}


def _run_job(
    job: SingingJob, mapping: VocalMapping, score: VocalScore, speakers: tuple[int, int] | None
) -> None:
    job.status = "running"
    try:
        engine, _ = parse_voice_engine(job.voice_id)
        store = _get_store()
        assert store is not None
        # claim the name up front: overlapping jobs must never share a file (and the placeholder
        # is removed again if the render fails)
        with store.claimed_path(None, f"{Path(score.source).stem}_vocal", extension=".wav") as path:
            if engine == "voicevox":
                assert speakers is not None
                result = _run_voicevox(job, mapping, score, *speakers)
                path.write_bytes(result["data"])
            else:
                result = _run_diffsinger(job, mapping, score, path)
        with wave.open(str(path), "rb") as handle:
            duration = handle.getnframes() / handle.getframerate()
            sample_rate = handle.getframerate()
        job.result = {
            "filename": path.name,
            "path": str(path),
            "uri": f"midi://{path.name}",
            "mime_type": "audio/wav",
            "sample_rate": sample_rate,
            "duration_seconds": round(duration, 3),
            "start_offset_seconds": 0.0,
            "mapping_id": mapping.mapping_id,
            "voice_id": job.voice_id,
        }
        if engine == "voicevox":
            job.result["query_voice_id"] = result["query_voice_id"]
        else:
            job.result.update({k: v for k, v in result["info"].items() if k not in ("duration_seconds",)})
        job.status = "done"
    except Exception as exc:  # the job must fail loudly, never render twice
        logger.warning("Singing job %s failed: %s", job.job_id, exc)
        job.status = "failed"
        job.error = str(exc)


def start_job(
    mapping: VocalMapping,
    score: VocalScore,
    voice_id: str,
    settings: dict,
    echo_settings: dict | None = None,
) -> tuple[SingingJob, bool]:
    cache_key = json.dumps(
        {"mapping": mapping.mapping_id, "voice": voice_id, "settings": settings}, sort_keys=True
    )
    with _registry.lock:
        for job in _registry.jobs.values():
            if job.cache_key == cache_key and job.status in ("queued", "running", "done"):
                return job, True
    engine, _ = parse_voice_engine(voice_id)
    speakers: tuple[int, int] | None = None
    if engine == "voicevox":
        speakers = resolve_speakers(voice_id, get_client().singers())
    elif not diffsinger.available():
        raise SingingError(
            "No DiffSinger voicebank configured; set CHORDSMITH_DIFFSINGER_VOICE (see docs/singing.md)."
        )
    job = SingingJob(
        job_id=f"job_{uuid.uuid4().hex[:10]}",
        status="queued",
        mapping_id=mapping.mapping_id,
        voice_id=voice_id,
        settings=settings,
        cache_key=cache_key,
        created_at=time.time(),
        echo_settings=echo_settings if echo_settings is not None else dict(settings),
    )
    with _registry.lock:
        _registry.jobs[job.job_id] = job
    thread = threading.Thread(target=_run_job, args=(job, mapping, score, speakers), daemon=True)
    thread.start()
    return job, False


def _job_payload(job: SingingJob) -> dict:
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "voice_id": job.voice_id,
        # the full settings (defaults included), so the record shows exactly what was used
        "settings": job.echo_settings or job.settings,
    }
    if job.result is not None:
        payload.update(job.result)
    if job.error is not None:
        payload["error"] = job.error
    return payload


# ------------------------------------------------------------------ mixing and export


def _ffmpeg(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    ffmpeg = audio.find_ffmpeg()
    if ffmpeg is None:
        raise SingingError("ffmpeg is not installed (it is in the Docker image); needed for mixing.")
    try:
        result = subprocess.run([ffmpeg, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SingingError(f"ffmpeg could not run: {exc}") from exc
    if result.returncode != 0:
        raise SingingError(f"ffmpeg failed: {(result.stderr or result.stdout).strip()[-300:]}")
    return result


def _peak_db(path: Path) -> float:
    result = _ffmpeg(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", result.stderr)
    if not match:
        return -99.0
    return float(match.group(1))


def _mix_filters(vocal_gain: float, backing_volume: float, reverb: bool, output_gain_db: float) -> str:
    # The vocal is mono (both engines) and the backing is stereo: pan the vocal to stereo
    # explicitly, otherwise amix spreads it across both channels about 3 dB lower than measured.
    vocal = f"[1:a]pan=stereo|c0=c0|c1=c0,volume={vocal_gain:.4f}"
    if reverb:
        vocal += ",aecho=0.8:0.9:60:0.25"
    filters = (
        f"[0:a]volume={backing_volume:.3f}[b];{vocal}[v];[b][v]amix=inputs=2:duration=longest:normalize=0"
    )
    if output_gain_db:
        filters += f",volume={output_gain_db:.2f}dB"
    return filters + "[m]"


def gated_rms_db(path: Path, gate: float = 0.004) -> float:
    """RMS level (dBFS) of the samples above a small gate: active loudness, not silence.

    Pure Python so mixing works without extra audio libraries; every fourth sample is enough
    for a level estimate.
    """
    import array
    import math

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SingingError("Mixing expects 16-bit audio files.")
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    samples = samples[::4]
    total = 0.0
    count = 0
    for sample in samples:
        value = sample / 32768.0
        if abs(value) >= gate:
            total += value * value
            count += 1
    if count == 0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(total / count))


def _read_pcm(path: Path):
    import array

    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SingingError("Mixing expects 16-bit audio files.")
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
        return samples, handle.getnchannels(), handle.getframerate()


def _block_energy(samples, start_frame: int, block_frames: int, channels: int, stride: int = 4) -> float:
    """Mean square over a block, averaged across every channel (stride is in frames)."""
    total = 0.0
    count = 0
    end = min(start_frame + block_frames, len(samples) // channels)
    for frame in range(start_frame, end, stride):
        offset = frame * channels
        for channel in range(channels):
            value = samples[offset + channel]
            total += value * value
            count += 1
    return total / count if count else 0.0


def measure_over_sung(
    vocal: Path, targets: list[Path], block_ms: float = 50.0, gate: float = 0.004
) -> list[float]:
    """Mean square (linear, channels averaged) of each target over the blocks where the voice sings.

    All channels are measured, so a stereo band is not judged by its left channel alone.
    """
    import math

    vocal_samples, vocal_channels, vocal_rate = _read_pcm(vocal)
    vocal_block = max(1, round(vocal_rate * block_ms / 1000.0))
    vocal_frames = len(vocal_samples) // vocal_channels
    loaded = []
    for target in targets:
        samples, channels, rate = _read_pcm(target)
        block = max(1, round(rate * block_ms / 1000.0))
        loaded.append((samples, channels, block))
    totals = [0.0] * len(targets)
    counts = [0] * len(targets)
    for block_index in range(vocal_frames // vocal_block):
        start_frame = block_index * vocal_block
        vocal_energy = _block_energy(vocal_samples, start_frame, vocal_block, vocal_channels)
        if math.sqrt(vocal_energy) / 32768.0 < gate:
            continue
        for target_index, (samples, channels, block) in enumerate(loaded):
            frames = len(samples) // channels
            if start_frame >= frames:
                continue
            energy = _block_energy(samples, start_frame, block, channels)
            span = min(block, frames - start_frame)
            totals[target_index] += energy * span * channels
            counts[target_index] += span * channels
    return [totals[i] / counts[i] if counts[i] else 0.0 for i in range(len(targets))]


def active_levels(
    backing: Path, vocal: Path, block_ms: float = 50.0, gate: float = 0.004
) -> tuple[float, float]:
    """(backing_db, vocal_db) measured over the blocks where the voice is singing.

    This is the level the tool's description promises: the band is measured only while the
    vocal is active, not over the whole song (long instrumental sections would drag it down).
    """
    import math

    energies = measure_over_sung(vocal, [backing, vocal], block_ms, gate)
    if energies[1] == 0.0:
        return gated_rms_db(backing), gated_rms_db(vocal)
    vocal_db = 20.0 * math.log10(math.sqrt(energies[1]) / 32768.0)
    if energies[0] == 0.0:
        return -120.0, vocal_db
    backing_db = 20.0 * math.log10(math.sqrt(energies[0]) / 32768.0)
    return backing_db, vocal_db


def mix_tracks(
    backing: Path,
    vocal: Path,
    target: Path,
    *,
    vocal_level_db: float = 6.0,
    backing_volume: float = 1.0,
    reverb: bool = False,
    normalize_peak_db: float | None = -1.0,
) -> dict:
    """Mix the vocal into the backing, balancing levels by measurement.

    ``vocal_level_db`` is the target level of the vocal *above the band*, both measured over the
    blocks where the voice is singing, so quiet backings and loud vocals are corrected instead
    of being multiplied blindly. The vocal is converted to the mix rate before measuring: the
    resampler's anti-alias filter costs 24 kHz engine output a couple of dB, and the level that
    matters is the one in the finished file. The finished mix is normalized to
    ``normalize_peak_db`` (default -1 dBFS; pass None to keep the raw level) so exports are not
    left very quiet.
    """
    original_vocal = vocal
    with tempfile.TemporaryDirectory() as scratch:
        vocal = Path(scratch) / "vocal_mix_rate.wav"
        _ffmpeg(
            ["-y", "-loglevel", "error", "-i", str(original_vocal), "-ac", "1", "-ar", "44100", str(vocal)]
        )
        return _mix_tracks_at_mix_rate(
            backing,
            vocal,
            target,
            vocal_level_db=vocal_level_db,
            backing_volume=backing_volume,
            reverb=reverb,
            normalize_peak_db=normalize_peak_db,
        )


def _mix_tracks_at_mix_rate(
    backing: Path,
    vocal: Path,
    target: Path,
    *,
    vocal_level_db: float,
    backing_volume: float,
    reverb: bool,
    normalize_peak_db: float | None,
) -> dict:
    backing_db, vocal_db = active_levels(backing, vocal)
    vocal_gain_db = (backing_db + vocal_level_db) - vocal_db
    vocal_gain = 10.0 ** (vocal_gain_db / 20.0)
    trim_db = 20.0 * math.log10(backing_volume) if backing_volume > 0 else -120.0
    balance_db = (vocal_db + vocal_gain_db) - (backing_db + trim_db)

    def render(output_gain_db: float) -> None:
        _ffmpeg(
            [
                "-y",
                "-i",
                str(backing),
                "-i",
                str(vocal),
                "-filter_complex",
                _mix_filters(vocal_gain, backing_volume, reverb, output_gain_db),
                "-map",
                "[m]",
                "-ar",
                "44100",
                str(target),
            ]
        )

    render(0.0)
    peak = _peak_db(target)

    # verify the balance by measurement: the finished mix holds band + vocal (uncorrelated),
    # so subtracting the band's energy leaves the vocal's energy
    measured_db: float | None = None
    if not reverb:
        mix_energy = measure_over_sung(vocal, [target])[0]
        band_energy = (10.0 ** (backing_db / 20.0) * 32768.0) ** 2 * (backing_volume**2)
        if band_energy > 0 and mix_energy > band_energy:
            measured_db = 10.0 * math.log10((mix_energy - band_energy) / band_energy)
    if measured_db is None:
        balance_check = "unavailable"
    elif abs(measured_db - balance_db) <= 1.5:
        balance_check = "ok"
    else:
        balance_check = "mismatch"

    correction = 0.0
    if normalize_peak_db is not None:
        correction = round(normalize_peak_db - peak, 2)
        if abs(correction) > 0.05:
            render(correction)
            peak = _peak_db(target)
    with wave.open(str(target), "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
    return {
        "peak_db": peak,
        "clipping": peak > -0.1,
        "gain_correction_db": correction,
        "normalize_peak_db": normalize_peak_db,
        "duration_seconds": round(duration, 3),
        "backing_rms_db": round(backing_db, 1),
        "vocal_rms_db": round(vocal_db, 1),
        "vocal_gain_db": round(vocal_gain_db, 1),
        "vocal_to_backing_db": round(balance_db, 1),
        "vocal_to_backing_measured_db": None if measured_db is None else round(measured_db, 1),
        "balance_check": balance_check,
    }


def to_mp3(source: Path, target: Path) -> None:
    _ffmpeg(["-y", "-loglevel", "error", "-i", str(source), "-b:a", "192k", str(target)])


def remove_track(source: Path, target: Path, track_index: int) -> None:
    """Write a copy of ``source`` without one track (the guide), for backing rendering."""
    midi = mido.MidiFile(source)
    if midi.type == 0:
        raise SingingError(
            "This file is type 0 (everything in one track), so the guide cannot be removed. "
            "Use the type 1 version."
        )
    if not 0 <= track_index < len(midi.tracks):
        raise SingingError(f"Track {track_index + 1} does not exist in {source.name}.")
    del midi.tracks[track_index]
    if not any(any(msg.type == "note_on" for msg in track) for track in midi.tracks):
        raise SingingError("No backing tracks would be left after removing the guide track.")
    midi.save(target)


# ------------------------------------------------------------------ MCP tools


def register(mcp: FastMCP, store_provider: Callable[[], FileStore]) -> None:
    """Register the singing tools on the ChordSmith server."""
    global _get_store
    _get_store = store_provider

    @mcp.tool(title="List singing voices", annotations=_READ_ONLY)
    def list_singing_voices(
        engine: Annotated[
            Literal["voicevox", "diffsinger", "all"], Field(description="Which engine to query.")
        ] = "all",
    ) -> results.VoicesResult:
        """List the singing voices an engine offers, with languages, licence notes and soft controls.

        Read-only; call this first to pick a voice id for render_singing. VOICEVOX sings Japanese
        kana or a wordless hum (decode-only voices, including the whisper styles, are prepared by
        the engine's teacher style). DiffSinger sings English lyrics from a voicebank mounted on
        this computer (CHORDSMITH_DIFFSINGER_VOICE) and reports its licence layers and commercial
        status. With engine='all' the result has a key per engine; with one engine you get that
        engine's entry directly.
        """
        _registry.prune()
        if engine not in ("voicevox", "diffsinger", "all"):
            raise SingingError(f"Unknown engine '{engine}'.")
        result: dict[str, Any] = {"engines": {}}
        if engine in ("voicevox", "all"):
            client = get_client()
            singers = client.singers()
            types = _style_types(singers)
            voices = []
            for singer in singers:
                for style in singer["styles"]:
                    style_type = types.get(style["id"], "sing")
                    voices.append(
                        {
                            "voice_id": f"voicevox:{style['id']}",
                            "name": f"{singer['name']} / {style['name']}",
                            "engine": "voicevox",
                            "language": "ja",
                            "style_type": style_type,
                            "query_via_teacher": style_type not in ("sing", "singing_teacher"),
                            "licence": VOICEVOX_LICENCE,
                            "soft_controls": {
                                "energy": True,
                                "volume_cap": True,
                                "breathiness": False,
                                "vibrato": False,
                            },
                        }
                    )
            result["engines"]["voicevox"] = {
                "engine": "voicevox",
                "engine_url": client.base_url,
                "voices": voices,
            }
        if engine in ("diffsinger", "all"):
            info = diffsinger.describe()
            if info is None:
                if engine == "diffsinger":
                    raise SingingError(
                        "No DiffSinger voicebank configured; set CHORDSMITH_DIFFSINGER_VOICE "
                        "(see docs/singing.md)."
                    )
                result["engines"]["diffsinger"] = {"configured": False, "voices": []}
            else:
                result["engines"]["diffsinger"] = {
                    "configured": True,
                    "voicebank": info["voicebank"],
                    "credit": info["credit"],
                    "licence": info["licence"],
                    "commercial_status": info["commercial_status"],
                    "voices": [
                        {
                            "voice_id": f"diffsinger:hanami/{mode}",
                            "name": f"Hoshino Hanami / {mode.capitalize()}"
                            + (" (soft)" if mode == "nectar" else ""),
                            "engine": "diffsinger",
                            "language": "en",
                            "style_type": "diffsinger",
                            "query_via_teacher": False,
                            "licence": info["licence"],
                            "credit": info["credit"],
                            "commercial_status": info["commercial_status"],
                            "soft_controls": {
                                "energy": True,
                                "velocity": True,
                                "gender": True,
                                "expr": True,
                                "steps": True,
                                "depth": True,
                                "breathiness": False,
                                "vibrato": False,
                            },
                        }
                        for mode in diffsinger.MODES
                    ],
                }
        if engine == "all":
            return result["engines"]
        return result["engines"][engine]

    @mcp.tool(title="Prepare vocal score", annotations=_READ_ONLY)
    def prepare_vocal_score(
        filename: Annotated[str, Field(description="MIDI file in the output folder.")],
        track: Annotated[
            str | int | None,
            Field(
                description="Melody track: a name, a 1-based index, or omit to auto-pick a track named "
                "like 'Melody'/'Vocal'."
            ),
        ] = None,
        transpose: Annotated[
            int,
            Field(ge=-24, le=24, description="Semitones for the vocal line (default -12: soft register)."),
        ] = DEFAULT_TRANSPOSE,
        legato: Annotated[
            float,
            Field(
                ge=0.0,
                le=2.0,
                description="Close gaps between notes shorter than this many beats, so syllables "
                "connect (default 0.25 joins typical instrument articulations; set 0 to keep the "
                "gaps as rests).",
            ),
        ] = 0.25,
    ) -> results.ScoreResult:
        """Prepare a monophonic vocal score from a MIDI melody track (step 1 of singing).

        Read-only for the source file: it registers a score in memory and returns its id plus
        every note with beats, seconds and engine frames, and the real rests. The score is kept
        for 24 hours. Fails if the track has overlapping notes (pick a monophonic track).
        ``legato`` closes the tiny gaps an instrumental melody leaves between notes, so the voice
        does not stop and start inside words. The next step is map_vocal_lyrics with the returned
        score_id.
        """
        store = _get_store()
        assert store is not None
        score = build_score(store.existing_path(filename), track, transpose, legato)
        with _registry.lock:
            _registry.scores[score.score_id] = score
        return _score_payload(score)

    @mcp.tool(title="Map vocal lyrics", annotations=_READ_ONLY)
    def map_vocal_lyrics(
        score_id: Annotated[str, Field(description="Score id from prepare_vocal_score.")],
        lyrics: Annotated[
            str | None,
            Field(
                description="One token per sung note (rests take none). English: words or syllables, "
                "e.g. 'sodium gold' or "
                "'so- di- um gold'; '+' continues the previous note, '-' is a pause, 'br' a breath. "
                "Japanese: kana, e.g. 'う う う'. Omit for a wordless hum."
            ),
        ] = None,
        language: Annotated[
            str, Field(description="'en' for English words, 'ja' for kana; omit lyrics for wordless mode.")
        ] = "ja",
        holds: Annotated[
            dict[int, int] | None,
            Field(
                description='Optional holds: {"<note_id>": how many following notes it is held over}. '
                "The held note keeps its pitch for the whole length; use '+' instead when the "
                "syllable should move across changing pitches."
            ),
        ] = None,
        align_stress: Annotated[
            bool,
            Field(
                description="English only, off by default: when a stressed syllable gets a much "
                "shorter note than a weak one in the same word, swap those note lengths so the "
                "stressed syllable is longest (pitch order and total length unchanged)."
            ),
        ] = False,
    ) -> results.MappingResult:
        """Attach one token per sung note (rests take none) and dry-run the phonemization.

        Read-only: registers the mapping in memory and returns the per-note plan (tokens,
        phonemes, timings) plus warnings, so mistakes are caught before rendering. The mapping is
        kept for 24 hours. English tokens are phonemized here; unknown words are refused by name
        and words are never dropped or invented. Mismatches produce warnings. The next step is
        render_singing with the returned mapping_id.
        """
        with _registry.lock:
            score = _registry.scores.get(score_id)
        if score is None:
            raise SingingError(f"Score '{score_id}' not found (scores expire after 24 hours).")
        mapping = build_mapping(score, lyrics, language, holds, align_stress)
        with _registry.lock:
            _registry.versions[score_id] = _registry.versions.get(score_id, 0) + 1
            mapping.version = _registry.versions[score_id]
            _registry.mappings[mapping.mapping_id] = mapping
        return _mapping_payload(mapping)

    @mcp.tool(title="Render singing", annotations=_CREATES)
    def render_singing(
        mapping_id: Annotated[str, Field(description="Mapping id from map_vocal_lyrics.")],
        voice_id: Annotated[
            str,
            Field(
                description="Voice id from list_singing_voices: 'voicevox:6000' or "
                "'diffsinger:hanami/nectar'."
            ),
        ],
        settings: Annotated[
            SoftSettings | None,
            Field(
                description="Soft-voice controls; omit for defaults. VOICEVOX: energy, volume_cap. "
                "DiffSinger: velocity, gender, expr, steps, depth, seed. The job record echoes the "
                "full settings (defaults included)."
            ),
        ] = None,
    ) -> results.RenderStartResult:
        """Start rendering the vocal in the background (step 3 of singing) and return a job id.

        Creates a wav when the job finishes; poll get_singing_job. The job record is kept for
        24 hours (the rendered file stays in the output folder). Repeating an identical request
        reuses the finished job instead of rendering twice. A 'seed' in settings fixes DiffSinger's
        sampling noise so the same input renders identical bytes (tested on Hanami v1.0; not
        promised across voicebank versions). Failures (missing voicebank, engine unreachable) are
        reported on the job as status 'failed' with the reason.
        """
        with _registry.lock:
            mapping = _registry.mappings.get(mapping_id)
            score = _registry.scores.get(mapping.score_id) if mapping else None
        if mapping is None or score is None:
            raise SingingError(f"Mapping '{mapping_id}' not found (mappings expire after 24 hours).")
        engine, _ = parse_voice_engine(voice_id)
        settings_dict = _settings_dict(settings, engine)
        echo_settings = (settings or SoftSettings()).model_dump(exclude_none=True)
        job, reused = start_job(mapping, score, voice_id, settings_dict, echo_settings)
        return {"job_id": job.job_id, "status": job.status, "reused": reused}

    @mcp.tool(title="Get singing job", annotations=_READ_ONLY)
    def get_singing_job(
        job_id: Annotated[str, Field(description="Job id from render_singing.")],
    ) -> results.JobResult:
        """Report a render job: queued, running, done (with the vocal file) or failed (with the error).

        Read-only. When done, the result includes the vocal file name/path, sample rate, duration
        and start offset, plus the settings that were used; when failed, the 'error' field says
        what went wrong and how to fix it.
        """
        with _registry.lock:
            job = _registry.jobs.get(job_id)
        if job is None:
            raise SingingError(f"Job '{job_id}' not found (jobs expire after 24 hours).")
        return _job_payload(job)

    @mcp.tool(title="Mix song with vocals", annotations=_CREATES)
    def mix_song_with_vocals(
        source: Annotated[str, Field(description="The MIDI file the score came from.")],
        vocal: Annotated[str, Field(description="Vocal filename from a finished job, or the job id.")],
        guide_track: Annotated[
            str | int | None,
            Field(description="Track to leave out of the backing (default: the same melody track)."),
        ] = None,
        vocal_level_db: Annotated[
            float,
            Field(
                ge=-12.0,
                le=18.0,
                description="How loud the vocal sits above the backing, in dB, measured over the "
                "sung parts (default 6: clearly on top but not overpowering).",
            ),
        ] = 6.0,
        backing_volume: Annotated[
            float, Field(ge=0.0, le=2.0, description="Backing trim (1.0 = the rendered level).")
        ] = 1.0,
        reverb: Annotated[
            bool, Field(description="Gentle reverb on the vocal (off by default: clarity first).")
        ] = False,
        normalize_peak_db: Annotated[
            float | None,
            Field(
                ge=-6.0,
                le=0.0,
                description="Normalize the exported mix to this peak level in dBFS "
                "(default -1.0); set null to keep the raw level.",
            ),
        ] = -1.0,
        output_filename: Annotated[
            str | None, Field(description="Name for the mix (default: <source>_mix).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing mix.")] = False,
    ) -> results.MixResult:
        """Render the backing without the guide track, mix in the vocal, and check the balance (step 4).

        Creates the backing and mix wavs (guide track left out). Levels are balanced by
        measurement: the vocal is placed ``vocal_level_db`` above the backing's active level,
        measured over the blocks where the voice sings, so quiet backings and loud vocals are
        corrected instead of multiplied blindly. The result reports the achieved
        vocal_to_backing_db and a balance_check of 'ok'/'mismatch'/'unavailable' (unavailable when
        reverb is on: the measurement assumes dry signals). The mix is normalized to
        normalize_peak_db unless null, and clipped exports are turned down (see clipping and
        gain_correction_db).
        """
        store = _get_store()
        assert store is not None
        source_path = store.existing_path(source)
        with _registry.lock:
            job = _registry.jobs.get(vocal)
        if job is not None:
            if job.status != "done" or job.result is None:
                raise SingingError(f"Job '{vocal}' is {job.status}; wait for it to finish first.")
            vocal_path = store.existing_media_path(job.result["filename"])
        else:
            vocal_path = store.existing_media_path(vocal)
        midi = mido.MidiFile(source_path)
        track_index = find_track(midi, guide_track)
        with (
            store.claimed_path(None, f"{source_path.stem}_backing", overwrite, ".mid") as backing_midi,
            store.claimed_path(None, f"{source_path.stem}_backing", overwrite, ".wav") as backing_wav,
            store.claimed_path(output_filename, f"{source_path.stem}_mix", overwrite, ".wav") as target,
        ):
            remove_track(source_path, backing_midi, track_index)
            audio.render(backing_midi, backing_wav, "wav")
            info = mix_tracks(
                backing_wav,
                vocal_path,
                target,
                vocal_level_db=vocal_level_db,
                backing_volume=backing_volume,
                reverb=reverb,
                normalize_peak_db=normalize_peak_db,
            )
        return {
            "filename": target.name,
            "path": str(target),
            "uri": f"midi://{target.name}",
            "mime_type": "audio/wav",
            "source": source_path.name,
            "vocal": vocal_path.name,
            "backing": backing_wav.name,
            "levels": {
                "vocal_level_db": vocal_level_db,
                "backing_volume": backing_volume,
                "normalize_peak_db": normalize_peak_db,
            },
            "guide_removed": _track_names(midi)[track_index],
            **info,
        }

    @mcp.tool(title="Export vocal song", annotations=_CREATES)
    def export_vocal_song(
        mix: Annotated[str, Field(description="Mix wav from mix_song_with_vocals.")],
        vocal: Annotated[str, Field(description="Vocal wav (from a job) to include separately.")],
        source: Annotated[str, Field(description="The untouched source MIDI to include.")],
        return_as: Annotated[
            delivery.ReturnAs,
            Field(
                description="'base64' returns the bytes inline; 'url' returns signed, expiring "
                "download links (the bytes stay on the server)."
            ),
        ] = "base64",
        expires_in: Annotated[
            int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
        ] = delivery.DEFAULT_TTL_SECONDS,
    ) -> results.ExportResult:
        """Return the finished song for delivery: full mix (WAV and MP3), vocal alone, source MIDI.

        Creates an MP3 of the mix. Each entry comes back like get_midi_file: base64 bytes, or a
        signed expiring link when return_as='url'. Every entry includes filename, size, sha256 and
        mime_type, so a caller can verify what it received.
        """
        store = _get_store()
        assert store is not None
        mix_path = store.existing_media_path(mix)
        vocal_path = store.existing_media_path(vocal)
        source_path = store.existing_path(source)
        with store.claimed_path(None, mix_path.stem, extension=".mp3") as mp3_path:
            to_mp3(mix_path, mp3_path)
        items = {
            "mix": (mix_path, "audio/wav"),
            "mix_mp3": (mp3_path, "audio/mpeg"),
            "vocal": (vocal_path, "audio/wav"),
            "midi": (source_path, "audio/midi"),
        }
        result: dict[str, Any] = {}
        for key, (path, mime) in items.items():
            entry = delivery.deliver_file(path.name, path.read_bytes(), return_as, expires_in)
            entry["mime_type"] = mime
            result[key] = entry
        return result
