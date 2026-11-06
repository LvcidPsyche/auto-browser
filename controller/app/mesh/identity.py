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
import os
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

_KEY_FILE_MODE = 0o600
_IDENTITY_DIR_MODE = 0o700
_GROUP_OR_WORLD_BITS = 0o077
_POSIX = os.name == "posix"


def _mode_bits(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _owned_by_this_process(path: Path) -> bool:
    return path.stat().st_uid == os.geteuid()


def _write_private_file(path: Path, data: bytes) -> None:
    # Created at 0600 from the first byte (O_EXCL: never reuse a file someone else put there), so the key is never
    # readable under the umask between a write and a chmod.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, _KEY_FILE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


class NodeIdentity:
    """Loaded (or generated) Ed25519 identity for this mesh node."""

    def __init__(self, identity_dir: Path) -> None:
        self._dir = identity_dir
        self._dir.mkdir(parents=True, exist_ok=True, mode=_IDENTITY_DIR_MODE)
        if _POSIX:
            self._tighten_a_loose_directory()

        priv_path = self._dir / _PRIVKEY_FILE
        pub_path = self._dir / _PUBKEY_FILE
        meta_path = self._dir / _META_FILE

        if priv_path.exists():
            logger.info("mesh.identity: loading existing keypair from %s", self._dir)
            if _POSIX and _mode_bits(priv_path) & _GROUP_OR_WORLD_BITS:
                logger.warning(
                    "mesh.identity: private key %s is readable by group or others (mode %o); run chmod 600 on it",
                    priv_path,
                    _mode_bits(priv_path),
                )
            pem = priv_path.read_bytes()
            self._private_key = Ed25519PrivateKey.from_private_bytes(self._extract_raw_ed25519(pem))
        else:
            logger.info("mesh.identity: generating new keypair in %s", self._dir)
            self._private_key = Ed25519PrivateKey.generate()
            # Write private key
            priv_pem = self._private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            _write_private_file(priv_path, priv_pem)

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

        # A consistency check, not tamper detection: meta.json sits in the same directory as the key, so whoever
        # can replace the key can replace it too. It catches a corrupt file or keys mixed from two identities.
        stored = json.loads(meta_path.read_text())
        if stored.get("node_id") != self._node_id:
            raise RuntimeError(
                "mesh.identity: stored node_id does not match the key (corrupt or mixed identity directory): "
                f"stored={stored.get('node_id')!r} derived={self._node_id!r}"
            )

        logger.info("mesh.identity: node_id=%s", self._node_id)

    def _tighten_a_loose_directory(self) -> None:
        """mkdir's mode applies only to a directory it creates: one an older release made at 0755 keeps that mode.

        Ours, holding only the identity files: tightened to 0700, with one warning. One that holds anything else,
        another user's, or one chmod refuses: a warning only.
        """
        mode = _mode_bits(self._dir)
        if not mode & _GROUP_OR_WORLD_BITS:
            return
        others = self._other_entries()
        if others:
            # MESH_IDENTITY_DIR pointed at a shared folder (/data/mesh, /data): a chmod 700 there would lock out
            # whatever else reads it (A-add-10). The key file itself is still created at 0600.
            logger.warning(
                "mesh.identity: identity directory %s is open to group or others (mode %o) and holds other files "
                "(%s), so its mode is left; give the identity a directory of its own (MESH_IDENTITY_DIR)",
                self._dir,
                mode,
                ", ".join(sorted(others)[:5]),
            )
            return
        if not _owned_by_this_process(self._dir):
            logger.warning(
                "mesh.identity: identity directory %s is open to group or others (mode %o) and belongs to another "
                "user; run chmod 700 on it",
                self._dir,
                mode,
            )
            return
        try:
            os.chmod(self._dir, _IDENTITY_DIR_MODE)
        except OSError as exc:
            logger.warning(
                "mesh.identity: identity directory %s is open to group or others (mode %o) and could not be "
                "tightened (%s); run chmod 700 on it",
                self._dir,
                mode,
                type(exc).__name__,
            )
            return
        kept = _mode_bits(self._dir)
        if kept & _GROUP_OR_WORLD_BITS:
            # A mount that fakes its modes (a Windows folder through Docker Desktop shows 0777) takes the chmod and
            # keeps the mode it shows.
            logger.warning(
                "mesh.identity: identity directory %s is open to group or others (mode %o), and chmod 700 did not "
                "change that: the filesystem may not keep modes",
                self._dir,
                kept,
            )
            return
        logger.warning(
            "mesh.identity: identity directory %s was open to group or others (mode %o); tightened it to 700",
            self._dir,
            mode,
        )

    def _other_entries(self) -> list[str]:
        """The names in the identity directory that are not the identity's own files (an unlistable one counts)."""
        own = {_PRIVKEY_FILE, _PUBKEY_FILE, _META_FILE}
        try:
            return [entry.name for entry in self._dir.iterdir() if entry.name not in own]
        except OSError as exc:
            return [f"<unlistable: {type(exc).__name__}>"]

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
