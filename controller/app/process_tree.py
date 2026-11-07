"""Stop a command and everything it started, then reap it without trusting its pipes.

A command run with `asyncio.create_subprocess_exec(..., **GROUP_SPAWN_KWARGS)` leads its own process group on POSIX,
so `kill_and_drain` reaches the descendants that inherited its pipes too (a descendant that calls setsid leaves the
group and is not reached). The pipes are then drained, discarding, for at most `DRAIN_TIMEOUT_SECONDS`: an
uncapped communicate() waited for whatever still held a pipe and buffered all it wrote. The UV verifier uses this;
the providers' run_cli can.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

DRAIN_TIMEOUT_SECONDS = 5.0
_DRAIN_CHUNK_BYTES = 65536

GROUPED = hasattr(os, "killpg")
GROUP_SPAWN_KWARGS: dict[str, bool] = {"start_new_session": True} if GROUPED else {}


def kill_tree(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the process group `process` leads on POSIX, the process itself elsewhere; gone already is fine."""
    try:
        if GROUPED:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass  # exited (and its group with it) before the kill


async def _discard(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while await stream.read(_DRAIN_CHUNK_BYTES):
        pass


async def kill_and_drain(
    process: asyncio.subprocess.Process, *, label: str = "command", timeout: float = DRAIN_TIMEOUT_SECONDS
) -> None:
    """Kill `process` and its group, then drain its pipes (discarding) and reap it, bounded by `timeout` in all.

    Past the timeout something outside the group still holds a pipe: that is logged, and the call returns.
    """
    kill_tree(process)
    try:
        await asyncio.wait_for(
            asyncio.gather(_discard(process.stdout), _discard(process.stderr), process.wait()),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            "%s (pid %s): its pipes were still open %.0f s after the kill; stopped draining them",
            label,
            process.pid,
            timeout,
        )
