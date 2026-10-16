"""Project folders: files grouped per song and addressed as 'project/name'."""

from __future__ import annotations

import json
import wave
from urllib.parse import urlsplit

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import delivery, renderers, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _write_tiny_wav(path, amplitude=1000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(amplitude.to_bytes(2, "little") * 4410)


async def test_create_add_and_list_in_a_project(store):
    created = await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "my_song/verse", "tempo": 120},
    )
    assert not created.isError, created.content[0].text
    data = created.structuredContent
    assert data["filename"] == "my_song/verse.mid"
    assert data["uri"] == "midi://my_song/verse.mid"
    assert (store.root / "my_song" / "verse.mid").is_file()
    manifest = json.loads((store.root / "my_song" / "project.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "my_song" and manifest["created"]

    added = await _call(
        "add_track",
        {
            "filename": "my_song/verse.mid",
            "track_name": "Bass",
            "notes": [{"pitch": "A2", "start_beat": 0, "beats": 1.0}],
        },
    )
    assert not added.isError, added.content[0].text
    assert added.structuredContent["source"] == "my_song/verse.mid"
    assert added.structuredContent["filename"] == "my_song/verse_Bass.mid"
    assert (store.root / "my_song" / "verse_Bass.mid").is_file()

    listing = (await _call("list_generated_files", {})).structuredContent
    assert {entry["project"] for entry in listing["files"]} == {"my_song"}
    assert [project["name"] for project in listing["projects"]] == ["my_song"]
    assert listing["projects"][0]["files"] == 2 and listing["projects"][0]["midi"] == 2

    filtered = (await _call("list_generated_files", {"project": "my_song"})).structuredContent
    assert filtered["total"] == 2
    root_only = (await _call("list_generated_files", {"project": "."})).structuredContent
    assert root_only["total"] == 0


async def test_render_stems_stay_in_the_project(store, monkeypatch):
    created = await _call("create_chord_progression", {"chords": ["Am", "F"], "filename": "song/backing"})
    song = created.structuredContent["filename"]
    await _call(
        "add_track",
        {
            "filename": song,
            "track_name": "Bass",
            "notes": [{"pitch": "A2", "start_beat": 0, "beats": 1.0}],
        },
    )

    class FakeRenderer:
        name = "fluidsynth"

        def available(self):
            return True

        def render(self, midi, spec, out, effects=None):
            _write_tiny_wav(out)

    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", FakeRenderer())
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target))
    result = await _call("render_audio", {"filename": "song/backing_Bass.mid", "stems": True})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["filename"] == "song/backing_Bass.wav"
    assert data["source"] == "song/backing_Bass.mid"
    assert (store.root / "song" / "backing_Bass.wav").is_file()
    stem_names = {stem["filename"] for stem in data["stems"]}
    assert stem_names == {"song/backing_Bass_Chords.wav", "song/backing_Bass_Bass.wav"}
    assert all((store.root / "song" / name.split("/", 1)[1]).is_file() for name in stem_names)


async def test_sidecars_follow_project_moves(store, monkeypatch):
    class FakeRenderer:
        name = "fluidsynth"
        notes = "test renderer"

        def available(self):
            return True

        def render(self, midi, spec, out, effects=None):
            _write_tiny_wav(out)

    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", FakeRenderer())
    await _call("create_chord_progression", {"chords": ["C", "G"], "filename": "one/song"})
    instrument = await _call(
        "set_track_instrument",
        {"filename": "one/song.mid", "tracks": {"Chords": {"gain_db": -2.0}}},
    )
    assert not instrument.isError, instrument.content[0].text
    assert instrument.structuredContent["filename"] == "one/song.mid"

    renamed = await _call("rename_midi_file", {"filename": "one/song.mid", "new_name": "two/song2"})
    assert renamed.structuredContent["filename"] == "two/song2.mid"
    assert (store.root / "two" / "song2.instruments.json").is_file()
    assert not (store.root / "one" / "song.instruments.json").exists()

    listed_result = await _call("list_instruments", {"filename": "two/song2.mid"})
    assert not listed_result.isError, listed_result.content[0].text
    listed = listed_result.structuredContent
    assert listed["file"]["filename"] == "two/song2.mid"
    assert listed["file"]["specs"]["Chords"]["gain_db"] == -2.0

    deleted = await _call("delete_midi_file", {"filename": "two/song2.mid"})
    assert deleted.structuredContent["deleted"] == "two/song2.mid"
    assert not (store.root / "two" / "song2.instruments.json").exists()


async def test_download_route_serves_project_files(store, monkeypatch):
    monkeypatch.setattr(delivery, "_secret", b"project-test")
    monkeypatch.setenv("CHORDSMITH_PUBLIC_URL", "https://example.test")
    await _call("create_chord_progression", {"chords": ["C", "G"], "filename": "proj/song"})
    delivered = await _call("get_midi_file", {"filename": "proj/song.mid", "return_as": "url"})
    url = delivered.structuredContent["download_url"]
    assert "/files/proj/song.mid" in url  # the project folder stays in the signed path
    parts = urlsplit(url)
    transport = httpx.ASGITransport(app=server.mcp.streamable_http_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.get(f"{parts.path}?{parts.query}")
    assert response.status_code == 200
    assert response.content == (store.root / "proj" / "song.mid").read_bytes()
