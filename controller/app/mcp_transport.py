from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import InitVar, dataclass, field, fields
from itertools import chain
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError

from . import events as _events
from .action_errors import SessionNotFoundError
from .audit import get_current_operator
from .browser.page_read import bounded, locked_read
from .mcp_instructions import server_instructions
from .models import McpToolCallRequest, OperatorIdentity
from .safe_json_response import SafeJSONResponse, errors_to_repeat
from .session_ownership import ANONYMOUS, acting_as, may_use, verified_operator
from .tool_inputs import MAX_TOOL_RESULT_CHARS
from .utils import (
    PRIVATE_FILE_MODE,
    atomic_write_text,
    capped_for_log,
    holds_lone_surrogate,
    json_safe,
    quoted_for_log,
    utc_now,
    without_lone_surrogates,
)

JSONRPC_VERSION = "2.0"
MCP_SESSION_HEADER = "MCP-Session-Id"
LEGACY_MCP_SESSION_HEADER = "Mcp-Session-Id"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
)
CURRENT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
# -32002 is the spec's "resource not found" code and is used as such below, so
# reusing it here left clients unable to tell "send initialized first" apart
# from "no such resource". -32005 sits in the implementation-defined range.
INITIALIZATION_REQUIRED_ERROR = -32005
# Readable, but no event ever notifies them (_updated_resource_uris), so a subscription would wait for ever.
NEVER_NOTIFIED_RESOURCES = frozenset({"browser://audit/events"})
# The most characters of a page's DOM one read returns: the most a tool result may return, so a page a tool could
# hand over whole is not cut here. A longer page ends in a comment, counted in this cap, that says so and names the
# paging tool.
MAX_DOM_RESOURCE_CHARS = MAX_TOOL_RESULT_CHARS
# How long GET /mcp's event stream waits for an event before it sends a keepalive.
_STREAM_KEEPALIVE_SECONDS = 15
# The most MCP sessions the table keeps, and the most one token-verified operator keeps: past its
# cap an operator loses its own least recently used sessions, so its initialize flood cannot evict
# another operator's. Unowned sessions (shared token, no token) are bounded by the table's cap alone.
MAX_MCP_SESSIONS = 500
MAX_MCP_SESSIONS_PER_OWNER = 100
# How deep an initialize's clientInfo or capabilities may be nested (the object itself is the first level, and a list
# is a level as an object is), and the most bytes either may take written compactly as JSON in UTF-8. Both are kept
# with the session, and the table of sessions is written whole, on the event loop, at every write of it: one nested
# some hundreds deep was written by its initialize and stopped the writer (RecursionError) for every client's DELETE,
# which writes from a shallower stack, and one of 32 MiB was written again at every write by anybody. What MCP
# defines for the two needs three or four levels and well under a kilobyte.
MAX_CLIENT_OBJECT_DEPTH = 32
MAX_CLIENT_OBJECT_JSON_BYTES = 64 * 1024
# How many decimal digits a bit of an integer is counted for (_json_length_at_least): 0.30102, just under log10(2).
_DIGITS_PER_BIT, _DIGITS_PER_BIT_SCALE = 30_102, 100_000
# How the JSON writer writes a float that is no finite number, by its repr.
_NON_FINITE_AS_JSON = {"nan": "NaN", "inf": "Infinity", "-inf": "-Infinity"}
logger = logging.getLogger(__name__)


