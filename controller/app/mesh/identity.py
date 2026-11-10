"""
mesh.identity — Ed25519 node identity.

Each auto-browser instance generates a keypair on first start.
The node_id is the hex-encoded SHA-256 of the public key bytes.
Private key lives on disk at 0600; never transmitted.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

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

from ..utils import atomic_write_text

logger = logging.getLogger(__name__)

_PRIVKEY_FILE = "node_private.pem"
_PUBKEY_FILE = "node_public.pem"
_META_FILE = "node_meta.json"
# The files published whole (atomic_write_text), and how the temp file such a publish keeps beside one ends.
_PUBLISHED_FILES = (_PUBKEY_FILE, _META_FILE)
_TEMP_SUFFIX = ".tmp"

_KEY_FILE_MODE = 0o600
_IDENTITY_DIR_MODE = 0o700
_GROUP_OR_WORLD_BITS = 0o077
_POSIX = os.name == "posix"
# A start can find an identity file that another start is still writing: how long it retries the read, and how often.
# Far longer than that moment, and short, as a file that stays unusable still fails the start.
_FILE_BEING_WRITTEN_WAIT_SECONDS = 2.0
_FILE_BEING_WRITTEN_POLL_SECONDS = 0.05

_T = TypeVar("_T")


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


def _tighten_a_loose_key(path: Path) -> None:
    """Take group and other bits off the private key, as the token files are, with one warning; never fails the load.

    Opened without following a symlink and changed through that descriptor, so a path swapped between the check and
    the chmod cannot aim it at another file (a symlinked key is only reported: its target is not ours). A key that
    cannot be tightened is still loaded, as before, with a warning that it stays readable.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if exc.errno == errno.ELOOP and _mode_bits(path) & _GROUP_OR_WORLD_BITS:
            logger.warning(
                "mesh.identity: private key %s is a symlink to a file readable by group or others (mode %o); it is "
                "loaded as it is: restrict it where it is mounted",
                path,
                _mode_bits(path),
            )
        return
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if not mode & _GROUP_OR_WORLD_BITS:
            return
        try:
            os.fchmod(fd, _KEY_FILE_MODE)
            if stat.S_IMODE(os.fstat(fd).st_mode) & _GROUP_OR_WORLD_BITS:
                raise OSError("the filesystem did not keep the new mode")
        except OSError as exc:
            logger.warning(
                "mesh.identity: private key %s is readable by group or others (mode %o) and could not be tightened "
                "(%s); it is loaded and stays readable: run chmod 600 on it",
                path,
                mode,
                exc,
            )
            return
    finally:
        os.close(fd)
    logger.warning(
        "mesh.identity: private key %s was readable by group or others (mode %o); changed it to 0600", path, mode
    )


def _is_a_publish_under_way(name: str) -> bool:
    """Whether ``name`` is the temp file atomic_write_text keeps beside a published file until it is whole.

    ``.<file>.<random>.tmp``, for the two files written that way. The private key is not: it is created in place.
    """
    for published in _PUBLISHED_FILES:
        prefix = f".{published}."
        if name.startswith(prefix) and name.endswith(_TEMP_SUFFIX) and len(name) > len(prefix) + len(_TEMP_SUFFIX):
            return True
    return False


def _read_while_another_start_writes(read: Callable[[], _T]) -> _T:
    """``read``, retried for a short bound: another start may be writing the file it reads.

    A start can find the private key empty or part-written (_write_private_file creates it, then fills it), by either
    way in (the file was already there, or the exclusive create lost), and on Windows a file another start is replacing
    refuses a reader for an instant. Still failing at the end, it raises as it would have. Blocks: this runs before the
    controller serves requests, in the lifespan or in the manager's __init__.
    """
    deadline = time.monotonic() + _FILE_BEING_WRITTEN_WAIT_SECONDS
    while True:
        try:
            return read()
        except (ValueError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(_FILE_BEING_WRITTEN_POLL_SECONDS)


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
            self._private_key = self._load_private_key(priv_path)
        else:
            logger.info("mesh.identity: generating new keypair in %s", self._dir)
            self._private_key = Ed25519PrivateKey.generate()
            # Write private key
            priv_pem = self._private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            try:
                _write_private_file(priv_path, priv_pem)
            except FileExistsError:
                # Another start made the key between the check and the exclusive create: that key is this node's.
                logger.info("mesh.identity: another start created the keypair in %s; loading it", self._dir)
                self._private_key = self._load_private_key(priv_path)

        self._public_key: Ed25519PublicKey = self._private_key.public_key()
        pub_raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)  # 32 bytes

        # Write/verify public key file. Published whole, as is the meta below: another start can be reading them.
        if not pub_path.exists():
            pub_pem = self._public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            atomic_write_text(pub_path, pub_pem.decode("ascii"))

        self._node_id = hashlib.sha256(pub_raw).hexdigest()
        self._pubkey_b64 = base64.b64encode(pub_raw).decode()

        # Write/verify meta
        if not meta_path.exists():
            atomic_write_text(meta_path, json.dumps({"node_id": self._node_id, "pubkey_b64": self._pubkey_b64}))

        # A consistency check, not tamper detection: meta.json sits in the same directory as the key, so whoever
        # can replace the key can replace it too. It catches a corrupt file or keys mixed from two identities.
        stored = _read_while_another_start_writes(lambda: json.loads(meta_path.read_text()))
        if stored.get("node_id") != self._node_id:
            raise RuntimeError(
                "mesh.identity: stored node_id does not match the key (corrupt or mixed identity directory): "
                f"stored={stored.get('node_id')!r} derived={self._node_id!r}"
            )

        logger.info("mesh.identity: node_id=%s", self._node_id)

    def _load_private_key(self, priv_path: Path) -> Ed25519PrivateKey:
        # The mode is put right once. Only the read and the parse are tried again while another start writes the
        # file: a key that cannot be tightened would otherwise write its warning at every try.
        if _POSIX:
            _tighten_a_loose_key(priv_path)
        return _read_while_another_start_writes(
            lambda: Ed25519PrivateKey.from_private_bytes(self._extract_raw_ed25519(priv_path.read_bytes()))
        )

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
        """The names in the identity directory that are not the identity's own files (an unlistable one counts).

        A publish under way is the identity's own too: another start's, or what a killed start left.
        """
        own = {_PRIVKEY_FILE, _PUBKEY_FILE, _META_FILE}
        try:
            names = [entry.name for entry in self._dir.iterdir()]
            return [name for name in names if name not in own and not _is_a_publish_under_way(name)]
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
