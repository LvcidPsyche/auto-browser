"""`require_approval` on a mesh grant is deprecated: nothing can satisfy it, so the peers file says so when loaded."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path

from app.mesh.peers import PeerRegistryFile

PEERS_LOGGER = "app.mesh.peers"


def _entry(grants: list[dict]) -> dict:
    raw = bytes(range(32))
    node_id = hashlib.sha256(raw).hexdigest()
    return {node_id: {"node_id": node_id, "pubkey_b64": base64.b64encode(raw).decode(), "grants": grants}}


class RequireApprovalDeprecationTests(unittest.TestCase):
    def _load(self, grants: list[dict]) -> PeerRegistryFile:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "peers.json"
        path.write_text(json.dumps(_entry(grants)), encoding="utf-8")
        return PeerRegistryFile(path)

    def test_a_grant_that_sets_it_logs_a_warning_naming_the_capability_and_still_loads(self) -> None:
        with self.assertLogs(PEERS_LOGGER, level=logging.WARNING) as logs:
            registry = self._load([{"capability": "tool:browser.get_html", "require_approval": True}])

        message = "\n".join(logs.output)
        self.assertIn("mesh.peers:", message)
        self.assertIn("require_approval", message)
        self.assertIn("tool:browser.get_html", message)
        self.assertIn("approval_required", message)
        (peer,) = registry.all()
        self.assertTrue(peer.grants[0].require_approval)

    def test_a_grant_without_it_logs_nothing(self) -> None:
        with self.assertNoLogs(PEERS_LOGGER, level=logging.WARNING):
            self._load([{"capability": "tool:browser.observe"}, {"capability": "tool:*", "require_approval": False}])


if __name__ == "__main__":
    unittest.main()
