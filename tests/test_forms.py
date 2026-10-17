"""Song-form templates: named section maps via create_empty_file/set_song_sections."""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import forms, sections, server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def test_every_form_has_unique_named_sections():
    for name, song_sections in forms.SONG_FORMS.items():
        assert song_sections, name
        names = [section.name for section in song_sections]
        assert len(set(names)) == len(names), name
        assert all(section.bars >= 1 and section.bpm >= 20 for section in song_sections), name


def test_researched_bar_counts():
    pop = forms.SONG_FORMS["pop"]
    assert sum(section.bars for section in pop) == 64 and pop[0].name == "Intro"
    jazz = forms.SONG_FORMS["jazz"]
    assert [section.bars for section in jazz] == [8, 8, 8, 8]  # 32-bar AABA
    blues = forms.SONG_FORMS["blues"]
    assert [section.bars for section in blues] == [12, 12, 12]
    metal = forms.SONG_FORMS["metal"]
    assert metal[1].name == "Riff" and any(section.name == "Breakdown" for section in metal)
    assert {section.bpm for section in metal} == {150}
    cinematic = forms.SONG_FORMS["cinematic"]
    assert cinematic[-2].name == "Climax" and cinematic[-2].bpm == 120  # builds to the climax
    assert cinematic[-1].bpm < cinematic[-2].bpm


def test_bpm_override_flattens_the_template():
    flat = forms.form_sections("cinematic", bpm=90)
    assert {section.bpm for section in flat} == {90}
    assert forms.form_sections("pop")[0].bpm == 110
    with pytest.raises(ValueError, match="Unknown song form"):
        forms.form_sections("opera")


async def test_create_empty_file_with_a_form(store):
    result = await _call("create_empty_file", {"filename": "hit", "form": "pop"})
    assert not result.isError, result.content[0].text
    names = [section["name"] for section in result.structuredContent["sections"]]
    assert names == [
        "Intro",
        "Verse 1",
        "Pre-Chorus 1",
        "Chorus 1",
        "Verse 2",
        "Pre-Chorus 2",
        "Chorus 2",
        "Bridge",
        "Chorus 3",
        "Outro",
    ]
    resolved = sections.read_sections(store.root / "hit.mid")
    assert resolved[3].name == "Chorus 1" and resolved[3].bpm == 110


async def test_set_song_sections_form_with_bpm(store):
    await _call("create_chord_progression", {"chords": ["Am", "F", "C", "G"], "filename": "song"})
    result = await _call("set_song_sections", {"filename": "song.mid", "form": "blues", "bpm": 90})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [section["bars"] for section in data["sections"]] == [12, 12, 12]
    assert {section["bpm"] for section in data["sections"]} == {90}
    assert data["total_beats"] == 144


async def test_section_relative_notes_use_form_names(store):
    await _call("create_empty_file", {"filename": "structured", "form": "pop"})
    added = await _call(
        "add_track",
        {
            "filename": "structured.mid",
            "track_name": "Melody",
            "notes": [{"pitch": "A4", "section": "Chorus 1", "bar": 1, "beat": 1, "beats": 1}],
        },
    )
    assert not added.isError, added.content[0].text


async def test_forms_and_explicit_sections_are_exclusive(store):
    await _call("create_empty_file", {"filename": "x"})
    both = await _call(
        "set_song_sections",
        {
            "filename": "x.mid",
            "form": "pop",
            "sections": [{"name": "A", "bars": 1, "bpm": 100}],
        },
    )
    assert both.isError and "not both" in both.content[0].text
    neither = await _call("set_song_sections", {"filename": "x.mid"})
    assert neither.isError and "section list or a form" in neither.content[0].text
    bpm_only = await _call(
        "set_song_sections",
        {"filename": "x.mid", "sections": [{"name": "A", "bars": 1, "bpm": 100}], "bpm": 90},
    )
    assert bpm_only.isError and "only applies with a form" in bpm_only.content[0].text
