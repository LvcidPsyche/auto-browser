from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...auth_policy import credentials_for, is_local_only_host
from ...utils import UTC

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession

logger = logging.getLogger(__name__)

# What an agent-facing result says about an isolated session's takeover: its takeover_url has no noVNC password,
# which only the human-only paths return (NAV-14).
TAKEOVER_NOTE = (
    "This link has no noVNC password. The operator opens the full link from the dashboard or with "
    "scripts/takeover_link.sh <session_id>; otherwise noVNC asks for the password."
)

# The fields of the reverse-ssh sidecar file that global_info passes on in its "tunnel" block.
_TUNNEL_FIELDS_SHOWN = ("status", "updated_at", "public_takeover_url", "public_api_url", "stale_after_seconds")


class BrowserRemoteAccessService:
    """Encapsulates remote access metadata and takeover URL resolution."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def get_info(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            session = self.manager.sessions.get(session_id)
            if session is not None:
                return self.session_info(session)
        return self.global_info()

    def global_info(self) -> dict[str, Any]:
        info_path = Path(self.manager.settings.remote_access_info_path)
        payload: dict[str, Any] = {
            "active": False,
            "status": "inactive",
            "stale": False,
            "source": "static",
            "configured_takeover_url": self.manager.settings.takeover_url,
            "takeover_url": self.manager.settings.takeover_url,
            "api_url": None,
            "api_auth_enabled": bool(credentials_for(self.manager.settings)),
            "info_path": str(info_path),
            "exists": info_path.exists(),
            "last_updated": None,
            "age_seconds": None,
            "stale_after_seconds": float(self.manager.settings.remote_access_stale_after_seconds),
            "tunnel": None,
            "error": None,
        }
        if not info_path.exists():
            return payload
        try:
            tunnel = json.loads(info_path.read_text(encoding="utf-8"))
            if not isinstance(tunnel, dict):
                raise ValueError(f"expected a JSON object, found {type(tunnel).__name__}")
        except Exception as exc:
            logger.warning("failed to read remote access info %s: %s", info_path, exc)
            payload["status"] = "error"
            payload["source"] = "metadata_file"
            payload["error"] = "remote_access_metadata_unreadable"
            return payload

        last_updated = self.parse_timestamp(tunnel.get("updated_at"))
        if last_updated is None:
            try:
                last_updated = datetime.fromtimestamp(info_path.stat().st_mtime, tz=UTC)
            except OSError:
                last_updated = None
        age_seconds = None
        if last_updated is not None:
            age_seconds = max(0.0, (datetime.now(UTC) - last_updated).total_seconds())
        stale_after_seconds = self._stale_after_seconds(tunnel.get("stale_after_seconds"))
        raw_status = str(tunnel.get("status") or "active")
        stale = bool(age_seconds is not None and age_seconds > stale_after_seconds)
        active = raw_status == "active" and not stale
        public_takeover_url = tunnel.get("public_takeover_url")
        public_api_url = tunnel.get("public_api_url")
        takeover_url = (
            public_takeover_url
            if active and isinstance(public_takeover_url, str) and public_takeover_url
            else self.manager.settings.takeover_url
        )
        api_url = public_api_url if active and isinstance(public_api_url, str) and public_api_url else None
        payload.update(
            {
                "active": active,
                "status": "stale" if stale else raw_status,
                "stale": stale,
                "source": "metadata_file",
                "takeover_url": takeover_url,
                "api_url": api_url,
                "last_updated": (last_updated.isoformat().replace("+00:00", "Z") if last_updated is not None else None),
                "age_seconds": age_seconds,
                "stale_after_seconds": stale_after_seconds,
                # Only these: the file also names the bastion, its user, a pid and the log path.
                "tunnel": self._shown_tunnel_fields(tunnel, stale_after_seconds),
            }
        )
        return payload

    @staticmethod
    def _shown_tunnel_fields(tunnel: dict[str, Any], stale_after_seconds: float) -> dict[str, Any]:
        """The sidecar fields the report passes on, as validated: stale_after_seconds as computed, updated_at only
        when it reads as a timestamp (and then in UTC), status and a URL only when they are a non-empty string. The
        raw file can hold NaN, "soon", a list or an object, and a NaN copied on made the REST report a 500 (B3-4)."""
        shown: dict[str, Any] = {}
        for key in _TUNNEL_FIELDS_SHOWN:
            if key not in tunnel:
                continue
            value = tunnel[key]
            if key == "stale_after_seconds":
                value = stale_after_seconds
            elif key == "updated_at":
                parsed = BrowserRemoteAccessService.parse_timestamp(value)
                if parsed is None:
                    continue
                value = parsed.isoformat().replace("+00:00", "Z")
            elif not (isinstance(value, str) and value):
                continue
            shown[key] = value
        return shown

    def _stale_after_seconds(self, value: Any) -> float:
        """The file's stale_after_seconds when it is a positive finite number, else the setting."""
        default = float(self.manager.settings.remote_access_stale_after_seconds)
        if value is None or isinstance(value, bool):
            return default
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too large for a float
            return default
        return seconds if math.isfinite(seconds) and seconds > 0 else default

    def session_info(self, session: "BrowserSession") -> dict[str, Any]:
        if session.isolation_mode != "docker_ephemeral":
            return self.global_info()

        shared_remote_access = self.global_info()
        takeover_url = session.takeover_url
        takeover_local_only = self.takeover_url_is_local_only(takeover_url)
        api_url = shared_remote_access.get("api_url")
        session_tunnel = self.manager.tunnel_broker.describe(session.tunnel)
        warning = None
        status = "active"
        active = True
        effective_takeover_url = takeover_url
        requires_direct_host_access = takeover_local_only
        local_only = takeover_local_only

        if session_tunnel and session_tunnel.get("active"):
            effective_takeover_url = str(session_tunnel["public_takeover_url"])
            requires_direct_host_access = False
            local_only = False
            status = "active"
            active = True
        elif not takeover_local_only:
            status = "active"
            active = True
            requires_direct_host_access = False
            local_only = False
        else:
            active = False
            status = "api_only" if api_url else "local_only"
            warning = (
                "This isolated takeover URL is still bound to a local host/port. "
                "Enable ISOLATED_TUNNEL_* settings or set ISOLATED_TAKEOVER_HOST to a remotely reachable hostname "
                "or IP if humans need remote takeover."
            )
            if session_tunnel and session_tunnel.get("status") in {"error", "degraded"}:
                status = "degraded"
                warning = (
                    "The isolated session tunnel is unavailable, so takeover fell back to the local-only URL. "
                    f"{session_tunnel.get('error') or ''}"
                ).strip()
            elif session.tunnel_error:
                status = "degraded"
                warning = (
                    "The isolated session tunnel could not be created, so takeover fell back to the local-only URL. "
                    f"{session.tunnel_error}"
                ).strip()

        payload = dict(shared_remote_access)
        payload.update(
            {
                "session_id": session.id,
                "source": (
                    "isolated_session_tunnel" if session_tunnel and session_tunnel.get("active") else "isolated_runtime"
                ),
                "configured_takeover_url": takeover_url,
                "takeover_url": effective_takeover_url,
                "local_only": local_only,
                "requires_direct_host_access": requires_direct_host_access,
                "shared_api_url": api_url,
                "shared_tunnel_active": bool(shared_remote_access.get("active")),
                "shared_tunnel": shared_remote_access.get("tunnel"),
                "session_tunnel": session_tunnel,
                "session_tunnel_error": session.tunnel_error,
                "active": active,
                "status": status,
                "warning": warning,
                "takeover_note": TAKEOVER_NOTE,
            }
        )
        if session.runtime is not None:
            payload["runtime"] = {
                "container_name": session.runtime.container_name,
                "browser_node": session.runtime.browser_node_name,
                "novnc_port": session.runtime.novnc_port,
            }
        return payload

    def takeover_link(self, session: "BrowserSession") -> dict[str, Any]:
        """The full takeover link a person opens: an isolated session's carries its noVNC password in the fragment.

        Only GET /sessions/{session_id}/takeover-link returns it (the dashboard and scripts/takeover_link.sh read
        it); no MCP tool and no other REST response does, since those reach the model. A shared browser-node
        session's link is the plain URL: its desktop takes the operator's VNC_PASSWORD.
        """
        url = self.current_takeover_url(session)
        password = getattr(session.runtime, "vnc_password", "") if session.runtime is not None else ""
        if password and session.isolation_mode == "docker_ephemeral":
            # Imported here: session_isolation imports the docker SDK lazily.
            from ...session_isolation import takeover_url_with_password

            url = takeover_url_with_password(url, password)
        return {"session_id": session.id, "takeover_url": url}

    def current_takeover_url(self, session: "BrowserSession | None" = None) -> str:
        if session is not None and session.isolation_mode == "docker_ephemeral":
            tunnel = self.manager.tunnel_broker.describe(session.tunnel)
            if tunnel and tunnel.get("active") and tunnel.get("public_takeover_url"):
                return str(tunnel["public_takeover_url"])
            return session.takeover_url
        remote_access = self.global_info()
        if remote_access.get("active") and remote_access.get("takeover_url"):
            return str(remote_access["takeover_url"])
        if session is not None:
            return session.takeover_url
        return self.manager.settings.takeover_url

    @staticmethod
    def parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (ValueError, OverflowError):  # OverflowError: a date the conversion to UTC takes out of range
            return None

    @staticmethod
    def takeover_url_is_local_only(value: str) -> bool:
        host = (urlparse(value).hostname or "").strip().lower()
        return is_local_only_host(host)
