"""Background mix jobs: full mixes that can outrun any client's tool timeout.

A stems mix re-renders the backing track by track, processes every voice, masters the result
and can run for minutes; ``mix_song_with_vocals_job`` starts the work on a worker thread and
returns immediately, and ``get_mix_job`` reports per-track progress and, when done, the full
mix result. Jobs live 24 hours.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from chordsmith import delivery, renderers, results
from chordsmith.models import (
    ChorusSettings,
    MasterSettings,
    Returns,
    ReverbSettings,
    VocalBus,
    VocalChain,
    VocalSegment,
    VocalSends,
)

MIX_JOB_TTL_SECONDS = 24 * 3600

logger = logging.getLogger(__name__)

_CREATES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@dataclass
class MixJob:
    job_id: str
    status: str  # queued | running | done | failed
    created_at: float
    progress: dict[str, str] = field(default_factory=dict)  # track/phase -> pending/running/done/failed
    errors: dict[str, str] = field(default_factory=dict)
    result: results.MixResult | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_state(self, name: str, state: str, error: str | None = None) -> None:
        """Record a track's or phase's state (called from the mix worker thread)."""
        with self.lock:
            self.progress[name] = state
            if error:
                self.errors[name] = error


_JOBS: dict[str, MixJob] = {}
_JOBS_LOCK = threading.Lock()


def _state_file() -> Path:
    return delivery.state_dir() / "mix_jobs.json"


def _persist() -> None:
    """Save the mix job records so a restart never loses a running job's history."""
    try:
        with _JOBS_LOCK:
            payload = {
                key: {
                    "job_id": job.job_id,
                    "status": job.status,
                    "created_at": job.created_at,
                    "progress": dict(job.progress),
                    "errors": dict(job.errors),
                    "result": job.result,
                    "error": job.error,
                }
                for key, job in _JOBS.items()
            }
        target = _state_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        part.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(part, target)
    except OSError as exc:
        logger.warning("Could not persist mix jobs: %s", exc)


def _load() -> None:
    """Reload mix jobs; ones cut off by a restart come back failed, with the reason."""
    target = _state_file()
    if not target.is_file():
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load mix jobs: %s", exc)
        return
    interrupted = 0
    with _JOBS_LOCK:
        for key, data in payload.items():
            job = MixJob(
                job_id=data["job_id"],
                status=data["status"],
                created_at=data["created_at"],
                progress=dict(data.get("progress") or {}),
                errors=dict(data.get("errors") or {}),
                result=data.get("result"),
                error=data.get("error"),
            )
            if job.status in ("queued", "running"):
                job.status = "failed"
                job.error = "The server restarted during the mix; start it again."
                interrupted += 1
            _JOBS[key] = job
    if interrupted:
        logger.warning("Marked %d interrupted mix job(s) as failed", interrupted)
        _persist()


def _prune() -> None:
    cutoff = time.time() - MIX_JOB_TTL_SECONDS
    removed = False
    with _JOBS_LOCK:
        for job_id in [key for key, job in _JOBS.items() if job.created_at < cutoff]:
            del _JOBS[job_id]
            removed = True
    if removed:
        _persist()


def _payload(job: MixJob) -> results.MixJobResult:
    with job.lock:
        progress: list[results.RenderJobTrack] = [
            {"track": name, "state": state, "error": job.errors.get(name)}
            for name, state in job.progress.items()
        ]
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": progress or None,
            "result": job.result,
            "error": job.error,
        }


def _run(job: MixJob, runner, args: dict) -> None:
    job.status = "running"
    _persist()
    try:
        job.result = cast(results.MixResult, runner(args, job.set_state))
        job.status = "done"
    except Exception as exc:  # the job must never die silently
        job.error = str(exc)
        job.status = "failed"
    _persist()


