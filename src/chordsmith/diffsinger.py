"""DiffSinger adapter (Sodium Gold slice 2): English lyrics through the Hanami voicebank.

The voicebank is **never bundled or redistributed**: download it yourself, mount it read-only,
and point ``CHORDSMITH_DIFFSINGER_VOICE`` at the directory that contains ``dsconfig.yaml``.

The pipeline follows the OpenUtau DiffSinger conventions: the dsmain linguistic encoder with
word_div/word_dur predicts phoneme durations (dur.onnx), the durations are laid out so every
syllable's vowel starts exactly on its note (consonants take time from the previous segment),
the dspitch models predict the f0 curve, the acoustic model renders a mel spectrogram and the
bundled AI-dolGAN vocoder turns it into 44.1 kHz audio.
"""

from __future__ import annotations

import logging
import os
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import cmudict
    import numpy as np
    import onnxruntime as ort
    import yaml

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised when the extra is not installed
    cmudict = np = ort = yaml = None
    _IMPORT_ERROR = exc

SAMPLE_RATE = 44100
HOP_SIZE = 512
FRAME_MS = 1000.0 * HOP_SIZE / SAMPLE_RATE
HEAD_FRAMES = 8
TAIL_FRAMES = 8
DEFAULT_STEPS = 20
DEFAULT_DEPTH = 0.6
MODES = {"root": "Root", "fragrance": "Fragrance", "nectar": "Nectar"}
CREDIT = "Hoshino Hanami ~AI*dol~ by Lotte V"
LICENCE_SUMMARY = (
    "Voicebank models: Team L<3VE Voicebank License - any creative use including commercial, "
    "credit required. Bundled vocoder (AI*dolGAN): CC BY-NC-SA 4.0 - non-commercial; get "
    "written permission or swap the vocoder before a commercial release. DiffSinger/OpenUtau "
    "code: Apache-2.0/MIT."
)


class DiffSingerError(ValueError):
    """Raised when the DiffSinger voicebank or pipeline cannot serve a request."""


def _require_dependencies() -> None:
    if _IMPORT_ERROR is not None:
        raise DiffSingerError(
            "DiffSinger support needs extra packages: install with "
            f"'pip install chordsmith-mcp[diffsinger]' (missing: {_IMPORT_ERROR})."
        )


