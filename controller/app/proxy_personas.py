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
      "description": "EU West datacenter proxy"
    }
  }

API:
  list_personas()              → list of persona summaries (no passwords)
  get_persona(name)            → full persona with credentials
  set_persona(name, **kwargs)  → create or update a persona
  delete_persona(name)         → remove a persona
  resolve_proxy(name)          → {server, username, password} ready for Playwright
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ProxyPersonaStore:
    """Read/write proxy personas from a JSON config file."""

    def __init__(self, file_path: str | Path | None):
        self._path = Path(file_path) if file_path else None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None:
            return {}
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("proxy persona file is not a JSON object: %s", self._path)
                return {}
            normalized: dict[str, dict[str, Any]] = {}
            for raw_name, raw_persona in data.items():
                try:
                    name, persona = self._normalize_persona(raw_name, raw_persona)
                except ValueError as exc:
                    logger.warning("skipping invalid proxy persona entry %r in %s: %s", raw_name, self._path, exc)
                    continue
                normalized[name] = persona
            return normalized
        except Exception as exc:
            logger.warning("failed to load proxy persona file %s: %s", self._path, exc)
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        if self._path is None:
            raise RuntimeError("No PROXY_PERSONA_FILE configured — cannot save proxy personas")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._path)

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
        }

    # ── Public API ──────────────────────────────────────────────────────────

    def list_personas(self) -> list[dict[str, Any]]:
        """Return all personas with passwords masked."""
        data = self._load()
        return [
            {
                "name": name,
                "server": persona.get("server", ""),
                "username": persona.get("username"),
                "has_password": bool(persona.get("password")),
                "description": persona.get("description", ""),
            }
            for name, persona in sorted(data.items())
        ]

    def get_persona(self, name: str) -> dict[str, Any]:
        """Return a persona by name. Raises KeyError if not found."""
        data = self._load()
        if name not in data:
            raise KeyError(f"Proxy persona not found: {name!r}")
        persona = dict(data[name])
        persona["name"] = name
        return persona

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
        data[normalized_name] = normalized_persona
        self._save(data)
        return {
            "name": normalized_name,
            "server": normalized_persona["server"],
            "username": normalized_persona["username"],
            "has_password": bool(normalized_persona["password"]),
            "description": normalized_persona["description"],
        }

    def delete_persona(self, name: str) -> bool:
        """Delete a persona. Returns True if deleted, False if not found."""
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def resolve_proxy(self, name: str) -> dict[str, Any]:
        """Return proxy config ready for Playwright context creation.

        Returns: {"server": str, "username": str | None, "password": str | None}
        """
        persona = self.get_persona(name)
        result: dict[str, Any] = {"server": persona["server"]}
        if persona.get("username"):
            result["username"] = persona["username"]
        if persona.get("password"):
            result["password"] = persona["password"]
        return result
