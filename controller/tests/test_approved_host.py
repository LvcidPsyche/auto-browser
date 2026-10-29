"""The host an operator approves is the host that runs.

Python's urlsplit does not end a URL's authority at a backslash; a browser,
Playwright and the ws library (WHATWG URL) do, for http(s) and ws(s). So
``wss://x@evil.example\\@bank.example/...`` was accepted as a CDP URL, its
preview read ``wss://<redacted>@bank.example/...`` (the real host inside the
userinfo it left out), and Playwright connected to evil.example. A cookie's url
and a harness run's start URL did the same. Such a URL is now refused where it
is validated, and the preview finds the authority as app/url_safety.py does, so
whatever reaches it shows the host that will be used.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import playwright
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.approval_display import approval_summary, describe_action
from app.browser_manager import BrowserManager
from app.harness.contracts import Precondition
from app.models import BrowserActionDecision, CreateSessionRequest, NavigateRequest, OpenTabRequest
from app.routes.sessions import create_sessions_router
from app.tool_gateway.previews import redacted_url
from app.tool_inputs import (
    CdpAttachInput,
    CreateCronJobInput,
    ForkSessionInput,
    HarnessStartConvergenceInput,
    SetCookiesInput,
)
from app.url_safety import host_as_sent
from tests.test_approval_spend import SESSION, SpendBase
from tests.test_browser_manager_create_session import _settings
from tests.test_url_safety import BROWSER_HOSTS

BS = chr(92)
IDEOGRAPHIC_FULL_STOP = chr(0x3002)
# An internationalized name, as written and as a browser connects to it (WHATWG domain to ASCII).
IDN_HOST, IDN_ASCII = "b" + chr(0xFC) + "cher.example", "xn--bcher-kva.example"
# What the preview writes after a URL whose host a browser connects to in another spelling.
HOST_WRITTEN_NOTE = " (host as written: "


def playwright_node() -> str | None:
    """The node Playwright ships with (its WHATWG URL is the parser the round compares with), else node on PATH."""
    driver = Path(playwright.__file__).parent / "driver"
    for name in ("node.exe", "node"):
        if (driver / name).is_file():
            return str(driver / name)
    return shutil.which("node")


def segmented(digits: str) -> str:
    """``digits`` as Unicode 13's segmented digits (U+1FBF0 to U+1FBF9), which NFKC maps to ASCII ones."""
    return "".join(chr(0x1FBF0 + int(digit)) for digit in digits)


def full_width(text: str) -> str:
    """``text`` with each printable ASCII character as its full-width form (U+FF01 to U+FF5E)."""
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


def shown_url(shown: str) -> str:
    """The URL a preview shows, without the note on the host as written."""
    return shown.split(HOST_WRITTEN_NOTE, 1)[0]


NODE = playwright_node()
NODE_TIMEOUT_SECONDS = 30
# The review's probes (verify-OV4-1): each names one host to urllib and another to a browser.
CONFUSED = [
    f"wss://x@evil.example{BS}@bank.example/devtools/browser/abc",
    f"wss://evil.example{BS}@bank.example/devtools/browser/abc",
    f"http://evil.example:9222{BS}@browser.corp.internal:9222/",
    f"https://sso@evil.example{BS}@intranet.example/login",
]
# A control character the browser drops or refuses, where urllib keeps it or drops it otherwise.
CONTROLLED = [
    f"https://bank.example{chr(1)}/",
    f"https://bank.exa{chr(9)}mple/",
    f"wss://evil.example{chr(0)}@bank.example/",
]
# A special-scheme URL written with another run of slashes (or backslashes) than // after its colon: a browser
# skips the run and reads the authority after it (WHATWG special authority ignore slashes), urllib reads none.
SLASHES_SKIPPED = [
    "https:u:SECRET@bank.example/",
    "https:/u:SECRET@bank.example/",
    "https:////u:SECRET@bank.example/",
    "https:///bank.example/",
]
ACCEPTED = [
    "wss://browser.example/devtools/browser/abc",
    "wss://user:pw@browser.example:9222/devtools/browser/abc?token=T&mode=x",
    "http://127.0.0.1:9222/",
    "http://[::1]:9222/json/version",
    "https://a@b@bank.example/path",
    "https://bank.example:443@evil.example/",
    "HTTPS://Bank.Example/x",
    "https://bank.example/a?next=https://evil.example/",
    "https://bank.example/#@evil.example",
    "https://bank.example/?@evil.example",
    f"https://{IDN_HOST}/a?token=T",
]


