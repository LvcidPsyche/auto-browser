"""controller/entrypoint.sh's capability handling, run for real under bash with a fake /proc/self/status.

A copy of the entrypoint reads a fake status file, and stub id/python/setpriv/getent on PATH record what it would
exec. Covers the branch for a container that cannot switch users (no CAP_SETUID/CAP_SETGID): it execs through
app.drop_caps with no capability, clears the bounding set when CAP_SETPCAP allows, names what the container still grants
at every start, and asks the controller which token file to check; the other exec paths (non-root, CONTROLLER_RUN_AS=
root); and the strict parse of CapEff/CapBnd (exit 70 on anything but 16 hex digits).
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

# Stub commands on PATH (tests/entrypoint_stubs/): id (FAKE_UID), python (app.run_as's lines), setpriv (records each
# call in FAKE_SETPRIV_LOG; answers the checks it runs: FAKE_SEARCHABLE, FAKE_READABLE), stat (FAKE_DATA_OWNER).
STUB_DIR = Path(__file__).resolve().parent / "entrypoint_stubs"
# What the cannot-switch branch execs through: setpriv's drop, then app.drop_caps for permitted and effective.
DROP_CHAIN = "python -m app.drop_caps --"


def run_entrypoint(
    cap_eff: str | None,
    cap_bnd: str | None,
    script: Path = ENTRYPOINT,
    *,
    data_root: Path | None = None,
    env: dict[str, str] | None = None,
    python_stub: str | None = None,
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
        for stub in STUB_DIR.iterdir():
            (stubs / stub.name).write_text(stub.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            (stubs / stub.name).chmod(0o755)
        if python_stub is not None:
            (stubs / "python").write_text(python_stub, encoding="utf-8", newline="\n")
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
                # tests/__init__.py sets API_TOKEN_FILE and tokenless loopback: each test sets only what it means.
                "unset API_TOKEN_FILE API_BEARER_TOKEN API_BEARER_TOKENS API_ALLOW_TOKENLESS_LOOPBACK; "
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
        self.assertIn(f"EXEC setpriv --inh-caps=-all --ambient-caps=-all --no-new-privs {DROP_CHAIN} uvicorn", out)

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

    def test_the_checks_run_through_the_same_drop_as_the_exec(self) -> None:
        # P4#8: with SETPCAP the drop clears the bounding set; the read check must carry it too, and app.drop_caps.
        log = self._data(False) / "setpriv.log"
        with_setpcap = NO_SETUID  # cap_drop [SETUID]: SETPCAP stays
        code, out = run_entrypoint(
            with_setpcap,
            with_setpcap,
            data_root=self._data(True),
            env={"FAKE_DATA_OWNER": "10002", "FAKE_SETPRIV_LOG": _bash_path(log)},
        )
        self.assertEqual(code, 0, out)
        checks = [line for line in log.read_text(encoding="utf-8").splitlines() if " test " in line]
        self.assertEqual(len(checks), 4, checks)  # -e and -x the directory, -e and -r the token
        for line in checks:
            self.assertTrue(line.startswith("--inh-caps=-all --ambient-caps=-all --no-new-privs --bounding-set=-all"))
            self.assertIn(f"{DROP_CHAIN} test ", line)

    def test_no_refusal_when_the_controller_reads_no_file(self) -> None:
        # Item 4: the controller says which file it reads (app.api_token --file-to-read); nothing, nothing checked.
        code, out = run_entrypoint(
            ALL_DROPPED,
            ALL_DROPPED,
            data_root=self._data(True),
            env={"FAKE_DATA_OWNER": "10002", "FAKE_READABLE": "0", "FAKE_FILE_TO_READ": ""},
        )
        self.assertEqual(code, 0, out)
        self.assertIn("will likely fail", out)

    def test_an_unreachable_token_directory_is_refused_too(self) -> None:
        code, out = run_entrypoint(
            ALL_DROPPED,
            ALL_DROPPED,
            data_root=self._data(True),
            env={"FAKE_DATA_OWNER": "10002", "FAKE_SEARCHABLE": "0"},
        )
        self.assertEqual(code, 70, out)
        # Round 7 (P6#1): only a directory that exists is refused, so the ERROR no longer says it may not exist.
        self.assertIn("cannot reach the API token's directory", out)
        self.assertIn(", so the controller cannot start", out)
        self.assertNotIn("does not exist", out)

    def test_a_missing_token_directory_is_left_to_the_controller(self) -> None:
        # P6#1: on a root-owned /data that uid 0 can write, the controller creates secrets/ and the token in it.
        data = self._data(False)
        code, out = run_entrypoint(
            ALL_DROPPED,
            ALL_DROPPED,
            data_root=data,
            env={
                "FAKE_DATA_OWNER": "0",
                "FAKE_SEARCHABLE": "0",  # what -x says of a directory that is not there
                "FAKE_FILE_TO_READ": _bash_path(data / "secrets" / "api-token"),
            },
        )
        self.assertEqual(code, 0, out)
        self.assertNotIn("ERROR", out)
        self.assertIn("EXEC setpriv", out)

    def test_the_file_the_controller_names_is_the_one_checked(self) -> None:
        elsewhere = self._data(False) / "secrets"
        elsewhere.mkdir()
        (elsewhere / "token").write_text("t", encoding="utf-8")
        code, out = run_entrypoint(
            ALL_DROPPED,
            ALL_DROPPED,
            data_root=self._data(False),  # no token in data_root itself
            env={
                "FAKE_DATA_OWNER": "10002",
                "FAKE_READABLE": "0",
                "FAKE_FILE_TO_READ": _bash_path(elsewhere / "token"),
            },
        )
        self.assertEqual(code, 70, out)
        self.assertIn("secrets/token", out)

    def test_a_root_owned_data_dir_with_an_unreadable_token_is_refused(self) -> None:
        # Item 4: the check runs whatever /data's owner (a root /data holding a 10002-owned 0600 token).
        code, out = run_entrypoint(
            ALL_DROPPED, ALL_DROPPED, data_root=self._data(True), env={"FAKE_DATA_OWNER": "0", "FAKE_READABLE": "0"}
        )
        self.assertEqual(code, 70, out)
        self.assertIn("cannot read the API token", out)

    def test_a_failed_drop_says_so_first(self) -> None:
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, data_root=self._data(True), env={"FAKE_DROP_RC": "1"})
        self.assertEqual(code, 70, out)
        self.assertIn("could not drop this container's capabilities", out)
        self.assertNotIn("API token", out)

    def test_a_failed_helper_is_a_warning_not_a_refusal(self) -> None:
        code, out = run_entrypoint(
            ALL_DROPPED,
            ALL_DROPPED,
            data_root=self._data(True),
            env={"FAKE_DATA_OWNER": "10002", "FAKE_READABLE": "0", "FAKE_HELPER_RC": "1"},
        )
        self.assertEqual(code, 0, out)
        self.assertIn("could not ask the controller which token file it reads", out)

    def test_the_bounding_set_it_still_grants_is_named(self) -> None:
        # Item 1: uid 0 can rewrite the image and regain these after a restart; cap_drop ALL grants none.
        code, out = run_entrypoint(CHOWN_DAC_FOWNER, CHOWN_DAC_FOWNER)
        self.assertEqual(code, 0, out)
        self.assertIn("this container still grants chown dac_override fowner at every start", out)
        # Round 7 (P6#3): under read_only the controller needs one writable temp dir, /tmp; HOME needs nothing.
        self.assertIn("read_only: true with a tmpfs for /tmp; see docs/deployment.md", out)
        self.assertNotIn("/var/tmp", out)
        self.assertNotIn("/home/controller", out)
        code, out = run_entrypoint(NO_SETUID, NO_SETUID)  # SETPCAP clears it here, but a restart grants it again
        self.assertIn("this container still grants", out)
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED)
        self.assertNotIn("still grants", out)

    def test_the_warning_names_the_bounding_set_not_the_effective_set(self) -> None:
        # P6#6: a runtime may start root with an effective set narrower than its bounding set. A restart after the
        # image is rewritten regains the bounding set, so that is the set the WARNING names.
        code, out = run_entrypoint(ALL_DROPPED, CHOWN_DAC_FOWNER)
        self.assertEqual(code, 0, out)
        self.assertIn("this container still grants chown dac_override fowner at every start", out)

    def test_a_capability_without_a_name_is_named_by_its_bit(self) -> None:
        # P6#12, F7: cap_names covers bits 0-63, so a bounding set that is not empty never reads "none".
        code, out = run_entrypoint(ALL_DROPPED, "0000020000000001")  # CAP_CHOWN and bit 41
        self.assertEqual(code, 0, out)
        self.assertIn("this container still grants chown cap_41 at every start", out)
        code, out = run_entrypoint(ALL_DROPPED, "8000000000000000")  # bit 63 alone
        self.assertEqual(code, 0, out)
        self.assertIn("this container still grants cap_63 at every start", out)
        self.assertNotIn("grants none", out)

    def test_a_non_root_start_checks_controller_run_as_too(self) -> None:
        # Item 11: a bad value exits 70 there as well, and root is said to be ignored.
        code, out = run_entrypoint(
            DOCKER_DEFAULT, DOCKER_DEFAULT, env={"FAKE_UID": "10002", "CONTROLLER_RUN_AS": "yes"}
        )
        self.assertEqual(code, 70, out)
        code, out = run_entrypoint(
            DOCKER_DEFAULT, DOCKER_DEFAULT, env={"FAKE_UID": "10002", "CONTROLLER_RUN_AS": "root"}
        )
        self.assertEqual(code, 0, out)
        self.assertIn("CONTROLLER_RUN_AS=root is ignored: this container was started as uid 10002", out)

    def test_controller_run_as_root_is_ignored_with_a_warning_when_it_cannot_switch(self) -> None:
        # P6#11: the cannot-switch branch drops every capability, so CONTROLLER_RUN_AS=root is ignored there too.
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": "root"})
        self.assertEqual(code, 0, out)
        self.assertIn("CONTROLLER_RUN_AS=root is ignored: this container cannot switch users", out)
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": "auto"})
        self.assertEqual(code, 0, out)
        self.assertNotIn("is ignored", out)

    def test_the_root_path_execs_with_no_new_privs(self) -> None:
        stub_python = '#!/usr/bin/env bash\nprintf "RUN_UID=\\nRUN_GID=\\nRUN_REASON=CONTROLLER_RUN_AS=root (not recommended)\\n"\n'
        code, out = run_entrypoint(DOCKER_DEFAULT, DOCKER_DEFAULT, python_stub=stub_python)
        self.assertEqual(code, 0, out)
        self.assertIn("EXEC setpriv --no-new-privs uvicorn", out)

    def test_a_nobody_owned_data_dir_points_to_65534(self) -> None:
        # P4#5: on root-squashed NFS only 65534 can read the data; root's chown cannot work there.
        code, out = run_entrypoint(
            ALL_DROPPED, ALL_DROPPED, data_root=self._data(False), env={"FAKE_DATA_OWNER": "65534"}
        )
        self.assertEqual(code, 0, out)
        self.assertIn('user: "65534:65534"', out)
        self.assertNotIn("chown ./data to 10002", out)

    def test_a_non_root_start_still_gets_no_new_privs(self) -> None:
        # D1: started as a user (compose user:, docker run --user), the entrypoint still execs through setpriv.
        code, out = run_entrypoint(DOCKER_DEFAULT, DOCKER_DEFAULT, env={"FAKE_UID": "10002"})
        self.assertEqual(code, 0, out)
        self.assertIn("EXEC setpriv --no-new-privs --inh-caps=-all --ambient-caps=-all uvicorn", out)

    def test_a_bad_controller_run_as_is_refused_on_this_branch_too(self) -> None:
        # P3#13: the cannot-switch branch never runs app.run_as, so bash checks the value itself.
        code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": "yes"})
        self.assertEqual(code, 70, out)
        self.assertIn("CONTROLLER_RUN_AS must be auto or root", out)
        # P4#10: exactly what app.run_as accepts (strip().lower()): echo's flags and inner spaces are not.
        for bad in ("-n", "-e", "ro ot"):
            with self.subTest(value=bad):
                code, out = run_entrypoint(ALL_DROPPED, ALL_DROPPED, env={"CONTROLLER_RUN_AS": bad})
                self.assertEqual(code, 70, out)
        for good in ("auto", "root", " Root ", "", "AUTO"):
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

    def test_cap_add_without_setuid_still_drops_everything(self) -> None:
        # D2: no SETPCAP, so setpriv cannot clear the bounding set; app.drop_caps empties permitted and effective
        # under no_new_privs before the exec, so "no capabilities" is true here too.
        code, out = run_entrypoint(CHOWN_DAC_FOWNER, CHOWN_DAC_FOWNER)
        self.assertEqual(code, 0, out)
        self.assertIn("running as uid 0 with no capabilities", out)
        self.assertIn(f"--no-new-privs {DROP_CHAIN} uvicorn", out)
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
