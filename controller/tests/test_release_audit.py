"""release_audit must build and inspect every package the release workflow publishes.

release.yml builds client/, integrations/langchain and packaging/auto-browser-mcp. The
audit used to build only three of the four directories it lists today, so a broken
metapackage first failed at tag time.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import release_audit  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"release_audit unavailable: {exc}") from exc

RELEASE_WORKFLOW = _REPO / ".github" / "workflows" / "release.yml"
GOOD_METADATA = (
    "Metadata-Version: 2.4\nName: auto-browser-mcp\nVersion: 1.0.0\nRequires-Dist: auto-browser-client>=1.0.0\n"
)
GOOD_ENTRY_POINTS = "[console_scripts]\nauto-browser-mcp = auto_browser_client.mcp_bridge:main\n"


def _write_wheel(wheelhouse: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(wheelhouse / "auto_browser_mcp-1.0.0-py3-none-any.whl", "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _good_files() -> dict[str, str]:
    return {
        "auto_browser_mcp-1.0.0.dist-info/METADATA": GOOD_METADATA,
        "auto_browser_mcp-1.0.0.dist-info/entry_points.txt": GOOD_ENTRY_POINTS,
        "auto_browser_mcp-1.0.0.dist-info/RECORD": "",
    }


class ReleaseWorkflowCoverageTests(unittest.TestCase):
    @unittest.skipUnless(RELEASE_WORKFLOW.is_file(), "release workflow is not in this tree")
    def test_every_package_release_yml_builds_is_built_by_the_audit(self) -> None:
        built = re.findall(r"python -m build --outdir dist (\S+)", RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("packaging/auto-browser-mcp", built)
        missing = [directory for directory in built if directory not in release_audit.WHEEL_PACKAGES]
        self.assertEqual(missing, [])


class InspectMcpWheelTests(unittest.TestCase):
    def _inspect(self, files: dict[str, str] | None) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            wheelhouse = Path(temp_dir)
            if files is not None:
                _write_wheel(wheelhouse, files)
            release_audit._inspect_mcp_wheel(wheelhouse)

    def test_accepts_the_expected_metapackage(self) -> None:
        self._inspect(_good_files())

    def test_rejects_a_missing_wheel(self) -> None:
        with self.assertRaisesRegex(SystemExit, "MCP metapackage wheel was not built"):
            self._inspect(None)

    def test_rejects_python_modules(self) -> None:
        files = _good_files() | {"stray.py": "print('x')\n"}
        with self.assertRaisesRegex(SystemExit, "stray.py"):
            self._inspect(files)

    def test_rejects_missing_client_dependency(self) -> None:
        files = _good_files() | {"auto_browser_mcp-1.0.0.dist-info/METADATA": "Name: auto-browser-mcp\n"}
        with self.assertRaisesRegex(SystemExit, "Requires-Dist: auto-browser-client"):
            self._inspect(files)

    def test_rejects_wrong_console_script(self) -> None:
        files = _good_files() | {
            "auto_browser_mcp-1.0.0.dist-info/entry_points.txt": (
                "[console_scripts]\nauto-browser-mcp = somewhere_else:main\n"
            )
        }
        with self.assertRaisesRegex(SystemExit, "auto_browser_client.mcp_bridge:main"):
            self._inspect(files)

    def test_rejects_missing_entry_points(self) -> None:
        files = _good_files()
        del files["auto_browser_mcp-1.0.0.dist-info/entry_points.txt"]
        with self.assertRaisesRegex(SystemExit, "auto_browser_client.mcp_bridge:main"):
            self._inspect(files)


if __name__ == "__main__":
    unittest.main()