# The navigation sites: a navigate action (its approval shows the url), and the navigate and open-tab requests.
NAVIGATION_BUILDS = {
    "navigate action": lambda url: BrowserActionDecision(action="navigate", reason="r", url=url),
    "navigate request": lambda url: NavigateRequest(url=url),
    "open tab request": lambda url: OpenTabRequest(url=url),
}
# A backslash or a control where it moves no host: in the query, in the path after its first slash, and
# percent-encoded. A browser loads each on the host urllib reads.
NAVIGATION_ACCEPTED = [
    f"https://example.com/search?q=C:{BS}Users{BS}me",
    f"https://example.com/a{BS}b",
    "https://example.com/search?q=%00%0A%1F",
]
# A Cyrillic a for a Latin one: the host a browser sends for it is xn--pple-43d.com.
CYRILLIC_APPLE, CYRILLIC_APPLE_SENT = f"https://{chr(0x430)}pple.com/", "xn--pple-43d.com"
# A control in a navigation URL's scheme or authority, which a browser drops (item 25): a tab or a newline in
# the host, a tab in the scheme or between its slashes, a control before it.
CONTROL_BEFORE_THE_PATH = [
    f"https://{chr(0x430)}pp{chr(9)}le.com/",
    f"https://{chr(0x430)}pp{chr(10)}le.com/",
    f"h{chr(9)}ttps://{chr(0x430)}pple.com/",
    f"https:/{chr(9)}/{chr(0x430)}pple.com/",
    f"{chr(1)}https://{chr(0x430)}pple.com/",
]
# A control after the host: in the path or the query, the page's to read (item 23).
CONTROL_AFTER_THE_HOST = [
    f"https://{chr(0x430)}pple.com/a{chr(9)}b?q={chr(1)}",
    f"https://example.com/a{chr(10)}b",
]
# Hosts a browser reads otherwise, at a navigation site too: IPv4 not as a dotted quad, a full-width name,
# segmented digits (each pinned by node in MappedHostTests and test_url_safety).
NAVIGATION_REFUSED = [
    "http://127.1/",
    f"https://{full_width('bank')}.example/",
    "http://" + segmented("127") + "." + segmented("1") + "/",
]


def whatwg_hostnames(urls: list[str]) -> list[str | None]:
    """Each URL's host as node's WHATWG URL reads it, as urlsplit writes a hostname (no brackets); None if it throws."""
    script = (
        "const urls = JSON.parse(process.argv[1]);"
        "const host = (url) => { try { return new URL(url).hostname.replace(/^\\[|\\]$/g, ''); }"
        " catch { return null; } };"
        "process.stdout.write(JSON.stringify(urls.map(host)));"
    )
    done = subprocess.run(
        [NODE, "-e", script, json.dumps(urls)], capture_output=True, text=True, check=True, timeout=NODE_TIMEOUT_SECONDS
    )
    return json.loads(done.stdout)


def as_https(url: str) -> str:
    """``url`` with the scheme a cookie's url takes."""
    return "https" + url[url.index(":") :]


class RefusalTestCase(unittest.TestCase):
    def assert_refused(self, build: object, url: str) -> None:
        # The message is checked inside the subtest: one that raised nothing ends there, not at the next line.
        with self.subTest(url=url):
            with self.assertRaises(ValidationError) as refused:
                build()  # type: ignore[operator]
            self.assertIn("another host", str(refused.exception))


