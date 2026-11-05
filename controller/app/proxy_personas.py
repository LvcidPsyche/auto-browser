"""
proxy_personas.py — Named proxy configuration management.

Each proxy persona has a static server/credential set so that different
agent sessions can be assigned distinct IPs — preventing shared network
footprints that platforms use to link sessions and trigger bans.

Persona file format (JSON at PROXY_PERSONA_FILE path):
  {
    "my-us-east": {
      "server": "http://proxy.example.com:8080",
      "username": "user1",
      "password": "secret",
      "description": "US East Coast residential proxy"
    },
    "my-eu-west": {
      "server": "socks5://proxy2.example.com:1080",
      "username": "user2",
      "password": "secret2",
      "description": "EU West datacenter proxy",
      "owner": "alice"
    }
  }

A persona belongs to the token-verified operator who first saved it (the optional "owner" key,
written by the store). Only that operator can list, read, resolve, overwrite or delete it; to every
other caller it does not exist. A persona with no owner (saved with the shared token or no token,
or written before owners existed, or with an empty "owner") is open to every caller, as memory
profiles are, and any caller can overwrite or delete it. To make a
persona private on a deployment with named tokens (API_BEARER_TOKENS), add "owner": "<operator id>"
to its entry by hand.

API (every call acts for the current operator):
  list_personas()              → list of the caller's persona summaries (no passwords, no owner)
  get_persona(name)            → one persona summary (no password), same shape as the list
  set_persona(name, **kwargs)  → create or update a persona; PermissionError if another
                                 operator owns the name
  delete_persona(name)         → remove a persona
  resolve_proxy(name)          → {server, username, password} ready for Playwright; the only
                                 way to read a stored password, for session creation
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .session_ownership import may_use, verified_operator
from .utils import PRIVATE_FILE_MODE, atomic_write_text

logger = logging.getLogger(__name__)


PROXY_PERSONA_FILE_UNREADABLE = (
    "the proxy persona file (PROXY_PERSONA_FILE) is unreadable; fix or move it. It was not changed."
)


class ProxyPersonaFileError(RuntimeError):
    """The persona file exists but cannot be read or parsed.

    The store refuses and leaves the file where it is, rather than moving it aside as the cron store does: a
    transient read error (a sharing violation, a brief permission fault) must never displace a good, hand-edited file.

    The message is fixed and path-free, because callers (REST and MCP) show it as it is. The path and the
    cause are logged at ERROR where the file is read. Copy and pickle rebuild an exception from its args (the
    message), so the constructor takes and ignores them.
    """

    def __init__(self, *_args: object) -> None:
        super().__init__(PROXY_PERSONA_FILE_UNREADABLE)


PROXY_PERSONA_DUPLICATE_ENTRIES = (
    "the proxy persona file (PROXY_PERSONA_FILE) holds more than one entry for this name (their keys differ only in "
    "surrounding spaces); remove all but one by hand. It was not changed."
)


class ProxyPersonaDuplicateError(ValueError):
    """More than one entry in the persona file normalizes to the name a save or delete is for.

    Only a hand edit makes this ("a " beside "a"); the store never writes one. Which entry a save or delete
    means cannot be told, so both are refused. The message is fixed and path-free, as ProxyPersonaFileError's
    is; the raw keys and the path are logged where the file is read.
    """

    def __init__(self, *_args: object) -> None:
        super().__init__(PROXY_PERSONA_DUPLICATE_ENTRIES)


class ProxyPersonaStore:
    """Read/write proxy personas from a JSON config file."""

    def __init__(self, file_path: str | Path | None):
        self._path = Path(file_path) if file_path else None

    def _read_object(self) -> dict[str, Any]:
        """The file's top-level object as written; {} only when there is no file.

        A file that exists but cannot be read or parsed raises: answering {} let the next save write
        one persona over it and erase the rest. Existence is not checked first: Path.exists() raises a
        permission error as itself (EACCES on Python 3.11 and 3.12), outside the handling below.
        """
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.error("failed to load proxy persona file %s: %s", self._path, exc)
            raise ProxyPersonaFileError() from exc
        if not isinstance(data, dict):
            logger.error("proxy persona file is not a JSON object: %s", self._path)
            raise ProxyPersonaFileError()
        return data

    def _load(self) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for raw_name, raw_persona in self._read_object().items():
            try:
                name, persona = self._normalize_persona(raw_name, raw_persona)
            except ValueError as exc:
                logger.warning("skipping invalid proxy persona entry %r in %s: %s", raw_name, self._path, exc)
                continue
            normalized[name] = persona
        return normalized

    def _save(self, data: dict[str, Any]) -> None:
        if self._path is None:
            raise RuntimeError("No PROXY_PERSONA_FILE configured — cannot save proxy personas")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # In the file's own order, not sorted: _load takes the last of two entries for one name, so sorting
        # on save let a save of any persona change which one wins.
        atomic_write_text(self._path, json.dumps(data, indent=2, ensure_ascii=False), mode=PRIVATE_FILE_MODE)

    def _raw_entries(self, name: str) -> tuple[dict[str, Any], str | None]:
        """The file's entries exactly as written, and the key of the one named *name* (None if none).

        A save starts from this, not from the normalized view, so an entry the store ignores (a typo in
        "server") and any extra key an operator added to another entry are written back untouched. A name
        that more than one entry normalizes to (a hand edit: "a " beside "a") is refused: which one is meant
        cannot be told, and removing them all would drop one another operator may own.
        """
        raw = self._read_object()
        keys = []
        for raw_name in raw:
            try:
                normalized_name = self._normalize_persona(raw_name, raw[raw_name])[0]
            except ValueError:
                continue  # an entry the store ignores is never the one being replaced or removed
            if normalized_name == name:
                keys.append(raw_name)
        if len(keys) > 1:
            logger.error(
                "proxy persona file %s holds %d entries for %r: %r; remove all but one",
                self._path,
                len(keys),
                name,
                keys,
            )
            raise ProxyPersonaDuplicateError()
        return raw, (keys[0] if keys else None)

    @staticmethod
    def _owner_of(entry: Any) -> str | None:
        """An entry's "owner" as the store reads it: blank or missing is no owner."""
        owner = entry.get("owner") if isinstance(entry, dict) else None
        return None if owner is None else str(owner).strip() or None

    @staticmethod
    def _normalize_persona(name: Any, persona: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("persona name must be a non-empty string")
        if not isinstance(persona, dict):
            raise ValueError("persona entry must be an object")

        normalized_name = name.strip()
        server = str(persona.get("server") or "").strip()
        if not server:
            raise ValueError("persona server is required")

        username_raw = persona.get("username")
        username = None if username_raw is None else str(username_raw).strip() or None
        password_raw = persona.get("password")
        password = None if password_raw is None else str(password_raw)
        if password == "":
            password = None
        description_raw = persona.get("description", "")
        description = str(description_raw).strip() if description_raw is not None else ""

        return normalized_name, {
            "server": server,
            "username": username,
            "password": password,
            "description": description,
            "owner": ProxyPersonaStore._owner_of(persona),
        }

    # ── Public API ──────────────────────────────────────────────────────────

    @staticmethod
    def _summary(name: str, persona: dict[str, Any]) -> dict[str, Any]:
        """A persona as the API shows it: the password is never part of it."""
        return {
            "name": name,
            "server": persona.get("server", ""),
            "username": persona.get("username"),
            "has_password": bool(persona.get("password")),
            "description": persona.get("description", ""),
        }

    def list_personas(self) -> list[dict[str, Any]]:
        """Return the personas the caller may use, with passwords masked."""
        return [
            self._summary(name, persona)
            for name, persona in sorted(self._load().items())
            if may_use(persona.get("owner"))
        ]

    def ownerless_names(self) -> list[str]:
        """Names of the personas open to every caller, whoever is asking (for the startup warning)."""
        return sorted(name for name, persona in self._load().items() if persona.get("owner") is None)

    def blank_owner_names(self) -> list[str]:
        """Names of personas whose "owner" is an empty string, which reads as no owner (for the startup warning)."""
        names: list[str] = []
        for raw_name, raw_persona in self._read_object().items():
            owner = raw_persona.get("owner") if isinstance(raw_persona, dict) else None
            if not isinstance(owner, str) or owner.strip():
                continue
            try:
                names.append(self._normalize_persona(raw_name, raw_persona)[0])
            except ValueError:
                continue  # an entry the store ignores anyway
        return sorted(names)

    def get_persona(self, name: str) -> dict[str, Any]:
        """Return one persona with the password masked. Raises KeyError if not found."""
        return self._summary(name, self._persona_with_password(name))

    def _persona_with_password(self, name: str) -> dict[str, Any]:
        data = self._load()
        # Another operator's persona is reported as missing, so a name leaks nothing.
        if name not in data or not may_use(data[name].get("owner")):
            raise KeyError(f"Proxy persona not found: {name!r}")
        return data[name]

    def set_persona(
        self,
        name: str,
        *,
        server: str,
        username: str | None = None,
        password: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create or update a proxy persona. Returns the new summary."""
        normalized_name, normalized_persona = self._normalize_persona(
            name,
            {
                "server": server,
                "username": username,
                "password": password,
                "description": description,
            },
        )
        data = self._load()
        existing = data.get(normalized_name)
        if existing is not None and not may_use(existing.get("owner")):
            # The one place a name is revealed: a single namespace cannot hide a collision.
            raise PermissionError(f"proxy persona {normalized_name!r} belongs to another operator")
        # An update never changes who owns a persona (nor claims an unowned one); a new
        # persona belongs to its creator when a named credential proved who that is.
        normalized_persona["owner"] = existing["owner"] if existing is not None else verified_operator()
        raw, key = self._raw_entries(normalized_name)
        # An entry under this exact key other than the one found is one the store ignores (a typo in "server");
        # the save would overwrite it, so it is refused as a valid entry would be when another operator owns it.
        if key != normalized_name and normalized_name in raw and not may_use(self._owner_of(raw[normalized_name])):
            raise PermissionError(f"proxy persona {normalized_name!r} belongs to another operator")
        # An update keeps its entry's place in the file and any key an operator added to it by hand; the store's
        # own fields are laid over it.
        entry = {**(raw[key] if key is not None else {}), **normalized_persona}
        place = key if key is not None else normalized_name
        saved: dict[str, Any] = {}
        for raw_name, value in raw.items():
            if raw_name == place:
                saved[normalized_name] = entry
            elif raw_name != normalized_name:  # an ignored entry under the name itself is overwritten
                saved[raw_name] = value
        saved.setdefault(normalized_name, entry)  # a new persona goes last
        self._save(saved)
        return self._summary(normalized_name, normalized_persona)

    def delete_persona(self, name: str) -> bool:
        """Delete a persona. Returns True if deleted, False if not found (or not the caller's)."""
        data = self._load()
        if name not in data or not may_use(data[name].get("owner")):
            return False
        raw, key = self._raw_entries(name)
        if key is not None:
            del raw[key]
        self._save(raw)
        return True

    def resolve_proxy(self, name: str) -> dict[str, Any]:
        """Return proxy config ready for Playwright context creation.

        Returns: {"server": str, "username": str | None, "password": str | None}
        """
        persona = self._persona_with_password(name)
        result: dict[str, Any] = {"server": persona["server"]}
        if persona.get("username"):
            result["username"] = persona["username"]
        if persona.get("password"):
            result["password"] = persona["password"]
        return result


def warn_about_ownerless_personas(store: ProxyPersonaStore, *, named_tokens: bool) -> None:
    """Say at startup which personas stay open to every operator, when operators can be told apart.

    Personas saved before owners existed carry none, and a named-token deployment
    (API_BEARER_TOKENS) would otherwise keep sharing them without a sign that it does. An empty
    "owner" is the same as none, so a hand edit that left one blank is called out separately.
    """
    if not named_tokens:
        return
    try:
        names = store.ownerless_names()
    except ProxyPersonaFileError:
        return  # already logged at error level by the read; the file is left alone
    if not names:
        return
    one = len(names) == 1
    logger.warning(
        "%d proxy %s without an owner %s open to every operator: %s. "
        'Add "owner": "<operator id>" to an entry in the proxy persona file to make it private.',
        len(names),
        "persona" if one else "personas",
        "is" if one else "are",
        ", ".join(names),
    )
    blank = store.blank_owner_names()
    if blank:
        logger.warning(
            'proxy personas with an empty "owner" have no owner, so they are open to every operator: %s. '
            'An empty "owner" does not make a persona private; put an operator id in it.',
            ", ".join(blank),
        )
