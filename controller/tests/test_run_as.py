"""The controller never runs as root by default (CONTROLLER_RUN_AS=auto|root).

controller/entrypoint.sh starts as root only to call app.run_as, which picks the user (the owner of /data when that
is not root, so the host user keeps its files and its 0600 api-token; otherwise the image's controller user, uid
10001), hands that user the root-owned entries of the controller's own /data subtrees (never a blanket chown, never
the browser's dirs, never outside /data), and then the entrypoint execs uvicorn through setpriv.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.readiness import run_readiness_checks
from app.run_as import (
    CONTROLLER_UID,
    controller_owned_paths,
    controller_user_check,
    decide_user,
    fix_ownership,
    main,
)

REPO = Path(__file__).resolve().parents[2]


class DecideUserTests(unittest.TestCase):
    def test_a_host_user_owned_data_dir_runs_as_that_user(self) -> None:
        decision = decide_user("auto", data_root=Path("/data"), owner_uid=1001, owner_gid=121)
        self.assertEqual((decision.uid, decision.gid), (1001, 121))
        self.assertIn("the owner of /data (uid 1001)", decision.reason)

    def test_a_root_owned_data_dir_runs_as_10001(self) -> None:
        decision = decide_user("auto", data_root=Path("/data"), owner_uid=0, owner_gid=0)
        self.assertEqual((decision.uid, decision.gid), (CONTROLLER_UID, CONTROLLER_UID))
        self.assertEqual(CONTROLLER_UID, 10001)
        self.assertIn("belongs to root", decision.reason)

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


class FakeFs:
    """lstat/chown over a dict of path -> uid, for root-owned bookkeeping without root."""

    def __init__(self, owners: dict[str, int], fail: set[str] = frozenset()) -> None:
        self.owners = owners
        self.fail = fail
        self.chowned: list[str] = []

    def owner(self, path: Path) -> int:
        return self.owners[str(path).replace("\\", "/")]

    def chown(self, path: Path, uid: int, gid: int) -> None:
        key = str(path).replace("\\", "/")
        if key in self.fail:
            raise PermissionError(1, "Operation not permitted", key)
        self.chowned.append(key)
        self.owners[key] = uid


class FixOwnershipTests(unittest.TestCase):
    def _tree(self, root: Path) -> None:
        for relative in ("artifacts/a/1.png", "artifacts/a/2.png", "auth/profile.json", "browser-profile/x"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        (root / "api-token").write_text("t", encoding="utf-8")

    def test_only_root_owned_entries_are_handed_over(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._tree(root)
            every = [p for p in [root, *root.rglob("*")]]
            owners = {str(p).replace("\\", "/"): 0 for p in every}
            owners[str(root / "artifacts" / "a" / "2.png").replace("\\", "/")] = 1001
            fs = FakeFs(owners)
            report = fix_ownership(
                root,
                [root / "artifacts", root / "auth", root / "api-token"],
                1001,
                1001,
                owner=fs.owner,
                chown=fs.chown,
            )
            rel = sorted(str(Path(p).relative_to(root)).replace("\\", "/") for p in fs.chowned)
            self.assertEqual(
                rel, [".", "api-token", "artifacts", "artifacts/a", "artifacts/a/1.png", "auth", "auth/profile.json"]
            )
            self.assertEqual(report.changed, 7)
            self.assertEqual(report.errors, [])
            # The browser's dir and the file already owned by the user were not touched.
            self.assertNotIn(str(root / "browser-profile").replace("\\", "/"), fs.chowned)

    def test_a_failed_chown_is_reported_with_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._tree(root)
            owners = {str(p).replace("\\", "/"): 0 for p in [root, *root.rglob("*")]}
            fs = FakeFs(owners, fail={str(root / "auth").replace("\\", "/")})
            report = fix_ownership(root, [root / "auth"], 10001, 10001, owner=fs.owner, chown=fs.chown)
            self.assertEqual(len(report.errors), 1)
            self.assertIn("auth", report.errors[0])

    def test_symlinks_are_not_followed_out_of_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tempdir)
            (root / "artifacts").mkdir()
            (Path(outside) / "secret").write_text("x", encoding="utf-8")
            try:
                os.symlink(outside, root / "artifacts" / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("no symlinks here")
            owners = {str(p).replace("\\", "/"): 0 for p in [root, root / "artifacts", root / "artifacts" / "link"]}
            fs = FakeFs(owners)
            fix_ownership(root, [root / "artifacts"], 10001, 10001, owner=fs.owner, chown=fs.chown)
            self.assertFalse(any("secret" in path for path in fs.chowned))


class MainTests(unittest.TestCase):
    def test_prints_the_users_for_the_entrypoint_and_logs_an_error_naming_the_way_out(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data = Path(tempdir)
            (data / "auth").mkdir()
            owners = {str(data).replace("\\", "/"): 0, str(data / "auth").replace("\\", "/"): 0}
            fs = FakeFs(owners, fail={str(data / "auth").replace("\\", "/")})
            out = io.StringIO()
            with redirect_stdout(out), self.assertLogs("app.run_as", level=logging.ERROR) as logs:
                code = main(
                    run_as="auto",
                    settings=_settings(data),
                    data_root=data,
                    owner=fs.owner,
                    owner_gid=lambda path: 0,
                    chown=fs.chown,
                )
            self.assertEqual(code, 0)
            self.assertIn("RUN_UID=10001\n", out.getvalue())
            self.assertIn("RUN_GID=10001\n", out.getvalue())
            self.assertIn("RUN_REASON=", out.getvalue())
            message = "\n".join(logs.output)
            self.assertIn(str(data / "auth"), message)
            self.assertIn("CONTROLLER_RUN_AS=root", message)

    def test_a_clean_pass_leaves_a_marker_and_the_next_start_skips_the_scan(self) -> None:
        # A Docker Desktop bind mount took 71.66s just to scan 20102 entries on every start: one pass is enough.
        with tempfile.TemporaryDirectory() as tempdir:
            data = Path(tempdir)
            (data / "auth").mkdir()
            owners = {str(data).replace("\\", "/"): 0, str(data / "auth").replace("\\", "/"): 0}
            fs = FakeFs(owners)
            seen: list[Path] = []

            def owner(path: Path) -> int:
                seen.append(path)
                return fs.owner(path)

            with redirect_stdout(io.StringIO()), self.assertLogs("app.run_as", level=logging.INFO):
                main(
                    run_as="auto",
                    settings=_settings(data),
                    data_root=data,
                    owner=owner,
                    owner_gid=lambda p: 0,
                    chown=fs.chown,
                )
            self.assertEqual((data / ".auto-browser-owner").read_text(encoding="utf-8"), "10001:10001\n")
            scanned = len(seen)
            out = io.StringIO()
            with redirect_stdout(out), self.assertLogs("app.run_as", level=logging.INFO) as logs:
                main(
                    run_as="auto",
                    settings=_settings(data),
                    data_root=data,
                    owner=owner,
                    owner_gid=lambda p: 10001,
                    chown=fs.chown,
                )
            self.assertEqual(len(seen) - scanned, 1, "only /data itself is looked at")
            self.assertIn("RUN_UID=10001\n", out.getvalue())
            self.assertIn(".auto-browser-owner", "\n".join(logs.output))

    def test_a_pass_with_errors_leaves_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data = Path(tempdir)
            (data / "auth").mkdir()
            owners = {str(data).replace("\\", "/"): 0, str(data / "auth").replace("\\", "/"): 0}
            fs = FakeFs(owners, fail={str(data / "auth").replace("\\", "/")})
            with redirect_stdout(io.StringIO()), self.assertLogs("app.run_as", level=logging.ERROR):
                main(
                    run_as="auto",
                    settings=_settings(data),
                    data_root=data,
                    owner=fs.owner,
                    owner_gid=lambda p: 0,
                    chown=fs.chown,
                )
            self.assertFalse((data / ".auto-browser-owner").exists())

    def test_a_root_run_removes_the_marker(self) -> None:
        # Root may write root-owned files, so the next non-root start must look again.
        with tempfile.TemporaryDirectory() as tempdir:
            data = Path(tempdir)
            (data / ".auto-browser-owner").write_text("10001:10001\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()), self.assertLogs("app.run_as", level=logging.WARNING):
                main(run_as="root", settings=_settings(data), data_root=data)
            self.assertFalse((data / ".auto-browser-owner").exists())

    def test_root_prints_no_user(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertLogs("app.run_as", level=logging.WARNING):
            code = main(run_as="root", settings=_settings(Path("/data")), data_root=Path("/data"), owner=lambda p: 0)
        self.assertEqual(code, 0)
        self.assertIn("RUN_UID=\n", out.getvalue())


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


@unittest.skipUnless((REPO / "controller" / "entrypoint.sh").is_file() or (REPO / "docker-compose.yml").is_file(), "")
class ImageWiringTests(unittest.TestCase):
    def test_the_image_has_the_user_the_entrypoint_and_setpriv(self) -> None:
        dockerfile = (REPO / "controller" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("useradd --uid 10001", dockerfile)
        self.assertRegex(dockerfile, r'(?m)^ENTRYPOINT \["/entrypoint\.sh"\]$')
        entrypoint = (REPO / "controller" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn("python -m app.run_as", entrypoint)
        self.assertIn("exec setpriv --reuid=", entrypoint)
        self.assertIn("docker.sock", entrypoint)
        self.assertNotRegex(entrypoint, r"chown -R")

    def test_ci_checks_the_controller_runs_as_the_data_owner_and_keeps_the_runners_token(self) -> None:
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        job = ci[ci.index("  compose-smoke:") :]
        self.assertIn('if [ "$token_file" != "600 runner" ]; then', job)
        self.assertIn("docker compose exec -T controller awk '/^Uid:/ {print $2}' /proc/1/status", job)
        self.assertIn('if [ "$controller_uid" != "$data_owner" ] || [ "$controller_uid" = "0" ]; then', job)


if __name__ == "__main__":
    unittest.main()


class FirstPassNoticeTests(unittest.TestCase):
    def test_a_scan_is_announced_as_one_time_before_it_starts(self) -> None:
        # Docker Desktop bind mounts took ~103s for 20102 entries: say so before, not only after.
        with tempfile.TemporaryDirectory() as tempdir:
            data = Path(tempdir)
            owners = {str(data).replace("\\", "/"): 0}
            fs = FakeFs(owners)
            with redirect_stdout(io.StringIO()), self.assertLogs("app.run_as", level=logging.INFO) as logs:
                main(
                    run_as="auto",
                    settings=_settings(data),
                    data_root=data,
                    owner=fs.owner,
                    owner_gid=lambda p: 0,
                    chown=fs.chown,
                )
            messages = [record.getMessage() for record in logs.records]
            notice = next(i for i, m in enumerate(messages) if "one-time" in m)
            done = next(i for i, m in enumerate(messages) if "handed to it" in m)
            self.assertLess(notice, done)
            self.assertIn("a minute or two on Docker Desktop bind mounts", messages[notice])


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
