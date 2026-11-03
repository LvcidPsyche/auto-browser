"""The controller never runs as root by default (CONTROLLER_RUN_AS=auto|root).

controller/entrypoint.sh starts as root only to call app.run_as, which picks the user (the owner of /data when that
is neither root nor the browser's uid 10001, so the host user keeps its files and its 0600 api-token; otherwise the
image's controller user, uid 10002), hands that user what earlier releases left in the controller's own /data
subtrees through directory fds, and then the entrypoint execs uvicorn through setpriv.

The ownership walk and the marker use dir fds (os.fwalk, dir_fd=), which exist on Linux only: those tests run on
CI's Linux hosts and inside the controller image (as root and as a user), and skip on Windows. Run as a non-root user,
they hand entries from the current uid to itself, which shows exactly what the walk touches without needing root.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.readiness import run_readiness_checks
from app.run_as import (
    BROWSER_UID,
    CONTROLLER_UID,
    MARKER_NAME,
    controller_owned_paths,
    controller_user_check,
    decide_user,
    fix_ownership,
    main,
    marker_bytes,
    read_marker,
    write_marker,
)

REPO = Path(__file__).resolve().parents[2]
LINUX = sys.platform.startswith("linux")
ME = os.getuid() if hasattr(os, "getuid") else -1
MY_GID = os.getgid() if hasattr(os, "getgid") else -1


class DecideUserTests(unittest.TestCase):
    def test_a_host_user_owned_data_dir_runs_as_that_user(self) -> None:
        decision = decide_user(
            "auto", data_root=Path("/data"), owner_uid=1001, owner_gid=1001, is_system_group=lambda g: False
        )
        self.assertEqual((decision.uid, decision.gid), (1001, 1001))
        self.assertIn("the owner of /data (uid 1001)", decision.reason)

    def test_a_root_owned_data_dir_runs_as_10002(self) -> None:
        decision = decide_user("auto", data_root=Path("/data"), owner_uid=0, owner_gid=0)
        self.assertEqual((decision.uid, decision.gid), (CONTROLLER_UID, CONTROLLER_UID))
        self.assertEqual(CONTROLLER_UID, 10002)
        self.assertIn("belongs to root", decision.reason)

    def test_never_the_browsers_uid(self) -> None:
        # A data dir a 1.14 pre-release handed to 10001: the controller must not share browser-node's uid.
        decision = decide_user("auto", data_root=Path("/data"), owner_uid=BROWSER_UID, owner_gid=BROWSER_UID)
        self.assertEqual((decision.uid, decision.gid), (10002, 10002))
        self.assertIn("browser's uid 10001", decision.reason)

    def test_the_primary_gid_is_never_0_or_a_system_group(self) -> None:
        zero = decide_user(
            "auto", data_root=Path("/data"), owner_uid=1001, owner_gid=0, is_system_group=lambda g: False
        )
        self.assertEqual((zero.uid, zero.gid), (1001, 1001))
        self.assertIn("its group 0 is root", zero.reason)
        shadow = decide_user(
            "auto", data_root=Path("/data"), owner_uid=1001, owner_gid=42, is_system_group=lambda g: g == 42
        )
        self.assertEqual((shadow.uid, shadow.gid), (1001, 1001))
        self.assertIn("its group 42 is a system group", shadow.reason)
        user_group = decide_user(
            "auto", data_root=Path("/data"), owner_uid=1001, owner_gid=121, is_system_group=lambda g: False
        )
        self.assertEqual(user_group.gid, 121)

    @unittest.skipUnless(LINUX, "grp is POSIX")
    def test_the_default_system_group_check_reads_the_images_groups(self) -> None:
        # gid 0 exists everywhere; on Debian (the image) gid 42 is shadow.
        from app.run_as import _is_image_system_group

        self.assertTrue(_is_image_system_group(0))
        self.assertFalse(_is_image_system_group(60000))

    def test_root_is_an_explicit_choice(self) -> None:
        decision = decide_user("root", data_root=Path("/data"), owner_uid=1001, owner_gid=121)
        self.assertIsNone(decision.uid)
        self.assertIn("CONTROLLER_RUN_AS=root", decision.reason)

    def test_anything_else_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "CONTROLLER_RUN_AS must be auto or root, got 'yes'"):
            decide_user("yes", data_root=Path("/data"), owner_uid=0, owner_gid=0)


def _settings(data: Path, **overrides) -> Settings:
    names = {
        "API_TOKEN_FILE": "api-token",
        "ARTIFACT_ROOT": "artifacts",
        "UPLOAD_ROOT": "uploads",
        "AUTH_ROOT": "auth",
        "APPROVAL_ROOT": "approvals",
        "AUDIT_ROOT": "audit",
        "WITNESS_ROOT": "witness",
        "SESSION_STORE_ROOT": "sessions",
        "JOB_STORE_ROOT": "jobs",
        "COMPLIANCE_MANIFEST_PATH": "compliance-manifest.json",
        "HARNESS_ROOT": "harness",
        "MEMORY_ROOT": "memory",
        "ISOLATED_TUNNEL_KEY_PATH": "ssh/id_ed25519",
        "ISOLATED_TUNNEL_KNOWN_HOSTS_PATH": "ssh/known_hosts",
        "ISOLATED_TUNNEL_INFO_ROOT": "tunnels/sessions",
        "REMOTE_ACCESS_INFO_PATH": "tunnels/reverse-ssh.json",
        "MCP_SESSION_STORE_PATH": "mcp/sessions.json",
        "CLI_HOME": "cli-home",
        "CRON_STORE_PATH": "crons/crons.json",
        "BROWSER_WS_ENDPOINT_FILE": "browser-profile/browser-ws-endpoint.txt",
    }
    values = {key: str(data / value) for key, value in names.items()}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class OwnedPathsTests(unittest.TestCase):
    def test_the_controllers_own_subtrees_and_nothing_of_the_browsers(self) -> None:
        data = Path("/data")
        paths = controller_owned_paths(_settings(data), data)
        names = sorted(str(path.relative_to(data)).replace("\\", "/") for path in paths)
        self.assertEqual(
            names,
            [
                "api-token",
                "approvals",
                "artifacts",
                "audit",
                "auth",
                "browser-sessions",
                "cli-home",
                "compliance-manifest.json",
                "crons",
                "db",
                "harness",
                "jobs",
                "mcp",
                "memory",
                "sessions",
                "ssh",
                "tunnels",
                "uploads",
                "witness",
            ],
        )
        self.assertNotIn(data / "browser-profile", paths)
        self.assertNotIn(data / "downloads", paths)

    def test_paths_outside_the_data_root_are_never_included(self) -> None:
        data = Path("/data")
        paths = controller_owned_paths(_settings(data, ARTIFACT_ROOT="/srv/artifacts", CLI_HOME="/"), data)
        self.assertTrue(all(path.is_relative_to(data) and path != data for path in paths), paths)
        self.assertNotIn(Path("/srv/artifacts"), paths)


def _tree(root: Path, *relatives: str) -> None:
    for relative in relatives:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


@unittest.skipUnless(LINUX, "dir-fd walk: Linux only")
class FixOwnershipLinuxTests(unittest.TestCase):
    """Real files and real chown, handing entries from the current uid to itself."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.outside = Path(self.tmp.name) / "outside"
        self.data.mkdir()
        self.outside.mkdir()
        _tree(self.outside, "secret", "deep/secret2")

    def _fix(self, paths, **kwargs):
        return fix_ownership(self.data, [self.data / p for p in paths], ME, MY_GID, from_uids=frozenset({ME}), **kwargs)

    def test_hands_over_the_tree_and_nothing_owned_by_others(self) -> None:
        _tree(self.data, "artifacts/a/1.png", "auth/p.json", "api-token")
        report = self._fix(["artifacts", "auth", "api-token"])
        self.assertEqual(
            sorted(report.changed_paths),
            [".", "api-token", "artifacts", "artifacts/a", "artifacts/a/1.png", "auth", "auth/p.json"],
        )
        none = fix_ownership(self.data, [self.data / "artifacts"], ME, MY_GID, from_uids=frozenset({ME + 12345}))
        self.assertEqual(none.changed_paths, [])

    def test_a_planted_symlink_is_changed_itself_and_never_followed(self) -> None:
        _tree(self.data, "artifacts/a.png")
        os.symlink(self.outside, self.data / "artifacts" / "link", target_is_directory=True)
        os.symlink(self.outside / "secret", self.data / "artifacts" / "filelink")
        report = self._fix(["artifacts"])
        self.assertIn("artifacts/link", report.changed_paths)
        self.assertFalse(any("secret" in p or p.startswith("artifacts/link/") for p in report.changed_paths), report)

    def test_a_top_that_is_a_symlink_is_not_walked(self) -> None:
        os.symlink(self.outside, self.data / "artifacts", target_is_directory=True)
        report = self._fix(["artifacts"])
        self.assertEqual(sorted(report.changed_paths), [".", "artifacts"])

    def test_a_directory_swapped_for_a_symlink_mid_walk_is_not_entered(self) -> None:
        _tree(self.data, "artifacts/a/1.png", "artifacts/b/2.png")

        def swap(base: str) -> None:
            # After artifacts/ was read and b handled, before fwalk descends into b.
            if base == "artifacts":
                (self.data / "artifacts" / "b").rename(self.data / "artifacts" / "b.real")
                os.symlink(self.outside, self.data / "artifacts" / "b", target_is_directory=True)

        report = self._fix(["artifacts"], after_dir=swap)
        self.assertFalse(any(p.startswith("artifacts/b/") for p in report.changed_paths), report.changed_paths)
        self.assertFalse(any("secret" in p for p in report.changed_paths))

    def test_hard_linked_files_are_skipped_and_reported(self) -> None:
        _tree(self.data, "artifacts/one")
        os.link(self.data / "artifacts" / "one", self.data / "artifacts" / "two")
        report = self._fix(["artifacts"])
        self.assertNotIn("artifacts/one", report.changed_paths)
        self.assertNotIn("artifacts/two", report.changed_paths)
        self.assertIn("artifacts/one: 2 hard links", report.skipped)

    def test_a_sub_mount_is_skipped_and_not_entered(self) -> None:
        _tree(self.data, "auth/mounted/inner.json", "auth/own.json")

        def fake_lstat(name, *, dir_fd, follow_symlinks):
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if name == "mounted":
                fields = list(st)
                fields[2] = st.st_dev + 1  # st_dev
                return os.stat_result(fields)
            return st

        report = self._fix(["auth"], lstat=fake_lstat)
        self.assertIn("auth/own.json", report.changed_paths)
        self.assertFalse(any(p.startswith("auth/mounted") for p in report.changed_paths), report.changed_paths)
        self.assertTrue(any(s.startswith("auth/mounted: another filesystem") for s in report.skipped))

    def test_the_browsers_directories_are_never_entered(self) -> None:
        _tree(
            self.data,
            "browser-sessions/s1/profile/Cookies",
            "browser-sessions/s1/downloads/f.pdf",
            "browser-sessions/s1/notes.json",
        )
        report = self._fix(["browser-sessions"])
        self.assertIn("browser-sessions/s1/notes.json", report.changed_paths)
        self.assertFalse(any("/profile" in p or "/downloads" in p for p in report.changed_paths), report.changed_paths)

    def test_a_deeper_configured_path_needs_root_owned_parents(self) -> None:
        if ME == 0:
            self.skipTest("as root the parent is root-owned")
        _tree(self.data, "x/artifacts/a.png")
        report = self._fix(["x/artifacts"])
        self.assertEqual(sorted(report.changed_paths), ["."])
        self.assertTrue(any("not a root-owned directory" in e for e in report.errors), report.errors)


