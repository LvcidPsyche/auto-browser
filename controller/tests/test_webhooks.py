"""Approval webhook targets are checked after DNS, and the request goes to the address that was checked (STORE-20).

The old check read an IP literal only: 0x7f.1 and 2130706433 passed it, and a name that resolved to 127.0.0.1 or
169.254.169.254 was posted to. The full URL (path and query often carry a token) was logged on an HTTP error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import unittest
import urllib.request
from unittest.mock import AsyncMock, Mock, patch

import httpcore
from httpx import _utils as httpx_utils

from app import webhooks
from app.models import ApprovalRecord, BrowserActionDecision

_PUBLIC = "93.184.216.34"
_SECOND = "93.184.216.35"
_PROXY = "http://proxy.example:3128"
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
_SERVER_ERROR = b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"
_TUNNEL_OPEN = b"HTTP/1.1 200 Connection established\r\n\r\n"
_CLIENTS = ("_client", "_direct_client")
# A reply that connects for real, to a listener of the test's own (one that fails the TLS handshake).
_REAL = object()
_REAL_CONNECT_TCP = httpcore.AnyIOBackend.connect_tcp


def _approval() -> ApprovalRecord:
    return ApprovalRecord(
        id="approval-w",
        session_id="session-1",
        kind="write",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        reason="needs review",
        action=BrowserActionDecision(action="click", reason="click", selector="button", risk_category="write"),
    )


class WebhookTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.post = AsyncMock(return_value=Mock(status_code=200))
        # No proxy here (WebhookWireTests covers one): both clients are this mock.
        for name, value in (("get_client", self.client), ("_env_proxy_applies", False)):
            patcher = patch.object(webhooks, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _resolver(self, *addresses: str) -> AsyncMock:
        mock = AsyncMock(return_value=list(addresses))
        patcher = patch.object(webhooks, "_resolve_host", mock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    async def _send(self, url: str, **kwargs) -> None:
        with patch.object(webhooks.logger, "warning"):
            await webhooks.dispatch_approval_event(_approval(), webhook_url=url, **kwargs)

    async def test_a_name_that_resolves_to_a_private_address_is_not_posted_to(self) -> None:
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "192.168.1.1"):
            with self.subTest(address=address):
                self._resolver(address)
                await self._send("https://rebind.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_one_private_answer_among_public_ones_refuses_the_target(self) -> None:
        self._resolver(_PUBLIC, "127.0.0.1")
        await self._send("https://mixed.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_numeric_spellings_of_loopback_are_refused_without_a_lookup(self) -> None:
        resolver = self._resolver(_PUBLIC)
        for url in (
            "http://2130706433/hook",
            "http://0x7f.1/hook",
            "http://127.1/hook",
            "http://0177.0.0.1/hook",
            "http://localhost./hook",
            "http://%31%32%37.0.0.1/hook",
        ):
            with self.subTest(url=url):
                await self._send(url)
        self.assertEqual(self.client.post.await_count, 0)
        resolver.assert_not_awaited()

    async def test_an_unresolvable_name_is_not_posted_to(self) -> None:
        patcher = patch.object(webhooks, "_resolve_host", AsyncMock(side_effect=socket.gaierror("no such host")))
        patcher.start()
        self.addCleanup(patcher.stop)
        await self._send("https://nowhere.example.com/hook")
        self.assertEqual(self.client.post.await_count, 0)

    async def test_the_request_goes_to_the_address_that_was_checked(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("https://hooks.example.com:8443/approval?x=1", webhook_secret="s")
        call = self.client.post.await_args
        self.assertEqual(call.args[0], f"https://{_PUBLIC}:8443/approval?x=1")
        self.assertEqual(call.kwargs["headers"]["Host"], "hooks.example.com:8443")
        self.assertEqual(call.kwargs["extensions"], {"sni_hostname": "hooks.example.com"})
        self.assertIn("X-Webhook-Signature", call.kwargs["headers"])

    async def test_an_http_target_is_pinned_without_sni_and_keeps_userinfo(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("http://u:p@hooks.example.com/approval")
        call = self.client.post.await_args
        self.assertEqual(call.args[0], f"http://u:p@{_PUBLIC}/approval")
        self.assertEqual(call.kwargs["headers"]["Host"], "hooks.example.com")
        self.assertEqual(call.kwargs.get("extensions", {}), {})

    async def test_an_ipv6_address_is_bracketed_in_the_pinned_url(self) -> None:
        self._resolver("2606:2800:220:1:248:1893:25c8:1946")
        await self._send("https://hooks.example.com/approval")
        self.assertEqual(self.client.post.await_args.args[0], "https://[2606:2800:220:1:248:1893:25c8:1946]/approval")

    async def test_a_public_ip_literal_is_posted_to_as_written(self) -> None:
        resolver = self._resolver()
        await self._send(f"https://{_PUBLIC}/approval")
        self.assertEqual(self.client.post.await_args.args[0], f"https://{_PUBLIC}/approval")
        resolver.assert_not_awaited()

    async def test_the_operator_can_allow_private_targets(self) -> None:
        self._resolver("10.0.0.5")
        await self._send("http://n8n.internal:5678/hook", allow_private_targets=True)
        self.assertEqual(self.client.post.await_args.args[0], "http://10.0.0.5:5678/hook")

    async def test_with_the_opt_in_loopback_private_and_lan_answers_are_posted_to(self) -> None:
        for address in ("10.0.0.5", "172.16.0.9", "192.168.1.20", "127.0.0.1", "::1", "fd12:3456::1"):
            with self.subTest(address=address):
                self.client.post.reset_mock()
                self._resolver(address)
                await self._send("http://n8n.internal:5678/hook", allow_private_targets=True)
                self.client.post.assert_awaited_once()
        self.client.post.reset_mock()
        self._resolver("127.0.0.1")
        await self._send("http://localhost:5678/hook", allow_private_targets=True)
        self.client.post.assert_awaited_once()

    async def test_a_cloud_metadata_answer_is_refused_even_with_the_opt_in(self) -> None:
        # The opt-in turned off every check (STORE-20); it opens loopback and private ranges, never cloud metadata.
        for address in (
            "169.254.169.254",
            "169.254.0.1",
            "fe80::1",
            "fe80::1%eth0",
            "fd00:ec2::254",
            "::ffff:169.254.169.254",
        ):
            with self.subTest(address=address):
                self.client.post.reset_mock()
                self._resolver(address)
                await self._send("http://hooks.internal/hook", allow_private_targets=True)
                self.client.post.assert_not_awaited()

    async def test_cloud_metadata_spelled_inside_an_ipv6_address_is_refused_too(self) -> None:
        # Only the mapped form (::ffff:a.b.c.d) was unwrapped: with the opt-in on, the translated, compatible, NAT64
        # and 6to4 spellings of 169.254.169.254 passed the always-refused check.
        for address in (
            "::ffff:0:169.254.169.254",
            "::169.254.169.254",
            "64:ff9b::169.254.169.254",
            "2002:a9fe:a9fe::1",
        ):
            for allow_private_targets in (True, False):
                with self.subTest(address=address, allow_private_targets=allow_private_targets):
                    self.client.post.reset_mock()
                    self._resolver(address)
                    await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                    self.client.post.assert_not_awaited()

    async def test_a_add_7_cloud_metadata_in_local_use_nat64_and_teredo_is_refused_with_the_opt_in_on(self) -> None:
        # RFC 8215 local-use NAT64 (64:ff9b:1::/48, the last 32 bits) and Teredo (the client address, stored
        # inverted) were not unwrapped, so the opt-in let them through.
        for address in ("64:ff9b:1::a9fe:a9fe", "64:ff9b:1:2::169.254.169.254", "2001:0:4136:e378:8000:63bf:5601:5601"):
            for allow_private_targets in (True, False):
                with self.subTest(address=address, allow_private_targets=allow_private_targets):
                    self.client.post.reset_mock()
                    self._resolver(address)
                    await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                    self.client.post.assert_not_awaited()

    async def test_w2_32_every_rfc_6052_layout_in_local_use_nat64_and_isatap_is_unwrapped(self) -> None:
        # Only the /96 layout inside 64:ff9b:1::/48 was unwrapped, so the /48, /56 and /64 spellings of
        # 169.254.169.254 passed with the opt-in on; an ISATAP interface id (0:5efe or 200:5efe, then the IPv4
        # address) behind a global prefix passed in both modes.
        for address in (
            "64:ff9b:1:a9fe:a9:fe00::",  # /48
            "64:ff9b:1:aba9:fe:a9fe::",  # /56
            "64:ff9b:1:abcd:a9:fea9:fe00:0",  # /64
            "2600::5efe:a9fe:a9fe",  # ISATAP, u/l bit clear
            "2600::200:5efe:a9fe:a9fe",  # ISATAP, u/l bit set
        ):
            for allow_private_targets in (True, False):
                with self.subTest(address=address, allow_private_targets=allow_private_targets):
                    self.client.post.reset_mock()
                    self._resolver(address)
                    await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                    self.client.post.assert_not_awaited()

    async def test_w2_33_gcp_and_oci_ipv6_metadata_addresses_are_refused_with_the_opt_in_on_and_off(self) -> None:
        # GCP's IPv6-only instances query fd20:ce::254; cloud-init's Oracle datasource uses fd00:c1::a9fe:a9fe. Both
        # are private (fc00::/7), so they passed with the opt-in on.
        for address in ("fd20:ce::254", "fd00:c1::a9fe:a9fe"):
            for allow_private_targets in (True, False):
                with self.subTest(address=address, allow_private_targets=allow_private_targets):
                    self.client.post.reset_mock()
                    self._resolver(address)
                    await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                    self.client.post.assert_not_awaited()
        # Single addresses: a neighbour in the same /64 is an ordinary private address, allowed with the opt-in.
        self.client.post.reset_mock()
        self._resolver("fd20:ce::253")
        await self._send("http://hooks.internal/hook", allow_private_targets=True)
        self.client.post.assert_awaited_once()

    async def test_w2_32_an_isatap_private_address_is_refused_without_the_opt_in_and_allowed_with_it(self) -> None:
        for allow_private_targets, posted in ((False, False), (True, True)):
            with self.subTest(allow_private_targets=allow_private_targets):
                self.client.post.reset_mock()
                self._resolver("2600::5efe:a00:1")  # a global prefix, then ISATAP's 10.0.0.1
                await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                self.assertEqual(self.client.post.await_count, int(posted))

    async def test_the_alibaba_cloud_metadata_address_is_refused_with_the_opt_in_on_and_off(self) -> None:
        # 100.100.100.200 sits in 100.64.0.0/10, which is neither private nor reserved: it passed both checks.
        for allow_private_targets in (False, True):
            with self.subTest(answer="100.100.100.200", allow_private_targets=allow_private_targets):
                self.client.post.reset_mock()
                self._resolver("100.100.100.200")
                await self._send("http://hooks.internal/hook", allow_private_targets=allow_private_targets)
                self.client.post.assert_not_awaited()
            with self.subTest(
                url="http://100.100.100.200/latest/meta-data/", allow_private_targets=allow_private_targets
            ):
                self.client.post.reset_mock()
                resolver = self._resolver(_PUBLIC)
                await self._send(
                    "http://100.100.100.200/latest/meta-data/", allow_private_targets=allow_private_targets
                )
                self.client.post.assert_not_awaited()
                resolver.assert_not_awaited()

    async def test_without_the_opt_in_an_address_that_is_not_global_is_refused(self) -> None:
        # Shared address space (carrier-grade NAT, Tailscale) is not global, though Python calls it neither private
        # nor reserved.
        for address in ("100.64.0.1", "100.101.102.103", "100.127.255.254"):
            with self.subTest(address=address):
                self.client.post.reset_mock()
                self._resolver(address)
                await self._send("https://tailnet.example.com/hook")
                self.client.post.assert_not_awaited()

    async def test_with_the_opt_in_a_shared_address_space_answer_is_posted_to(self) -> None:
        self._resolver("100.101.102.103")
        await self._send("http://n8n.tailnet.internal:5678/hook", allow_private_targets=True)
        self.assertEqual(self.client.post.await_args.args[0], "http://100.101.102.103:5678/hook")

    async def test_with_the_opt_in_a_private_address_spelled_inside_ipv6_is_still_allowed(self) -> None:
        self._resolver("64:ff9b::10.0.0.5")
        await self._send("http://n8n.internal:5678/hook", allow_private_targets=True)
        self.client.post.assert_awaited_once()

    async def test_cloud_metadata_names_and_literals_are_refused_without_a_lookup(self) -> None:
        resolver = self._resolver(_PUBLIC)
        for url in (
            "http://metadata/computeMetadata/v1/",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://METADATA.Google.Internal./x",
            "http://169.254.169.254/latest/meta-data/",
            "http://[fd00:ec2::254]/latest/meta-data/",
        ):
            for allow_private_targets in (False, True):
                with self.subTest(url=url, allow_private_targets=allow_private_targets):
                    self.client.post.reset_mock()
                    await self._send(url, allow_private_targets=allow_private_targets)
                    self.client.post.assert_not_awaited()
        resolver.assert_not_awaited()

    async def test_the_logs_carry_the_scheme_and_host_only(self) -> None:
        self._resolver(_PUBLIC)
        self.client.post = AsyncMock(return_value=Mock(status_code=500))
        url = "https://user:topsecret@hooks.example.com/services/T0KEN/path?token=abc123"
        with self.assertLogs("app.webhooks", level="WARNING") as captured:
            await webhooks.dispatch_approval_event(_approval(), webhook_url=url)
        text = "\n".join(captured.output)
        self.assertIn("https://hooks.example.com", text)
        for leaked in ("topsecret", "T0KEN", "abc123", "/services", "user:"):
            self.assertNotIn(leaked, text)

    async def test_a_blocked_target_logs_the_host_only(self) -> None:
        with self.assertLogs("app.webhooks", level="WARNING") as captured:
            await webhooks.dispatch_approval_event(_approval(), webhook_url="http://127.0.0.1:8080/secret?token=abc")
        text = "\n".join(captured.output)
        self.assertNotIn("token=abc", text)
        self.assertNotIn("/secret", text)

    async def test_the_body_is_unchanged(self) -> None:
        self._resolver(_PUBLIC)
        await self._send("https://hooks.example.com/approval")
        body = json.loads(self.client.post.await_args.kwargs["content"])
        self.assertEqual(body["approval_id"], "approval-w")


class _Wire:
    """What reaches the network: httpx and httpcore run for real over a fake socket layer (httpcore's mock stream)."""

    def __init__(self, replies: dict[str, list[bytes] | Exception]) -> None:
        self.connects: list[tuple[str, int]] = []
        self.tls_names: list[str | None] = []
        self.sent: list[bytes] = []
        wire = self

        class Stream(httpcore.AsyncMockStream):
            async def write(self, buffer: bytes, timeout: float | None = None) -> None:
                wire.sent.append(buffer)

            async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
                # A reply may hold an exception, raised where the answer is read (a read timeout).
                if self._buffer and isinstance(self._buffer[0], Exception):
                    raise self._buffer.pop(0)
                return await super().read(max_bytes, timeout)

            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                wire.tls_names.append(server_hostname)
                return self

        async def connect_tcp(_backend, host, port, timeout=None, local_address=None, socket_options=None):
            self.connects.append((host, port))
            reply = replies.get(host, httpcore.ConnectError(f"no route to {host}"))
            if reply is _REAL:
                return await _REAL_CONNECT_TCP(
                    _backend, host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            if isinstance(reply, Exception):
                raise reply
            return Stream(list(reply))

        self.connect_tcp = connect_tcp

    def request_head(self, method: bytes = b"POST") -> tuple[bytes, dict[bytes, bytes]]:
        head = next(chunk for chunk in self.sent if chunk.startswith(method + b" "))
        request_line, *lines = head.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        fields = (line.partition(b":") for line in lines)
        return request_line, {name.strip().lower(): value.strip() for name, _, value in fields}


class WebhookWireTests(unittest.IsolatedAsyncioTestCase):
    """STORE-20 behind a proxy: the asserts see the TCP connects, the CONNECT line and the TLS name."""

    async def asyncSetUp(self) -> None:
        for name in _CLIENTS:  # fresh clients, built under each test's environment
            self.addCleanup(setattr, webhooks, name, getattr(webhooks, name, None))
            setattr(webhooks, name, None)

    async def asyncTearDown(self) -> None:
        for name in _CLIENTS:
            client = getattr(webhooks, name, None)
            if client is not None:
                await client.aclose()

    async def _dispatch(
        self,
        env: dict[str, str],
        replies: dict,
        *answers: str,
        url: str = "https://hooks.example.com:8443/approval?x=1",
        resolver: AsyncMock | None = None,
        **kwargs,
    ) -> _Wire:
        wire = _Wire(replies)
        self.resolver = resolver or AsyncMock(return_value=list(answers))
        # Only the proxy variables go: OpenSSL on Windows cannot build a context in an empty environment.
        kept = {name: value for name, value in os.environ.items() if not name.lower().endswith("_proxy")}
        with (
            patch.dict(os.environ, {**kept, **env}, clear=True),
            # The environment only: on Windows and macOS getproxies() also reads the system proxy, which would
            # reroute these requests (httpx reads it through httpx._utils).
            patch.object(httpx_utils, "getproxies", urllib.request.getproxies_environment),
            patch.object(httpcore.AnyIOBackend, "connect_tcp", wire.connect_tcp),
            patch.object(webhooks, "_resolve_host", self.resolver),
            patch.object(webhooks.logger, "warning") as self.warnings,
        ):
            await webhooks.dispatch_approval_event(_approval(), webhook_url=url, **kwargs)
        return wire

    async def test_a_non_ascii_host_is_posted_to_under_its_idna_name(self) -> None:
        # R-9: the netloc went into Host and the TLS name as written, and httpx raised UnicodeEncodeError for it.
        wire = await self._dispatch({}, {_PUBLIC: [_OK]}, _PUBLIC, url="https://b\u00fccher.example/hook")

        self.assertEqual(wire.connects, [(_PUBLIC, 443)])
        self.assertEqual(wire.tls_names, ["xn--bcher-kva.example"])
        self.assertEqual(wire.request_head()[1][b"host"], b"xn--bcher-kva.example")
        self.assertEqual(self.resolver.await_args.args[0], "xn--bcher-kva.example")

    async def test_a_non_ascii_host_is_looked_up_under_its_idna_name(self) -> None:
        await self._dispatch({}, {_PUBLIC: [_OK]}, _PUBLIC, url="https://stra\u00dfe.de/hook")

        self.assertEqual(self.resolver.await_args.args[0], "xn--strae-oqa.de")

    async def test_a_lookup_that_raises_unicode_error_posts_nothing_and_does_not_raise(self) -> None:
        resolver = AsyncMock(side_effect=UnicodeError("label too long"))
        wire = await self._dispatch({}, {_PUBLIC: [_OK]}, url="https://hooks.example.com/hook", resolver=resolver)

        self.assertEqual(wire.connects, [])
        messages = [call.args[0] for call in self.warnings.call_args_list]
        self.assertIn("approval webhook host did not resolve: %s (%s)", messages)

    async def test_without_a_proxy_the_connection_goes_to_the_checked_address_with_the_name_for_tls(self) -> None:
        wire = await self._dispatch({}, {_PUBLIC: [_OK]}, _PUBLIC)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443)])
        self.assertEqual(wire.tls_names, ["hooks.example.com"])
        self.assertEqual(wire.request_head()[1][b"host"], b"hooks.example.com:8443")

    async def test_through_a_proxy_from_the_environment_the_request_goes_by_name(self) -> None:
        wire = await self._dispatch({"HTTPS_PROXY": _PROXY}, {"proxy.example": [_TUNNEL_OPEN, _OK]}, _PUBLIC)

        self.resolver.assert_awaited()  # the answers are still checked before anything is sent
        self.assertEqual(wire.connects, [("proxy.example", 3128)])
        self.assertEqual(wire.request_head(b"CONNECT")[0], b"CONNECT hooks.example.com:8443 HTTP/1.1")
        self.assertEqual(wire.tls_names, ["hooks.example.com"])
        request_line, headers = wire.request_head()
        self.assertEqual(request_line, b"POST /approval?x=1 HTTP/1.1")
        self.assertEqual(headers[b"host"], b"hooks.example.com:8443")

    async def test_no_proxy_naming_the_host_keeps_the_direct_pinned_connection(self) -> None:
        env = {"HTTPS_PROXY": _PROXY, "NO_PROXY": "hooks.example.com"}
        wire = await self._dispatch(env, {_PUBLIC: [_OK]}, _PUBLIC)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443)])
        self.assertEqual(wire.tls_names, ["hooks.example.com"])

    async def test_the_next_checked_address_is_tried_when_one_refuses_the_connection(self) -> None:
        # STORE-20: pinning to the first answer lost the failover httpx's own lookup had (anyio tries each address).
        refused = httpcore.ConnectError("connection refused")
        wire = await self._dispatch({}, {_PUBLIC: refused, _SECOND: [_OK]}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443), (_SECOND, 8443)])
        self.assertEqual(wire.tls_names, ["hooks.example.com"])
        self.assertEqual(wire.request_head()[1][b"host"], b"hooks.example.com:8443")
        self.assertEqual(sum(chunk.startswith(b"POST ") for chunk in wire.sent), 1)

    async def test_the_next_checked_address_is_tried_when_one_times_out(self) -> None:
        timed_out = httpcore.ConnectTimeout("timed out")
        wire = await self._dispatch({}, {_PUBLIC: timed_out, _SECOND: [_OK]}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443), (_SECOND, 8443)])
        self.assertEqual(sum(chunk.startswith(b"POST ") for chunk in wire.sent), 1)

    async def test_the_next_checked_address_is_tried_when_one_fails_the_tls_handshake(self) -> None:
        # R5C-7: a listener on the first address that answers the ClientHello in plaintext. At httpx 0.28.1 and
        # httpcore 1.0.9 the failed handshake (ssl.SSLError, as for a certificate that does not verify) is raised as
        # ConnectError: no byte of the request had gone, so the next address is tried.
        heard: list[bytes] = []

        async def not_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            heard.append(await reader.read(4096))
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

        listener = await asyncio.start_server(not_tls, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        async with listener:
            wire = await self._dispatch(
                {},
                {"127.0.0.1": _REAL, _SECOND: [_OK]},
                "127.0.0.1",
                _SECOND,
                url=f"https://hooks.example.com:{port}/approval",
                allow_private_targets=True,
            )

        self.assertEqual(wire.connects, [("127.0.0.1", port), (_SECOND, port)])
        self.assertEqual(len(heard), 1)
        self.assertTrue(heard[0].startswith(b"\x16\x03"), "the first address heard a TLS ClientHello")
        self.assertNotIn(b"POST", heard[0])
        self.assertEqual(wire.tls_names, ["hooks.example.com"])
        self.assertEqual(sum(chunk.startswith(b"POST ") for chunk in wire.sent), 1)
        self.assertEqual([args for args in self._warned() if args[0] == "webhook %s returned %d"], [])

    async def test_a_read_timeout_on_the_first_address_sends_nothing_to_the_second(self) -> None:
        # The request had gone when the answer timed out: posting it again could deliver the event twice.
        timed_out = [httpcore.ReadTimeout("timed out")]
        wire = await self._dispatch({}, {_PUBLIC: timed_out, _SECOND: [_OK]}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443)])
        self.assertEqual(sum(chunk.startswith(b"POST ") for chunk in wire.sent), 1)
        failed = [args for args in self._warned() if args[0] == "webhook dispatch to %s failed: %s"]
        self.assertEqual(failed, [("webhook dispatch to %s failed: %s", "https://hooks.example.com", "ReadTimeout")])

    async def test_an_http_error_answer_is_final(self) -> None:
        wire = await self._dispatch({}, {_PUBLIC: [_SERVER_ERROR], _SECOND: [_OK]}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443)])
        self.assertIn(("webhook %s returned %d", "https://hooks.example.com", 500), self._warned())

    async def test_a_connection_that_drops_after_the_request_is_sent_is_not_tried_again(self) -> None:
        # The request may have arrived: posting it to the next address could deliver the event twice.
        wire = await self._dispatch({}, {_PUBLIC: [], _SECOND: [_OK]}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443)])
        self.assertEqual(sum(chunk.startswith(b"POST ") for chunk in wire.sent), 1)

    async def test_when_no_checked_address_connects_the_failure_is_logged_once(self) -> None:
        refused = httpcore.ConnectError("connection refused")
        wire = await self._dispatch({}, {_PUBLIC: refused, _SECOND: refused}, _PUBLIC, _SECOND)

        self.assertEqual(wire.connects, [(_PUBLIC, 8443), (_SECOND, 8443)])
        failed = [args for args in self._warned() if args[0] == "webhook dispatch to %s failed: %s"]
        self.assertEqual(failed, [("webhook dispatch to %s failed: %s", "https://hooks.example.com", "ConnectError")])

    def _warned(self) -> list[tuple]:
        return [call.args for call in self.warnings.call_args_list]

    async def test_a_private_answer_is_refused_before_any_proxy_is_used(self) -> None:
        wire = await self._dispatch({"HTTPS_PROXY": _PROXY}, {"proxy.example": [_TUNNEL_OPEN, _OK]}, "10.0.0.5")

        self.resolver.assert_awaited()
        self.assertEqual(wire.connects, [])

    async def test_no_log_line_carries_the_webhook_url_at_the_controllers_default_level(self) -> None:
        # httpx logs each request's whole URL at INFO, userinfo, path and query included (checked against httpx 0.28),
        # and the controller logs at INFO by default: a webhook's path and query are often its secret.
        import app.main  # noqa: F401  (the controller's logging setup)

        lines: list[str] = []

        class _Recorder(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(record.getMessage())

        recorder = _Recorder()
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        root.setLevel(logging.INFO)
        root.addHandler(recorder)
        self.addCleanup(root.removeHandler, recorder)

        await self._dispatch({}, {_PUBLIC: [_OK]}, _PUBLIC)

        self.assertEqual([line for line in lines if "/approval" in line or "x=1" in line], [])

    async def test_a_proxy_httpx_cannot_use_is_logged_by_type_and_never_raised(self) -> None:
        # Without socksio, httpx raises while it builds a client for a socks5:// proxy; with it, the fake socket
        # refuses the connect. Either way the dispatch logs the error type, as for a failed post, and returns: the
        # proxy URL, which can carry credentials, is not logged.
        await self._dispatch({"ALL_PROXY": "socks5://user:pw-in-proxy@127.0.0.1:1080"}, {}, _PUBLIC)

        logged = [call.args for call in self.warnings.call_args_list]
        self.assertTrue(any(args[0] == "webhook dispatch to %s failed: %s" for args in logged), logged)
        self.assertNotIn("pw-in-proxy", repr(logged))


if __name__ == "__main__":
    unittest.main()
