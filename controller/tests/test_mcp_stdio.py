from __future__ import annotations

import io
import json
import os
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import app.mcp_stdio as mcp_stdio
import app.mcp_transport as mcp_transport
from app.mcp_stdio import HttpMcpClient, HttpMcpResponse, StdioMcpBridge
from app.mcp_transport import MCP_PROTOCOL_HEADER, MCP_SESSION_HEADER

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLIENT_BRIDGE = _REPO_ROOT / "client" / "auto_browser_client" / "mcp_bridge.py"


class FakeHttpMcpClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []
        self.deleted_session_ids: list[str] = []

    def post_json(self, payload, *, session_id=None, protocol_version=None):
        self.posts.append(
            {
                "payload": payload,
                "session_id": session_id,
                "protocol_version": protocol_version,
            }
        )
        method = payload.get("method")
        if method == "initialize":
            return HttpMcpResponse(
                status_code=200,
                headers={
                    MCP_SESSION_HEADER.lower(): "mcp-session-1",
                    MCP_PROTOCOL_HEADER.lower(): "2025-11-25",
                },
                body={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "auto-browser", "version": "0.2.0"},
                    },
                },
            )
        if method == "notifications/initialized":
            return HttpMcpResponse(status_code=202, headers={}, body=None)
        return HttpMcpResponse(
            status_code=200,
            headers={},
            body={
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"ok": True},
            },
        )

    def delete_session(self, *, session_id=None):
        if session_id:
            self.deleted_session_ids.append(session_id)


