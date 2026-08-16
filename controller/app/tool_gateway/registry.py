from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from ..models import McpToolDescriptor

READ_ONLY_TOOL_NAMES = {
    "browser.get_auth_profile",
    "browser.get_console",
    "browser.get_cookies",
    "browser.get_html",
    "browser.get_local_storage",
    "browser.get_memory_profile",
    "browser.get_network_log",
    "browser.get_page_errors",
    "browser.get_remote_access",
    "browser.get_request_failures",
    "browser.get_session",
    "browser.list_agent_jobs",
    "browser.list_approvals",
    "browser.list_auth_profiles",
    "browser.list_cron_jobs",
    "browser.list_downloads",
    "browser.list_memory_profiles",
    "browser.list_providers",
    "browser.list_proxy_personas",
    "browser.list_sessions",
    "browser.list_tabs",
    "browser.pii_scrubber_status",
    "browser.readiness_check",
    "harness.check_all_drifts",
    "harness.check_drift",
    "harness.get_candidate",
    "harness.get_status",
    "harness.get_trace",
    "harness.list_candidates",
    "harness.list_runs",
    "youcom.search",
    "youcom.contents",
}

READ_ONLY_NON_IDEMPOTENT_TOOL_NAMES = {
    "browser.find_by_vision",
    "browser.observe",
    "browser.screenshot",
    "browser.wait_for_selector",
}

DESTRUCTIVE_TOOL_NAMES = {
    "browser.cancel_agent_job",
    "browser.close_session",
    "browser.close_tab",
    "browser.delete_cron_job",
    "browser.delete_memory_profile",
    "browser.delete_proxy_persona",
    "browser.discard_agent_job",
}

OPEN_WORLD_TOOL_NAMES = {
    "browser.activate_tab",
    "browser.cdp_attach",
    "browser.close_tab",
    "browser.create_session",
    "browser.drag_drop",
    "browser.eval_js",
    "browser.execute_action",
    "browser.find_by_vision",
    "browser.find_elements",
    "browser.fork_session",
    "browser.get_console",
    "browser.get_cookies",
    "browser.get_html",
    "browser.get_local_storage",
    "browser.get_network_log",
    "browser.get_page_errors",
    "browser.get_request_failures",
    "browser.get_session",
    "browser.list_downloads",
    "browser.list_tabs",
    "browser.observe",
    "browser.request_human_takeover",
    "browser.screenshot",
    "browser.set_cookies",
    "browser.set_local_storage",
    "browser.set_viewport",
    "browser.share_session",
    "browser.wait_for_selector",
    "harness.start_convergence",
}


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[dict[str, Any] | list[dict[str, Any]]]]
    profiles: tuple[str, ...] = ("curated", "full")
    experimental: str | None = None
    governed_kind: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None

    def annotations(self) -> dict[str, bool]:
        read_only = self._read_only_hint()
        return {
            "readOnlyHint": read_only,
            "destructiveHint": self._destructive_hint(read_only),
            "idempotentHint": self._idempotent_hint(read_only),
            "openWorldHint": self._open_world_hint(),
        }

    def _read_only_hint(self) -> bool:
        if self.read_only_hint is not None:
            return self.read_only_hint
        return self.name in READ_ONLY_TOOL_NAMES or self.name in READ_ONLY_NON_IDEMPOTENT_TOOL_NAMES

    def _destructive_hint(self, read_only: bool) -> bool:
        if self.destructive_hint is not None:
            return self.destructive_hint
        if read_only:
            return False
        return self.name in DESTRUCTIVE_TOOL_NAMES or self.governed_kind == "destructive"

    def _idempotent_hint(self, read_only: bool) -> bool:
        if self.idempotent_hint is not None:
            return self.idempotent_hint
        if self.name in READ_ONLY_NON_IDEMPOTENT_TOOL_NAMES:
            return False
        return read_only

    def _open_world_hint(self) -> bool:
        if self.open_world_hint is not None:
            return self.open_world_hint
        return self.name in OPEN_WORLD_TOOL_NAMES


class ToolRegistry:
    def __init__(
        self,
        *,
        tool_profile: str,
        experimental_enabled: Callable[[str | None], bool],
    ) -> None:
        self.tool_profile = "full" if tool_profile == "full" else "curated"
        self._experimental_enabled = experimental_enabled
        self._tools: dict[str, ToolSpec] = {}
        self._descriptor_cache_json: str | None = None

    def register(self, spec: ToolSpec) -> None:
        if self.tool_profile not in spec.profiles:
            return
        if not self._experimental_enabled(spec.experimental):
            return

        self._tools[spec.name] = spec
        self._descriptor_cache_json = None

    def unregister(self, name: str) -> None:
        if self._tools.pop(name, None) is not None:
            self._descriptor_cache_json = None

    @property
    def tools(self) -> dict[str, ToolSpec]:
        return self._tools

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        if self._descriptor_cache_json is None:
            descriptors = [
                McpToolDescriptor(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.input_model.model_json_schema(),
                    annotations=spec.annotations(),
                ).model_dump(exclude_none=True)
                for spec in self._tools.values()
            ]
            self._descriptor_cache_json = json.dumps(descriptors, separators=(",", ":"))

        return json.loads(self._descriptor_cache_json)
