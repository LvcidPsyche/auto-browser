#!/usr/bin/env python
"""Fail unless build, pip and the backend that built each wheel are the versions the lock pins.

    python scripts/check_wheel_build_tools.py controller/constraints.txt dist/*.whl

The frontend is read where it runs: the ``build`` and ``pip`` distributions installed in the Python that runs this
check must be the versions the lock pins with ``==``, each pinned exactly once. build installs each backend with the pip
of the Python it runs in (``python -m pip --python <its environment>``, for a pip of 22.3 or later), and the workflows
and the release audit run this check with the interpreter that ran their builds; run by another Python, these two lines
speak of that Python.

A wheel's ``*.dist-info/WHEEL`` file names what built it on its ``Generator:`` line, which the backend writes from its own
version: setuptools writes ``Generator: setuptools (84.2.1)``, hatchling ``Generator: hatchling 1.33.0``. Each wheel's
line must name one of those two backends and the version the lock pins for it with ``==``. A generator this check does
not recognise fails, and so does a lock that does not pin that backend exactly once.

What the Generator line witnesses is the backend's own version. It says nothing of the versions of what the backend
imported (packaging, pathspec and the rest). An sdist records no generator, so only wheels are checked.

It prints one line for build, one for pip and one per wheel, and exits with status 1 if any of them fails.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
import zipfile
from email.parser import HeaderParser
from pathlib import Path

# The frontend, and the pip it installs each backend with.
FRONTEND = ("build", "pip")

# Each backend's Generator value, with its version as the one group.
GENERATORS = {
    "setuptools": re.compile(r"setuptools \(([^()\s]+)\)"),
    "hatchling": re.compile(r"hatchling ([^()\s]+)"),
}
WHEEL_FILE = re.compile(r"[^/]+\.dist-info/WHEEL")


def pins_of(text: str, name: str) -> list[str]:
    """Every version the lock's ``text`` pins ``name`` to, on a line that starts ``name==``, as uv writes a pin."""
    return re.findall(rf"(?m)^{re.escape(name)}==([^\s;]+)", text)


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


def check_installed(name: str, text: str, lock: str) -> tuple[bool, str]:
    """Whether the ``name`` installed in this Python is the version the lock's ``text`` pins, and the line that says so."""
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False, f"FAIL {name}: not installed in {sys.executable}"
    pinned = pins_of(text, name)
    if len(pinned) != 1:
        return False, f"FAIL {name} {installed} in {sys.executable}, but {lock} has {len(pinned)} {name}== pins"
    if installed != pinned[0]:
        return False, f"FAIL {name} {installed} in {sys.executable}, but {lock} pins {name}=={pinned[0]}"
    return True, f"ok   {name} {installed} in {sys.executable}, as {lock} pins"


def check(wheel: Path, text: str, lock: str) -> tuple[bool, str]:
    """Whether ``wheel`` was built by the backend version the lock's ``text`` pins, and the line that says so."""
    try:
        value = generator(wheel)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, ValueError) as exc:
        return False, f"FAIL {wheel.name}: cannot read what built it: {exc}"
    for backend, pattern in GENERATORS.items():
        match = pattern.fullmatch(value)
        if match is None:
            continue
        built = match.group(1)
        pinned = pins_of(text, backend)
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
    parser.add_argument("lock", help="the constraints file that pins build, pip and the backends")
    parser.add_argument("wheels", nargs="+", type=Path, metavar="wheel", help="a wheel to check")
    args = parser.parse_args(argv)
    text = Path(args.lock).read_text(encoding="utf-8")
    results = [check_installed(name, text, args.lock) for name in FRONTEND]
    results += [check(wheel, text, args.lock) for wheel in args.wheels]
    for _, line in results:
        print(line)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
