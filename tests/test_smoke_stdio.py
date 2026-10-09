"""CI smoke test: run the real server over stdio and use it like an MCP client would.

This is the CI-friendly equivalent of the MCP Inspector: the server is started as a subprocess
with the stdio transport, initialized, asked for its tools and used to create a file.
"""

from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_stdio_smoke(tmp_path):
    output_dir = tmp_path / "out"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "chordsmith"],
        env={**os.environ, "CHORDSMITH_OUTPUT_DIR": str(output_dir)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "ChordSmith"

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "create_chord_progression",
                "create_progression_from_roman",
                "analyze_midi",
                "get_midi_file",
                "render_singing",
            } <= names

            created = await session.call_tool(
                "create_chord_progression",
                {"chords": ["Am", "F", "C", "G"], "filename": "smoke", "tempo": 120},
            )
            assert not created.isError
            assert (output_dir / "smoke.mid").is_file()

            listing = await session.call_tool("list_generated_files", {})
            filenames = [entry["filename"] for entry in listing.structuredContent["files"]]
            assert "smoke.mid" in filenames
