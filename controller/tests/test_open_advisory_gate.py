"""The release gate must actually block on an advisory left in triage.

A gate that only ever runs against a clean repo proves nothing: it looks
identical to a gate that returns "all clear" unconditionally. GHSA-xmh3-cw7j-9gp5
sat unacknowledged for seven weeks, so the blocking case is the one under test
here, with the API response shape taken from a real
`GET /repos/{owner}/{repo}/security-advisories` payload.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import check_open_advisories  # noqa: E402
    from check_open_advisories import age_days, report  # noqa: E402
except ModuleNotFoundError as exc:  # scripts/ is not shipped in the controller image
    raise unittest.SkipTest(f"check_open_advisories unavailable: {exc}") from exc

NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def advisory(state: str, *, created: datetime = NOW, ghsa_id: str = "GHSA-test-0000") -> dict:
    return {
        "ghsa_id": ghsa_id,
        "state": state,
        "severity": "critical",
        "summary": "Test advisory",
        "html_url": f"https://github.com/o/r/security/advisories/{ghsa_id}",
        "created_at": created.isoformat().replace("+00:00", "Z"),
    }


class OpenAdvisoryGateTests(unittest.TestCase):
    def test_triage_advisory_blocks_the_release(self) -> None:
        stale = advisory("triage", created=NOW - timedelta(days=52))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            report([stale], now=NOW)
        self.assertIn("still sit in triage", str(caught.exception))

    def test_waiting_time_is_reported_so_the_message_is_actionable(self) -> None:
        stale = advisory("triage", created=NOW - timedelta(days=52))
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit):
            report([stale], now=NOW)
        self.assertIn("waiting 52 days", stream.getvalue())

    def test_published_advisories_do_not_block(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(report([advisory("published"), advisory("closed")], now=NOW), 0)

    def test_draft_is_reported_but_does_not_block(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(report([advisory("draft")], now=NOW), 0)
        self.assertIn("accepted draft", stream.getvalue())

    def test_empty_repository_passes(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(report([], now=NOW), 0)

    def test_a_summary_the_output_cannot_hold_is_escaped_and_the_release_still_blocked(self) -> None:
        # A reporter's text, written where stdout's encoding has no such character (Windows' ANSI code page on a pipe,
        # as when the release audit's output is captured), ended the check with a UnicodeEncodeError at that summary.
        entry = advisory("triage") | {"summary": "a → b, 日本語"}
        with tempfile.TemporaryDirectory() as temp_dir:
            listed = Path(temp_dir) / "advisories.json"
            listed.write_text(json.dumps([entry], ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(_SCRIPTS / "check_open_advisories.py"), "--from-json", str(listed)],
                capture_output=True,
                env={**os.environ, "PYTHONIOENCODING": "ascii"},
            )
        blocked = "Release blocked" in result.stderr.decode("ascii", "replace")
        self.assertEqual((result.returncode, blocked, b"a \\u2192 b, \\u65e5" in result.stdout), (1, True, True))

    def test_age_days_tolerates_a_missing_timestamp(self) -> None:
        self.assertIsNone(age_days(None, NOW))
        # The real GHSA-xmh3-cw7j-9gp5 submission timestamp: 51 whole days by
        # 2026-08-08T00:00Z, and the count truncates rather than rounds.
        self.assertEqual(age_days("2026-06-17T04:40:04Z", NOW), 51)


class FetchAdvisoriesTests(unittest.TestCase):
    """`gh api --paginate` prints each page as its own JSON array, one after another, which is not one JSON document.

    With `--slurp` it prints one array of the pages, and the check reads every advisory on every page.
    """

    def _fetch(self, stdout: str, returncode: int = 0) -> tuple[list[dict], list[str]]:
        completed = subprocess.CompletedProcess(["gh"], returncode, stdout, "HTTP 404")
        with (
            mock.patch.object(check_open_advisories.shutil, "which", return_value="/usr/bin/gh"),
            mock.patch.object(check_open_advisories.subprocess, "run", return_value=completed) as run,
        ):
            advisories = check_open_advisories.fetch_advisories("o/r")
        return advisories, run.call_args.args[0]

    def test_every_page_is_read(self) -> None:
        first, second = advisory("published", ghsa_id="GHSA-aaaa"), advisory("triage", ghsa_id="GHSA-bbbb")
        advisories, argv = self._fetch(json.dumps([[first], [second]]))
        self.assertIn("--paginate", argv)
        self.assertIn("--slurp", argv)
        self.assertEqual(advisories, [first, second])

    def test_a_triage_advisory_on_a_later_page_blocks_the_release(self) -> None:
        pages = [[advisory("published", ghsa_id=f"GHSA-{n:04d}") for n in range(30)], [advisory("triage")]]
        advisories, _ = self._fetch(json.dumps(pages))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            report(advisories, now=NOW)

    def test_a_reply_that_is_not_a_list_of_pages_fails(self) -> None:
        for stdout in (json.dumps({"message": "Not Found"}), json.dumps([{"ghsa_id": "GHSA-x"}])):
            with self.subTest(stdout=stdout), self.assertRaisesRegex(SystemExit, "not a list of pages"):
                self._fetch(stdout)

    def test_a_failed_call_still_fails(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Could not list security advisories for o/r"):
            self._fetch("", returncode=1)

    def test_an_advisory_s_text_is_read_as_the_utf_8_gh_prints(self) -> None:
        # Decoded in Windows' ANSI code page (cp1252), the 0x9D that ends ” and the 0x81 of Á or of a Cyrillic letter
        # stopped the read, and the check died with a TypeError; other text read garbled.
        run = subprocess.run
        summaries = [
            'a "quoted" word',
            "a “quoted” word",
            "Árvore",
            "сервер",
            "日本語",
        ]
        for summary in summaries:
            printed = json.dumps([[advisory("triage") | {"summary": summary}]], ensure_ascii=False).encode()
            # Not gh: a child that prints what gh would, read through subprocess with the check's own arguments.
            child = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(bytes.fromhex({printed.hex()!r}))"]

            def gh(argv: list[str], child: list[str] = child, **kwargs: object) -> subprocess.CompletedProcess:
                return run(child, **kwargs)

            with (
                self.subTest(summary=summary),
                mock.patch.object(check_open_advisories.shutil, "which", return_value="/usr/bin/gh"),
                mock.patch.object(check_open_advisories.subprocess, "run", gh),
            ):
                self.assertEqual(check_open_advisories.fetch_advisories("o/r")[0]["summary"], summary)


SECURITY = _REPO / "SECURITY.md"
WORKFLOWS = _REPO / ".github" / "workflows"


@unittest.skipUnless(SECURITY.is_file() and WORKFLOWS.is_dir(), "the repository root is not in this tree")
class AdvisoryCheckWordingTests(unittest.TestCase):
    """The check runs in the release audit, which is run by hand: the texts must say so while no workflow runs it."""

    def test_the_texts_say_where_the_check_runs(self) -> None:
        in_ci = any(
            name in path.read_text(encoding="utf-8")
            for path in WORKFLOWS.glob("*.yml")
            for name in ("check_open_advisories", "release_audit")
        )
        texts = {"SECURITY.md": SECURITY.read_text(encoding="utf-8"), "docstring": check_open_advisories.__doc__ or ""}
        for label, text in texts.items():
            flat = " ".join(text.split())
            with self.subTest(label):
                self.assertEqual("CI does not run it" in flat, not in_ci)
                if not in_ci:
                    self.assertNotIn("guaranteed", flat)


if __name__ == "__main__":
    unittest.main()
