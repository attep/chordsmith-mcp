"""Render a DiffSinger vocal in a worker process, one phrase chunk at a time.

The server spawns this module for every DiffSinger job (``python -m
chordsmith.diffsinger_worker spec.json``). The acoustic model's attention is O(n^2) in frames,
so a whole three-minute score in one inference can exhaust the machine's memory — and when it
did, it took the server down with it. Chunking at rests bounds the memory by phrase; running in
a separate process means a native crash can never take the server down; and the wav is written
to a ``.part`` file and renamed only when complete, so a crash never leaves a 0-byte result that
looks like one. Progress is one JSON line per chunk on stdout; the final line carries the info.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

from chordsmith import audio, diffsinger


def _emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m chordsmith.diffsinger_worker <spec.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    notes = [diffsinger.NoteSpec(**note) for note in spec["notes"]]
    output = Path(spec["output"])
    max_seconds = float(spec.get("max_chunk_seconds") or diffsinger.DEFAULT_CHUNK_SECONDS)
    chunks = diffsinger.plan_chunks(notes, max_seconds=max_seconds)
    rendered: list[tuple[float, Path]] = []
    with tempfile.TemporaryDirectory() as scratch:
        for index, chunk in enumerate(chunks, start=1):
            _emit({"chunk": index, "chunks": len(chunks), "start_seconds": round(chunk.start_seconds, 3)})
            chunk_path = Path(scratch) / f"chunk_{index:03d}.wav"
            diffsinger.render(
                chunk.notes,
                chunk_path,
                mode=spec.get("mode", "nectar"),
                steps=int(spec.get("steps", diffsinger.DEFAULT_STEPS)),
                depth=float(spec.get("depth", diffsinger.DEFAULT_DEPTH)),
                velocity=float(spec.get("velocity", 1.0)),
                gender=float(spec.get("gender", 0.0)),
                expr=float(spec.get("expr", 1.0)),
                energy=float(spec.get("energy", 1.0)),
                breathiness=spec.get("breathiness"),
                voicing=spec.get("voicing"),
                tension=spec.get("tension"),
                seed=spec.get("seed"),
                total_seconds=chunk.seconds,
            )
            rendered.append((chunk.start_seconds, chunk_path))
        part = output.with_name(output.stem + ".part" + output.suffix)
        duration = diffsinger.stitch_chunks(rendered, part, total_seconds=spec.get("total_seconds"))
        os.replace(part, output)
    _emit(
        {
            "done": True,
            "duration_seconds": round(duration, 3),
            "chunks": len(chunks),
            "mode": spec.get("mode", "nectar"),
            "peak_db": round(audio.wav_peak_db(output), 2),
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
