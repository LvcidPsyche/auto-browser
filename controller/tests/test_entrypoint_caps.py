"""controller/entrypoint.sh's capability handling, run for real under bash with a fake /proc/self/status.

A copy of the entrypoint reads a fake status file, and stub id/python/setpriv/getent on PATH record what it would
exec. Covers the branch for a container that cannot switch users (no CAP_SETUID/CAP_SETGID): it clears the bounding set
when CAP_SETPCAP allows, and otherwise names the capabilities that remain instead of claiming none; and the strict
parse of CapEff/CapBnd (exit 70 on anything but 16 hex digits).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_scripts_token_argv import BASH, _bash_path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"
DOCKER_DEFAULT = "00000000a80425fb"  # Docker's default set, as root sees it
NO_SETUID = "00000000a804257b"  # cap_drop: [SETUID]
ALL_DROPPED = "0000000000000000"  # cap_drop: [ALL]
CHOWN_DAC_FOWNER = "000000000000000b"  # cap_drop ALL + cap_add CHOWN, DAC_OVERRIDE, FOWNER (no SETUID, no SETPCAP)
SETUID_NO_SETPCAP = "00000000000000c1"  # cap_drop ALL + cap_add CHOWN, SETUID, SETGID

STUBS = {
    "id": '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 0 && exit 0\necho "uid=0(root)"\n',
    "python": '#!/usr/bin/env bash\nprintf "RUN_UID=10002\\nRUN_GID=10002\\nRUN_REASON=test reason\\n"\n',
    # `setpriv <drop> test -r <file>` is the entrypoint's read check under the dropped capabilities: its answer is
    # FAKE_READABLE (1 by default); any other setpriv call is the final exec, echoed.
    "setpriv": (
        '#!/usr/bin/env bash\nfor arg in "$@"; do if [ "$arg" = test ]; then [ "${FAKE_READABLE:-1}" = 1 ]; exit $?; '
        'fi; done\necho "EXEC setpriv $*"\n'
    ),
    # The owner of /data as the entrypoint reads it: FAKE_DATA_OWNER, else the real stat.
    "stat": '#!/usr/bin/env bash\nif [ -n "${FAKE_DATA_OWNER:-}" ]; then echo "$FAKE_DATA_OWNER"; exit 0; fi\n'
    'exec /usr/bin/stat "$@"\n',
    "getent": "#!/usr/bin/env bash\nexit 0\n",
    "groupadd": "#!/usr/bin/env bash\nexit 0\n",
    "useradd": "#!/usr/bin/env bash\nexit 0\n",
}


def run_entrypoint(
    cap_eff: str | None,
    cap_bnd: str | None,
    script: Path = ENTRYPOINT,
    *,
    data_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        status = root / "status"
        lines = ["Name:\tentrypoint"]
        if cap_eff is not None:
            lines.append(f"CapEff:\t{cap_eff}")
        if cap_bnd is not None:
            lines.append(f"CapBnd:\t{cap_bnd}")
        status.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stubs = root / "stubs"
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text(body, encoding="utf-8", newline="\n")
            (stubs / name).chmod(0o755)
        copy = root / "entrypoint.sh"
        copy.write_text(
            script.read_text(encoding="utf-8").replace("/proc/self/status", _bash_path(status)),
            encoding="utf-8",
            newline="\n",
        )
        result = subprocess.run(
            [
                BASH,
                "-c",
                # tests/__init__.py points API_TOKEN_FILE at a temp file: the entrypoint must look in data_root.
                "unset API_TOKEN_FILE; "
                f'export PATH="{_bash_path(stubs)}:$PATH"; '
                f'export CONTROLLER_DATA_ROOT="{_bash_path(data_root) if data_root else "/nonexistent-data"}"; '
                + "".join(f"export {key}='{value}'; " for key, value in (env or {}).items())
                + f'bash "{_bash_path(copy)}" uvicorn',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode, result.stdout + result.stderr


@unittest.skipUnless(BASH and ENTRYPOINT.is_file(), "no bash, or the entrypoint is not beside the tests (the image)")
class EntrypointCapabilityTests(unittest.TestCase):
    def test_docker_default_drops_to_10002_with_the_bounding_set_cleared(self) -> None:
        code, out = run_entrypoint(DOCKER_DEFAULT, DOCKER_DEFAULT)
        self.assertEqual(code, 0, out)
        self.assertIn("EXEC setpriv --reuid=10002 --regid=10002", out)
        self.assertIn("--bounding-set=-all", out)

    def test_cap_drop_all_runs_as_uid_0_with_no_capabilities_and_names_the_way_out(self) -> None:
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED)
        self.assertEqual(code, 0, out)
        self.assertIn("running as uid 0 with no capabilities", out)
        self.assertIn('user: "10002:10002"', out)
        # The set measured to work for 1.13.0 data and a pre-release's 10001-owned 0700 directories.
        self.assertIn("or add cap_add CHOWN, DAC_READ_SEARCH, SETUID and SETGID.", out)
        self.assertIn("EXEC setpriv --inh-caps=-all --ambient-caps=-all --no-new-privs uvicorn", out)

    def _data(self, token: bool) -> Path:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        if token:
            (root / "api-token").write_text("t", encoding="utf-8")
        return root

    def test_cap_drop_all_refuses_when_the_token_cannot_be_read(self) -> None:
        # P3#3, the measured failure: a token the dropped uid 0 cannot read. One clear line and exit 70, not a
        # restart loop of PermissionError tracebacks.
        data = self._data(token=True)
        code, out = run_entrypoint(
            ALL_DROPPED, ALL_DROPPED, data_root=data, env={"FAKE_DATA_OWNER": "10002", "FAKE_READABLE": "0"}
        )
        self.assertEqual(code, 70, out)
        self.assertIn("entrypoint: ERROR:", out)
        self.assertIn("cannot read the API token", out)
        self.assertIn('user: "10002:10002"', out)
        self.assertNotIn("EXEC setpriv --inh-caps", out)

    def test_cap_drop_all_on_another_uids_data_warns_it_will_likely_fail(self) -> None:
        for token, readable in ((False, "0"), (True, "1")):
            with self.subTest(token=token, readable=readable):
                code, out = run_entrypoint(
                    ALL_DROPPED,
                    ALL_DROPPED,
                    data_root=self._data(token),
                    env={"FAKE_DATA_OWNER": "10002", "FAKE_READABLE": readable},
                )
                self.assertEqual(code, 0, out)
                self.assertIn("will likely fail", out)
                self.assertIn("cannot read its private files", out)
                self.assertIn("EXEC setpriv --inh-caps=-all", out)
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"FAKE_DATA_OWNER": "0"})
        self.assertNotIn("will likely fail", out)

    def test_a_browser_owned_data_dir_points_to_10002_not_10001(self) -> None:
        # P3#4: never tell the operator to run the controller as browser-node's uid.
        code, out = run_entrypoint(
            ALL_DROPPED, ALL_DROPPED, data_root=self._data(False), env={"FAKE_DATA_OWNER": "10001"}
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn('"10001:10001"', out)
        self.assertNotIn("--user 10001", out)
        self.assertIn("chown ./data to 10002", out)

    def test_dac_read_search_alone_can_read_but_not_write(self) -> None:
        # P3#4: the read claim follows bits 1 (DAC_OVERRIDE) and 2 (DAC_READ_SEARCH); the write side is separate.
        read_only = "0000000000000004"  # cap_drop ALL + cap_add DAC_READ_SEARCH: no SETPCAP, so it remains
        code, out = run_entrypoint(read_only, read_only, data_root=self._data(False), env={"FAKE_DATA_OWNER": "10002"})
        self.assertEqual(code, 0, out)
        self.assertNotIn("cannot read its private files", out)
        self.assertIn("cannot write into it", out)

    def test_a_bad_controller_run_as_is_refused_on_this_branch_too(self) -> None:
        # P3#13: the cannot-switch branch never runs app.run_as, so bash checks the value itself.
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": "yes"})
        self.assertEqual(code, 70, out)
        self.assertIn("CONTROLLER_RUN_AS must be auto or root", out)
        for good in ("auto", "root", " Root ", ""):
            with self.subTest(value=good):
                code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": good})
                self.assertEqual(code, 0, out)

    def test_setuid_missing_only_clears_the_bounding_set(self) -> None:
        # SETPCAP is still there, so nothing need remain.
        code, out = run_entrypoint(NO_SETUID, NO_SETUID)
        self.assertEqual(code, 0, out)
        self.assertIn("--bounding-set=-all", out)
        self.assertIn("running as uid 0 with no capabilities", out)
        self.assertNotIn("--reuid", out)

    def test_cap_add_without_setuid_names_what_remains(self) -> None:
        code, out = run_entrypoint(CHOWN_DAC_FOWNER, CHOWN_DAC_FOWNER)
        self.assertEqual(code, 0, out)
        self.assertNotIn("no capabilities", out)
        self.assertIn("capabilities 000000000000000b (chown dac_override fowner)", out)
        self.assertNotIn("--bounding-set", out)

    def test_setpcap_is_probed_in_the_effective_set(self) -> None:
        # In the bounding set but not effective: setpriv could not clear the bounding set, so it is not asked to.
        code, out = run_entrypoint(SETUID_NO_SETPCAP, DOCKER_DEFAULT)
        self.assertEqual(code, 0, out)
        self.assertIn("EXEC setpriv --reuid=10002", out)
        self.assertNotIn("--bounding-set", out)

    def test_an_empty_or_non_hex_capability_line_stops_the_container(self) -> None:
        for cap_eff, cap_bnd in ((None, DOCKER_DEFAULT), (DOCKER_DEFAULT, None), ("zz", DOCKER_DEFAULT), ("", "")):
            with self.subTest(cap_eff=cap_eff, cap_bnd=cap_bnd):
                code, out = run_entrypoint(cap_eff, cap_bnd)
                self.assertEqual(code, 70, out)
                self.assertNotIn("EXEC setpriv", out)


if __name__ == "__main__":
    unittest.main()
