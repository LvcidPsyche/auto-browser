"""Files in and out of the owner's real browser, end to end: approval broker ->
real controller -> real browser-node persistent profile (headless Chromium).

An employee must be able to run a creative pipeline across sites: make a
picture on site A, take it into our media library, put it into site B's
uploader, take B's video back. This drives exactly that through the broker's
file routes with a fake library on the other end:

* a click that starts a real browser download (Content-Disposition)
* an <img> the employee points at, and the page's main video (a blob: URL)
* a cross-origin picture served with CORS (fetched by the page itself) and one
  without CORS (the controller-side fallback must refuse a private address)
* the library pushing those bytes back into a drag-and-drop zone that hides
  its file input, and into a plain file input -- the page proves it got the
  exact bytes
* refusals: a disguised HTML file, an over-cap push, another agent's transfer,
  an unknown transfer, a revoked grant

Skipped unless REAL_BROWSER_NODE_TESTS=1 (needs node, `npm ci` in
browser-node/, Playwright's Chromium). Headless (PERSISTENT_PROFILE_HEADLESS).
"""

from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import quote

from tests.test_broker_e2e_real import LAN, REPO, SERVER

TOKEN = "e2e-files-profile-token-" + "p" * 32
CONTROLLER_TOKEN = "e2e-files-controller-token-" + "c" * 32
OWNER, AGENT, OTHER_AGENT = "o" * 40, "a" * 40, "b" * 40
BASE = 19520

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
PDF = b"%PDF-1.4\n" + b"report-body " * 800 + b"\n%%EOF\n"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(reversed(range(256))) * 60
CORS_PNG = b"\x89PNG\r\n\x1a\n" + b"cors-picture" * 300
NOCORS_PNG = b"\x89PNG\r\n\x1a\n" + b"no-cors-picture" * 300

STUDIO = """<!doctype html><html><head><title>Studio</title></head><body>
<h1>Studio</h1>
<a id="dl" href="/files/report.pdf">Download report</a>
<img id="pic" src="/files/photo.png" alt="Generated photo" width="200" height="120">
<img id="corspic" src="{other}/cors.png" alt="Partner photo" width="50" height="50">
<img id="nocorspic" src="{other}/nocors.png" alt="Locked photo" width="50" height="50">
<video id="vid" aria-label="Result video" width="480" height="270" muted></video>
<div id="drop" role="button" aria-label="Drop files here" style="border:1px dashed;padding:20px"
     onclick="document.getElementById('hidden').click()">Drop files here</div>
<input id="hidden" type="file" accept="image/*" style="display:none">
<label>Attach video <input id="visible" type="file" accept="video/*"></label>
<p id="out">none</p>
<script>
fetch('/files/clip.mp4').then(r => r.blob()).then(b => {{ document.getElementById('vid').src = URL.createObjectURL(b); }});
for (const id of ['hidden', 'visible']) {{
  document.getElementById(id).addEventListener('change', (e) => {{
    const f = e.target.files[0];
    f.arrayBuffer().then((buf) => {{
      let sum = 0; for (const b of new Uint8Array(buf)) sum = (sum * 31 + b) % 1000000007;
      document.getElementById('out').textContent = id + '|' + f.name + '|' + f.size + '|' + sum;
    }});
  }});
}}
</script>
</body></html>"""


def _checksum(data: bytes) -> int:
    total = 0
    for byte in data:
        total = (total * 31 + byte) % 1000000007
    return total


