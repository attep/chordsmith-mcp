"""Master-bus finishing for rendered mixes: an optional loudness target and true-peak limiter.

The vocal mix tools have the same master stage inside their pipeline; this module applies it to
an already-rendered wav (a render or stems mix): one static gain from an EBU R128 measurement
(loudness scales with gain, so one measurement is enough — no pumping), a true-peak limiter
running 0.3 dB under its ceiling, and the same measurements and warnings the mix reports.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from chordsmith import audio, singing
from chordsmith.models import MasterSettings

LIMITER_MARGIN_DB = singing.LIMITER_MARGIN_DB


def _apply_filters(source: Path, filter_text: str) -> None:
    """Run one ffmpeg filter pass over ``source`` in place (via a scratch file)."""
    ffmpeg = audio.find_ffmpeg()
    if ffmpeg is None:
        raise audio.AudioError(
            "Mastering needs ffmpeg; install it or drop the 'master' options (the Docker image includes it)."
        )
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "mastered.wav"
        process = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-af", filter_text, str(target)],
            capture_output=True,
            text=True,
        )
        if process.returncode != 0 or not target.is_file():
            tail = (process.stderr or "").strip()[-300:]
            raise audio.AudioError("ffmpeg mastering failed" + (f": {tail}" if tail else "."))
        target.replace(source)


def apply_master(path: Path, master: MasterSettings) -> dict:
    """Master a rendered wav in place; returns the measurements and warnings.

    ``loudness_target_lufs`` becomes one static gain (it supersedes peak normalization), and an
    enabled limiter sits ``LIMITER_MARGIN_DB`` under its ceiling so inter-sample peaks stay
    inside it. The report mirrors the mix tools: measured ``loudness``, the limiter's
    ``reduction`` and any warnings (target missed, ceiling exceeded, clipping).
    """
    limiter_on = master.limiter is not None and master.limiter.enabled
    target_lufs = master.loudness_target_lufs
    before = singing.loudness_report(path)
    correction = 0.0
    if target_lufs is not None and before["integrated_lufs"] is not None:
        correction = round(target_lufs - before["integrated_lufs"], 2)
    filters = []
    if correction:
        filters.append(f"volume={correction:.2f}dB")
    if limiter_on:
        assert master.limiter is not None
        filters.append(singing.limiter_filter(master.limiter.ceiling_db - LIMITER_MARGIN_DB))
    if filters:
        _apply_filters(path, ",".join(filters))
    loudness = singing.loudness_report(path)
    peak = singing.peak_db(path)
    warnings: list[str] = []
    limiter_reduction: float | None = None
    if limiter_on:
        assert master.limiter is not None
        before_peak = before["true_peak_db"] if before["true_peak_db"] is not None else peak
        limiter_reduction = round(before_peak + correction - peak, 2)
        if limiter_reduction > 2.0:
            warnings.append(
                f"the master limiter takes off {limiter_reduction:.1f} dB; leave more headroom "
                "or tame the mix"
            )
        if (
            loudness["true_peak_db"] is not None
            and loudness["true_peak_db"] > master.limiter.ceiling_db + 0.05
        ):
            warnings.append(
                f"true peak {loudness['true_peak_db']:g} dBFS is over the "
                f"{master.limiter.ceiling_db:g} dBFS ceiling"
            )
    if target_lufs is not None and loudness["integrated_lufs"] is not None:
        if abs(loudness["integrated_lufs"] - target_lufs) > 1.0:
            warnings.append(
                f"the mix landed at {loudness['integrated_lufs']} LUFS, not the {target_lufs:g} LUFS target"
            )
    if not limiter_on and peak > -0.05:
        warnings.append(
            "the mix clips at the target; lower loudness_target_lufs or enable the master limiter"
        )
    return {
        "loudness": loudness,
        "limiter_reduction_db": limiter_reduction,
        "gain_correction_db": correction,
        "peak_db": round(peak, 2),
        "warnings": warnings,
    }
