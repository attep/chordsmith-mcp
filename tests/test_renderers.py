"""Tests for per-track rendering: instrument specs, stems and the renderer registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from pathlib import Path

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, renderers, server

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
STAGE0_MANIFEST = REPO / "data" / "stage0" / "manifest.json"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _call(name, args):
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        return await client.call_tool(name, args)


def _ffmpeg_ready() -> bool:
    return audio.find_ffmpeg() is not None


def _full_audio_ready() -> bool:
    return (
        audio.find_ffmpeg() is not None
        and audio.find_fluidsynth() is not None
        and audio.find_soundfont() is not None
    )


def _write_tiny_wav(path, amplitude=1000, seconds=0.2, rate=44100):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(amplitude.to_bytes(2, "little") * round(seconds * rate))


async def _make_band(store):
    """Chords + a Bass track + a Melody track, so there are several note tracks to stem."""
    store.ensure_root()
    await _call(
        "create_chord_progression",
        {"chords": ["Am", "F", "C", "G"], "filename": "band", "tempo": 120},
    )
    bass = [
        {"pitch": pitch, "start_beat": start, "beats": 1.0, "velocity": 80}
        for pitch, start in [("C2", 0), ("F2", 1), ("C2", 2), ("G2", 3)]
    ]
    with_bass = (
        await _call("add_track", {"filename": "band.mid", "track_name": "Bass", "notes": bass})
    ).structuredContent["filename"]
    melody = [
        {"pitch": pitch, "start_beat": start, "beats": 0.5, "velocity": 80}
        for pitch, start in [("C5", 0), ("D5", 1), ("E5", 2), ("G5", 3)]
    ]
    final = (
        await _call("add_track", {"filename": with_bass, "track_name": "Melody", "notes": melody})
    ).structuredContent["filename"]
    return final


@pytest.fixture
def engine_available(monkeypatch):
    """Pretend FluidSynth is installed, so spec storage can be tested without audio tools."""
    monkeypatch.setattr(renderers.RENDERERS["fluidsynth"], "available", lambda: True)


async def test_set_track_instrument_merges_and_removes(store, engine_available):
    song = await _make_band(store)
    result = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Bass": {"engine": "fluidsynth", "program": 38, "gain_db": -3.0},
                "Melody": {"engine": "fluidsynth", "gain_db": 1.0},
            },
        },
    )
    assert not result.isError, result.content[0].text
    assert result.structuredContent["tracks"]["Bass"]["gain_db"] == -3.0
    assert result.structuredContent["tracks"]["Bass"]["program"] == 38

    # merge: updating one track keeps the other, and null removes an entry
    merged = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "gain_db": -6.0}}},
    )
    assert merged.structuredContent["tracks"]["Melody"]["gain_db"] == 1.0
    removed = await _call("set_track_instrument", {"filename": song, "tracks": {"Melody": None}})
    assert "Melody" not in removed.structuredContent["tracks"]

    sidecar = store.root / f"{Path(song).stem}.instruments.json"
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["tracks"]["Bass"]["gain_db"] == -6.0


async def test_set_track_instrument_validates(store, engine_available):
    song = await _make_band(store)
    unknown = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Cowbell": {"engine": "fluidsynth"}}},
    )
    assert unknown.isError and "Unknown track 'Cowbell'" in unknown.content[0].text
    assert "Bass" in unknown.content[0].text  # the error lists the real tracks

    missing = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "preset": "/no/such.sf2"}}},
    )
    assert missing.isError and "does not exist" in missing.content[0].text

    bad_engine = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "surge"}}},
    )
    assert bad_engine.isError  # the engine literal rejects unknown engines


async def test_instrument_sidecar_follows_rename_and_delete(store, engine_available):
    song = await _make_band(store)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "gain_db": -2.0}}},
    )
    renamed = (
        await _call("rename_midi_file", {"filename": song, "new_name": "band_renamed"})
    ).structuredContent
    old_sidecar = store.root / f"{Path(song).stem}.instruments.json"
    new_sidecar = store.root / "band_renamed.instruments.json"
    assert not old_sidecar.exists() and new_sidecar.is_file()

    await _call("delete_midi_file", {"filename": renamed["filename"]})
    assert not new_sidecar.exists()


async def test_list_instruments_reports_engines_and_specs(store, engine_available):
    song = await _make_band(store)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "gain_db": -4.0}}},
    )
    engines_only = (await _call("list_instruments", {})).structuredContent
    assert [engine["name"] for engine in engines_only["engines"]] == ["fluidsynth"]
    assert engines_only["file"] is None
    assert len(engines_only["programs"]["melodic"]) == 128
    assert engines_only["programs"]["melodic"][38] == "Synth Bass 1"
    assert engines_only["programs"]["drums"]["Power"] == 16

    with_file = (await _call("list_instruments", {"filename": song})).structuredContent
    assert set(with_file["file"]["tracks"]) == {"Chords", "Bass", "Melody"}
    assert with_file["file"]["specs"]["Bass"]["gain_db"] == -4.0


async def test_override_program_rewrites_the_stem_midi(store, engine_available):
    from chordsmith.singing import extract_track

    song = await _make_band(store)
    stem = store.root / "stem.mid"
    extract_track(store.root / song, stem, 2)  # the Bass track

    plain = renderers.override_program(stem, renderers.InstrumentSpec())
    assert plain == stem  # nothing to apply, the original file is rendered

    rewritten = renderers.override_program(stem, renderers.InstrumentSpec(program=38))
    file = mido.MidiFile(rewritten)
    programs = [
        message.program for track in file.tracks for message in track if message.type == "program_change"
    ]
    assert programs == [38]  # the file's own program change is replaced

    banked = renderers.override_program(stem, renderers.InstrumentSpec(program=38, bank=2))
    file = mido.MidiFile(banked)
    controls = [
        (message.control, message.value)
        for track in file.tracks
        for message in track
        if message.type == "control_change"
    ]
    assert (0, 2) in controls and (32, 0) in controls
    programs = [
        message.program for track in file.tracks for message in track if message.type == "program_change"
    ]
    assert programs == [38]


class _FakeRenderer:
    name = "fluidsynth"

    def __init__(self):
        self.rendered = []

    def available(self):
        return True

    def render(self, midi, spec, out):
        self.rendered.append((mido.MidiFile(midi).tracks[-1], spec.preset))
        _write_tiny_wav(out, amplitude=1000)


async def test_render_audio_stems_plumbing(store, monkeypatch):
    song = await _make_band(store)
    fake = _FakeRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "gain_db": -5.0}}},
    )

    def fake_combine(stems, target):
        _write_tiny_wav(target, amplitude=900)

    monkeypatch.setattr(renderers, "combine_stems", fake_combine)
    result = await _call("render_audio", {"filename": song, "stems": True, "return_as": "base64"})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert [stem["track"] for stem in data["stems"]] == ["Chords", "Bass", "Melody"]
    assert [stem["gain_db"] for stem in data["stems"]] == [0.0, -5.0, 0.0]
    assert all(stem["data_base64"] for stem in data["stems"])
    assert data["duration_seconds"] == pytest.approx(0.2, abs=0.01)
    assert len(fake.rendered) == 3  # one render per note track, conductor excluded


async def test_concurrent_stem_renders_get_distinct_files(store, monkeypatch):
    song = await _make_band(store)
    fake = _FakeRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target))

    async def render_once():
        return await _call("render_audio", {"filename": song, "stems": True})

    first, second = await asyncio.gather(render_once(), render_once())
    assert not first.isError and not second.isError
    names_a = {stem["filename"] for stem in first.structuredContent["stems"]}
    names_b = {stem["filename"] for stem in second.structuredContent["stems"]}
    assert not names_a & names_b  # every stem claimed its own file


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_program_choice_changes_the_stem_audio(store):
    song = await _make_band(store)
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "program": 38}}},
    )
    synth = (
        await _call("render_audio", {"filename": song, "stems": True, "output_filename": "prog38"})
    ).structuredContent
    await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "program": 33}}},
    )
    finger = (
        await _call("render_audio", {"filename": song, "stems": True, "output_filename": "prog33"})
    ).structuredContent
    bass_synth = next(stem for stem in synth["stems"] if stem["track"] == "Bass")
    bass_finger = next(stem for stem in finger["stems"] if stem["track"] == "Bass")
    assert bass_synth["sha256"] != bass_finger["sha256"]


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_render_audio_stems_match_the_single_pass_within_tolerance(store):
    song = await _make_band(store)
    single = (await _call("render_audio", {"filename": song, "output_filename": "single"})).structuredContent
    stems = (
        await _call("render_audio", {"filename": song, "stems": True, "output_filename": "stemmed"})
    ).structuredContent
    assert len(stems["stems"]) == 3
    single_path = store.root / single["filename"]
    stemmed_path = store.root / stems["filename"]
    assert audio.wav_duration(single_path) == pytest.approx(audio.wav_duration(stemmed_path), abs=0.05)
    from chordsmith.singing import gated_rms_db

    assert abs(gated_rms_db(single_path) - gated_rms_db(stemmed_path)) < 2.0


@pytest.mark.skipif(
    not _full_audio_ready() or not STAGE0_MANIFEST.is_file(),
    reason="stage-0 references (data/stage0) not present",
)
async def test_stage0_backing_renders_stay_byte_identical(store):
    import shutil

    manifest = json.loads(STAGE0_MANIFEST.read_text(encoding="utf-8"))
    store.ensure_root()
    for midi_name, reference in (
        ("beast_in_the_pines_dnb_172bpm_backing.mid", "beast_backing.wav"),
        ("oldones_dm_full_backing.mid", "oldones_backing.wav"),
    ):
        source = REPO / "data" / midi_name
        if not source.is_file():
            pytest.skip(f"{midi_name} not present")
        shutil.copyfile(source, store.root / midi_name)
        result = await _call(
            "render_audio",
            {"filename": midi_name, "output_filename": f"stage0_check_{reference[:-4]}"},
        )
        assert not result.isError, result.content[0].text
        rendered = store.root / result.structuredContent["filename"]
        digest = hashlib.sha256(rendered.read_bytes()).hexdigest()
        assert digest == manifest["files"][reference]["sha256"]
