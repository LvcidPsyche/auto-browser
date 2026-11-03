"""A closed isolated session leaves no profile (cookies) or downloads behind.

browser-node's entrypoint hands an isolated container's profile/ and downloads/ to its browser user (10001). The
controller runs as 10002 (or a host user), so its rmtree(ignore_errors=True) could not delete them and hid that:
every closed session's cookies stayed on disk. The controller now empties those dirs as the browser user, through a
short --rm helper from the browser image (no network, no capabilities), after the browser container has stopped, then
removes the rest itself, and logs a WARNING with the path when anything remains.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer


class SessionDirCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data = Path(self.tempdir.name) / "data"
        self.data.mkdir()
        self.settings = Settings(
            _env_file=None, ARTIFACT_ROOT=str(self.data / "artifacts"), ISOLATED_HOST_DATA_ROOT=str(self.data)
        )
        self.controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
        self.client = FakeDockerClient(self.controller)
        self.client.containers.sandbox_state = {"sandbox": "on", "verified": {"renderers": 1}}
        self.provisioner = DockerBrowserNodeProvisioner(self.settings, client=self.client)
        self.provisioner._controller_container_id = self.controller.id

    def _session_dir(self, session_id: str) -> Path:
        return self.data / "browser-sessions" / session_id

    def _fill(self, session_id: str) -> None:
        root = self._session_dir(session_id)
        (root / "profile" / "Default").mkdir(parents=True, exist_ok=True)
        (root / "profile" / "Default" / "Cookies").write_text("secret", encoding="utf-8")
        (root / "downloads").mkdir(parents=True, exist_ok=True)
        (root / "downloads" / "statement.pdf").write_text("pdf", encoding="utf-8")

    async def test_release_empties_the_dirs_as_the_browser_user_then_removes_them(self) -> None:
        runtime = await self.provisioner.provision("s1")
        self._fill("s1")
        await self.provisioner.release(runtime)
        self.assertFalse(self._session_dir("s1").exists())
        self.assertEqual(len(self.client.containers.cleanup_runs), 1)
        run = self.client.containers.cleanup_runs[0]
        self.assertEqual(run["user"], "10001:10001")
        self.assertEqual(run["network_mode"], "none")
        self.assertEqual(run["cap_drop"], ["ALL"])
        self.assertTrue(run["remove"])
        self.assertEqual(run["image"], self.settings.isolated_browser_image)
        self.assertEqual(
            sorted(mount["bind"] for mount in run["volumes"].values()), ["/data/downloads", "/data/profile"]
        )
        self.assertIn("find /data/profile /data/downloads -mindepth 1 -delete", " ".join(run["command"]))
        # After the browser container stopped: Chromium writes to its profile as it exits.
        stop_index = next(i for i, event in enumerate(self.client.events) if event[0] == "container.stop")
        self.assertGreater(len(self.client.events), stop_index)

    async def test_anything_left_is_a_warning_with_the_path(self) -> None:
        runtime = await self.provisioner.provision("s2")
        self._fill("s2")
        self.client.containers.cleanup_fails = True
        browser_owned = self._session_dir("s2") / "profile" / "Default"

        with self.assertLogs("app.session_isolation", level=logging.WARNING) as logs:
            import os

            # The controller cannot delete inside a browser-owned dir (it is not 10001).
            real_os_unlink = os.unlink

            def os_unlink(path, *args, **kwargs):
                if Path(path).is_relative_to(browser_owned):
                    raise PermissionError(13, "Permission denied", str(path))
                return real_os_unlink(path, *args, **kwargs)

            os.unlink = os_unlink
            try:
                await self.provisioner.release(runtime)
            finally:
                os.unlink = real_os_unlink
        self.assertTrue((browser_owned / "Cookies").exists())
        text = "\n".join(logs.output)
        self.assertIn(str(self._session_dir("s2").resolve()), text)
        self.assertRegex(text, r"could not remove|left")

    async def test_the_orphan_sweep_empties_leftovers_with_no_container(self) -> None:
        # A previous run's session: its container is gone, its profile is still here.
        self._fill("orphan")
        self.provisioner._reap_orphaned_containers()
        self.assertFalse(self._session_dir("orphan").exists())
        self.assertEqual(len(self.client.containers.cleanup_runs), 1)
        self.assertEqual(self.client.containers.cleanup_runs[0]["user"], "10001:10001")

    async def test_a_dir_already_gone_runs_no_helper(self) -> None:
        import shutil

        runtime = await self.provisioner.provision("s3")
        shutil.rmtree(self._session_dir("s3"))
        await self.provisioner.release(runtime)
        self.assertEqual(self.client.containers.cleanup_runs, [])
        self.assertFalse(self._session_dir("s3").exists())


if __name__ == "__main__":
    unittest.main()
