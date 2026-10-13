"""Tests for multi-voicebank DiffSinger support: scanning, slugs, variants, errors.

Fake banks are plain folders (configs + placeholder files), so these run without real ONNX
models; rendering against a real bank stays covered by the voicebank-gated tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chordsmith import diffsinger

pytestmark = pytest.mark.skipif(
    diffsinger._IMPORT_ERROR is not None, reason="the diffsinger extra is not installed"
)


def _fake_bank(
    root: Path,
    name: str,
    *,
    display: str,
    author: str = "Tester",
    variants: tuple[str, ...] = ("one", "two"),
    languages: tuple[str, ...] = ("en",),
    vocoder: bool = True,
) -> Path:
    bank = root / name
    (bank / "dsdur").mkdir(parents=True)
    (bank / "dspitch").mkdir(parents=True)
    (bank / "dsvocoder").mkdir(parents=True)
    (bank / "dsmain" / "embeds" / "acoustic").mkdir(parents=True)
    (bank / "dsdur" / "files").mkdir(parents=True)
    (bank / "dspitch" / "files").mkdir(parents=True)
    (bank / "dsmain" / "phonemes.txt").write_text("SP\nAP\nl\na\n", encoding="utf-8")
    (bank / "dsmain" / "acoustic.onnx").write_bytes(b"stub")
    (bank / "dsmain" / "linguistic.onnx").write_bytes(b"stub")
    (bank / "dsdur" / "dur.onnx").write_bytes(b"stub")
    (bank / "dspitch" / "linguistic.onnx").write_bytes(b"stub")
    (bank / "dspitch" / "pitch.onnx").write_bytes(b"stub")
    if vocoder:
        (bank / "dsvocoder" / "voc.onnx").write_bytes(b"stub")
    (bank / "dsdur" / "dsdict.yaml").write_text(
        "symbols:\n- symbol: l\n  type: consonant\n", encoding="utf-8"
    )
    for code in languages:
        (bank / "dsdur" / f"dsdict-{code}.yaml").write_text("entries: []\n", encoding="utf-8")
    (bank / "character.txt").write_text(f"name={display}\nauthor={author}\n", encoding="utf-8")
    speakers = [f"dsmain/embeds/acoustic/{name}_{variant}" for variant in variants]
    lines = [
        "acoustic: dsmain/acoustic.onnx",
        "phonemes: dsmain/phonemes.txt",
        "vocoder: dsvocoder",
        "speakers:",
        *[f"- {entry}" for entry in speakers],
    ]
    (bank / "dsconfig.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    dur_speakers = [f"files/{name}_{variant}" for variant in variants]
    (bank / "dsdur" / "dsconfig.yaml").write_text(
        "linguistic: ../dsmain/linguistic.onnx\ndur: dur.onnx\nspeakers:\n"
        + "\n".join(f"- {entry}" for entry in dur_speakers)
        + "\n",
        encoding="utf-8",
    )
    (bank / "dspitch" / "dsconfig.yaml").write_text(
        "linguistic: linguistic.onnx\npitch: pitch.onnx\nspeakers:\n- files/single\n",
        encoding="utf-8",
    )
    for variant in variants:
        (bank / "dsmain" / "embeds" / "acoustic" / f"{name}_{variant}.emb").write_bytes(b"\x00" * 16)
        (bank / "dsdur" / "files" / f"{name}_{variant}.emb").write_bytes(b"\x00" * 16)
    (bank / "dspitch" / "files" / "single.emb").write_bytes(b"\x00" * 16)
    return bank


@pytest.fixture
def two_banks(tmp_path, monkeypatch):
    root = tmp_path / "banks"
    root.mkdir()
    alpha = _fake_bank(
        root, "AlphaBank", display="Alpha ~AI~ Singer", variants=("one", "two"), languages=("en", "ja")
    )
    beta = _fake_bank(root, "BetaBank", display="Beta", variants=("solo",), languages=("en",))
    monkeypatch.setenv("CHORDSMITH_DIFFSINGER_VOICE", str(root))
    monkeypatch.chdir(tmp_path)  # keep the real ./voicebank out of the scan
    return alpha, beta


def test_parent_folder_scan_lists_all_banks(two_banks):
    banks = diffsinger.list_voicebanks()
    assert [info["slug"] for info in banks] == ["alpha", "beta"]
    assert banks[0]["variants"] == ["one", "two"]
    assert banks[0]["languages"] == ["en", "ja"]
    assert banks[0]["author"] == "Tester"


def test_bank_and_variant_lookup(two_banks):
    assert diffsinger.voicebank_path("alpha") == two_banks[0]
    assert diffsinger.voicebank_path("beta") == two_banks[1]
    # the bare word inside a decorated name still resolves
    assert diffsinger.voicebank_path("singer") == two_banks[0]
    assert diffsinger.voicebank_path() == two_banks[0]  # first bank by default
    bank = diffsinger.get_voicebank("beta")
    assert bank.variant(None) == "solo"
    assert bank.variant("SOLO") == "solo"
    with pytest.raises(diffsinger.DiffSingerError, match="one, two"):
        diffsinger.get_voicebank("alpha").variant("missing")


def test_unknown_bank_names_the_configured_ones(two_banks):
    with pytest.raises(diffsinger.DiffSingerError, match="AlphaBank"):
        diffsinger.get_voicebank("no-such-bank")


def test_variant_embeddings_load_per_module(two_banks):
    bank = diffsinger.get_voicebank("alpha")
    acoustic = bank.embed("acoustic", "one")
    dur = bank.embed("dur", "two")
    pitch = bank.embed("pitch", "one")  # single pitch embedding serves every variant
    assert acoustic is not None and dur is not None and pitch is not None
    assert len(acoustic) == 4  # 16 bytes as float32
    with pytest.raises(diffsinger.DiffSingerError, match="no 'missing' embeddings"):
        bank.embed("acoustic", "missing")


def test_broken_bank_appears_with_error_not_a_crash(tmp_path, monkeypatch):
    root = tmp_path / "banks"
    root.mkdir()
    _fake_bank(root, "BrokenBank", display="Broken", vocoder=False)
    monkeypatch.setenv("CHORDSMITH_DIFFSINGER_VOICE", str(root))
    monkeypatch.chdir(tmp_path)
    banks = diffsinger.list_voicebanks()
    assert len(banks) == 1 and "vocoder" in banks[0]["error"]
    with pytest.raises(diffsinger.DiffSingerError, match="vocoder"):
        diffsinger.get_voicebank("brokenbank")


def test_legacy_short_id_still_matches_hanami():
    # 'hanami' finds the real bank when it is installed (folder name and full name both carry it)
    if not diffsinger.available():
        pytest.skip("no voicebank installed")
    assert diffsinger._match_bank("hanami") is not None
