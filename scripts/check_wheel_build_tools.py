#!/usr/bin/env python
"""Fail unless each wheel was built by the version of its build backend the lock pins.

    python scripts/check_wheel_build_tools.py controller/constraints.txt dist/*.whl

A wheel's ``*.dist-info/WHEEL`` file names what built it on its ``Generator:`` line, which the backend writes from its own
version: setuptools writes ``Generator: setuptools (84.2.1)``, hatchling ``Generator: hatchling 1.33.0``. Each wheel's
line must name one of those two backends and the version the lock pins for it with ``==``. A generator this check does
not recognise fails, and so does a lock that does not pin that backend exactly once.

What the line witnesses is the backend's own version. It says nothing of the versions of what the backend imported
(packaging, pathspec and the rest), nor of the frontend (build) that installed and ran it. An sdist records no
generator, so only wheels are checked.

It prints one line per wheel, and exits with status 1 if any wheel fails.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from email.parser import HeaderParser
from pathlib import Path

# Each backend's Generator value, with its version as the one group.
GENERATORS = {
    "setuptools": re.compile(r"setuptools \(([^()\s]+)\)"),
    "hatchling": re.compile(r"hatchling ([^()\s]+)"),
}
# A lock line's name and the version it pins with ==, after any extras.
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;]+)")
WHEEL_FILE = re.compile(r"[^/]+\.dist-info/WHEEL")


def lock_pins(text: str) -> dict[str, list[str]]:
    """Every version the lock pins with == for each package, by its normalized name."""
    pins: dict[str, list[str]] = {}
    for raw in text.splitlines():
        match = PIN.match(raw.split("#", 1)[0].strip())
        if match:
            pins.setdefault(re.sub(r"[-_.]+", "-", match.group(1)).lower(), []).append(match.group(2))
    return pins


def generator(wheel: Path) -> str:
    """The Generator value of the wheel's own WHEEL file."""
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if WHEEL_FILE.fullmatch(name)]
        if len(names) != 1:
            raise ValueError(f"it holds {len(names)} *.dist-info/WHEEL files")
        values = HeaderParser().parsestr(archive.read(names[0]).decode("utf-8")).get_all("Generator", [])
    if len(values) != 1:
        raise ValueError(f"its WHEEL file holds {len(values)} Generator lines")
    return values[0].strip()


def check(wheel: Path, pins: dict[str, list[str]], lock: str) -> tuple[bool, str]:
    """Whether ``wheel`` was built by the backend version ``pins`` holds, and the line that says so."""
    try:
        value = generator(wheel)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, ValueError) as exc:
        return False, f"FAIL {wheel.name}: cannot read what built it: {exc}"
    for backend, pattern in GENERATORS.items():
        match = pattern.fullmatch(value)
        if match is None:
            continue
        built = match.group(1)
        pinned = pins.get(backend, [])
        if len(pinned) != 1:
            return (
                False,
                f"FAIL {wheel.name}: built by {backend} {built}, but {lock} has {len(pinned)} {backend}== pins",
            )
        if built != pinned[0]:
            return False, f"FAIL {wheel.name}: built by {backend} {built}, but {lock} pins {backend}=={pinned[0]}"
        return True, f"ok   {wheel.name}: built by {backend} {built}, as {lock} pins"
    return False, f"FAIL {wheel.name}: built by {value!r}, which is no backend this check knows"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lock", help="the constraints file that pins the backends")
    parser.add_argument("wheels", nargs="+", type=Path, metavar="wheel", help="a wheel to check")
    args = parser.parse_args(argv)
    pins = lock_pins(Path(args.lock).read_text(encoding="utf-8"))
    passed = True
    for wheel in args.wheels:
        ok, line = check(wheel, pins, args.lock)
        print(line)
        passed = passed and ok
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
