"""Render MIDI files to audio with FluidSynth (and ffmpeg for MP3)."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
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


def bridge_url() -> str | None:
    """The remote VST bridge's base URL (CHORDSMITH_VST_BRIDGE_URL), when one is configured.

    With a bridge, every VST operation — plugin scans, renders, state loads/saves and editor
    windows — runs on the machine that hosts the bridge (see scripts/vst_bridge.py), so a
    server without Windows plugins can still use them.
    """
    url = os.environ.get("CHORDSMITH_VST_BRIDGE_URL", "").strip().rstrip("/")
    return url or None


def local_vst_host() -> str | None:
    """This machine's own vst_host binary (CHORDSMITH_VST_HOST or PATH)."""
    return os.environ.get("CHORDSMITH_VST_HOST") or shutil.which("vst_host")


def find_vst_host() -> str | None:
    """Some VST host: the bridge URL when configured, else the local vst_host binary."""
    bridge = bridge_url()
    if bridge:
        return bridge
    return local_vst_host()


def is_windows_path(path: str) -> bool:
    """Whether a path is a Windows path (drive letter or UNC), as specs name them."""
    return len(path) > 1 and (path[1] == ":" or path.startswith("\\\\"))


def host_for_plugin(plugin: str) -> str | None:
    """The host that can load this plugin: the bridge for Windows plugins, else the local one.

    A bridged server still owns its own plugins (e.g. the Linux .vst3 files under /vst): only
    Windows-path plugins go to the bridge, so both worlds work in one session.
    """
    bridge = bridge_url()
    if bridge and is_windows_path(plugin):
        return bridge
    return local_vst_host()


def _bridge_headers() -> dict[str, str]:
    token = os.environ.get("CHORDSMITH_VST_BRIDGE_TOKEN", "")
    return {"X-Chordsmith-Token": token} if token else {}


