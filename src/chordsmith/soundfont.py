"""SoundFont (.sf2) inspection: the actual preset inventory of a file.

ChordSmith's per-track instruments name a bank and program; whether the loaded SoundFont really
contains that preset used to be unknowable from the API (FluidSynth silently keeps the previous
program on a miss). This module reads the file's ``pdta``/``phdr`` table — the authoritative
list of preset names, banks and programs — with a seek-based walk, so even a 148 MB soundfont is
inspected without loading its samples.

Conventions: banks and programs are **0-based MIDI numbers** (the numbers the tools take);
GM display numbering is program + 1. The percussion bank is soundfont-specific: FluidR3 puts its
drum kits in bank 128, which is a fact this module reports rather than assumes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

PHDR_RECORD_SIZE = 38


class SoundFontError(ValueError):
    """Raised when a file is not a readable SoundFont."""


@dataclass(frozen=True)
class Preset:
    """One preset in a SoundFont: its display name, bank and program (both 0-based)."""

    name: str
    bank: int
    program: int

    def label(self) -> str:
        return f"{self.bank:03d}-{self.program:03d} {self.name}"


def _parse_phdr(blob: bytes) -> list[Preset]:
    presets: list[Preset] = []
    for offset in range(0, len(blob) - PHDR_RECORD_SIZE + 1, PHDR_RECORD_SIZE):
        record = blob[offset : offset + PHDR_RECORD_SIZE]
        name = record[:20].split(b"\x00", 1)[0].decode("latin-1").strip()
        program, bank = struct.unpack_from("<HH", record, 20)
        if name == "EOP":  # end-of-presets sentinel
            break
        if name:
            presets.append(Preset(name=name, bank=bank, program=program))
    return presets


def _read_pdta(handle, remaining: int) -> list[Preset] | None:
    while remaining >= 8:
        header = handle.read(8)
        if len(header) < 8:
            break
        chunk_id = header[:4]
        size = struct.unpack("<I", header[4:])[0]
        remaining -= 8 + size + (size & 1)
        if chunk_id == b"phdr":
            return _parse_phdr(handle.read(size))
        handle.seek(size + (size & 1), 1)
    return None


def read_presets(path: str | Path) -> list[Preset]:
    """Read every preset in ``path`` (name, bank, program — 0-based), in table order.

    Raises :class:`SoundFontError` when the file is not a SoundFont or has no preset table.
    """
    path = Path(path)
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise SoundFontError(f"Soundfont '{path}' cannot be read: {exc}") from exc
    with handle:
        header = handle.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"sfbk":
            raise SoundFontError(f"'{path.name}' is not a SoundFont (.sf2) file.")
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[:4]
            size = struct.unpack("<I", chunk_header[4:])[0]
            if chunk_id == b"LIST" and size >= 4:
                list_type = handle.read(4)
                if list_type == b"pdta":
                    presets = _read_pdta(handle, size - 4)
                    if presets is not None:
                        return presets
                    break
                handle.seek(size - 4 + (size & 1), 1)
            else:
                handle.seek(size + (size & 1), 1)
    raise SoundFontError(f"'{path.name}' has no preset table (not a valid SoundFont).")


def find_preset(presets: list[Preset], bank: int, program: int) -> Preset | None:
    """The preset at ``bank``/``program``, or None when the SoundFont has no such preset."""
    for preset in presets:
        if preset.bank == bank and preset.program == program:
            return preset
    return None


def available_labels(presets: list[Preset], limit: int = 40) -> str:
    """A comma-separated list of preset labels for error messages (truncated when long)."""
    labels = [preset.label() for preset in presets]
    if len(labels) > limit:
        return ", ".join(labels[:limit]) + f", ... ({len(labels)} total)"
    return ", ".join(labels) or "(none)"
