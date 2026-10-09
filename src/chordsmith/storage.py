"""Output directory handling. Every file read or written stays inside the output folder."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_NAME_LENGTH = 100
MIDI_EXTENSIONS = (".mid", ".midi")
AUDIO_EXTENSIONS = (".wav", ".mp3")
MEDIA_EXTENSIONS = MIDI_EXTENSIONS + AUDIO_EXTENSIONS


class StorageError(ValueError):
    pass


def default_output_dir() -> Path:
    env = os.environ.get("CHORDSMITH_OUTPUT_DIR")
    return Path(env).expanduser() if env else Path.home() / "ChordSmith"


class FileStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root).expanduser() if root else default_output_dir()

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    @staticmethod
    def safe_name(name: str, extension: str = ".mid") -> str:
        base = name.replace("\\", "/").split("/")[-1].strip()
        if base.lower().endswith(MEDIA_EXTENSIONS):
            base = base.rsplit(".", 1)[0]
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-")
        if not base:
            raise StorageError(f"'{name}' is not a usable file name.")
        return base[: MAX_NAME_LENGTH - len(extension)] + extension

    def _inside(self, path: Path) -> Path:
        root = self.ensure_root()
        resolved = path.resolve()
        if resolved.parent != root:
            raise StorageError("Files must stay inside the ChordSmith output folder.")
        return resolved

    def new_path(
        self, name: str | None, default_stem: str, overwrite: bool = False, extension: str = ".mid"
    ) -> Path:
        stem = name or default_stem
        filename = self.safe_name(stem, extension)
        path = self._inside(self.root / filename)
        if overwrite or not path.exists():
            return path
        base = path.stem
        for i in range(2, 1000):
            candidate = path.with_name(f"{base}_{i}{extension}")
            if not candidate.exists():
                return candidate
        raise StorageError(f"Too many files named like '{filename}'.")

    def existing_path(self, name: str) -> Path:
        path = self._inside(self.root / self.safe_name(name))
        if not path.is_file():
            available = ", ".join(f["filename"] for f in self.list_files()[:20]) or "none yet"
            raise StorageError(f"File '{name}' not found. Available files: {available}.")
        return path

    def existing_media_path(self, name: str) -> Path:
        """Resolve a file that may be MIDI or rendered audio (used by the download route)."""
        base = name.replace("\\", "/").split("/")[-1].strip()
        extension = next((ext for ext in MEDIA_EXTENSIONS if base.lower().endswith(ext)), None)
        if extension is None:
            raise StorageError(f"'{name}' is not a .mid, .wav or .mp3 file.")
        path = self._inside(self.root / self.safe_name(base, extension))
        if not path.is_file():
            raise StorageError(f"File '{name}' not found.")
        return path

    def remove(self, name: str) -> Path:
        path = self.existing_path(name)
        path.unlink()
        return path

    def rename(self, name: str, new_name: str) -> Path:
        source = self.existing_path(name)
        target = self._inside(self.root / self.safe_name(new_name))
        if target.exists():
            raise StorageError(f"'{target.name}' already exists; pick another name.")
        source.rename(target)
        return target

    def list_files(self) -> list[dict]:
        root = self.ensure_root()
        files = []
        for path in sorted(root.glob("*.mid"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = path.stat()
            files.append(
                {
                    "filename": path.name,
                    "uri": f"midi://{path.name}",
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
            )
        return files
