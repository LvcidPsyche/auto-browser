"""
mesh.peers — Peer registry backed by a JSON file.

Hot-reloadable: the file is re-read when its mtime changes.
Atomic writes via a temp file + rename.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .models import PeerRecord

logger = logging.getLogger(__name__)


class PeerRegistryFile:
    """JSON-backed peer registry."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._mtime: float = 0.0
        self._peers: dict[str, PeerRecord] = {}
        self._reload_if_changed()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reload_if_changed(self) -> None:
        if not self._path.exists():
            return
        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return
        try:
            raw = json.loads(self._path.read_text())
            self._peers = {k: PeerRecord(**v) for k, v in raw.items()}
            self._mtime = mtime
            logger.debug("mesh.peers: reloaded %d peers from %s", len(self._peers), self._path)
        except Exception as exc:
            logger.warning("mesh.peers: reload failed — %s", exc)

    def _save(self) -> None:
        data = {k: v.model_dump() for k, v in self._peers.items()}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
            self._mtime = self._path.stat().st_mtime
        except Exception:
            os.unlink(tmp)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, node_id: str) -> PeerRecord:
        """Return a peer by node_id; raise KeyError if unknown."""
        self._reload_if_changed()
        if node_id not in self._peers:
            raise KeyError(f"Unknown peer node_id={node_id!r}")
        return self._peers[node_id]

    def get_optional(self, node_id: str) -> Optional[PeerRecord]:
        self._reload_if_changed()
        return self._peers.get(node_id)

    def all(self) -> list[PeerRecord]:
        self._reload_if_changed()
        return list(self._peers.values())

    def add(self, peer: PeerRecord) -> None:
        self._reload_if_changed()
        self._peers[peer.node_id] = peer
        self._save()
        logger.info("mesh.peers: added peer node_id=%s endpoint=%s", peer.node_id, peer.endpoint)

    def update_last_seen(self, node_id: str) -> None:
        self._reload_if_changed()
        if node_id in self._peers:
            self._peers[node_id].last_seen = time.time()
            self._save()

    def remove(self, node_id: str) -> bool:
        self._reload_if_changed()
        if node_id not in self._peers:
            return False
        del self._peers[node_id]
        self._save()
        logger.info("mesh.peers: removed peer node_id=%s", node_id)
        return True
