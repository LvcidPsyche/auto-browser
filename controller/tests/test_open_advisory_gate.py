"""The release gate must actually block on an advisory left in triage.

A gate that only ever runs against a clean repo proves nothing: it looks
identical to a gate that returns "all clear" unconditionally. GHSA-xmh3-cw7j-9gp5
sat unacknowledged for seven weeks, so the blocking case is the one under test
here, with the API response shape taken from a real
`GET /repos/{owner}/{repo}/security-advisories` payload.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
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

    def test_age_days_tolerates_a_missing_timestamp(self) -> None:
        self.assertIsNone(age_days(None, NOW))
        # The real GHSA-xmh3-cw7j-9gp5 submission timestamp: 51 whole days by
        # 2026-08-08T00:00Z, and the count truncates rather than rounds.
        self.assertEqual(age_days("2026-06-17T04:40:04Z", NOW), 51)


if __name__ == "__main__":
    unittest.main()
