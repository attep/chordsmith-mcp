"""ChordSmith VST bridge — run vst_host jobs for a remote ChordSmith server (Windows worker).

The ChordSmith server normally spawns vst_host itself. With this bridge running on a Windows
machine (which has the Windows-only plugins), a server on another machine (e.g. the Linux
Docker container) can route every VST operation here: plugin scans, renders, state loads and
saves, and editor windows. The server talks to it with CHORDSMITH_VST_BRIDGE_URL.

    python scripts/vst_bridge.py --token <secret> [--port 18002] [--host-exe <vst_host.exe>]

Endpoints (all but /healthz need the X-Chordsmith-Token header):
    GET  /healthz                    -> {"ok": true}
    GET  /scan?dir=<dir>             -> {"plugins": [{"name", "path"}, ...]}
    POST /run    {"args": [...]}     -> {"exit", "stdout", "stderr"}
    POST /job    {"args": [...], "files": {path: base64}}
                                     -> {"exit", "stdout", "stderr", "outputs": {path: base64}}
        File arguments (--midi/--in/--state in; --wav/--out/--save-state/--dump-state out)
        are rewritten to a private temp dir, so the server can be on another filesystem.
    POST /exists {"paths": [...]}    -> {"exists": {path: bool}}
    POST /editor {"args": [...]}     -> {"ok": true} (spawns vst_host detached; the window
                                        opens on this machine's desktop)
    POST /tab    {"name": "riff.gpx", "data": <base64>}
                                     -> {"exit", "midi": <base64>, "stdout", "stderr"}
        Converts a Guitar Pro (.gp3-.gp5/.gp/.gpx) or MusicXML/MSCZ tab to MIDI with
        MuseScore. Needs --musescore <MuseScore4.exe>; without it the endpoint reports
        that no converter is configured.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FILE_INPUT_FLAGS = {"--midi", "--in", "--state"}
FILE_OUTPUT_FLAGS = {"--wav", "--out", "--save-state", "--dump-state"}
FILE_FLAGS = FILE_INPUT_FLAGS | FILE_OUTPUT_FLAGS
MAX_BODY = 512 * 1024 * 1024
HOST_TIMEOUT_SECONDS = 1800
TAB_TIMEOUT_SECONDS = 180
MAX_TAB_BYTES = 16 * 1024 * 1024
MAX_SCAN = 500


def scan_plugins(directory: str | None, limit: int = MAX_SCAN) -> list[dict[str, str]]:
    """The .vst3 plugins under a directory; a bundle is listed, not the file inside it."""
    if not directory:
        return []
    root = Path(directory)
    if not root.is_dir():
        return []
    kept: list[Path] = []
    for path in sorted(root.rglob("*.vst3")):
        if any(entry in path.parents for entry in kept):
            continue
        kept.append(path)
        if len(kept) >= limit:
            break
    return [{"name": path.stem, "path": str(path)} for path in kept]


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "ChordSmithVSTBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # keep the console quiet
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def finish(self) -> None:
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def _authorized(self) -> bool:
        token = self.server.token
        return not token or self.headers.get("X-Chordsmith-Token") == token

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/healthz":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        url = urlparse(self.path)
        if url.path == "/scan":
            directory = (parse_qs(url.query).get("dir") or [""])[0]
            self._send(200, {"plugins": scan_plugins(directory)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send(400, {"error": "missing or oversized body"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send(400, {"error": "body is not JSON"})
            return
        route = urlparse(self.path).path
        if route == "/run":
            self._send(200, self._run(payload.get("args") or [], {}))
        elif route == "/job":
            self._send(200, self._run(payload.get("args") or [], payload.get("files") or {}))
        elif route == "/exists":
            paths = [str(entry) for entry in payload.get("paths") or []]
            self._send(200, {"exists": {path: os.path.exists(path) for path in paths}})
        elif route == "/editor":
            self._send(200, self._spawn_editor([str(item) for item in payload.get("args") or []]))
        elif route == "/tab":
            self._send(200, self._convert_tab(payload))
        else:
            self._send(404, {"error": "not found"})

    def _run(self, args: list[str], files: dict[str, str]) -> dict:
        scratch = tempfile.mkdtemp(prefix="cs-bridge-")
        try:
            rewritten = [str(item) for item in args]
            wait = os.environ.get("CHORDSMITH_VST_LOAD_WAIT")
            if wait and "--render" in rewritten and "--load-wait" not in rewritten:
                rewritten += ["--load-wait", wait]
            rewritten_map: dict[str, str] = {}  # temp path -> the path the caller gave us
            for index in range(1, len(rewritten)):
                if rewritten[index - 1] not in FILE_FLAGS:
                    continue
                original = rewritten[index]
                temp = os.path.join(
                    scratch, f"{index}.{'mid' if rewritten[index - 1] == '--midi' else 'wav'}"
                )
                upload = files.get(original)
                if upload is not None:
                    Path(temp).write_bytes(base64.b64decode(upload))
                rewritten_map[temp] = original
                rewritten[index] = temp
            try:
                process = subprocess.run(
                    [self.server.host_exe, *rewritten],
                    capture_output=True,
                    text=True,
                    timeout=HOST_TIMEOUT_SECONDS,
                    cwd=self.server.workdir,
                )
                result = {"exit": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
            except subprocess.TimeoutExpired:
                return {
                    "exit": -1,
                    "stdout": "",
                    "stderr": f"vst_host timed out after {HOST_TIMEOUT_SECONDS}s",
                    "outputs": {},
                }
            except OSError as exc:
                return {
                    "exit": -1,
                    "stdout": "",
                    "stderr": f"could not run {self.server.host_exe}: {exc}",
                    "outputs": {},
                }
            outputs = {}
            for index in range(1, len(rewritten)):
                if rewritten[index - 1] not in FILE_OUTPUT_FLAGS:
                    continue
                temp = rewritten[index]
                if os.path.isfile(temp):
                    outputs[rewritten_map.get(temp, temp)] = base64.b64encode(
                        Path(temp).read_bytes()
                    ).decode()
            result["outputs"] = outputs
            return result
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _spawn_editor(self, args: list[str]) -> dict:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                [self.server.host_exe, *args],
                creationflags=flags,
                close_fds=True,
                cwd=self.server.workdir,
            )
        except OSError as exc:
            return {"ok": False, "error": f"could not run {self.server.host_exe}: {exc}"}
        return {"ok": True}

    def _convert_tab(self, payload: dict) -> dict:
        """Convert a tab to MIDI with MuseScore (--musescore); never touches the caller's disk."""
        musescore = self.server.musescore
        if not musescore:
            return {
                "exit": -1,
                "midi": None,
                "error": "no MuseScore configured on this bridge; restart it with --musescore <path>",
                "stdout": "",
                "stderr": "",
            }
        name = str(payload.get("name") or "tab.gpx")
        suffix = Path(name).suffix.lower() or ".gp"
        try:
            data = base64.b64decode(str(payload.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return {"exit": -1, "midi": None, "error": "data is not valid base64", "stdout": "", "stderr": ""}
        if not data or len(data) > MAX_TAB_BYTES:
            return {
                "exit": -1,
                "midi": None,
                "error": f"empty or oversized tab ({len(data)} bytes; limit {MAX_TAB_BYTES})",
                "stdout": "",
                "stderr": "",
            }
        scratch = tempfile.mkdtemp(prefix="cs-tab-")
        try:
            source = Path(scratch) / f"input{suffix}"
            target = Path(scratch) / "output.mid"
            source.write_bytes(data)
            try:
                process = subprocess.run(
                    [musescore, "-o", str(target), str(source)],
                    capture_output=True,
                    text=True,
                    timeout=TAB_TIMEOUT_SECONDS,
                    cwd=scratch,
                )
            except subprocess.TimeoutExpired:
                return {
                    "exit": -1,
                    "midi": None,
                    "error": f"MuseScore timed out after {TAB_TIMEOUT_SECONDS}s",
                    "stdout": "",
                    "stderr": "",
                }
            except OSError as exc:
                return {
                    "exit": -1,
                    "midi": None,
                    "error": f"could not run MuseScore ({musescore}): {exc}",
                    "stdout": "",
                    "stderr": "",
                }
            stdout = process.stdout[-2000:]
            stderr = process.stderr[-2000:]
            if not target.is_file():
                return {
                    "exit": process.returncode,
                    "midi": None,
                    "error": "MuseScore produced no MIDI (see stderr)",
                    "stdout": stdout,
                    "stderr": stderr,
                }
            return {
                "exit": process.returncode,
                "midi": base64.b64encode(target.read_bytes()).decode(),
                "stdout": stdout,
                "stderr": stderr,
            }
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="ChordSmith VST bridge (Windows worker)")
    parser.add_argument(
        "--host-exe", default=os.environ.get("CHORDSMITH_VST_HOST", r"D:\ChordSmith\bin\vst_host.exe")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CHORDSMITH_VST_BRIDGE_PORT", "18002"))
    )
    parser.add_argument("--bind", default=os.environ.get("CHORDSMITH_VST_BRIDGE_BIND", "0.0.0.0"))
    parser.add_argument("--token", default=os.environ.get("CHORDSMITH_VST_BRIDGE_TOKEN", ""))
    parser.add_argument(
        "--musescore",
        default=os.environ.get("CHORDSMITH_MUSESCORE", ""),
        help="Optional MuseScore executable for /tab conversion (Guitar Pro/MusicXML -> MIDI).",
    )
    args = parser.parse_args()
    if not args.token:
        parser.error("--token (or CHORDSMITH_VST_BRIDGE_TOKEN) is required; the bridge runs plugins")
    if not os.path.isfile(args.host_exe):
        parser.error(f"vst_host not found at {args.host_exe} (pass --host-exe)")
    server = ThreadingHTTPServer((args.bind, args.port), BridgeHandler)
    server.token = args.token
    server.host_exe = args.host_exe
    server.musescore = args.musescore or None
    server.workdir = str(Path(args.host_exe).parent)
    print(f"vst_bridge on http://{args.bind}:{args.port} -> {args.host_exe}", flush=True)
    if server.musescore:
        print(f"tab conversion (POST /tab) via {server.musescore}", flush=True)
    else:
        print("tab conversion disabled (pass --musescore to enable POST /tab)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
