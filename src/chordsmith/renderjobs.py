"""Background render jobs: long renders that outlive any client's tool timeout.

A stems render of a three-minute song writes tens of megabytes per track and can run past a
client's request timeout; ``render_audio_job`` starts the work on a worker thread and returns
immediately, and ``get_render_job`` reports per-track progress and, when done, the full render
result. A track that failed can be retried with ``tracks`` — only those tracks render (and are
combined), so successful stems are never redone. Jobs live 24 hours.
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
from typing import Annotated

import mido
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from chordsmith import delivery, renderers, results
from chordsmith.models import ChorusSettings, ReverbSettings

RENDER_JOB_TTL_SECONDS = 24 * 3600

logger = logging.getLogger(__name__)

_CREATES = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@dataclass
class RenderJob:
    job_id: str
    status: str  # queued | running | done | failed
    created_at: float
    progress: dict[str, str] = field(default_factory=dict)  # track -> pending/running/done/failed
    errors: dict[str, str] = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set_state(self, track: str, state: str, error: str | None = None) -> None:
        """Record a track's state (called from render worker threads)."""
        with self.lock:
            self.progress[track] = state
            if error:
                self.errors[track] = error


_JOBS: dict[str, RenderJob] = {}
_JOBS_LOCK = threading.Lock()


def _state_file() -> Path:
    return delivery.state_dir() / "render_jobs.json"


def _persist() -> None:
    """Save the render job records so a restart never loses a running job's history."""
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
        logger.warning("Could not persist render jobs: %s", exc)


