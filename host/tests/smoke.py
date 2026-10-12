"""Smoke test for the ChordSmith VST host (stdlib only).

Builds the host and the tiny test plugins first, then:

    python host/tests/smoke.py <build-dir>

Locates vst_host and the ToneSynth/GainFx bundles under the build directory, generates a
one-note MIDI file and a short wav, and exercises --list, --render and --process.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


def find_file(root: Path, name: str) -> Path:
    for path in sorted(root.rglob(name)):
        if path.is_file():
            return path
    raise SystemExit(f"error: {name} not found under {root}")


def find_dir(root: Path, name: str) -> Path:
    for path in sorted(root.rglob(name)):
        if path.is_dir():
            return path
    raise SystemExit(f"error: {name} not found under {root}")


def write_midi(path: Path) -> None:
    tempo = struct.pack(">I", 500_000)[1:]  # 100 BPM
    events = bytearray()
    events += b"\x00\xff\x51\x03" + tempo
    events += b"\x00\x90\x48\x64"  # note on, C5 (72), velocity 100
    events += b"\x87\x40\x80\x48\x00"  # delta 960 ticks -> note off
    events += b"\x00\xff\x2f\x00"
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
    path.write_bytes(header + b"MTrk" + struct.pack(">I", len(events)) + bytes(events))


def write_wav(path: Path, seconds: float = 0.5, rate: int = 44100) -> None:
    frames = bytearray()
    for index in range(int(seconds * rate)):
        value = int(12000 * math.sin(2 * math.pi * 440.0 * index / rate))
        frames += struct.pack("<hh", value, value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


def wav_stats(path: Path) -> tuple[float, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return frames / rate, max(abs(value) for value in samples)


def run(host: Path, args: list[str]) -> dict:
    result = subprocess.run([str(host), *args], capture_output=True, text=True, timeout=300)
    payload = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    if payload is None:
        raise SystemExit(f"error: no JSON from {args}:\n{result.stdout}\n{result.stderr}")
    if not payload.get("ok"):
        raise SystemExit(f"error: {args} failed: {payload.get('error')}")
    return payload


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: smoke.py <build-dir>")
    root = Path(sys.argv[1]).resolve()
    host = find_file(root, "vst_host.exe") if sys.platform == "win32" else find_file(root, "vst_host")
    synth = find_dir(root, "ToneSynth.vst3")
    gain = find_dir(root, "GainFx.vst3")
    print(f"host:    {host}")
    print(f"plugins: {synth.name}, {gain.name}")

    listing = run(host, ["--list", "--plugin", str(synth)])
    first = listing["types"][0]
    assert first["is_instrument"] is True, first
    assert "Sine" in first["programs"], first
    assert "Saw" in first["programs"], first
    print(f"list:    {first['name']} by {first['vendor']} | programs {first['programs']}")

    effects = run(host, ["--list", "--plugin", str(gain)])
    assert effects["types"][0]["is_instrument"] is False, effects
    assert effects["types"][0]["latency_samples"] == 64, effects
    print(f"list:    {effects['types'][0]['name']} latency {effects['types'][0]['latency_samples']}")

    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        midi = scratch_path / "note.mid"
        rendered = scratch_path / "rendered.wav"
        processed = scratch_path / "processed.wav"
        source = scratch_path / "source.wav"
        write_midi(midi)
        write_wav(source)

        result = run(host, ["--render", "--plugin", str(synth), "--preset", "Sine",
                            "--midi", str(midi), "--wav", str(rendered), "--tail", "1.0"])
        duration, peak = wav_stats(rendered)
        assert 1.9 < duration < 2.1, duration  # 1.0 s of notes + 1.0 s tail
        assert peak > 1000, peak
        print(f"render:  {duration:.2f}s peak {peak} (program {result.get('plugin')})")

        params = run(host, ["--params", "--plugin", str(synth)])
        gain_index = next(p["index"] for p in params["parameters"] if p["name"] == "Gain")
        values = run(host, ["--params", "--plugin", str(synth), "--param-values", str(gain_index)])
        assert values["steps"] == 0 and values["values"] == [], values  # a continuous parameter
        quiet = scratch_path / "quiet.wav"
        run(host, ["--render", "--plugin", str(synth), "--preset", "Sine",
                   "--set", f"{gain_index}=0.1", "--midi", str(midi), "--wav", str(quiet), "--tail", "1.0"])
        quiet_peak = wav_stats(quiet)[1]
        assert quiet_peak < peak * 0.2, (quiet_peak, peak)  # 0.1 vs the 0.8 default
        print(f"params:  Gain index {gain_index}, set to 0.1 -> peak {quiet_peak}")

        result = run(host, ["--process", "--plugin", str(gain),
                            "--in", str(source), "--out", str(processed)])
        out_duration, out_peak = wav_stats(processed)
        in_duration, in_peak = wav_stats(source)
        assert abs(out_duration - in_duration) < 0.01, (out_duration, in_duration)
        assert out_peak > 1000 and abs(out_peak - in_peak * 0.5) < in_peak * 0.1, (out_peak, in_peak)
        assert result["latency_samples"] == 64, result
        print(f"process: {out_duration:.2f}s peak {out_peak} (input {in_peak}), latency compensated")

    print("smoke test passed")


if __name__ == "__main__":
    main()