def _bridge_post(path: str, payload: dict) -> dict:
    import httpx

    url = bridge_url()
    if url is None:
        raise AudioError("No VST bridge is configured (CHORDSMITH_VST_BRIDGE_URL).")
    try:
        response = httpx.post(
            f"{url}{path}", json=payload, headers=_bridge_headers(), timeout=VST_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise AudioError(f"The VST bridge at {url} is unreachable: {exc}") from exc
    if response.status_code != 200:
        raise AudioError(
            f"The VST bridge at {url} answered HTTP {response.status_code}: {response.text[:200]}"
        )
    return response.json()


def bridge_plugin_scan(directory: str | None = None) -> list[dict[str, str]]:
    """The .vst3 plugins the bridge's machine can see (CHORDSMITH_VST_BRIDGE_VST_DIR)."""
    import httpx

    url = bridge_url()
    if url is None:
        return []
    target = directory or os.environ.get("CHORDSMITH_VST_BRIDGE_VST_DIR", "")
    try:
        response = httpx.get(f"{url}/scan", params={"dir": target}, headers=_bridge_headers(), timeout=120)
    except httpx.HTTPError as exc:
        raise AudioError(f"The VST bridge at {url} is unreachable: {exc}") from exc
    if response.status_code != 200:
        raise AudioError(f"The VST bridge at {url} answered HTTP {response.status_code} for a plugin scan.")
    return response.json().get("plugins") or []


def bridge_paths_exist(paths: list[str]) -> dict[str, bool]:
    """Which of these paths exist on the bridge's machine."""
    if bridge_url() is None or not paths:
        return {}
    return _bridge_post("/exists", {"paths": paths}).get("exists") or {}


def bridge_shared_path(path: str | Path) -> str:
    """Map a server-side path into the bridge's view of the shared output folder.

    Editor saves cannot be uploaded after the fact (the window closes minutes later), so their
    path must be one both machines see — CHORDSMITH_VST_BRIDGE_SHARED_FROM/_TO describe it.
    """
    from_env = os.environ.get("CHORDSMITH_VST_BRIDGE_SHARED_FROM", "").rstrip("/")
    to_env = os.environ.get("CHORDSMITH_VST_BRIDGE_SHARED_TO", "").rstrip("\\/")
    text = str(path)
    if from_env and to_env and text.startswith(from_env):
        rest = text[len(from_env) :].replace("/", "\\").lstrip("\\")
        return f"{to_env}\\{rest}" if rest else to_env
    return text


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


BRIDGE_INPUT_FLAGS = ("--midi", "--in", "--state")
BRIDGE_OUTPUT_FLAGS = ("--wav", "--out", "--save-state", "--dump-state")


def _run_vst_bridge(url: str, args: list[str]) -> subprocess.CompletedProcess:
    """Send a job to the bridge, uploading input files and writing output files back."""
    payload: dict = {"args": [str(item) for item in args]}
    files: dict[str, str] = {}
    for index in range(1, len(args)):
        if args[index - 1] in BRIDGE_INPUT_FLAGS:
            path = Path(args[index])
            if path.is_file():
                files[str(path)] = base64.b64encode(path.read_bytes()).decode()
    needs_files = bool(files) or any(args[index - 1] in BRIDGE_OUTPUT_FLAGS for index in range(1, len(args)))
    if needs_files:
        payload["files"] = files
    response = _bridge_post("/job" if needs_files else "/run", payload)
    for original, blob in (response.get("outputs") or {}).items():
        target = Path(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(blob))
    return subprocess.CompletedProcess(
        args=[url, *args],
        returncode=int(response.get("exit") or 0),
        stdout=response.get("stdout") or "",
        stderr=response.get("stderr") or "",
    )


def _run_vst_host(host: str, args: list[str]) -> dict:
    """Run the VST host CLI (locally or through the bridge) and parse its JSON line."""
    import json

    if host.startswith("http"):
        result = _run_vst_bridge(host, args)
    else:
        try:
            result = subprocess.run(
                [host, *args], capture_output=True, text=True, timeout=VST_TIMEOUT_SECONDS
            )
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
    state: str | None = None,
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
    if state:
        args += ["--state", state]
    if preset:
        args += ["--preset", preset]
    args += _set_arguments(sets)
    return _run_vst_host(host, args)


def process_vst(
    host: str,
    chain: list[tuple[str, str | None, list[tuple[int, float | str]], str | None]],
    source: Path,
    target: Path,
) -> dict:
    """Run a wav through effect plugins in order; the host compensates reported latency."""
    args = ["--process"]
    for plugin, preset, sets, state in chain:
        args += ["--plugin", plugin]
        if state:
            args += ["--state", state]
        if preset:
            args += ["--preset", preset]
        args += _set_arguments(sets)
    args += ["--in", str(source), "--out", str(target)]
    return _run_vst_host(host, args)


def dump_vst_state(
    host: str,
    plugin: str,
    target: Path,
    sets: list[tuple[int, float | str]] | None = None,
    preset: str | None = None,
) -> dict:
    """Write the plugin's state snapshot to a file with no GUI (parameter-driven plugins)."""
    args = ["--dump-state", str(target), "--plugin", plugin]
    if preset:
        args += ["--preset", preset]
    args += _set_arguments(sets)
    return _run_vst_host(host, args)


def start_vst_editor(host: str, plugin: str, target: Path, state: str | None = None) -> None:
    """Launch the plugin's editor; on a bridge the window opens on the bridge's desktop.

    The state target must be on the shared folder when bridged: it is written minutes later,
    after the window closes, straight into the path the host was given.
    """
    if host.startswith("http"):
        args = ["--editor", "--plugin", plugin, "--save-state", bridge_shared_path(target)]
        if state:
            args += ["--state", bridge_shared_path(state)]
        response = _bridge_post("/editor", {"args": args})
        if not response.get("ok"):
            raise AudioError(
                f"The VST bridge could not open the editor: {response.get('error', 'unknown error')}"
            )
        return
    args = [host, "--editor", "--plugin", plugin, "--save-state", str(target)]
    if state:
        args += ["--state", state]
    kwargs: dict = {"close_fds": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as exc:
        raise AudioError(f"Could not start the VST editor: {exc}") from exc
