"""Make the controller process non-dumpable, and keep the controller's secrets out of the Playwright driver.

The controller starts children that run as its own uid: the provider CLIs, the Universal Verifier command, autossh,
the regex worker, and Playwright's node driver (and any browser the driver launches). proc(5): reading
/proc/PID/environ (or mem, maps, fd) takes a PTRACE_MODE_READ check, which a same-uid caller passes while the target
is dumpable. A non-dumpable process passes it only for a caller holding CAP_SYS_PTRACE, so a same-uid child that runs
unsandboxed can no longer read the controller's whole environment around the allowlisted one it was given. The flag
resets at execve, so the children themselves stay ordinary and dumpable: each gets an allowlisted environment
instead of the controller's, the driver through confine_playwright_driver_environment(). make_non_dumpable acts on
Linux only.
"""

from __future__ import annotations

import ctypes
import functools
import importlib
import importlib.metadata
import logging
import os
import sys

PR_SET_DUMPABLE = 4
# What the Playwright driver keeps of its environment besides the provider CLIs' base names (CLI_BASE_ENV_NAMES and
# LC_*): Playwright's own variables (PW_*, PLAYWRIGHT_BROWSERS_PATH and the rest of PLAYWRIGHT_*), its debug logging,
# and what shadow browse's headed browser needs to reach a host's display and session bus.
PLAYWRIGHT_DRIVER_ENV_NAMES = frozenset(
    {"DEBUG", "DEBUG_FILE", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
)
PLAYWRIGHT_DRIVER_ENV_PREFIXES = ("PW_", "PLAYWRIGHT_")
_CONFINED = "_auto_browser_confined"

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


def confine_playwright_driver_environment() -> None:
    """Give Playwright's node driver, and the browsers it launches, an allowlisted environment. Idempotent.

    Playwright has no way to set its driver's environment: PipeTransport.connect starts the driver with
    env=get_driver_env(), which copies the whole of os.environ, and looks that name up in
    playwright._impl._transport each time it runs. So this replaces that private name with a wrapper that keeps
    only the base names and PLAYWRIGHT_DRIVER_ENV_NAMES / _PREFIXES of what the original returns. If an upgrade moves
    it, this raises rather than let the driver start with the controller's secrets (the pins are exact, and CI runs
    the lifespan, so that goes red there first).
    """
    from .providers.base import CLI_BASE_ENV_NAMES, CLI_BASE_ENV_PREFIXES

    transport = importlib.import_module("playwright._impl._transport")
    original = getattr(transport, "get_driver_env", None)
    if not callable(original):
        try:
            version = importlib.metadata.version("playwright")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        raise RuntimeError(
            f"Playwright {version} has no playwright._impl._transport.get_driver_env to confine; its driver would "
            "start with the controller's whole environment"
        )
    if getattr(original, _CONFINED, False):
        return
    kept_names = CLI_BASE_ENV_NAMES | PLAYWRIGHT_DRIVER_ENV_NAMES
    kept_prefixes = (*CLI_BASE_ENV_PREFIXES, *PLAYWRIGHT_DRIVER_ENV_PREFIXES)

    @functools.wraps(original)
    def confined_driver_env() -> dict:
        return {
            name: value
            for name, value in original().items()
            if name.upper() in kept_names or name.upper().startswith(kept_prefixes)
        }

    setattr(confined_driver_env, _CONFINED, True)
    transport.get_driver_env = confined_driver_env
