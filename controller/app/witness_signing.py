"""Ed25519 signing for witness receipt chains.

The receipt chain was SHA-256 linked but unsigned, which meant it detected
*accidental* corruption and nothing else: anyone holding the JSONL could edit a
receipt, recompute every downstream hash, and produce a chain that verified
perfectly. Receipts proved nothing to anyone outside the box that wrote them.

Signing each receipt's `chain_hash` fixes that. Because `chain_hash` already
covers the entire preceding chain, a signature over it attests to both the
receipt and its whole history up to that point.

Deliberately not solved here — state it rather than let a reader assume it:

* **Tail truncation.** Dropping the last k receipts leaves a shorter chain whose
  every signature is still valid. Detecting that needs an external anchor (a
  published head, a countersigning peer). `verify()` reports the signed head so
  a caller holding a previously-published head can check for themselves.
* **Key compromise.** A holder of the private key can rewrite history and
  re-sign it. This raises the bar from "anyone with the file" to "anyone with
  the key", which is the point, but it is not tamper-proof storage.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger(__name__)

SIGNATURE_ALGORITHM = "ed25519"


class WitnessSigner:
    """Signs receipt chain hashes with this deployment's Ed25519 identity.

    Reuses `mesh.identity.NodeIdentity` rather than managing a second keypair:
    it already handles generation, 0600 permissions, and node_id tamper
    detection, and is a leaf module with no mesh runtime dependency. Witness
    signing therefore works whether or not the mesh subsystem is enabled.
    """

    def __init__(self, identity_dir: str | Path) -> None:
        from .mesh.identity import NodeIdentity

        self._identity = NodeIdentity(Path(identity_dir))

    @property
    def key_id(self) -> str:
        """SHA-256 of the raw public key — stable, and safe to publish."""
        return self._identity.node_id

    @property
    def public_key_b64(self) -> str:
        return self._identity.pubkey_b64

    def sign(self, chain_hash: str) -> str:
        return base64.b64encode(self._identity.sign(chain_hash.encode("utf-8"))).decode("ascii")


def verify_signature(*, public_key_b64: str, chain_hash: str, signature_b64: str) -> bool:
    """Verify one receipt signature against a public key.

    Free function, not a method: verification must be possible for anyone
    holding only the exported bundle, with no signer and no private key.
    """
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        public_key.verify(base64.b64decode(signature_b64), chain_hash.encode("utf-8"))
        return True
    except Exception:
        return False
