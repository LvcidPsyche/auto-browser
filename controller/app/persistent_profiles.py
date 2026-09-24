"""Client for browser-node's persistent-profile control API.

Each named identity/profile (an auth_profile, or the auto-persist default
when the caller names none) gets its own on-disk Chromium user-data-dir
inside browser-node, launched via Playwright's `launchPersistentContext` so
IndexedDB, service workers, cache and history survive across Opens,
controller restarts and image rebuilds -- not just the cookies + localStorage
that a `storage_state` replay carries. browser-node owns the actual Chromium
process (it is the container with the X display the owner's noVNC view
renders); this module only talks to its small internal HTTP control API,
reachable exclusively over the tenant-private Docker network.

See browser-node/server.mjs for the server side of this protocol.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def normalize_profile_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized or not _NAME_RE.fullmatch(normalized):
        raise ValueError("persistent profile names may contain letters, numbers, dots, underscores, and hyphens")
    return normalized


@dataclass
class PersistentProfileHandle:
    name: str
    cdp_endpoint: str
    already_open: bool
    seeded: bool
    was_empty: bool


class PersistentProfileClient:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def base_url(self) -> str:
        return f"http://{self.settings.browser_node_host}:{self.settings.profile_control_port}"

    async def open(
        self,
        name: str,
        *,
        context_kwargs: dict[str, Any] | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> PersistentProfileHandle:
        name = normalize_profile_name(name)
        context_kwargs = context_kwargs or {}
        body: dict[str, Any] = {
            "name": name,
            "viewport": context_kwargs.get("viewport"),
            "accept_downloads": context_kwargs.get("accept_downloads", True),
            "locale": context_kwargs.get("locale") or self.settings.persistent_profile_locale,
            "timezone_id": context_kwargs.get("timezone_id") or self.settings.persistent_profile_timezone,
            "extra_http_headers": context_kwargs.get("extra_http_headers"),
            "proxy": context_kwargs.get("proxy"),
            "storage_state": storage_state,
        }
        user_agent = context_kwargs.get("user_agent") or self.settings.persistent_profile_user_agent
        if user_agent:
            body["user_agent"] = user_agent

        async with httpx.AsyncClient(timeout=self.settings.profile_control_timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/profiles/open", json=body)
        if response.status_code >= 400:
            raise RuntimeError(
                f"browser-node refused to open persistent profile '{name}': "
                f"{response.status_code} {response.text[:500]}"
            )
        data = response.json()
        cdp_endpoint = data.get("cdp_endpoint")
        if not cdp_endpoint:
            raise RuntimeError(f"browser-node did not return a CDP endpoint for persistent profile '{name}'")
        return PersistentProfileHandle(
            name=name,
            cdp_endpoint=cdp_endpoint,
            already_open=bool(data.get("already_open")),
            seeded=bool(data.get("seeded")),
            was_empty=bool(data.get("was_empty")),
        )

    async def ping(self) -> None:
        """Raise unless browser-node's profile-control API is reachable.

        Used by /readyz and the deep health probe in persistent-profile mode,
        where there is no longer one shared browser for `ensure_browser()` to
        connect to -- readiness there means "browser-node is up", not "a
        browser is already running" (persistent profiles launch lazily, on
        the first session that asks for one).
        """
        async with httpx.AsyncClient(timeout=self.settings.profile_control_timeout_seconds) as client:
            response = await client.get(f"{self.base_url}/healthz")
        response.raise_for_status()

    async def close(self, name: str) -> None:
        """Release this session's hold on the profile.

        Best-effort and non-fatal: browser-node keeps a refcount per profile
        (see server.mjs) and only tears down the actual Chromium process once
        nothing else holds it, so a failure here leaves behind a running
        profile process rather than losing session-close cleanup for
        everything else. Logged, never raised.
        """
        try:
            name = normalize_profile_name(name)
        except ValueError as exc:
            logger.warning("persistent profile close skipped: %s", exc)
            return
        try:
            async with httpx.AsyncClient(timeout=self.settings.profile_control_timeout_seconds) as client:
                response = await client.post(f"{self.base_url}/profiles/close", json={"name": name})
            if response.status_code >= 400:
                logger.warning(
                    "persistent profile close for '%s' returned %s: %s",
                    name,
                    response.status_code,
                    response.text[:500],
                )
        except Exception as exc:
            logger.warning("persistent profile close failed for '%s': %s", name, exc)

    def profile_disk_usage_bytes(self, name: str) -> int | None:
        """Best-effort on-disk size of a profile's user-data-dir.

        Both browser-node and the controller mount the same `/data` volume,
        so this reads it directly rather than adding another network hop.
        Returns None if the profile has never been opened (no directory yet),
        the root itself is not mounted (e.g. docker_ephemeral / tests), or it
        cannot be read -- the production tenant compose hardens the
        controller with `cap_drop: [ALL]` and the profile dirs are
        mode 0700 owned by browser-node's uid, so this is expected to come
        back empty there; `du -sh /data/browser-profiles/*` from inside the
        browser-node container is the reliable way to check sizes in
        production (see the deploy notes).
        """
        from pathlib import Path

        try:
            name = normalize_profile_name(name)
        except ValueError:
            return None
        root = Path(self.settings.browser_profiles_root) / name
        try:
            if not root.exists():
                return None
            total = 0
            for entry in root.rglob("*"):
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
            return total
        except OSError as exc:
            logger.debug("could not read disk usage for persistent profile '%s': %s", name, exc)
            return None
