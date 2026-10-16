"""Headless plugin-state generation (generate_plugin_states) with a stubbed host call."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, renderers, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    plugin = tmp_path / "FakeSynth.vst3"
    plugin.mkdir()
    monkeypatch.setattr(renderers, "_resolve_plugin", lambda name: str(plugin))
    monkeypatch.setattr(renderers, "_plugin_exists", lambda path: True)
    monkeypatch.setattr(audio, "host_for_plugin", lambda path: "vst_host")
    monkeypatch.setattr(renderers, "_parameter_indices", lambda host, plugin_path: {"Drive": 3, "Tone": 7})
    return plugin


@pytest.fixture
def fake_dump(monkeypatch):
    calls: list[dict] = []

    def dump(host, plugin, target, sets=None, preset=None, state=None):
        calls.append({"target": Path(target), "sets": sets, "state": state})
        Path(target).write_bytes(b"STATE")
        return {"bytes": 5}

    monkeypatch.setattr(audio, "dump_vst_state", dump)
    return calls


async def test_generate_plugin_states_sweeps_the_grid(store, fake_plugin, fake_dump):
    result = await _call(
        "generate_plugin_states",
        {
            "plugin": str(fake_plugin),
            "parameters": {"Drive": [0, 6], "Tone": ["Bright", "Warm"]},
            "name_prefix": "tone",
            "make_default": True,
        },
    )
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [entry["name"] for entry in data["states"]] == ["tone_1", "tone_2", "tone_3", "tone_4"]
    assert data["defaults"][next(iter(data["defaults"]))] == "tone_1"
    assert fake_dump[0]["sets"] == [(3, 0.0), (7, "Bright")]
    assert fake_dump[-1]["sets"] == [(3, 6.0), (7, "Warm")]
    assert all(call["state"] is None for call in fake_dump)
    assert all(call["target"].is_file() for call in fake_dump)


async def test_generate_plugin_states_uses_a_base_state(store, fake_plugin, fake_dump):
    await _call(
        "generate_plugin_states",
        {
            "plugin": str(fake_plugin),
            "parameters": {"Drive": [3]},
            "base_state": None,
        },
    )
    # make a captured state to start from, then sweep from it
    (store.ensure_root() / "states").mkdir(parents=True, exist_ok=True)
    (store.ensure_root() / "states" / "captured.state").write_bytes(b"BASE")
    (store.ensure_root() / "states" / "captured.json").write_text(
        '{"name": "captured", "plugin": "' + str(fake_plugin).replace("\\", "\\\\") + '"}',
        encoding="utf-8",
    )
    result = await _call(
        "generate_plugin_states",
        {
            "plugin": str(fake_plugin),
            "parameters": {"Drive": [1, 9]},
            "base_state": "captured",
            "name_prefix": "fromcaptured",
        },
    )
    assert not result.isError, result.content[0].text
    assert all(call["state"] and call["state"].endswith("captured.state") for call in fake_dump[1:])


async def test_generate_plugin_states_rejects_wide_grids_and_bad_names(store, fake_plugin, fake_dump):
    wide = await _call(
        "generate_plugin_states",
        {"plugin": str(fake_plugin), "parameters": {"Drive": list(range(40))}},
    )
    assert wide.isError and "limit is 32" in wide.content[0].text
    unknown = await _call(
        "generate_plugin_states",
        {"plugin": str(fake_plugin), "parameters": {"Nope": [1]}},
    )
    assert unknown.isError and "no parameter named 'Nope'" in unknown.content[0].text
    empty = await _call("generate_plugin_states", {"plugin": str(fake_plugin), "parameters": {}})
    assert empty.isError and "at least one parameter" in empty.content[0].text
