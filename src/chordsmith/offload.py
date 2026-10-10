"""Run tool bodies off the event loop so long renders never freeze the server.

FastMCP calls synchronous tool functions directly on the event loop (its metadata helper does
``return fn(...)``), so a two-minute stem render would block every other request — status
checks, list calls, even the health route — for its whole duration. Wrapping a tool body with
``offloaded`` keeps the signature and docstring (the tool schema is built from them) and runs it
on a worker thread, so independent calls overlap and the server stays responsive.
"""

from __future__ import annotations

import functools

import anyio


def offloaded(fn):
    """Wrap a synchronous tool body so FastMCP awaits it on a worker thread."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper
