"""Render MIDI files to audio with FluidSynth (and ffmpeg for MP3)."""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import mido

DEFAULT_SOUNDFONT_PATHS = (
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/soundfonts/FluidR3_GM.sf2",
    "/usr/share/soundfonts/default.sf2",
)
RENDER_TIMEOUT_SECONDS = 300
VST_TIMEOUT_SECONDS = 600
EFFECTS_TAIL_SECONDS = 3.0  # with effects on, keep FluidSynth's natural tail past the last note


class AudioError(ValueError):
    """Raised when audio cannot be rendered (missing tool, bad soundfont, ...)."""


def find_fluidsynth() -> str | None:
    return os.environ.get("CHORDSMITH_FLUIDSYNTH") or shutil.which("fluidsynth")


def find_ffmpeg() -> str | None:
    return os.environ.get("CHORDSMITH_FFMPEG") or shutil.which("ffmpeg")


def find_sfizz_render() -> str | None:
    return os.environ.get("CHORDSMITH_SFIZZ_RENDER") or shutil.which("sfizz_render")


def find_vst_host() -> str | None:
    return os.environ.get("CHORDSMITH_VST_HOST") or shutil.which("vst_host")


def find_soundfont() -> str | None:
    env = os.environ.get("CHORDSMITH_SOUNDFONT")
    if env:
        return env if Path(env).is_file() else None
    for candidate in DEFAULT_SOUNDFONT_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=RENDER_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise AudioError(f"{Path(command[0]).name} failed: {detail or 'unknown error'}")


def midi_duration(midi_path: Path) -> float:
    """Length of a MIDI file in seconds (tempo aware)."""
    return mido.MidiFile(midi_path).length


def trim_wav(path: Path, seconds: float) -> None:
    """Cut a wav file down to at most ``seconds`` of audio (in place)."""
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames = min(source.getnframes(), int(seconds * rate))
        data = source.readframes(frames)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(data)


def pad_wav(path: Path, seconds: float) -> None:
    """Append silence so the wav lasts exactly ``seconds``; a no-op when it is already longer."""
    with wave.open(str(path), "rb") as source:
        channels, width, rate = source.getnchannels(), source.getsampwidth(), source.getframerate()
        frames = source.getnframes()
        data = source.readframes(frames)
    wanted = int(seconds * rate)
    if frames >= wanted:
        return
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(data + b"\x00" * ((wanted - frames) * width * channels))


def wav_peak_db(path: Path) -> float:
    """Peak level (dBFS) of a 16-bit wav file."""
    import array
    import math

    with wave.open(str(path), "rb") as handle:
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    peak = max((abs(value) for value in samples), default=0)
    return 20.0 * math.log10(max(peak / 32768.0, 1e-9))


def normalize_wav(path: Path, target_peak_db: float) -> float:
    """Gain ``path`` in place so its sample peak hits ``target_peak_db``.

    Returns the correction applied in dB (0.0 when it already sits at the target). ffmpeg does
    the gain pass, so 16-bit quantization noise is not compounded by a Python loop.
    """
    peak = wav_peak_db(path)
    correction = round(target_peak_db - peak, 2)
    if correction == 0.0:
        return 0.0
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise AudioError("Normalization needs ffmpeg; install it or skip normalize_peak_db.")
    temp = path.with_name(path.stem + ".norm.wav")
    try:
        _run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-af",
                f"volume={correction:.2f}dB",
                str(temp),
            ]
        )
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return correction


def _effect_options(effects: dict[str, float | int] | None) -> list[str]:
    """FluidSynth ``-o`` arguments: explicit effect settings, or both effects forced off."""
    if not effects:
        return ["-o", "synth.reverb.active=0", "-o", "synth.chorus.active=0"]
    args: list[str] = []
    for name, value in effects.items():
        args += ["-o", f"{name}={value}"]
    return args


def probe_media(path: Path) -> dict:
    """Duration, sample rate and channels of an audio file.

    Uses ffprobe when available (any format); falls back to the wave module for wav files.
    """
    import json

    ffprobe = os.environ.get("CHORDSMITH_FFPROBE") or shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate,channels,duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            stream = json.loads(result.stdout)["streams"][0]
            return {
                "duration_seconds": round(float(stream.get("duration") or 0.0), 3),
                "sample_rate": int(stream.get("sample_rate") or 0),
                "channels": int(stream.get("channels") or 0),
            }
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError, json.JSONDecodeError):
            pass
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return {
                "duration_seconds": round(handle.getnframes() / rate, 3),
                "sample_rate": rate,
                "channels": handle.getnchannels(),
            }
    except (OSError, wave.Error) as exc:
        raise AudioError(f"Could not read '{path.name}': {exc}") from exc


