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
    import onnx
    import onnxruntime as ort
    import yaml

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised when the extra is not installed
    cmudict = np = onnx = ort = yaml = None
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


_RANDOM_OPS = ("RandomNormal", "RandomNormalLike", "RandomUniform", "RandomUniformLike")


def _patch_graph(graph, seed: int, counter: list[int]) -> None:
    """Give every random node a deterministic seed attribute (ONNX's standard 'seed')."""
    for node in graph.node:
        if node.op_type in _RANDOM_OPS:
            counter[0] += 1
            value = float((seed * 1000003 + counter[0] * 7919) % (2**31 - 1) + 1)
            for attribute in node.attribute:
                if attribute.name == "seed":
                    attribute.f = value
                    break
            else:
                node.attribute.append(onnx.helper.make_attribute("seed", value))
        for attribute in node.attribute:
            if attribute.type == onnx.AttributeProto.GRAPH:
                _patch_graph(attribute.g, seed, counter)
            elif attribute.type == onnx.AttributeProto.GRAPHS:
                for subgraph in attribute.graphs:
                    _patch_graph(subgraph, seed, counter)


def seeded_model_bytes(path: Path, seed: int) -> bytes:
    """Load a model and fix the seed of its random sampling ops, in memory only."""
    model = onnx.load_model(str(path))
    _patch_graph(model.graph, seed, [0])
    return model.SerializeToString()


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


def split_syllables(lyrics: str) -> list[list[str]]:
    """Group lyrics into words: whitespace-separated, hyphens join syllables of one word.

    'so- di- um gold' -> [['so', 'di', 'um'], ['gold']]; '+' continues the previous note,
    '-' is a pause and 'br' a breath (each their own group).
    """
    groups: list[list[str]] = []
    current: list[str] = []
    for raw in lyrics.split():
        lowered = raw.lower()
        if lowered in ("+", "-", "br"):
            if current:
                groups.append(current)
                current = []
            groups.append([lowered])
            continue
        continues = raw.endswith("-")
        token = raw.strip("-").lower()
        if not token:
            continue
        current.append(token)
        if not continues:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


# English syllable onsets (ARPAbet), longest first within each length; used to split a word's
# sounds between its syllables ("s-t-r" can start a syllable, "l-t" cannot).
_VALID_ONSETS = frozenset(
    {
        "b",
        "ch",
        "d",
        "dh",
        "f",
        "g",
        "hh",
        "jh",
        "k",
        "l",
        "m",
        "n",
        "p",
        "r",
        "s",
        "sh",
        "t",
        "th",
        "v",
        "w",
        "y",
        "z",
        "zh",
        "bl",
        "br",
        "dr",
        "dw",
        "fl",
        "fr",
        "gl",
        "gr",
        "gw",
        "kl",
        "kr",
        "kw",
        "pl",
        "pr",
        "shl",
        "shr",
        "sk",
        "sl",
        "sm",
        "sn",
        "sp",
        "spl",
        "spr",
        "st",
        "str",
        "sw",
        "tr",
        "thr",
        "tw",
    }
)


def _split_word_into_syllables(
    phones: list[str], stress: list[float | None], bank: _Voicebank, count: int
) -> list[tuple[list[str], list[float | None]]] | None:
    """Split a word's phonemes (and their stress values) into `count` syllables, one vowel each.

    Uses the maximal onset principle: consonants between two vowels go to the next syllable as
    far as English allows ("s-t-r" can start a syllable, "l-t" cannot). Returns None when the
    vowel count does not match the number of pieces.
    """
    vowel_positions = [
        index for index, phone in enumerate(phones) if bank.is_vowel(phone) and phone not in ("SP", "AP")
    ]
    if len(vowel_positions) != count:
        return None
    ranges: list[tuple[int, int]] = []
    start = 0
    for position_index, vowel_position in enumerate(vowel_positions):
        if position_index + 1 < len(vowel_positions):
            next_vowel = vowel_positions[position_index + 1]
            cluster = phones[vowel_position + 1 : next_vowel]
            take = 0
            for size in range(min(len(cluster), 3), 0, -1):
                if "".join(cluster[-size:]) in _VALID_ONSETS:
                    take = size
                    break
            ranges.append((start, vowel_position + 1 + (len(cluster) - take)))
            start = next_vowel - take
        else:
            ranges.append((start, len(phones)))
    return [(phones[first:last], stress[first:last]) for first, last in ranges]