def voicebank_path() -> Path | None:
    """Find the voicebank: CHORDSMITH_DIFFSINGER_VOICE, or one level under ./voicebank."""
    candidates: list[Path] = []
    env = os.environ.get("CHORDSMITH_DIFFSINGER_VOICE", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    default_root = Path.cwd() / "voicebank"
    if default_root.is_dir():
        candidates.extend(sorted(p for p in default_root.iterdir() if p.is_dir()))
    for candidate in candidates:
        if (candidate / "dsconfig.yaml").is_file():
            return candidate
    return None


def available() -> bool:
    return voicebank_path() is not None


def describe() -> dict | None:
    """Voicebank metadata for list_singing_voices."""
    path = voicebank_path()
    if path is None:
        return None
    return {
        "engine": "diffsinger",
        "voicebank": path.name,
        "path": str(path),
        "credit": CREDIT,
        "licence": LICENCE_SUMMARY,
        "commercial_status": "voicebank allows commercial use with credit; bundled vocoder is "
        "non-commercial - check before a commercial release",
        "languages": ["en"],
        "modes": list(MODES),
        "sample_rate": SAMPLE_RATE,
    }


@dataclass
class NoteSpec:
    """One note for the renderer: phonemes (onset..vowel..coda), timing and pitch."""

    phonemes: list[str]
    start_seconds: float
    seconds: float
    midi: int
    is_rest: bool = False


@dataclass
class _Voicebank:
    path: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    sessions: dict = field(default_factory=dict)
    phoneme_ids: dict = field(default_factory=dict)
    symbol_types: dict = field(default_factory=dict)
    en_dict: dict = field(default_factory=dict)
    cmu: dict = field(default_factory=dict)
    embeds: dict = field(default_factory=dict)

    def session(self, rel: str):
        with self.lock:
            if rel not in self.sessions:
                self.sessions[rel] = ort.InferenceSession(
                    str(self.path / rel), providers=["CPUExecutionProvider"]
                )
            return self.sessions[rel]

    def embed(self, sub: str, mode: str):
        key = f"{sub}/{mode}"
        with self.lock:
            if key not in self.embeds:
                self.embeds[key] = np.fromfile(self.path / sub / f"{MODES[mode]}.emb", dtype=np.float32)
            return self.embeds[key]

    def is_vowel(self, phoneme: str) -> bool:
        return self.symbol_types.get(phoneme) == "vowel" or phoneme in ("SP", "AP")

    def phonemize(self, word: str) -> list[str]:
        key = word.lower()
        if key in self.en_dict:
            return list(self.en_dict[key])
        if key in self.cmu:
            return [phone.rstrip("012").lower() for phone in self.cmu[key][0]]
        raise DiffSingerError(f"Unknown word for the English dictionary: '{word}'")


_voicebank: _Voicebank | None = None
_voicebank_lock = threading.Lock()


def get_voicebank() -> _Voicebank:
    global _voicebank
    _require_dependencies()
    path = voicebank_path()
    if path is None:
        raise DiffSingerError(
            "No DiffSinger voicebank configured. Download one (see docs/singing.md), mount it and "
            "set CHORDSMITH_DIFFSINGER_VOICE to its directory."
        )
    with _voicebank_lock:
        if _voicebank is None or _voicebank.path != path:
            bank = _Voicebank(path=path)
            bank.phoneme_ids = {
                line: index
                for index, line in enumerate(
                    (path / "dsmain/phonemes.txt").read_text(encoding="utf-8").splitlines()
                )
            }
            dsdict = yaml.safe_load((path / "dsdur/dsdict.yaml").read_text(encoding="utf-8"))
            bank.symbol_types = {s["symbol"]: s["type"] for s in dsdict["symbols"]}
            entries = yaml.safe_load((path / "dsdur/dsdict-en.yaml").read_text(encoding="utf-8"))["entries"]
            bank.en_dict = {e["grapheme"].lower(): e["phonemes"] for e in entries}
            bank.cmu = cmudict.dict()
            _voicebank = bank
    return _voicebank


# ------------------------------------------------------------------ phonemizer


def split_syllables(lyrics: str) -> list[str]:
    """Split lyrics into one token per note: whitespace-separated, hyphens join syllables.

    'so- di- um gold' -> ['so', 'di', 'um', 'gold']; '+' continues the previous note,
    '-' is a pause and 'br' a breath.
    """
    tokens = []
    for raw in lyrics.split():
        lowered = raw.lower()
        if lowered in ("+", "-", "br"):
            tokens.append(lowered)
            continue
        token = raw.strip("-").lower()
        if token:
            tokens.append(token)
    return tokens


def phonemize_tokens(bank: _Voicebank, tokens: list[str]) -> tuple[list[list[str]], list[str]]:
    """Map syllable tokens to phonemes per note; refuses unknown words by name."""
    result: list[list[str]] = []
    unknown: list[str] = []
    previous: list[str] = []
    for token in tokens:
        if token == "+":
            if not previous:
                raise DiffSingerError("'+' needs a preceding note with lyrics.")
            result.append(list(previous))
            continue
        if token == "-":
            result.append(["SP"])
            previous = []
            continue
        if token == "br":
            result.append(["AP"])
            previous = []
            continue
        try:
            phones = bank.phonemize(token)
        except DiffSingerError:
            unknown.append(token)
            continue
        result.append(phones)
        previous = phones
    if unknown:
        raise DiffSingerError(
            "These words are not in the voicebank's English dictionary or CMUdict: "
            + ", ".join(sorted(set(unknown)))
            + ". Use a per-syllable spelling, or '-' for a pause."
        )
    return result, []


# ------------------------------------------------------------------ timeline planner


def frame_at(seconds: float) -> int:
    return round(seconds * 1000.0 / FRAME_MS)


def distribute(total: int, weights: list[float]) -> list[int]:
    """Split integer frames proportional to weights, each part at least one frame."""
    weights_arr = np.asarray(weights, dtype=np.float64)
    if len(weights_arr) == 0:
        return []
    total = max(total, len(weights_arr))
    share = weights_arr / weights_arr.sum() * total
    frames = np.maximum(1, np.floor(share).astype(int))
    while frames.sum() < total:
        frames[int(np.argmax(share - frames))] += 1
    while frames.sum() > total:
        index = int(np.argmax(frames - share))
        if frames[index] > 1:
            frames[index] -= 1
        else:
            break
    return frames.tolist()


def plan_timeline(
    notes: list[NoteSpec],
    predicted: list[list[float]],
    bank: _Voicebank | None = None,
    total_seconds: float | None = None,
) -> list[dict]:
    """Lay phonemes on the note grid: every vowel starts exactly on its note.

    Onsets are sung ahead of the beat by taking time from the previous segment (silence first,
    then the previous coda/vowel, keeping at least one frame); codas stay inside the note.
    Notes are anchored to absolute time, so leading silence and rests keep the vocal aligned
    with the backing; trailing silence (up to ``total_seconds``) is padded too.
    """
    bank = bank or get_voicebank()
    timeline: list[dict] = [{"phoneme": "SP", "note": -1, "kind": "pad", "frames": HEAD_FRAMES}]
    last_end = 0
    for index, note in enumerate(notes):
        note_start = HEAD_FRAMES + frame_at(note.start_seconds)
        note_end = HEAD_FRAMES + frame_at(note.start_seconds + note.seconds)
        note_frames = max(1, note_end - note_start)
        phones = note.phonemes
        vowel_position = next((j for j, p in enumerate(phones) if bank.is_vowel(p)), len(phones) - 1)
        onset = phones[:vowel_position]
        vowel = phones[vowel_position]
        coda = phones[vowel_position + 1 :]
        weights = {j: float(predicted[index][j]) for j in range(len(phones))}

        coda_total = 0
        if coda:
            wanted_coda = int(round(sum(weights[vowel_position + 1 + k] for k in range(len(coda)))))
            coda_total = min(max(len(coda), wanted_coda), max(len(coda), int(note_frames * 0.4)))
        vowel_frames = max(1, note_frames - coda_total)

        cursor = sum(entry["frames"] for entry in timeline)
        gap = note_start - cursor
        if onset:
            wanted = max(len(onset), int(round(sum(weights[k] for k in range(len(onset))))))
            if wanted > gap:
                deficit = wanted - gap
                for entry in reversed(timeline):
                    if deficit <= 0:
                        break
                    if entry["kind"] in ("vowel", "coda", "gap", "pad"):
                        take = min(deficit, entry["frames"] - 1)
                        entry["frames"] -= take
                        deficit -= take
                wanted -= deficit
            if wanted >= len(onset):
                if gap > wanted:
                    timeline.append({"phoneme": "SP", "note": -1, "kind": "gap", "frames": gap - wanted})
                for offset, frames in enumerate(distribute(wanted, [weights[k] for k in range(len(onset))])):
                    timeline.append(
                        {"phoneme": onset[offset], "note": index, "kind": "onset", "frames": frames}
                    )
            elif gap > 0:
                timeline.append({"phoneme": "SP", "note": -1, "kind": "gap", "frames": gap})
        elif gap > 0:
            timeline.append({"phoneme": "SP", "note": -1, "kind": "gap", "frames": gap})
        timeline.append({"phoneme": vowel, "note": index, "kind": "vowel", "frames": vowel_frames})
        if coda:
            weights_coda = [weights[vowel_position + 1 + k] for k in range(len(coda))]
            for offset, frames in enumerate(distribute(coda_total, weights_coda)):
                timeline.append({"phoneme": coda[offset], "note": index, "kind": "coda", "frames": frames})
        last_end = note_end
    if total_seconds is not None:
        trailing = HEAD_FRAMES + frame_at(total_seconds) - last_end
        if trailing > 0:
            timeline.append({"phoneme": "SP", "note": -1, "kind": "gap", "frames": trailing})
    timeline.append({"phoneme": "SP", "note": -1, "kind": "pad", "frames": TAIL_FRAMES})
    return timeline


# ------------------------------------------------------------------ renderer


def _note_partition(timeline: list[dict], notes: list[NoteSpec]) -> tuple[list[int], list[float], list[bool]]:
    """Note-space partition for the pitch model, derived from the timeline itself."""
    runs: list[dict] = []
    for entry in timeline:
        key = "rest" if entry["kind"] in ("pad", "gap") else f"note{entry['note']}"
        if runs and runs[-1]["key"] == key:
            runs[-1]["frames"] += entry["frames"]
        else:
            runs.append({"key": key, "frames": entry["frames"], "note": entry["note"]})
    durations, midis, rests = [], [], []
    for run in runs:
        if run["key"] == "rest":
            midis.append(float(notes[run["note"]].midi) if run["note"] >= 0 else 0.0)
            rests.append(True)
        else:
            midis.append(float(notes[int(run["key"][4:])].midi))
            rests.append(False)
        durations.append(run["frames"])
    for index, rest in enumerate(rests):
        if not rest:
            break
        midis[index] = midis[index] or float(notes[0].midi)
    return durations, midis, rests


def render(
    notes: list[NoteSpec],
    output: Path,
    *,
    mode: str = "nectar",
    steps: int = DEFAULT_STEPS,
    depth: float = DEFAULT_DEPTH,
    velocity: float = 1.0,
    gender: float = 0.0,
    expr: float = 1.0,
    energy: float = 1.0,
    total_seconds: float | None = None,
) -> dict:
    """Render the notes to a 44.1 kHz mono wav with the voicebank."""
    if mode not in MODES:
        raise DiffSingerError(f"Unknown mode '{mode}'; use one of {', '.join(MODES)}.")
    if not notes:
        raise DiffSingerError("No notes to render.")
    if any(note.is_rest for note in notes):
        raise DiffSingerError("Rest notes are not supported yet; drop silent notes instead.")
    bank = get_voicebank()

    # --- duration prediction
    segments: list[dict] = [{"phoneme": "SP", "note": -1, "kind": "pad"}]
    for index, note in enumerate(notes):
        vowel_position = next(
            (j for j, p in enumerate(note.phonemes) if bank.is_vowel(p)), len(note.phonemes) - 1
        )
        for j, phone in enumerate(note.phonemes):
            kind = "vowel" if j == vowel_position else ("onset" if j < vowel_position else "coda")
            segments.append({"phoneme": phone, "note": index, "kind": kind})
    segments.append({"phoneme": "SP", "note": -1, "kind": "pad"})

    tokens = np.array([[bank.phoneme_ids[s["phoneme"]] for s in segments]], dtype=np.int64)
    vowel_ids = [i for i, s in enumerate(segments) if bank.is_vowel(s["phoneme"])]
    word_div = [vowel_ids[0]] + [vowel_ids[i + 1] - vowel_ids[i] for i in range(len(vowel_ids) - 1)]
    word_div.append(tokens.shape[1] - vowel_ids[-1])
    encoder_out, x_masks = bank.session("dsmain/linguistic.onnx").run(
        None,
        {
            "tokens": tokens,
            "word_div": np.array([word_div], dtype=np.int64),
            "word_dur": np.ones((1, len(word_div)), dtype=np.int64),
        },
    )[:2]
    note_pitch = np.array(
        [[notes[s["note"]].midi if s["note"] >= 0 else 0 for s in segments]], dtype=np.int64
    )
    variance_embed = bank.embed("dsmain/embeds/variance", mode)
    predicted_flat = bank.session("dsdur/dur.onnx").run(
        None,
        {
            "encoder_out": encoder_out,
            "x_masks": x_masks,
            "ph_midi": note_pitch,
            "spk_embed": np.tile(variance_embed, (1, tokens.shape[1], 1)),
        },
    )[0][0]
    predicted_by_note: list[list[float]] = [[] for _ in notes]
    for position, segment in enumerate(segments):
        if segment["note"] >= 0:
            predicted_by_note[segment["note"]].append(float(predicted_flat[position]))
    predicted = predicted_by_note

    # --- timeline + acoustic tokens
    timeline = plan_timeline(notes, predicted, bank=bank, total_seconds=total_seconds)
    ph_dur = np.array([[entry["frames"] for entry in timeline]], dtype=np.int64)
    total_frames = int(ph_dur.sum())
    if total_frames < HEAD_FRAMES + TAIL_FRAMES + len(notes):
        raise DiffSingerError("Notes are too short for the model.")
    tokens = np.array([[bank.phoneme_ids[entry["phoneme"]] for entry in timeline]], dtype=np.int64)

    # --- pitch
    note_dur, note_midis, note_rests = _note_partition(timeline, notes)
    pitch_enc = bank.session("dspitch/linguistic.onnx").run(None, {"tokens": tokens, "ph_dur": ph_dur})[0]
    pitch_base = np.zeros(total_frames, dtype=np.float32)
    frame = 0
    for frames, midi in zip(note_dur, note_midis, strict=True):
        pitch_base[frame : frame + frames] = midi
        frame += frames
    pitch_out = bank.session("dspitch/pitch.onnx").run(
        None,
        {
            "encoder_out": pitch_enc,
            "ph_dur": ph_dur,
            "note_midi": np.array([note_midis], dtype=np.float32),
            "note_rest": np.array([note_rests], dtype=bool),
            "note_dur": np.array([note_dur], dtype=np.int64),
            "pitch": pitch_base.reshape(1, -1),
            "expr": np.full((1, total_frames), expr, dtype=np.float32),
            "retake": np.ones((1, total_frames), dtype=bool),
            "spk_embed": np.tile(bank.embed("dspitch/embeds", mode), (1, total_frames, 1)),
            "steps": np.array(steps, dtype=np.int64),
        },
    )[0][0]
    voiced = np.concatenate(
        [np.full(entry["frames"], entry["kind"] not in ("pad", "gap"), dtype=bool) for entry in timeline]
    )
    voiced_index = np.flatnonzero(voiced)
    for index in np.flatnonzero(~voiced):
        pitch_out[index] = pitch_out[voiced_index[np.argmin(np.abs(voiced_index - index))]]
    f0 = (440.0 * np.power(2.0, (pitch_out - 69.0) / 12.0)).astype(np.float32).reshape(1, -1)

    # --- acoustic + vocoder
    mel = bank.session("dsmain/acoustic.onnx").run(
        None,
        {
            "tokens": tokens,
            "durations": ph_dur,
            "f0": f0,
            "gender": np.full((1, total_frames), gender, dtype=np.float32),
            "velocity": np.full((1, total_frames), velocity, dtype=np.float32),
            "spk_embed": np.tile(bank.embed("dsmain/embeds/acoustic", mode), (1, total_frames, 1)),
            "depth": np.array(depth, dtype=np.float32),
            "steps": np.array(steps, dtype=np.int64),
        },
    )[0]
    samples = bank.session("dsvocoder/aidolgan.onnx").run(None, {"mel": mel, "f0": f0})[0][0]
    if not np.isfinite(samples).all():
        raise DiffSingerError("The vocoder produced invalid samples.")
    samples = samples.astype(np.float32) * float(energy)
    peak = float(np.abs(samples).max())
    if peak > 0.891:  # keep exports below -1 dBFS
        samples = samples * (0.891 / peak)
        peak = float(np.abs(samples).max())
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    duration = len(samples) / SAMPLE_RATE
    return {
        "duration_seconds": round(duration, 3),
        "sample_rate": SAMPLE_RATE,
        "frames": total_frames,
        "peak_db": round(20.0 * float(np.log10(max(peak, 1e-6))), 2),
        "mode": mode,
    }