@dataclass
class McpSession:
    id: str
    protocol_version: str
    client_info: dict[str, Any]
    client_capabilities: dict[str, Any]
    initialized: bool = False
    created_at: str = ""
    resource_subscriptions: list[str] = field(default_factory=list)
    # Updated in memory on every request and saved with the next store write;
    # eviction goes by this, so a busy long-lived client is not dropped.
    last_used_at: str = ""
    # The token-verified operator who initialized it (None: unowned, open to all,
    # as sessions are). Another operator finds no such MCP session.
    owner: str | None = None
    # client_info and client_capabilities as _compact_json wrote them to measure them (an initialize, a start): see
    # client_json.
    measured_json: InitVar[tuple[str, str] | None] = None

    def __post_init__(self, measured_json: tuple[str, str] | None) -> None:
        if not self.created_at:
            self.created_at = utc_now()
        if not self.last_used_at:
            self.last_used_at = self.created_at
        # client_info and client_capabilities as the table writes them, written once: by the bound that measured them,
        # or here. Each is the client's and may take up to the bounds, and neither changes over the session's life,
        # where the fields above do. It is no field, so it is neither written nor compared: table_record joins it into
        # each write of the table.
        self.client_json: tuple[str, str] = measured_json or (
            _compact_json(self.client_info),
            _compact_json(self.client_capabilities),
        )

    def table_record(self) -> str:
        """The session's record in the table: what _compact_json(asdict(self)) writes, byte for byte, of its client
        fields as they were written once (client_json) and of each other field written now, as they are small and
        some change over the session's life.

        asdict's deep copy of the client fields, and their encoding, at every write of the whole table took some 13 s
        on the event loop for a table at its cap of 500 sessions with fields of small numbers at the bounds.
        """
        client_json = dict(zip(("client_info", "client_capabilities"), self.client_json, strict=True))
        written = []
        for item in fields(self):
            value = client_json[item.name] if item.name in client_json else _compact_json(getattr(self, item.name))
            written.append(f"{_compact_json(item.name)}:{value}")
        return "{" + ",".join(written) + "}"


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
        # The stop event of each MCP session's one live GET stream. Never saved: a stream does not outlive the process.
        # A response whose body never starts leaves its entry until the session's next GET, DELETE or eviction, holding
        # no queue and no task.
        self._event_streams: dict[str, asyncio.Event] = {}
        self._session_store_path = Path(session_store_path).resolve() if session_store_path else None
        self.manager = manager  # BrowserManager for Resources protocol
        self._load_sessions()

    async def handle_post_request(self, request: Request) -> Response:
        origin_error = self._validate_origin(request)
        if origin_error is not None:
            return origin_error

        try:
            payload = await request.json()
        except (ValueError, RecursionError):
            # RecursionError: a body nested past what the JSON reader goes down, which is no ValueError.
            return self._json_error_response(None, -32700, "Invalid JSON payload", status_code=400)

        if isinstance(payload, list):
            return self._json_error_response(None, -32600, "JSON-RPC batches are not supported", status_code=400)
        if not isinstance(payload, dict):
            return self._json_error_response(None, -32600, "JSON-RPC body must be an object", status_code=400)
        # Before anything repeats it: every answer repeats the id whole, and one nested some hundreds of lists deep made
        # an answer's walks give up (an unhandled 500). An id that cannot be read is answered with id null.
        if not _is_a_json_rpc_id(payload.get("id")):
            return self._json_error_response(
                None, -32600, "JSON-RPC id must be a string, a number or null", status_code=400
            )
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

        session = self._require_session(request)
        if isinstance(session, Response):
            return session
        protocol_error = self._validate_protocol_header(request, session)
        if protocol_error is not None:
            return protocol_error
        if not session.initialized:
            return self._json_error_response(
                None,
                INITIALIZATION_REQUIRED_ERROR,
                "Session not initialized. Send notifications/initialized before opening the event stream.",
                headers=self._session_headers(session),
            )

        # Here, not in the generator, which starts only when the body is streamed: two GETs would race it.
        stop = self._take_over_event_stream(session.id)
        # The body is streamed after this handler has returned, and the HTTP
        # middleware resets the operator once its call_next returns, so the
        # operator is not reliably set while the stream runs: take it now.
        return StreamingResponse(
            self._resource_event_stream(request, session, stop, get_current_operator()),
            media_type="text/event-stream",
            headers={
                **self._session_headers(session),
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def handle_delete_request(self, request: Request) -> Response:
        origin_error = self._validate_origin(request)
        if origin_error is not None:
            return origin_error

        session_id = self._read_session_id(request)
        if not session_id:
            return self._json_error_response(
                None, -32000, f"Missing required header: {MCP_SESSION_HEADER}", status_code=400
            )
        session = self._sessions.get(session_id)
        if session is None or not may_use(session.owner):
            # The header as sent is as long as the server lets a header be: cut, as the routes' 404s cut an id.
            return self._json_error_response(
                None, -32001, f"Unknown MCP session: {capped_for_log(session_id)}", status_code=404
            )
        protocol_error = self._validate_protocol_header(request, session)
        if protocol_error is not None:
            return protocol_error
        self._forget_session(session_id)
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
        # Once: each one wrote the whole table, so a client could send it again and again to have the table written.
        if method == "notifications/initialized" and not session.initialized:
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
                    data={"errors": errors_to_repeat(exc.errors())},
                    headers=self._session_headers(session),
                )
            tool_response = await self.tool_gateway.call_tool(tool_request)
            result = tool_response.model_dump(exclude_none=True, by_alias=True)
            # Protocol versions supported here require structuredContent to be an object.
            # List and scalar results remain available in the text content block.
            if not isinstance(result.get("structuredContent"), dict):
                result.pop("structuredContent", None)
            return self._json_result_response(
                request_id,
                result,
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
            return await self._handle_resource_read(request_id, session, params)
        if method == "resources/subscribe":
            return await self._handle_resource_subscribe(request_id, session, params)
        if method == "resources/unsubscribe":
            return self._handle_resource_unsubscribe(request_id, session, params)

        return self._json_error_response(
            request_id,
            -32601,
            f"Unknown MCP method: {capped_for_log(method)}",
            headers=self._session_headers(session),
        )

    async def _list_resources(self) -> list[dict[str, Any]]:
        """Return the browser resources a client can read; all but NEVER_NOTIFIED_RESOURCES can be subscribed to."""
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
        if callable(getattr(self.manager, "list_audit_events", None)):
            resources.append(
                {
                    "uri": "browser://audit/events",
                    "name": "Recent Audit Events",
                    "description": "Recent browser audit events across sessions",
                    "mimeType": "application/json",
                }
            )

        # Only sessions the caller may use: listing the rest would hand out the ids
        # to subscribe to, and subscribing is validated against this list.
        for session_id, live in list(self.manager.sessions.items()):
            if not may_use(getattr(live, "owner", None)):
                continue
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

    async def _handle_resource_read(
        self,
        request_id: Any,
        session: McpSession,
        params: dict[str, Any],
    ) -> Response:
        uri = params.get("uri")
        headers = self._session_headers(session)
        if not isinstance(uri, str) or not uri:
            return self._json_error_response(
                request_id,
                -32602,
                "resources/read requires a non-empty uri",
                headers=headers,
            )
        # The one place a read's failure becomes an answer. -32002 is only for what does not exist; a session of
        # another operator's raises SessionNotFoundError, and must read the same as a resource that was never there.
        try:
            content = await self._read_resource(uri)
        except SessionNotFoundError:
            content = None
        except Exception as exc:
            # The class only, and the URI quoted and cut, as it is caller input of any length: an exception's text can
            # carry a page URL with a token in it, so the client's message is fixed text and the log keeps the class.
            logger.warning("MCP resource read of %s failed: %s", quoted_for_log(uri), type(exc).__name__)
            return self._json_error_response(
                request_id, -32603, f"Could not read resource: {capped_for_log(uri)}", headers=headers
            )
        if content is None:
            return self._json_error_response(
                request_id, -32002, f"Resource not found: {capped_for_log(uri)}", headers=headers
            )
        return self._json_result_response(request_id, {"contents": [content]}, headers=headers)

    async def _read_resource(self, uri: str) -> dict[str, Any] | None:
        """Fetch the content of a resource URI, or None if it names no resource.

        Any other failure raises, and _handle_resource_read decides the answer.
        """
        if uri == "browser://sessions":
            if self.manager is None:
                return {"uri": uri, "mimeType": "application/json", "text": "[]"}
            sessions = await self.manager.list_sessions()
            return _json_resource(uri, sessions)
        if uri == "browser://audit/events":
            if self.manager is None or not callable(getattr(self.manager, "list_audit_events", None)):
                return None
            events = await self.manager.list_audit_events(limit=100)
            return _json_resource(uri, events)

        # Parse browser://{session_id}/{resource}
        if not uri.startswith("browser://"):
            return None
        path = uri[len("browser://") :]
        parts = path.split("/", 1)
        if len(parts) != 2:
            return None
        session_id, resource = parts

        if self.manager is None:
            return None

        if resource == "screenshot":
            import base64

            shot = await self.manager.capture_screenshot(session_id, label="mcp-resource")
            img_b64 = base64.standard_b64encode(Path(shot["screenshot_path"]).read_bytes()).decode()
            return {"uri": uri, "mimeType": "image/png", "blob": img_b64}

        if resource == "dom":
            session = await self.manager.get_session(session_id)
            async with locked_read(self.manager, session):
                html = await bounded(session.page.content())
            return {"uri": uri, "mimeType": "text/html", "text": _capped_dom(html)}

        if resource == "console":
            result = await self.manager.get_console_messages(session_id, limit=50)
            return _json_resource(uri, result.get("items", []))

        if resource == "network":
            result = await self.manager.get_network_log(session_id, limit=100)
            return _json_resource(uri, result.get("entries", []))

        return None

    async def _handle_resource_subscribe(
        self,
        request_id: Any,
        session: McpSession,
        params: dict[str, Any],
    ) -> Response:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return self._json_error_response(
                request_id,
                -32602,
                "resources/subscribe requires a non-empty uri",
                headers=self._session_headers(session),
            )
        known_uris = {resource["uri"] for resource in await self._list_resources()}
        if uri not in known_uris:
            return self._json_error_response(
                request_id,
                -32002,
                f"Resource not found: {capped_for_log(uri)}",
                headers=self._session_headers(session),
            )
        if uri in NEVER_NOTIFIED_RESOURCES:
            return self._json_error_response(
                request_id,
                -32602,
                f"Resource is not notified of changes; poll resources/read instead: {uri}",
                headers=self._session_headers(session),
            )
        if uri not in session.resource_subscriptions:
            session.resource_subscriptions.append(uri)
            session.resource_subscriptions.sort()
            self._persist_sessions()
        return self._json_result_response(request_id, {}, headers=self._session_headers(session))

    def _handle_resource_unsubscribe(
        self,
        request_id: Any,
        session: McpSession,
        params: dict[str, Any],
    ) -> Response:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return self._json_error_response(
                request_id,
                -32602,
                "resources/unsubscribe requires a non-empty uri",
                headers=self._session_headers(session),
            )
        if uri in session.resource_subscriptions:
            session.resource_subscriptions.remove(uri)
            self._persist_sessions()
        return self._json_result_response(request_id, {}, headers=self._session_headers(session))

    def _take_over_event_stream(self, session_id: str) -> asyncio.Event:
        """Stop the session's live stream, if any, and register the next one: one stream per MCP session."""
        self._stop_event_stream(session_id)
        stop = self._event_streams[session_id] = asyncio.Event()
        return stop

    def _stop_event_stream(self, session_id: str) -> None:
        stop = self._event_streams.pop(session_id, None)
        if stop is not None:
            stop.set()

    def _forget_session(self, session_id: str) -> None:
        """Drop a session and end its stream; every deletion of a session (DELETE, eviction) goes through here."""
        del self._sessions[session_id]
        self._stop_event_stream(session_id)

    async def _resource_event_stream(
        self,
        request: Request,
        session: McpSession,
        stop: asyncio.Event,
        operator: OperatorIdentity | None = None,
    ):
        """GET /mcp's body: a stream of update notifications until stop is set, the client goes or the body is closed.

        A stopped stream says nothing more, but the stop wakes only the wait for an event: a stream in an owner lookup
        ends when that returns, and one blocked in sending to a client that has stopped reading ends when that send
        returns or the connection goes.
        """
        queue = _events.subscribe_all()
        try:
            yield ": connected\n\n"
            while not stop.is_set():
                if await request.is_disconnected():
                    break
                # A listening stream is use, even on a turn that reaches neither a keepalive nor a notification;
                # eviction goes by this. Saved with the next store write, not per turn.
                session.last_used_at = utc_now()
                payload = await _next_stream_event(queue, stop)
                if stop.is_set():
                    break
                if payload is None:
                    yield ": keepalive\n\n"
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug("dropping malformed browser event payload from MCP resource stream")
                    continue
                if not await self._may_see_event(event, operator):
                    continue
                for uri in self._updated_resource_uris(event, subscribed=session.resource_subscriptions):
                    # Here and not only in the wait: a stop can land during the owner lookup above or the send below.
                    if stop.is_set():
                        break
                    message = {
                        "jsonrpc": JSONRPC_VERSION,
                        "method": "notifications/resources/updated",
                        "params": {"uri": uri},
                    }
                    yield f"event: message\ndata: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            _events.unsubscribe_all(queue)
            # Only the stream that owns the entry removes it: a successor may hold it by now.
            if self._event_streams.get(session.id) is stop:
                del self._event_streams[session.id]

    async def _may_see_event(self, event: dict[str, Any], operator: OperatorIdentity | None) -> bool:
        """Whether the subscriber may hear about this event's session.

        Per-session URIs can only be subscribed to for sessions the subscriber
        could use then (see _list_resources); this also keeps the generic
        browser://sessions update from telling a subscriber that another
        operator's session is active or has closed. A session that is no longer
        live (its close is announced after it leaves manager.sessions) is judged
        by its record; an id with neither names no operator and is let through.
        """
        session_id = event.get("session_id")
        if self.manager is None or not isinstance(session_id, str):
            return True
        live = self.manager.sessions.get(session_id)
        if live is not None:
            owner = getattr(live, "owner", None)
        else:
            try:
                _known, owner = await self.manager.session_owner_lookup(session_id)
            except Exception:
                # Fail closed: an update that cannot be attributed is dropped, not sent.
                logger.warning(
                    "could not read the owner of session %s; its update is withheld", session_id, exc_info=True
                )
                return False
        with acting_as(operator or ANONYMOUS):
            return may_use(owner)

    @staticmethod
    def _updated_resource_uris(event: dict[str, Any], *, subscribed: list[str]) -> list[str]:
        subscribed_set = set(subscribed)
        if not subscribed_set:
            return []
        candidates = {"browser://sessions"}
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            event_type = event.get("event")
            if event_type == "observe":
                candidates.update(
                    {
                        f"browser://{session_id}/dom",
                        f"browser://{session_id}/screenshot",
                    }
                )
            elif event_type == "action":
                candidates.update(
                    {
                        f"browser://{session_id}/dom",
                        f"browser://{session_id}/network",
                        f"browser://{session_id}/screenshot",
                    }
                )
            elif event_type == "session":
                candidates.add(f"browser://{session_id}/dom")
            elif event_type == "approval":
                candidates.add(f"browser://{session_id}/console")
        return sorted(uri for uri in candidates if uri in subscribed_set)

    def _handle_initialize(self, request_id: Any, params: dict[str, Any]) -> Response:
        requested_version = params.get("protocolVersion")
        if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
            negotiated_version = requested_version
        else:
            # The spec requires a counter-offer here, not an error: "the server
            # MUST respond with another protocol version it supports". Returning
            # -32602 killed the connection outright for any client that opened
            # with a version we do not speak — including a newer client probing
            # for backwards compatibility, which is exactly the case graceful
            # downgrade exists to serve. The client compares this against what it
            # supports and disconnects itself if it cannot proceed.
            negotiated_version = CURRENT_PROTOCOL_VERSION
            # Quoted and cut: the client's value, as long as the body.
            logger.info(
                "MCP client requested unsupported protocol version %s; offering %s",
                quoted_for_log(requested_version),
                negotiated_version,
            )

        # The client's own, kept in the session table, which is written as UTF-8: a lone surrogate in either (JSON's
        # "\ud800" reads as one) could not be written, so it is kept as U+FFFD, as the stores do. Each is measured as
        # sent, before it is cleaned, and then, if it held one, as it is kept, which is what the next start measures:
        # keys that meet once their lone surrogates are replaced are numbered, so what is kept can be longer than what
        # was sent. The walk on what was sent stops at the first thing past a bound, so a million keys are not cleaned
        # to be refused. The answer names the field and the bound, and nothing of the value. The text each is measured
        # by is what the table writes of it (McpSession.client_json).
        kept: dict[str, dict[str, Any]] = {}
        measured: dict[str, str | None] = {}
        for name, value in (("clientInfo", params.get("clientInfo")), ("capabilities", params.get("capabilities"))):
            kept[name] = self._coerce_dict(value)
            past_a_bound, measured[name] = _past_a_client_object_bound(kept[name])
            # Its text holds a lone surrogate where the value does, keys included, and nothing else is cleaned.
            if past_a_bound is None and holds_lone_surrogate(measured[name]):
                kept[name] = without_lone_surrogates(kept[name])
                past_a_bound, measured[name] = _past_a_client_object_bound(kept[name])
            if past_a_bound is not None:
                return self._json_error_response(request_id, -32602, f"initialize: {name} {past_a_bound}")

        session = McpSession(
            id=uuid4().hex,
            protocol_version=negotiated_version,
            client_info=kept["clientInfo"],
            client_capabilities=kept["capabilities"],
            owner=verified_operator(),
            measured_json=(measured["clientInfo"], measured["capabilities"]),
        )
        self._sessions[session.id] = session
        try:
            # The sessions this one puts out of the table go once it is written, and not before.
            self._persist_sessions(keep=session.id)
        except Exception:
            # The table is written whole, so a session that cannot be written would fail every later write too:
            # each initialize by anybody, until a restart. Its id was never answered, so nobody holds it; it is
            # taken back out before the error goes on.
            self._forget_session(session.id)
            raise

        result = {
            "protocolVersion": session.protocol_version,
            "capabilities": {
                "tools": {},
                "resources": {"subscribe": True},
                "experimental": {
                    "autoBrowser": {
                        "workflowProfiles": ["fast", "governed"],
                        "resumableAgentJobs": True,
                        "discardableAgentJobs": True,
                        "cancellableAgentJobs": True,
                    }
                },
            },
            "serverInfo": {
                "name": self.server_name,
                "title": self.server_title,
                "version": self.server_version,
            },
            "instructions": server_instructions({tool["name"] for tool in self.tool_gateway.list_tools()}),
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
        if session is None or not may_use(session.owner):
            return self._json_error_response(
                request_id,
                -32001,
                f"Unknown MCP session: {capped_for_log(session_id)}",
                status_code=404,
            )
        session.last_used_at = utc_now()
        return session

    def _validate_protocol_header(
        self, request: Request, session: McpSession, *, request_id: Any = None
    ) -> Response | None:
        protocol_version = request.headers.get(MCP_PROTOCOL_HEADER)
        # A session goes on at the version it negotiated, whatever that is, until it initializes again. A session
        # restored at a version no longer offered needs this: a stdio bridge sends it on every POST and re-initializes
        # only on a 404, so a 400 here would never end.
        if not protocol_version or protocol_version == session.protocol_version:
            return None
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            return self._json_error_response(
                request_id,
                -32602,
                "Unsupported MCP protocol version",
                data={"requested": capped_for_log(protocol_version), "supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
                status_code=400,
                headers=self._session_headers(session),
            )
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

    def _validate_origin(self, request: Request) -> Response | None:
        origin = request.headers.get("origin")
        if not origin:
            return None

        normalized_origin = self._normalize_origin(origin)
        if normalized_origin is None:
            return self._json_error_response(None, -32000, "Malformed Origin header", status_code=400)

        allowed_origins = {
            normalized
            for normalized in (self._normalize_origin(origin_value) for origin_value in self.allowed_origins)
            if normalized
        }
        request_origin = self._normalize_origin(str(request.base_url))
        if request_origin:
            allowed_origins.add(request_origin)

        if normalized_origin not in allowed_origins:
            return self._json_error_response(
                None,
                -32000,
                f"Forbidden Origin header: {capped_for_log(origin)}",
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
        past_a_bound = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            client_info = self._coerce_dict(item.get("client_info"))
            client_capabilities = self._coerce_dict(item.get("client_capabilities"))
            # Measured before the session is made, by the text it keeps of each (McpSession.client_json).
            info_json, capabilities_json = _stored_client_json(client_info), _stored_client_json(client_capabilities)
            if info_json is None or capabilities_json is None:
                # Written by a version that kept whatever a client sent. Its client is answered 404 "Unknown MCP
                # session" and initializes again, as for a session that was evicted.
                past_a_bound += 1
                continue
            try:
                session = McpSession(
                    id=str(item["id"]),
                    protocol_version=str(item["protocol_version"]),
                    client_info=client_info,
                    client_capabilities=client_capabilities,
                    initialized=bool(item.get("initialized", False)),
                    created_at=str(item.get("created_at") or ""),
                    last_used_at=str(item.get("last_used_at") or ""),
                    owner=item["owner"] if isinstance(item.get("owner"), str) else None,
                    resource_subscriptions=[
                        str(uri) for uri in item.get("resource_subscriptions", []) if isinstance(uri, str)
                    ],
                    measured_json=(info_json, capabilities_json),
                )
            except Exception:
                continue
            restored[session.id] = session
        if past_a_bound:
            logger.warning(
                "dropped %d MCP sessions read from %s: a clientInfo or capabilities nested deeper than %d levels or "
                "larger than %d bytes as JSON is no longer kept",
                past_a_bound,
                self._session_store_path,
                MAX_CLIENT_OBJECT_DEPTH,
                MAX_CLIENT_OBJECT_JSON_BYTES,
            )
        self._sessions = restored

    def _sessions_past_the_caps(self, keep: str | None) -> list[McpSession]:
        # Sessions without a live stream go first, so a client that only listens outlasts a flood of initializations,
        # and within each group the least recently used goes first. Ordering by creation evicted a client that had been
        # connected (and busy) the longest as soon as 500 newer sessions, most of them abandoned without a DELETE,
        # piled up behind it. keep is the session an initialize has just created: see _eviction_order.
        order = self._eviction_order(keep)
        by_owner: dict[str, list[McpSession]] = {}
        for session in order:
            if session.owner is not None:
                by_owner.setdefault(session.owner, []).append(session)
        going = {
            session.id: session
            for sessions in by_owner.values()
            for session in sessions[: max(0, len(sessions) - MAX_MCP_SESSIONS_PER_OWNER)]
        }
        staying = [session for session in order if session.id not in going]
        going.update((session.id, session) for session in staying[: max(0, len(staying) - MAX_MCP_SESSIONS)])
        return list(going.values())

    def _eviction_order(self, keep: str | None) -> list[McpSession]:
        """Every session without a live stream, then those with one, each least recently used first.

        keep ranks with the live streams: the session of an initialize being answered has had no time to open one, and
        evicted here the id in its answer would already be gone. It is a session like any other at the next eviction.
        """

        def order(session: McpSession) -> tuple[bool, str, str]:
            listening = session.id in self._event_streams or session.id == keep
            return (listening, session.last_used_at or session.created_at or "", session.id)

        return sorted(self._sessions.values(), key=order)

    def _persist_sessions(self, *, keep: str | None = None) -> None:
        """Write the table without the sessions past its caps, and then drop those: the caps hold with no store too.

        In that order, so that a write that fails leaves the table what it was, in memory as in the file. Dropped
        first, a session was gone from memory and its stream was ended for an initialize that was then answered 500,
        while the file still held it. keep is the session an initialize has just created: see _eviction_order.
        """
        going = self._sessions_past_the_caps(keep)
        if self._session_store_path is not None:
            gone = {session.id for session in going}
            self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
            # Compact, as the bounds measure each field: indented, a session at the bounds took some 35 times what is
            # bounded. Joined from each session's record, it is what _compact_json of the table whole writes, so every
            # controller reads it as before.
            records = ",".join(session.table_record() for session in self._sessions.values() if session.id not in gone)
            atomic_write_text(self._session_store_path, f"[{records}]", mode=PRIVATE_FILE_MODE)
        for session in going:
            self._forget_session(session.id)

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
    ) -> SafeJSONResponse:
        return SafeJSONResponse(status_code=status_code, content=self._json_result(request_id, result), headers=headers)

    def _json_error_response(
        self,
        request_id: Any,
        code: int,
        message: str,
        *,
        data: Any = None,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> SafeJSONResponse:
        return SafeJSONResponse(
            status_code=status_code,
            content=self._json_error(request_id, code, message, data=data),
            headers=headers,
        )

    @staticmethod
    def _session_headers(session: McpSession) -> dict[str, str]:
        return {MCP_PROTOCOL_HEADER: session.protocol_version}


def _is_a_json_rpc_id(value: object) -> bool:
    """Whether ``value`` is an id JSON-RPC 2.0 allows: a string, a number or null (true and false are no numbers)."""
    return value is None or isinstance(value, str) or (isinstance(value, int | float) and not isinstance(value, bool))


def _compact_json(value: Any) -> str:
    """``value`` as the MCP session table is written: JSON, compactly, its text as it is (the file is UTF-8)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _past_a_client_object_bound(value: dict[str, Any]) -> tuple[str | None, str | None]:
    """Which bound an initialize's clientInfo or capabilities is past, in the words of its refusal, and None; or None
    and the value as _compact_json writes it, which is what it was measured by.

    The walk is a loop over one iterator a level, where a function that calls itself would run out of frames on the
    depth it is to refuse. It ends at the first thing past a bound, so a value of a million keys is not gone through
    to be refused: it counts a byte for each key and each value, and one for each character of text, each digit of
    an integer it can count at no cost and each character of a float (_json_length_at_least), which is no more than
    the JSON takes. Only a value that is under both by that count is written out to be measured.
    """
    too_large = f"is larger than {MAX_CLIENT_OBJECT_JSON_BYTES} bytes as JSON"
    at_least = 0
    levels = [iter((value,))]
    while levels:
        for part in levels[-1]:
            at_least += 1 + _json_length_at_least(part)
            if at_least > MAX_CLIENT_OBJECT_JSON_BYTES:
                return too_large, None
            if isinstance(part, dict | list):
                if len(levels) > MAX_CLIENT_OBJECT_DEPTH:
                    return f"is nested deeper than {MAX_CLIENT_OBJECT_DEPTH} levels", None
                levels.append(chain.from_iterable(part.items()) if isinstance(part, dict) else iter(part))
                break
        else:
            levels.pop()
    written = _compact_json(value)
    # A lone surrogate, which UTF-8 does not carry, takes the three bytes that U+FFFD takes in its place.
    if len(written.encode("utf-8", "surrogatepass")) > MAX_CLIENT_OBJECT_JSON_BYTES:
        return too_large, None
    return None, written


def _json_length_at_least(part: object) -> int:
    """At most the bytes ``part`` takes as JSON, as far as the walk can count them at no cost: a text's length, an
    integer's digits counted from its bits, and a float's length as the writer writes it.

    An integer of n bits is at least 2 ** (n - 1), so it has at least 1 + (n - 1) * log10(2) digits, the fraction
    dropped, and _DIGITS_PER_BIT is just under log10(2): no more are counted than it has. str() of it would cost
    what writing it out does, and counted as one byte, 2,000 integers of 4,000 digits passed the walk and were
    written out whole to be refused. A float is written as its repr, or as NaN, Infinity or -Infinity, which the
    JSON reader takes from a body: counted as one byte, 65,000 of them were written out (1,625,007 bytes) to be
    refused. A bool, which is an int, counts nothing here, as anything else does.
    """
    if isinstance(part, str):
        return len(part)
    if isinstance(part, float):
        written = float.__repr__(part)
        return len(_NON_FINITE_AS_JSON.get(written, written))
    if isinstance(part, int) and not isinstance(part, bool):
        return (max(part.bit_length(), 1) - 1) * _DIGITS_PER_BIT // _DIGITS_PER_BIT_SCALE + 1
    return 0


def _stored_client_json(value: dict[str, Any]) -> str | None:
    """A clientInfo or capabilities a start read from the table, as _compact_json writes it; or None where it is past
    a bound. Of any value, what _past_a_client_object_bound decides.

    A start reads values its file bounds, so it needs no early stop. The initialize's walk counts each element in
    Python, to stop early in a body of any size: run at a start, it held a table at its cap of 500 sessions, both
    fields of each small numbers at the bound, for some 40 s. Here each is written out once, by the C encoder, as
    each write of the table needs it, and that text is measured: its UTF-8 for the size, its brackets for the depth.
    """
    try:
        written = _compact_json(value)
    except RecursionError:
        # Nested deeper than the writer goes from here, which is far deeper than the bound.
        return None
    # A lone surrogate, which UTF-8 does not carry, takes the three bytes that U+FFFD takes in its place.
    if len(written.encode("utf-8", "surrogatepass")) > MAX_CLIENT_OBJECT_JSON_BYTES:
        return None
    return None if _nested_deeper_than(written, MAX_CLIENT_OBJECT_DEPTH) else written


def _nested_deeper_than(written: str, levels: int) -> bool:
    """Whether ``written``, a JSON text as _compact_json writes it, nests lists and objects more than ``levels``
    deep, the outermost counted.

    By str's own methods, each a pass in C over the text: a walk in Python over the value's lists and objects,
    looking at each element only to find them, took twice as long as this on the shape slowest for both (lists
    nested 30 deep, side by side) and six times as long on small numbers.
    """
    # Its escaped backslashes taken out and then its escaped quotes, each quote left opens or closes a string, so the
    # pieces between quotes are in turn outside a string and in one.
    unescaped = written.replace("\\\\", "").replace('\\"', "")
    outside = "".join(unescaped.split('"')[::2])
    # Its brackets alone, of one kind, which is all the depth goes by. Each pass takes out every pair with nothing
    # between them, the lists and objects that hold none, so a bracket is left after ``levels`` passes only where
    # they nest deeper.
    brackets = outside.translate(str.maketrans("{}", "[]", "".join(set(outside) - set("[]{}"))))
    for _ in range(levels):
        brackets = brackets.replace("[]", "")
    return bool(brackets)


async def _next_stream_event(queue: asyncio.Queue, stop: asyncio.Event) -> str | None:
    """The next event off the queue, or None at a keepalive's wake or once stop is set.

    Waits on the queue and on stop in their own tasks, cancels whichever is left, and waits for both, on every way
    out (the generator cancelled here included): a stopped stream wakes at once and leaves no task behind. A cancel
    that lands as an event arrives leaves the event on the queue, and one landing with the stop is dropped with it.
    """
    getter = asyncio.ensure_future(queue.get())
    stopper = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({getter, stopper}, timeout=_STREAM_KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        return getter.result() if getter.done() else None
    finally:
        getter.cancel()
        stopper.cancel()
        await asyncio.gather(getter, stopper, return_exceptions=True)


def _json_resource(uri: str, payload: Any) -> dict[str, Any]:
    """A JSON resource, encoded as strictly as a tool result: a NaN or an infinity (a console or network entry can
    carry one) is null, not the bare NaN strict parsers reject, and an object is refused rather than sent as its
    repr. A payload that cannot be encoded raises, and is answered as a failed read (-32603)."""
    text = json.dumps(json_safe(payload), ensure_ascii=False, allow_nan=False)
    return {"uri": uri, "mimeType": "application/json", "text": text}


def _capped_dom(html: str) -> str:
    """A page's HTML, at most MAX_DOM_RESOURCE_CHARS in all: a longer page is cut and ends in a comment saying so."""
    if len(html) <= MAX_DOM_RESOURCE_CHARS:
        return html
    marker = (
        f"<!-- auto-browser: cut to {MAX_DOM_RESOURCE_CHARS:,} of {len(html):,} "
        "characters; browser.get_html pages through the whole text -->"
    )
    return html[: MAX_DOM_RESOURCE_CHARS - len(marker)] + marker
