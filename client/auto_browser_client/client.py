"""
auto_browser_client.client — Sync/async Python client for the auto-browser REST API.

Usage (sync):
    from auto_browser_client import AutoBrowserClient

    client = AutoBrowserClient("http://localhost:8000", token="secret")
    session = client.create_session(start_url="https://example.com")
    obs = client.observe(session["id"], preset="fast")
    client.navigate(session["id"], url="https://news.ycombinator.com")
    client.close_session(session["id"])

Usage (async):
    async with AutoBrowserClient("http://localhost:8000") as client:
        session = await client.async_create_session(start_url="https://example.com")
        obs = await client.async_observe(session["id"])
        await client.async_close_session(session["id"])
"""
from __future__ import annotations

import contextlib
from typing import Any, Generator, Iterator

import httpx


class AutoBrowserError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class AutoBrowserClient:
    """Thin wrapper around the auto-browser REST API.

    All methods have both a sync variant (e.g. ``create_session``) and an
    async variant prefixed with ``async_`` (e.g. ``async_create_session``).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        token: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._timeout = timeout

    # ── Context managers ─────────────────────────────────────────────────────

    def __enter__(self) -> "AutoBrowserClient":
        self._sync_client = httpx.Client(base_url=self.base_url, headers=self._headers, timeout=self._timeout)
        return self

    def __exit__(self, *_) -> None:
        if self._sync_client:
            self._sync_client.close()
            self._sync_client = None

    async def __aenter__(self) -> "AutoBrowserClient":
        self._async_client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=self._timeout)
        return self

    async def __aexit__(self, *_) -> None:
        if self._async_client:
            await self._async_client.aclose()
            self._async_client = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(base_url=self.base_url, headers=self._headers, timeout=self._timeout)
        return self._sync_client

    def _aclient(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=self._timeout)
        return self._async_client

    @staticmethod
    def _raise(r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise AutoBrowserError(r.status_code, detail)

    def _get(self, path: str, **params) -> Any:
        r = self._client().get(path, params=params)
        self._raise(r)
        return r.json()

    def _post(self, path: str, body: dict | None = None) -> Any:
        r = self._client().post(path, json=body or {})
        self._raise(r)
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self._client().delete(path)
        self._raise(r)
        return r.json()

    async def _aget(self, path: str, **params) -> Any:
        r = await self._aclient().get(path, params=params)
        self._raise(r)
        return r.json()

    async def _apost(self, path: str, body: dict | None = None) -> Any:
        r = await self._aclient().post(path, json=body or {})
        self._raise(r)
        return r.json()

    async def _adelete(self, path: str) -> Any:
        r = await self._aclient().delete(path)
        self._raise(r)
        return r.json()

    # ── Health ───────────────────────────────────────────────────────────────

    def health(self) -> dict:
        return self._get("/healthz")

    async def async_health(self) -> dict:
        return await self._aget("/healthz")

    # ── Sessions ─────────────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        return self._get("/sessions")

    async def async_list_sessions(self) -> list[dict]:
        return await self._aget("/sessions")

    def create_session(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        auth_profile: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if start_url:
            body["start_url"] = start_url
        if auth_profile:
            body["auth_profile"] = auth_profile
        return self._post("/sessions", body)

    async def async_create_session(
        self,
        *,
        name: str | None = None,
        start_url: str | None = None,
        auth_profile: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if start_url:
            body["start_url"] = start_url
        if auth_profile:
            body["auth_profile"] = auth_profile
        return await self._apost("/sessions", body)

    def get_session(self, session_id: str) -> dict:
        return self._get(f"/sessions/{session_id}")

    async def async_get_session(self, session_id: str) -> dict:
        return await self._aget(f"/sessions/{session_id}")

    def close_session(self, session_id: str) -> dict:
        return self._delete(f"/sessions/{session_id}")

    async def async_close_session(self, session_id: str) -> dict:
        return await self._adelete(f"/sessions/{session_id}")

    # ── Observe ──────────────────────────────────────────────────────────────

    def observe(
        self,
        session_id: str,
        *,
        preset: str = "normal",
        limit: int = 40,
    ) -> dict:
        """Observe the current browser state.

        preset: "fast" (screenshot only), "normal" (default), "rich" (extended)
        """
        return self._post(f"/sessions/{session_id}/observe", {"preset": preset, "limit": limit})

    async def async_observe(
        self,
        session_id: str,
        *,
        preset: str = "normal",
        limit: int = 40,
    ) -> dict:
        return await self._apost(f"/sessions/{session_id}/observe", {"preset": preset, "limit": limit})

    # ── Navigation & actions ─────────────────────────────────────────────────

    def navigate(self, session_id: str, url: str) -> dict:
        return self._post(f"/sessions/{session_id}/actions/navigate", {"url": url})

    async def async_navigate(self, session_id: str, url: str) -> dict:
        return await self._apost(f"/sessions/{session_id}/actions/navigate", {"url": url})

    def click(self, session_id: str, *, selector: str | None = None, element_id: str | None = None, x: float | None = None, y: float | None = None) -> dict:
        body: dict[str, Any] = {}
        if selector:
            body["selector"] = selector
        if element_id:
            body["element_id"] = element_id
        if x is not None:
            body["x"] = x
        if y is not None:
            body["y"] = y
        return self._post(f"/sessions/{session_id}/actions/click", body)

    async def async_click(self, session_id: str, *, selector: str | None = None, element_id: str | None = None, x: float | None = None, y: float | None = None) -> dict:
        body: dict[str, Any] = {}
        if selector:
            body["selector"] = selector
        if element_id:
            body["element_id"] = element_id
        if x is not None:
            body["x"] = x
        if y is not None:
            body["y"] = y
        return await self._apost(f"/sessions/{session_id}/actions/click", body)

    def type_text(self, session_id: str, text: str, *, selector: str | None = None, element_id: str | None = None, clear_first: bool = True) -> dict:
        body: dict[str, Any] = {"text": text, "clear_first": clear_first}
        if selector:
            body["selector"] = selector
        if element_id:
            body["element_id"] = element_id
        return self._post(f"/sessions/{session_id}/actions/type", body)

    async def async_type_text(self, session_id: str, text: str, *, selector: str | None = None, element_id: str | None = None, clear_first: bool = True) -> dict:
        body: dict[str, Any] = {"text": text, "clear_first": clear_first}
        if selector:
            body["selector"] = selector
        if element_id:
            body["element_id"] = element_id
        return await self._apost(f"/sessions/{session_id}/actions/type", body)

    def scroll(self, session_id: str, *, delta_x: float = 0, delta_y: float = 600) -> dict:
        return self._post(f"/sessions/{session_id}/actions/scroll", {"delta_x": delta_x, "delta_y": delta_y})

    async def async_scroll(self, session_id: str, *, delta_x: float = 0, delta_y: float = 600) -> dict:
        return await self._apost(f"/sessions/{session_id}/actions/scroll", {"delta_x": delta_x, "delta_y": delta_y})

    def screenshot(self, session_id: str, label: str = "manual") -> dict:
        return self._post(f"/sessions/{session_id}/screenshot", {"label": label})

    async def async_screenshot(self, session_id: str, label: str = "manual") -> dict:
        return await self._apost(f"/sessions/{session_id}/screenshot", {"label": label})

    def screenshot_diff(self, session_id: str) -> dict:
        """Capture screenshot and diff against the most recent prior screenshot."""
        return self._post(f"/sessions/{session_id}/screenshot/compare")

    async def async_screenshot_diff(self, session_id: str) -> dict:
        return await self._apost(f"/sessions/{session_id}/screenshot/compare")

    # ── Agent ────────────────────────────────────────────────────────────────

    def agent_step(self, session_id: str, *, provider: str, goal: str, **kwargs) -> dict:
        return self._post(f"/sessions/{session_id}/agent/step", {"provider": provider, "goal": goal, **kwargs})

    async def async_agent_step(self, session_id: str, *, provider: str, goal: str, **kwargs) -> dict:
        return await self._apost(f"/sessions/{session_id}/agent/step", {"provider": provider, "goal": goal, **kwargs})

    def agent_run(self, session_id: str, *, provider: str, goal: str, max_steps: int = 6, **kwargs) -> dict:
        return self._post(f"/sessions/{session_id}/agent/run", {"provider": provider, "goal": goal, "max_steps": max_steps, **kwargs})

    async def async_agent_run(self, session_id: str, *, provider: str, goal: str, max_steps: int = 6, **kwargs) -> dict:
        return await self._apost(f"/sessions/{session_id}/agent/run", {"provider": provider, "goal": goal, "max_steps": max_steps, **kwargs})

    # ── Approvals ────────────────────────────────────────────────────────────

    def list_approvals(self, *, status: str | None = None, session_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if session_id:
            params["session_id"] = session_id
        return self._get("/approvals", **params)

    async def async_list_approvals(self, *, status: str | None = None, session_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if session_id:
            params["session_id"] = session_id
        return await self._aget("/approvals", **params)

    def approve(self, approval_id: str, comment: str | None = None) -> dict:
        return self._post(f"/approvals/{approval_id}/approve", {"comment": comment})

    async def async_approve(self, approval_id: str, comment: str | None = None) -> dict:
        return await self._apost(f"/approvals/{approval_id}/approve", {"comment": comment})

    def reject(self, approval_id: str, comment: str | None = None) -> dict:
        return self._post(f"/approvals/{approval_id}/reject", {"comment": comment})

    async def async_reject(self, approval_id: str, comment: str | None = None) -> dict:
        return await self._apost(f"/approvals/{approval_id}/reject", {"comment": comment})

    # ── Auth profiles ────────────────────────────────────────────────────────

    def list_auth_profiles(self) -> list[dict]:
        return self._get("/auth-profiles")

    async def async_list_auth_profiles(self) -> list[dict]:
        return await self._aget("/auth-profiles")

    def save_auth_profile(self, session_id: str, profile_name: str) -> dict:
        return self._post(f"/sessions/{session_id}/auth-profiles", {"profile_name": profile_name})

    async def async_save_auth_profile(self, session_id: str, profile_name: str) -> dict:
        return await self._apost(f"/sessions/{session_id}/auth-profiles", {"profile_name": profile_name})

    def import_auth_profile(self, archive_path: str, *, overwrite: bool = False) -> dict:
        return self._post("/auth-profiles/import", {"archive_path": archive_path, "overwrite": overwrite})

    async def async_import_auth_profile(self, archive_path: str, *, overwrite: bool = False) -> dict:
        return await self._apost("/auth-profiles/import", {"archive_path": archive_path, "overwrite": overwrite})

    # ── SSE event stream (sync generator) ────────────────────────────────────

    def stream_events(self, session_id: str) -> Generator[dict, None, None]:
        """Yield parsed event dicts from the SSE stream. Blocks until disconnected."""
        import json as _json

        url = f"{self.base_url}/sessions/{session_id}/events"
        with httpx.stream("GET", url, headers=self._headers, timeout=None) as r:
            self._raise(r)
            buffer = ""
            for chunk in r.iter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    for line in block.splitlines():
                        if line.startswith("data: "):
                            try:
                                yield _json.loads(line[6:])
                            except Exception:
                                pass

    # ── Audit ────────────────────────────────────────────────────────────────

    def list_audit_events(self, *, limit: int = 50, session_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        return self._get("/audit/events", **params)

    async def async_list_audit_events(self, *, limit: int = 50, session_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        return await self._aget("/audit/events", **params)
