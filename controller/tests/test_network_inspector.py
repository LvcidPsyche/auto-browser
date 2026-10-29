from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from app.network_inspector import NetworkInspector
from app.pii_scrub import PiiScrubber


class FakeRequest:
    def __init__(
        self,
        *,
        url: str = "https://example.com/api",
        method: str = "GET",
        resource_type: str = "xhr",
        headers: dict[str, str] | None = None,
        post_data: str | None = None,
        failure: str = "net::ERR_FAILED",
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data
        self._failure = failure

    def failure(self) -> str:
        return self._failure

    def is_navigation_request(self) -> bool:
        return False


class FakeResponse:
    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.request = request
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.body_calls = 0

    async def body(self) -> bytes:
        self.body_calls += 1
        return self._body


class FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}

    def on(self, event: str, callback) -> None:
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback) -> None:
        self.listeners.pop(event, None)


class FakeScrubber:
    def network_body(self, body, content_type: str):
        return "[redacted]", ["secret"] if "secret" in str(body).lower() else []

    def url(self, value: str):
        return value, []


class NetworkInspectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_request_masks_headers_scrubs_bodies_and_fires_hook(self) -> None:
        inspector = NetworkInspector("session-1", scrubber=FakeScrubber())
        matched: list[dict] = []

        async def hook(entry: dict) -> None:
            matched.append(entry)

        inspector.register_hook("https://example.com/*", hook)
        request = FakeRequest(
            method="POST",
            headers={"authorization": "Bearer secret", "content-type": "application/json"},
            post_data='{"secret":"token"}',
        )

        await inspector._handle_request(request)
        await inspector._handle_response(
            FakeResponse(
                request,
                status=201,
                headers={"set-cookie": "sid=secret", "content-type": "application/json"},
                body=b'{"secret":"response"}',
            )
        )
        await inspector._handle_request_finished(request)

        entries = inspector.entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], 201)
        self.assertEqual(entry["request_headers"]["authorization"], "[MASKED]")
        self.assertEqual(entry["response_headers"]["set-cookie"], "[MASKED]")
        self.assertEqual(entry["request_body"], "[redacted]")
        self.assertEqual(entry["response_body"], "[redacted]")
        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(matched[0]["id"], entry["id"])
        self.assertEqual(inspector.summary()["hooks"], 1)
        self.assertTrue(inspector.remove_hook("https://example.com/*"))
        self.assertFalse(inspector.remove_hook("https://example.com/*"))

    async def test_skips_static_resources_records_failures_and_flushes_pending(self) -> None:
        inspector = NetworkInspector("session-2", capture_bodies=False)
        await inspector._handle_request(FakeRequest(resource_type="image"))
        self.assertEqual(inspector.summary()["total"], 0)

        failed = FakeRequest(url="https://example.com/fail")
        await inspector._handle_request(failed)
        await inspector._handle_request_failed(failed)
        failure = inspector.entries()[0]
        self.assertTrue(failure["failed"])
        self.assertEqual(failure["failure_text"], "net::ERR_FAILED")

        pending = FakeRequest(url="https://example.com/pending")
        await inspector._handle_request(pending)
        await inspector._flush_pending()
        flushed = inspector.entries(url_contains="pending")[0]
        self.assertTrue(flushed["failed"])
        self.assertEqual(flushed["failure_text"], "session detached")

        page = FakePage()
        inspector.attach(page)
        self.assertIn("request", page.listeners)
        inspector.detach()
        await asyncio.sleep(0)
        self.assertEqual(page.listeners, {})
        inspector.clear()
        self.assertEqual(inspector.entries(), [])

    async def test_requests_that_never_finish_are_bounded(self) -> None:
        inspector = NetworkInspector("session-3", max_entries=3, capture_bodies=False)
        requests = [FakeRequest(url=f"https://example.com/poll/{i}") for i in range(5)]
        for request in requests:
            await inspector._handle_request(request)

        self.assertEqual(len(inspector._pending), 3)
        untracked = inspector.entries()
        self.assertEqual([e["url"] for e in reversed(untracked)], [r.url for r in requests[:2]])
        self.assertTrue(all(e["failed"] for e in untracked))

        # A request still tracked finishes normally.
        await inspector._handle_request_finished(requests[-1])
        self.assertFalse(inspector.entries()[0]["failed"])

    async def test_hooks_see_the_status_while_the_body_is_still_being_read(self) -> None:
        inspector = NetworkInspector("session-4")
        seen: list[int | None] = []

        async def hook(entry: dict) -> None:
            seen.append(entry["status"])

        inspector.register_hook("*", hook)
        request = FakeRequest()
        body_started = asyncio.Event()
        release_body = asyncio.Event()

        class SlowResponse(FakeResponse):
            async def body(self) -> bytes:
                body_started.set()
                await release_body.wait()
                return b"{}"

        await inspector._handle_request(request)
        response_task = asyncio.create_task(
            inspector._handle_response(SlowResponse(request, status=204, headers={"content-type": "application/json"}))
        )
        await asyncio.wait_for(body_started.wait(), timeout=5)
        await inspector._handle_request_finished(request)
        release_body.set()
        await response_task

        self.assertEqual(seen, [204])
        self.assertEqual(inspector.entries()[0]["response_body"], "{}")

    async def test_body_is_not_read_for_binary_or_declared_oversize_responses(self) -> None:
        # response.body() buffers the whole payload, so the decision to skip
        # must come from the headers: a download read "just to be dropped" is
        # the memory spike this guards against.
        inspector = NetworkInspector("session-5", body_max_bytes=100)
        cases = {
            "binary": {"content-type": "video/mp4", "content-length": "50"},
            "oversize": {"content-type": "application/json", "content-length": "5000"},
        }
        for name, headers in cases.items():
            request = FakeRequest(url=f"https://example.com/{name}")
            response = FakeResponse(request, headers=headers, body=b"x" * 50)
            await inspector._handle_request(request)
            await inspector._handle_response(response)
            await inspector._handle_request_finished(request)
            self.assertEqual(response.body_calls, 0, name)
            self.assertIsNone(inspector.entries()[0]["response_body"], name)

    async def test_body_is_read_when_length_is_declared_small_or_unknown(self) -> None:
        inspector = NetworkInspector("session-6", body_max_bytes=100)
        cases = {
            "small": ({"content-type": "application/json", "content-length": "2"}, b"{}", "{}"),
            "unknown-length": ({"content-type": "application/json"}, b"{}", "{}"),
            # An undeclared length can only be checked after the read; still dropped.
            "unknown-length-oversize": ({"content-type": "application/json"}, b"x" * 500, None),
            "garbled-length": ({"content-type": "text/html", "content-length": "abc"}, b"<p>", "<p>"),
        }
        for name, (headers, body, expected) in cases.items():
            request = FakeRequest(url=f"https://example.com/{name}")
            response = FakeResponse(request, headers=headers, body=body)
            await inspector._handle_request(request)
            await inspector._handle_response(response)
            await inspector._handle_request_finished(request)
            self.assertEqual(response.body_calls, 1, name)
            self.assertEqual(inspector.entries()[0]["response_body"], expected, name)


