"""DiffSinger adapter (Sodium Gold slice 2): lyrics through any compatible voicebank.

Voicebanks are **never bundled or redistributed**: download them yourself, mount them
read-only, and point ``CHORDSMITH_DIFFSINGER_VOICE`` at one bank, several (path-separated) or
a parent folder — every directory with a ``dsconfig.yaml`` is registered.

The pipeline follows the OpenUtau DiffSinger conventions, resolved per bank from its
``dsconfig.yaml`` files: the duration module's linguistic encoder with word_div/word_dur
predicts phoneme durations (dur.onnx), the durations are laid out so every syllable's vowel
starts exactly on its note, the pitch module predicts the f0 curve, the acoustic model renders
a mel spectrogram and the bank's vocoder turns it into 44.1 kHz audio. Inputs are fed
according to what each ONNX graph declares, so banks with different exports (steps vs
speedup, speaker embeddings or none) run the same code path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import wave
from dataclasses import dataclass, field, replace
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
SPEEDUP = 10.0  # the sampling-acceleration control that newer ONNX exports declare instead of steps
DEFAULT_CHUNK_SECONDS = 30.0  # long scores render in phrase-sized chunks (attention is O(n^2))
MIN_CHUNK_GAP_SECONDS = 0.3  # a gap at least this long is a clean split point (a rest)
DEFAULT_DEPTH = 0.6
_BANK_LICENCES = {
    "hoshino hanami": (
        "Voicebank models: Team L<3VE Voicebank License - any creative use including commercial, "
        "credit required. Bundled vocoder (AI*dolGAN): CC BY-NC-SA 4.0 - non-commercial; get "
        "written permission or swap the vocoder before a commercial release."
    ),
    "leona": (
        "Team L<3VE Voicebank License family (same author as Hanami): creative use with credit; "
        "check the bank's readme for the bundled vocoder's terms before a commercial release."
    ),
    "tiger": (
        "CC BY-NC-ND 4.0 + Commons Clause: free for non-commercial/personal use per the "
        "character TOS; commercial use needs a paid license from the author, and the model may "
        "not be redistributed (see the bank's extra/LICENSE.md)."
    ),
}
_GENERIC_LICENCE = "Check the voicebank's own readme and licence before publishing rendered audio."


class DiffSingerError(ValueError):
    """Raised when the DiffSinger voicebank or pipeline cannot serve a request."""


def _require_dependencies() -> None:
    if _IMPORT_ERROR is not None:
        raise DiffSingerError(
            "DiffSinger support needs extra packages: install with "
            f"'pip install chordsmith-mcp[diffsinger]' (missing: {_IMPORT_ERROR})."
        )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _bank_roots() -> list[Path]:
    """Every configured voicebank folder, deduplicated and in a stable order.

    ``CHORDSMITH_DIFFSINGER_VOICE`` may name one bank, several (``os.pathsep``-separated), or a
    parent folder to scan one level; ``./voicebank`` is scanned the same way. A directory is a
    bank when it contains ``dsconfig.yaml``.
    """
    candidates: list[Path] = []
    env = os.environ.get("CHORDSMITH_DIFFSINGER_VOICE", "").strip()
    if env:
        for entry in env.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            path = Path(entry).expanduser()
            if (path / "dsconfig.yaml").is_file():
                candidates.append(path)
            elif path.is_dir():
                candidates.extend(sorted(p for p in path.iterdir() if p.is_dir()))
    default_root = Path.cwd() / "voicebank"
    if default_root.is_dir():
        candidates.extend(sorted(p for p in default_root.iterdir() if p.is_dir()))
    banks: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not (candidate / "dsconfig.yaml").is_file():
            continue
        # The same bank can be reachable through two paths (env + ./voicebank, two mounts):
        # identify it by folder name + config content, not by path.
        key = f"{candidate.name.casefold()}:{_bank_fingerprint(candidate)}"
        if key in seen:
            continue
        seen.add(key)
        banks.append(candidate)
    return banks


def _bank_fingerprint(bank: Path) -> str:
    try:
        return hashlib.sha256((bank / "dsconfig.yaml").read_bytes()).hexdigest()
    except OSError:
        return ""


def _match_bank(identifier: str) -> Path | None:
    """Resolve a bank identifier: a folder path, a folder name, or a slug/word in either.

    A short word finds the folder whose slug, character name or path word contains it —
    case-insensitively.
    """
    text = str(identifier).strip()
    if not text:
        return None
    direct = Path(text).expanduser()
    if (direct / "dsconfig.yaml").is_file():
        return direct
    banks = _bank_roots()
    lowered = text.casefold()
    for bank in banks:
        full = _full_name(bank)
        display = _short_name(full) if full else bank.name
        if lowered in (bank.name.casefold(), _slug(bank.name), _slug(display), _slug(full) if full else ""):
            return bank
    for bank in banks:
        full = _full_name(bank)
        words = set(_slug(full or bank.name).split("_"))
        words.update(_slug(bank.name).split("_"))
        if lowered in words:
            return bank
    return None


def _full_name(bank: Path) -> str:
    """The bank's full name: character.yaml ``name``, then the ``name=`` line of character.txt."""
    name = ""
    try:
        config = yaml.safe_load((bank / "character.yaml").read_text(encoding="utf-8")) or {}
        name = str(config.get("name") or "")
    except Exception:  # noqa: BLE001 - a broken character.yaml never blocks a bank
        name = ""
    if not name:
        try:
            for line in (bank / "character.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("name="):
                    name = line.split("=", 1)[1].strip()
                    break
        except OSError:
            name = ""
    return name


def _short_name(text: str) -> str:
    """The name up to its first decoration, e.g. 'Hoshino Hanami ~AI...' -> 'Hoshino Hanami'."""
    text = text.strip()
    return re.split(r"\s+[~\-(\[]", text)[0].strip() or text


def _bank_text_value(bank: Path, key: str) -> str:
    """A ``key=value`` line from character.txt (author, voice, ...)."""
    try:
        for line in (bank / "character.txt").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def voicebank_path(identifier: str | None = None) -> Path | None:
    """Resolve a voicebank: by identifier (slug/name/path), else the first configured one."""
    if identifier:
        return _match_bank(identifier)
    banks = _bank_roots()
    return banks[0] if banks else None


def available() -> bool:
    return bool(_bank_roots())


def _relative(base: Path, value: str) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path)


