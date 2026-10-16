"""Output directory handling. Every file read or written stays inside the output folder.

Files live either directly in the output folder or inside one level of a **project** folder:
``"my_song/verse.mid"`` addresses (and creates) a file in the project ``my_song``. A small
``project.json`` manifest is written when a project folder is created, so several clients can
find the song they are working on. The ``states/`` folder is reserved for the plugin state
library and is never treated as a project.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_NAME_LENGTH = 100
MIDI_EXTENSIONS = (".mid", ".midi")
AUDIO_EXTENSIONS = (".wav", ".mp3")
MEDIA_EXTENSIONS = MIDI_EXTENSIONS + AUDIO_EXTENSIONS
PROJECT_MANIFEST = "project.json"
RESERVED_FOLDERS = {"states"}
# Names Windows refuses to create (devices); a file or project must not be called these.
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class StorageError(ValueError):
    pass


def default_output_dir() -> Path:
    env = os.environ.get("CHORDSMITH_OUTPUT_DIR")
    return Path(env).expanduser() if env else Path.home() / "ChordSmith"


def split_name(name: str) -> tuple[str | None, str]:
    """Split ``'project/file'`` into ``('project', 'file')``; a bare name has no project.

    Both segments are returned unstripped (the sanitizers run later); more than one folder
    level, empty segments and traversal attempts are rejected here.
    """
    text = name.replace("\\", "/")
    if "/" not in text:
        return None, text
    parts = text.split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise StorageError(
            f"'{name}' should be a file name or 'project/file' (one folder deep, e.g. 'my_song/verse.mid')."
        )
    return parts[0].strip(), parts[1].strip()


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
        if base.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise StorageError(f"'{base}' is a reserved name; pick another file name.")
        return base[: MAX_NAME_LENGTH - len(extension)] + extension

    @staticmethod
    def safe_project(name: str) -> str:
        """Sanitize a project folder name (no extension handling; 'states' is reserved)."""
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._-")
        if not slug:
            raise StorageError(f"'{name}' is not a usable project name.")
        if slug.lower() in RESERVED_FOLDERS:
            raise StorageError(f"'{slug}' is reserved for the server's own files; pick another project name.")
        if slug.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise StorageError(f"'{slug}' is a reserved name; pick another project name.")
        return slug[:MAX_NAME_LENGTH]

    def ensure_project(self, project: str) -> Path:
        """The project folder, created (with its ``project.json`` manifest) on first use."""
        root = self.ensure_root()
        directory = root / self.safe_project(project)
        created = not directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True)
        manifest = directory / PROJECT_MANIFEST
        if created or not manifest.is_file():
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            payload = {"name": directory.name, "created": stamp, "updated": stamp}
            manifest.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        return directory

    def read_manifest(self, project: str) -> dict:
        """The project's manifest (name/created/updated, plus any extra keys), or ``{}``."""
        try:
            manifest = self.ensure_root() / self.safe_project(project) / PROJECT_MANIFEST
            if not manifest.is_file():
                return {}
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, StorageError):
            return {}

    def _resolve_parts(self, project: str | None, filename: str, extension: str = ".mid") -> Path:
        root = self.ensure_root()
        if project is None:
            return root / self.safe_name(filename, extension)
        directory = (root / self.safe_project(project)).resolve()
        if directory.parent != root:
            raise StorageError("Projects must live directly inside the output folder.")
        return directory / self.safe_name(filename, extension)

    def _resolve(self, name: str, extension: str = ".mid") -> Path:
        project, filename = split_name(name)
        return self._resolve_parts(project, filename, extension)

    def project_of(self, path: Path) -> str | None:
        """The project a resolved path belongs to (None for files directly in the root)."""
        root = self.ensure_root()
        try:
            parts = path.resolve().relative_to(root).parts
        except ValueError:
            return None
        return parts[0] if len(parts) > 1 else None

    def project_of_name(self, name: str) -> str | None:
        """The project a store name ('project/file') names, without touching the disk."""
        project, _ = split_name(name)
        return self.safe_project(project) if project is not None else None

    def relative_name(self, path: Path) -> str:
        """The addressable name of a store path: 'file.ext' or 'project/file.ext'."""
        return path.resolve().relative_to(self.ensure_root()).as_posix()

    def new_path(
        self,
        name: str | None,
        default_stem: str,
        overwrite: bool = False,
        extension: str = ".mid",
        project: str | None = None,
    ) -> Path:
        """Reserve a free file name by creating a placeholder atomically.

        Two overlapping writers (for example two render jobs that finish minutes apart) can never
        claim the same name: the claim uses O_CREAT|O_EXCL, so the second one moves on to ``_2``.
        A name may carry a project (``"my_song/verse"``); otherwise ``project`` places the file
        (derived outputs stay in their source's project).
        """
        if name:
            name_project, stem = split_name(name)
            if name_project is not None:
                project = name_project
        else:
            stem = default_stem
        if project is not None:
            directory = self.ensure_project(project)
        else:
            directory = self.ensure_root()
        path = directory / self.safe_name(stem, extension)
        if overwrite:
            return path
        base = path.stem
        candidates = [path] + [path.with_name(f"{base}_{i}{extension}") for i in range(2, 1000)]
        for candidate in candidates:
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(descriptor)
            return candidate
        raise StorageError(f"Too many files named like '{path.name}'.")

    @contextlib.contextmanager
    def claimed_path(
        self,
        name: str | None,
        default_stem: str,
        overwrite: bool = False,
        extension: str = ".mid",
        project: str | None = None,
    ):
        """Claim a name and remove the placeholder again if writing the file fails."""
        path = self.new_path(name, default_stem, overwrite, extension, project)
        try:
            yield path
        except BaseException:
            if not overwrite:
                path.unlink(missing_ok=True)
            raise

    def existing_path(self, name: str) -> Path:
        path = self._resolve(name)
        if not path.is_file():
            midi = [entry["filename"] for entry in self.list_files() if entry["kind"] == "midi"][:20]
            available = ", ".join(midi) or "none yet"
            raise StorageError(
                f"File '{name}' not found. MIDI files: {available}. (Audio files are served by get_file.)"
            )
        return path

    def existing_media_path(self, name: str) -> Path:
        """Resolve a file that may be MIDI or rendered audio (used by the download route)."""
        project, filename = split_name(name)
        extension = next((ext for ext in MEDIA_EXTENSIONS if filename.lower().endswith(ext)), None)
        if extension is None:
            raise StorageError(f"'{name}' is not a .mid, .wav or .mp3 file.")
        path = self._resolve_parts(project, filename, extension)
        if not path.is_file():
            raise StorageError(f"File '{name}' not found.")
        return path

    def remove(self, name: str) -> Path:
        path = self.existing_path(name)
        path.unlink()
        return path

    def rename(self, name: str, new_name: str) -> Path:
        """Rename a file; a ``'project/newname'`` target also moves it to that project.

        Without a project in ``new_name`` the file keeps its folder.
        """
        source = self.existing_path(name)
        project, stem = split_name(new_name)
        if project is not None:
            directory = self.ensure_project(project)
        else:
            source_project = self.project_of(source)
            directory = self.ensure_root() / source_project if source_project else self.ensure_root()
        target = directory / self.safe_name(stem, source.suffix)
        if target.exists():
            raise StorageError(f"'{target.name}' already exists; pick another name.")
        source.rename(target)
        return target

    def _media_dirs(self) -> list[Path]:
        """The root plus every project folder (reserved folders excluded)."""
        root = self.ensure_root()
        directories = [root]
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name.lower() not in RESERVED_FOLDERS:
                directories.append(child)
        return directories

    def remove_empty_media(self) -> list[str]:
        """Delete 0-byte media files and return their names.

        These are crash leftovers: a job that claimed its output name and then died left a
        0-byte file that looks like a result. The sweep runs at server startup, when nothing can
        be writing (all writes land via a ``.part`` file and a rename, so live files are never
        seen empty).
        """
        removed: list[str] = []
        for directory in self._media_dirs():
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS and path.stat().st_size == 0:
                    path.unlink()
                    removed.append(self.relative_name(path))
        return removed

    def list_files(self, project: str | None = None) -> list[dict]:
        """Media files, newest first; each entry carries its ``project`` (None = output root).

        ``project='.'`` lists only files outside any project; a project name lists only that
        project; None lists everything.
        """
        if project == "":
            project = None
        wanted = None if project in (None, ".") else self.safe_project(project)
        directories: list[tuple[Path, str | None]] = []
        if project in (None, "."):
            directories.append((self.ensure_root(), None))
        if project != ".":
            for child in sorted(self.ensure_root().iterdir()):
                if not child.is_dir() or child.name.lower() in RESERVED_FOLDERS:
                    continue
                if project is None or child.name == wanted:
                    directories.append((child, child.name))
        files = []
        patterns = (("*.mid", "midi"), ("*.midi", "midi"), ("*.wav", "wav"), ("*.mp3", "mp3"))
        for directory, entry_project in directories:
            seen: set[str] = set()
            paths = []
            for pattern, _kind in patterns:
                for path in directory.glob(pattern):
                    if path.name not in seen:
                        seen.add(path.name)
                        paths.append(path)
            for path in paths:
                kind = {".wav": "wav", ".mp3": "mp3"}.get(path.suffix.lower(), "midi")
                stat = path.stat()
                name = path.name if entry_project is None else f"{entry_project}/{path.name}"
                files.append(
                    {
                        "filename": name,
                        "uri": f"{'audio' if kind != 'midi' else 'midi'}://{name}",
                        "kind": kind,
                        "project": entry_project,
                        "size_bytes": stat.st_size,
                        "empty": stat.st_size == 0,
                        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                    }
                )
        files.sort(key=lambda entry: str(entry["modified"]), reverse=True)
        return files

    def list_projects(self) -> list[dict]:
        """Every project folder with file counts and manifest metadata, newest activity first."""
        projects: dict[str, dict] = {}
        for entry in self.list_files():
            name = entry["project"]
            if name is None:
                continue
            info = projects.setdefault(
                name,
                {
                    "name": name,
                    "files": 0,
                    "midi": 0,
                    "audio": 0,
                    "last_modified": None,
                    "created": None,
                    "title": None,
                },
            )
            info["files"] += 1
            info["midi" if entry["kind"] == "midi" else "audio"] += 1
            if info["last_modified"] is None or entry["modified"] > info["last_modified"]:
                info["last_modified"] = entry["modified"]
        for child in sorted(self.ensure_root().iterdir()):
            if not child.is_dir() or child.name.lower() in RESERVED_FOLDERS:
                continue
            info = projects.setdefault(
                child.name,
                {
                    "name": child.name,
                    "files": 0,
                    "midi": 0,
                    "audio": 0,
                    "last_modified": None,
                    "created": None,
                    "title": None,
                },
            )
            manifest = self.read_manifest(child.name)
            info["created"] = manifest.get("created")
            if manifest.get("title"):
                info["title"] = manifest["title"]
        return sorted(projects.values(), key=lambda item: str(item["last_modified"] or ""), reverse=True)