def _handler(pages: dict[str, tuple[bytes, dict[str, str]]]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body, headers = pages.get(self.path.split("?")[0], (b"not found", {"Content-Type": "text/plain"}))
            self.send_response(200 if self.path.split("?")[0] in pages else 404)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


@unittest.skipUnless(os.environ.get("REAL_BROWSER_NODE_TESTS") == "1" and LAN, "real browser-node test disabled")
class FileTransferEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="e2e-files-"))
        other_pages = {
            "/cors.png": (CORS_PNG, {"Content-Type": "image/png", "Access-Control-Allow-Origin": "*"}),
            "/nocors.png": (NOCORS_PNG, {"Content-Type": "image/png"}),
        }
        cls.other = http.server.ThreadingHTTPServer((LAN, 0), _handler(other_pages))
        threading.Thread(target=cls.other.serve_forever, daemon=True).start()
        other_origin = f"http://{LAN}:{cls.other.server_address[1]}"
        pages = {
            "/": (STUDIO.format(other=other_origin).encode(), {"Content-Type": "text/html"}),
            "/files/report.pdf": (
                PDF, {"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="report.pdf"'},
            ),
            "/files/photo.png": (PNG, {"Content-Type": "image/png"}),
            "/files/clip.mp4": (MP4, {"Content-Type": "video/mp4"}),
        }
        cls.fixture = http.server.ThreadingHTTPServer((LAN, 0), _handler(pages))
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
        cls.other.shutdown()
        cls.controller_log.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_download_to_library_and_upload_back(self) -> None:
        sys.path.insert(0, str(REPO))
        import httpx
        from approval_broker.app import create_app, totp_code
        from fastapi.testclient import TestClient

        private = self.tmp / "broker"
        private.mkdir(mode=0o700, exist_ok=True)
        port = self.controller_port

        class ToTestController(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                request.url = request.url.copy_with(port=port)
                return await super().handle_async_request(request)

        cap = 64 * 1024  # small broker cap so the over-cap refusal is cheap to prove
        broker = create_app(
            owner_token=OWNER,
            agent_tokens=f"emad:{AGENT},other:{OTHER_AGENT}",
            upstream_token=CONTROLLER_TOKEN,
            upstream_url="http://127.0.0.1:8000",
            transport=ToTestController(),
            totp_db_path=private / "totp.sqlite3",
            transfer_max_bytes=cap,
        )
        owner = {"Authorization": f"Bearer {OWNER}"}
        agent = {"Authorization": f"Bearer {AGENT}"}
        other = {"Authorization": f"Bearer {OTHER_AGENT}"}
        library: dict[str, tuple[bytes, str, str]] = {}  # the fake media library

        with TestClient(broker) as client:
            secret = client.post("/owner/totp/setup", headers=owner).json()["secret"]
            now = int(time.time() // 30)
            assert client.post(
                "/owner/totp/activate", headers=owner, json={"totp_code": totp_code(secret, now)}
            ).status_code == 200
            opened = client.post(
                "/owner/sessions", headers=owner,
                json={"start_url": self.site + "/", "totp_code": totp_code(secret, now + 1)},
            )
            self.assertEqual(opened.status_code, 200, opened.text)
            request_id = client.post("/requests", headers=agent, json={"purpose": "e2e files"}).json()["id"]
            other_id = client.post("/requests", headers=other, json={"purpose": "other"}).json()["id"]

            def act(name: str, arguments: dict, *, headers=agent, grant=None, ok=(200,)) -> dict:
                response = client.post(
                    f"/requests/{grant or request_id}/actions/{name}", headers=headers, json={"arguments": arguments},
                )
                self.assertIn(response.status_code, ok, f"{name}: {response.status_code} {response.text[:600]}")
                return response.json() if response.content else {}

            def to_library(transfer: dict, label: str) -> bytes:
                pulled = client.get(f"/requests/{request_id}/files/{transfer['id']}", headers=agent)
                self.assertEqual(pulled.status_code, 200, pulled.text[:300])
                self.assertEqual(pulled.headers["content-type"], transfer["mime_type"])
                self.assertEqual(pulled.headers.get("x-content-type-options"), "nosniff")
                self.assertTrue(pulled.headers["content-disposition"].startswith("attachment"))
                self.assertEqual(hashlib.sha256(pulled.content).hexdigest(), transfer["sha256"])
                library[label] = (pulled.content, transfer["mime_type"], transfer["filename"])
                dropped = client.delete(f"/requests/{request_id}/files/{transfer['id']}", headers=agent)
                self.assertEqual(dropped.status_code, 200, dropped.text)
                return pulled.content

            observed = client.get(f"/requests/{request_id}/observe", headers=agent).json()
            self.assertEqual(observed["title"], "Studio")
            link = next(i for i in observed["interactables"] if "Download report" in str(i.values()))

            # 1. a click that starts a real browser download
            pdf = act("download_file", {"mode": "click", "element_id": link["element_id"], "pace": "fast"})
            self.assertEqual((pdf["mime_type"], pdf["kind"], pdf["size_bytes"]), ("application/pdf", "pdf", len(PDF)))
            self.assertEqual(pdf["filename"], "report.pdf")
            self.assertEqual(to_library(pdf, "report"), PDF)

            # 2. the picture an employee points at
            pic = act("download_file", {"mode": "element", "selector": "#pic"})
            self.assertEqual((pic["mime_type"], pic["size_bytes"]), ("image/png", len(PNG)))
            self.assertEqual(to_library(pic, "photo"), PNG)

            # 3. the page's main video, a blob: URL the page built itself
            clip = act("download_file", {"mode": "media", "media_kind": "video"})
            self.assertEqual((clip["mime_type"], clip["kind"]), ("video/mp4", "video"))
            self.assertEqual(to_library(clip, "clip"), MP4)

            # 4. another site's picture: with CORS the page fetches it itself...
            partner = act("download_file", {"mode": "element", "selector": "#corspic"})
            self.assertEqual(to_library(partner, "partner"), CORS_PNG)
            # ...without CORS the controller would fetch it -- never a private address.
            blocked = act("download_file", {"mode": "element", "selector": "#nocorspic"}, ok=(403,))
            self.assertEqual(blocked["detail"]["code"], "file_fetch_blocked")

            # 5. transfers belong to the agent that made them
            again = act("download_file", {"mode": "element", "selector": "#pic"})
            self.assertEqual(client.get(f"/requests/{other_id}/files/{again['id']}", headers=other).status_code, 404)
            self.assertEqual(client.get(f"/requests/{request_id}/files/{again['id']}", headers=other).status_code, 404)
            self.assertEqual(client.get(f"/requests/{request_id}/files/{'0' * 24}", headers=agent).status_code, 404)
            self.assertEqual(client.get(f"/requests/{request_id}/files/{again['id']}").status_code, 401)

            # 6. the library pushes the picture into a drop zone that hides its input
            data, mime, name = library["photo"]
            pushed = client.put(
                f"/requests/{request_id}/files", headers={**agent, "Content-Type": mime, "X-File-Name": quote(name)},
                content=data,
            )
            self.assertEqual(pushed.status_code, 200, pushed.text)
            drop = next(i for i in observed["interactables"] if "Drop files here" in str(i.values()))
            attached = act("upload_file", {"transfer_id": pushed.json()["id"], "element_id": drop["element_id"]})
            self.assertIn(attached["via"], {"file_chooser", "nearby_input"})
            deadline = time.time() + 10
            while True:
                out = client.get(f"/requests/{request_id}/observe", headers=agent).json()["text_excerpt"]
                if "hidden|" in out or time.time() > deadline:
                    break
                time.sleep(0.3)
            self.assertIn(f"hidden|{name}|{len(data)}|{_checksum(data)}", out)

            # ...and the video into the plain file input, found without any target
            data, mime, name = library["clip"]
            pushed = client.put(
                f"/requests/{request_id}/files", headers={**agent, "Content-Type": mime, "X-File-Name": quote("نتيجة.mp4")},
                content=data,
            )
            self.assertEqual(pushed.status_code, 200, pushed.text)
            self.assertEqual(pushed.json()["filename"], "نتيجة.mp4")
            attached = act("upload_file", {"transfer_id": pushed.json()["id"]})
            self.assertEqual(attached["via"], "page_input")
            deadline = time.time() + 10
            while True:
                out = client.get(f"/requests/{request_id}/observe", headers=agent).json()["text_excerpt"]
                if "visible|" in out or time.time() > deadline:
                    break
                time.sleep(0.3)
            self.assertIn(f"visible|نتيجة.mp4|{len(data)}|{_checksum(data)}", out)

            # 7. refusals: a disguised web page, an over-cap file, a missing length,
            #    and another agent attaching this agent's upload
            disguised = client.put(
                f"/requests/{request_id}/files",
                headers={**agent, "Content-Type": "image/png", "X-File-Name": "x.png"},
                content=b"<html><script>alert(1)</script></html>",
            )
            self.assertEqual(disguised.status_code, 415, disguised.text)
            self.assertEqual(disguised.json()["detail"]["code"], "file_unsupported_type")
            too_big = client.put(
                f"/requests/{request_id}/files", headers={**agent, "Content-Type": "video/mp4"},
                content=MP4 * 10,
            )
            self.assertEqual(too_big.status_code, 413)
            self.assertEqual(
                client.put(f"/requests/{request_id}/files", headers={**agent, "Content-Type": "text/html"},
                           content=b"<p>").status_code, 415,
            )
            act("upload_file", {"transfer_id": pushed.json()["id"]}, headers=other, grant=other_id, ok=(404,))

            # 8. a revoked agent can no longer move files
            client.post(f"/requests/{request_id}/revoke", headers=owner)
            self.assertEqual(client.get(f"/requests/{request_id}/files/{again['id']}", headers=agent).status_code, 403)
            act("download_file", {"mode": "element", "selector": "#pic"}, ok=(403,))


if __name__ == "__main__":
    unittest.main()
