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

Every call except the unauthenticated liveness ping carries the shared
PROFILE_CONTROL_TOKEN as a bearer token, and so does the CDP connection
itself (browser-node relays it to the profile's loopback-only debugging
port). With no token configured nothing is attempted at all.

Ownership: every call that can open or move a profile's directory carries
the effective owner the controller already authorized (`require_access`).
browser-node records it in the directory and refuses (or, on open, trashes
and starts fresh) when it does not match, so the same name recreated under
a different owner never reopens the old identity's logins.

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


class PersistentProfileError(RuntimeError):
    """browser-node refused or failed a persistent-profile operation."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PersistentProfileHandle:
    name: str
    cdp_endpoint: str
    already_open: bool
    seeded: bool
    was_empty: bool
    # Opaque lease token for this open (unique across browser-node restarts);
    # /profiles/close must carry it.
    generation: str | None = None


class PersistentProfileClient:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    @property
    def base_url(self) -> str:
        return f"http://{self.settings.browser_node_host}:{self.settings.profile_control_port}"

    @property
    def configured(self) -> bool:
        return bool(getattr(self.settings, "profile_control_token", ""))

    def auth_headers(self) -> dict[str, str]:
        token = getattr(self.settings, "profile_control_token", "")
        if not token:
            raise PersistentProfileError(
                "PROFILE_CONTROL_TOKEN is not configured; refusing to use persistent browser profiles"
            )
        return {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = self.auth_headers()
        async with httpx.AsyncClient(timeout=self.settings.profile_control_timeout_seconds) as client:
            response = await client.post(f"{self.base_url}{path}", json=body, headers=headers)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error") or response.text
            except ValueError:
                detail = response.text
            raise PersistentProfileError(
                f"browser-node refused {path} for '{body.get('name')}': {response.status_code} {str(detail)[:300]}",
                status_code=response.status_code,
            )
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def open(
        self,
        name: str,
        *,
        owner: str | None = None,
        adopt_unmarked: bool = False,
        context_kwargs: dict[str, Any] | None = None,
        storage_state: dict[str, Any] | None = None,
        reattach_only: bool = False,
    ) -> PersistentProfileHandle:
        name = normalize_profile_name(name)
        context_kwargs = context_kwargs or {}
        body: dict[str, Any] = {
            "name": name,
            "owner": owner,
            "adopt_unmarked": adopt_unmarked,
            "viewport": context_kwargs.get("viewport"),
            "accept_downloads": context_kwargs.get("accept_downloads", True),
            "locale": context_kwargs.get("locale") or self.settings.persistent_profile_locale,
            "timezone_id": context_kwargs.get("timezone_id") or self.settings.persistent_profile_timezone,
            "extra_http_headers": context_kwargs.get("extra_http_headers"),
            "proxy": context_kwargs.get("proxy"),
            "storage_state": storage_state,
        }
        if reattach_only:
            # Hand back the running browser or fail -- never launch one.
            body["reattach_only"] = True
        user_agent = context_kwargs.get("user_agent") or self.settings.persistent_profile_user_agent
        if user_agent:
            body["user_agent"] = user_agent

        data = await self._post("/profiles/open", body)
        cdp_endpoint = data.get("cdp_endpoint")
        if not cdp_endpoint:
            raise PersistentProfileError(f"browser-node did not return a CDP endpoint for persistent profile '{name}'")
        generation = data.get("generation")
        if not isinstance(generation, str) or not generation:
            raise PersistentProfileError(f"browser-node did not return a lease generation for profile '{name}'")
        return PersistentProfileHandle(
            name=name,
            cdp_endpoint=cdp_endpoint,
            already_open=bool(data.get("already_open")),
            seeded=bool(data.get("seeded")),
            was_empty=bool(data.get("was_empty")),
            generation=generation,
        )

    # --- downloads a persistent profile produced (browser-node owns them) ---
    #
    # browser-node's own Playwright launched the persistent context, so it is
    # the one that receives the profile's download events and holds the files
    # (the controller only attaches over CDP with no_defaults and never sees
    # them). These three calls are how the controller lists, pulls and clears
    # them -- over the tenant-private control API, never the shared volume, so
    # file ownership inside /data/downloads never matters.

    async def list_downloads(self, name: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        name = normalize_profile_name(name)
        data = await self._post("/downloads/list", {"name": name, "after_seq": int(after_seq)})
        items = data.get("downloads")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def fetch_download(self, download_id: str, destination: Any, *, max_bytes: int) -> int:
        """Stream one finished download into `destination`; returns its size.

        Raises PersistentProfileError (status 413) past `max_bytes` -- the
        partial file is removed by the caller."""
        if not re.fullmatch(r"[0-9a-f]{16,64}", download_id or ""):
            raise PersistentProfileError("invalid download id", status_code=400)
        headers = self.auth_headers()
        timeout = httpx.Timeout(max(self.settings.profile_control_timeout_seconds, 60.0), connect=10.0)
        total = 0
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET", f"{self.base_url}/downloads/file", params={"id": download_id}, headers=headers
            ) as response:
                if response.status_code >= 400:
                    raise PersistentProfileError(
                        f"browser-node refused download {download_id}: {response.status_code}",
                        status_code=response.status_code,
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PersistentProfileError("download too large", status_code=413)
                with open(destination, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise PersistentProfileError("download too large", status_code=413)
                        handle.write(chunk)
        return total

    async def delete_download(self, download_id: str) -> None:
        try:
            await self._post("/downloads/delete", {"id": download_id})
        except Exception as exc:  # best effort: browser-node also drops old ones itself
            logger.debug("could not delete browser-node download %s: %s", download_id, exc)

    async def ping(self) -> None:
        """Raise unless browser-node's profile-control API is reachable.

        Used by /readyz and the deep health probe in persistent-profile mode.
        Readiness there means "browser-node is up"; persistent profiles launch
        lazily, on the first session that asks for one.
        """
        async with httpx.AsyncClient(timeout=self.settings.profile_control_timeout_seconds) as client:
            response = await client.get(f"{self.base_url}/healthz")
        response.raise_for_status()

    async def close(self, name: str, *, generation: str | None) -> bool:
        """Close the profile's Chromium process in browser-node.

        The controller holds at most one live session per profile (see
        BrowserSessionService), so this is called exactly once per session.
        Best-effort and non-fatal: a failure leaves a running profile process
        behind (the next Open simply re-attaches to it) rather than breaking
        the rest of session-close cleanup. Returns whether it succeeded.

        `generation` is the one this session's open returned; browser-node
        ignores the close if the profile was reopened under a newer one.
        """
        try:
            name = normalize_profile_name(name)
        except ValueError as exc:
            logger.warning("persistent profile close skipped: %s", exc)
            return False
        try:
            if generation is None:
                logger.warning("persistent profile close for '%s' skipped: no lease generation", name)
                return False
            await self._post("/profiles/close", {"name": name, "generation": generation})
            return True
        except Exception as exc:
            logger.warning("persistent profile close failed for '%s': %s", name, exc)
            return False

    async def trash(self, name: str, *, reason: str, owner: str | None) -> dict[str, Any]:
        """Move a profile's on-disk directory to browser-node's trash.

        Never deletes: a login is always recoverable from
        /data/browser-profiles/.trash. Closes the profile first if it is
        running. Raises on any failure, so callers can abort the operation
        that asked for it instead of leaving the old directory in place.
        """
        name = normalize_profile_name(name)
        return await self._post("/profiles/trash", {"name": name, "reason": reason, "owner": owner})

    async def rename(self, name: str, new_name: str, *, owner: str | None) -> dict[str, Any]:
        """Rename a profile's on-disk directory (a stale destination goes to trash)."""
        name = normalize_profile_name(name)
        new_name = normalize_profile_name(new_name)
        return await self._post("/profiles/rename", {"name": name, "new_name": new_name, "owner": owner})

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
