"""Tests for the remote VST bridge: the server routes VST jobs to a bridge worker.

A tiny fake bridge stands in for scripts/vst_bridge.py, so these run anywhere (no Windows,
no vst_host): they prove the routing, the upload/download of job files, the plugin scan, and
that the editor tool opens the window on the bridge's machine instead of demanding a local
desktop.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, server

pytestmark = pytest.mark.anyio

TOKEN = "test-token"
DRUM = r"C:\Plugins\MODO DRUM.vst3"


class _FakeBridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("X-Chordsmith-Token") == TOKEN

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/healthz":
            self._json(200, {"ok": True})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/scan"):
            self._json(200, {"plugins": [{"name": "MODO DRUM", "path": DRUM}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/run":
            self._json(200, {"exit": 0, "stdout": '{"ok":true,"mode":"list","plugin":"X"}', "stderr": ""})
        elif self.path == "/job":
            outputs = {}
            args = payload.get("args") or []
            for index in range(1, len(args)):
                if args[index - 1] == "--wav":
                    outputs[args[index]] = base64.b64encode(b"RIFF").decode()
            self._json(
                200, {"exit": 0, "stdout": '{"ok":true,"mode":"render"}', "stderr": "", "outputs": outputs}
            )
        elif self.path == "/exists":
            paths = payload.get("paths") or []
            self._json(200, {"exists": {path: path.lower().endswith(".vst3") for path in paths}})
        elif self.path == "/editor":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_bridge(monkeypatch):
    bridge = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBridgeHandler)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{bridge.server_address[1]}"
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_URL", url)
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_VST_DIR", r"C:\Plugins")
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_SHARED_FROM", "/data")
    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_SHARED_TO", r"D:\ChordSmith\data")
    yield url
    bridge.shutdown()
    bridge.server_close()


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def test_bridge_routing_and_file_transfer(fake_bridge, tmp_path, monkeypatch):
    assert audio.bridge_url() == fake_bridge
    assert audio.find_vst_host() == fake_bridge
    assert audio.bridge_plugin_scan() == [{"name": "MODO DRUM", "path": DRUM}]
    assert audio.bridge_paths_exist([DRUM]) == {DRUM: True}
    assert audio.bridge_shared_path("/data/states/x.state") == r"D:\ChordSmith\data\states\x.state"

    # Routing is per plugin: Windows paths go to the bridge, local Linux paths stay here.
    assert audio.host_for_plugin(DRUM) == fake_bridge
    monkeypatch.setenv("CHORDSMITH_VST_HOST", "local-host")
    assert audio.host_for_plugin("/vst/Local.vst3") == "local-host"
    assert audio.host_for_plugin(DRUM) == fake_bridge

    payload = audio._run_vst_host(fake_bridge, ["--list", "--plugin", "X.vst3"])
    assert payload["ok"] is True and payload["mode"] == "list"

    source = tmp_path / "stem.mid"
    source.write_bytes(b"MThd")
    target = tmp_path / "out.wav"
    payload = audio.render_vst(fake_bridge, DRUM, None, None, source, target)
    assert payload["ok"] is True
    assert target.read_bytes() == b"RIFF"  # the bridge's output landed at the server's path

    monkeypatch.setenv("CHORDSMITH_VST_BRIDGE_TOKEN", "wrong")
    with pytest.raises(audio.AudioError):
        audio.bridge_plugin_scan()


async def test_bridge_tools_see_both_worlds(store, monkeypatch, fake_bridge, tmp_path):
    local_dir = tmp_path / "vst"
    local_dir.mkdir()
    (local_dir / "Local.vst3").write_bytes(b"x")
    monkeypatch.setenv("CHORDSMITH_VST_DIR", str(local_dir))

    listed = (await _call("list_plugins", {})).structuredContent
    assert {entry["name"] for entry in listed["plugins"]} == {"Local", "MODO DRUM"}
    assert listed["directory"] == f"{local_dir} + C:\\Plugins"

    filtered = (await _call("list_plugins", {"name_contains": "modo"})).structuredContent
    assert {entry["name"] for entry in filtered["plugins"]} == {"MODO DRUM"}

    detail = await _call("list_plugins", {"detail": DRUM})
    assert not detail.isError, detail.content[0].text

    editor = await _call("open_plugin_state_editor", {"plugin": "MODO DRUM", "state_name": "demo"})
    assert not editor.isError, editor.content[0].text
    body = editor.structuredContent
    assert body["started"] is True
    assert body["state_path"].endswith("demo.state")


async def test_bridge_detail_uses_the_per_plugin_host(store, monkeypatch, fake_bridge, tmp_path):
    local_dir = tmp_path / "vst"
    local_dir.mkdir()
    (local_dir / "Local.vst3").write_bytes(b"x")
    monkeypatch.setenv("CHORDSMITH_VST_DIR", str(local_dir))
    monkeypatch.setenv("CHORDSMITH_VST_HOST", "/fake/local-host")
    hosts: list[str] = []

    def fake_info(host, target):
        hosts.append(host)
        return {"types": [{"name": "Local", "programs": [], "is_instrument": True}]}

    def fake_params(host, target):
        hosts.append(host)
        return {"parameters": []}

    monkeypatch.setattr(audio, "vst_plugin_info", fake_info)
    monkeypatch.setattr(audio, "vst_plugin_params", fake_params)

    local = await _call("list_plugins", {"detail": str(local_dir / "Local.vst3"), "parameters": True})
    assert not local.isError, local.content[0].text
    assert hosts == ["/fake/local-host"]

    hosts.clear()
    bridged = await _call("list_plugins", {"detail": DRUM, "parameters": True})
    assert not bridged.isError, bridged.content[0].text
    assert hosts == [fake_bridge]
