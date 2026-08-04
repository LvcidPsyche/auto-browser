"""
mesh.identity — Ed25519 node identity.

Each auto-browser instance generates a keypair on first start.
The node_id is the hex-encoded SHA-256 of the public key bytes.
Private key lives on disk at 0600; never transmitted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

logger = logging.getLogger(__name__)

_PRIVKEY_FILE = "node_private.pem"
_PUBKEY_FILE = "node_public.pem"
_META_FILE = "node_meta.json"


class NodeIdentity:
    """Loaded (or generated) Ed25519 identity for this mesh node."""

    def __init__(self, identity_dir: Path) -> None:
        self._dir = identity_dir
        self._dir.mkdir(parents=True, exist_ok=True)

        priv_path = self._dir / _PRIVKEY_FILE
        pub_path = self._dir / _PUBKEY_FILE
        meta_path = self._dir / _META_FILE

        if priv_path.exists():
            logger.info("mesh.identity: loading existing keypair from %s", self._dir)
            pem = priv_path.read_bytes()
            self._private_key = Ed25519PrivateKey.from_private_bytes(self._extract_raw_ed25519(pem))
        else:
            logger.info("mesh.identity: generating new keypair in %s", self._dir)
            self._private_key = Ed25519PrivateKey.generate()
            # Write private key
            priv_pem = self._private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            priv_path.write_bytes(priv_pem)
            priv_path.chmod(0o600)

        self._public_key: Ed25519PublicKey = self._private_key.public_key()
        pub_raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)  # 32 bytes

        # Write/verify public key file
        if not pub_path.exists():
            pub_pem = self._public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            pub_path.write_bytes(pub_pem)

        self._node_id = hashlib.sha256(pub_raw).hexdigest()
        self._pubkey_b64 = base64.b64encode(pub_raw).decode()

        # Write/verify meta
        if not meta_path.exists():
            meta_path.write_text(json.dumps({"node_id": self._node_id, "pubkey_b64": self._pubkey_b64}))

        # Tamper detection — re-derive node_id from current public key and compare to stored meta
        stored = json.loads(meta_path.read_text())
        if stored.get("node_id") != self._node_id:
            raise RuntimeError(
                f"mesh.identity: node_id mismatch — stored={stored.get('node_id')!r} "
                f"derived={self._node_id!r}. Key material may have been tampered with."
            )

        logger.info("mesh.identity: node_id=%s", self._node_id)

    @staticmethod
    def _extract_raw_ed25519(pem: bytes) -> bytes:
        """Extract 32-byte raw private key from PKCS8 PEM."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(pem, password=None)
        return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def pubkey_b64(self) -> str:
        return self._pubkey_b64

    def sign(self, data: bytes) -> bytes:
        """Sign arbitrary bytes, returning 64-byte Ed25519 signature."""
        return self._private_key.sign(data)

    def verify_self(self, data: bytes, signature: bytes) -> bool:
        """Verify a signature made by this node's own key."""
        try:
            self._public_key.verify(signature, data)
            return True
        except Exception:
            return False