def _module_speakers(base: Path, entries) -> list[tuple[str, Path]]:
    """(variant name, embedding base path) for a module's ``speakers`` list."""
    out: list[tuple[str, Path]] = []
    for entry in entries or []:
        path = _relative(base, str(entry))
        out.append((path.name, path))
    return out


def _strip_common_prefix(names: list[str]) -> list[str]:
    """Drop a shared ``..._`` word prefix so ``tiger_fresh`` reads as ``fresh``.

    Works for a single name too (``BetaBank_solo`` -> ``solo``); a name with no ``_`` in its
    common prefix stays as it is.
    """
    if not names:
        return names
    prefix = os.path.commonprefix(names)
    cut = prefix.rfind("_") + 1
    if cut <= 0:
        return names
    return [name[cut:] or name for name in names]


def _shorten_speakers(speakers: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Speaker lists with the shared variant prefix stripped (tiger_fresh -> fresh)."""
    if not speakers:
        return []
    names = _strip_common_prefix([name for name, _ in speakers])
    return list(zip(names, [path for _, path in speakers], strict=True))


def _find_vocoder(bank: Path, value) -> Path | None:
    """The bank's vocoder ONNX: the declared folder, else ``dsvocoder/``."""
    text = str(value or "").strip()
    declared = bank / text if text else None
    directory = declared if declared is not None and declared.is_dir() else bank / "dsvocoder"
    if not directory.is_dir():
        return None
    try:
        config = yaml.safe_load((directory / "vocoder.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - the vocoder.yaml is a hint, not a requirement
        config = {}
    model = config.get("model")
    if model and (directory / str(model)).is_file():
        return directory / str(model)
    models = sorted(directory.glob("*.onnx"))
    return models[0] if models else None


@dataclass
class BankPaths:
    """One voicebank's resolved files, read from its dsconfig.yaml files."""

    root: Path
    display_name: str
    author: str
    phonemes: Path
    dur_phonemes: Path
    pitch_phonemes: Path
    dictionaries: dict[str, Path]
    acoustic: Path
    acoustic_speakers: list[tuple[str, Path]]
    dur_linguistic: Path
    dur: Path
    dur_speakers: list[tuple[str, Path]]
    pitch_linguistic: Path
    pitch: Path
    pitch_speakers: list[tuple[str, Path]]
    vocoder: Path

    @property
    def variants(self) -> list[str]:
        return [name for name, _ in self.acoustic_speakers]


def _load_bank(bank: Path) -> BankPaths:
    """Resolve a bank's model files from its configs (cheap; never loads a model)."""
    _require_dependencies()
    try:
        root_cfg = yaml.safe_load((bank / "dsconfig.yaml").read_text(encoding="utf-8")) or {}
        dur_cfg = yaml.safe_load((bank / "dsdur" / "dsconfig.yaml").read_text(encoding="utf-8")) or {}
        pitch_cfg = yaml.safe_load((bank / "dspitch" / "dsconfig.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DiffSingerError(f"Voicebank '{bank.name}' has unreadable config files: {exc}") from None
    dur_dir = bank / "dsdur"
    pitch_dir = bank / "dspitch"
    acoustic_speakers = _shorten_speakers(_module_speakers(bank, root_cfg.get("speakers")))
    dur_speakers = _shorten_speakers(_module_speakers(dur_dir, dur_cfg.get("speakers")))
    pitch_speakers = _shorten_speakers(_module_speakers(pitch_dir, pitch_cfg.get("speakers")))
    dictionaries: dict[str, Path] = {}
    for path in sorted(dur_dir.glob("dsdict-*.yaml")):
        code = path.stem[len("dsdict-") :]
        if re.fullmatch(r"[a-z]{2}|zh-yue", code):
            dictionaries[code] = path
    vocoder = _find_vocoder(bank, root_cfg.get("vocoder"))
    root_phonemes = _relative(bank, str(root_cfg.get("phonemes", "")))

    def module_phonemes(config: dict, base: Path) -> Path:
        value = str(config.get("phonemes") or "").strip()
        return _relative(base, value) if value else root_phonemes

    paths = BankPaths(
        root=bank,
        display_name=_short_name(_full_name(bank)) or bank.name,
        author=_character_author(bank),
        phonemes=root_phonemes,
        dur_phonemes=module_phonemes(dur_cfg, dur_dir),
        pitch_phonemes=module_phonemes(pitch_cfg, pitch_dir),
        dictionaries=dictionaries,
        acoustic=_relative(bank, str(root_cfg.get("acoustic", ""))),
        acoustic_speakers=acoustic_speakers,
        dur_linguistic=_relative(dur_dir, str(dur_cfg.get("linguistic", ""))),
        dur=_relative(dur_dir, str(dur_cfg.get("dur", ""))),
        dur_speakers=dur_speakers,
        pitch_linguistic=_relative(pitch_dir, str(pitch_cfg.get("linguistic", ""))),
        pitch=_relative(pitch_dir, str(pitch_cfg.get("pitch", ""))),
        pitch_speakers=pitch_speakers,
        vocoder=vocoder or (bank / "dsvocoder" / "missing.onnx"),
    )
    for path, label in (
        (paths.phonemes, "phoneme list"),
        (paths.dur_phonemes, "duration phoneme list"),
        (paths.pitch_phonemes, "pitch phoneme list"),
        (paths.acoustic, "acoustic model"),
        (paths.dur_linguistic, "duration linguistic model"),
        (paths.dur, "duration model"),
        (paths.pitch_linguistic, "pitch linguistic model"),
        (paths.pitch, "pitch model"),
        (paths.vocoder, "vocoder"),
    ):
        if not path.is_file():
            raise DiffSingerError(f"Voicebank '{bank.name}' is missing its {label} (expected at {path}).")
    return paths


def _character_author(bank: Path) -> str:
    author = ""
    try:
        config = yaml.safe_load((bank / "character.yaml").read_text(encoding="utf-8")) or {}
        author = str(config.get("author") or "")
    except Exception:  # noqa: BLE001 - a broken character.yaml never blocks a bank
        author = ""
    return author or _bank_text_value(bank, "author")


def list_voicebanks() -> list[dict]:
    """Metadata for every configured bank (cheap: reads configs, never loads models).

    A broken bank appears with an ``error`` field instead of taking the listing down.
    """
    entries: list[dict] = []
    for path in _bank_roots():
        try:
            entries.append(_describe_bank(_load_bank(path)))
        except DiffSingerError as exc:
            entries.append({"voicebank": path.name, "path": str(path), "error": str(exc)})
    return entries


def describe(identifier: str | None = None) -> dict | None:
    """Voicebank metadata for list_singing_voices and render_singing's voice ids."""
    path = voicebank_path(identifier)
    if path is None:
        return None
    return _describe_bank(_load_bank(path))


def _describe_bank(bank: BankPaths) -> dict:
    display = bank.display_name or bank.root.name
    licence = _GENERIC_LICENCE
    haystack = f"{_slug(display)} {_slug(bank.root.name)}"
    for key, note in _BANK_LICENCES.items():
        if _slug(key) in haystack:
            licence = note
            break
    return {
        "engine": "diffsinger",
        "voicebank": display,
        "slug": _slug(display) or _slug(bank.root.name),
        "path": str(bank.root),
        "author": bank.author,
        "credit": f"{display} by {bank.author}" if bank.author else display,
        "licence": licence,
        "languages": sorted(bank.dictionaries),
        "variants": bank.variants,
        "sample_rate": SAMPLE_RATE,
        "soft_controls": _soft_controls_of(bank.root) or {},
    }


def soft_controls(identifier: str | None = None) -> dict | None:
    """Which soft controls a voicebank supports, from its dsconfig flags.

    DiffSinger voicebanks declare whether they were trained with the variance embeddings
    (``use_breathiness_embed``, ``use_voicing_embed``, ``use_tension_embed``, ``use_energy_embed``)
    and the key-shift/speed embeddings behind the ``gender`` and ``velocity`` controls. A bank
    that declares none of them cannot use those settings — the refusal names the bank and the
    missing flag — while a bank that declares one gets the setting fed to its acoustic model.
    Returns None when the bank is not configured.
    """
    path = voicebank_path(identifier)
    if path is None:
        return None
    return _soft_controls_of(path)


def _soft_controls_of(path: Path) -> dict | None:
    try:
        config = yaml.safe_load((path / "dsconfig.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - an unreadable config reads as "none"
        return None
    return {
        "voicebank": path.name,
        "breathiness": bool(config.get("use_breathiness_embed")),
        "voicing": bool(config.get("use_voicing_embed")),
        "tension": bool(config.get("use_tension_embed")),
        "energy_embed": bool(config.get("use_energy_embed")),
        "key_shift": bool(config.get("use_key_shift_embed", True)),
        "speed": bool(config.get("use_speed_embed", True)),
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


_EMBED_SUFFIXES = (".emb", ".npy", ".bin")


@dataclass
class _Voicebank:
    """One loaded bank: resolved paths plus lazily loaded sessions, dictionaries, embeddings."""

    paths: BankPaths
    lock: threading.Lock = field(default_factory=threading.Lock)
    sessions: dict[str, ort.InferenceSession] = field(default_factory=dict)
    phoneme_ids: dict[str, int] = field(default_factory=dict)
    dur_phoneme_ids: dict[str, int] = field(default_factory=dict)
    pitch_phoneme_ids: dict[str, int] = field(default_factory=dict)
    symbol_types: dict[str, str] = field(default_factory=dict)
    en_dict: dict[str, list[str]] = field(default_factory=dict)
    cmu: dict = field(default_factory=dict)
    embeds: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.paths.root

    @property
    def variants(self) -> list[str]:
        return self.paths.variants

    def session(self, path: Path) -> ort.InferenceSession:
        key = str(path)
        with self.lock:
            if key not in self.sessions:
                self.sessions[key] = ort.InferenceSession(key, providers=["CPUExecutionProvider"])
            return self.sessions[key]

    def variant(self, mode: str | None) -> str:
        """Canonical variant name for a mode id (case-insensitive; the only one when single)."""
        names = self.variants
        if not names:
            if mode:
                raise DiffSingerError(
                    f"Voicebank '{self.paths.display_name}' has no voice variants; drop '/{mode}'."
                )
            return "default"
        if mode is None:
            return names[0]
        lowered = str(mode).casefold()
        for name in names:
            if name.casefold() == lowered:
                return name
        for name in names:
            if _slug(name) == _slug(str(mode)):
                return name
        raise DiffSingerError(
            f"Unknown voice variant '{mode}' for '{self.paths.display_name}'; use one of {', '.join(names)}."
        )

    def embed(self, module: str, variant: str) -> np.ndarray | None:
        """The variant's embedding for a module ('acoustic'/'dur'/'pitch'), or None."""
        speakers = {
            "acoustic": self.paths.acoustic_speakers,
            "dur": self.paths.dur_speakers,
            "pitch": self.paths.pitch_speakers,
        }[module]
        if not speakers:
            return None
        match = None
        for name, path in speakers:
            if name.casefold() == variant.casefold():
                match = (name, path)
                break
        if match is None and len(speakers) == 1:
            match = speakers[0]
        if match is None:
            names = ", ".join(name for name, _ in speakers)
            raise DiffSingerError(
                f"Voicebank '{self.paths.display_name}' has no '{variant}' embeddings for its "
                f"{module} model; it has {names}."
            )
        name, base = match
        key = f"{module}/{name}"
        with self.lock:
            if key not in self.embeds:
                for suffix in _EMBED_SUFFIXES:
                    candidate = base.with_name(base.name + suffix)
                    if candidate.is_file():
                        self.embeds[key] = np.fromfile(candidate, dtype=np.float32)
                        break
                else:
                    raise DiffSingerError(f"Embedding file for '{base}' not found.")
            return self.embeds[key]

    def is_vowel(self, phoneme: str) -> bool:
        return self.symbol_types.get(phoneme) == "vowel" or phoneme in ("SP", "AP")

    def phonemize(self, word: str) -> list[str]:
        key = word.lower()
        if key in self.en_dict:
            return list(self.en_dict[key])
        if key in self.cmu:
            return [phone.rstrip("012").lower() for phone in self.cmu[key][0]]
        if not self.en_dict:
            raise DiffSingerError(
                f"Voicebank '{self.paths.display_name}' has no English dictionary; use "
                "language='phonemes' with its own phoneme set."
            )
        raise DiffSingerError(f"Unknown word for the English dictionary: '{word}'")


_banks: dict[str, _Voicebank] = {}
_voicebank_lock = threading.Lock()


def get_voicebank(identifier: str | Path | None = None) -> _Voicebank:
    """The loaded bank for an identifier (slug/name/path); the first configured one by default."""
    _require_dependencies()
    path = voicebank_path(str(identifier) if identifier is not None else None)
    if path is None:
        if identifier is not None:
            names = ", ".join(bank.name for bank in _bank_roots()) or "(none)"
            raise DiffSingerError(f"Unknown DiffSinger voicebank '{identifier}'. Banks here: {names}.")
        raise DiffSingerError(
            "No DiffSinger voicebank configured. Download one (see docs/singing.md), mount it and "
            "set CHORDSMITH_DIFFSINGER_VOICE to its directory (or drop it into ./voicebank)."
        )
    key = str(path.resolve()).lower()
    with _voicebank_lock:
        if key not in _banks:
            bank = _Voicebank(paths=_load_bank(path))
            bank.phoneme_ids = _phoneme_table(bank.paths.phonemes)
            bank.dur_phoneme_ids = _phoneme_table(bank.paths.dur_phonemes)
            bank.pitch_phoneme_ids = _phoneme_table(bank.paths.pitch_phonemes)
            dsdict = yaml.safe_load((path / "dsdur/dsdict.yaml").read_text(encoding="utf-8"))
            bank.symbol_types = {s["symbol"]: s["type"] for s in dsdict["symbols"]}
            en_path = bank.paths.dictionaries.get("en")
            if en_path is not None:
                entries = yaml.safe_load(en_path.read_text(encoding="utf-8"))["entries"]
                bank.en_dict = {e["grapheme"].lower(): e["phonemes"] for e in entries}
            bank.cmu = cmudict.dict()
            _banks[key] = bank
    return _banks[key]


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


def _word_phones(
    bank: _Voicebank, word: str, overrides: dict[str, list[str]] | None = None
) -> tuple[list[str], list[float | None]] | None:
    """Whole-word lookup: custom words first, then the voicebank dictionary, then CMUdict."""
    key = word.lower()
    if overrides and key in overrides:
        phones = overrides[key]
        return list(phones), [None] * len(phones)
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
    bank: _Voicebank, groups: list[list[str]], overrides: dict[str, list[str]] | None = None
) -> tuple[list[list[str]], list[str], list[float | None]]:
    """Phones per flat token, warnings, and the stress level of each token's vowel.

    Syllables of one word are looked up as the whole word and its sounds are split between the
    word's notes (one vowel per syllable); when that is impossible, the pieces are looked up
    individually and a warning names the word. A '+' carries the previous vowel onto the next
    note and moves the closing consonants to the last note of the slur ("still +" sings
    s-t-ih then ih-l, not s-t-ih-l then ih). ``overrides`` maps custom words to voicebank
    phonemes and is checked before the dictionaries.
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
        whole = _word_phones(bank, word, overrides)
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
                single = _word_phones(bank, token, overrides)
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


@dataclass
class Chunk:
    """One renderable phrase: notes with their times shifted to start at zero."""

    start_seconds: float
    notes: list[NoteSpec]

    @property
    def seconds(self) -> float:
        return max((note.start_seconds + note.seconds for note in self.notes), default=0.0)


def plan_chunks(
    notes: list[NoteSpec],
    max_seconds: float = DEFAULT_CHUNK_SECONDS,
    min_gap: float = MIN_CHUNK_GAP_SECONDS,
) -> list[Chunk]:
    """Split notes into phrase-sized chunks so long scores render with bounded memory.

    The acoustic model's attention is O(n^2) in frames, so a whole three-minute score is what
    makes a render run out of memory; phrase chunks bound it by the phrase. Boundaries are
    placed only at note boundaries (never inside a note) and prefer the largest rest in the
    window, so a chunk is a singable phrase. A single note longer than ``max_seconds`` becomes
    its own chunk. Notes in a chunk are shifted to start at zero; the chunk remembers its
    absolute start so the caller can stitch at exact offsets.
    """
    ordered = sorted(notes, key=lambda note: note.start_seconds)
    chunks: list[Chunk] = []
    total = len(ordered)
    index = 0
    while index < total:
        start = ordered[index].start_seconds
        cursor = index + 1
        while cursor < total and (
            ordered[cursor].start_seconds + ordered[cursor].seconds - start <= max_seconds
        ):
            cursor += 1
        if cursor < total:
            # the chunk must end before `cursor`; prefer the largest rest in (index, cursor]
            best: int | None = None
            best_gap = -1.0
            for candidate in range(index + 1, cursor + 1):
                previous = ordered[candidate - 1]
                gap = ordered[candidate].start_seconds - (previous.start_seconds + previous.seconds)
                if gap >= min_gap and gap > best_gap:
                    best_gap = gap
                    best = candidate
            cut = best if best is not None else cursor
        else:
            cut = total
        chunk_notes = [
            replace(note, start_seconds=round(note.start_seconds - start, 6)) for note in ordered[index:cut]
        ]
        chunks.append(Chunk(start_seconds=start, notes=chunk_notes))
        index = cut
    return chunks


def stitch_chunks(
    rendered: list[tuple[float, Path]], output: Path, total_seconds: float | None = None
) -> float:
    """Sum rendered chunk wavs onto a silence canvas at their exact start offsets.

    Chunks never overlap a note; where a release tail overlaps the next chunk's start the tails
    sum (overlap-add), which is what a continuous render would have produced. Writes a 44.1 kHz
    mono wav and returns its duration in seconds.
    """
    _require_dependencies()
    pieces: list[tuple[int, np.ndarray]] = []
    rate = SAMPLE_RATE
    for start, path in rendered:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
        pieces.append((round(start * rate), samples.astype(np.float32) / 32768.0))
    length = max((offset + len(samples) for offset, samples in pieces), default=0)
    if total_seconds is not None:
        length = max(length, round(total_seconds * rate))
    canvas = np.zeros(length, dtype=np.float32)
    for offset, samples in pieces:
        canvas[offset : offset + len(samples)] += samples
    peak = float(np.abs(canvas).max())
    if peak > 0.891:  # keep exports below -1 dBFS
        canvas = canvas * (0.891 / peak)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(canvas, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return length / rate


_EMBED_NEUTRAL = {"breathiness": 0.0, "voicing": 1.0, "tension": 0.0, "energy": 1.0}


def _phoneme_table(path: Path) -> dict[str, int]:
    """The phoneme-id table of one module's phonemes.txt (one symbol per line)."""
    return {line: index for index, line in enumerate(path.read_text(encoding="utf-8").splitlines())}


def _index_tokens(bank: _Voicebank, phonemes: list[str], table: dict[str, int], module: str) -> np.ndarray:
    """Token ids for one module, indexed by its own phoneme table.

    Modules can carry different tables (TIGER's pitch list holds 60 phones, its acoustic 68 —
    the same phones at different indices), so every model gets the ids it was trained with.
    """
    missing = sorted({phone for phone in phonemes if phone not in table})
    if missing:
        raise DiffSingerError(
            f"Phoneme(s) {', '.join(missing)} are not in '{bank.paths.display_name}'s {module} "
            "phoneme list; this voicebank cannot sing them."
        )
    return np.array([[table[phone] for phone in phonemes]], dtype=np.int64)


def _feed(session, values: dict, context: str) -> dict:
    """The declared inputs of a session, filled from ``values`` and cast to their dtypes.

    Export packs differ in dtype as well as names (one bank's pitch model wants
    ``note_midi`` as int64, another's as float), so every value is cast to whatever the
    graph declares. A declared input we cannot fill fails loudly.
    """
    dtypes = {
        "tensor(float)": np.float32,
        "tensor(double)": np.float64,
        "tensor(int64)": np.int64,
        "tensor(int32)": np.int32,
        "tensor(bool)": np.bool_,
    }
    declared = [value for value in session.get_inputs()]
    missing = [value.name for value in declared if value.name not in values]
    if missing:
        raise DiffSingerError(
            f"The {context} model of this voicebank needs input(s) this render cannot provide: "
            f"{', '.join(missing)}. This model family is not supported yet."
        )
    feed: dict = {}
    for value in declared:
        data = values[value.name]
        dtype = dtypes.get(value.type)
        if dtype is not None and isinstance(data, np.ndarray) and data.dtype != dtype:
            data = data.astype(dtype)
        feed[value.name] = data
    return feed


def _acoustic_inputs(
    session,
    *,
    tokens,
    ph_dur,
    f0,
    gender: float,
    velocity: float,
    spk_embed,
    depth: float,
    steps: int,
    speedup: float = SPEEDUP,
    breathiness: float | None = None,
    voicing: float | None = None,
    tension: float | None = None,
    context: str = "acoustic",
) -> dict:
    """The acoustic model's declared inputs, filled from the render settings.

    Only inputs the graph declares are fed — export packs differ (older ones take ``steps``,
    newer ones ``speedup``; some have speaker embeddings, some do not). A bank trained with
    breathiness/voicing/tension/energy embeddings expects those inputs, so a user-set or
    neutral value is fed whenever the model declares the input. A declared input we cannot
    fill raises with the model's name, so an incompatible bank fails loudly.
    """
    total_frames = int(ph_dur.sum())
    values: dict = {
        "tokens": tokens,
        "durations": ph_dur,
        "f0": f0,
        "gender": np.full((1, total_frames), gender, dtype=np.float32),
        "velocity": np.full((1, total_frames), velocity, dtype=np.float32),
        "depth": np.array(depth, dtype=np.float32),
        "steps": np.array(steps, dtype=np.int64),
        "speedup": np.array(speedup, dtype=np.float32),
    }
    if spk_embed is not None:
        values["spk_embed"] = spk_embed
    declared = {value.name for value in session.get_inputs()}
    for name, setting in (
        ("breathiness", breathiness),
        ("voicing", voicing),
        ("tension", tension),
        ("energy", None),  # the energy setting is output gain; the embed always gets its neutral
    ):
        if name in declared:
            value = _EMBED_NEUTRAL[name] if setting is None else float(setting)
            values[name] = np.full((1, total_frames), value, dtype=np.float32)
    return _feed(session, values, context)


def render(
    notes: list[NoteSpec],
    output: Path,
    *,
    mode: str | None = None,
    bank_id: str | Path | None = None,
    steps: int = DEFAULT_STEPS,
    depth: float = DEFAULT_DEPTH,
    velocity: float = 1.0,
    gender: float = 0.0,
    expr: float = 1.0,
    energy: float = 1.0,
    breathiness: float | None = None,
    voicing: float | None = None,
    tension: float | None = None,
    seed: int | None = None,
    total_seconds: float | None = None,
) -> dict:
    """Render the notes to a 44.1 kHz mono wav with one variant of a voicebank.

    ``bank_id`` names the bank (slug/name/path; the first configured one by default) and
    ``mode`` its voice variant (case-insensitive; the first when omitted). ``seed`` fixes the
    sampling noise of the diffusion models (patched into the ONNX graphs in memory), so the
    same input renders identical audio; None draws fresh noise every run.
    """
    if not notes:
        raise DiffSingerError("No notes to render.")
    if any(note.is_rest for note in notes):
        raise DiffSingerError("Rest notes are not supported yet; drop silent notes instead.")
    bank = get_voicebank(bank_id)
    variant = bank.variant(mode)

    if seed is None:

        def session_for(path: Path):
            return bank.session(path)
    else:
        # ORT's random generators live inside the session: a fresh session starts from the
        # patched seeds' initial state, while a reused one keeps advancing. So seeded renders
        # build fresh sessions (cached per render, not across renders) and are reproducible.
        created: dict[str, ort.InferenceSession] = {}

        def session_for(path: Path):
            key = str(path)
            if key not in created:
                created[key] = ort.InferenceSession(
                    seeded_model_bytes(path, seed), providers=["CPUExecutionProvider"]
                )
            return created[key]

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

    dur_tokens = _index_tokens(bank, [s["phoneme"] for s in segments], bank.dur_phoneme_ids, "duration")
    word_div, word_dur = _word_inputs(segments, notes, bank)
    linguistic = session_for(bank.paths.dur_linguistic)
    encoder_out, x_masks = linguistic.run(
        None,
        _feed(
            linguistic,
            {
                "tokens": dur_tokens,
                "word_div": np.array([word_div], dtype=np.int64),
                "word_dur": np.array([word_dur], dtype=np.int64),
            },
            f"'{bank.paths.display_name}' duration linguistic",
        ),
    )[:2]
    note_pitch = np.array(
        [[notes[s["note"]].midi if s["note"] >= 0 else 0 for s in segments]], dtype=np.int64
    )
    dur_embed = bank.embed("dur", variant)
    dur_values = {"encoder_out": encoder_out, "x_masks": x_masks, "ph_midi": note_pitch}
    if dur_embed is not None:
        dur_values["spk_embed"] = np.tile(dur_embed, (1, dur_tokens.shape[1], 1))
    dur_session = session_for(bank.paths.dur)
    predicted_flat = dur_session.run(
        None, _feed(dur_session, dur_values, f"'{bank.paths.display_name}' duration")
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
    pitch_tokens = _index_tokens(
        bank, [entry["phoneme"] for entry in timeline], bank.pitch_phoneme_ids, "pitch"
    )
    tokens = _index_tokens(bank, [entry["phoneme"] for entry in timeline], bank.phoneme_ids, "acoustic")

    # --- pitch
    note_dur, note_midis, note_rests = _note_partition(timeline, notes)
    pitch_linguistic = session_for(bank.paths.pitch_linguistic)
    pitch_enc = pitch_linguistic.run(
        None,
        _feed(
            pitch_linguistic,
            {"tokens": pitch_tokens, "ph_dur": ph_dur},
            f"'{bank.paths.display_name}' pitch linguistic",
        ),
    )[0]
    pitch_base = np.zeros(total_frames, dtype=np.float32)
    frame = 0
    for frames, midi in zip(note_dur, note_midis, strict=True):
        pitch_base[frame : frame + frames] = midi
        frame += frames
    pitch_embed = bank.embed("pitch", variant)
    pitch_values = {
        "encoder_out": pitch_enc,
        "ph_dur": ph_dur,
        "note_midi": np.array([note_midis], dtype=np.float32),
        "note_rest": np.array([note_rests], dtype=bool),
        "note_dur": np.array([note_dur], dtype=np.int64),
        "pitch": pitch_base.reshape(1, -1),
        "expr": np.full((1, total_frames), expr, dtype=np.float32),
        "retake": np.ones((1, total_frames), dtype=bool),
        "steps": np.array(steps, dtype=np.int64),
        "speedup": np.array(SPEEDUP, dtype=np.float32),
    }
    if pitch_embed is not None:
        pitch_values["spk_embed"] = np.tile(pitch_embed, (1, total_frames, 1))
    pitch_session = session_for(bank.paths.pitch)
    pitch_out = pitch_session.run(
        None, _feed(pitch_session, pitch_values, f"'{bank.paths.display_name}' pitch")
    )[0][0]
    voiced = np.concatenate(
        [np.full(entry["frames"], entry["kind"] not in ("pad", "gap"), dtype=bool) for entry in timeline]
    )
    voiced_index = np.flatnonzero(voiced)
    for index in np.flatnonzero(~voiced):
        pitch_out[index] = pitch_out[voiced_index[np.argmin(np.abs(voiced_index - index))]]
    f0 = (440.0 * np.power(2.0, (pitch_out - 69.0) / 12.0)).astype(np.float32).reshape(1, -1)

    # --- acoustic + vocoder
    acoustic_session = session_for(bank.paths.acoustic)
    acoustic_embed = bank.embed("acoustic", variant)
    mel = acoustic_session.run(
        None,
        _acoustic_inputs(
            acoustic_session,
            tokens=tokens,
            ph_dur=ph_dur,
            f0=f0,
            gender=gender,
            velocity=velocity,
            spk_embed=None if acoustic_embed is None else np.tile(acoustic_embed, (1, total_frames, 1)),
            depth=depth,
            steps=steps,
            breathiness=breathiness,
            voicing=voicing,
            tension=tension,
            context=f"'{bank.paths.display_name}' acoustic",
        ),
    )[0]
    vocoder = session_for(bank.paths.vocoder)
    samples = vocoder.run(
        None, _feed(vocoder, {"mel": mel, "f0": f0}, f"'{bank.paths.display_name}' vocoder")
    )[0][0]
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
        "mode": variant,
    }