def register(mcp: FastMCP, store_provider, runner) -> None:
    """Register the mix job tools; ``runner(args, progress) -> result`` runs the mix."""
    _runner = runner
    _load()

    @mcp.tool(title="Mix song with vocals (background job)", annotations=_CREATES)
    def mix_song_with_vocals_job(
        source: Annotated[str, Field(description="The MIDI file the score came from.")],
        vocal: Annotated[
            str | None, Field(description="Vocal filename from a finished job, or the job id.")
        ] = None,
        vocals: Annotated[
            list[VocalSegment] | None,
            Field(description="Vocal pieces to join first (as mix_song_with_vocals)."),
        ] = None,
        guide_track: Annotated[
            str | int | None, Field(description="Track to leave out of the backing (default: auto).")
        ] = None,
        vocal_level_db: Annotated[
            float,
            Field(
                ge=-12.0,
                le=18.0,
                description="Vocal level over the band as heard (default 6).",
            ),
        ] = 6.0,
        backing_volume: Annotated[
            float, Field(ge=0.0, le=2.0, description="Backing trim (1.0 = the rendered level).")
        ] = 1.0,
        backing_levels: Annotated[
            dict[str, Annotated[float, Field(ge=-24.0, le=12.0)]] | None,
            Field(description="Per-track backing trims in dB, keyed by track name."),
        ] = None,
        vocal_reverb: Annotated[bool, Field(description="Gentle reverb on the vocal.")] = False,
        vocal_chain: Annotated[
            VocalChain | None,
            Field(
                description="Single vocal's chain; a <source>.vocal_chain.json sidecar loads "
                "automatically when no chain option is passed."
            ),
        ] = None,
        vocal_sends: Annotated[
            VocalSends | None, Field(description="Single vocal's sends to the shared returns.")
        ] = None,
        vocal_bus: Annotated[
            VocalBus | None, Field(description="Summed-voice bus for several voices.")
        ] = None,
        returns: Annotated[Returns | None, Field(description="Shared reverb/delay returns.")] = None,
        master: Annotated[
            MasterSettings | None, Field(description="Master limiter and/or loudness target.")
        ] = None,
        master_effects: Annotated[
            list[renderers.EffectPlugin] | None,
            Field(
                description="Whole-song VST chain over the summed mix (backing plus vocals), "
                "before the master stage (like mix_song_with_vocals)."
            ),
        ] = None,
        save_chain: Annotated[
            bool, Field(description="Save the effective chain to <source>.vocal_chain.json.")
        ] = False,
        reverb: Annotated[
            ReverbSettings | None, Field(description="FluidSynth reverb for the backing.")
        ] = None,
        chorus: Annotated[
            ChorusSettings | None, Field(description="FluidSynth chorus for the backing.")
        ] = None,
        normalize_peak_db: Annotated[
            float | None,
            Field(
                ge=-6.0,
                le=0.0,
                description="Normalize the mix to this peak (default -1.0; null keeps the raw level).",
            ),
        ] = -1.0,
        compress: Annotated[bool, Field(description="Gentle high-pass and compressor on the vocal.")] = False,
        ducking: Annotated[bool, Field(description="Dip the backing under the vocal.")] = False,
        duck_db: Annotated[
            float, Field(ge=0.0, le=12.0, description="Ducking depth in dB (default 4).")
        ] = 4.0,
        output_filename: Annotated[
            str | None, Field(description="Name for the mix (default: <source>_mix).")
        ] = None,
        overwrite: Annotated[bool, Field(description="Replace an existing mix.")] = False,
    ) -> results.MixJobHandle:
        """Start a mix as a background job and return its id immediately — for long mixes.

        Same options and behavior as mix_song_with_vocals (see its documentation for the
        semantics): the backing is rendered per track with the per-track specs, each voice is
        processed, the balance is measured and the master stage applied. Use this when the mix
        may outrun a client's tool timeout — a stems mix with chains easily runs for minutes.
        Poll get_mix_job for per-track progress (each backing track, then the ``mix`` phase) and
        the full result. Jobs live 24 hours.
        """
        store = store_provider()
        store.existing_path(source)
        if vocals and vocal:
            raise ValueError("Give either vocal or vocals, not both.")
        if not vocals and not vocal:
            raise ValueError("Give a vocal file (or job id), or a list of vocals with their start_seconds.")
        job = MixJob(job_id=f"mix_{uuid.uuid4().hex[:10]}", status="queued", created_at=time.time())
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        _persist()
        args = {
            "source": source,
            "vocal": vocal,
            "vocals": vocals,
            "guide_track": guide_track,
            "vocal_level_db": vocal_level_db,
            "backing_volume": backing_volume,
            "backing_levels": backing_levels,
            "vocal_reverb": vocal_reverb,
            "vocal_chain": vocal_chain,
            "vocal_sends": vocal_sends,
            "vocal_bus": vocal_bus,
            "returns": returns,
            "master": master,
            "master_effects": master_effects,
            "save_chain": save_chain,
            "reverb": reverb,
            "chorus": chorus,
            "normalize_peak_db": normalize_peak_db,
            "compress": compress,
            "ducking": ducking,
            "duck_db": duck_db,
            "output_filename": output_filename,
            "overwrite": overwrite,
        }
        threading.Thread(target=_run, args=(job, _runner, args), daemon=True).start()
        return {"job_id": job.job_id, "status": "queued"}

    @mcp.tool(title="Get mix job", annotations=_READ_ONLY)
    def get_mix_job(
        job_id: Annotated[str, Field(description="Job id from mix_song_with_vocals_job.")],
    ) -> results.MixJobResult:
        """Report a background mix's status, per-track progress and the result when done.

        Read-only. ``status`` is queued, running, done or failed; ``progress`` lists each
        backing track (and the final ``mix`` phase) with its state and, for failed tracks, the
        error; ``result`` is the full mix result once the job is done. Jobs expire after
        24 hours.
        """
        _prune()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Mix job '{job_id}' not found (jobs expire after 24 hours).")
        return _payload(job)

    @mcp.tool(title="List mix jobs", annotations=_READ_ONLY)
    def list_mix_jobs() -> results.MixJobsResult:
        """Return the recent mix jobs (newest first): id, status and any error.

        Read-only; use it to find a job whose reply was lost — records survive server restarts,
        and a job that was running when the server went down comes back failed with the reason.
        Jobs live 24 hours.
        """
        _prune()
        with _JOBS_LOCK:
            jobs = sorted(_JOBS.values(), key=lambda job: job.created_at, reverse=True)
        return {
            "jobs": [
                {
                    "job_id": job.job_id,
                    "status": job.status,
                    "created_at": datetime.fromtimestamp(job.created_at, timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "error": job.error,
                }
                for job in jobs
            ]
        }
