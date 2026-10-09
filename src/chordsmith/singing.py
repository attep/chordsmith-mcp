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
import os
import re
import subprocess
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
from pydantic import BaseModel, Field

from chordsmith import audio, delivery
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
    frames: int
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


@dataclass
class MappingNote:
    note_id: int
    key: int | None
    frames: int
    lyric: str
    is_rest: bool
    start_frame: int


@dataclass
class VocalMapping:
    mapping_id: str
    score_id: str
    version: int
    voice_hint: str
    notes: list[MappingNote]
    total_frames: int
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


def build_score(path: Path, track: str | int | None, transpose: int) -> VocalScore:
    midi = mido.MidiFile(path)
    track_index = find_track(midi, track)
    track_name = _track_names(midi)[track_index]
    tempo = 120.0
    for msg in midi.tracks[0]:
        if msg.type == "set_tempo":
            tempo = mido.tempo2bpm(msg.tempo)
            break
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

    frames_per_beat = FRAME_RATE * 60.0 / tempo
    warnings: list[str] = []

    def to_frame(tick: int) -> int:
        return round(tick / midi.ticks_per_beat * frames_per_beat)

    notes: list[ScoreNote] = []
    note_id = 0
    cursor_tick = 0
    for start, end, pitch in raw:
        if start > cursor_tick:
            frames = to_frame(start) - to_frame(cursor_tick)
            if frames > 0:
                notes.append(
                    ScoreNote(
                        note_id,
                        None,
                        cursor_tick / midi.ticks_per_beat,
                        (start - cursor_tick) / midi.ticks_per_beat,
                        frames,
                        True,
                    )
                )
                note_id += 1
        key = pitch + transpose
        if not 0 <= key <= 127:
            raise SingingError(
                f"Transposing {midi_note_name(pitch)} by {transpose:+d} leaves the MIDI range 0-127."
            )
        frames = to_frame(end) - to_frame(start)
        if frames < MIN_FRAME_LENGTH:
            warnings.append(
                f"Note at tick {start} was shorter than {MIN_FRAME_LENGTH} frames and was lengthened."
            )
            frames = MIN_FRAME_LENGTH
        notes.append(
            ScoreNote(
                note_id, key, start / midi.ticks_per_beat, (end - start) / midi.ticks_per_beat, frames, False
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
    )


def _score_payload(score: VocalScore) -> dict:
    return {
        "score_id": score.score_id,
        "source": score.source,
        "track": {"index": score.track_index + 1, "name": score.track_name},
        "tempo_bpm": score.tempo_bpm,
        "transpose": score.transpose,
        "warnings": score.warnings,
        "notes": [
            {
                "note_id": note.note_id,
                "kind": "rest" if note.is_rest else "note",
                "pitch": note.key,
                "name": midi_note_name(note.key) if note.key is not None else None,
                "start_beat": round(note.start_beat, 4),
                "beats": round(note.beats, 4),
                "frames": note.frames,
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


def build_mapping(
    score: VocalScore, lyrics: str | None, language: str, holds: dict[int, int] | None
) -> VocalMapping:
    wordless = lyrics is None or lyrics.strip() == ""
    syllables = [] if wordless else _parse_syllables(lyrics, language)
    warnings: list[str] = []
    sung = [note for note in score.notes if not note.is_rest]
    if not wordless:
        if len(syllables) > len(sung):
            warnings.append(
                f"{len(syllables) - len(sung)} extra syllables were not used "
                f"(the melody has {len(sung)} notes)."
            )
        if len(syllables) < len(sung):
            warnings.append(
                f"{len(sung) - len(syllables)} notes had no syllable and were turned into rests "
                "(no words were invented)."
            )

    holds = holds or {}
    held_frames: dict[int, int] = {}
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
                    held_frames[key] = held_frames.get(key, 0) + next(
                        note.frames for note in sung if note.note_id == target
                    )
                    skipped.add(target)

    mapping_notes: list[MappingNote] = []
    cursor = 0
    syllable_iter = iter(syllables)
    for note in score.notes:
        if note.is_rest:
            mapping_notes.append(MappingNote(note.note_id, None, note.frames, "", True, cursor))
            cursor += note.frames
            continue
        if note.note_id in skipped:
            continue
        if wordless:
            lyric = WORDLESS_LYRIC
        else:
            lyric = next(syllable_iter, "")
        frames = note.frames + held_frames.get(note.note_id, 0)
        mapping_notes.append(MappingNote(note.note_id, note.key, frames, lyric, False, cursor))
        cursor += frames
    if not wordless and not syllables:
        warnings.append("No syllables were mapped; every note is a rest.")
    return VocalMapping(
        mapping_id=f"map_{uuid.uuid4().hex[:10]}",
        score_id=score.score_id,
        version=1,
        voice_hint="voicevox",
        notes=mapping_notes,
        total_frames=cursor,
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
        "total_frames": mapping.total_frames,
        "duration_seconds": round(mapping.total_frames / FRAME_RATE, 3),
        "warnings": mapping.warnings,
        "notes": [
            {
                "note_id": note.note_id,
                "kind": "rest" if note.is_rest else "note",
                "pitch": note.key,
                "lyric": note.lyric,
                "frames": note.frames,
                "start_frame": note.start_frame,
            }
            for note in mapping.notes
        ],
    }


# ------------------------------------------------------------------ render jobs


class SoftSettings(BaseModel):
    """Soft-voice controls.

    VOICEVOX supports energy and volume_cap; breathiness/vibrato are DiffSinger-only.
    """

    energy: float = Field(
        1.0, ge=0.1, le=2.0, description="Overall vocal level (scales the engine's output)."
    )
    volume_cap: float | None = Field(
        None,
        ge=0.05,
        le=1.0,
        description="Clamp every frame's volume to this value, so loud notes stay soft (e.g. 0.6).",
    )
    breathiness: float | None = Field(
        None, ge=0.0, le=1.0, description="DiffSinger-only; VOICEVOX fails if this is set."
    )
    vibrato: float | None = Field(
        None, ge=0.0, le=1.0, description="DiffSinger-only; VOICEVOX fails if this is set."
    )


def parse_voice_id(voice_id: str, singers: list[dict] | None = None) -> int:
    engine, _, style = voice_id.partition(":")
    if engine != "voicevox" or not style.isdigit():
        raise SingingError(f"Unknown voice '{voice_id}'. Use list_singing_voices to pick one.")
    speaker = int(style)
    if singers is not None:
        known = {style_info["id"] for singer in singers for style_info in singer["styles"]}
        if speaker not in known:
            raise SingingError(
                f"Voice '{voice_id}' is not offered by the engine (no swapping in another voice)."
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


def _settings_dict(settings: SoftSettings | None) -> dict:
    data = (settings or SoftSettings()).model_dump(exclude_none=True)
    for unsupported in ("breathiness", "vibrato"):
        if unsupported in data:
            raise SingingError(
                f"VOICEVOX does not support '{unsupported}' (that control is DiffSinger-only). "
                "Use energy and volume_cap for a soft VOICEVOX voice."
            )
    return data


def _run_job(
    job: SingingJob, mapping: VocalMapping, score: VocalScore, query_speaker: int, synth_speaker: int
) -> None:
    job.status = "running"
    try:
        client = get_client()
        payload = {
            "notes": [
                {"key": note.key, "frame_length": note.frames, "lyric": note.lyric} for note in mapping.notes
            ]
        }
        query = client.sing_frame_audio_query(payload, query_speaker)
        energy = float(job.settings.get("energy", 1.0))
        if energy != 1.0:
            query["volumeScale"] = float(query.get("volumeScale", 1.0)) * energy
        cap = job.settings.get("volume_cap")
        if cap is not None:
            query["volume"] = [min(float(v), float(cap)) for v in query.get("volume", [])]
        data = client.frame_synthesis(query, synth_speaker)
        store = _get_store()
        assert store is not None
        path = store.new_path(None, f"{Path(score.source).stem}_vocal", extension=".wav")
        path.write_bytes(data)
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
            "query_voice_id": f"voicevox:{query_speaker}",
        }
        job.status = "done"
    except Exception as exc:  # the job must fail loudly, never render twice
        logger.warning("Singing job %s failed: %s", job.job_id, exc)
        job.status = "failed"
        job.error = str(exc)


def start_job(
    mapping: VocalMapping, score: VocalScore, voice_id: str, settings: dict
) -> tuple[SingingJob, bool]:
    cache_key = json.dumps(
        {"mapping": mapping.mapping_id, "voice": voice_id, "settings": settings}, sort_keys=True
    )
    with _registry.lock:
        for job in _registry.jobs.values():
            if job.cache_key == cache_key and job.status in ("queued", "running", "done"):
                return job, True
    query_speaker, synth_speaker = resolve_speakers(voice_id, get_client().singers())
    job = SingingJob(
        job_id=f"job_{uuid.uuid4().hex[:10]}",
        status="queued",
        mapping_id=mapping.mapping_id,
        voice_id=voice_id,
        settings=settings,
        cache_key=cache_key,
        created_at=time.time(),
    )
    with _registry.lock:
        _registry.jobs[job.job_id] = job
    thread = threading.Thread(
        target=_run_job, args=(job, mapping, score, query_speaker, synth_speaker), daemon=True
    )
    thread.start()
    return job, False


def _job_payload(job: SingingJob) -> dict:
    payload: dict[str, Any] = {"job_id": job.job_id, "status": job.status, "voice_id": job.voice_id}
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


def _mix_filters(vocal_volume: float, backing_volume: float, reverb: bool, output_gain_db: float) -> str:
    vocal = f"[1:a]volume={vocal_volume:.3f}"
    if reverb:
        vocal += ",aecho=0.8:0.9:60:0.25"
    filters = (
        f"[0:a]volume={backing_volume:.3f}[b];{vocal}[v];[b][v]amix=inputs=2:duration=longest:normalize=0"
    )
    if output_gain_db:
        filters += f",volume={output_gain_db:.2f}dB"
    return filters + "[m]"


def mix_tracks(
    backing: Path, vocal: Path, target: Path, *, vocal_volume: float, backing_volume: float, reverb: bool
) -> dict:
    _ffmpeg(
        [
            "-y",
            "-i",
            str(backing),
            "-i",
            str(vocal),
            "-filter_complex",
            _mix_filters(vocal_volume, backing_volume, reverb, 0.0),
            "-map",
            "[m]",
            "-ar",
            "44100",
            str(target),
        ]
    )
    peak = _peak_db(target)
    correction = 0.0
    if peak > -0.5:
        correction = round(-0.5 - peak, 2)
        _ffmpeg(
            [
                "-y",
                "-i",
                str(backing),
                "-i",
                str(vocal),
                "-filter_complex",
                _mix_filters(vocal_volume, backing_volume, reverb, correction),
                "-map",
                "[m]",
                "-ar",
                "44100",
                str(target),
            ]
        )
        peak = _peak_db(target)
    with wave.open(str(target), "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
    return {
        "peak_db": peak,
        "clipping": peak > -0.1,
        "gain_correction_db": correction,
        "duration_seconds": round(duration, 3),
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

    @mcp.tool()
    def list_singing_voices(
        engine: Annotated[Literal["voicevox", "all"], Field(description="Which engine to query.")] = "all",
    ) -> dict[str, Any]:
        """List the singing voices an engine offers, with languages, licence notes and soft controls.

        The VOICEVOX adapter sings Japanese kana (or a wordless hum) and supports the 'energy' and
        'volume_cap' soft controls. Decode-only voices (including the whisper styles) are prepared
        by the engine's teacher style and sung in the chosen voice's timbre.
        """
        _registry.prune()
        if engine not in ("voicevox", "all"):
            raise SingingError(f"Unknown engine '{engine}'.")
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
        return {"engine": "voicevox", "engine_url": client.base_url, "voices": voices}

    @mcp.tool()
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
    ) -> dict[str, Any]:
        """Prepare a monophonic vocal score from a MIDI melody track, with rests and frame timings.

        The source file is never changed. Fails if the track has overlapping notes.
        """
        store = _get_store()
        assert store is not None
        score = build_score(store.existing_path(filename), track, transpose)
        with _registry.lock:
            _registry.scores[score.score_id] = score
        return _score_payload(score)

    @mcp.tool()
    def map_vocal_lyrics(
        score_id: Annotated[str, Field(description="Score id from prepare_vocal_score.")],
        lyrics: Annotated[
            str | None,
            Field(description="Kana syllables, e.g. 'う う う' (one per note). Omit for a wordless hum."),
        ] = None,
        language: Annotated[str, Field(description="'ja' for kana; omit lyrics for wordless mode.")] = "ja",
        holds: Annotated[
            dict[int, int] | None,
            Field(description='Optional holds: {"<note_id>": how many following notes it is held over.'),
        ] = None,
    ) -> dict[str, Any]:
        """Attach one syllable per note (or 'う' on every note in wordless mode).

        Mismatches produce warnings; words are never dropped or invented.
        """
        with _registry.lock:
            score = _registry.scores.get(score_id)
        if score is None:
            raise SingingError(f"Score '{score_id}' not found (scores expire after 24 hours).")
        mapping = build_mapping(score, lyrics, language, holds)
        with _registry.lock:
            _registry.versions[score_id] = _registry.versions.get(score_id, 0) + 1
            mapping.version = _registry.versions[score_id]
            _registry.mappings[mapping.mapping_id] = mapping
        return _mapping_payload(mapping)

    @mcp.tool()
    def render_singing(
        mapping_id: Annotated[str, Field(description="Mapping id from map_vocal_lyrics.")],
        voice_id: Annotated[
            str, Field(description="Voice id from list_singing_voices, e.g. 'voicevox:6000'.")
        ],
        settings: SoftSettings | None = None,
    ) -> dict[str, Any]:
        """Start rendering the vocal in the background and return a job id right away.

        Poll with get_singing_job. Repeating the same request reuses the finished job instead of
        rendering (and charging) twice.
        """
        with _registry.lock:
            mapping = _registry.mappings.get(mapping_id)
            score = _registry.scores.get(mapping.score_id) if mapping else None
        if mapping is None or score is None:
            raise SingingError(f"Mapping '{mapping_id}' not found (mappings expire after 24 hours).")
        settings_dict = _settings_dict(settings)
        job, reused = start_job(mapping, score, voice_id, settings_dict)
        return {"job_id": job.job_id, "status": job.status, "reused": reused}

    @mcp.tool()
    def get_singing_job(
        job_id: Annotated[str, Field(description="Job id from render_singing.")],
    ) -> dict[str, Any]:
        """Report a render job: queued, running, done (with the vocal file) or failed (with the error)."""
        with _registry.lock:
            job = _registry.jobs.get(job_id)
        if job is None:
            raise SingingError(f"Job '{job_id}' not found.")
        return _job_payload(job)

    @mcp.tool()
    def mix_song_with_vocals(
        source: Annotated[str, Field(description="The MIDI file the score came from.")],
        vocal: Annotated[str, Field(description="Vocal filename from a finished job, or the job id.")],
        guide_track: Annotated[
            str | int | None,
            Field(description="Track to leave out of the backing (default: the same melody track)."),
        ] = None,
        vocal_volume: Annotated[float, Field(ge=0.0, le=2.0, description="Vocal level.")] = 0.9,
        backing_volume: Annotated[float, Field(ge=0.0, le=2.0, description="Backing level.")] = 0.55,
        reverb: Annotated[bool, Field(description="Gentle reverb on the vocal.")] = True,
        output_filename: Annotated[
            str | None, Field(description="Name for the mix (default: <source>_mix).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing mix.")] = False,
    ) -> dict[str, Any]:
        """Render the backing without the guide track, mix in the vocal, and check for clipping.

        The mix is gently corrected down if it would clip, so exports stay clean.
        """
        store = _get_store()
        assert store is not None
        source_path = store.existing_path(source)
        with _registry.lock:
            job = _registry.jobs.get(vocal)
        if job is not None:
            if job.status != "done" or job.result is None:
                raise SingingError(f"Job '{vocal}' is {job.status}; wait for it to finish first.")
            vocal_path = store.existing_path(job.result["filename"])
        else:
            vocal_path = store.existing_media_path(vocal)
        midi = mido.MidiFile(source_path)
        track_index = find_track(midi, guide_track)
        backing_midi = store.new_path(None, f"{source_path.stem}_backing", overwrite, ".mid")
        remove_track(source_path, backing_midi, track_index)
        backing_wav = store.new_path(None, f"{source_path.stem}_backing", overwrite, ".wav")
        audio.render(backing_midi, backing_wav, "wav")
        target = store.new_path(output_filename, f"{source_path.stem}_mix", overwrite, ".wav")
        info = mix_tracks(
            backing_wav,
            vocal_path,
            target,
            vocal_volume=vocal_volume,
            backing_volume=backing_volume,
            reverb=reverb,
        )
        return {
            "filename": target.name,
            "path": str(target),
            "uri": f"midi://{target.name}",
            "mime_type": "audio/wav",
            "source": source_path.name,
            "vocal": vocal_path.name,
            "backing": backing_wav.name,
            "levels": {"vocal": vocal_volume, "backing": backing_volume},
            "guide_removed": _track_names(midi)[track_index],
            **info,
        }

    @mcp.tool()
    def export_vocal_song(
        mix: Annotated[str, Field(description="Mix wav from mix_song_with_vocals.")],
        vocal: Annotated[str, Field(description="Vocal wav (from a job) to include separately.")],
        source: Annotated[str, Field(description="The untouched source MIDI to include.")],
        return_as: delivery.ReturnAs = "base64",
        expires_in: Annotated[
            int, Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds.")
        ] = delivery.DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Return the full mix (WAV and MP3), the vocal alone and the untouched MIDI.

        Files come back the same way as get_midi_file: base64 bytes or signed, expiring links.
        """
        store = _get_store()
        assert store is not None
        mix_path = store.existing_media_path(mix)
        vocal_path = store.existing_media_path(vocal)
        source_path = store.existing_path(source)
        mp3_path = store.new_path(None, mix_path.stem, extension=".mp3")
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
