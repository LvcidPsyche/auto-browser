from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from .utils import UTC

logger = logging.getLogger(__name__)

# Auth state is cookies plus storage state — kilobytes. Anything larger is not
# a state file, and content-sniffing it is not worth the read.
_MAX_INSPECT_BYTES = 8 * 1024 * 1024

# A rotated history copy is named "<state file name>.<UTC yyyymmddTHHMMSS>"
# (optionally "-N" when two rotations land in the same second). Recognising the
# exact shape keeps history files from ever being mistaken for the live state
# file by resolve_state_path()/list(), which only look for the exact name.
_HISTORY_SUFFIX_RE = re.compile(r"^\d{8}T\d{6}(-\d+)?$")


@dataclass(frozen=True)
class _SignInRule:
    """One site's proof-of-login: which cookie(s), on which domain(s)."""

    site: str
    domains: tuple[str, ...]
    cookie_names: tuple[str, ...]
    require_all: bool = False


# The single table of "what does a signed-in cookie jar look like" used by the
# auto-persist downgrade guard (see BrowserAuthProfileService.save_auto_persist).
# Keep this the one place that knows these cookie names.
SIGN_IN_RULES: tuple[_SignInRule, ...] = (
    _SignInRule("facebook.com", ("facebook.com",), ("c_user", "xs"), require_all=True),
    _SignInRule("instagram.com", ("instagram.com",), ("sessionid",)),
    _SignInRule("google.com", ("google.com",), ("SID", "__Secure-1PSID")),
    _SignInRule("youtube.com", ("youtube.com",), ("LOGIN_INFO",)),
    _SignInRule("tiktok.com", ("tiktok.com",), ("sessionid",)),
    _SignInRule("x.com", ("x.com", "twitter.com"), ("auth_token",)),
    _SignInRule("linkedin.com", ("linkedin.com",), ("li_at",)),
    _SignInRule(
        "chatgpt.com",
        ("chatgpt.com", "openai.com"),
        ("__Secure-next-auth.session-token",),
    ),
)


def _cookie_domain_matches(cookie_domain: str, domain: str) -> bool:
    host = str(cookie_domain or "").lower().lstrip(".").rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


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
        history_keep: int = 20,
    ):
        self.encryption_key = encryption_key
        self.require_encryption = require_encryption
        self.max_age_hours = max_age_hours
        self.history_keep = history_keep
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
            self._rotate_history(final_path)
            temp_encrypted.replace(final_path)
            temp_plain.unlink(missing_ok=True)
        else:
            self._rotate_history(final_path)
            temp_plain.replace(final_path)

        return self.inspect(final_path)

    def _rotate_history(self, final_path: Path) -> None:
        """Copy the about-to-be-overwritten `final_path` into a history file.

        Runs before every write (auto-persist and explicit save alike) so a
        blind overwrite is never the last copy of a login. A no-op when there
        is nothing there yet (first save). Best-effort: a rotation failure
        (e.g. disk full) must not block the save itself, since refusing to
        save a fresh, good login over a rotation hiccup is the wrong trade.
        """
        if self.history_keep <= 0 or not final_path.exists():
            return
        try:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            history_path = final_path.with_name(f"{final_path.name}.{ts}")
            suffix = 1
            while history_path.exists():
                history_path = final_path.with_name(f"{final_path.name}.{ts}-{suffix}")
                suffix += 1
            shutil.copy2(final_path, history_path)
            self._prune_history(final_path)
        except OSError:
            logger.warning("auth_state: failed to rotate history for %s", final_path, exc_info=True)

    def _prune_history(self, final_path: Path) -> None:
        prefix = f"{final_path.name}."
        parent = final_path.parent
        if not parent.exists():
            return
        history_files = [
            candidate
            for candidate in parent.iterdir()
            if candidate.is_file()
            and candidate.name != final_path.name
            and candidate.name.startswith(prefix)
            and _HISTORY_SUFFIX_RE.match(candidate.name[len(prefix) :])
        ]
        history_files.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
        for stale in history_files[self.history_keep :]:
            stale.unlink(missing_ok=True)

    def prepare_for_context(self, source_path: Path, *, max_age_hours: float | None = None) -> PreparedAuthState:
        info = self.inspect(source_path, max_age_hours=max_age_hours)
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

    def inspect(self, path: Path | None, *, max_age_hours: float | None = None) -> dict[str, Any]:
        effective_max_age = float(self.max_age_hours if max_age_hours is None else max_age_hours)
        payload: dict[str, Any] = {
            "path": str(path) if path else None,
            "exists": False,
            "encrypted": False,
            "last_modified": None,
            "age_hours": None,
            "stale": False,
            "max_age_hours": effective_max_age,
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
        stale = bool(effective_max_age > 0 and age_hours > effective_max_age)
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

    def read_cookies(self, path: Path | None) -> list[dict[str, Any]]:
        """Best-effort: the cookie list stored at `path`, or `[]` if unreadable.

        Used by the auto-persist downgrade guard to see what the *saved*
        profile already has before deciding whether a new write would erase a
        signed-in site. Never raises: a missing, corrupt, or unencryptable
        file just means "nothing to compare against", not a reason to block
        the caller.
        """
        if path is None or not path.exists():
            return []
        try:
            encrypted = self._detect_encrypted(path)
            if encrypted is None:
                encrypted = path.name.endswith(".enc")
            if encrypted:
                if self._fernet is None:
                    return []
                payload = json.loads(path.read_text(encoding="utf-8"))
                plaintext = self._fernet.decrypt(payload["ciphertext"].encode("utf-8"))
                body = json.loads(plaintext)
            else:
                body = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("auth_state: could not read cookies from %s", path, exc_info=True)
            return []
        cookies = body.get("cookies") if isinstance(body, dict) else None
        return cookies if isinstance(cookies, list) else []

    def signed_in_sites(self, cookies: list[dict[str, Any]] | None) -> set[str]:
        """Which sites in `SIGN_IN_RULES` look signed-in from this cookie jar.

        A site counts as signed in when its required cookie(s) are present on
        a matching domain and not expired (a session cookie -- no `expires`,
        or `expires` <= 0 -- always counts as not expired).
        """
        if not cookies:
            return set()
        now = datetime.now(UTC)
        signed_in: set[str] = set()
        for rule in SIGN_IN_RULES:
            matched: list[dict[str, Any]] = []
            present_names: set[str] = set()
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                name = cookie.get("name")
                if name not in rule.cookie_names:
                    continue
                domain = cookie.get("domain") or ""
                if not any(_cookie_domain_matches(domain, d) for d in rule.domains):
                    continue
                matched.append(cookie)
                present_names.add(name)

            has_required = (
                all(name in present_names for name in rule.cookie_names)
                if rule.require_all
                else bool(present_names)
            )
            if not has_required:
                continue

            not_expired = True
            for cookie in matched:
                expires = cookie.get("expires")
                if expires is None:
                    continue
                try:
                    expires = float(expires)
                except (TypeError, ValueError):
                    continue
                if expires <= 0:
                    continue  # session cookie
                if datetime.fromtimestamp(expires, tz=UTC) <= now:
                    not_expired = False
                    break
            if not_expired:
                signed_in.add(rule.site)
        return signed_in
