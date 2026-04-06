from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .models import McpToolCallRequest

JSONRPC_VERSION = "2.0"
MCP_SESSION_HEADER = "MCP-Session-Id"
LEGACY_MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
)
CURRENT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
INITIALIZATION_REQUIRED_ERROR = -32002
logger = logging.getLogger(__name__)


@dataclass
class McpSession:
    id: str
    protocol_version: str
    client_info: dict[str, Any]
    client_capabilities: dict[str, Any]
    initialized: bool = False


class McpHttpTransport:
    def __init__(
        self,
        *,
        tool_gateway,
        server_name: str,
        server_version: str,
        server_title: str | None = None,
        allowed_origins: list[str] | None = None,
        session_store_path: str | None = None,
        manager=None,
    ):
        self.tool_gateway = tool_gateway
        self.server_name = server_name
        self.server_version = server_version
        self.server_title = server_title or server_name
        self.allowed_origins = tuple(allowed_origins or [])
        self._sessions: dict[str, McpSession] = {}
        self._session_store_path = Path(session_store_path).resolve() if session_store_path else None
        self.manager = manager  # BrowserManager for Resources protocol
        self._load_sessions()

    async def handle_post_request(self, request: Request) -> Response:
        origin_error = self._validate_origin(request)
        if origin_error is not None:
            return origin_error

        try:
            payload = await request.json()
        except ValueError:
            return self._json_error_response(None, -32700, "Invalid JSON payload", status_code=400)

        if isinstance(payload, list):
            return self._json_error_response(None, -32600, "JSON-RPC batches are not supported", status_code=400)
        if not isinstance(payload, dict):
            return self._json_error_response(None, -32600, "JSON-RPC body must be an object", status_code=400)
        if payload.get("jsonrpc") != JSONRPC_VERSION:
            return self._json_error_response(
                payload.get("id"),
                -32600,
                "Only JSON-RPC 2.0 is supported",
                status_code=400,
            )

        method = payload.get("method")
        if not isinstance(method, str) or not method:
            return self._json_error_response(payload.get("id"), -32600, "JSON-RPC method is required", status_code=400)

        if "id" not in payload:
            return await self._handle_notification(request, payload)
        return await self._handle_request(request, payload)

    async def handle_get_request(self, request: Request) -> Response:
        origin_error = self._validate_origin(request)
        if origin_error is not None:
            return origin_error
        return JSONResponse(
            status_code=405,
            headers={"Allow": "POST, DELETE"},
            content={"detail": "This MCP endpoint only supports POST JSON-RPC and DELETE session teardown."},
        )

    async def handle_delete_request(self, request: Request) -> Response:
        origin_error = self._validate_origin(request)
        if origin_error is not None:
            return origin_error

        session_id = self._read_session_id(request)
        if not session_id:
            return self._json_error_response(None, -32000, f"Missing required header: {MCP_SESSION_HEADER}", status_code=400)
        if self._sessions.pop(session_id, None) is None:
            return self._json_error_response(None, -32001, f"Unknown MCP session: {session_id}", status_code=404)
        self._persist_sessions()
        return Response(status_code=204)

    async def _handle_notification(self, request: Request, payload: dict[str, Any]) -> Response:
        session = self._require_session(request)
        if isinstance(session, Response):
            return session

        protocol_error = self._validate_protocol_header(request, session)
        if protocol_error is not None:
            return protocol_error

        method = payload["method"]
        if method == "notifications/initialized":
            session.initialized = True
            self._persist_sessions()
        return Response(status_code=202)

    async def _handle_request(self, request: Request, payload: dict[str, Any]) -> Response:
        request_id = payload.get("id")
        method = payload["method"]
        params = payload.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._json_error_response(request_id, -32602, "JSON-RPC params must be an object")

        if method == "initialize":
            if self._read_session_id(request):
                return self._json_error_response(
                    request_id,
                    -32600,
                    "Initialize requests must not include an MCP session header",
                    status_code=400,
                )
            return self._handle_initialize(request_id, params)

        session = self._require_session(request, request_id=request_id)
        if isinstance(session, Response):
            return session

        protocol_error = self._validate_protocol_header(request, session, request_id=request_id)
        if protocol_error is not None:
            return protocol_error

        if not session.initialized and method != "ping":
            return self._json_error_response(
                request_id,
                INITIALIZATION_REQUIRED_ERROR,
                "Session not initialized. Send notifications/initialized before calling tools.",
                headers=self._session_headers(session),
            )

        if method == "ping":
            return self._json_result_response(request_id, {}, headers=self._session_headers(session))
        if method == "tools/list":
            return self._json_result_response(
                request_id,
                {"tools": self.tool_gateway.list_tools()},
                headers=self._session_headers(session),
            )
        if method == "tools/call":
            try:
                tool_request = McpToolCallRequest.model_validate(params)
            except ValidationError as exc:
                return self._json_error_response(
                    request_id,
                    -32602,
                    "Invalid tools/call params",
                    data={"errors": exc.errors()},
                    headers=self._session_headers(session),
                )
            tool_response = await self.tool_gateway.call_tool(tool_request)
            return self._json_result_response(
                request_id,
                tool_response.model_dump(exclude_none=True),
                headers=self._session_headers(session),
            )

        # ── MCP Resources Protocol ─────────────────────────────────────────
        if method == "resources/list":
            return self._json_result_response(
                request_id,
                {"resources": await self._list_resources()},
                headers=self._session_headers(session),
            )
        if method == "resources/read":
            uri = params.get("uri", "")
            content = await self._read_resource(uri)
            if content is None:
                return self._json_error_response(
                    request_id,
                    -32002,
                    f"Resource not found: {uri}",
                    headers=self._session_headers(session),
                )
            return self._json_result_response(
                request_id,
                {"contents": [content]},
                headers=self._session_headers(session),
            )

        return self._json_error_response(
            request_id,
            -32601,
            f"Unknown MCP method: {method}",
            headers=self._session_headers(session),
        )

    async def _list_resources(self) -> list[dict[str, Any]]:
        """Return the list of subscribable browser resources."""
        resources: list[dict[str, Any]] = [
            {
                "uri": "browser://sessions",
                "name": "Active Sessions",
                "description": "List of all active browser sessions",
                "mimeType": "application/json",
            },
        ]
        if self.manager is None:
            return resources

        for session_id in list(self.manager.sessions.keys()):
            resources += [
                {
                    "uri": f"browser://{session_id}/screenshot",
                    "name": f"Screenshot [{session_id}]",
                    "description": f"Latest screenshot for session {session_id}",
                    "mimeType": "image/png",
                },
                {
                    "uri": f"browser://{session_id}/dom",
                    "name": f"DOM [{session_id}]",
                    "description": f"Current page HTML for session {session_id}",
                    "mimeType": "text/html",
                },
                {
                    "uri": f"browser://{session_id}/console",
                    "name": f"Console [{session_id}]",
                    "description": f"Recent console messages for session {session_id}",
                    "mimeType": "application/json",
                },
                {
                    "uri": f"browser://{session_id}/network",
                    "name": f"Network Log [{session_id}]",
                    "description": f"Recent network requests/responses for session {session_id}",
                    "mimeType": "application/json",
                },
            ]
        return resources

    async def _read_resource(self, uri: str) -> dict[str, Any] | None:
        """Fetch the content of a specific resource URI."""
        if uri == "browser://sessions":
            if self.manager is None:
                return {"uri": uri, "mimeType": "application/json", "text": "[]"}
            sessions = await self.manager.list_sessions()
            return {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(sessions, ensure_ascii=False),
            }

        # Parse browser://{session_id}/{resource}
        if not uri.startswith("browser://"):
            return None
        path = uri[len("browser://"):]
        parts = path.split("/", 1)
        if len(parts) != 2:
            return None
        session_id, resource = parts

        if self.manager is None:
            return None

        try:
            if resource == "screenshot":
                shot = await self.manager.capture_screenshot(session_id, label="mcp-resource")
                shot_path = Path(shot["screenshot_path"])
                if shot_path.exists():
                    import base64
                    img_b64 = base64.standard_b64encode(shot_path.read_bytes()).decode()
                    return {"uri": uri, "mimeType": "image/png", "blob": img_b64}
                return None

            if resource == "dom":
                session = await self.manager.get_session(session_id)
                html = await session.page.content()
                return {"uri": uri, "mimeType": "text/html", "text": html}

            if resource == "console":
                result = await self.manager.get_console_messages(session_id, limit=50)
                return {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(result.get("items", []), ensure_ascii=False),
                }

            if resource == "network":
                result = await self.manager.get_network_log(session_id, limit=100)
                return {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(result.get("entries", []), ensure_ascii=False),
                }

        except Exception as exc:
            logger.debug("resource read error for %s: %s", uri, exc)

        return None

    def _handle_initialize(self, request_id: Any, params: dict[str, Any]) -> Response:
        requested_version = params.get("protocolVersion")
        if requested_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return self._json_error_response(
                request_id,
                -32602,
                "Unsupported MCP protocol version",
                data={
                    "requested": requested_version,
                    "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                },
            )

        session = McpSession(
            id=uuid4().hex,
            protocol_version=requested_version,
            client_info=self._coerce_dict(params.get("clientInfo")),
            client_capabilities=self._coerce_dict(params.get("capabilities")),
        )
        self._sessions[session.id] = session
        self._persist_sessions()

        result = {
            "protocolVersion": session.protocol_version,
            "capabilities": {
                "tools": {},
                "resources": {"subscribe": False},
            },
            "serverInfo": {
                "name": self.server_name,
                "title": self.server_title,
                "version": self.server_version,
            },
            "instructions": (
                "Use these tools for supervised browser automation. Sensitive actions may require approvals "
                "or human takeover before execution."
            ),
        }
        return self._json_result_response(
            request_id,
            result,
            headers={**self._session_headers(session), MCP_SESSION_HEADER: session.id},
        )

    def _require_session(self, request: Request, *, request_id: Any = None) -> McpSession | Response:
        session_id = self._read_session_id(request)
        if not session_id:
            return self._json_error_response(
                request_id,
                -32000,
                f"Missing required header: {MCP_SESSION_HEADER}",
                status_code=400,
            )
        session = self._sessions.get(session_id)
        if session is None:
            return self._json_error_response(
                request_id,
                -32001,
                f"Unknown MCP session: {session_id}",
                status_code=404,
            )
        return session

    def _validate_protocol_header(self, request: Request, session: McpSession, *, request_id: Any = None) -> Response | None:
        protocol_version = request.headers.get(MCP_PROTOCOL_HEADER)
        if not protocol_version:
            return None
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return self._json_error_response(
                request_id,
                -32602,
                "Unsupported MCP protocol version",
                data={"requested": protocol_version, "supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
                status_code=400,
                headers=self._session_headers(session),
            )
        if protocol_version != session.protocol_version:
            return self._json_error_response(
                request_id,
                -32600,
                (
                    f"Protocol version mismatch for session {session.id}: expected {session.protocol_version}, "
                    f"got {protocol_version}"
                ),
                status_code=400,
                headers=self._session_headers(session),
            )
        return None

    def _validate_origin(self, request: Request) -> Response | None:
        origin = request.headers.get("origin")
        if not origin:
            return None

        normalized_origin = self._normalize_origin(origin)
        if normalized_origin is None:
            return self._json_error_response(None, -32000, "Malformed Origin header", status_code=400)

        allowed_origins = {
            normalized
            for normalized in (
                self._normalize_origin(origin_value) for origin_value in self.allowed_origins
            )
            if normalized
        }
        request_origin = self._normalize_origin(str(request.base_url))
        if request_origin:
            allowed_origins.add(request_origin)

        if normalized_origin not in allowed_origins:
            return self._json_error_response(
                None,
                -32000,
                f"Forbidden Origin header: {origin}",
                status_code=403,
            )
        return None

    @staticmethod
    def _normalize_origin(value: str) -> str | None:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    def _load_sessions(self) -> None:
        if self._session_store_path is None or not self._session_store_path.exists():
            return
        try:
            payload = json.loads(self._session_store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load MCP sessions from %s: %s", self._session_store_path, exc)
            return
        if not isinstance(payload, list):
            logger.warning("ignoring malformed MCP session store at %s", self._session_store_path)
            return
        restored: dict[str, McpSession] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                session = McpSession(
                    id=str(item["id"]),
                    protocol_version=str(item["protocol_version"]),
                    client_info=self._coerce_dict(item.get("client_info")),
                    client_capabilities=self._coerce_dict(item.get("client_capabilities")),
                    initialized=bool(item.get("initialized", False)),
                )
            except Exception:
                continue
            restored[session.id] = session
        self._sessions = restored

    def _persist_sessions(self) -> None:
        if self._session_store_path is None:
            return
        self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._session_store_path.with_suffix(".json.tmp")
        payload = [asdict(session) for session in self._sessions.values()]
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._session_store_path)

    @staticmethod
    def _coerce_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _read_session_id(request: Request) -> str | None:
        return request.headers.get(MCP_SESSION_HEADER) or request.headers.get(LEGACY_MCP_SESSION_HEADER)

    @staticmethod
    def _json_result(request_id: Any, result: Any) -> dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "result": result,
        }

    @staticmethod
    def _json_error(request_id: Any, code: int, message: str, *, data: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": error,
        }

    def _json_result_response(
        self,
        request_id: Any,
        result: Any,
        *,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> JSONResponse:
        return JSONResponse(status_code=status_code, content=self._json_result(request_id, result), headers=headers)

    def _json_error_response(
        self,
        request_id: Any,
        code: int,
        message: str,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content=self._json_error(request_id, code, message, data=data),
            headers=headers,
        )

    @staticmethod
    def _session_headers(session: McpSession) -> dict[str, str]:
        return {MCP_PROTOCOL_HEADER: session.protocol_version}