SIGNUP_URL = "https://x.test/signup?email=ops%40example.com"
SIGNUP_URL_SCRUBBED = "https://x.test/signup?email=%5BREDACTED%5D"
JSON_BODY = '{"email":"ops@example.com"}'
EMAIL_HIT = {"pattern": "email", "offset": 10, "length": 15, "preview": "ops@ex…"}


async def _record(inspector: NetworkInspector, request: FakeRequest, response: FakeResponse | None = None) -> dict:
    await inspector._handle_request(request)
    if response is not None:
        await inspector._handle_response(response)
    await inspector._handle_request_finished(request)
    return inspector.entries()[0]


class NetworkInspectorEncodedPiiTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_url_query_is_scrubbed_in_the_stored_entry(self) -> None:
        inspector = NetworkInspector("session-url", scrubber=PiiScrubber())

        entry = await _record(inspector, FakeRequest(url=SIGNUP_URL))

        self.assertEqual(entry["url"], SIGNUP_URL_SCRUBBED)
        self.assertTrue(entry["pii_redacted"])
        self.assertNotIn("ops", json.dumps(inspector.entries(url_contains="signup")))

    async def test_url_without_pii_is_stored_unchanged(self) -> None:
        inspector = NetworkInspector("session-url-clean", scrubber=PiiScrubber())

        entry = await _record(inspector, FakeRequest(url="https://x.test/search?q=shoes&page=2"))

        self.assertEqual(entry["url"], "https://x.test/search?q=shoes&page=2")
        self.assertFalse(entry["pii_redacted"])

    async def test_referer_and_location_are_url_scrubbed_and_more_token_headers_masked(self) -> None:
        inspector = NetworkInspector("session-headers", scrubber=PiiScrubber())
        request = FakeRequest(
            url="https://x.test/next",
            headers={"referer": SIGNUP_URL, "x-xsrf-token": "abc123", "x-amz-security-token": "abc123"},
        )
        response = FakeResponse(request, headers={"location": SIGNUP_URL, "content-type": "text/html"}, body=b"<p>")

        entry = await _record(inspector, request, response)

        self.assertEqual(entry["request_headers"]["referer"], SIGNUP_URL_SCRUBBED)
        self.assertEqual(entry["request_headers"]["x-xsrf-token"], "[MASKED]")
        self.assertEqual(entry["request_headers"]["x-amz-security-token"], "[MASKED]")
        self.assertEqual(entry["response_headers"]["location"], SIGNUP_URL_SCRUBBED)

    async def test_a_redaction_in_the_referer_alone_is_counted(self) -> None:
        inspector = NetworkInspector("session-referer-flag", scrubber=PiiScrubber())

        entry = await _record(inspector, FakeRequest(url="https://x.test/clean", headers={"referer": SIGNUP_URL}))

        self.assertEqual(entry["request_headers"]["referer"], SIGNUP_URL_SCRUBBED)
        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(inspector.summary()["pii_redacted"], 1)

    async def test_a_redaction_in_the_location_alone_is_counted(self) -> None:
        inspector = NetworkInspector("session-location-flag", scrubber=PiiScrubber())
        request = FakeRequest(url="https://x.test/clean")
        # A body that is never read: the flag has to come from the header, set when the headers are recorded.
        response = FakeResponse(request, status=302, headers={"location": SIGNUP_URL, "content-type": "video/mp4"})

        entry = await _record(inspector, request, response)

        self.assertEqual(entry["response_headers"]["location"], SIGNUP_URL_SCRUBBED)
        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(inspector.summary()["pii_redacted"], 1)

    async def test_clean_url_headers_do_not_set_the_flag(self) -> None:
        inspector = NetworkInspector("session-clean-headers", scrubber=PiiScrubber())
        request = FakeRequest(url="https://x.test/clean", headers={"referer": "https://x.test/r?q=shoes"})
        response = FakeResponse(request, headers={"location": "https://x.test/next", "content-type": "video/mp4"})

        entry = await _record(inspector, request, response)

        self.assertFalse(entry["pii_redacted"])
        self.assertEqual(inspector.summary()["pii_redacted"], 0)

    async def test_query_longer_than_the_body_cap_is_replaced_not_scrubbed(self) -> None:
        inspector = NetworkInspector("session-url-long", scrubber=PiiScrubber(), body_max_bytes=256)
        url = "https://x.test/p?" + "&".join(f"k{i}=ops%40example.com" for i in range(40)) + "#f"

        entry = await _record(inspector, FakeRequest(url=url, headers={"referer": url}))

        self.assertEqual(entry["url"], "https://x.test/p?[QUERY TOO LONG]")
        self.assertEqual(entry["request_headers"]["referer"], "https://x.test/p?[QUERY TOO LONG]")
        self.assertTrue(entry["pii_redacted"])

    async def test_a_long_fragment_is_replaced_and_does_not_make_the_query_long(self) -> None:
        inspector = NetworkInspector("session-url-frag", scrubber=PiiScrubber(), body_max_bytes=256)
        fragment_only = "https://x.test/p#" + "a" * 300
        short_query = "https://x.test/p?email=ops%40example.com#" + "a" * 300

        long_fragment = await _record(inspector, FakeRequest(url=fragment_only, headers={"referer": fragment_only}))
        scrubbed = await _record(inspector, FakeRequest(url=short_query))

        self.assertEqual(long_fragment["url"], "https://x.test/p#[FRAGMENT TOO LONG]")
        self.assertEqual(long_fragment["request_headers"]["referer"], "https://x.test/p#[FRAGMENT TOO LONG]")
        self.assertTrue(long_fragment["pii_redacted"])
        self.assertEqual(scrubbed["url"], "https://x.test/p?email=%5BREDACTED%5D#[FRAGMENT TOO LONG]")
        self.assertTrue(scrubbed["pii_redacted"])

    async def test_a_fragment_at_the_cap_is_scrubbed_and_one_byte_over_is_replaced(self) -> None:
        inspector = NetworkInspector("session-url-frag-cap", scrubber=PiiScrubber(), body_max_bytes=256)
        at_cap = "https://x.test/p#" + "é" * 128  # 256 bytes
        over_cap = "https://x.test/p#" + "é" * 129
        emoji = "https://x.test/p#t=" + "\U0001f600" * 100  # 100 characters, 400 bytes

        kept = await _record(inspector, FakeRequest(url=at_cap))
        over = await _record(inspector, FakeRequest(url=over_cap))
        four_byte = await _record(inspector, FakeRequest(url=emoji, headers={"referer": emoji}))

        self.assertEqual(kept["url"], at_cap)
        self.assertFalse(kept["pii_redacted"])
        self.assertEqual(over["url"], "https://x.test/p#[FRAGMENT TOO LONG]")
        self.assertEqual(four_byte["url"], "https://x.test/p#[FRAGMENT TOO LONG]")
        self.assertEqual(four_byte["request_headers"]["referer"], "https://x.test/p#[FRAGMENT TOO LONG]")

    async def test_a_query_over_the_cap_still_drops_the_fragment_with_it(self) -> None:
        inspector = NetworkInspector("session-url-query-and-frag", scrubber=PiiScrubber(), body_max_bytes=256)
        url = "https://x.test/p?" + "&".join(f"k{i}=ops%40example.com" for i in range(40)) + "#access_token=Opaque-1234"

        entry = await _record(inspector, FakeRequest(url=url))

        self.assertEqual(entry["url"], "https://x.test/p?[QUERY TOO LONG]")

    async def test_the_fragment_of_a_location_and_a_referer_is_scrubbed_and_counted(self) -> None:
        inspector = NetworkInspector("session-location-frag", scrubber=PiiScrubber())
        callback = "https://app.test/cb#access_token=Opaque-Access-123&id_token=Opaque-Id-4567&state=s"
        request = FakeRequest(
            url="https://x.test/clean", headers={"referer": "https://app.test/r#code=Opaque-Code-1234567"}
        )
        response = FakeResponse(request, status=302, headers={"location": callback, "content-type": "video/mp4"})

        entry = await _record(inspector, request, response)

        self.assertEqual(
            entry["response_headers"]["location"],
            "https://app.test/cb#access_token=%5BREDACTED%5D&id_token=%5BREDACTED%5D&state=s",
        )
        self.assertEqual(entry["request_headers"]["referer"], "https://app.test/r#[REDACTED]")
        self.assertTrue(entry["pii_redacted"])
        self.assertNotIn("Opaque", json.dumps(inspector.entries()))

    async def test_a_redaction_in_the_location_fragment_alone_is_counted(self) -> None:
        inspector = NetworkInspector("session-location-frag-flag", scrubber=PiiScrubber())
        request = FakeRequest(url="https://x.test/clean")
        response = FakeResponse(
            request, status=302, headers={"location": "https://app.test/cb#access_token=Opaque-Access-123"}
        )

        entry = await _record(inspector, request, response)

        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(inspector.summary()["pii_redacted"], 1)

    async def test_a_failing_scrub_of_a_url_with_a_fragment_drops_the_query_and_the_fragment(self) -> None:
        scrubber = PiiScrubber()
        inspector = NetworkInspector("session-url-frag-fail", scrubber=scrubber)
        url = "https://x.test/cb?a=1#access_token=Opaque-Access-123"

        with mock.patch.object(scrubber, "url", side_effect=RuntimeError("boom")):
            entry = await _record(inspector, FakeRequest(url=url))

        self.assertEqual(entry["url"], "https://x.test/cb")
        self.assertTrue(entry["pii_redacted"])

    async def test_query_cap_counts_utf8_bytes_not_characters(self) -> None:
        inspector = NetworkInspector("session-url-bytes", scrubber=PiiScrubber(), body_max_bytes=256)
        # "q=" plus n characters of 2 bytes each: 128 of them is exactly the cap, 129 is one over.
        at_cap = "https://x.test/p?" + "é" * 128
        over_cap = "https://x.test/p?" + "é" * 129
        four_byte = "https://x.test/p?q=" + "\U0001f600" * 100  # 100 characters, 400 bytes

        kept = await _record(inspector, FakeRequest(url=at_cap))
        over = await _record(inspector, FakeRequest(url=over_cap))
        emoji = await _record(inspector, FakeRequest(url=four_byte, headers={"referer": four_byte}))

        self.assertEqual(kept["url"], at_cap)
        self.assertFalse(kept["pii_redacted"])
        self.assertEqual(over["url"], "https://x.test/p?[QUERY TOO LONG]")
        self.assertEqual(emoji["url"], "https://x.test/p?[QUERY TOO LONG]")
        self.assertEqual(emoji["request_headers"]["referer"], "https://x.test/p?[QUERY TOO LONG]")
        self.assertTrue(emoji["pii_redacted"])

    async def test_url_with_a_lone_surrogate_is_still_recorded(self) -> None:
        inspector = NetworkInspector("session-url-surrogate", scrubber=PiiScrubber(), body_max_bytes=256)

        entry = await _record(inspector, FakeRequest(url="https://x.test/p?q=\ud800"))

        self.assertEqual(entry["url"], "https://x.test/p?q=\ud800")

    async def test_url_scrub_result_that_is_not_a_string_drops_the_query(self) -> None:
        scrubber = PiiScrubber()
        inspector = NetworkInspector("session-url-type", scrubber=scrubber)

        with mock.patch.object(scrubber, "url", return_value=(None, [EMAIL_HIT])):
            entry = await _record(inspector, FakeRequest(url=SIGNUP_URL))

        self.assertEqual(entry["url"], "https://x.test/signup")
        self.assertTrue(entry["pii_redacted"])
        self.assertEqual(inspector.entries(url_contains="signup")[0]["url"], "https://x.test/signup")

    async def test_failing_url_scrub_drops_the_query_instead_of_storing_it(self) -> None:
        scrubber = PiiScrubber()
        inspector = NetworkInspector("session-url-fail", scrubber=scrubber)
        request = FakeRequest(url=SIGNUP_URL, headers={"referer": SIGNUP_URL})

        with (
            mock.patch.object(scrubber, "url", side_effect=RuntimeError("boom")),
            self.assertLogs("app.network_inspector", level="DEBUG") as logs,
        ):
            entry = await _record(inspector, request)

        self.assertFalse(any("ops" in line or "email=" in line for line in logs.output), logs.output)
        self.assertEqual(entry["url"], "https://x.test/signup")
        self.assertEqual(entry["request_headers"]["referer"], "https://x.test/signup")
        self.assertTrue(entry["pii_redacted"])