@unittest.skipUnless(LINUX, "dir-fd marker: Linux only")
class MarkerLinuxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.target = Path(self.tmp.name) / "target"
        self.target.write_bytes(b"precious\n")

    def test_round_trip(self) -> None:
        write_marker(self.data, ME, MY_GID)
        self.assertEqual(read_marker(self.data, ME), marker_bytes(ME, MY_GID))

    def test_a_symlink_is_no_marker_and_writing_replaces_it_without_touching_its_target(self) -> None:
        self.target.write_bytes(marker_bytes(ME, MY_GID))
        os.symlink(self.target, self.data / MARKER_NAME)
        self.assertIsNone(read_marker(self.data, ME))
        write_marker(self.data, ME, MY_GID)
        self.assertFalse((self.data / MARKER_NAME).is_symlink())
        self.assertEqual(self.target.read_bytes(), marker_bytes(ME, MY_GID))  # unchanged, not truncated

    def test_a_hard_link_is_replaced_not_truncated(self) -> None:
        os.link(self.target, self.data / MARKER_NAME)
        write_marker(self.data, ME, MY_GID)
        self.assertEqual(self.target.read_bytes(), b"precious\n")

    def test_a_fifo_is_no_marker_and_does_not_hang(self) -> None:
        os.mkfifo(self.data / MARKER_NAME)
        self.assertIsNone(read_marker(self.data, ME))

    def test_non_utf8_content_is_just_a_mismatch(self) -> None:
        (self.data / MARKER_NAME).write_bytes(b"\xff\xfe\x00")
        self.assertEqual(read_marker(self.data, ME), b"\xff\xfe\x00")
        self.assertNotEqual(read_marker(self.data, ME), marker_bytes(ME, MY_GID))

    def test_another_owner_or_a_big_file_is_no_marker(self) -> None:
        write_marker(self.data, ME, MY_GID)
        self.assertIsNone(read_marker(self.data, ME + 1))
        (self.data / MARKER_NAME).unlink()
        (self.data / MARKER_NAME).write_bytes(b"1" * 4096)
        self.assertIsNone(read_marker(self.data, ME))


