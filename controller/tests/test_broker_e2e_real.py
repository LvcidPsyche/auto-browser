"""Employee operations end to end: approval broker -> real controller -> real
browser-node persistent profile.

2026-09-25 04:01: in persistent mode an employee's first observe through the
broker hung, the controller held the session lock forever and the owner's
live view dropped -- employees could not use the browser at all. This drives
every operation an employee has (observe, click, type, press, scroll, hover,
select_option, navigate, reload, go_back, go_forward, wait, list/open/activate
tabs, upload) through the real broker against a real persistent profile.

Skipped unless REAL_BROWSER_NODE_TESTS=1 (needs node, `npm ci` in
browser-node/, Playwright's Chromium). Browsers run headless
(PERSISTENT_PROFILE_HEADLESS) so nothing opens on the machine's desktop.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "browser-node" / "server.mjs"
TOKEN = "e2e-profile-token-" + "p" * 32
CONTROLLER_TOKEN = "e2e-controller-token-" + "c" * 32
OWNER, AGENT = "o" * 40, "a" * 40
BASE = 19420


def _lan() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


LAN = _lan()

FIXTURE = b"""<!doctype html><html><head><title>Shop form</title></head><body>
<h1 id="h">Order form</h1>
<label>Name <input id="name" name="name" aria-label="Name"></label>
<label>Color <select id="color" aria-label="Color"><option value="red">Red</option><option value="blue">Blue</option></select></label>
<button id="go" onclick="document.getElementById('out').textContent='sent:'+document.getElementById('name').value+':'+document.getElementById('color').value">Send</button>
<input id="file" type="file" aria-label="Photo">
<p id="out">nothing yet</p>
<a id="next" href="/page2">Next page</a>
<div style="height:3000px">tall</div>
</body></html>"""
PAGE2 = b"<!doctype html><html><head><title>Page two</title></head><body><p>second page</p></body></html>"


class _Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGE2 if self.path.startswith("/page2") else FIXTURE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get("REAL_BROWSER_NODE_TESTS") == "1" and LAN, "real browser-node test disabled")
class BrokerEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="e2e-broker-"))
        cls.fixture = http.server.ThreadingHTTPServer((LAN, 0), _Fixture)
        threading.Thread(target=cls.fixture.serve_forever, daemon=True).start()
        cls.site = f"http://{LAN}:{cls.fixture.server_address[1]}"
        endpoint_file = cls.tmp / "browser-profile" / "ws.txt"
        node_env = {
            **os.environ,
            "PERSISTENT_PROFILES_ENABLED": "true",
            "PROFILE_CONTROL_TOKEN": TOKEN,
            "PROFILE_CONTROL_PORT": str(BASE + 4),
            "PROFILE_CDP_RELAY_PORT": str(BASE + 5),
            "PROFILE_CDP_RELAY_ADVERTISED_HOST": LAN,
            "PLAYWRIGHT_SERVER_HOST": "0.0.0.0",
            "PLAYWRIGHT_SERVER_PORT": str(BASE + 3),
            "PLAYWRIGHT_SERVER_ADVERTISED_HOST": LAN,
            "BROWSER_WS_ENDPOINT_FILE": str(endpoint_file),
            "BROWSER_PROFILES_ROOT": str(cls.tmp / "browser-profiles"),
            "BROWSER_DOWNLOADS_DIR": str(cls.tmp / "downloads"),
            "PERSISTENT_PROFILE_HEADLESS": "0" if os.environ.get("REAL_BROWSER_TESTS_HEADED") == "1" else "1",
        }
        cls.node = subprocess.Popen(
            ["node", str(SERVER)], cwd=str(SERVER.parent), env=node_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 60
        while not endpoint_file.exists():
            if time.time() > deadline or cls.node.poll() is not None:
                raise RuntimeError("browser-node did not start")
            time.sleep(0.25)
        data = cls.tmp / "data"
        controller_env = {
            **os.environ,
            "API_BEARER_TOKENS": f"tenant:{CONTROLLER_TOKEN}",
            "REQUIRE_OPERATOR_ID": "true",
            "ALLOWED_HOSTS": "*",
            "SESSION_ISOLATION_MODE": "shared_browser_node",
            "MAX_SESSIONS": "1",
            "PERSISTENT_PROFILES_ENABLED": "true",
            "BROWSER_NODE_HOST": LAN,
            "PROFILE_CONTROL_PORT": str(BASE + 4),
            "PROFILE_CONTROL_TOKEN": TOKEN,
            "BROWSER_WS_ENDPOINT_FILE": str(endpoint_file),
            "BROWSER_PROFILES_ROOT": str(cls.tmp / "browser-profiles"),
            "PERCEPTION_PRESET_DEFAULT": "text",
            "PII_SCRUB_SCREENSHOT": "false",
            "OCR_ENABLED": "false",
            "WITNESS_ENABLED": "false",
            "METRICS_ENABLED": "false",
            "REQUEST_RATE_LIMIT_ENABLED": "false",
            "ARTIFACT_ROOT": str(data / "artifacts"),
            "UPLOAD_ROOT": str(data / "uploads"),
            "AUTH_ROOT": str(data / "auth"),
            "APPROVAL_ROOT": str(data / "approvals"),
            "AUDIT_ROOT": str(data / "audit"),
            "WITNESS_ROOT": str(data / "witness"),
            "SESSION_STORE_ROOT": str(data / "sessions"),
            "STATE_DB_PATH": str(data / "db" / "operator.db"),
            "REMOTE_ACCESS_INFO_PATH": str(data / "tunnels" / "reverse-ssh.json"),
            "MEMORY_ROOT": str(data / "memory"),
            "LOG_LEVEL": "WARNING",
        }
        cls.controller_port = BASE + 10
        cls.controller_log = open(cls.tmp / "controller.log", "w", encoding="utf-8")  # noqa: SIM115
        cls.controller = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(cls.controller_port)],
            cwd=os.environ.get("E2E_CONTROLLER_DIR", str(REPO / "controller")), env=controller_env,
            stdout=cls.controller_log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 90
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.controller_port}/healthz", timeout=2)
                break
            except Exception:
                if time.time() > deadline or cls.controller.poll() is not None:
                    raise RuntimeError(
                        "controller did not start:\n" + (cls.tmp / "controller.log").read_text(encoding="utf-8")[-3000:]
                    ) from None
                time.sleep(0.5)

    @classmethod
    def tearDownClass(cls) -> None:
        for proc in (getattr(cls, "controller", None), getattr(cls, "node", None)):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        cls.fixture.shutdown()
        cls.controller_log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_employee_operation_through_the_broker(self) -> None:
        sys.path.insert(0, str(REPO))
        from approval_broker.app import create_app, totp_code
        from fastapi.testclient import TestClient

        private = self.tmp / "broker"
        private.mkdir(mode=0o700, exist_ok=True)
        import httpx

        port = self.controller_port

        class ToTestController(httpx.AsyncHTTPTransport):
            """The broker only accepts its private endpoint (127.0.0.1:8000);
            send those requests to this test's controller port instead."""

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                request.url = request.url.copy_with(port=port)
                return await super().handle_async_request(request)

        broker = create_app(
            owner_token=OWNER,
            agent_token=AGENT,
            upstream_token=CONTROLLER_TOKEN,
            upstream_url="http://127.0.0.1:8000",
            transport=ToTestController(),
            totp_db_path=private / "totp.sqlite3",
        )
        owner = {"Authorization": f"Bearer {OWNER}"}
        agent = {"Authorization": f"Bearer {AGENT}"}
        (self.tmp / "data" / "uploads").mkdir(parents=True, exist_ok=True)
        with TestClient(broker) as client:
            secret = client.post("/owner/totp/setup", headers=owner).json()["secret"]
            now = int(time.time() // 30)
            assert client.post("/owner/totp/activate", headers=owner, json={"totp_code": totp_code(secret, now)}).status_code == 200
            opened = client.post(
                "/owner/sessions", headers=owner,
                json={"start_url": self.site + "/", "totp_code": totp_code(secret, now + 1)},
            )
            self.assertEqual(opened.status_code, 200, opened.text)
            request_id = client.post("/requests", headers=agent, json={"purpose": "e2e"}).json()["id"]

            timings: dict[str, float] = {}

            def act(name: str, arguments: dict | None = None, *, ok: tuple[int, ...] = (200,)) -> dict:
                started = time.monotonic()
                if name == "observe":
                    response = client.get(f"/requests/{request_id}/observe", headers=agent)
                else:
                    response = client.post(
                        f"/requests/{request_id}/actions/{name}", headers=agent, json={"arguments": arguments or {}}
                    )
                timings[name] = round(time.monotonic() - started, 2)
                self.assertIn(response.status_code, ok, f"{name}: {response.status_code} {response.text[:500]}")
                return response.json() if response.content else {}

            first = act("observe")
            self.assertEqual(first["title"], "Shop form")
            by_label = {item.get("label") or item.get("name") or item.get("text"): item for item in first["interactables"]}
            name_box = next(i for i in first["interactables"] if "Name" in str(i.values()))
            color_box = next(i for i in first["interactables"] if "Color" in str(i.values()))
            send_button = next(i for i in first["interactables"] if "Send" in str(i.values()))
            self.assertTrue(by_label)

            act("type", {"element_id": name_box["element_id"], "text": "Abu Abdullah", "pace": "fast"})
            act("select_option", {"element_id": color_box["element_id"], "value": "blue"})
            act("hover", {"element_id": send_button["element_id"]})
            act("click", {"element_id": send_button["element_id"], "pace": "fast"})
            self.assertIn("sent:Abu Abdullah:blue", act("observe")["text_excerpt"])
            act("press", {"key": "Tab"})
            act("scroll", {"delta_y": 800, "pace": "fast"})
            act("wait", {"wait_ms": 200})
            act("navigate", {"url": self.site + "/page2"})
            self.assertEqual(act("observe")["title"], "Page two")
            act("go_back")
            self.assertEqual(act("observe")["title"], "Shop form")
            act("go_forward")
            act("reload")
            self.assertEqual(act("observe")["title"], "Page two")
            opened_tab = act("open_tab", {"url": self.site + "/", "activate": True})
            self.assertEqual(len(opened_tab["tabs"]), 2)
            tabs = act("list_tabs")
            self.assertEqual(len(tabs), 2)
            act("activate_tab", {"index": 0})
            # Upload is a sensitive action: an employee gets a structured
            # refusal/approval request back -- never a hang or a 5xx.
            (self.tmp / "data" / "uploads" / "photo.txt").write_text("x", encoding="utf-8")
            act("upload", {"selector": "#file", "file_path": "photo.txt"}, ok=(200, 400, 403, 409, 422))
            final = act("observe")
            self.assertEqual(final["session"]["status"], "active")
            slowest = max(timings.values())
            self.assertLess(slowest, 20, f"an operation took {slowest}s: {timings}")
            print("broker e2e timings (s):", timings)


if __name__ == "__main__":
    unittest.main()
