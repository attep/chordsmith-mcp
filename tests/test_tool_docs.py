"""Guardrails for the agent-facing tool documentation.

Tool-only agents see nothing but names, titles, descriptions and schemas, so these tests treat
them as a product surface: every tool must carry a title, MCP behaviour annotations, a
description that says what comes back, an input schema where every property is described and
defaulted, and a machine-checkable output schema. The total description prose is budgeted so
tools/list stays cheap to load.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import server

pytestmark = pytest.mark.anyio

DESCRIPTION_BUDGET_BYTES = 22 * 1024
# Deliberately raised from 72 KB (v0.11.0: verification/diagnostics) to 96 KB (v0.14.0:
# effects, import, sections), 108 KB (v0.16.0: background render jobs), 116 KB (v0.17.0: wet
# mix + effect sends), 128 KB (v0.18.0: job listings, get_file, import_audio), 136 KB
# (v0.19.0: create_empty_file, remove_track, automation, tuplets), 148 KB (v0.20.0: vocal
# chains, bus, returns, master; the description budget moved 17 -> 19 KB in the same patch),
# 152 KB (v0.21.0: voicebank soft controls, air layer, de-esser/saturation enablement),
# 160 KB (v0.22.0: swing/humanize on add_track, the sfizz engine, SFZ libraries), 164 KB
# (v0.23.0: the VST engine, per-track effect chains, list_plugins) and 232 KB (v0.24.0:
# instrument chains + copy_from layers, and the mix job tool — MCP schemas are per-tool, so the
# big mix schema is duplicated by design; descriptions moved 20 -> 22 KB in the same patch).
# The surface grows with capabilities; keep new schemas lean.
PAYLOAD_CEILING_BYTES = 232 * 1024


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _tools():
    return await server.mcp.list_tools()


async def test_every_tool_has_a_unique_title_and_annotations():
    tools = await _tools()
    assert len(tools) == 36
    titles = [tool.title for tool in tools]
    assert all(titles), f"tools without a title: {[t.name for t in tools if not t.title]}"
    assert len(set(titles)) == len(titles), f"duplicate titles: {titles}"
    for tool in tools:
        annotations = tool.annotations
        assert annotations is not None, f"{tool.name} has no annotations"
        assert annotations.readOnlyHint is not None, f"{tool.name} misses readOnlyHint"
        assert annotations.destructiveHint is not None, f"{tool.name} misses destructiveHint"
        assert annotations.idempotentHint is not None, f"{tool.name} misses idempotentHint"
        assert annotations.openWorldHint is False, f"{tool.name} should not be open-world"
        if annotations.destructiveHint:
            assert annotations.readOnlyHint is False, f"{tool.name} cannot be destructive and read-only"
        if annotations.readOnlyHint:
            assert annotations.destructiveHint is False


async def test_descriptions_say_what_comes_back():
    for tool in await _tools():
        description = tool.description or ""
        assert len(description) >= 80, f"{tool.name} description is too thin"
        assert re.search(r"\breturns?\b|\breports?\b", description, re.I), (
            f"{tool.name} description never says what it returns"
        )


async def test_descriptions_within_budget():
    total = sum(len(tool.description or "") for tool in await _tools())
    assert total <= DESCRIPTION_BUDGET_BYTES, (
        f"tool descriptions grew to {total} bytes; trim prose or raise the budget deliberately"
    )


async def test_payload_stays_compact():
    payload = [tool.model_dump(mode="json", exclude_none=True) for tool in await _tools()]
    size = len(json.dumps(payload))
    assert size <= PAYLOAD_CEILING_BYTES, f"tools/list payload grew to {size} bytes"


async def test_every_input_property_is_described_and_defaulted():
    for tool in await _tools():
        schema = tool.inputSchema
        required = set(schema.get("required", []))
        for name, prop in schema.get("properties", {}).items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
            if name not in required:
                assert "default" in prop, f"{tool.name}.{name} is optional but has no default"


async def test_every_tool_has_an_object_output_schema():
    for tool in await _tools():
        schema = tool.outputSchema
        assert schema, f"{tool.name} has no outputSchema"
        assert schema.get("type") == "object", f"{tool.name} outputSchema is not an object"
        assert schema.get("properties"), f"{tool.name} outputSchema has no properties"


async def test_heavy_tools_do_not_block_the_event_loop():
    # FastMCP calls sync tool bodies on the event loop; a long render would freeze every other
    # request (status checks, list calls) for its whole duration, so these must be offloaded.
    manager = server.mcp._tool_manager
    for name in ("render_audio", "mix_song_with_vocals"):
        tool = manager.get_tool(name)
        assert tool is not None and inspect.iscoroutinefunction(tool.fn), f"{name} would block the server"


async def test_structured_content_matches_the_schema(store):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        chord_types = await client.call_tool("list_chord_types", {})
        files = await client.call_tool("list_generated_files", {})
    assert not chord_types.isError
    assert not files.isError
    assert isinstance(chord_types.structuredContent["result"], list)
    assert isinstance(files.structuredContent["files"], list)
    assert "output_dir" in files.structuredContent