def _word_phones(bank: _Voicebank, word: str) -> tuple[list[str], list[float | None]] | None:
    """Whole-word lookup: voicebank dictionary first, then CMUdict (stress digits kept)."""
    key = word.lower()
    if key in bank.en_dict:
        return list(bank.en_dict[key]), [None] * len(bank.en_dict[key])
    if key in bank.cmu:
        entry = bank.cmu[key][0]
        phones = [phone.rstrip("012").lower() for phone in entry]
        stress: list[float | None] = [float(phone[-1]) if phone[-1].isdigit() else None for phone in entry]
        return phones, stress
    return None


def _syllable_stress(phones: list[str], stress: list[float | None], bank: _Voicebank) -> float | None:
    for phone, value in zip(phones, stress, strict=False):
        if bank.is_vowel(phone) and phone not in ("SP", "AP"):
            return value
    return None


def phonemize_groups(
    bank: _Voicebank, groups: list[list[str]]
) -> tuple[list[list[str]], list[str], list[float | None]]:
    """Phones per flat token, warnings, and the stress level of each token's vowel.

    Syllables of one word are looked up as the whole word and its sounds are split between the
    word's notes (one vowel per syllable); when that is impossible, the pieces are looked up
    individually and a warning names the word. A '+' carries the previous vowel onto the next
    note and moves the closing consonants to the last note of the slur ("still +" sings
    s-t-ih then ih-l, not s-t-ih-l then ih).
    """
    flat_tokens = [token for group in groups for token in group]
    phones_per_token: list[list[str] | None] = [None] * len(flat_tokens)
    stress_per_token: list[float | None] = [None] * len(flat_tokens)
    warnings: list[str] = []
    unknown: list[str] = []
    index = 0
    for group in groups:
        if len(group) == 1 and group[0] in ("+", "-", "br"):
            control = group[0]
            phones_per_token[index] = {"+": None, "-": ["SP"], "br": ["AP"]}[control]
            index += 1
            continue
        word = "".join(group)
        whole = _word_phones(bank, word)
        pieces: list[list[str]] = []
        stresses: list[float | None] = []
        if whole is not None:
            split = _split_word_into_syllables(whole[0], whole[1], bank, len(group))
            if split is not None:
                pieces = [piece for piece, _ in split]
                stresses = [_syllable_stress(piece, piece_stress, bank) for piece, piece_stress in split]
            else:
                vowels = sum(1 for phone in whole[0] if bank.is_vowel(phone) and phone not in ("SP", "AP"))
                warnings.append(
                    f"'{word}' has {vowels} vowels but {len(group)} notes; falling back to per-piece lookup."
                )
        if not pieces:
            for token in group:
                single = _word_phones(bank, token)
                if single is None:
                    unknown.append(token)
                    continue
                pieces.append(single[0])
                stresses.append(_syllable_stress(single[0], single[1], bank))
        for offset in range(len(group)):
            if offset < len(pieces):
                phones_per_token[index + offset] = pieces[offset]
                stress_per_token[index + offset] = stresses[offset]
        index += len(group)
    if unknown:
        raise DiffSingerError(
            "These words are not in the voicebank's English dictionary or CMUdict: "
            + ", ".join(sorted(set(unknown)))
            + ". Use a per-syllable spelling, or '-' for a pause."
        )

    # slur pass: '+' carries the vowel; the closing consonants move to the last note of the run
    for position, token in enumerate(flat_tokens):
        if token != "+":
            continue
        if position > 0 and flat_tokens[position - 1] == "+":
            continue  # only the first '+' of a run does the work; the rest just carry the vowel
        base_index = position - 1
        while base_index >= 0 and flat_tokens[base_index] == "+":
            base_index -= 1
        if base_index < 0 or phones_per_token[base_index] is None:
            raise DiffSingerError("'+' needs a preceding syllable with a vowel.")
        base_phones = phones_per_token[base_index]
        vowel_positions = [
            i for i, phone in enumerate(base_phones) if bank.is_vowel(phone) and phone not in ("SP", "AP")
        ]
        if not vowel_positions:
            raise DiffSingerError("'+' needs a preceding syllable with a vowel.")
        vowel_position = vowel_positions[-1]
        vowel = base_phones[vowel_position]
        coda = base_phones[vowel_position + 1 :]
        phones_per_token[base_index] = base_phones[: vowel_position + 1]  # keep onset + vowel only
        # find the last '+' of this run
        run_end = position
        while run_end + 1 < len(flat_tokens) and flat_tokens[run_end + 1] == "+":
            run_end += 1
        for offset in range(position, run_end + 1):
            phones_per_token[offset] = [vowel]
        phones_per_token[run_end] = [vowel] + coda
    result = [phones if phones is not None else ["SP"] for phones in phones_per_token]
    return result, warnings, stress_per_token