class NetworkInspectorFailClosedTests(unittest.IsolatedAsyncioTestCase):
    CASES = {
        "bytes-result": {"return_value": (b"x", [EMAIL_HIT])},
        "empty-result": {"return_value": ("", [EMAIL_HIT])},
        "raises": {"side_effect": RuntimeError(f"cannot scrub {JSON_BODY}")},
        # An unusable value is unusable whatever the hits say: a scrubber that reports no hits
        # must not get its (wrong-typed) result waved through with the raw body.
        "none-empty-hits": {"return_value": (None, [])},
        "bytes-empty-hits": {"return_value": (b"x", [])},
        "int-false-hits": {"return_value": (42, False)},
        "none-none": {"return_value": (None, None)},
        "empty-text-no-hits": {"return_value": ("", [])},
    }

    async def test_request_body_is_not_stored_when_the_scrub_result_is_unusable(self) -> None:
        for name, behaviour in self.CASES.items():
            with self.subTest(name):
                scrubber = PiiScrubber()
                inspector = NetworkInspector(f"session-req-{name}", scrubber=scrubber)
                request = FakeRequest(method="POST", headers={"content-type": "application/json"}, post_data=JSON_BODY)

                with mock.patch.object(scrubber, "network_body", **behaviour):
                    entry = await _record(inspector, request)

                self.assertIsNone(entry["request_body"])
                self.assertTrue(entry["pii_redacted"])

    async def test_response_body_is_not_stored_when_the_scrub_result_is_unusable(self) -> None:
        for name, behaviour in self.CASES.items():
            with self.subTest(name):
                scrubber = PiiScrubber()
                inspector = NetworkInspector(f"session-resp-{name}", scrubber=scrubber)
                request = FakeRequest()
                response = FakeResponse(request, headers={"content-type": "application/json"}, body=JSON_BODY.encode())

                with mock.patch.object(scrubber, "network_body", **behaviour):
                    entry = await _record(inspector, request, response)

                self.assertIsNone(entry["response_body"])
                self.assertTrue(entry["pii_redacted"])

    async def test_scrub_failure_is_logged_with_the_url_and_never_the_body(self) -> None:
        scrubber = PiiScrubber()
        inspector = NetworkInspector("session-log", scrubber=scrubber)
        request = FakeRequest(
            url="https://x.test/api", method="POST", headers={"content-type": "application/json"}, post_data=JSON_BODY
        )
        response = FakeResponse(request, headers={"content-type": "application/json"}, body=JSON_BODY.encode())

        with (
            mock.patch.object(scrubber, "network_body", side_effect=RuntimeError(f"cannot scrub {JSON_BODY}")),
            self.assertLogs("app.network_inspector", level="WARNING") as logs,
        ):
            await _record(inspector, request, response)

        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 2)
        for line in warnings:
            self.assertIn("https://x.test/api", line)
            self.assertNotIn("ops@example.com", line)

    async def test_usable_scrub_result_is_still_stored(self) -> None:
        inspector = NetworkInspector("session-ok", scrubber=PiiScrubber())
        request = FakeRequest(method="POST", headers={"content-type": "application/json"}, post_data=JSON_BODY)

        entry = await _record(inspector, request)

        self.assertEqual(entry["request_body"], '{"email":"[REDACTED]"}')
        self.assertTrue(entry["pii_redacted"])


if __name__ == "__main__":
    unittest.main()
