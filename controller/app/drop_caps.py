"""Exec a command with no capabilities in any set and no_new_privs, whatever the container was started with.

usage: python -m app.drop_caps -- <command> [args...]

entrypoint.sh's cannot-switch branch (no CAP_SETUID/CAP_SETGID: cap_drop ALL, cap_drop [SETUID], a cap_add set
without them) runs as uid 0. setpriv clears the inheritable and ambient sets, and the bounding set only when
CAP_SETPCAP is there; uid 0 would otherwise keep the rest of its permitted and effective sets across the exec. This
sets no_new_privs, clears the ambient set, and capsets permitted, effective and inheritable to 0, then execs: with
no_new_privs the kernel never grants the new program more than the old permitted set (security/commoncap.c,
cap_bprm_creds_from_file), so the command starts with nothing. Linux only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]


def drop_all() -> None:
    """no_new_privs, an empty ambient set, and empty permitted, effective and inheritable sets, or OSError."""
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    for args in ((PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), (PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)):
        if libc.prctl(*[ctypes.c_ulong(value) for value in args]) != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"prctl{args[:2]} failed: {os.strerror(code)}")
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()  # two 32-bit words per set: all zero
    if libc.capset(ctypes.byref(header), data) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"capset failed: {os.strerror(code)}")


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: python -m app.drop_caps -- <command> [args...]", file=sys.stderr)
        return 64
    try:
        drop_all()
    except OSError as exc:
        print(f"entrypoint: ERROR: could not drop the remaining capabilities: {exc}", file=sys.stderr)
        return 70
    os.execvp(argv[2], argv[2:])  # noqa: S606 - the entrypoint's own command line, exec'd as it was given
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))
