"""
mesh.peers — Peer registry backed by a JSON file the host operator owns.

/mesh/receive trusts whoever this file lists, so the file is also how a peer is revoked, and
revoking has to be reliable:

- A missing file is an empty registry.
- A file that cannot be read, is not JSON, is not an object, or holds any entry that is not a
  valid peer accepts nobody (the whole file is refused, not just the bad entry), and is logged.
  The registry stays empty until the file is valid again.
- Every lookup stats the file (modification time, size and identity), so an edit applies to the
  next lookup with no restart.
- The controller never writes the file while serving. `last_seen` is kept in memory, so a save
  can neither overwrite the host's edit nor bring a revoked peer back. `add` and `remove` are for
  tests and host tooling only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .models import PeerRecord

logger = logging.getLogger(__name__)

# (modification time in ns, size, identity) of the file as last seen.
_Signature = tuple[int, int, int]
ED25519_PUBLIC_KEY_BYTES = 32


def _signature_of(stat: os.stat_result) -> _Signature:
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _check_entry(key: str, peer: PeerRecord) -> None:
    """A node_id is the SHA-256 of the node's Ed25519 key, as /mesh/identity reports both.

    Before 1.10.2 any API credential could post a key of its own under a known node_id; an entry
    that went through that keeps the id and grants a review would recognise, but not this.
    """
    try:
        raw = base64.b64decode(peer.pubkey_b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{key}: pubkey_b64 is not base64") from None
    if len(raw) != ED25519_PUBLIC_KEY_BYTES:
        raise ValueError(f"{key}: pubkey_b64 is not a {ED25519_PUBLIC_KEY_BYTES}-byte Ed25519 public key")
    if peer.node_id != hashlib.sha256(raw).hexdigest():
        raise ValueError(f"{key}: node_id is not the SHA-256 of pubkey_b64 (lower-case hex)")
    if key != peer.node_id:
        raise ValueError(f"{key}: the entry is filed under a key other than its node_id")


class PeerRegistryFile:
    """JSON-backed peer registry."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # None when there is no file, or it could not be read (so the next lookup tries again).
        self._signature: Optional[_Signature] = None
        self._peers: dict[str, PeerRecord] = {}
        self._last_seen: dict[str, float] = {}
        self._reload_if_changed()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fail_closed(self, reason: str, signature: Optional[_Signature]) -> None:
        self._peers = {}
        self._signature = signature
        logger.warning("mesh.peers: %s — no peer is accepted until %s is valid", reason, self._path)

    def _reload_if_changed(self) -> None:
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            # No file is an empty registry; it is only worth a log line when peers just lost it.
            if self._signature is not None:
                self._fail_closed("the peers file is gone", None)
            return
        except OSError as exc:
            self._fail_closed(f"the peers file cannot be checked ({exc})", None)
            return
        signature = _signature_of(stat)
        if signature == self._signature:
            return
        # The signature is taken before the read, so a write in between is picked up next time.
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            self._fail_closed(f"the peers file cannot be read ({exc})", None)
            return
        except (ValueError, RecursionError) as exc:
            # RecursionError: json.loads on a deeply nested file; /mesh/receive needs no credential,
            # so that must be a refusal logged once for this version, not a traceback per request.
            self._fail_closed(f"the peers file is not valid JSON ({exc})", signature)
            return
        try:
            if not isinstance(raw, dict):
                raise ValueError("the top level must be an object keyed by node_id")
            peers = {key: PeerRecord(**value) for key, value in raw.items()}
            for key, peer in peers.items():
                _check_entry(key, peer)
        except (TypeError, ValueError) as exc:
            self._fail_closed(f"the peers file has an invalid entry ({exc})", signature)
            return
        self._peers = peers
        self._signature = signature
        self._last_seen = {key: seen for key, seen in self._last_seen.items() if key in peers}
        logger.debug("mesh.peers: loaded %d peers from %s", len(peers), self._path)

    def _with_last_seen(self, key: str, peer: PeerRecord) -> PeerRecord:
        seen = self._last_seen.get(key)
        return peer if seen is None else peer.model_copy(update={"last_seen": seen})

    def _save(self) -> None:
        data = {k: v.model_dump() for k, v in self._peers.items()}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
            self._signature = _signature_of(os.stat(self._path))
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
        return self._with_last_seen(node_id, self._peers[node_id])

    def get_optional(self, node_id: str) -> Optional[PeerRecord]:
        self._reload_if_changed()
        peer = self._peers.get(node_id)
        return None if peer is None else self._with_last_seen(node_id, peer)

    def all(self) -> list[PeerRecord]:
        self._reload_if_changed()
        return [self._with_last_seen(key, peer) for key, peer in self._peers.items()]

    def update_last_seen(self, node_id: str) -> None:
        """Remember in memory when a peer was last heard from; the file is not touched."""
        self._reload_if_changed()
        if node_id in self._peers:
            self._last_seen[node_id] = time.time()

    def add(self, peer: PeerRecord) -> None:
        """Write a peer into the file. The controller never calls this: tests and host tooling do."""
        _check_entry(peer.node_id, peer)
        self._reload_if_changed()
        self._peers[peer.node_id] = peer
        self._save()
        logger.info("mesh.peers: added peer node_id=%s endpoint=%s", peer.node_id, peer.endpoint)

    def remove(self, node_id: str) -> bool:
        """Delete a peer from the file. The controller never calls this: tests and host tooling do."""
        self._reload_if_changed()
        if node_id not in self._peers:
            return False
        del self._peers[node_id]
        self._save()
        logger.info("mesh.peers: removed peer node_id=%s", node_id)
        return True
