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
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from chordsmith import audio, delivery, diffsinger, renderers, results, sections
from chordsmith.models import (
    AirLayer,
    ChorusSettings,
    DelayReturn,
    MasterSettings,
    Returns,
    ReverbReturn,
    ReverbSettings,
    VocalBus,
    VocalChain,
    VocalCompressor,
    VocalEq,
    VocalSegment,
    VocalSends,
)
from chordsmith.offload import offloaded
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
    source: str = ""
    progress: str | None = None
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
        removed = False
        with self.lock:
            for store_dict in (self.scores, self.mappings, self.jobs):
                for key in [k for k, v in store_dict.items() if v.created_at < cutoff]:
                    del store_dict[key]
                    removed = True
        if removed:
            _persist_singing()


_registry = _Registry()
_get_store: Callable[[], FileStore] | None = None


# ------------------------------------------------------------------ registry persistence


def _singing_state_dir() -> Path:
    return delivery.state_dir() / "singing"


def _write_state(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(part, target)


def _persist_singing() -> None:
    """Save scores, mappings and jobs as JSON in the state directory.

    The records used to live only in memory, so every restart lost the whole singing setup and
    any running job; they now survive restarts (and the state directory is the one the signed
    download links already use).
    """
    try:
        directory = _singing_state_dir()
        with _registry.lock:
            scores = {key: asdict(value) for key, value in _registry.scores.items()}
            mappings = {key: asdict(value) for key, value in _registry.mappings.items()}
            jobs = {key: asdict(value) for key, value in _registry.jobs.items()}
        _write_state(directory / "scores.json", scores)
        _write_state(directory / "mappings.json", mappings)
        _write_state(directory / "jobs.json", jobs)
    except OSError as exc:
        logger.warning("Could not persist the singing registry: %s", exc)


def _load_singing() -> None:
    """Reload the registry; jobs cut off by a restart come back failed, with the reason."""
    directory = _singing_state_dir()
    loaded = {"scores": {}, "mappings": {}, "jobs": {}}
    for name, key in (("scores.json", "scores"), ("mappings.json", "mappings"), ("jobs.json", "jobs")):
        path = directory / name
        if not path.is_file():
            continue
        try:
            loaded[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load %s: %s", path.name, exc)
    interrupted = 0
    with _registry.lock:
        for key, data in loaded["scores"].items():
            try:
                data["notes"] = [ScoreNote(**note) for note in data.get("notes", [])]
                _registry.scores[key] = VocalScore(**data)
            except TypeError as exc:
                logger.warning("Skipping unreadable score %s: %s", key, exc)
        for key, data in loaded["mappings"].items():
            try:
                data["notes"] = [MappingNote(**note) for note in data.get("notes", [])]
                _registry.mappings[key] = VocalMapping(**data)
            except TypeError as exc:
                logger.warning("Skipping unreadable mapping %s: %s", key, exc)
        for key, data in loaded["jobs"].items():
            try:
                job = SingingJob(**data)
            except TypeError as exc:
                logger.warning("Skipping unreadable job %s: %s", key, exc)
                continue
            if job.status in ("queued", "running"):
                job.status = "failed"
                job.error = "The server restarted during the render; start it again."
                job.progress = None
                interrupted += 1
            _registry.jobs[key] = job
    if interrupted:
        logger.warning("Marked %d interrupted singing job(s) as failed", interrupted)
        _persist_singing()


# ------------------------------------------------------------------ score preparation


def _track_names(midi: mido.MidiFile) -> list[str]:
    names = []
    for track in midi.tracks:
        name = next((msg.name for msg in track if msg.type == "track_name"), "")
        names.append(name or "(unnamed)")
    return names


def find_track(midi: mido.MidiFile, track: str | int | None) -> int:
    """Resolve a track reference: 1-based index, name, or the guide track when None.

    The default prefers an exact ``Vocal``, ``Voice`` or ``Melody`` name (in that order — the
    guide is what the voice replaces), then the first track whose name merely mentions
    melody/vocal/lead/voice/sing. Without the exact pass, a file holding both ``Lead`` and
    ``Vocal`` resolves to whichever comes first in the file.
    """
    names = _track_names(midi)
    if track is None:
        for wanted in ("vocal", "voice", "melody"):
            for index, name in enumerate(names):
                if name.lower() == wanted:
                    return index
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
    source_path = Path(score.source)
    store = _get_store()
    if store is not None:
        try:
            source_path = store.existing_path(score.source)
        except Exception:  # the source may have been deleted; the section map is then empty
            source_path = Path(score.source)
    section_map = sections.read_sections(source_path) if source_path.is_file() else []
    tempo_changes: list[dict] = []
    try:
        midi = mido.MidiFile(source_path)
        tempo_changes = [
            {"beat": round(tick / midi.ticks_per_beat, 3), "bpm": round(bpm, 2)}
            for tick, bpm in _tempo_map(midi)
        ]
    except (OSError, ValueError):
        tempo_changes = [{"beat": 0.0, "bpm": round(score.tempo_bpm, 2)}]
    return {
        "score_id": score.score_id,
        "source": score.source,
        "track": {"index": score.track_index + 1, "name": score.track_name},
        # the single tempo is the starting tempo; tempo_changes and sections carry the rest
        "tempo_bpm": score.tempo_bpm,
        "tempo_changes": tempo_changes,
        "sections": [section.payload() for section in section_map],
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
    words: dict[str, str] | None = None,
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
    phoneme_mode = False
    overrides: dict[str, list[str]] = {}
    if not wordless:
        if language in ("en", "phonemes"):
            if not diffsinger.available():
                raise SingingError(
                    "English lyrics need the DiffSinger voicebank (see docs/singing.md) and "
                    "CHORDSMITH_DIFFSINGER_VOICE. Use wordless mode or Japanese kana for VOICEVOX."
                )
            bank = diffsinger.get_voicebank()
            if language == "en":
                english = True
                groups = diffsinger.split_syllables(lyrics)
                syllables = [token for group in groups for token in group]
                unknown: list[str] = []
                for word, pronunciation in (words or {}).items():
                    phones = pronunciation.split()
                    if not phones:
                        raise SingingError(f"Custom word '{word}' has no phonemes.")
                    unknown.extend(phone for phone in phones if phone not in bank.phoneme_ids)
                    overrides[word.lower()] = phones
                if unknown:
                    raise SingingError(
                        f"Custom word phoneme(s) not in this voicebank: "
                        f"{', '.join(sorted(set(unknown)))}. Use the voicebank's own phonemes."
                    )
            else:
                # raw phonemes: one token per note, phonemes hyphen-separated (e.g. 'l-aa')
                phoneme_mode = True
                unknown = []
                for token in lyrics.split():
                    lowered = token.lower()
                    if lowered in ("+", "-", "br"):
                        groups.append([token])
                        continue
                    phones = [part for part in token.split("-") if part]
                    if not phones:
                        raise SingingError(f"Empty phoneme token '{token}'.")
                    unknown.extend(phone for phone in phones if phone not in bank.phoneme_ids)
                    overrides[lowered] = phones
                    groups.append([token])
                if unknown:
                    known = sorted(bank.phoneme_ids)
                    preview = ", ".join(known[:40]) + ("..." if len(known) > 40 else "")
                    raise SingingError(
                        f"Unknown phoneme(s): {', '.join(sorted(set(unknown)))}. This voicebank "
                        f"uses: {preview}"
                    )
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
        if (english or phoneme_mode) and syllables:
            phones_per_token, group_warnings, stress = diffsinger.phonemize_groups(
                bank, groups, overrides or None
            )
            warnings.extend(group_warnings)
            for note, phones, level in zip(remaining, phones_per_token, stress, strict=False):
                phonemes_per_note[note.note_id] = phones
                stress_per_note[note.note_id] = level
            if english:
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
    depth; breathiness/voicing/tension pass through to voicebanks that declare them (Hanami
    declares none of the three and they are refused for it).
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
        None,
        ge=0.0,
        le=1.0,
        description="DiffSinger only: breathiness embedding, 0-1. Needs a voicebank that declares "
        "use_breathiness_embed (Hanami does not); refused with a clear error otherwise.",
    )
    voicing: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="DiffSinger only: voicing embedding, 0-1 (1 = fully voiced). Needs a voicebank "
        "that declares use_voicing_embed.",
    )
    tension: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="DiffSinger only: tension embedding, 0-1. Needs a voicebank that declares "
        "use_tension_embed.",
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
    if engine == "voicevox":
        for unsupported in ("breathiness", "voicing", "tension", "vibrato"):
            if unsupported in data:
                raise SingingError(
                    f"'{unsupported}' is a DiffSinger setting; VOICEVOX uses energy/volume_cap."
                )
        for diffsinger_only in ("velocity", "gender", "expr", "steps", "depth", "seed"):
            if diffsinger_only in data:
                raise SingingError(
                    f"'{diffsinger_only}' is a DiffSinger setting; VOICEVOX uses energy/volume_cap."
                )
    else:
        if "vibrato" in data:
            raise SingingError(
                "'vibrato' is not supported yet (DiffSinger vibrato needs pitch-curve support)."
            )
        capabilities = diffsinger.soft_controls()
        if capabilities:
            for param in ("breathiness", "voicing", "tension"):
                if param in data and not capabilities.get(param):
                    bank = capabilities.get("voicebank", "the configured voicebank")
                    raise SingingError(
                        f"'{param}' is not supported by {bank}: it declares no {param} embedding "
                        f"(use_{param}_embed is false in its dsconfig). A voicebank with it will work."
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
        {
            "phonemes": note.phonemes or [],
            "start_seconds": note.start_seconds,
            "seconds": note.seconds,
            "midi": note.key or 60,
        }
        for note in mapping.notes
        if not note.is_rest
    ]
    if not specs:
        raise SingingError("Nothing to sing: every note is a rest.")
    if any(not spec["phonemes"] for spec in specs):
        raise SingingError("English notes need phonemes; re-run map_vocal_lyrics with language='en'.")
    # The render runs in a worker process (chunked at rests): a long score's acoustic model
    # needs memory that scales with the phrase, and a native crash must never take the server
    # down with it. Progress arrives as JSON lines on stdout.
    spec = {
        "notes": specs,
        "output": str(path),
        "mode": mode,
        "steps": int(job.settings.get("steps", 20)),
        "depth": float(job.settings.get("depth", 0.6)),
        "velocity": float(job.settings.get("velocity", 1.0)),
        "gender": float(job.settings.get("gender", 0.0)),
        "expr": float(job.settings.get("expr", 1.0)),
        "energy": float(job.settings.get("energy", 1.0)),
        "breathiness": job.settings.get("breathiness"),
        "voicing": job.settings.get("voicing"),
        "tension": job.settings.get("tension"),
        "seed": job.settings.get("seed"),
        "total_seconds": mapping.total_seconds,
    }
    with tempfile.TemporaryDirectory() as scratch:
        spec_path = Path(scratch) / "render_spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", "chordsmith.diffsinger_worker", str(spec_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        info: dict | None = None
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "chunk" in message:
                job.progress = f"rendering phrase {message['chunk']}/{message['chunks']}"
            if message.get("done"):
                info = message
        stderr = process.stderr.read() if process.stderr is not None else ""
        code = process.wait()
    if code != 0 or info is None:
        detail = (stderr or "").strip().splitlines()[-6:]
        reason = " | ".join(detail) if detail else "no error output"
        raise SingingError(f"The render worker exited (code {code}): {reason}")
    return {
        "info": {
            "duration_seconds": info.get("duration_seconds"),
            "chunks": info.get("chunks"),
            "mode": info.get("mode"),
            "peak_db": info.get("peak_db"),
        }
    }


def _run_job(
    job: SingingJob, mapping: VocalMapping, score: VocalScore, speakers: tuple[int, int] | None
) -> None:
    job.status = "running"
    try:
        engine, _ = parse_voice_engine(job.voice_id)
        store = _get_store()
        assert store is not None
        # claim the name up front: overlapping jobs must never share a file (and the placeholder
        # is removed again if the render fails); the audio itself lands via a .part file and a
        # rename, so a crash can never leave a half-written (or 0-byte) result
        with store.claimed_path(None, f"{Path(score.source).stem}_vocal", extension=".wav") as path:
            if engine == "voicevox":
                assert speakers is not None
                result = _run_voicevox(job, mapping, score, *speakers)
                part = path.with_name(path.stem + ".part" + path.suffix)
                part.write_bytes(result["data"])
                os.replace(part, path)
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
        job.progress = None
        job.status = "done"
        _persist_singing()
    except Exception as exc:  # the job must fail loudly, never render twice
        logger.warning("Singing job %s failed: %s", job.job_id, exc)
        job.status = "failed"
        job.error = str(exc)
        _persist_singing()


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
        source=Path(score.source).name,
        echo_settings=echo_settings if echo_settings is not None else dict(settings),
    )
    with _registry.lock:
        _registry.jobs[job.job_id] = job
    _persist_singing()
    thread = threading.Thread(target=_run_job, args=(job, mapping, score, speakers), daemon=True)
    thread.start()
    return job, False


def _job_payload(job: SingingJob) -> dict:
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "voice_id": job.voice_id,
        "source": job.source or None,
        "created_at": datetime.fromtimestamp(job.created_at, timezone.utc).isoformat(timespec="seconds"),
        "progress": job.progress,
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


def _mix_filters(vocal_gain: float, backing_volume: float, vocal_reverb: bool, output_gain_db: float) -> str:
    # The vocal is mono (both engines) and the backing is stereo: pan the vocal to stereo
    # explicitly, otherwise amix spreads it across both channels about 3 dB lower than measured.
    vocal = f"[1:a]pan=stereo|c0=c0|c1=c0,volume={vocal_gain:.4f}"
    if vocal_reverb:
        # Dry at unity (in/out gain 1.0): the echo rides on top instead of pulling the vocal
        # below its measured level — aecho's default in_gain would attenuate the dry signal.
        vocal += ",aecho=1.0:1.0:60:0.25"
    filters = (
        f"[0:a]volume={backing_volume:.3f}[b];{vocal}[v];[b][v]amix=inputs=2:duration=longest:normalize=0"
    )
    if output_gain_db:
        filters += f",volume={output_gain_db:.2f}dB"
    return filters + "[m]"


DUCK_THRESHOLD_DB = -36.0  # vocal block level that counts as "singing"
DUCK_BLOCK_MS = 25.0


def _duck_backing(backing: Path, vocal: Path, target: Path, duck_db: float) -> None:
    """Write the backing with a gain dip wherever the voice sings (ducking).

    ffmpeg's sidechaincompress truncates its output unpredictably, so the envelope is computed
    here instead: the vocal is measured in short blocks, the dip is smoothed with a fast attack
    and a slow release, and the backing is scaled per block with a linear ramp between blocks.
    Only this file carries the dip; the vocal gain is targeted against the original backing minus
    ``duck_db``, so ``vocal_level_db`` is the ratio the listener hears over the dipped band.
    """
    import array
    import math

    vocal_samples, vocal_channels, vocal_rate = _read_pcm(vocal)
    backing_samples, channels, rate = _read_pcm(backing)
    block = max(1, round(rate * DUCK_BLOCK_MS / 1000.0))
    vocal_block = max(1, round(vocal_rate * DUCK_BLOCK_MS / 1000.0))
    frames = len(backing_samples) // channels
    duck_gain = 10.0 ** (-duck_db / 20.0)
    attack = 1.0 - math.exp(-DUCK_BLOCK_MS / 1000.0 / 0.01)
    release = 1.0 - math.exp(-DUCK_BLOCK_MS / 1000.0 / 0.30)
    gains: list[float] = []
    gain = 1.0
    blocks = (frames + block - 1) // block
    for index in range(blocks):
        start = index * vocal_block
        energy = _block_energy(vocal_samples, start, vocal_block, vocal_channels, stride=1)
        level_db = 20.0 * math.log10(math.sqrt(energy) / 32768.0) if energy > 0 else -120.0
        target_gain = duck_gain if level_db > DUCK_THRESHOLD_DB else 1.0
        gain += (target_gain - gain) * (attack if target_gain < gain else release)
        gains.append(gain)
    out = array.array("h", bytes(2 * len(backing_samples)))
    for index in range(blocks):
        start = index * block
        end = min(start + block, frames)
        previous = gains[index - 1] if index else gains[0]
        current = gains[index]
        span = max(1, end - start)
        for frame in range(start, end):
            value = previous + (current - previous) * (frame - start) / span
            offset = frame * channels
            for channel in range(channels):
                out[offset + channel] = int(backing_samples[offset + channel] * value)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(out.tobytes())


def _float_peak_db(path: Path) -> float:
    """Peak level (dBFS) of a raw f32le render; the sample peak decides s16 clamping.

    ffmpeg's volumedetect misreports float wavs and Python's wave module cannot read them, so
    the probe is written as headerless f32le and read with the stdlib array module.
    """
    import array

    samples = array.array("f")
    samples.frombytes(path.read_bytes())
    peak = max((abs(value) for value in samples), default=0.0)
    return 20.0 * math.log10(max(peak, 1e-9))


def _f32_mix_energy_over_sung(vocal: Path, probe: Path, block_ms: float = 50.0, gate: float = 0.004) -> float:
    """Mean square of a raw f32le stereo render over the vocal's blocks, in int16 units.

    The probe is the uncorrected mix (gain 0), so comparing it against the backing's level is
    honest even when the export is later normalized up or down.
    """
    import array

    vocal_samples, vocal_channels, vocal_rate = _read_pcm(vocal)
    probe_samples = array.array("f")
    probe_samples.frombytes(probe.read_bytes())
    probe_channels = 2  # the mix bus always renders stereo
    vocal_block = max(1, round(vocal_rate * block_ms / 1000.0))
    probe_block = max(1, round(44100 * block_ms / 1000.0))
    frames = len(probe_samples) // probe_channels
    total = 0.0
    count = 0
    for index in range(len(vocal_samples) // vocal_channels // vocal_block):
        start = index * vocal_block
        energy = _block_energy(vocal_samples, start, vocal_block, vocal_channels)
        if math.sqrt(energy) / 32768.0 < gate:
            continue
        if start >= frames:
            continue
        block_energy = _block_energy(probe_samples, start, probe_block, probe_channels)
        span = min(probe_block, frames - start)
        total += block_energy * span * probe_channels
        count += span * probe_channels
    return (total / count) * (32768.0**2) if count else 0.0


def _wav_energy_over_sung(vocal: Path, backing: Path, block_ms: float = 50.0, gate: float = 0.004) -> float:
    """Mean square of a 16-bit backing wav over the vocal's voiced blocks, in int16 units.

    Reading the backing over the *same* blocks as the mix probe is what keeps the balance check
    honest with a wet backing: a whole-file gated-RMS estimate drifts once reverb fills the
    gaps, while this measures exactly the band energy the subtraction assumes.
    """
    vocal_samples, vocal_channels, vocal_rate = _read_pcm(vocal)
    backing_samples, backing_channels, backing_rate = _read_pcm(backing)
    vocal_block = max(1, round(vocal_rate * block_ms / 1000.0))
    backing_block = max(1, round(backing_rate * block_ms / 1000.0))
    frames = len(backing_samples) // backing_channels
    total = 0.0
    count = 0
    for index in range(len(vocal_samples) // vocal_channels // vocal_block):
        start = index * vocal_block
        energy = _block_energy(vocal_samples, start, vocal_block, vocal_channels)
        if math.sqrt(energy) / 32768.0 < gate:
            continue
        if start >= frames:
            continue
        block_energy = _block_energy(backing_samples, start, backing_block, backing_channels)
        span = min(backing_block, frames - start)
        total += block_energy * span * backing_channels
        count += span * backing_channels
    return total / count if count else 0.0


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


def _resolve_vocal_path(name: str, store: FileStore) -> Path:
    """A vocal file name or a finished singing job id -> the wav path."""
    with _registry.lock:
        job = _registry.jobs.get(name)
    if job is not None:
        if job.status != "done" or job.result is None:
            raise SingingError(f"Job '{name}' is {job.status}; wait for it to finish first.")
        return store.existing_media_path(job.result["filename"])
    return store.existing_media_path(name)


# ------------------------------------------------------------------ vocal processing


IR_SEED = 4242  # the generated reverb impulse is deterministic
LIMITER_MARGIN_DB = 0.3  # the limiter ceiling sits this far under the target: room for inter-sample peaks
VOCAL_CHAIN_SUFFIX = ".vocal_chain.json"  # the chain sidecar next to the source MIDI


@dataclass
class ProcessedVoice:
    """One voice with its resolved file and processing settings."""

    path: Path
    start_seconds: float = 0.0
    name: str = "voice"
    gain_db: float = 0.0
    pan: float = 0.0
    chain: VocalChain | None = None
    sends: VocalSends | None = None
    reverb_mix: float | None = None


@dataclass
class ProcessedVocals:
    """The staged vocal pipeline's output: the dry bus and the effect returns."""

    dry: Path
    reverb_return: Path | None
    delay_return: Path | None
    gain_reduction: dict[str, float]
    warnings: list[str]
    delay_ms: float | None = None  # the delay's resolved time (tempo sync made visible)


def _reverb_ir(scratch: Path, reverb: ReverbReturn) -> Path:
    """A deterministic stereo impulse response: decay length, pre-delay and damping.

    Two decorrelated pink-noise channels with an exponential fade make the room; damping is a
    lowpass on the tail (1 = dark, 0 = bright) and the pre-delay is leading silence. The IR is
    normalized to unit energy so a convolution does not change the signal's level: the ``wet``
    fraction then mixes the reverb in at a predictable amount (afir runs with ``gtype=none``).
    The file is cached per settings inside the scratch directory.
    """
    target = scratch / f"reverb_ir_{reverb.decay_s:g}_{reverb.predelay_ms:g}_{reverb.damping:g}.wav"
    if target.is_file():
        return target
    raw = scratch / f"reverb_ir_{reverb.decay_s:g}_{reverb.predelay_ms:g}_{reverb.damping:g}_raw.wav"
    cutoff = 12000.0 - reverb.damping * 10500.0  # 0 -> 12 kHz, 1 -> 1.5 kHz
    _ffmpeg(
        [
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:seed={IR_SEED}:duration={reverb.decay_s:.3f}:sample_rate=44100",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=pink:seed={IR_SEED + 1}:duration={reverb.decay_s:.3f}:sample_rate=44100",
            "-filter_complex",
            "[0:a][1:a]join=inputs=2:channel_layout=stereo,"
            f"afade=t=out:st=0:d={reverb.decay_s:.3f}:curve=exp,"
            f"lowpass=f={cutoff:.0f},adelay={reverb.predelay_ms:g}:all=1[ir]",
            "-map",
            "[ir]",
            str(raw),
        ]
    )
    energy_file = scratch / "reverb_ir_energy.raw"
    _ffmpeg(["-y", "-loglevel", "error", "-i", str(raw), "-f", "f32le", str(energy_file)])
    import array

    samples = array.array("f")
    samples.frombytes(energy_file.read_bytes())
    energy = sum(value * value for value in samples)
    gain = 1.0 / math.sqrt(energy) if energy > 0 else 1.0
    _ffmpeg(["-y", "-loglevel", "error", "-i", str(raw), "-af", f"volume={gain:.8f}", str(target)])
    return target


def _eq_filters(eq: VocalEq) -> list[str]:
    """The FFmpeg filter strings for one voice's EQ, in application order."""
    filters: list[str] = []
    if eq.highpass_hz is not None:
        filters.append(f"highpass=f={eq.highpass_hz:g}")
    for bell in eq.bells:
        filters.append(f"equalizer=f={bell.hz:g}:width_type=q:width={bell.q:g}:g={bell.gain_db:g}")
    if eq.low_shelf is not None:
        filters.append(f"lowshelf=f={eq.low_shelf.hz:g}:width_type=q:width=0.707:g={eq.low_shelf.gain_db:g}")
    if eq.high_shelf is not None:
        filters.append(
            f"highshelf=f={eq.high_shelf.hz:g}:width_type=q:width=0.707:g={eq.high_shelf.gain_db:g}"
        )
    return filters


def _compressor_filter(comp: VocalCompressor) -> str:
    """The FFmpeg acompressor string; makeup is given in dB and converted to linear gain."""
    makeup = 10.0 ** (comp.makeup_db / 20.0)
    return (
        f"acompressor=threshold={comp.threshold_db:g}dB:ratio={comp.ratio:g}:"
        f"attack={comp.attack_ms:g}:release={comp.release_ms:g}:makeup={makeup:.6f}:knee={comp.knee_db:g}"
    )


def chain_filters(chain: VocalChain | None, include_compressor: bool = True) -> list[str]:
    """FFmpeg filters for a processing chain: EQ, de-esser, saturation, compressor.

    The same stages serve vocals and per-track instrument chains, in this order.
    """
    if chain is None:
        return []
    parts: list[str] = []
    if chain.eq is not None:
        parts.extend(_eq_filters(chain.eq))
    if chain.deesser is not None and chain.deesser.enabled:
        deesser = chain.deesser
        parts.append(f"deesser=i={deesser.intensity:g}:f={deesser.frequency:g}:m={deesser.makeup:g}")
    if chain.saturation is not None and chain.saturation.enabled:
        drive = chain.saturation.drive_db
        # pre-gain into the tanh curve, then compensated: peaks stay put, harmonics are added
        parts.append(f"volume={drive:g}dB,asoftclip=type=tanh,volume=-{drive:g}dB")
    if include_compressor and chain.compressor is not None and chain.compressor.enabled:
        parts.append(_compressor_filter(chain.compressor))
    return parts


def _voice_chain_filters(voice: ProcessedVoice, include_compressor: bool = True) -> list[str]:
    """A voice's filters from raw input to its processed stereo output (no sends)."""
    parts = ["aformat=channel_layouts=mono"]
    delay_ms = round(voice.start_seconds * 1000)
    if delay_ms:
        parts.append(f"adelay={delay_ms}:all=1")
    left = math.cos((voice.pan + 1.0) * math.pi / 4.0)
    right = math.sin((voice.pan + 1.0) * math.pi / 4.0)
    parts.append(f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c0")
    if voice.gain_db:
        parts.append(f"volume={voice.gain_db:g}dB")
    parts.extend(chain_filters(voice.chain, include_compressor=include_compressor))
    return parts


def apply_stem_chain(stem: Path, chain: VocalChain | None) -> None:
    """Run a spec's built-in chain over its rendered stem, in place (via a temp file).

    EQ/saturation/compression and an optional convolution reverb without a plugin host; any
    engine's stem can take it, before VST effect chains.
    """
    filters = chain_filters(chain)
    reverb = chain.reverb if chain is not None else None
    if reverb is not None and reverb.wet <= 0.0:
        reverb = None
    if not filters and reverb is None:
        return
    target = stem.with_name(stem.stem + ".chain.wav")
    try:
        if reverb is None:
            _ffmpeg(["-y", "-loglevel", "error", "-i", str(stem), "-af", ",".join(filters), str(target)])
        else:
            ir_path = _reverb_ir(stem.parent, reverb)
            # afir's output is only as long as its input: pad with the IR's length first, so the
            # tail rings past the stem's last sample (the combined mix then runs slightly longer).
            pad = _wav_duration(ir_path)
            steps = [f"[0:a]{','.join(filters)}[dry]" if filters else "[0:a]anull[dry]"]
            steps.append("[dry]asplit=2[dryout][wetin]")
            steps.append(f"[wetin]apad=pad_dur={pad:.3f}[wpadded]")
            steps.append(f"[wpadded][1:a]afir=wet={reverb.wet:g}:dry=0:gtype=none[wet]")
            steps.append("[dryout][wet]amix=inputs=2:duration=longest:normalize=0[out]")
            _ffmpeg(
                [
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(stem),
                    "-i",
                    str(ir_path),
                    "-filter_complex",
                    ";".join(steps),
                    "-map",
                    "[out]",
                    str(target),
                ]
            )
        target.replace(stem)
    except SingingError as exc:
        raise renderers.RenderError(str(exc)) from None
    finally:
        target.unlink(missing_ok=True)


def _render_voice(
    voice: ProcessedVoice,
    index: int,
    scratch: Path,
    ir_path: Path | None,
    include_compressor: bool = True,
) -> tuple[dict[str, Path], float | None, list[str]]:
    """Render one voice: its dry output plus any reverb/delay sends.

    One FFmpeg pass produces the processed dry voice and the scaled sends (the dry file is the
    first output, sends follow in reverb-then-delay order). With ``reverb_mix`` the reverb is an
    insert on the voice itself instead of a shared return. Returns the output paths, the
    compressor's gain reduction in dB (None when there is no compressor) and warnings.
    """
    warnings: list[str] = []
    mono = scratch / f"voice_{index}_mono.wav"
    _ffmpeg(["-y", "-loglevel", "error", "-i", str(voice.path), "-ac", "1", "-ar", "44100", str(mono)])
    parts = _voice_chain_filters(voice, include_compressor=include_compressor)
    filters = [f"[0:a]{','.join(parts)}[chain]"]
    inputs = [str(mono)]
    label = "chain"
    if voice.reverb_mix is not None and ir_path is not None:
        inputs.append(str(ir_path))
        # afir's output is only as long as its input: pad with the IR's length first, so the
        # insert's reverb tail is not cut off at the voice's last sample
        pad = _wav_duration(ir_path)
        filters.append(f"[{label}]asplit=2[vdry][vwet]")
        filters.append(f"[vwet]apad=pad_dur={pad:.3f}[vpadded]")
        filters.append(f"[vpadded][1:a]afir=wet={voice.reverb_mix:g}:dry=0:gtype=none[vrev]")
        filters.append("[vdry][vrev]amix=inputs=2:duration=longest:normalize=0[vmix]")
        label = "vmix"
    sends: list[tuple[str, float]] = []
    if voice.sends is not None:
        if voice.sends.reverb_db is not None:
            sends.append(("reverb", voice.sends.reverb_db))
        if voice.sends.delay_db is not None:
            sends.append(("delay", voice.sends.delay_db))
    outputs: dict[str, Path] = {}
    dry_out = scratch / f"voice_{index}_dry.wav"
    if sends:
        filters.append(
            f"[{label}]asplit={len(sends) + 1}[dryout]"
            + "".join(f"[s{position}]" for position in range(len(sends)))
        )
        map_labels = ["[dryout]"]
        for position, (kind, level_db) in enumerate(sends):
            filters.append(f"[s{position}]volume={level_db:g}dB[s{position}out]")
            map_labels.append(f"[s{position}out]")
            outputs[kind] = scratch / f"voice_{index}_{kind}_send.wav"
    else:
        map_labels = [f"[{label}]"]
    command = ["-y", "-loglevel", "error"]
    for path in inputs:
        command += ["-i", path]
    command += ["-filter_complex", ";".join(filters)]
    # each -map sits right before its output file: with several outputs, trailing maps would
    # all attach to the first file
    command += ["-map", map_labels[0], "-ar", "44100", str(dry_out)]
    for map_label, target_path in zip(map_labels[1:], outputs.values(), strict=True):
        command += ["-map", map_label, str(target_path)]
    _ffmpeg(command)
    outputs["dry"] = dry_out
    reduction: float | None = None
    if voice.chain is not None and voice.chain.compressor is not None and voice.chain.compressor.enabled:
        # measure the reduction on plain chain renders (no reverb insert), so the number is the
        # compressor's own work
        with_compressor = scratch / f"voice_{index}_gr_with.wav"
        without_compressor = scratch / f"voice_{index}_gr_without.wav"
        for path, with_comp in ((with_compressor, True), (without_compressor, False)):
            chain_parts = _voice_chain_filters(voice, include_compressor=with_comp)
            _ffmpeg(
                [
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(mono),
                    "-filter_complex",
                    f"[0:a]{','.join(chain_parts)}[v]",
                    "-map",
                    "[v]",
                    "-ar",
                    "44100",
                    str(path),
                ]
            )
        # raw gain reduction (before makeup), like a DAW's meter: with = without - raw + makeup,
        # so the measured difference plus the makeup recovers the compressor's own work
        compressor = voice.chain.compressor
        reduction = gated_rms_db(without_compressor) - gated_rms_db(with_compressor) + compressor.makeup_db
        if reduction > 6.0:
            warnings.append(f"voice '{voice.name}': the compressor is working hard ({reduction:.1f} dB)")
    return outputs, reduction, warnings


def _sum_files(paths: list[Path], target: Path) -> None:
    """Sum wav files into one (amix, no normalization)."""
    if len(paths) == 1:
        _ffmpeg(["-y", "-loglevel", "error", "-i", str(paths[0]), str(target)])
        return
    command = ["-y", "-loglevel", "error"]
    for path in paths:
        command += ["-i", str(path)]
    labels = "".join(f"[{index}:a]" for index in range(len(paths)))
    command += [
        "-filter_complex",
        f"{labels}amix=inputs={len(paths)}:duration=longest:normalize=0[m]",
        "-map",
        "[m]",
        str(target),
    ]
    _ffmpeg(command)


def _apply_gain(source: Path, gain_db: float, target: Path) -> None:
    _ffmpeg(["-y", "-loglevel", "error", "-i", str(source), "-af", f"volume={gain_db:g}dB", str(target)])


def _limiter_filter(ceiling_db: float) -> str:
    """alimiter with a dB ceiling; level=false keeps the output at the ceiling (no auto level).

    ``latency=true`` compensates the lookahead: the default would push the output ~5 ms late
    (and cut that much off the end), which would misalign the mix against the backing or stems.
    """
    limit = 10.0 ** (ceiling_db / 20.0)
    return f"alimiter=limit={limit:.6f}:level=false:latency=true"


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def _reverb_return(send: Path, ir_path: Path, target: Path) -> None:
    """Convolve the summed reverb sends with the impulse response (wet only).

    afir's output is only as long as its input, so the send is padded with the IR's length
    first: the padded window holds the full convolution tail, which is the point of a reverb.
    """
    pad = _wav_duration(ir_path)
    _ffmpeg(
        [
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(send),
            "-i",
            str(ir_path),
            "-filter_complex",
            f"[0:a]apad=pad_dur={pad:.3f}[p];[p][1:a]afir=wet=1:dry=0:gtype=none[m]",
            "-map",
            "[m]",
            str(target),
        ]
    )


def _delay_return(send: Path, delay_ms: float, feedback: float, target: Path) -> None:
    """The delay return: the send repeated at the delay time, each repeat quieter.

    ffmpeg's ``aecho`` is a single-tap delay (its decay scales one echo, it does not repeat),
    so the repeats are built explicitly: delayed copies at 1..N times the delay, scaled
    ``feedback``, ``feedback²``, ... and summed. Repeats are kept while above -40 dB (at most
    8), and the dry path stays clean.
    """
    taps = 1
    while feedback > 0.0 and taps < 8 and feedback ** (taps + 1) > 0.01:
        taps += 1
    filters = [f"[0:a]asplit={taps}" + "".join(f"[t{position}]" for position in range(taps))]
    labels = []
    for position in range(taps):
        level = feedback ** (position + 1)
        filters.append(
            f"[t{position}]adelay={round(delay_ms * (position + 1))}:all=1,volume={level:.6f}[t{position}out]"
        )
        labels.append(f"[t{position}out]")
    filters.append(f"{''.join(labels)}amix=inputs={taps}:duration=longest:normalize=0[m]")
    _ffmpeg(
        [
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(send),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[m]",
            str(target),
        ]
    )


def _delay_ms(delay: DelayReturn, starting_bpm: float | None) -> float:
    """Resolve a delay's time: explicit ms, or a note value at the song's starting tempo."""
    if delay.delay_ms is not None:
        return delay.delay_ms
    bpm = starting_bpm or 120.0
    quarter = 60000.0 / bpm
    return {"1/4": quarter, "1/8": quarter / 2, "1/8d": quarter * 0.75, "1/16": quarter / 4}[
        delay.tempo_sync or "1/4"
    ]


def _render_processed_vocals(
    voices: list[ProcessedVoice],
    vocal_bus: VocalBus | None,
    returns: Returns | None,
    starting_bpm: float | None,
    scratch: Path,
) -> ProcessedVocals:
    """Run the staged vocal pipeline: per-voice chains, sends, the bus, and the returns."""
    warnings: list[str] = []
    chained_reverb = [
        voice.name for voice in voices if voice.chain is not None and voice.chain.reverb is not None
    ]
    if chained_reverb:
        warnings.append(
            "Voice chains set 'reverb' (" + ", ".join(chained_reverb) + "); it applies to "
            "instrument stems — for voices use the mix reverb return or a voice's reverb send."
        )
    gain_reduction: dict[str, float] = {}
    ir_path: Path | None = None
    needs_ir = bool(returns is not None and returns.reverb is not None) or any(
        voice.reverb_mix is not None for voice in voices
    )
    if needs_ir:
        reverb_settings = (
            returns.reverb if returns is not None and returns.reverb is not None else ReverbReturn()
        )
        ir_path = _reverb_ir(scratch, reverb_settings)
    rendered: list[dict[str, Path]] = []
    for index, voice in enumerate(voices):
        outputs, reduction, voice_warnings = _render_voice(voice, index, scratch, ir_path)
        warnings.extend(voice_warnings)
        if reduction is not None:
            gain_reduction[f"voice '{voice.name}' compressor"] = round(reduction, 2)
        rendered.append(outputs)
    bus_source = scratch / "bus_pre.wav"
    _sum_files([outputs["dry"] for outputs in rendered], bus_source)
    bus_current = bus_source
    if vocal_bus is not None and vocal_bus.gain_db:
        gained = scratch / "bus_gained.wav"
        _apply_gain(bus_current, vocal_bus.gain_db, gained)
        bus_current = gained
    if vocal_bus is not None and vocal_bus.compressor is not None and vocal_bus.compressor.enabled:
        compressed = scratch / "bus_compressed.wav"
        _ffmpeg(
            [
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(bus_current),
                "-af",
                _compressor_filter(vocal_bus.compressor),
                str(compressed),
            ]
        )
        gain_reduction["bus compressor"] = round(
            gated_rms_db(bus_current) - gated_rms_db(compressed) + vocal_bus.compressor.makeup_db, 2
        )
        bus_current = compressed
    if vocal_bus is not None and vocal_bus.limiter is not None and vocal_bus.limiter.enabled:
        limited = scratch / "bus_limited.wav"
        _ffmpeg(
            [
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(bus_current),
                "-af",
                _limiter_filter(vocal_bus.limiter.ceiling_db),
                str(limited),
            ]
        )
        reduction = audio.wav_peak_db(bus_current) - audio.wav_peak_db(limited)
        gain_reduction["bus limiter"] = round(reduction, 2)
        if reduction > 2.0:
            warnings.append(f"the bus limiter takes off {reduction:.1f} dB; something earlier is too hot")
        bus_current = limited
    reverb_return_path: Path | None = None
    delay_return_path: Path | None = None
    resolved_delay_ms: float | None = None
    if returns is not None:
        reverb_sends = [outputs["reverb"] for outputs in rendered if "reverb" in outputs]
        if reverb_sends and ir_path is not None:
            summed = scratch / "reverb_send.wav"
            _sum_files(reverb_sends, summed)
            reverb_return_path = scratch / "reverb_return.wav"
            _reverb_return(summed, ir_path, reverb_return_path)
        delay_sends = [outputs["delay"] for outputs in rendered if "delay" in outputs]
        if delay_sends and returns.delay is not None:
            summed = scratch / "delay_send.wav"
            _sum_files(delay_sends, summed)
            delay_return_path = scratch / "delay_return.wav"
            resolved_delay_ms = _delay_ms(returns.delay, starting_bpm)
            _delay_return(summed, resolved_delay_ms, returns.delay.feedback, delay_return_path)
    return ProcessedVocals(
        dry=bus_current,
        reverb_return=reverb_return_path,
        delay_return=delay_return_path,
        gain_reduction=gain_reduction,
        warnings=warnings,
        delay_ms=resolved_delay_ms,
    )


def _loudness_report(path: Path, raw_f32: bool = False) -> dict:
    """Integrated LUFS, loudness range, true peak and short-term max via ebur128.

    With ``raw_f32`` the input is the uncorrected float mix probe (headerless f32le stereo at
    44.1 kHz), so the loudness is measured before the gain correction — loudness scales with
    gain, so the correction can be computed from it.
    """
    command = ["-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"]
    if raw_f32:
        command = [
            "-f",
            "f32le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    result = _ffmpeg(command)
    text = result.stderr
    integrated = re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", text)
    lra = re.findall(r"LRA:\s*(-?\d+\.?\d*)\s*LU", text)
    peak = re.findall(r"Peak:\s*(-?\d+\.?\d*)\s*dBFS", text)
    short_term = [float(value) for value in re.findall(r"S:\s*(-?\d+\.?\d*)", text) if float(value) > -100.0]
    return {
        "integrated_lufs": round(float(integrated[-1]), 2) if integrated else None,
        "loudness_range_lu": round(float(lra[-1]), 2) if lra else None,
        "true_peak_db": round(float(peak[-1]), 2) if peak else None,
        "short_term_max_lufs": round(max(short_term), 2) if short_term else None,
    }


def _combine_vocals(segments: list[tuple[Path, float]], target: Path) -> None:
    """Place each vocal piece at its start time and sum them into one vocal track."""
    args = ["-y", "-loglevel", "error"]
    for path, _ in segments:
        args += ["-i", str(path)]
    filters = []
    labels = []
    for index, (_, start_seconds) in enumerate(segments):
        filters.append(f"[{index}:a]adelay={round(start_seconds * 1000)}:all=1[v{index}]")
        labels.append(f"[v{index}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(segments)}:duration=longest:normalize=0[m]")
    _ffmpeg([*args, "-filter_complex", ";".join(filters), "-map", "[m]", "-ar", "44100", str(target)])


def mix_tracks(
    backing: Path,
    vocal: Path | None,
    target: Path,
    *,
    vocal_level_db: float = 6.0,
    backing_volume: float = 1.0,
    vocal_reverb: bool = False,
    normalize_peak_db: float | None = -1.0,
    compress: bool = False,
    ducking: bool = False,
    duck_db: float = 4.0,
    voices: list[ProcessedVoice] | None = None,
    vocal_bus: VocalBus | None = None,
    returns: Returns | None = None,
    master: MasterSettings | None = None,
    starting_bpm: float | None = None,
) -> dict:
    """Mix the vocal into the backing, balancing levels by measurement.

    ``vocal_level_db`` is the target level of the vocal *above the band as heard* — both measured
    over the blocks where the voice is singing, so quiet backings and loud vocals are corrected
    instead of being multiplied blindly. With ``compress`` the vocal gets a gentle high-pass and
    compressor before the measurement, so its per-note level swings are tamed and the balance is
    computed on what actually lands in the file (off by default: it costs diction on dense
    mixes). ``ducking`` dips the backing by ``duck_db`` while the voice sings and the vocal gain
    is reduced by the same amount, so ``vocal_level_db`` is what the listener hears over the
    dipped band; against the original backing the vocal sits ``vocal_level_db - duck_db`` (both
    ratios are reported). The vocal is converted to the mix rate before measuring: the resampler's
    anti-alias filter costs 24 kHz engine output a couple of dB, and the level that matters is
    the one in the finished file. The peak is measured on a float render, so the export is
    corrected to ``normalize_peak_db`` (default -1 dBFS) from the true peak, or guarded to
    -0.1 dBFS when normalization is off: a clipped file is never written.

    With ``voices`` the staged vocal pipeline runs instead of a single vocal file: each voice
    keeps its own chain (EQ, compressor), gain, pan and sends, the results are summed into the
    vocal bus (``vocal_bus``: gain, compressor, limiter) and the bus feeds the same measurement
    and mix as before, so ``vocal_level_db`` means the same thing. ``returns`` adds a shared
    reverb (a generated impulse response) and delay fed by the voices' sends; ``master`` adds a
    true-peak-safe limiter and, with ``loudness_target_lufs``, a static gain to hit a loudness
    target (the peak normalization is superseded; the result reports what was measured).
    Everything off keeps the graph byte-identical to a plain mix.
    """
    original_vocal = vocal
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        processed: ProcessedVocals | None = None
        if voices is not None:
            processed = _render_processed_vocals(voices, vocal_bus, returns, starting_bpm, scratch_path)
            vocal = processed.dry
            if compress:
                processed.warnings.append(
                    "compress was ignored: the voice chain has its own compressor (use its settings)"
                )
        else:
            if vocal is None:
                raise SingingError("A vocal file is required when there are no voices.")
            vocal = scratch_path / "vocal_mix_rate.wav"
            _ffmpeg(
                [
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(original_vocal),
                    "-ac",
                    "1",
                    "-ar",
                    "44100",
                    str(vocal),
                ]
            )
            if compress:
                vocal_processed = scratch_path / "vocal_processed.wav"
                _ffmpeg(
                    [
                        "-y",
                        "-loglevel",
                        "error",
                        "-i",
                        str(vocal),
                        "-af",
                        "highpass=f=75,acompressor=threshold=-24dB:ratio=3:attack=10:release=250:makeup=2",
                        str(vocal_processed),
                    ]
                )
                vocal = vocal_processed
        mix_backing = backing
        effective_duck = 0.0
        if ducking and duck_db > 0:
            mix_backing = scratch_path / "backing_ducked.wav"
            _duck_backing(backing, vocal, mix_backing, duck_db)
            effective_duck = duck_db
        info = _mix_tracks_at_mix_rate(
            backing,
            mix_backing,
            vocal,
            target,
            scratch_path,
            vocal_level_db=vocal_level_db,
            backing_volume=backing_volume,
            vocal_reverb=vocal_reverb,
            normalize_peak_db=normalize_peak_db,
            duck_db=effective_duck,
            returns_paths=(processed.reverb_return, processed.delay_return) if processed else (None, None),
            master=master,
            staged_vocal=processed is not None,
            air_layer=vocal_bus.air_layer if (vocal_bus is not None and processed is not None) else None,
        )
        if processed is not None:
            info["gain_reduction_db"] = processed.gain_reduction
            info["resolved_delay_ms"] = processed.delay_ms
            info["warnings"] = processed.warnings + info["warnings"]
        return info


def _mix_tracks_at_mix_rate(
    measure_backing: Path,
    mix_backing: Path,
    vocal: Path,
    target: Path,
    scratch: Path,
    *,
    vocal_level_db: float,
    backing_volume: float,
    vocal_reverb: bool,
    normalize_peak_db: float | None,
    duck_db: float = 0.0,
    returns_paths: tuple[Path | None, Path | None] = (None, None),
    master: MasterSettings | None = None,
    staged_vocal: bool = False,
    air_layer: AirLayer | None = None,
) -> dict:
    """Mix at 44.1 kHz, measure the balance and correct the export's level.

    The legacy graph (no staged vocal, no returns, no master) is kept exactly as it was, so a
    plain mix stays byte-identical. With a staged vocal the graph skips the legacy mono pan
    (the processed bus is already stereo, so panning it would collapse it); returns and master
    settings add the return inputs (scaled with the vocal), the master limiter (a true-peak-safe
    margin under the ceiling) and the gain correction; the correction is computed on the float
    probe and applied before the limiter, so the limiter always sees the final level. An
    ``air_layer`` adds band-shaped noise gated by the vocal bus (it rides with the vocal).
    """
    backing_db, vocal_db = active_levels(measure_backing, vocal)
    # vocal_level_db means the level the listener hears over the band during phrases. With
    # ducking the band dips by duck_db, so the vocal is targeted duck_db lower against the
    # original backing and lands at vocal_level_db above the dipped band.
    vocal_gain_db = (backing_db + vocal_level_db - duck_db) - vocal_db
    vocal_gain = 10.0 ** (vocal_gain_db / 20.0)
    trim_db = 20.0 * math.log10(backing_volume) if backing_volume > 0 else -120.0
    balance_db = (vocal_db + vocal_gain_db) - (backing_db + trim_db - duck_db)
    limiter_on = master is not None and master.limiter is not None and master.limiter.enabled
    staged = staged_vocal or returns_paths != (None, None) or master is not None or air_layer is not None
    air_noise: Path | None = None
    air_gain: float | None = None
    if air_layer is not None:
        # the noise runs as long as the longest input, so the air never extends the mix
        duration = max(
            _wav_duration(mix_backing),
            _wav_duration(vocal),
            *(_wav_duration(path) for path in returns_paths if path is not None),
        )
        air_noise = scratch / "air_noise.wav"
        _ffmpeg(
            [
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"anoisesrc=color={air_layer.color}:seed={IR_SEED + 7}:sample_rate=44100:"
                f"duration={duration:.3f}",
                "-ac",
                "2",
                str(air_noise),
            ]
        )
        # measured, like the balance: the air sits level_db under the vocal's active level
        air_gain_db = (vocal_db + air_layer.level_db) - gated_rms_db(air_noise)
        air_gain = vocal_gain * 10.0 ** (air_gain_db / 20.0)

    def render(output_gain_db: float, destination: Path, float_output: bool = False) -> None:
        if not staged:
            command = [
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mix_backing),
                "-i",
                str(vocal),
                "-filter_complex",
                _mix_filters(vocal_gain, backing_volume, vocal_reverb, output_gain_db),
                "-map",
                "[m]",
                "-ar",
                "44100",
            ]
        else:
            # the staged graph: the processed bus is already stereo, so it skips the legacy pan;
            # the returns ride with the vocal gain (they are part of the vocal's sound)
            command = ["-y", "-loglevel", "error", "-i", str(mix_backing), "-i", str(vocal)]
            extra: list[tuple[int, str]] = []
            input_index = 2
            for path, kind in zip(returns_paths, ("rv", "dl"), strict=True):
                if path is not None:
                    command += ["-i", str(path)]
                    extra.append((input_index, kind))
                    input_index += 1
            air_index: int | None = None
            if air_noise is not None:
                command += ["-i", str(air_noise)]
                air_index = input_index
            vocal_filters = f"volume={vocal_gain:.4f}"
            if vocal_reverb:
                vocal_filters += ",aecho=1.0:1.0:60:0.25"
            if air_index is not None:
                # the vocal is split: one copy for the mix, one as the gate's sidechain
                filters = [
                    f"[0:a]volume={backing_volume:.3f}[b]",
                    f"[1:a]{vocal_filters},asplit=2[v][vsc]",
                ]
            else:
                filters = [f"[0:a]volume={backing_volume:.3f}[b]", f"[1:a]{vocal_filters}[v]"]
            mix_labels = "[b][v]"
            for extra_index, kind in extra:
                filters.append(f"[{extra_index}:a]volume={vocal_gain:.4f}[{kind}]")
                mix_labels += f"[{kind}]"
            inputs_count = 2 + len(extra)
            if air_index is not None:
                assert air_gain is not None and air_layer is not None
                threshold = 10.0 ** (air_layer.threshold_db / 20.0)
                filters.append(
                    f"[{air_index}:a]highpass=f={air_layer.highpass_hz:g},"
                    f"lowpass=f={air_layer.lowpass_hz:g}[airraw]"
                )
                filters.append(
                    f"[airraw][vsc]sidechaingate=range=0:threshold={threshold:.6f}:"
                    f"attack={air_layer.attack_ms:g}:release={air_layer.release_ms:g}[airgate]"
                )
                filters.append(f"[airgate]volume={air_gain:.6f}[airout]")
                mix_labels += "[airout]"
                inputs_count += 1
            graph = ";".join(filters)
            graph += f";{mix_labels}amix=inputs={inputs_count}:duration=longest:normalize=0[pm]"
            label = "pm"
            if output_gain_db:
                graph += f";[{label}]volume={output_gain_db:.2f}dB[g]"
                label = "g"
            if limiter_on:
                graph += f";[{label}]{_limiter_filter(master.limiter.ceiling_db - LIMITER_MARGIN_DB)}[ml]"
                label = "ml"
            command += ["-filter_complex", graph, "-map", f"[{label}]", "-ar", "44100"]
        if float_output:
            command += ["-f", "f32le"]
        command.append(str(destination))
        _ffmpeg(command)

    # Measure the true peak on a float render: the 16-bit file clamps at 0 dBFS, so a clipped
    # peak cannot be told apart from a loud one, and a correction computed from it only ever
    # turns the file down by the 1 dB margin.
    probe = scratch / "probe_f32.raw"
    render(0.0, probe, float_output=True)
    true_peak = _float_peak_db(probe)
    loudness_target = master.loudness_target_lufs if master is not None else None
    if loudness_target is not None:
        # loudness scales with gain, so the correction to hit the target is read off the probe
        probe_loudness = _loudness_report(probe, raw_f32=True)
        integrated = probe_loudness["integrated_lufs"]
        correction = round(loudness_target - integrated, 2) if integrated is not None else 0.0
    elif normalize_peak_db is not None:
        correction = round(normalize_peak_db - true_peak, 2)
    elif true_peak > -0.1:
        correction = round(-0.1 - true_peak, 2)  # clipping guard: raw level, but never clipped
    else:
        correction = 0.0
    render(correction, target)
    peak = _peak_db(target)

    loudness: dict | None = None
    limiter_reduction: float | None = None
    master_warnings: list[str] = []
    if master is not None:
        loudness = _loudness_report(target)
        if loudness_target is not None and loudness["integrated_lufs"] is not None:
            if abs(loudness["integrated_lufs"] - loudness_target) > 1.0:
                master_warnings.append(
                    f"the mix landed at {loudness['integrated_lufs']} LUFS, not the "
                    f"{loudness_target:g} LUFS target"
                )
        if limiter_on:
            # the probe is already limited, so this is a lower bound on the limiter's work
            limiter_reduction = round(true_peak + correction - peak, 2)
            if limiter_reduction > 2.0:
                master_warnings.append(
                    f"the master limiter takes off {limiter_reduction:.1f} dB; "
                    "leave more headroom or tame the bus"
                )
            if (
                loudness["true_peak_db"] is not None
                and loudness["true_peak_db"] > master.limiter.ceiling_db + 0.05
            ):
                master_warnings.append(
                    f"true peak {loudness['true_peak_db']:g} dBFS is over the "
                    f"{master.limiter.ceiling_db:g} dBFS ceiling"
                )
        if not limiter_on and peak > -0.05:
            master_warnings.append(
                "the mix clips at the target; lower loudness_target_lufs or enable the master limiter"
            )

    # verify the balance by measurement on the uncorrected probe: the mix holds band + vocal
    # (uncorrelated), so subtracting the band's energy — measured from the backing file over the
    # same voiced blocks, which stays honest when the backing is rendered wet — leaves the
    # vocal's energy (with the vocal reverb riding along, the ratio reflects what is heard).
    measured_db: float | None = None
    mix_energy = _f32_mix_energy_over_sung(vocal, probe)
    band_energy = (
        _wav_energy_over_sung(vocal, measure_backing) * (backing_volume**2) * 10.0 ** (-duck_db / 10.0)
    )
    if band_energy > 0 and mix_energy > band_energy:
        measured_db = 10.0 * math.log10((mix_energy - band_energy) / band_energy)
    if measured_db is None:
        balance_check = "unavailable"
    elif abs(measured_db - balance_db) <= 1.5:
        balance_check = "ok"
    else:
        balance_check = "mismatch"

    with wave.open(str(target), "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
    return {
        "peak_db": peak,
        # whether the export itself clips; a hot raw mix is turned down (gain_correction_db)
        "clipping": peak > -0.05,
        "gain_correction_db": correction,
        "normalize_peak_db": normalize_peak_db,
        "duck_db": duck_db if duck_db > 0 else None,
        "duration_seconds": round(duration, 3),
        "backing_rms_db": round(backing_db, 1),
        "vocal_rms_db": round(vocal_db, 1),
        "vocal_gain_db": round(vocal_gain_db, 1),
        "vocal_to_backing_db": round(balance_db, 1),
        "vocal_to_original_backing_db": round(balance_db - duck_db, 1),
        "vocal_to_backing_measured_db": None if measured_db is None else round(measured_db, 1),
        "balance_check": balance_check,
        "loudness": loudness,
        "limiter_reduction_db": limiter_reduction,
        "warnings": master_warnings,
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


def extract_track(source: Path, target: Path, track_index: int) -> None:
    """Write a copy of ``source`` with the conductor track and one note track (a stem)."""
    midi = mido.MidiFile(source)
    keep = [0, track_index] if track_index != 0 else [0]
    midi.tracks = [midi.tracks[index] for index in keep]
    midi.save(target)


def _mix_stems(stems: list[Path], levels_db: list[float], target: Path) -> None:
    """Combine rendered stems into one backing wav, applying a dB trim per stem."""
    args = ["-y"]
    for stem in stems:
        args += ["-i", str(stem)]
    filters = ";".join(
        f"[{index}:a]volume={10 ** (level / 20.0):.4f}[s{index}]" for index, level in enumerate(levels_db)
    )
    inputs = "".join(f"[s{index}]" for index in range(len(stems)))
    filters += f";{inputs}amix=inputs={len(stems)}:duration=longest:normalize=0[m]"
    _ffmpeg([*args, "-filter_complex", filters, "-map", "[m]", "-ar", "44100", str(target)])


# ------------------------------------------------------------------ MCP tools


def run_mix(args: dict, progress: Callable[[str, str, str | None], None] | None = None) -> dict:
    """The pipeline behind mix_song_with_vocals and its background job.

    ``args`` holds the tool's validated values (the same keys as the tool parameters);
    ``progress`` is called as ``(name, state, error=None)`` for each backing track and for the
    final ``mix`` phase — the background job reports it.
    """
    source = args["source"]
    vocal = args.get("vocal")
    vocals = args.get("vocals")
    guide_track = args.get("guide_track")
    vocal_level_db = args.get("vocal_level_db", 6.0)
    backing_volume = args.get("backing_volume", 1.0)
    backing_levels = args.get("backing_levels")
    vocal_reverb = args.get("vocal_reverb", False)
    vocal_chain = args.get("vocal_chain")
    vocal_sends = args.get("vocal_sends")
    vocal_bus = args.get("vocal_bus")
    returns = args.get("returns")
    master = args.get("master")
    save_chain = args.get("save_chain", False)
    reverb = args.get("reverb")
    chorus = args.get("chorus")
    normalize_peak_db = args.get("normalize_peak_db", -1.0)
    compress = args.get("compress", False)
    ducking = args.get("ducking", False)
    duck_db = args.get("duck_db", 4.0)
    output_filename = args.get("output_filename")
    overwrite = args.get("overwrite", False)
    store = _get_store()
    assert store is not None
    source_path = store.existing_path(source)
    if vocals and vocal:
        raise SingingError("Give either vocal or vocals, not both.")
    if not vocals and not vocal:
        raise SingingError("Give a vocal file (or job id), or a list of vocals with their start_seconds.")
    segment_list: list[dict] | None = None
    resolved_segments: list[tuple[Path, float]] = []
    if vocals:
        segment_list = [segment.model_dump() for segment in vocals]
        resolved_segments = [
            (_resolve_vocal_path(segment.file, store), segment.start_seconds) for segment in vocals
        ]
        vocal_path = None  # built from the segments inside the claim block below
    else:
        vocal_path = _resolve_vocal_path(vocal, store)
    sidecar_path = source_path.with_name(source_path.stem + VOCAL_CHAIN_SUFFIX)
    chain_source = "parameters"
    if (
        all(value is None for value in (vocal_chain, vocal_sends, vocal_bus, returns, master))
        and sidecar_path.is_file()
    ):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        vocal_chain = VocalChain.model_validate(data["vocal_chain"]) if data.get("vocal_chain") else None
        vocal_sends = VocalSends.model_validate(data["vocal_sends"]) if data.get("vocal_sends") else None
        vocal_bus = VocalBus.model_validate(data["vocal_bus"]) if data.get("vocal_bus") else None
        returns = Returns.model_validate(data["returns"]) if data.get("returns") else None
        master = MasterSettings.model_validate(data["master"]) if data.get("master") else None
        chain_source = "sidecar"
    if vocals and (vocal_chain is not None or vocal_sends is not None):
        raise SingingError(
            "vocal_chain/vocal_sends apply to a single vocal; with vocals, put them on each segment."
        )
    voice_list: list[ProcessedVoice] | None = None
    process_voices = (
        vocal_chain is not None
        or vocal_sends is not None
        or vocal_bus is not None
        or returns is not None
        or master is not None
        or (
            vocals is not None
            and any(
                segment.chain is not None
                or segment.sends is not None
                or segment.gain_db
                or segment.pan
                or segment.reverb_mix is not None
                for segment in vocals
            )
        )
    )
    if process_voices:
        if vocals:
            voice_list = [
                ProcessedVoice(
                    path=path,
                    start_seconds=segment.start_seconds,
                    name=segment.name or f"voice {position + 1}",
                    gain_db=segment.gain_db,
                    pan=segment.pan,
                    chain=segment.chain,
                    sends=segment.sends,
                    reverb_mix=segment.reverb_mix,
                )
                for position, ((path, _), segment) in enumerate(zip(resolved_segments, vocals, strict=True))
            ]
        else:
            voice_list = [ProcessedVoice(path=vocal_path, name="voice", chain=vocal_chain, sends=vocal_sends)]
    midi = mido.MidiFile(source_path)
    track_index = find_track(midi, guide_track)
    names = _track_names(midi)
    note_tracks = [
        index
        for index, track in enumerate(midi.tracks)
        if index != track_index and any(msg.type == "note_on" and msg.velocity > 0 for msg in track)
    ]
    if backing_levels is not None:
        if midi.type == 0:
            raise SingingError("Per-stem levels need a type-1 file with separate tracks.")
        unknown = sorted(set(backing_levels) - {names[index] for index in note_tracks})
        if unknown:
            available = ", ".join(names[index] for index in note_tracks) or "(none)"
            raise SingingError(f"Unknown backing track(s) {', '.join(unknown)}. Tracks: {available}.")
    specs = renderers.load_specs(source_path)
    if specs:
        # muted tracks stay out of the backing entirely
        note_tracks = [
            index for index in note_tracks if not (specs.get(names[index]) and specs[names[index]].mute)
        ]
    use_stems = backing_levels is not None or bool(specs)
    resolved_sections = sections.read_sections(source_path)
    section_timeline = sections.timeline_seconds(resolved_sections)
    starting_bpm = resolved_sections[0].bpm if resolved_sections else None
    effects = renderers.effect_options(reverb, chorus)
    effects_echo = {
        "reverb": reverb.model_dump() if reverb is not None else None,
        "chorus": chorus.model_dump() if chorus is not None else None,
    }
    with (
        # The backing is an internal intermediate: always claim a unique name, even when the
        # mix itself is overwritten, so two concurrent mixes can never share (and clobber)
        # the same backing file.
        store.claimed_path(None, f"{source_path.stem}_backing", extension=".wav") as backing_wav,
        store.claimed_path(None, f"{source_path.stem}_vocals", extension=".wav") as vocals_wav,
        store.claimed_path(output_filename, f"{source_path.stem}_mix", overwrite, ".wav") as target,
    ):
        if voice_list is None and resolved_segments:
            # several vocal pieces (takes, or a vocal rendered in chunks): place each at its
            # start time and sum them into one vocal track
            _combine_vocals(resolved_segments, vocals_wav)
            vocal_path = vocals_wav
        else:
            vocals_wav.unlink(missing_ok=True)
        instruments: dict[str, dict] | None = None
        if not use_stems:
            if progress is not None:
                progress("backing", "running")
            with store.claimed_path(None, f"{source_path.stem}_backing", extension=".mid") as backing_midi:
                remove_track(source_path, backing_midi, track_index)
                audio.render(backing_midi, backing_wav, "wav", effects=effects)
            if progress is not None:
                progress("backing", "done")
        else:
            with tempfile.TemporaryDirectory() as scratch:

                def render_backing_track(index: int) -> tuple[str, Path, float, renderers.InstrumentSpec]:
                    name = names[index]
                    if progress is not None:
                        progress(name, "running")
                    spec = specs.get(name) or renderers.InstrumentSpec()
                    gain_db = (
                        backing_levels.get(name, spec.gain_db) if backing_levels is not None else spec.gain_db
                    )
                    stem_midi = Path(scratch) / f"stem_{index}.mid"
                    stem_wav = Path(scratch) / f"stem_{index}.wav"
                    extract_track(source_path, stem_midi, index)
                    renderer = renderers.get_renderer(spec.engine)
                    try:
                        renderer.render(stem_midi, spec, stem_wav, effects)
                        windows = renderers.section_gain_windows(spec, section_timeline)
                        if windows:
                            renderers.apply_section_gains(stem_wav, windows, spec.gain_db)
                        apply_stem_chain(stem_wav, spec.chain)
                        renderers.apply_stem_effects(stem_wav, spec.effects)
                    except renderers.RenderError as exc:
                        if progress is not None:
                            progress(name, "failed", f"Track '{name}': {exc}")
                        raise SingingError(f"Track '{name}': {exc}") from None
                    if progress is not None:
                        progress(name, "done")
                    return name, stem_wav, gain_db, spec

                rendered = renderers.parallel_map(render_backing_track, note_tracks)
                _mix_stems(
                    [stem_wav for _, stem_wav, _, _ in rendered],
                    [gain_db for _, _, gain_db, _ in rendered],
                    backing_wav,
                )
                instruments = {
                    name: {**spec.model_dump(), "gain_db": gain_db} for name, _, gain_db, spec in rendered
                }
        if progress is not None:
            progress("mix", "running")
        info = mix_tracks(
            backing_wav,
            vocal_path,
            target,
            vocal_level_db=vocal_level_db,
            backing_volume=backing_volume,
            vocal_reverb=vocal_reverb,
            normalize_peak_db=normalize_peak_db,
            compress=compress,
            ducking=ducking,
            duck_db=duck_db,
            voices=voice_list,
            vocal_bus=vocal_bus,
            returns=returns,
            master=master,
            starting_bpm=starting_bpm,
        )
        if save_chain:
            payload: dict[str, Any] = {}
            if vocal_chain is not None:
                payload["vocal_chain"] = vocal_chain.model_dump(exclude_none=True)
            if vocal_sends is not None:
                payload["vocal_sends"] = vocal_sends.model_dump(exclude_none=True)
            if vocal_bus is not None:
                payload["vocal_bus"] = vocal_bus.model_dump(exclude_none=True)
            if returns is not None:
                payload["returns"] = returns.model_dump(exclude_none=True)
            if master is not None:
                payload["master"] = master.model_dump(exclude_none=True)
            sidecar_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    gain_reduction = info.pop("gain_reduction_db", {})
    loudness = info.pop("loudness", None)
    limiter_reduction = info.pop("limiter_reduction_db", None)
    resolved_delay_ms = info.pop("resolved_delay_ms", None)
    warnings = info.pop("warnings", [])
    processing = None
    if process_voices:
        processing = {
            "chain_source": chain_source,
            "vocal_chain": vocal_chain.model_dump(exclude_none=True) if vocal_chain is not None else None,
            "vocal_sends": vocal_sends.model_dump(exclude_none=True) if vocal_sends is not None else None,
            "vocal_bus": vocal_bus.model_dump(exclude_none=True) if vocal_bus is not None else None,
            "returns": returns.model_dump(exclude_none=True) if returns is not None else None,
            "master": master.model_dump(exclude_none=True) if master is not None else None,
            "gain_reduction_db": gain_reduction,
            "loudness": loudness,
            "limiter_reduction_db": limiter_reduction,
            "resolved_delay_ms": resolved_delay_ms,
        }
    if progress is not None:
        progress("mix", "done")
    return {
        "filename": target.name,
        "path": str(target),
        "uri": f"midi://{target.name}",
        "mime_type": "audio/wav",
        "source": source_path.name,
        "vocal": vocal_path.name if vocal_path is not None else f"{len(resolved_segments)} vocal segments",
        "backing": backing_wav.name,
        "levels": {
            "vocal_level_db": vocal_level_db,
            "backing_volume": backing_volume,
            "normalize_peak_db": normalize_peak_db,
            "duck_db": duck_db if ducking else None,
        },
        "backing_levels": backing_levels,
        "instruments": instruments,
        "effects": effects_echo,
        "vocal_segments": segment_list,
        "compress": compress,
        "ducking": ducking,
        "guide_removed": _track_names(midi)[track_index],
        "processing": processing,
        "warnings": warnings,
        **info,
    }


def register(mcp: FastMCP, store_provider: Callable[[], FileStore]) -> None:
    """Register the singing tools on the ChordSmith server."""
    global _get_store
    _get_store = store_provider
    _load_singing()

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
                bank_controls = info.get("soft_controls") or {}
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
                                "breathiness": bool(bank_controls.get("breathiness")),
                                "voicing": bool(bank_controls.get("voicing")),
                                "tension": bool(bank_controls.get("tension")),
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
                description="Melody track: a name, a 1-based index, or omit to auto-pick (an "
                "exact 'Vocal'/'Voice'/'Melody' name first, else the first melody-like name)."
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
        _persist_singing()
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
            str,
            Field(
                description="'en' for English words, 'ja' for kana, 'phonemes' for raw "
                "voicebank phonemes (one token per note, hyphen-separated, e.g. 'l-aa v-ey'); "
                "omit lyrics for wordless mode."
            ),
        ] = "ja",
        words: Annotated[
            dict[str, str] | None,
            Field(
                description="Custom pronunciations for 'en' (checked before the dictionaries), "
                "e.g. {'lah': 'l aa', 'vey': 'v ey'} — word → voicebank phonemes. This is how "
                "made-up languages or names get exact sounds."
            ),
        ] = None,
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
        (all of them at once) and words are never dropped or invented. Made-up words get exact
        sounds through ``words``, or bypass the dictionaries entirely with
        language='phonemes'. Mismatches produce warnings. The next step is render_singing with
        the returned mapping_id.
        """
        with _registry.lock:
            score = _registry.scores.get(score_id)
        if score is None:
            raise SingingError(f"Score '{score_id}' not found (scores expire after 24 hours).")
        mapping = build_mapping(score, lyrics, language, holds, align_stress, words)
        with _registry.lock:
            _registry.versions[score_id] = _registry.versions.get(score_id, 0) + 1
            mapping.version = _registry.versions[score_id]
            _registry.mappings[mapping.mapping_id] = mapping
        _persist_singing()
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

    @mcp.tool(title="List singing jobs", annotations=_READ_ONLY)
    def list_singing_jobs() -> results.SingingJobsResult:
        """Return the recent singing jobs (newest first): id, status, source and progress.

        Read-only; use it to find a job whose reply was lost — records survive server restarts,
        and a job that was running when the server went down comes back failed with the reason.
        Jobs live 24 hours.
        """
        _registry.prune()
        with _registry.lock:
            jobs = sorted(_registry.jobs.values(), key=lambda job: job.created_at, reverse=True)
        return {
            "jobs": [
                {
                    "job_id": job.job_id,
                    "status": job.status,
                    "source": job.source or None,
                    "voice_id": job.voice_id,
                    "created_at": datetime.fromtimestamp(job.created_at, timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "progress": job.progress,
                    "error": job.error,
                }
                for job in jobs
            ]
        }

    @mcp.tool(title="Mix song with vocals", annotations=_CREATES)
    @offloaded
    def mix_song_with_vocals(
        source: Annotated[str, Field(description="The MIDI file the score came from.")],
        vocal: Annotated[
            str | None,
            Field(description="Vocal filename from a finished job, or the job id. Give this, or vocals."),
        ] = None,
        vocals: Annotated[
            list[VocalSegment] | None,
            Field(
                description="Several vocal pieces to join first, e.g. [{'file': 'verse1.wav', "
                "'start_seconds': 0}, {'file': 'verse2.wav', 'start_seconds': 41.2}] — for takes "
                "or a vocal rendered in pieces. Each piece is placed at its start time and "
                "summed into one vocal track. Give this, or vocal."
            ),
        ] = None,
        guide_track: Annotated[
            str | int | None,
            Field(
                description="Track to leave out of the backing. Default: auto — an exact "
                "'Vocal'/'Voice'/'Melody' name first, else the first melody-like name; pass the "
                "name explicitly when the file has several (e.g. both 'Lead' and 'Vocal')."
            ),
        ] = None,
        vocal_level_db: Annotated[
            float,
            Field(
                ge=-12.0,
                le=18.0,
                description="How loud the vocal sits over the band as heard, in dB, measured "
                "over the sung parts (default 6: clearly on top but not overpowering). With "
                "ducking, this is the level over the dipped band during phrases.",
            ),
        ] = 6.0,
        backing_volume: Annotated[
            float, Field(ge=0.0, le=2.0, description="Backing trim (1.0 = the rendered level).")
        ] = 1.0,
        backing_levels: Annotated[
            dict[str, Annotated[float, Field(ge=-24.0, le=12.0)]] | None,
            Field(
                description="Per-track backing trims in dB, keyed by track name, e.g. "
                "{'Drums': -4, 'Pad': 2}. Each track is rendered separately (per-track "
                "set_track_instrument specs apply) and mixed with its trim; trims here override "
                "a spec's gain_db. Omit to use only the specs (single pass when there are none)."
            ),
        ] = None,
        vocal_reverb: Annotated[
            bool, Field(description="Gentle reverb on the vocal (off by default: clarity first).")
        ] = False,
        vocal_chain: Annotated[
            VocalChain | None,
            Field(
                description="Processing chain for a single vocal: EQ then compressor (de-esser and "
                "saturation are later stages; leave them off). With several vocals, put the chain "
                "on each segment instead. When omitted, a <source>.vocal_chain.json sidecar (if "
                "present) loads automatically."
            ),
        ] = None,
        vocal_sends: Annotated[
            VocalSends | None,
            Field(
                description="Send levels from the single vocal to the shared returns, e.g. "
                "{'reverb_db': -6, 'delay_db': -12}; null = no send. With several vocals, put "
                "sends on each segment instead."
            ),
        ] = None,
        vocal_bus: Annotated[
            VocalBus | None,
            Field(
                description="The summed-voice bus for several voices (duets): one gain, optional "
                "glue compressor, limiter and breathy air layer. Applies after the per-voice chains."
            ),
        ] = None,
        returns: Annotated[
            Returns | None,
            Field(
                description="Shared effect returns, fed by each voice's sends: {'reverb': {'decay_s', "
                "'predelay_ms', 'damping'}, 'delay': {'delay_ms' or 'tempo_sync', 'feedback'}}."
            ),
        ] = None,
        master: Annotated[
            MasterSettings | None,
            Field(
                description="Master stage: a true-peak limiter (sits 0.3 dB under its ceiling and is "
                "verified) and/or loudness_target_lufs, applied as one static gain measured with "
                "EBU R128 (supersedes normalize_peak_db)."
            ),
        ] = None,
        save_chain: Annotated[
            bool,
            Field(
                description="Save the effective chain/bus/returns/master to <source>.vocal_chain.json "
                "(loaded automatically by later mixes of this song)."
            ),
        ] = False,
        reverb: Annotated[
            ReverbSettings | None,
            Field(
                description="FluidSynth reverb for the backing instruments (off when omitted; "
                "the vocal has its own vocal_reverb). Per-track amounts come from each spec's "
                "reverb_send (CC91)."
            ),
        ] = None,
        chorus: Annotated[
            ChorusSettings | None,
            Field(
                description="FluidSynth chorus for the backing instruments (off when omitted). "
                "Per-track amounts come from each spec's chorus_send (CC93)."
            ),
        ] = None,
        normalize_peak_db: Annotated[
            float | None,
            Field(
                ge=-6.0,
                le=0.0,
                description="Normalize the exported mix to this peak level in dBFS "
                "(default -1.0); set null to keep the raw level.",
            ),
        ] = -1.0,
        compress: Annotated[
            bool,
            Field(
                description="Gentle high-pass and compressor on the vocal (off by default): tames "
                "the voice's note-to-note level swings before the balance is measured, but can "
                "cost diction on dense, loud mixes — compare both when words matter most.",
            ),
        ] = False,
        ducking: Annotated[
            bool,
            Field(
                description="Dip the backing under the vocal (off by default) so the band steps "
                "back while the voice sings; the dip is duck_db deep and the vocal is targeted "
                "vocal_level_db over the dipped band (what you hear).",
            ),
        ] = False,
        duck_db: Annotated[
            float,
            Field(
                ge=0.0,
                le=12.0,
                description="How deep the backing dips when ducking is on, in dB (default 4). "
                "Ignored when ducking is off.",
            ),
        ] = 4.0,
        output_filename: Annotated[
            str | None, Field(description="Name for the mix (default: <source>_mix).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing mix.")] = False,
    ) -> results.MixResult:
        """Render the backing without the guide track, mix in the vocal, and check the balance (step 4).

        Creates the backing and mix wavs (guide track left out). Levels are balanced by
        measurement: the vocal is placed ``vocal_level_db`` over the band as heard (over the
        dipped band when ducking is on), measured over the blocks where the voice sings, so
        quiet backings and loud vocals are corrected instead of multiplied blindly. Per-track
        set_track_instrument specs (soundfont, GM program, gain, effect sends) are honored for
        the backing; with ``backing_levels`` each backing track is rendered separately and
        trimmed by its dB value, which overrides a spec's gain (renders run several at a time).
        ``reverb``/``chorus`` switch FluidSynth's engine effects on for the backing (off by
        default, so mixes stay byte-identical until asked; per-track wetness comes from each
        spec's reverb_send/chorus_send); ``vocal_reverb`` is the vocal's own effect. The result
        reports the achieved vocal_to_backing_db (during phrases), vocal_to_original_backing_db,
        a balance_check of 'ok'/'mismatch'/'unavailable' (the measurement also works with the
        vocal reverb on: the reverb rides with the vocal), and the effective backing effects.
        The mix is normalized to normalize_peak_db unless null, and clipped exports are turned
        down (see clipping and gain_correction_db).

        ``vocal_chain``/``vocal_sends`` (single vocal) or the per-segment chain/sends/gain/pan run
        each voice through its own processing before the voices are summed into the bus
        (``vocal_bus``: gain, glue compressor, limiter, air layer); the balance is then measured
        on the processed bus, so ``vocal_level_db`` keeps its meaning. ``returns`` feeds a shared
        convolution reverb and delay from the voices' sends; ``master`` adds a true-peak-safe
        limiter and/or a loudness target (one static gain from an EBU R128 measurement, verified
        after the render). The result's ``processing`` block echoes the effective settings,
        per-stage gain reduction, the measured loudness (integrated, range, true peak,
        short-term max) and the limiter's work; ``warnings`` call out anything suspicious. The
        effective chain can be saved as a ``<source>.vocal_chain.json`` sidecar (save_chain) and
        loads automatically on later mixes when no chain option is passed.
        """
        return run_mix(
            {
                "source": source,
                "vocal": vocal,
                "vocals": vocals,
                "guide_track": guide_track,
                "vocal_level_db": vocal_level_db,
                "backing_volume": backing_volume,
                "backing_levels": backing_levels,
                "vocal_reverb": vocal_reverb,
                "vocal_chain": vocal_chain,
                "vocal_sends": vocal_sends,
                "vocal_bus": vocal_bus,
                "returns": returns,
                "master": master,
                "save_chain": save_chain,
                "reverb": reverb,
                "chorus": chorus,
                "normalize_peak_db": normalize_peak_db,
                "compress": compress,
                "ducking": ducking,
                "duck_db": duck_db,
                "output_filename": output_filename,
                "overwrite": overwrite,
            }
        )

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
        mime_type, so a caller can verify what it received. The MIDI entry is the untouched source;
        call bake_instruments first when a DAW or other player should hear the intended programs
        (a MIDI file cannot carry a SoundFont or samples).
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
