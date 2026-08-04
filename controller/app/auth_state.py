from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .utils import UTC

# Auth state is cookies plus storage state — kilobytes. Anything larger is not
# a state file, and content-sniffing it is not worth the read.
_MAX_INSPECT_BYTES = 8 * 1024 * 1024


@dataclass
class PreparedAuthState:
    path: Path
    source_info: dict[str, Any]
    cleanup_path: Path | None = None

    def cleanup(self) -> None:
        if self.cleanup_path and self.cleanup_path.exists():
            self.cleanup_path.unlink(missing_ok=True)


class AuthStateManager:
    def __init__(
        self,
        *,
        encryption_key: str | None,
        require_encryption: bool,
        max_age_hours: float,
    ):
        self.encryption_key = encryption_key
        self.require_encryption = require_encryption
        self.max_age_hours = max_age_hours
        self._fernet = Fernet(encryption_key.encode("utf-8")) if encryption_key else None
        if self.require_encryption and self._fernet is None:
            raise RuntimeError("REQUIRE_AUTH_STATE_ENCRYPTION=true but AUTH_STATE_ENCRYPTION_KEY is not set")

    @property
    def encryption_enabled(self) -> bool:
        return self._fernet is not None

    def output_path(self, destination: Path) -> Path:
        if self.encryption_enabled or self.require_encryption:
            if destination.name.endswith(".enc"):
                return destination
            return destination.with_name(f"{destination.name}.enc")
        if destination.name.endswith(".enc"):
            return destination.with_name(destination.name.removesuffix(".enc"))
        return destination

    async def write_storage_state(self, context, destination: Path) -> dict[str, Any]:
        final_path = self.output_path(destination)
        temp_plain = final_path.with_name(f".{final_path.name}.tmp.json")
        await context.storage_state(path=str(temp_plain))

        if self.encryption_enabled or self.require_encryption:
            ciphertext = self._encrypt(temp_plain.read_bytes())
            payload = {
                "version": 1,
                "format": "fernet-json",
                "ciphertext": ciphertext,
            }
            temp_encrypted = final_path.with_suffix(f"{final_path.suffix}.tmp")
            temp_encrypted.write_text(json.dumps(payload), encoding="utf-8")
            temp_encrypted.replace(final_path)
            temp_plain.unlink(missing_ok=True)
        else:
            temp_plain.replace(final_path)

        return self.inspect(final_path)

    def prepare_for_context(self, source_path: Path) -> PreparedAuthState:
        info = self.inspect(source_path)
        if not info["exists"]:
            raise FileNotFoundError(source_path)
        if info["stale"]:
            raise PermissionError(
                f"Auth state is stale ({info['age_hours']}h old, max {info['max_age_hours']}h): {source_path}"
            )
        if not info["encrypted"]:
            if self.require_encryption:
                # REQUIRE_AUTH_STATE_ENCRYPTION was only enforced at
                # construction ("is a key configured?") and never on read, so a
                # plaintext state file loaded happily and its cookies went
                # straight into a live browser context. The setting promised
                # encrypted-at-rest and silently did not deliver it.
                raise PermissionError(
                    "REQUIRE_AUTH_STATE_ENCRYPTION=true but this auth state is not encrypted: "
                    f"{source_path}. Re-save it with an encryption key configured."
                )
            return PreparedAuthState(path=source_path, source_info=info)
        if self._fernet is None:
            raise RuntimeError("Encrypted auth state provided but AUTH_STATE_ENCRYPTION_KEY is not configured")

        payload = json.loads(source_path.read_text(encoding="utf-8"))
        plaintext = self._fernet.decrypt(payload["ciphertext"].encode("utf-8"))
        fd, temp_name = tempfile.mkstemp(suffix=".json", prefix="auth-state-", dir=str(source_path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        temp_path.write_bytes(plaintext)
        return PreparedAuthState(path=temp_path, source_info=info, cleanup_path=temp_path)

    def inspect(self, path: Path | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(path) if path else None,
            "exists": False,
            "encrypted": False,
            "last_modified": None,
            "age_hours": None,
            "stale": False,
            "max_age_hours": float(self.max_age_hours),
            "encryption_enabled": self.encryption_enabled,
            "encryption_required": self.require_encryption,
        }
        if path is None:
            return payload
        # Suffix is only a hint for a path that does not exist yet. For a real
        # file the content decides: naming alone meant a mislabelled file was
        # classified wrongly in both directions — an encrypted envelope without
        # `.enc` was passed to Playwright verbatim as if it were storage state
        # (no cookies load, and the agent proceeds believing the profile applied).
        payload["encrypted"] = path.name.endswith(".enc")
        if not path.exists():
            return payload
        detected = self._detect_encrypted(path)
        if detected is not None:
            payload["encrypted"] = detected
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        age_hours = max(0.0, (datetime.now(UTC) - modified).total_seconds() / 3600.0)
        stale = bool(self.max_age_hours > 0 and age_hours > self.max_age_hours)
        payload.update(
            {
                "exists": True,
                "last_modified": modified.isoformat().replace("+00:00", "Z"),
                "age_hours": round(age_hours, 3),
                "stale": stale,
            }
        )
        return payload

    @staticmethod
    def _detect_encrypted(path: Path) -> bool | None:
        """Whether `path` holds a fernet envelope, by content.

        Returns None when the file cannot be classified (unreadable, not JSON,
        implausibly large) so the caller keeps its suffix-based assumption
        rather than guessing.
        """
        try:
            if path.stat().st_size > _MAX_INSPECT_BYTES:
                return None
            body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        if body.get("format") == "fernet-json" and isinstance(body.get("ciphertext"), str):
            return True
        # A Playwright storage-state file is the other legitimate shape here.
        if "cookies" in body or "origins" in body:
            return False
        return None

    def _encrypt(self, plaintext: bytes) -> str:
        if self._fernet is None:
            raise RuntimeError("Auth state encryption key is not configured")
        return self._fernet.encrypt(plaintext).decode("utf-8")
