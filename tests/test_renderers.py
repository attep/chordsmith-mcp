"""Tests for per-track rendering: instrument specs, stems and the renderer registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import wave
from pathlib import Path

import mido
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from chordsmith import audio, renderers, server

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent
STAGE0_MANIFEST = REPO / "data" / "stage0" / "manifest.json"


def _make_sf2(presets: list[tuple[str, int, int]]) -> bytes:
    """A minimal valid .sf2 holding just a phdr table (name, bank, program)."""
    phdr = b"".join(
        name.encode("latin-1").ljust(20, b"\x00") + struct.pack("<HHHIII", program, bank, 0, 0, 0, 0)
        for name, bank, program in presets
    )
    phdr += b"EOP".ljust(20, b"\x00") + struct.pack("<HHHIII", 0, 0, 0, 0, 0, 0)
    pdta = b"pdta" + b"phdr" + struct.pack("<I", len(phdr)) + phdr
    body = b"sfbk" + b"LIST" + struct.pack("<I", len(pdta)) + pdta
    return b"RIFF" + struct.pack("<I", len(body)) + body


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
    if engines_only["soundfont"] is not None:
        # the real preset table of the installed SoundFont (bank/program are 0-based)
        presets = engines_only["soundfont"]["presets"]
        assert len(presets) > 100
        assert any(
            entry["bank"] == 128 and entry["program"] == 16 and "Power" in entry["name"] for entry in presets
        )
    else:
        assert len(engines_only["programs"]["melodic"]) == 128
        assert engines_only["programs"]["melodic"][38] == "Synth Bass 1"

    with_file = (await _call("list_instruments", {"filename": song})).structuredContent
    assert set(with_file["file"]["tracks"]) == {"Chords", "Bass", "Melody"}
    assert with_file["file"]["specs"]["Bass"]["gain_db"] == -4.0
    assert with_file["file"]["resolved"]["Bass"] is None  # nothing to resolve without a program


async def test_set_track_instrument_validates_against_the_soundfont(store, engine_available, tmp_path):
    song = await _make_band(store)
    kit = tmp_path / "kit.sf2"
    kit.write_bytes(_make_sf2([("Standard", 128, 0), ("Power", 128, 16), ("Synth Bass 1", 0, 38)]))

    ok = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Bass": {"engine": "fluidsynth", "preset": str(kit), "program": 38}},
        },
    )
    assert not ok.isError, ok.content[0].text
    assert ok.structuredContent["resolved"]["Bass"] == "000-038 Synth Bass 1"

    missing = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Bass": {"engine": "fluidsynth", "preset": str(kit), "program": 99}},
        },
    )
    assert missing.isError and "000-099" in missing.content[0].text
    assert "Synth Bass 1" in missing.content[0].text  # the error lists what the bank has

    bad_bank = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "preset": str(kit), "bank": 5}}},
    )
    assert bad_bank.isError and "Bank 5" in bad_bank.content[0].text

    not_sf2 = tmp_path / "not.sf2"
    not_sf2.write_bytes(b"nope")
    invalid = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "preset": str(not_sf2)}}},
    )
    assert invalid.isError and "not a SoundFont" in invalid.content[0].text

    # a track on MIDI channel 10 (0-based 9) resolves programs against the percussion bank
    drums = [
        {"pitch": "C2", "start_beat": 0, "beats": 0.5, "velocity": 100},
        {"pitch": "D2", "start_beat": 1, "beats": 0.5, "velocity": 100},
    ]
    with_drums = (
        await _call(
            "add_track",
            {"filename": song, "track_name": "Drums", "notes": drums, "channel": 10},
        )
    ).structuredContent["filename"]
    drum_ok = await _call(
        "set_track_instrument",
        {
            "filename": with_drums,
            "tracks": {"Drums": {"engine": "fluidsynth", "preset": str(kit), "program": 16}},
        },
    )
    assert not drum_ok.isError, drum_ok.content[0].text
    assert drum_ok.structuredContent["resolved"]["Drums"] == "128-016 Power"
    drum_miss = await _call(
        "set_track_instrument",
        {
            "filename": with_drums,
            "tracks": {"Drums": {"engine": "fluidsynth", "preset": str(kit), "program": 38}},
        },
    )
    assert drum_miss.isError and "128-038" in drum_miss.content[0].text  # not a kit


async def test_list_instruments_inspects_a_given_soundfont(store, engine_available, tmp_path):
    kit = tmp_path / "kit.sf2"
    kit.write_bytes(_make_sf2([("Power", 128, 16)]))
    result = await _call("list_instruments", {"soundfont": str(kit)})
    assert not result.isError, result.content[0].text
    inventory = result.structuredContent["soundfont"]
    assert inventory["presets"] == [{"name": "Power", "bank": 128, "program": 16}]
    assert result.structuredContent["programs"] is None

    broken = await _call("list_instruments", {"soundfont": str(tmp_path / "missing.sf2")})
    assert broken.isError and "cannot be read" in broken.content[0].text


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

    def render(self, midi, spec, out, effects=None):
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


async def test_render_audio_stems_use_signed_urls_when_configured(store, monkeypatch):
    song = await _make_band(store)
    fake = _FakeRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target))
    monkeypatch.setenv("CHORDSMITH_PUBLIC_URL", "https://example.test")
    result = await _call("render_audio", {"filename": song, "stems": True})
    assert not result.isError, result.content[0].text
    data = result.structuredContent
    assert data["data_base64"]  # the mix still follows return_as (base64 by default)
    for stem in data["stems"]:
        assert "data_base64" not in stem  # stems are megabytes each; links keep them off the wire
        assert stem["download_url"].startswith("https://example.test/")


async def test_spec_sections_round_trip(store, engine_available):
    song = await _make_band(store)
    await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "B", "bars": 4, "bpm": 120}],
        },
    )
    result = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Bass": {"engine": "fluidsynth", "program": 38, "sections": {"B": {"program": 81}}}},
        },
    )
    assert not result.isError, result.content[0].text
    spec = result.structuredContent["tracks"]["Bass"]
    assert spec["sections"] == {"B": {"preset": None, "bank": None, "program": 81}}
    sidecar = json.loads((store.root / f"{song[:-4]}.instruments.json").read_text(encoding="utf-8"))
    assert sidecar["tracks"]["Bass"]["sections"]["B"]["program"] == 81
    loaded = renderers.load_specs(store.root / song)
    assert loaded["Bass"].sections["B"].program == 81


async def test_override_program_applies_section_timeline(store, engine_available):
    from chordsmith.singing import extract_track

    song = await _make_band(store)
    await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "B", "bars": 4, "bpm": 120}],
        },
    )
    stem = store.root / "stem.mid"
    extract_track(store.root / song, stem, 2)  # the Bass track
    spec = renderers.InstrumentSpec(program=38, sections={"B": renderers.SectionInstrument(program=81)})
    rewritten = renderers.override_program(stem, spec)
    file = mido.MidiFile(rewritten)
    tpb = file.ticks_per_beat
    events = []
    for track in file.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if message.type == "program_change":
                events.append((tick, message.type, message.program))
            elif message.type == "control_change":
                events.append((tick, message.type, message.control))
    assert (0, "program_change", 38) in events  # section A uses the track default
    assert (16 * tpb, "program_change", 81) in events  # section B switches exactly at its start

    banked = renderers.override_program(
        stem,
        renderers.InstrumentSpec(
            bank=0, program=38, sections={"B": renderers.SectionInstrument(bank=0, program=81)}
        ),
    )
    file = mido.MidiFile(banked)
    ordered = []
    for message in file.tracks[0]:
        if message.type == "control_change" and message.control in (0, 32):
            ordered.append(("cc", message.control, message.value))
        elif message.type == "program_change":
            ordered.append(("pc", message.program))
    # bank select lands before the program change at both boundaries
    assert ordered[:3] == [("cc", 0, 0), ("cc", 32, 0), ("pc", 38)]
    assert ordered[3:] == [("cc", 0, 0), ("cc", 32, 0), ("pc", 81)]


async def test_set_track_instrument_section_validation(store, engine_available, tmp_path):
    song = await _make_band(store)
    no_map = await _call(
        "set_track_instrument",
        {"filename": song, "tracks": {"Bass": {"engine": "fluidsynth", "sections": {"B": {"program": 81}}}}},
    )
    assert no_map.isError and "no section map" in no_map.content[0].text

    await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "B", "bars": 4, "bpm": 120}],
        },
    )
    unknown = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Bass": {"engine": "fluidsynth", "sections": {"Nope": {"program": 81}}}},
        },
    )
    assert unknown.isError and "Unknown section(s) Nope" in unknown.content[0].text
    assert "A, B" in unknown.content[0].text

    kit_a = tmp_path / "a.sf2"
    kit_b = tmp_path / "b.sf2"
    kit_a.write_bytes(_make_sf2([("Synth Bass 1", 0, 38)]))
    kit_b.write_bytes(_make_sf2([("Lead 2", 0, 81)]))
    cross = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Bass": {
                    "engine": "fluidsynth",
                    "preset": str(kit_a),
                    "sections": {"B": {"preset": str(kit_b), "program": 81}},
                }
            },
        },
    )
    assert cross.isError and "not supported yet" in cross.content[0].text

    bad_program = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Bass": {"engine": "fluidsynth", "preset": str(kit_a), "sections": {"B": {"program": 99}}}
            },
        },
    )
    assert bad_program.isError and "000-099" in bad_program.content[0].text  # checked per section too


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_section_program_changes_keep_held_voices(store):
    from chordsmith.singing import extract_track

    await _call(
        "create_chord_progression",
        {"chords": ["Am"] * 8, "filename": "held", "tempo": 120},
    )
    await _call(
        "set_song_sections",
        {
            "filename": "held.mid",
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "B", "bars": 4, "bpm": 120}],
        },
    )
    held = [
        {"pitch": "C2", "start_beat": 0, "beats": 32, "velocity": 90}  # sounds through both sections
    ]
    with_held = (
        await _call(
            "add_track",
            {"filename": "held.mid", "track_name": "Bass", "notes": held, "output_filename": "held_bass"},
        )
    ).structuredContent["filename"]
    stem = store.root / "held_stem.mid"
    extract_track(store.root / with_held, stem, 2)

    renderer = renderers.RENDERERS["fluidsynth"]
    switch = renderers.InstrumentSpec(program=38, sections={"B": renderers.SectionInstrument(program=81)})
    plain = renderers.InstrumentSpec(program=38)
    first = store.root / "held_first.wav"
    second = store.root / "held_second.wav"
    renderer.render(stem, switch, first)
    renderer.render(stem, plain, second)
    # the held note never restarts, so the program change must not alter its sound at all
    assert first.read_bytes() == second.read_bytes()

    # a note that starts in section B does use the new program
    notes = [
        {"pitch": "C2", "start_beat": 0, "beats": 16, "velocity": 90},
        {"pitch": "C2", "start_beat": 16, "beats": 8, "velocity": 90},
    ]
    with_new = (
        await _call(
            "add_track",
            {"filename": "held.mid", "track_name": "Bass", "notes": notes, "output_filename": "held_bass2"},
        )
    ).structuredContent["filename"]
    stem2 = store.root / "held_stem2.mid"
    extract_track(store.root / with_new, stem2, 2)
    third = store.root / "held_third.wav"
    fourth = store.root / "held_fourth.wav"
    renderer.render(stem2, switch, third)
    renderer.render(stem2, plain, fourth)
    assert third.read_bytes() != fourth.read_bytes()


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_bake_instruments_materializes_the_timeline(store, engine_available):
    song = await _make_band(store)
    await _call(
        "set_song_sections",
        {
            "filename": song,
            "sections": [{"name": "A", "bars": 4, "bpm": 120}, {"name": "B", "bars": 4, "bpm": 120}],
        },
    )
    await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {
                "Bass": {
                    "engine": "fluidsynth",
                    "program": 38,
                    "reverb_send": 90,
                    "sections": {"B": {"program": 81}},
                }
            },
        },
    )
    result = await _call("bake_instruments", {"filename": song, "output_filename": "baked"})
    assert not result.isError, result.content[0].text
    baked = store.root / result.structuredContent["filename"]
    assert baked.is_file()
    # the original file is untouched
    assert renderers.load_specs(store.root / song)["Bass"].program == 38
    # the copy carries the timeline and the send in the Bass track
    file = mido.MidiFile(baked)
    bass = next(
        track
        for track in file.tracks
        if next((m.name for m in track if m.type == "track_name"), "") == "Bass"
    )
    programs = [(m.program) for m in bass if m.type == "program_change"]
    assert programs == [38, 81]
    sends = [(m.control, m.value) for m in bass if m.type == "control_change" and m.control == 91]
    assert sends == [(91, 90)]


async def test_override_program_writes_effect_sends(store, engine_available):
    from chordsmith.singing import extract_track

    song = await _make_band(store)
    stem = store.root / "stem.mid"
    extract_track(store.root / song, stem, 2)  # the Bass track

    # put a send in the file first: a spec without sends must leave it untouched
    file = mido.MidiFile(stem)
    file.tracks[-1].insert(0, mido.Message("control_change", control=91, value=77, channel=0, time=0))
    file.save(stem)
    plain = renderers.override_program(stem, renderers.InstrumentSpec(program=38))
    kept = [
        (message.control, message.value)
        for track in mido.MidiFile(plain).tracks
        for message in track
        if message.type == "control_change" and message.control in (91, 93)
    ]
    assert kept == [(91, 77)]

    spec = renderers.InstrumentSpec(reverb_send=100, chorus_send=50)
    rewritten = renderers.override_program(stem, spec)
    sends = [
        (message.control, message.value)
        for track in mido.MidiFile(rewritten).tracks
        for message in track
        if message.type == "control_change" and message.control in (91, 93)
    ]
    assert (91, 100) in sends and (93, 50) in sends
    assert (91, 77) not in sends  # the file's own send is replaced


async def test_set_track_instrument_stores_effect_sends(store, engine_available):
    song = await _make_band(store)
    result = await _call(
        "set_track_instrument",
        {
            "filename": song,
            "tracks": {"Bass": {"engine": "fluidsynth", "reverb_send": 110, "chorus_send": 60}},
        },
    )
    assert not result.isError, result.content[0].text
    spec = result.structuredContent["tracks"]["Bass"]
    assert spec["reverb_send"] == 110 and spec["chorus_send"] == 60
    sidecar = json.loads((store.root / f"{song[:-4]}.instruments.json").read_text(encoding="utf-8"))
    assert sidecar["tracks"]["Bass"]["reverb_send"] == 110
    assert renderers.load_specs(store.root / song)["Bass"].chorus_send == 60


def test_effect_options_map_engine_settings():
    from chordsmith.models import ChorusSettings, ReverbSettings

    assert renderers.effect_options(None, None) is None
    reverb_only = renderers.effect_options(ReverbSettings(room_size=1.0), None)
    assert reverb_only["synth.reverb.active"] == 1
    assert reverb_only["synth.reverb.room-size"] == 1.0
    assert reverb_only["synth.chorus.active"] == 0
    both = renderers.effect_options(ReverbSettings(), ChorusSettings(voices=6))
    assert both["synth.chorus.active"] == 1 and both["synth.chorus.nr"] == 6
    assert both["synth.reverb.level"] == 0.7  # the engine's own default is echoed


@pytest.mark.skipif(not _full_audio_ready(), reason="ffmpeg/FluidSynth/soundfont not installed")
async def test_render_effects_are_opt_in_and_keep_tails(store):
    song = await _make_band(store)
    dry1 = (await _call("render_audio", {"filename": song, "output_filename": "dry1"})).structuredContent
    dry2 = (await _call("render_audio", {"filename": song, "output_filename": "dry2"})).structuredContent
    assert dry1["sha256"] == dry2["sha256"]  # the default render stays byte-identical
    assert dry1["effects"] == {"reverb": None, "chorus": None}

    wet = (
        await _call("render_audio", {"filename": song, "output_filename": "wet", "reverb": {}})
    ).structuredContent
    assert wet["sha256"] != dry1["sha256"]
    assert wet["effects"]["reverb"]["room_size"] == 0.5  # the engine's own default
    assert wet["effects"]["chorus"] is None
    assert wet["duration_seconds"] > dry1["duration_seconds"]  # the tail is not truncated
    assert wet["clipping"] is False

    chorus = (
        await _call("render_audio", {"filename": song, "output_filename": "chorus", "chorus": {}})
    ).structuredContent
    assert chorus["sha256"] not in {dry1["sha256"], wet["sha256"]}

    both = (
        await _call(
            "render_audio",
            {
                "filename": song,
                "output_filename": "both",
                "reverb": {"room_size": 0.9},
                "chorus": {"voices": 2},
            },
        )
    ).structuredContent
    assert both["sha256"] not in {dry1["sha256"], wet["sha256"], chorus["sha256"]}
    assert both["effects"]["chorus"]["voices"] == 2


@pytest.mark.skipif(not _ffmpeg_ready(), reason="ffmpeg is not installed")
async def test_render_audio_normalizes_only_the_mix(store, monkeypatch):
    song = await _make_band(store)
    fake = _FakeRenderer()
    monkeypatch.setitem(renderers.RENDERERS, "fluidsynth", fake)
    monkeypatch.setattr(
        renderers, "combine_stems", lambda stems, target: _write_tiny_wav(target, amplitude=800)
    )

    raw = (
        await _call("render_audio", {"filename": song, "stems": True, "output_filename": "raw"})
    ).structuredContent
    assert raw["gain_correction_db"] == 0.0
    assert raw["peak_db"] < -20  # the raw combined level is preserved
    assert all(stem["peak_db"] < -20 for stem in raw["stems"])

    loud = (
        await _call(
            "render_audio",
            {
                "filename": song,
                "stems": True,
                "output_filename": "loud",
                "normalize_peak_db": -1.0,
            },
        )
    ).structuredContent
    assert loud["gain_correction_db"] > 20
    assert abs(loud["peak_db"] - (-1.0)) < 0.2
    assert loud["clipping"] is False
    # stems stay raw: their balance must sum back to the unnormalized mix
    assert all(stem["peak_db"] < -20 for stem in loud["stems"])


async def test_list_generated_files_includes_audio(store, engine_available):
    song = await _make_band(store)
    _write_tiny_wav(store.root / "sketch.wav")
    listing = (await _call("list_generated_files", {})).structuredContent
    kinds = {entry["filename"]: entry["kind"] for entry in listing["files"]}
    assert kinds[song] == "midi"
    assert kinds["sketch.wav"] == "wav"


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