# ------------------------------------------------------------------ timeline planner


def frame_at(seconds: float) -> int:
    return round(seconds * 1000.0 / FRAME_MS)


def distribute(total: int, weights: list[float]) -> list[int]:
    """Split integer frames proportional to weights, each part at least one frame.

    Pure Python on purpose: the timeline planner runs (and is tested) without the onnxruntime
    extra installed.
    """
    if not weights:
        return []
    total = max(total, len(weights))
    weight_sum = sum(weights) or 1.0
    share = [weight / weight_sum * total for weight in weights]
    frames = [max(1, int(portion)) for portion in share]
    while sum(frames) < total:
        index = max(range(len(frames)), key=lambda i: share[i] - frames[i])
        frames[index] += 1
    while sum(frames) > total:
        index = max(range(len(frames)), key=lambda i: frames[i] - share[i])
        if frames[index] > 1:
            frames[index] -= 1
        else:
            break
    return frames


def plan_timeline(
    notes: list[NoteSpec],
    predicted: list[list[float]],
    bank: _Voicebank | None = None,
    total_seconds: float | None = None,
) -> list[dict]:
    """Lay phonemes on the note grid: every vowel starts exactly on its note.

    Onsets are sung ahead of the beat by taking time from the previous vowel first (consonant
    anticipation), then the previous coda (kept at least one frame), and only then older
    silence; codas stay inside the note. Notes are anchored to absolute time, so leading
    silence and rests keep the vocal aligned with the backing; trailing silence (up to
    ``total_seconds``) is padded too.
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
            # up to half the note goes to closing consonants, so dense codas stay intelligible
            coda_total = min(max(len(coda), wanted_coda), max(len(coda), int(note_frames * 0.5)))
        vowel_frames = max(1, note_frames - coda_total)

        cursor = sum(entry["frames"] for entry in timeline)
        gap = note_start - cursor
        if onset:
            wanted = max(len(onset), int(round(sum(weights[k] for k in range(len(onset))))))
            if wanted > gap:
                deficit = wanted - gap
                # Onsets are sung ahead of the beat: take time from the previous vowel first
                # (consonant anticipation), then the previous coda (kept at least one frame so
                # it stays audible), and only then older silence.
                for kinds in (("vowel",), ("coda",), ("gap", "pad")):
                    for entry in reversed(timeline):
                        if deficit <= 0:
                            break
                        if entry["kind"] in kinds:
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


def _word_inputs(
    segments: list[dict], notes: list[NoteSpec], bank: _Voicebank
) -> tuple[list[int], list[int]]:
    """Word boundaries and durations for the linguistic encoder.

    Words are vowel-anchored spans (the voicebank's ``ph_num`` convention): each vowel starts a
    span that runs to the next vowel, and the trailing silence is the last span. Durations are in
    frames; the span anchored at the leading pad uses the first note's duration, because that
    span carries the first word's onset consonants (the pad is sung ahead of the note). Without
    real word durations the encoder assumes one-frame words and predicts consonants at 1-3
    frames, which is why final consonants used to be cut off.
    """
    vowel_ids = [i for i, s in enumerate(segments) if bank.is_vowel(s["phoneme"])]
    word_div = [vowel_ids[i + 1] - vowel_ids[i] for i in range(len(vowel_ids) - 1)]
    word_div.append(len(segments) - vowel_ids[-1])
    note_frames = [frame_at(note.seconds) for note in notes]
    word_dur: list[int] = []
    for position, vowel_id in enumerate(vowel_ids):
        note = segments[vowel_id]["note"]
        if note >= 0:
            word_dur.append(note_frames[note])
        elif position == 0:
            word_dur.append(note_frames[0])
        else:
            word_dur.append(TAIL_FRAMES)
    return word_div, word_dur


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
    seed: int | None = None,
    total_seconds: float | None = None,
) -> dict:
    """Render the notes to a 44.1 kHz mono wav with the voicebank.

    ``seed`` fixes the sampling noise of the diffusion models (patched into the ONNX graphs in
    memory), so the same input renders identical audio; None draws fresh noise every run.
    """
    if mode not in MODES:
        raise DiffSingerError(f"Unknown mode '{mode}'; use one of {', '.join(MODES)}.")
    if not notes:
        raise DiffSingerError("No notes to render.")
    if any(note.is_rest for note in notes):
        raise DiffSingerError("Rest notes are not supported yet; drop silent notes instead.")
    bank = get_voicebank()

    if seed is None:

        def session_for(rel: str):
            return bank.session(rel)
    else:
        # ORT's random generators live inside the session: a fresh session starts from the
        # patched seeds' initial state, while a reused one keeps advancing. So seeded renders
        # build fresh sessions (cached per render, not across renders) and are reproducible.
        created: dict[str, ort.InferenceSession] = {}

        def session_for(rel: str):
            if rel not in created:
                created[rel] = ort.InferenceSession(
                    seeded_model_bytes(bank.path / rel, seed), providers=["CPUExecutionProvider"]
                )
            return created[rel]

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
    word_div, word_dur = _word_inputs(segments, notes, bank)
    encoder_out, x_masks = session_for("dsmain/linguistic.onnx").run(
        None,
        {
            "tokens": tokens,
            "word_div": np.array([word_div], dtype=np.int64),
            "word_dur": np.array([word_dur], dtype=np.int64),
        },
    )[:2]
    note_pitch = np.array(
        [[notes[s["note"]].midi if s["note"] >= 0 else 0 for s in segments]], dtype=np.int64
    )
    variance_embed = bank.embed("dsmain/embeds/variance", mode)
    predicted_flat = session_for("dsdur/dur.onnx").run(
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
    pitch_enc = session_for("dspitch/linguistic.onnx").run(None, {"tokens": tokens, "ph_dur": ph_dur})[0]
    pitch_base = np.zeros(total_frames, dtype=np.float32)
    frame = 0
    for frames, midi in zip(note_dur, note_midis, strict=True):
        pitch_base[frame : frame + frames] = midi
        frame += frames
    pitch_out = session_for("dspitch/pitch.onnx").run(
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
    mel = session_for("dsmain/acoustic.onnx").run(
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
    samples = session_for("dsvocoder/aidolgan.onnx").run(None, {"mel": mel, "f0": f0})[0][0]
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
