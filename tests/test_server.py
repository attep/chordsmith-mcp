"""End-to-end tests through a real MCP client session (in memory)."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


async def test_create_chord_progression(store):
    result = await _call(
        "create_chord_progression",
        {
            "chords": ["Am", "F", {"chord": "C", "beats": 2}, {"chord": "G", "beats": 2}],
            "filename": "melancholy",
            "tempo": 90,
            "voicing": {"voice_leading": True, "add_bass": True},
            "rhythm": {"pattern": "arpeggio_up"},
        },
    )
    assert not result.isError
    data = result.structuredContent
    assert data["filename"] == "melancholy.mid"
    assert data["total_beats"] == 12
    assert (store.root / "melancholy.mid").is_file()


async def test_roman_tool(store):
    result = await _call("create_progression_from_roman", {"numerals": "i–VI–III–VII", "key": "A minor"})
    assert not result.isError
    assert result.structuredContent["resolved_chords"] == ["Am", "F", "C", "G"]


async def test_bad_input_is_a_clear_error(store):
    result = await _call("create_chord_progression", {"chords": ["Am", "Hmaj"]})
    assert result.isError
    assert "Hmaj" in result.content[0].text


async def test_transpose_analyze_and_list(store):
    await _call("create_chord_progression", {"chords": ["Am", "F", "C", "G"], "filename": "p"})
    t = await _call("transpose_midi", {"filename": "p.mid", "from_key": "A minor", "to_key": "C minor"})
    assert t.structuredContent["semitones"] == 3
    a = await _call("analyze_midi", {"filename": t.structuredContent["filename"]})
    assert a.structuredContent["progression"] == ["Cm", "Ab", "Eb", "Bb"]
    files = (await _call("list_generated_files", {})).structuredContent["files"]
    assert {f["filename"] for f in files} == {"p.mid", "p_up3.mid"}


async def test_list_chord_types():
    result = await _call("list_chord_types", {})
    names = {row["quality"] for row in result.structuredContent["result"]}
    assert {"maj", "min", "7", "maj7", "dim", "sus4", "add9"} <= names


async def test_resources_and_prompts(store):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        types = await client.read_resource("chords://types")
        assert "maj7" in types.contents[0].text
        scale = json.loads((await client.read_resource("scales://A-minor")).contents[0].text)
        assert [c["chord"] for c in scale["triads"]] == ["Am", "Bdim", "C", "Dm", "Em", "F", "G"]
        await client.call_tool("create_chord_progression", {"chords": ["C"], "filename": "one"})
        blob = await client.read_resource("midi://one.mid")
        assert blob.contents[0].mimeType == "audio/midi"
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        assert prompts == {"compose_progression", "explain_progression"}
        prompt = await client.get_prompt("compose_progression", {"mood": "melancholic"})
        assert "scales://" in prompt.messages[0].content.text
