"""Read a command's output with a cap, and stop it and everything it started without trusting its pipes.

A command run with `asyncio.create_subprocess_exec(..., **GROUP_SPAWN_KWARGS)` leads its own process group on POSIX,
so `kill_and_drain` reaches the descendants that inherited its pipes too (a descendant that calls setsid leaves the
group and is not reached). The pipes are then drained, discarding, for at most `DRAIN_TIMEOUT_SECONDS`: an
uncapped communicate() waited for whatever still held a pipe and buffered all it wrote. `exchange` feeds the
command's stdin and reads its two pipes at once, with a cap on each, so that neither a runaway nor a full pipe holds
the call. The UV verifier and the providers' run_cli use these.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)

DRAIN_TIMEOUT_SECONDS = 5.0
_READ_CHUNK_BYTES = 65536

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
    while await stream.read(_READ_CHUNK_BYTES):
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


class OutputTooLarge(Exception):
    """A stream read with a strict cap held more than the cap."""


async def read_capped(stream: asyncio.StreamReader, limit: int, *, strict: bool) -> bytes:
    """Read the stream to its end, keeping at most `limit` bytes; past it, `strict` raises and otherwise discards.

    Not strict, the part of the chunk that crosses the limit that still fits is kept, so the start always is.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        room = limit - total
        total += len(chunk)
        if total > limit:
            if strict:
                raise OutputTooLarge
            if room > 0:
                chunks.append(chunk[:room])
            continue
        chunks.append(chunk)
    return b"".join(chunks)


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the command answered without reading all of its input
    finally:
        process.stdin.close()


async def exchange(
    process: asyncio.subprocess.Process, data: bytes | None, *, stdout_limit: int, stderr_keep: int
) -> tuple[bytes, bytes]:
    """Send `data` to the command's stdin (none when it is None: the command was started with no stdin pipe), and
    return (stdout, stderr) once it has exited.

    Both pipes are read at once, so that a command filling one cannot stall on the other. Stdout raises OutputTooLarge
    past `stdout_limit`; of stderr the first `stderr_keep` bytes are kept and the rest is read and dropped. Nothing
    here kills the command: the caller does, on that error, on a timeout and on a cancel. No reader outlives the call.
    """
    tasks = [
        asyncio.ensure_future(read_capped(process.stdout, stdout_limit, strict=True)),
        asyncio.ensure_future(read_capped(process.stderr, stderr_keep, strict=False)),
    ]
    if data is not None:
        tasks.append(asyncio.ensure_future(_feed(process, data)))
    try:
        stdout, stderr, *_ = await asyncio.gather(*tasks)  # what is left is the feeder's, which returns nothing
        await process.wait()
        return stdout, stderr
    finally:
        # No reader may outlive the call: the reaping that follows reads the pipes itself.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