def _lines(text: str) -> list[str]:
    return text.splitlines()


@unittest.skipUnless(LINUX, "the root step is Linux only")
class MainLinuxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def _main(self, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out), self.assertLogs("app.run_as", level=logging.INFO) as logs:
            code = main(settings=_settings(self.data), data_root=self.data, **kwargs)
        return code, _lines(out.getvalue()), [r.getMessage() for r in logs.records]

    def test_three_strict_lines_then_a_marker_and_a_skipped_second_start(self) -> None:
        if ME in (0, BROWSER_UID):
            self.skipTest("needs a data dir owned by an ordinary uid")
        code, lines, messages = self._main(run_as="auto")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^RUN_UID=[0-9]+$")
        self.assertRegex(lines[1], r"^RUN_GID=[0-9]+$")
        self.assertRegex(lines[2], r"^RUN_REASON=[^\n]+$")
        self.assertEqual(lines[0], f"RUN_UID={ME}")
        notice = next(i for i, m in enumerate(messages) if "one-time" in m)
        done = next(i for i, m in enumerate(messages) if "handed to it" in m)
        self.assertLess(notice, done)
        self.assertIn("a minute or two on Docker Desktop bind mounts", messages[notice])
        self.assertTrue((self.data / MARKER_NAME).is_file())
        _, _, messages = self._main(run_as="auto")
        self.assertTrue(any("handed over already" in m for m in messages))

    def test_a_non_utf8_marker_rescans_instead_of_crashing(self) -> None:
        if ME in (0, BROWSER_UID):
            self.skipTest("needs a data dir owned by an ordinary uid")
        (self.data / MARKER_NAME).write_bytes(b"\xff\xfe\x00")
        code, lines, messages = self._main(run_as="auto")
        self.assertEqual(code, 0)
        self.assertTrue(any("one-time" in m for m in messages))

    def test_a_root_run_removes_the_marker_and_prints_no_user(self) -> None:
        (self.data / MARKER_NAME).write_bytes(b"1:1\n")
        out = io.StringIO()
        with redirect_stdout(out), self.assertLogs("app.run_as", level=logging.WARNING):
            main(run_as="root", settings=_settings(self.data), data_root=self.data)
        self.assertEqual(
            _lines(out.getvalue()), ["RUN_UID=", "RUN_GID=", "RUN_REASON=CONTROLLER_RUN_AS=root (not recommended)"]
        )
        self.assertFalse((self.data / MARKER_NAME).exists())

    def test_a_bad_value_exits_64(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertLogs("app.run_as", level=logging.ERROR):
            self.assertEqual(main(run_as="yes", settings=_settings(self.data), data_root=self.data), 64)


class ReadinessTests(unittest.TestCase):
    def test_root_warns_and_says_not_recommended(self) -> None:
        check = controller_user_check(SimpleNamespace(euid=0, reason="CONTROLLER_RUN_AS=root"))
        self.assertEqual(check.name, "controller_user")
        self.assertEqual(check.status, "warn")
        self.assertIn("root", check.message)
        self.assertIn("CONTROLLER_RUN_AS=auto", check.fix)

    def test_non_root_passes_with_the_uid_and_why(self) -> None:
        check = controller_user_check(SimpleNamespace(euid=1001, reason="the owner of /data (uid 1001)"))
        self.assertEqual(check.status, "pass")
        self.assertIn("uid 1001", check.message)
        self.assertIn("the owner of /data (uid 1001)", check.message)
        self.assertEqual(check.details, {"uid": 1001, "reason": "the owner of /data (uid 1001)"})

    def test_readiness_includes_it_when_given(self) -> None:
        user = SimpleNamespace(euid=10001, reason="x")
        report = run_readiness_checks(Settings(_env_file=None), controller_user=user)
        self.assertIn("controller_user", [check.name for check in report.checks])


@unittest.skipUnless((REPO / "controller" / "entrypoint.sh").is_file(), "the entrypoint is not in the controller image")
class ImageWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dockerfile = (REPO / "controller" / "Dockerfile").read_text(encoding="utf-8")
        self.entrypoint = (REPO / "controller" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_the_image_has_the_users_and_the_entrypoint(self) -> None:
        self.assertIn("useradd --uid 10002", self.dockerfile)
        self.assertRegex(self.dockerfile, r'(?m)^ENTRYPOINT \["/entrypoint\.sh"\]$')
        self.assertIn("python -m app.run_as", self.entrypoint)
        self.assertIn("docker.sock", self.entrypoint)
        self.assertNotRegex(self.entrypoint, r"chown -R")

    def test_nothing_in_the_image_is_writable_through_group_0_alone(self) -> None:
        # The controller joins group 0 for Docker Desktop's root:root socket.
        self.assertIn(
            "RUN find / -xdev -group 0 -perm -g+w ! -type l ! -perm -1000 -exec chmod g-w {} +", self.dockerfile
        )

    def test_no_eval_and_a_strict_parse(self) -> None:
        self.assertNotRegex(self.entrypoint, r"(?m)^\s*eval\b")
        self.assertIn("^RUN_UID=([0-9]*)$", self.entrypoint)
        self.assertIn("^RUN_GID=([0-9]*)$", self.entrypoint)
        self.assertIn("RUN_REASON=", self.entrypoint)

    def test_setpriv_drops_every_capability_and_sets_no_new_privs(self) -> None:
        self.assertRegex(self.entrypoint, r"--inh-caps=-all")
        self.assertRegex(self.entrypoint, r"--ambient-caps=-all")
        self.assertRegex(self.entrypoint, r"--no-new-privs")
        self.assertRegex(self.entrypoint, r"--bounding-set=-all")
        # Only when CAP_SETPCAP (bit 8) is in the bounding set, so a cap_drop ALL run still starts.
        self.assertIn("1 << 8", self.entrypoint)

    def test_ci_checks_the_data_owner_the_token_and_the_dropped_capabilities(self) -> None:
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci[ci.index("  compose-smoke:") :]
        self.assertIn('if [ "$token_file" != "600 runner" ]; then', job)
        self.assertIn("docker compose exec -T controller awk '/^Uid:/ {print $2}' /proc/1/status", job)
        self.assertIn('if [ "$controller_uid" != "$data_owner" ] || [ "$controller_uid" = "0" ]; then', job)
        for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            self.assertIn(field, job)
        self.assertIn("NoNewPrivs", job)


@unittest.skipUnless((REPO / "scripts" / "doctor.sh").is_file(), "scripts/ is not in the controller image")
class SlowFirstStartTests(unittest.TestCase):
    def test_doctor_waits_long_enough_for_a_first_hand_over(self) -> None:
        doctor = (REPO / "scripts" / "doctor.sh").read_text(encoding="utf-8")
        match = re.search(r'wait_for_http "http://127\.0\.0\.1:\$\{API_PORT\}/readyz" (\d+)', doctor)
        self.assertIsNotNone(match, "doctor no longer waits on /readyz with an explicit attempt count")
        self.assertGreaterEqual(int(match.group(1)), 240)


class PackageDataTests(unittest.TestCase):
    def test_the_seccomp_files_ship_in_the_controller_package(self) -> None:
        import tomllib

        pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
        globs = pyproject["tool"]["setuptools"]["package-data"]["app"]
        app = Path(__file__).resolve().parents[1] / "app"
        shipped = {path.relative_to(app).as_posix() for pattern in globs for path in app.glob(pattern)}
        for name in ("chromium-sandbox.json", "moby-default.json", "README.md"):
            self.assertIn(f"browser/seccomp/{name}", shipped)


if __name__ == "__main__":
    unittest.main()