def _load() -> None:
    """Reload render jobs; ones cut off by a restart come back failed, with the reason."""
    target = _state_file()
    if not target.is_file():
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load render jobs: %s", exc)
        return
    interrupted = 0
    with _JOBS_LOCK:
        for key, data in payload.items():
            job = RenderJob(
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
                job.error = "The server restarted during the render; start it again."
                interrupted += 1
            _JOBS[key] = job
    if interrupted:
        logger.warning("Marked %d interrupted render job(s) as failed", interrupted)
        _persist()


def _prune() -> None:
    cutoff = time.time() - RENDER_JOB_TTL_SECONDS
    removed = False
    with _JOBS_LOCK:
        for job_id in [key for key, job in _JOBS.items() if job.created_at < cutoff]:
            del _JOBS[job_id]
            removed = True
    if removed:
        _persist()


def _payload(job: RenderJob) -> dict:
    with job.lock:
        progress = [
            {"track": track, "state": state, "error": job.errors.get(track)}
            for track, state in job.progress.items()
        ]
        return {
            "job_id": job.job_id,
            "status": job.status,
            "progress": progress or None,
            "result": job.result,
            "error": job.error,
        }


def _run(job: RenderJob, runner, args: dict) -> None:
    job.status = "running"
    try:
        job.result = runner(args, job.set_state)
        job.status = "done"
    except Exception as exc:  # the job must never die silently
        job.error = str(exc)
        job.status = "failed"
    _persist()


def register(mcp: FastMCP, store_provider, runner) -> None:
    """Register the render job tools; ``runner(args, set_state) -> result`` does the render."""
    _runner = runner
    _load()

    @mcp.tool(title="Render audio (background job)", annotations=_CREATES)
    def render_audio_job(
        filename: Annotated[str, Field(description="MIDI file in the output folder to render.")],
        format: Annotated[str, Field(description="'wav' (default) or 'mp3'.")] = "wav",
        return_as: Annotated[
            delivery.ReturnAs,
            Field(description="How the rendered files are delivered (like render_audio)."),
        ] = "base64",
        soundfont: Annotated[
            str | None,
            Field(description="Path to a .sf2 soundfont; default: the server's soundfont."),
        ] = None,
        stems: Annotated[
            bool,
            Field(
                description="Render every track on its own and combine them (like render_audio); "
                "the job then reports per-track progress."
            ),
        ] = False,
        tracks: Annotated[
            list[str] | None,
            Field(
                description="Render only these tracks (retry after a partial failure): a stems "
                "render of just those tracks, combined into a mix. Omit for a full render."
            ),
        ] = None,
        normalize_peak_db: Annotated[
            float | None,
            Field(ge=-6.0, le=0.0, description="Normalize the mix to this peak (like render_audio)."),
        ] = None,
        reverb: Annotated[
            ReverbSettings | None, Field(description="FluidSynth reverb (off when omitted).")
        ] = None,
        chorus: Annotated[
            ChorusSettings | None, Field(description="FluidSynth chorus (off when omitted).")
        ] = None,
        output_filename: Annotated[
            str | None, Field(description="Name for the rendered file (like render_audio).")
        ] = None,
        expires_in: Annotated[
            int,
            Field(ge=30, le=delivery.MAX_TTL_SECONDS, description="Download link lifetime in seconds."),
        ] = delivery.DEFAULT_TTL_SECONDS,
        overwrite: Annotated[bool, Field(description="Replace an existing output file.")] = False,
    ) -> results.RenderJobHandle:
        """Start a render as a background job and return its id immediately — for long songs.

        Use this instead of render_audio when the render may outrun a client's tool timeout: a
        three-minute, multi-track stems render writes tens of megabytes and can take minutes.
        The job runs on the server; poll get_render_job for per-track progress (pending,
        running, done, failed) and the full render result when done. Failed tracks keep their
        error message; retry them with ``tracks`` (only those tracks render and combine, so
        finished stems are not redone). Jobs live 24 hours.
        """
        store = store_provider()
        source = store.existing_path(filename)
        midi = mido.MidiFile(source)
        names = renderers.track_names(midi)
        specs = renderers.load_specs(source)
        note_names = [
            names[index]
            for index in renderers.note_tracks(midi)
            if not (specs.get(names[index]) and specs[names[index]].mute)
        ]
        wanted: list[str] | None = None
        if tracks:
            unknown = sorted(set(tracks) - set(note_names))
            if unknown:
                raise ValueError(f"Unknown track(s) {', '.join(unknown)}. Tracks: {', '.join(note_names)}.")
            wanted = [name for name in note_names if name in set(tracks)]
        elif stems:
            wanted = note_names
        job = RenderJob(job_id=f"render_{uuid.uuid4().hex[:10]}", status="queued", created_at=time.time())
        if wanted:
            job.progress = {name: "pending" for name in wanted}
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        _persist()
        args = {
            "filename": filename,
            "format": format,
            "return_as": return_as,
            "soundfont": soundfont,
            "stems": stems,
            "tracks": tracks,
            "normalize_peak_db": normalize_peak_db,
            "reverb": reverb,
            "chorus": chorus,
            "output_filename": output_filename,
            "expires_in": expires_in,
            "overwrite": overwrite,
        }
        threading.Thread(target=_run, args=(job, _runner, args), daemon=True).start()
        return {"job_id": job.job_id, "status": "queued", "tracks": wanted}

    @mcp.tool(title="Get render job", annotations=_READ_ONLY)
    def get_render_job(
        job_id: Annotated[str, Field(description="Job id from render_audio_job.")],
    ) -> results.RenderJobResult:
        """Report a background render's status, per-track progress and the result when done.

        Read-only. ``status`` is queued, running, done or failed; ``progress`` lists every track
        with its state and, for failed tracks, the error; ``result`` is the full render result
        (files, stems, peaks) once the job is done. Jobs expire after 24 hours.
        """
        _prune()
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            raise ValueError(f"Render job '{job_id}' not found (jobs expire after 24 hours).")
        return _payload(job)

    @mcp.tool(title="List render jobs", annotations=_READ_ONLY)
    def list_render_jobs() -> results.RenderJobsResult:
        """Return the recent render jobs (newest first): id, status, tracks and progress.

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
                    "tracks": sorted(job.progress) or None,
                    "error": job.error,
                }
                for job in jobs
            ]
        }
