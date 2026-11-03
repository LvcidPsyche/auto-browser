"""Browser containers run with only the capabilities their entrypoint needs.

browser-node/entrypoint.sh runs as root until setpriv drops to the browser
user, and that root phase needs exactly CHOWN, DAC_OVERRIDE, KILL, SETGID and
SETUID. The compose browser-node service and every isolated session container
(session_isolation.py) drop everything else and set no-new-privileges, so
the two lists must not drift apart: an isolated container missing one would
crash-loop or skip its graceful shutdown where the compose one does not.

The compose checks are repo-layout checks (regex, no PyYAML): the controller
image ships only app/ and tests/, so they skip there.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.session_isolation import _BROWSER_NODE_CAP_ADD, DockerBrowserNodeProvisioner
from tests.test_session_isolation import FakeDockerClient, FakeDockerContainer

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
EXPECTED_CAP_ADD = ["CHOWN", "DAC_OVERRIDE", "KILL", "SETGID", "SETUID"]


def _service_block(compose_text: str, service: str) -> str:
    """The lines of one top-level service, up to the next service at the same indent."""
    match = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^\S|\Z)", compose_text)
    assert match, f"no {service} service in docker-compose.yml"
    return match.group(1)


def _flow_list(block: str, key: str) -> list[str] | None:
    """A service-level `key: [A, B]` flow list (4-space indent), unquoted."""
    match = re.search(rf"(?m)^    {re.escape(key)}:\s*\[([^\]]*)\]\s*$", block)
    if match is None:
        return None
    return [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()]


class IsolatedContainerHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_containers_drop_every_capability_but_the_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "data"
            data_root.mkdir(parents=True)
            settings = Settings(
                _env_file=None,
                ARTIFACT_ROOT=str(data_root / "artifacts"),
                ISOLATED_HOST_DATA_ROOT=str(data_root),
            )
            controller = FakeDockerContainer(container_id="controller-id", name="controller", attrs={})
            client = FakeDockerClient(controller)
            provisioner = DockerBrowserNodeProvisioner(settings, client=client)
            provisioner._controller_container_id = controller.id

            runtime = await provisioner.provision("hardened")

            kwargs = client.containers.browser_containers[runtime.container_name].run_kwargs
            self.assertEqual(kwargs["cap_drop"], ["ALL"])
            self.assertEqual(kwargs["cap_add"], EXPECTED_CAP_ADD)
            self.assertEqual(kwargs["security_opt"], ["no-new-privileges:true"])
            # The resource limits stay as they were.
            self.assertEqual(kwargs["pids_limit"], 2048)
            self.assertEqual(kwargs["mem_limit"], "4g")


@unittest.skipUnless(COMPOSE.is_file(), "docker-compose.yml is not shipped inside the controller image")
class ComposeHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = COMPOSE.read_text(encoding="utf-8")

    def test_browser_node_capabilities_match_isolated_containers(self) -> None:
        block = _service_block(self.compose, "browser-node")
        self.assertEqual(_flow_list(block, "cap_drop"), ["ALL"])
        self.assertEqual(_flow_list(block, "cap_add"), list(_BROWSER_NODE_CAP_ADD))
        self.assertEqual(sorted(_BROWSER_NODE_CAP_ADD), EXPECTED_CAP_ADD)

    def test_browser_node_sets_no_new_privileges_and_a_pids_limit(self) -> None:
        block = _service_block(self.compose, "browser-node")
        self.assertEqual(_flow_list(block, "security_opt"), ["no-new-privileges:true"])
        self.assertRegex(block, r"(?m)^    pids_limit: 2048$")

    def test_the_controller_sets_only_no_new_privileges(self) -> None:
        """It keeps its capabilities and root user: a non-root or capability-
        dropped controller breaks existing /data, docker.sock and CLI-auth
        setups."""
        block = _service_block(self.compose, "controller")
        self.assertEqual(_flow_list(block, "security_opt"), ["no-new-privileges:true"])
        for key in ("cap_drop", "cap_add", "user", "read_only", "mem_limit"):
            self.assertNotRegex(block, rf"(?m)^    {key}:", f"controller sets {key}")

    def test_the_parser_sees_the_services(self) -> None:
        # Guards the regexes above against a layout change that would make the
        # parity checks vacuous.
        self.assertIn("healthcheck:", _service_block(self.compose, "browser-node"))
        self.assertIn("stop_grace_period:", _service_block(self.compose, "controller"))


if __name__ == "__main__":
    unittest.main()