class RefusedAtValidationTests(RefusalTestCase):
    def test_a_cdp_url_a_browser_reads_another_way_is_refused(self) -> None:
        for url in CONFUSED + CONTROLLED:
            self.assert_refused(lambda url=url: CdpAttachInput(cdp_url=url), url)

    def test_a_cookie_url_a_browser_reads_another_way_is_refused(self) -> None:
        for url in map(as_https, CONFUSED + CONTROLLED):
            self.assert_refused(
                lambda url=url: SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": url}]), url
            )

    def test_a_harness_start_url_a_browser_reads_another_way_is_refused(self) -> None:
        for url in CONFUSED + CONTROLLED:
            self.assert_refused(lambda url=url: Precondition(kind="start_url", value=url), url)

    def test_a_harness_mock_final_url_a_browser_reads_another_way_is_refused(self) -> None:
        # The redaction table knows it (round 5, item 4), so the same rule holds.
        contract = {"id": "c1", "goal": "g", "postconditions": [{"kind": "text_contains", "value": "done"}]}
        for url in CONFUSED + CONTROLLED:
            self.assert_refused(
                lambda url=url: HarnessStartConvergenceInput(contract=contract, mock_final_observation={"url": url}),
                url,
            )
        accepted = HarnessStartConvergenceInput(contract=contract, mock_final_observation={"url": ACCEPTED[1]})
        self.assertEqual(accepted.mock_final_observation, {"url": ACCEPTED[1]})

    def test_a_cookie_url_whose_host_reads_otherwise_is_refused(self) -> None:
        # A cookie's url is kept as given: urllib reads the host "bank.example ", a browser "bank.example".
        url = "https://bank.example "
        with self.assertRaises(ValidationError) as refused:
            SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": url}])
        self.assertIn("names another host to a browser than it shows", str(refused.exception))

    def test_a_backslash_is_refused_wherever_it_is(self) -> None:
        # One in the path names no other host, but the rule refuses every backslash: each probe above
        # also names another host, so none of them showed whether this half of the check held.
        url = f"https://bank.example/a{BS}b"
        self.assert_refused(lambda: CdpAttachInput(cdp_url=url), url)
        self.assert_refused(lambda: SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": url}]), url)
        self.assert_refused(lambda: Precondition(kind="start_url", value=url), url)

    def test_a_navigation_url_a_browser_reads_another_way_is_refused(self) -> None:
        """B-DISP-4 / Codex B 1: a navigate action's url, which an approval shows, was checked by validate_url
        alone, so "http://evil.example\\@bank.example/" was approved as written and loaded evil.example."""
        refused = [*map(as_https, CONFUSED), *NAVIGATION_REFUSED, *SLASHES_SKIPPED]
        for name, build in NAVIGATION_BUILDS.items():
            for url in refused:
                self.assert_refused(lambda build=build, url=url: build(url), f"{name}: {url}")
            for url in map(as_https, ACCEPTED):
                with self.subTest(accepted=f"{name}: {url}"):
                    self.assertEqual(build(url).url, url)

    def test_a_sessions_start_url_is_checked_by_its_host_as_a_navigation_url_is(self) -> None:
        """Item 32 (Opus C): browser.create_session, browser.fork_session and their REST routes took a start_url
        with validate_url alone, while navigate and open_tab check its host; a cron run's start_url ends there
        too (CronGovernedTests' stored-job case)."""
        builds = {
            "create session": lambda url: CreateSessionRequest(start_url=url),
            "fork session": lambda url: ForkSessionInput(session_id="abcdef123456", start_url=url),
        }
        refused = [*map(as_https, CONFUSED), *NAVIGATION_REFUSED, *SLASHES_SKIPPED, *CONTROL_BEFORE_THE_PATH]
        for name, build in builds.items():
            for url in refused:
                self.assert_refused(lambda build=build, url=url: build(url), f"{name}: {url!a}")
            for url in [*map(as_https, ACCEPTED), *NAVIGATION_ACCEPTED]:
                with self.subTest(accepted=f"{name}: {url!a}"):
                    self.assertEqual(build(url).start_url, url)

    def test_the_rest_fork_routes_start_url_is_checked_by_its_host(self) -> None:
        """Item 32: POST /sessions/{id}/fork takes start_url as a query parameter, outside the request models."""
        with tempfile.TemporaryDirectory() as tempdir:
            manager = BrowserManager(_settings(Path(tempdir)))
            manager.fork_session = AsyncMock(return_value={"id": "fork"})  # type: ignore[method-assign]
            app = FastAPI()
            app.include_router(create_sessions_router(manager=manager))
            client = TestClient(app, raise_server_exceptions=False)
            for url in ("https:example.com/", f"https://ex{chr(9)}ample.com/", "http://127.1/"):
                with self.subTest(url=ascii(url)):
                    response = client.post("/sessions/abcdef123456/fork", params={"start_url": url})
                    self.assertEqual(response.status_code, 400, response.text)
            manager.fork_session.assert_not_called()
            control = client.post("/sessions/abcdef123456/fork", params={"start_url": NAVIGATION_ACCEPTED[0]})
            self.assertEqual(control.status_code, 200, control.text)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_navigation_url_with_a_control_in_its_scheme_or_host_is_refused(self) -> None:
        """Item 25 (Codex C 2): a browser drops a tab or a newline anywhere, and a leading control, so each of
        these loads xn--pple-43d.com (node pins it), and urllib read the same host: the host-only check passed
        them. The summary punycoded the host with the tab in it (xn--pp\\tle-3ve.com), or, with a control in
        the scheme or before it, found no host and showed the Cyrillic name as written."""
        self.assertEqual(
            whatwg_hostnames(CONTROL_BEFORE_THE_PATH), [CYRILLIC_APPLE_SENT] * len(CONTROL_BEFORE_THE_PATH)
        )
        for name, build in NAVIGATION_BUILDS.items():
            for url in CONTROL_BEFORE_THE_PATH:
                self.assert_refused(lambda build=build, url=url: build(url), f"{name}: {url!a}")
            for url in CONTROL_AFTER_THE_HOST:
                with self.subTest(accepted=f"{name}: {url!a}"):
                    self.assertEqual(build(url).url, url)

    def test_a_space_or_whitespace_at_either_end_of_a_navigation_url_is_stripped_before_it_is_shown(self) -> None:
        """Item 25: what a browser strips at either end, the models strip first (str_strip_whitespace), so the
        URL shown is the one loaded, its host as sent; a control the models keep there is refused above."""
        for url in (
            f" {CYRILLIC_APPLE}",
            f"{CYRILLIC_APPLE} ",
            f"{chr(9)}{CYRILLIC_APPLE}",
            f"{CYRILLIC_APPLE}{chr(10)}",
        ):
            for name, build in NAVIGATION_BUILDS.items():
                with self.subTest(url=ascii(url), site=name):
                    self.assertEqual(build(url).url, CYRILLIC_APPLE)
            shown = describe_action(NAVIGATION_BUILDS["navigate action"](url), cut=None)
            self.assertIn(f'url="https://{CYRILLIC_APPLE_SENT}/"', shown)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_navigation_url_with_a_backslash_or_a_control_that_moves_no_host_is_accepted(self) -> None:
        """Item 23: the navigation sites took refuse_ambiguous_host's blanket rule, which refuses any backslash or
        control anywhere, so a search for a Windows path, or a path a browser reads with a slash, could no longer be
        opened, fast profile included. A browser reads each on the host urllib reads (node pins it); the fields an
        operator approves by reading them keep the blanket rule."""
        self.assertEqual(whatwg_hostnames(NAVIGATION_ACCEPTED), [urlsplit(url).hostname for url in NAVIGATION_ACCEPTED])
        for name, build in NAVIGATION_BUILDS.items():
            for url in NAVIGATION_ACCEPTED:
                with self.subTest(accepted=f"{name}: {url}"):
                    self.assertEqual(build(url).url, url)
        for url in (url for url in NAVIGATION_ACCEPTED if BS in url):
            with self.subTest(cron=url), self.assertRaisesRegex(ValidationError, "backslash"):
                CreateCronJobInput(name="j", goal="g", webhook_enabled=True, start_url=url)
            self.assert_refused(lambda url=url: Precondition(kind="start_url", value=url), url)

    def test_a_url_written_with_other_slashes_after_its_scheme_is_refused_for_every_field(self) -> None:
        """B-DISP-2 / Codex B 2: a browser reads a special-scheme URL written with no slash, one, or several
        after the colon as one whose authority follows them; urllib reads no host in it, so the check saw
        None == None and accepted it, and the preview showed its password."""
        contract = {"id": "c1", "goal": "g", "postconditions": [{"kind": "text_contains", "value": "done"}]}
        builds = {
            "cdp_url": lambda url: CdpAttachInput(cdp_url=url.replace("https:", "wss:", 1)),
            "cookie url": lambda url: SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": url}]),
            "harness start_url": lambda url: Precondition(kind="start_url", value=url),
            "harness mock final url": lambda url: HarnessStartConvergenceInput(
                contract=contract, mock_final_observation={"url": url}
            ),
            "cron start_url": lambda url: CreateCronJobInput(name="j", goal="g", webhook_enabled=True, start_url=url),
            "navigate url": lambda url: BrowserActionDecision(action="navigate", reason="r", url=url),
        }
        for name, build in builds.items():
            for url in SLASHES_SKIPPED:
                with self.subTest(field=name, url=url), self.assertRaises(ValidationError):
                    build(url)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_browser_reads_the_host_after_those_slashes(self) -> None:
        self.assertEqual(whatwg_hostnames(SLASHES_SKIPPED), ["bank.example"] * len(SLASHES_SKIPPED))
        self.assertEqual(whatwg_hostnames(["wss:u:SECRET@bank.example:9222/"]), ["bank.example"])

    def test_the_same_urls_stay_accepted_where_they_are_no_url(self) -> None:
        # A note is text, not a URL a browser loads.
        self.assertEqual(Precondition(kind="note", value=CONFUSED[0]).value, CONFUSED[0])

    def test_a_url_both_read_alike_is_accepted(self) -> None:
        for url in ACCEPTED:
            with self.subTest(url=url):
                CdpAttachInput(cdp_url=url)
                Precondition(kind="start_url", value=url)
                SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": as_https(url)}])


class DecodedHostTests(RefusalTestCase):
    """A host a browser decodes before it connects: shown as written, it is not the host used.

    WHATWG URL percent-decodes a host, and reads a host whose last label is a
    number as IPv4 in decimal, octal or hex parts, one to four of them. The
    preview showed the host as written (%62ank.example, 2130706433), and the
    browser connected to bank.example or 127.0.0.1. The rule is that the host
    shown is the host used, so each such form is refused, and the canonical one
    accepted.
    """

    def assert_refused_and_canonical_accepted(self, url: str, canonical: str) -> None:
        for build in (
            lambda url: CdpAttachInput(cdp_url=url),
            lambda url: SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": as_https(url)}]),
            lambda url: Precondition(kind="start_url", value=url),
        ):
            self.assert_refused(lambda build=build: build(url), url)
            build(canonical)  # the control: the same host, written as a browser reads it

    def test_a_percent_encoded_host_is_refused(self) -> None:
        self.assert_refused_and_canonical_accepted("https://%62ank.example/", "https://bank.example/")

    def test_a_decimal_ipv4_host_is_refused(self) -> None:
        self.assert_refused_and_canonical_accepted("http://2130706433:9222/", "http://127.0.0.1:9222/")

    def test_an_octal_ipv4_host_is_refused(self) -> None:
        self.assert_refused_and_canonical_accepted("http://0177.0.0.01:9222/", "http://127.0.0.1:9222/")

    def test_a_hex_ipv4_host_is_refused(self) -> None:
        for url in ("http://0x7f.0.0.1:9222/", "http://0x7f000001:9222/"):
            with self.subTest(url=url):
                self.assert_refused_and_canonical_accepted(url, "http://127.0.0.1:9222/")

    def test_an_ipv4_host_of_fewer_than_four_parts_is_refused(self) -> None:
        for url in ("http://127.1:9222/", "http://127.0.1:9222/"):
            with self.subTest(url=url):
                self.assert_refused_and_canonical_accepted(url, "http://127.0.0.1:9222/")

    def test_an_ipv4_host_with_a_trailing_dot_is_refused(self) -> None:
        # A browser drops the empty last label and reads the number before it: 127.1. is 127.0.0.1.
        for url in ("http://127.1.:9222/", "http://127.0.0.1.:9222/"):
            with self.subTest(url=url):
                self.assert_refused_and_canonical_accepted(url, "http://127.0.0.1:9222/")

    def test_a_name_that_only_holds_digits_is_no_ipv4_host(self) -> None:
        for url in ("https://1password.example/", "http://10.example/", "http://[::1]:9222/"):
            with self.subTest(url=url):
                CdpAttachInput(cdp_url=url)


# Codex's round-5 read: a host a browser maps before it connects, and the host it then connects to.
MAPPED = [
    (f"http://{full_width('127')}.{full_width('1')}:9222/", "127.0.0.1"),
    (f"http://{full_width('2130706433')}:9222/", "127.0.0.1"),
    (f"http://{full_width('0x7f000001')}:9222/", "127.0.0.1"),
    (f"http://127{IDEOGRAPHIC_FULL_STOP}0{IDEOGRAPHIC_FULL_STOP}0{IDEOGRAPHIC_FULL_STOP}1:9222/", "127.0.0.1"),
    (f"https://{full_width('bank')}.example/", "bank.example"),
    # What Python's idna codec, on Unicode 3.2, keeps and a browser (UTS 46) maps: Unicode 13's segmented
    # digits, and Unicode 7's U+1BCA0, which it drops.
    ("http://" + segmented("127") + "." + segmented("1") + ":9222/", "127.0.0.1"),
    ("https://bank" + chr(0x1BCA0) + ".example/", "bank.example"),
    # And what both drop: a zero-width space, a soft hyphen.
    ("https://bank" + chr(0x200B) + ".example/", "bank.example"),
    ("https://bank" + chr(0xAD) + ".example/", "bank.example"),
]
# Internationalized names a browser connects to as written, and the host it sends for each. The codec
# wrote straße as strasse (IDNA 2003) and kept a capital sharp s, so it refused both.
IDN_ACCEPTED = [
    ("B" + chr(0xDC) + "CHER.example", "xn--bcher-kva.example"),
    ("stra" + chr(0xDF) + "e.example", "xn--strae-oqa.example"),
    ("STRA" + chr(0x1E9E) + "E.example", "xn--strae-oqa.example"),
    (chr(0x1F600) + ".example", "xn--e28h.example"),
]
# Names a browser maps only by case or by composition, as written and as it sends them (node pins each):
# Cherokee capital and small letters (UTS 46 maps small to capital, str.lower capital to small), a name
# written decomposed (NFD), and the Kelvin sign, which both map to an ASCII k.
CASE_ONLY = [
    (chr(0x13A0) + ".example", "xn--58d.example"),
    (chr(0xAB70) + ".example", "xn--58d.example"),
    ("bu" + chr(0x308) + "cher.example", "xn--bcher-kva.example"),
    (chr(0x212A) + "elvin.example", "kelvin.example"),
]
# Compatibility forms str.casefold maps as UTS 46 does, as written and as a browser sends them (node pins
# each): the fi ligature, the long s, the micro sign (to a Greek mu). f8ae122 refused them (item 31).
CASEFOLD_COMPATIBILITY = [
    (chr(0xFB01) + "le.example", "file.example"),
    (chr(0x17F) + "ecure.example", "secure.example"),
    (chr(0xB5) + "site.example", "xn--site-knd.example"),
]
# A host with a code point a browser refuses (private use): no host to it.
DISALLOWED = "https://a" + chr(0xE000) + "b.example/"
# A capital sigma that ends a host: urlsplit lower-cases it to a final small sigma (str.lower's rule), and
# a browser maps it to a small sigma (bank.xn--mxa0b).
FINAL_SIGMA = "https://bank." + chr(0x391) + chr(0x3A3) + "/"


class MappedHostTests(RefusalTestCase):
    """A host a browser maps first: full-width digits and dots, and other compatibility forms.

    WHATWG URL maps a domain through IDNA (UTS 46) before it reads it as IPv4
    or resolves it, so 127.1 in full-width digits is 127.0.0.1 to a browser.
    The checks read urllib's spelling, so it passed all four inputs, and the
    preview showed it as written. A host the mapping changes is now
    refused; an internationalized name is accepted, and the preview writes the
    ASCII form a browser connects to, with the name as written beside it.
    """

    def test_a_host_a_browser_maps_to_another_is_refused(self) -> None:
        contract = {"id": "c1", "goal": "g", "postconditions": [{"kind": "text_contains", "value": "done"}]}
        for url, _ in MAPPED:
            for build in (
                lambda url: CdpAttachInput(cdp_url=url),
                lambda url: SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": as_https(url)}]),
                lambda url: Precondition(kind="start_url", value=url),
                lambda url: HarnessStartConvergenceInput(contract=contract, mock_final_observation={"url": url}),
            ):
                self.assert_refused(lambda build=build, url=url: build(url), url)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_browser_connects_each_to_another_host_than_urllib_reads(self) -> None:
        urls = [url for url, _ in MAPPED]
        self.assertEqual(whatwg_hostnames(urls), [host for _, host in MAPPED])
        for url, host in MAPPED:
            with self.subTest(url=url):
                self.assertNotEqual(urlsplit(url).hostname, host)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_an_idn_and_an_ipv6_literal_are_accepted_and_previewed_as_a_browser_reads_them(self) -> None:
        idn, ipv6 = f"https://{IDN_HOST}/a?token=T", "http://[::1]:9222/json/version"
        for url in (idn, ipv6):
            CdpAttachInput(cdp_url=url)
            Precondition(kind="start_url", value=url)
            SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": as_https(url)}])

        self.assertEqual(whatwg_hostnames([idn, ipv6]), [IDN_ASCII, "::1"])
        self.assertEqual(redacted_url(idn), f"https://{IDN_ASCII}/a?token=<redacted>{HOST_WRITTEN_NOTE}{IDN_HOST})")
        self.assertEqual(redacted_url(ipv6), ipv6)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_an_internationalized_name_is_accepted_and_previewed_as_the_host_a_browser_sends(self) -> None:
        urls = [f"https://{host}:8443/x" for host, _ in IDN_ACCEPTED]
        self.assertEqual(whatwg_hostnames(urls), [sent for _, sent in IDN_ACCEPTED])
        for url, (host, sent) in zip(urls, IDN_ACCEPTED, strict=True):
            with self.subTest(url=url):
                CdpAttachInput(cdp_url=url)
                self.assertEqual(redacted_url(url), f"https://{sent}:8443/x{HOST_WRITTEN_NOTE}{host})")

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_name_a_browser_maps_only_by_case_or_composition_is_accepted(self) -> None:
        """B-DISP-6: compared with Python's lower(), UTS 46's mapping refused every Cherokee host (it maps small
        letters to capitals, lower() the other way) and a name written decomposed (NFD), though a browser loads
        each as one valid host, with the message naming an identical-looking host."""
        urls = [f"https://{host}:8443/x" for host, _ in CASE_ONLY]
        self.assertEqual(whatwg_hostnames(urls), [sent for _, sent in CASE_ONLY])
        for url, (host, sent) in zip(urls, CASE_ONLY, strict=True):
            with self.subTest(url=ascii(url)):
                CdpAttachInput(cdp_url=url)
                Precondition(kind="start_url", value=url)
                SetCookiesInput(cookies=[{"name": "sid", "value": "v", "url": url}])
                shown = redacted_url(url)
                self.assertTrue(shown.startswith(f"https://{sent}:8443/x"), shown)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_compatibility_form_casefold_maps_as_a_browser_does_is_accepted_and_shown_as_sent(self) -> None:
        """Item 31 (Opus C): item 16 compares the mapping with the host as written, case-folded, and casefold
        maps these as UTS 46 does, so they are accepted where f8ae122 refused them; each is shown as the host a
        browser sends."""
        urls = [f"https://{host}:8443/x" for host, _ in CASEFOLD_COMPATIBILITY]
        self.assertEqual(whatwg_hostnames(urls), [sent for _, sent in CASEFOLD_COMPATIBILITY])
        for url, (_host, sent) in zip(urls, CASEFOLD_COMPATIBILITY, strict=True):
            with self.subTest(url=ascii(url)):
                CdpAttachInput(cdp_url=url)
                Precondition(kind="start_url", value=url)
                self.assertEqual(NAVIGATION_BUILDS["navigate request"](url).url, url)
                shown = redacted_url(url)
                self.assertTrue(shown.startswith(f"https://{sent}:8443/x"), shown)

    def test_a_host_with_a_code_point_a_browser_refuses_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as refused:
            CdpAttachInput(cdp_url=DISALLOWED)
        self.assertIn("a browser refuses", str(refused.exception))

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_browser_reads_no_host_in_it(self) -> None:
        self.assertEqual(whatwg_hostnames([DISALLOWED]), [None])

    def test_the_preview_shows_a_host_it_cannot_map_as_written(self) -> None:
        # Validation refuses such a host first; the preview, which a governed call must get through to reach its
        # approval, does not raise on it either.
        self.assertEqual(redacted_url(DISALLOWED), DISALLOWED)

    def test_a_host_is_mapped_as_written_not_as_urllib_lower_cases_it(self) -> None:
        # Mapped as urlsplit wrote it, the host would be accepted and previewed as another than a browser's.
        self.assert_refused(lambda: CdpAttachInput(cdp_url=FINAL_SIGMA), FINAL_SIGMA)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_a_browser_sends_another_host_than_urllibs_spelling_maps_to(self) -> None:
        self.assertEqual(whatwg_hostnames([FINAL_SIGMA]), ["bank.xn--mxa0b"])
        self.assertNotEqual(host_as_sent(urlsplit(FINAL_SIGMA).hostname), "bank.xn--mxa0b")


class PreviewHostTests(unittest.TestCase):
    def test_the_preview_shows_the_host_a_browser_loads(self) -> None:
        # Chromium's own answers (test_url_safety), and the review's probes as the preview would get them
        # if anything let them through.
        cases = [*BROWSER_HOSTS, ("wss://x@evil.example" + BS + "@bank.example/devtools/browser/abc", "evil.example")]
        for url, host in cases:
            with self.subTest(url=url):
                shown = redacted_url(url)
                self.assertEqual(urlsplit(shown).hostname, host, shown)

    @unittest.skipIf(NODE is None, "no node binary")
    def test_every_accepted_url_previews_the_host_whatwg_reads(self) -> None:
        urls = ACCEPTED + CONFUSED
        for url, host in zip(urls, whatwg_hostnames(urls), strict=True):
            with self.subTest(url=url):
                shown = redacted_url(url)
                self.assertEqual(urlsplit(shown_url(shown)).hostname, host, shown)


class GovernedNavigationTests(SpendBase):
    """A navigate an approval holds, through the real gateway and the manager (item 23, B-DISP-4)."""

    async def ask(self, url: str):
        self.settings.allowed_hosts = f"example.com,{IDN_ASCII},{IDN_HOST}"
        return await self.call(
            "browser.execute_action",
            session_id=SESSION,
            workflow_profile="governed",
            action={"action": "navigate", "reason": "pay", "url": url, "risk_category": "payment"},
        )

    async def test_a_governed_navigate_approval_shows_an_idn_as_the_host_a_browser_sends(self) -> None:
        ask = await self.ask(f"https://{IDN_HOST}/a")

        self.assertEqual(ask.structuredContent.get("status"), "approval_required", ask.content[0].text)
        approval = await self.manager.approvals.get(ask.structuredContent["approval_id"])
        self.assertEqual(
            approval_summary(approval), f'navigate \u00b7 url="https://{IDN_ASCII}/a"{HOST_WRITTEN_NOTE}{IDN_HOST})'
        )

    async def test_a_governed_navigate_with_a_backslash_in_its_query_asks_for_approval(self) -> None:
        ask = await self.ask(NAVIGATION_ACCEPTED[0])

        self.assertEqual(ask.structuredContent.get("status"), "approval_required", ask.content[0].text)
