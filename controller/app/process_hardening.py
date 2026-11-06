"""Make the controller process non-dumpable, so a process of its uid cannot read its memory or environment.

The controller starts children that run as its own uid: the provider CLIs, the Universal Verifier command, autossh
and the regex worker. proc(5): reading /proc/PID/environ (or mem, maps, fd) takes a PTRACE_MODE_READ check, which a
same-uid caller passes while the target is dumpable. A non-dumpable process passes it only for a caller holding
CAP_SYS_PTRACE, so a same-uid child that runs unsandboxed can no longer read the controller's whole environment
around the allowlisted one it was given. The flag resets at execve, so the children themselves stay ordinary.
Linux only.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

PR_SET_DUMPABLE = 4

logger = logging.getLogger(__name__)


def make_non_dumpable() -> bool:
    """prctl(PR_SET_DUMPABLE, 0) on Linux: True when it took, False (after one warning on Linux) when it did not."""
    if not sys.platform.startswith("linux"):
        logger.debug("process hardening: PR_SET_DUMPABLE is Linux-only; skipped on %s", sys.platform)
        return False
    try:
        # The libc already loaded in this process, as drop_caps does.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(*[ctypes.c_ulong(value) for value in (PR_SET_DUMPABLE, 0, 0, 0, 0)]) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    except (OSError, AttributeError) as exc:  # AttributeError: no prctl in this libc
        logger.warning(
            "process hardening: could not make the controller non-dumpable (%s); a process running as its uid "
            "can read its environment through /proc",
            exc,
        )
        return False
    return True
