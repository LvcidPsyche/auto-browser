"""scripts/check_chromium_freshness.py, offline: every fetch is a fixture.

Fixtures (tests/fixtures/chromium_freshness/) are real responses, trimmed: Google's versionhistory for Chrome stable
on Linux, and the browsers.json of playwright-core 1.64.1 (packed into a tarball here, as npm serves it).
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_chromium_freshness.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chromium_freshness"
_SECCOMP = Path(__file__).resolve().parents[1] / "app" / "browser" / "seccomp"

pytestmark = pytest.mark.skipif(not _SCRIPT.is_file(), reason="scripts/ is not shipped in the controller image")


def _load():
    spec = importlib.util.spec_from_file_location("check_chromium_freshness_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tarball(browsers: dict) -> bytes:
    payload = json.dumps(browsers).encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/browsers.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _browsers(chromium: str) -> dict:
    browsers = json.loads((_FIXTURES / "playwright-core-1.64.1-browsers.json").read_text(encoding="utf-8"))
    for browser in browsers["browsers"]:
        if browser["name"] == "chromium":
            browser["browserVersion"] = chromium
    return browsers


def _stable(*versions: str) -> bytes:
    if not versions:
        return (_FIXTURES / "versionhistory-linux-stable.json").read_bytes()
    return json.dumps({"versions": [{"version": version} for version in versions]}).encode()


class FakeSources:
    """The npm registry, versionhistory and GitHub/moby, as one fetch function."""

    def __init__(self, *, latest="1.64.1", latest_chromium="156.0.8078.4", stable=b"", moby=None, tag="docker-v29.9.2"):
        self.responses = {
            "https://registry.npmjs.org/playwright-core/1.64.1": json.dumps(
                {"dist": {"tarball": "https://registry.npmjs.org/playwright-core/-/playwright-core-1.64.1.tgz"}}
            ).encode(),
            "https://registry.npmjs.org/playwright-core/-/playwright-core-1.64.1.tgz": _tarball(
                _browsers("156.0.8078.4")
            ),
            "https://registry.npmjs.org/playwright-core/latest": json.dumps({"version": latest}).encode(),
            f"https://registry.npmjs.org/playwright-core/{latest}": json.dumps(
                {"dist": {"tarball": f"https://registry.npmjs.org/playwright-core/-/playwright-core-{latest}.tgz"}}
            ).encode(),
            "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions": stable
            or _stable(),
            "https://api.github.com/repos/moby/moby/releases/latest": json.dumps({"tag_name": tag}).encode(),
            f"https://raw.githubusercontent.com/moby/moby/{tag}/vendor/github.com/moby/profiles/seccomp/default.json": (
                json.dumps(moby).encode() if moby is not None else (_SECCOMP / "moby-default.json").read_bytes()
            ),
        }
        if latest != "1.64.1":
            self.responses[f"https://registry.npmjs.org/playwright-core/-/playwright-core-{latest}.tgz"] = _tarball(
                _browsers(latest_chromium)
            )
        self.fetched: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.fetched.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected fetch {url}")
        return self.responses[url]


class ChromiumFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = _load()

    def _run(self, sources: FakeSources) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.check.run(fetcher=sources)
        return code, out.getvalue()

    def test_reads_the_pinned_version_from_the_lockfile_and_its_bundled_chromium(self) -> None:
        self.assertEqual(self.check.pinned_playwright_core(), "1.64.1")
        sources = FakeSources()
        self.assertEqual(self.check.bundled_chromium("1.64.1", sources), "156.0.8078.4")
        self.assertEqual(self.check.chrome_stable(sources), "156.0.8078.79")

    def test_current_passes_and_prints_the_patch_gap(self) -> None:
        code, out = self._run(FakeSources())
        self.assertEqual(code, 0, out)
        self.assertIn("pinned playwright-core 1.64.1 bundles Chromium 156.0.8078.4", out)
        self.assertIn("Chrome stable for Linux: 156.0.8078.79", out)
        self.assertIn(
            "OK: same build 156.0.8078; stable is 75 patch number(s) ahead (156.0.8078.79 vs 156.0.8078.4)", out
        )
        self.assertIn(
            "OK: the seccomp profile minus its auto-browser entry matches moby's default as docker-v29.9.2", out
        )

    def test_stable_a_major_ahead_fails_naming_both(self) -> None:
        code, out = self._run(FakeSources(stable=_stable("157.0.8100.12", "156.0.8078.79")))
        self.assertEqual(code, 1)
        self.assertIn("STALE: Chrome stable 157.0.8100.12 is a major ahead of the bundled Chromium 156.0.8078.4", out)

    def test_a_newer_playwright_with_a_newer_chromium_fails_naming_both(self) -> None:
        code, out = self._run(FakeSources(latest="1.65.0", latest_chromium="157.0.8100.3"))
        self.assertEqual(code, 1)
        self.assertIn(
            "STALE: playwright-core 1.65.0 bundles Chromium 157.0.8100.3, newer than the Chromium 156.0.8078.4 of the "
            "pinned 1.64.1",
            out,
        )

    def test_a_newer_playwright_with_the_same_chromium_passes(self) -> None:
        code, out = self._run(FakeSources(latest="1.64.2", latest_chromium="156.0.8078.4"))
        self.assertEqual(code, 0, out)

    def test_a_bundled_chromium_newer_than_stable_passes(self) -> None:
        ok, lines, warning = self.check.evaluate_chromium("1.64.1", "157.0.1.0", "1.64.1", "157.0.1.0", "156.0.8078.79")
        self.assertIsNone(warning)
        self.assertTrue(ok)
        self.assertIn("newer than Chrome stable", lines[-1])

    def test_moby_drift_fails_and_says_how(self) -> None:
        moby = json.loads((_SECCOMP / "moby-default.json").read_text(encoding="utf-8"))
        drifted = copy.deepcopy(moby)
        drifted["syscalls"][0]["names"].append("some_new_syscall")
        code, out = self._run(FakeSources(moby=drifted, tag="docker-v30.0.0"))
        self.assertEqual(code, 1)
        self.assertIn("DRIFT:", out)
        self.assertIn("as docker-v30.0.0 vendors it", out)
        self.assertIn("some_new_syscall", out)
        self.assertIn("Refresh it", out)

    def test_the_auto_browser_entry_is_not_drift_but_any_other_change_is(self) -> None:
        shipped = json.loads((_SECCOMP / "chromium-sandbox.json").read_text(encoding="utf-8"))
        moby = json.loads((_SECCOMP / "moby-default.json").read_text(encoding="utf-8"))
        self.assertEqual(self.check.profile_drift(shipped, moby), [])
        changed = copy.deepcopy(shipped)
        changed["defaultAction"] = "SCMP_ACT_ALLOW"
        self.assertEqual(self.check.profile_drift(changed, moby), ["top-level 'defaultAction' differs"])

    def test_an_unreadable_source_exits_2(self) -> None:
        sources = FakeSources()
        del sources.responses[
            "https://versionhistory.googleapis.com/v1/chrome/platforms/linux/channels/stable/versions"
        ]

        def failing(url: str) -> bytes:
            if "versionhistory" in url:
                raise self.check.SourceError(f"could not fetch {url}: offline")
            return sources(url)

        code, out = self._run(failing)
        self.assertEqual(code, 2)
        self.assertIn("ERROR: could not fetch https://versionhistory", out)


class RobustnessTests(unittest.TestCase):
    """Every source anomaly is exit 2 with an ERROR line, never a traceback (the review's fresh_robust cases)."""

    def setUp(self) -> None:
        self.check = _load()

    def _run(self, sources, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.check.run(fetcher=sources, **kwargs)
        return code, out.getvalue()

    def _assert_error(self, sources, **kwargs) -> str:
        code, out = self._run(sources, **kwargs)
        self.assertEqual(code, 2, out)
        self.assertIn("ERROR:", out)
        self.assertNotIn("Traceback", out)
        return out

    def test_npm_metadata_that_is_a_list(self) -> None:
        sources = FakeSources()
        sources.responses["https://registry.npmjs.org/playwright-core/1.64.1"] = b"[1, 2]"
        self._assert_error(sources)

    def test_a_tarball_that_is_not_gzip(self) -> None:
        sources = FakeSources()
        sources.responses["https://registry.npmjs.org/playwright-core/-/playwright-core-1.64.1.tgz"] = b"not a tarball"
        self._assert_error(sources)

    def test_versionhistory_with_versions_null(self) -> None:
        self._assert_error(FakeSources(stable=b'{"versions": null}'))

    def test_a_lockfile_pinning_a_prerelease(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tempdir:
            lock = Path(tempdir) / "package-lock.json"
            lock.write_text(json.dumps({"packages": {"node_modules/playwright-core": {"version": "1.65.0-alpha"}}}))
            sources = FakeSources()
            sources.responses["https://registry.npmjs.org/playwright-core/1.65.0-alpha"] = sources.responses[
                "https://registry.npmjs.org/playwright-core/1.64.1"
            ]
            self._assert_error(sources, lock_path=lock)

    def test_a_moby_profile_that_is_not_a_profile(self) -> None:
        out = self._assert_error(FakeSources(moby={}))
        self.assertIn("not a seccomp profile", out)

    def test_anything_unexpected_is_still_exit_2(self) -> None:
        def boom(url: str) -> bytes:
            raise ZeroDivisionError("surprise")

        out = self._assert_error(boom)
        self.assertIn("surprise", out)

    def test_fetch_wraps_an_incomplete_read(self) -> None:
        import http.client

        with patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaises(self.check.SourceError):
                self.check.fetch("https://registry.npmjs.org/playwright-core/latest")


class PatchGapWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.check = _load()

    def test_a_gap_over_20_is_a_warning_and_a_summary_line_but_exit_0(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tempdir:
            summary = Path(tempdir) / "summary.md"
            out = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), contextlib.redirect_stdout(out):
                code = self.check.run(fetcher=FakeSources())
            self.assertEqual(code, 0)
            self.assertIn("::warning::", out.getvalue())
            self.assertIn("75 patch", out.getvalue())
            self.assertIn("75 patch", summary.read_text(encoding="utf-8"))

    def test_a_small_gap_is_no_warning(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.check.run(fetcher=FakeSources(stable=_stable("156.0.8078.20")))
        self.assertEqual(code, 0)
        self.assertNotIn("::warning::", out.getvalue())


class TokenTests(unittest.TestCase):
    def test_github_token_goes_to_api_github_com_only(self) -> None:
        check = _load()
        seen: dict[str, object] = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=0):
            seen[request.full_url] = request.get_header("Authorization")
            return Response(b"{}")

        with patch.dict(os.environ, {"GITHUB_TOKEN": "t0k3n"}), patch("urllib.request.urlopen", fake_urlopen):
            check.fetch("https://api.github.com/repos/moby/moby/releases/latest")
            check.fetch("https://registry.npmjs.org/playwright-core/latest")
            check.fetch("https://raw.githubusercontent.com/moby/moby/x/default.json")
        self.assertEqual(seen["https://api.github.com/repos/moby/moby/releases/latest"], "Bearer t0k3n")
        self.assertIsNone(seen["https://registry.npmjs.org/playwright-core/latest"])
        self.assertIsNone(seen["https://raw.githubusercontent.com/moby/moby/x/default.json"])


class DriftGroupingTests(unittest.TestCase):
    def test_a_split_entry_is_reported_once_per_syscall_with_its_args(self) -> None:
        check = _load()
        shipped = json.loads((_SECCOMP / "chromium-sandbox.json").read_text(encoding="utf-8"))
        moby = json.loads((_SECCOMP / "moby-default.json").read_text(encoding="utf-8"))
        upstream = copy.deepcopy(moby)
        # Like moby master did to socket: a syscall leaves the big allow-list for three arg-filtered entries.
        allow = next(entry for entry in upstream["syscalls"] if "getpid" in entry["names"] and not entry.get("args"))
        allow["names"].remove("getpid")
        for value in (1, 2, 10):
            upstream["syscalls"].append(
                {
                    "names": ["getpid"],
                    "action": "SCMP_ACT_ALLOW",
                    "args": [{"index": 0, "value": value, "op": "SCMP_CMP_EQ"}],
                }
            )
        lines = check.profile_drift(shipped, upstream)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("getpid: profile has [SCMP_ACT_ALLOW]"), lines[0])
        self.assertIn("arg0 == 10", lines[0])
        self.assertIn("3 upstream entries", lines[0])


class WorkflowTests(unittest.TestCase):
    def test_weekly_and_on_demand_read_only_token_for_github_only_sha_pinned(self) -> None:
        import re

        workflow = (_REPO / ".github" / "workflows" / "chromium-freshness.yml").read_text(encoding="utf-8")
        release = (_REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  schedule:\n    - cron: ")
        self.assertRegex(workflow, r"(?m)^  workflow_dispatch:")
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: read$")
        self.assertNotIn("secrets.", workflow)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("python scripts/check_chromium_freshness.py", workflow)
        uses = re.findall(r"uses: (\S+ # \S+)", workflow)
        self.assertEqual(len(uses), 2)
        for pinned in uses:
            self.assertRegex(pinned, r"@[0-9a-f]{40} # v")
            self.assertIn(f"uses: {pinned}", release, f"{pinned} is not pinned as release.yml pins it")


if __name__ == "__main__":
    unittest.main()