def render(
    midi_path: Path,
    target: Path,
    audio_format: str,
    soundfont: str | None = None,
    normalize_peak_db: float | None = None,
    effects: dict[str, float | int] | None = None,
) -> float:
    """Render ``midi_path`` to ``target`` (a .wav or .mp3 path).

    With ``normalize_peak_db`` the render is gained to that peak before the mp3 encode (None
    keeps FluidSynth's raw level). ``effects`` is a FluidSynth setting map (reverb/chorus);
    None renders dry with both effects explicitly off — the byte-identical default. With
    effects on, the natural tail past the last note is kept (up to EFFECTS_TAIL_SECONDS).
    Returns the gain correction applied in dB.
    """
    if audio_format not in ("wav", "mp3"):
        raise AudioError("format must be 'wav' or 'mp3'.")
    fluidsynth = find_fluidsynth()
    if fluidsynth is None:
        raise AudioError(
            "FluidSynth is not installed. Install it (Docker image already includes it) or set "
            "CHORDSMITH_FLUIDSYNTH to the executable."
        )
    sf2 = soundfont or find_soundfont()
    if not sf2:
        raise AudioError("No soundfont found. Set CHORDSMITH_SOUNDFONT to a .sf2 file.")
    wav_path = target if audio_format == "wav" else target.with_suffix(".tmp.wav")
    _run(
        [
            fluidsynth,
            "-ni",
            "-F",
            str(wav_path),
            "-r",
            "44100",
            *_effect_options(effects),
            str(sf2),
            str(midi_path),
        ]
    )
    if not wav_path.is_file():
        raise AudioError("FluidSynth produced no audio.")
    # FluidSynth keeps rendering past the last note; cut back to the MIDI length so the audio
    # matches the file's reported duration — but keep the natural tail when effects are on, so
    # reverb/chorus tails are not truncated.
    tail = EFFECTS_TAIL_SECONDS if effects else 0.0
    trim_wav(wav_path, midi_duration(midi_path) + tail)
    correction = 0.0
    if normalize_peak_db is not None:
        correction = normalize_wav(wav_path, normalize_peak_db)
    if audio_format == "mp3":
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            wav_path.unlink(missing_ok=True)
            raise AudioError("MP3 output needs ffmpeg; install it or ask for format='wav'.")
        try:
            _run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-b:a", "192k", str(target)])
        finally:
            wav_path.unlink(missing_ok=True)
        if not target.is_file():
            raise AudioError("ffmpeg produced no audio.")
    return correction


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def render_sfizz(sfz: str, midi_path: Path, target: Path) -> None:
    """Render ``midi_path`` through an SFZ instrument with sfizz_render (44.1 kHz, like FluidSynth).

    Keeps the dry FluidSynth convention: the wav is trimmed to the MIDI file's own length.
    """
    binary = find_sfizz_render()
    if binary is None:
        raise AudioError(
            "sfizz_render is not installed. Install sfizz (the Docker image includes it) or set "
            "CHORDSMITH_SFIZZ_RENDER to the executable."
        )
    _run(
        [
            binary,
            "--sfz",
            sfz,
            "--midi",
            str(midi_path),
            "--wav",
            str(target),
            "--samplerate",
            "44100",
        ]
    )
    if not target.is_file():
        raise AudioError("sfizz_render produced no audio.")
    # sfizz_render stops at the last MIDI event (it does not know the song's length), so a track
    # whose notes end early would leave a short stem. Trim any overshoot and pad the rest, so
    # every stem has the same length as the song.
    duration = midi_duration(midi_path)
    trim_wav(target, duration)
    pad_wav(target, duration)


def _run_vst_host(host: str, args: list[str]) -> dict:
    """Run the VST host CLI and parse its JSON line; AudioError with its message on failure."""
    import json

    try:
        result = subprocess.run([host, *args], capture_output=True, text=True, timeout=VST_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Could not run the VST host: {exc}") from exc
    payload: dict | None = None
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = None
            if payload is not None:
                break
    if payload is None:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise AudioError(f"vst_host failed: {detail or 'no output'}")
    if not payload.get("ok"):
        raise AudioError(f"vst_host: {payload.get('error', 'unknown error')}")
    return payload


def vst_plugin_info(host: str, plugin: str) -> dict:
    """Load a plugin in the host and return its types (vendor, programs, latency, ...)."""
    return _run_vst_host(host, ["--list", "--plugin", plugin])


def vst_plugin_params(host: str, plugin: str) -> dict:
    """The plugin's parameters (index, name, steps, current value and text)."""
    return _run_vst_host(host, ["--params", "--plugin", plugin])


def vst_param_values(host: str, plugin: str, index: int, limit: int = 200) -> dict:
    """The value strings of one discrete parameter (a patch list selector, for example)."""
    return _run_vst_host(
        host, ["--params", "--plugin", plugin, "--param-values", str(index), "--param-limit", str(limit)]
    )


def _set_arguments(sets: list[tuple[int, float | str]] | None) -> list[str]:
    """--set arguments: numbers as plain values, strings as value names the host resolves."""
    args: list[str] = []
    for index, value in sets or []:
        text = f"{value:g}" if isinstance(value, (int, float)) else str(value)
        args += ["--set", f"{index}={text}"]
    return args


def render_vst(
    host: str,
    plugin: str,
    preset: str | None,
    sets: list[tuple[int, float | str]] | None,
    midi_path: Path,
    target: Path,
    tail: float = 2.0,
) -> dict:
    """Render a MIDI file through a VST3 instrument with the host (44.1 kHz, stereo)."""
    args = [
        "--render",
        "--plugin",
        plugin,
        "--midi",
        str(midi_path),
        "--wav",
        str(target),
        "--tail",
        f"{tail:g}",
    ]
    if preset:
        args += ["--preset", preset]
    args += _set_arguments(sets)
    return _run_vst_host(host, args)


def process_vst(
    host: str,
    chain: list[tuple[str, str | None, list[tuple[int, float | str]]]],
    source: Path,
    target: Path,
) -> dict:
    """Run a wav through effect plugins in order; the host compensates reported latency."""
    args = ["--process"]
    for plugin, preset, sets in chain:
        args += ["--plugin", plugin]
        if preset:
            args += ["--preset", preset]
        args += _set_arguments(sets)
    args += ["--in", str(source), "--out", str(target)]
    return _run_vst_host(host, args)
