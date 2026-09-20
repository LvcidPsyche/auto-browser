"""One-time, fail-closed preparation for the private broker MFA database mount."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

ENV = Path("/opt/auto-browser/.env")
STATE = Path("/srv/auto-browser-broker")
BROKER_UID = 10001


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the target host")
    if not ENV.is_file() or ENV.is_symlink() or stat.S_IMODE(ENV.stat().st_mode) != 0o600:
        raise SystemExit("Refusing to change an absent, linked, or non-private .env")
    if STATE.is_symlink() or (STATE.exists() and not STATE.is_dir()):
        raise SystemExit("Broker state target is not a private directory")
    if not STATE.exists():
        STATE.mkdir(mode=0o700)
    STATE.chmod(0o700)
    os.chown(STATE, BROKER_UID, BROKER_UID)

    original = ENV.read_text(encoding="utf-8")
    settings = [line for line in original.splitlines() if line.startswith("BROKER_STATE_ROOT=")]
    if settings:
        if len(settings) != 1 or settings[0] != f"BROKER_STATE_ROOT={STATE}":
            raise SystemExit("Unexpected existing broker state setting; inspect manually")
        print("Private broker state directory and setting already ready.")
        return

    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.broker-", dir=ENV.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(original.rstrip("\n") + f"\nBROKER_STATE_ROOT={STATE}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, ENV)
        print("Private broker state directory and setting prepared; no secrets printed.")
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == "__main__":
    main()
