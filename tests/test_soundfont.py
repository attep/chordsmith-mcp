"""Tests for SoundFont preset inspection (the pdta/phdr reader)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from chordsmith import audio, soundfont


def _phdr_record(name: str, bank: int, program: int) -> bytes:
    return name.encode("latin-1").ljust(20, b"\x00") + struct.pack("<HHHIII", program, bank, 0, 0, 0, 0)


def _make_sf2(presets: list[tuple[str, int, int]], extra_lists: bool = False) -> bytes:
    phdr = b"".join(_phdr_record(name, bank, program) for name, bank, program in presets)
    phdr += _phdr_record("EOP", 0, 0)
    pdta = b"pdta" + b"phdr" + struct.pack("<I", len(phdr)) + phdr
    body = b"sfbk"
    if extra_lists:
        # an odd-sized INFO list and a stray chunk must be walked past (RIFF pads to even)
        info_sub = b"IENG" + struct.pack("<I", 3) + b"abc\x00"
        body += b"LIST" + struct.pack("<I", 4 + len(info_sub)) + b"INFO" + info_sub
        body += b"junk" + struct.pack("<I", 1) + b"x\x00"
    body += b"LIST" + struct.pack("<I", len(pdta)) + pdta
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_read_presets_parses_name_bank_program(tmp_path: Path):
    path = tmp_path / "kit.sf2"
    path.write_bytes(_make_sf2([("Yamaha Grand Piano", 0, 0), ("Synth Bass 1", 0, 38), ("Power", 128, 16)]))
    presets = soundfont.read_presets(path)
    assert [preset.name for preset in presets] == ["Yamaha Grand Piano", "Synth Bass 1", "Power"]
    assert [(preset.bank, preset.program) for preset in presets] == [(0, 0), (0, 38), (128, 16)]
    assert presets[2].label() == "128-016 Power"
    assert soundfont.find_preset(presets, 128, 16) == presets[2]
    assert soundfont.find_preset(presets, 128, 25) is None


def test_read_presets_walks_past_other_chunks(tmp_path: Path):
    path = tmp_path / "chunks.sf2"
    path.write_bytes(_make_sf2([("Standard", 128, 0)], extra_lists=True))
    presets = soundfont.read_presets(path)
    assert [(preset.bank, preset.program, preset.name) for preset in presets] == [(128, 0, "Standard")]


def test_read_presets_rejects_non_soundfonts(tmp_path: Path):
    not_riff = tmp_path / "x.sf2"
    not_riff.write_bytes(b"not a soundfont at all")
    with pytest.raises(soundfont.SoundFontError, match="not a SoundFont"):
        soundfont.read_presets(not_riff)

    riff_without_pdta = tmp_path / "y.sf2"
    riff_without_pdta.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"sfbk")
    with pytest.raises(soundfont.SoundFontError, match="no preset table"):
        soundfont.read_presets(riff_without_pdta)

    missing = tmp_path / "missing.sf2"
    with pytest.raises(soundfont.SoundFontError, match="cannot be read"):
        soundfont.read_presets(missing)


def test_available_labels_truncates():
    presets = [soundfont.Preset(name=f"P{i}", bank=0, program=i) for i in range(50)]
    text = soundfont.available_labels(presets, limit=3)
    assert text.startswith("000-000 P0, 000-001 P1, 000-002 P2, ... (50 total)")


@pytest.mark.skipif(audio.find_soundfont() is None, reason="no soundfont installed")
def test_read_presets_of_the_installed_soundfont():
    presets = soundfont.read_presets(audio.find_soundfont())
    assert len(presets) > 100
    power = soundfont.find_preset(presets, 128, 16)
    assert power is not None and "Power" in power.name  # FluidR3's drum kit, 0-based program 16
