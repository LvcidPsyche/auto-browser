"""Lazy relative imports must resolve to modules that actually exist.

`browser.export_script` shipped broken: `tool_gateway/gateway.py` did
`from .playwright_export import export_session_script`, which resolves to
`app.tool_gateway.playwright_export` — a module that does not exist. The real
module is `app.playwright_export`, and the REST route imported it correctly
(`from ..playwright_export`), which hid the difference.

Because the import sat *inside* the handler body, it only ran when an MCP client
called the tool. Every call raised ModuleNotFoundError, the gateway's catch-all
collapsed it into the opaque "Tool execution failed", and no test exercised the
gateway path for that tool — so CI stayed green while a published tool was 100%
broken.

A function-level import is invisible to import-time checks and to linters that
only resolve top-level imports, so nothing else in this repo covers it. This
walks every module in `app/` and resolves every relative import, at any nesting
depth, whether module-level or lazy.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _module_name_for(path: Path) -> str:
    """app/tool_gateway/gateway.py -> app.tool_gateway.gateway"""
    rel = path.relative_to(APP_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _relative_imports(path: Path) -> list[tuple[int, int, str]]:
    """Every `from .x import y` in the file as (lineno, level, module)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno, node.level, node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0
    ]


def _resolve(importer: str, level: int, module: str) -> str:
    """Mirror Python's relative-import resolution."""
    base = importer
    # A module (not a package) consumes one level to reach its own package.
    for _ in range(level):
        base = base.rpartition(".")[0]
    return f"{base}.{module}" if module else base


ALL_MODULES = sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("path", ALL_MODULES, ids=lambda p: _module_name_for(p))
def test_relative_imports_resolve(path: Path) -> None:
    importer = _module_name_for(path)
    is_package = path.name == "__init__.py"

    for lineno, level, module in _relative_imports(path):
        # Inside a package's __init__, level 1 means the package itself.
        effective_level = level - 1 if is_package else level
        target = _resolve(importer, effective_level, module)

        assert importlib.util.find_spec(target) is not None, (
            f"{importer}:{lineno} imports {'.' * level}{module!r}, which resolves "
            f"to {target!r} — that module does not exist. If this import is inside "
            f"a function body it will raise ModuleNotFoundError only when that code "
            f"path runs in production."
        )


def test_walk_actually_covers_the_gateway() -> None:
    """Guard the guard: the scan must reach the file the bug shipped in."""
    names = {_module_name_for(p) for p in ALL_MODULES}
    assert "app.tool_gateway.gateway" in names
    assert len(names) > 100, "expected the whole app package to be walked"
