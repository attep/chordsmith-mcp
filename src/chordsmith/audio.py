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


class AudioError(ValueError):
    """Raised when audio cannot be rendered (missing tool, bad soundfont, ...)."""


def find_fluidsynth() -> str | None:
    return os.environ.get("CHORDSMITH_FLUIDSYNTH") or shutil.which("fluidsynth")


def find_ffmpeg() -> str | None:
    return os.environ.get("CHORDSMITH_FFMPEG") or shutil.which("ffmpeg")


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


def render(midi_path: Path, target: Path, audio_format: str, soundfont: str | None = None) -> None:
    """Render ``midi_path`` to ``target`` (a .wav or .mp3 path)."""
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
            "-o",
            "synth.reverb.active=0",
            "-o",
            "synth.chorus.active=0",
            str(sf2),
            str(midi_path),
        ]
    )
    if not wav_path.is_file():
        raise AudioError("FluidSynth produced no audio.")
    # FluidSynth keeps rendering release tails past the last note; cut back to the MIDI length
    # so the audio matches the file's reported duration.
    trim_wav(wav_path, midi_duration(midi_path))
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


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / handle.getframerate()