class McpStdioBridgeTests(unittest.TestCase):
    def test_bridge_tracks_session_and_protocol_headers(self) -> None:
        client = FakeHttpMcpClient()
        bridge = StdioMcpBridge(client=client)

        stdin = io.StringIO(
            "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-11-25",
                                "clientInfo": {"name": "pytest", "version": "1.0.0"},
                                "capabilities": {},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {},
                        }
                    ),
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()

        exit_code = bridge.run(stdin=stdin, stdout=stdout)

        self.assertEqual(exit_code, 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(lines[1]["result"]["ok"], True)
        self.assertEqual(client.posts[0]["session_id"], None)
        self.assertEqual(client.posts[1]["session_id"], "mcp-session-1")
        self.assertEqual(client.posts[2]["protocol_version"], "2025-11-25")
        self.assertEqual(client.deleted_session_ids, ["mcp-session-1"])

    def test_invalid_json_returns_parse_error(self) -> None:
        bridge = StdioMcpBridge(client=FakeHttpMcpClient())
        stdout = io.StringIO()

        bridge.run(stdin=io.StringIO("{not-json}\n"), stdout=stdout)

        payload = json.loads(stdout.getvalue().strip())
        self.assertEqual(payload["error"]["code"], -32700)


def _headers(pairs: dict[str, str]) -> Message:
    """urllib exposes response headers as an email.Message; mirror that shape."""
    message = Message()
    for key, value in pairs.items():
        message[key] = value
    return message


class _FakeResponse:
    def __init__(self, *, status: int, headers: dict[str, str], raw: bytes) -> None:
        self.status = status
        self.headers = _headers(headers)
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class HttpMcpClientTests(unittest.TestCase):
    """The HTTP half of the bridge — header assembly and error translation."""

    def _capture(self, response: object):
        """Patch urlopen, returning the list that collects outgoing Requests."""
        captured: list[object] = []

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            captured.append(request)
            if isinstance(response, Exception):
                raise response
            return response

        return captured, fake_urlopen

    def test_post_json_sends_session_protocol_and_bearer_headers(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:8000/mcp", bearer_token="s3cret")
        captured, fake = self._capture(_FakeResponse(status=200, headers={}, raw=b'{"ok": true}'))

        with patch.object(mcp_stdio, "urlopen", fake):
            result = client.post_json({"method": "tools/list"}, session_id="sess-1", protocol_version="2025-11-25")

        request = captured[0]
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header(MCP_SESSION_HEADER.capitalize()), "sess-1")
        self.assertEqual(request.get_header(MCP_PROTOCOL_HEADER.capitalize()), "2025-11-25")
        self.assertEqual(request.get_header("Authorization"), "Bearer s3cret")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(result.body, {"ok": True})

    def test_post_json_omits_optional_headers_when_unset(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:8000/mcp")
        captured, fake = self._capture(_FakeResponse(status=200, headers={}, raw=b"{}"))

        with patch.object(mcp_stdio, "urlopen", fake):
            client.post_json({"method": "tools/list"})

        request = captured[0]
        self.assertIsNone(request.get_header(MCP_SESSION_HEADER.capitalize()))
        self.assertIsNone(request.get_header(MCP_PROTOCOL_HEADER.capitalize()))
        self.assertIsNone(request.get_header("Authorization"))

    def test_http_error_is_returned_not_raised(self) -> None:
        # A 4xx/5xx carries a JSON-RPC error body the client must see; raising
        # here would surface a transport crash instead of the server's message.
        client = HttpMcpClient(base_url="http://localhost:8000/mcp")
        error = HTTPError(
            "http://localhost:8000/mcp",
            401,
            "Unauthorized",
            _headers({"content-type": "application/json"}),
            io.BytesIO(b'{"error": {"code": -32001, "message": "unauthorized"}}'),
        )
        _captured, fake = self._capture(error)

        with patch.object(mcp_stdio, "urlopen", fake):
            result = client.post_json({"method": "tools/list"})

        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.body["error"]["message"], "unauthorized")
        self.assertEqual(result.headers["content-type"], "application/json")

    def test_empty_body_decodes_to_none(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:8000/mcp")
        _captured, fake = self._capture(_FakeResponse(status=202, headers={}, raw=b""))

        with patch.object(mcp_stdio, "urlopen", fake):
            self.assertIsNone(client.post_json({"method": "notifications/initialized"}).body)

    def test_delete_session_is_a_noop_without_a_session(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:8000/mcp")
        captured, fake = self._capture(_FakeResponse(status=200, headers={}, raw=b""))

        with patch.object(mcp_stdio, "urlopen", fake):
            client.delete_session(session_id=None)

        self.assertEqual(captured, [])

    def test_delete_session_sends_session_and_bearer(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:8000/mcp", bearer_token="s3cret")
        captured, fake = self._capture(_FakeResponse(status=200, headers={}, raw=b""))

        with patch.object(mcp_stdio, "urlopen", fake):
            client.delete_session(session_id="sess-1")

        request = captured[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.get_header(MCP_SESSION_HEADER.capitalize()), "sess-1")
        self.assertEqual(request.get_header("Authorization"), "Bearer s3cret")

    def test_delete_session_swallows_transport_errors(self) -> None:
        # Teardown must never mask the real exit path; a dead server on shutdown
        # is not worth propagating.
        client = HttpMcpClient(base_url="http://localhost:8000/mcp")
        _captured, fake = self._capture(URLError("connection refused"))

        with patch.object(mcp_stdio, "urlopen", fake):
            client.delete_session(session_id="sess-1")  # must not raise


class BridgeProtocolTests(unittest.TestCase):
    """JSON-RPC framing rules the bridge enforces before touching HTTP."""

    def _run(self, client: object, stdin_text: str) -> list[dict[str, object]]:
        stdout = io.StringIO()
        StdioMcpBridge(client=client).run(stdin=io.StringIO(stdin_text), stdout=stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def test_batches_are_rejected(self) -> None:
        lines = self._run(FakeHttpMcpClient(), json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "x"}]) + "\n")
        self.assertEqual(lines[0]["error"]["code"], -32600)
        self.assertIn("batches", lines[0]["error"]["message"])

    def test_non_object_bodies_are_rejected(self) -> None:
        for raw in ('"a string"', "5", "true"):
            with self.subTest(raw=raw):
                lines = self._run(FakeHttpMcpClient(), raw + "\n")
                self.assertEqual(lines[0]["error"]["code"], -32600)

    def test_blank_lines_are_skipped(self) -> None:
        client = FakeHttpMcpClient()
        lines = self._run(client, "\n   \n\n")
        self.assertEqual(lines, [])
        self.assertEqual(client.posts, [])

    def test_notifications_produce_no_response(self) -> None:
        client = FakeHttpMcpClient()
        lines = self._run(client, json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.assertEqual(lines, [])
        self.assertEqual(len(client.posts), 1)

    def test_unreachable_endpoint_becomes_a_jsonrpc_error(self) -> None:
        class DeadClient:
            base_url = "http://ctrl.test/mcp"

            def post_json(self, *_args: object, **_kwargs: object):
                raise URLError("connection refused")

            def delete_session(self, *, session_id=None) -> None:
                return None

        lines = self._run(DeadClient(), json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n")
        self.assertEqual(lines[0]["id"], 7)
        self.assertEqual(lines[0]["error"]["code"], -32000)
        message = lines[0]["error"]["message"]
        self.assertIn("http://ctrl.test/mcp", message)
        self.assertIn("docker compose up -d", message)
        self.assertIn("--base-url/AUTO_BROWSER_BASE_URL", message)

    def test_empty_body_for_a_request_becomes_an_error(self) -> None:
        class EmptyBodyClient:
            def post_json(self, *_args: object, **_kwargs: object):
                return HttpMcpResponse(status_code=204, headers={}, body=None)

            def delete_session(self, *, session_id=None) -> None:
                return None

        lines = self._run(EmptyBodyClient(), json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n")
        self.assertEqual(lines[0]["error"]["code"], -32000)
        self.assertIn("204", lines[0]["error"]["message"])

    def test_protocol_version_falls_back_to_initialize_result_body(self) -> None:
        # Some deployments strip the MCP-Protocol-Version response header; the
        # bridge must still learn the version from the initialize result.
        class HeaderlessClient:
            def __init__(self) -> None:
                self.posts: list[dict[str, object]] = []

            def post_json(self, payload, *, session_id=None, protocol_version=None):  # noqa: ANN001
                self.posts.append({"payload": payload, "protocol_version": protocol_version})
                if payload.get("method") == "initialize":
                    return HttpMcpResponse(
                        status_code=200,
                        headers={},
                        body={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": " 2025-11-25 "}},
                    )
                return HttpMcpResponse(status_code=200, headers={}, body={"jsonrpc": "2.0", "id": 2, "result": {}})

            def delete_session(self, *, session_id=None) -> None:
                return None

        client = HeaderlessClient()
        self._run(
            client,
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n",
        )
        self.assertEqual(client.posts[1]["protocol_version"], "2025-11-25")


class ArgParserTests(unittest.TestCase):
    def test_defaults_come_from_environment(self) -> None:
        env = {
            "AUTO_BROWSER_BASE_URL": "http://example.test/mcp",
            "AUTO_BROWSER_BEARER_TOKEN": "env-token",
            "AUTO_BROWSER_HTTP_TIMEOUT_SECONDS": "12.5",
        }
        with patch.dict(os.environ, env, clear=False):
            args = mcp_stdio.build_arg_parser().parse_args([])

        self.assertEqual(args.base_url, "http://example.test/mcp")
        self.assertEqual(args.bearer_token, "env-token")
        self.assertAlmostEqual(args.timeout_seconds, 12.5)

    def test_builtin_default_base_url_when_env_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            args = mcp_stdio.build_arg_parser().parse_args([])

        self.assertEqual(args.base_url, "http://127.0.0.1:8000/mcp")
        self.assertIsNone(args.bearer_token)
        self.assertAlmostEqual(args.timeout_seconds, 60.0)

    def test_explicit_flags_override_environment(self) -> None:
        with patch.dict(os.environ, {"AUTO_BROWSER_BASE_URL": "http://ignored.test/mcp"}, clear=False):
            args = mcp_stdio.build_arg_parser().parse_args(
                ["--base-url", "http://flag.test/mcp", "--bearer-token", "flag-token", "--timeout-seconds", "3"]
            )

        self.assertEqual(args.base_url, "http://flag.test/mcp")
        self.assertEqual(args.bearer_token, "flag-token")
        self.assertAlmostEqual(args.timeout_seconds, 3.0)

    def test_main_wires_parsed_args_into_the_client(self) -> None:
        seen: dict[str, object] = {}

        def fake_run(self, **_kwargs: object) -> int:  # noqa: ANN001
            seen["base_url"] = self.client.base_url
            seen["bearer_token"] = self.client.bearer_token
            seen["timeout_seconds"] = self.client.timeout_seconds
            return 0

        argv = ["prog", "--base-url", "http://main.test/mcp", "--bearer-token", "tok", "--timeout-seconds", "7"]
        with patch.object(mcp_stdio.StdioMcpBridge, "run", fake_run), patch.object(mcp_stdio.sys, "argv", argv):
            self.assertEqual(mcp_stdio.main(), 0)

        self.assertEqual(seen["base_url"], "http://main.test/mcp")
        self.assertEqual(seen["bearer_token"], "tok")
        self.assertAlmostEqual(seen["timeout_seconds"], 7.0)


class BridgeCopySyncTests(unittest.TestCase):
    """The bridge ships twice: app.mcp_stdio (controller image) and
    auto_browser_client.mcp_bridge (PyPI console script). These guards keep
    the copies from drifting."""

    def test_header_constants_match_mcp_transport(self) -> None:
        self.assertEqual(mcp_stdio.MCP_SESSION_HEADER, mcp_transport.MCP_SESSION_HEADER)
        self.assertEqual(mcp_stdio.MCP_PROTOCOL_HEADER, mcp_transport.MCP_PROTOCOL_HEADER)

    @unittest.skipUnless(_CLIENT_BRIDGE.exists(), "client package not present (packaged/Docker run)")
    def test_client_bridge_copy_is_identical(self) -> None:
        controller_copy = Path(mcp_stdio.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
        client_copy = _CLIENT_BRIDGE.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(
            controller_copy,
            client_copy,
            "controller/app/mcp_stdio.py and client/auto_browser_client/mcp_bridge.py "
            "must stay byte-identical — mirror your edit to the other copy.",
        )


if __name__ == "__main__":
    unittest.main()
