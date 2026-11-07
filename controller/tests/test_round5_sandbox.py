"""1.14.0 round 5: the state's trust edges, the seccomp profile read, and CI's gate-check script.

- D3: in shared mode with no endpoint file, only a "refused" state is taken from the state file; anything else (a
  stale or forged "on") is "has not reported its sandbox yet".
- D6: the seccomp profile is read like the state files: no links, no FIFO, a regular file under a size cap.
- S4#6, D13: format characters (bidi, zero-width, line and paragraph separators) and C1 are stripped, method too.
- S4#7: no "(None)" in readiness lines, and a detail keeps its own trailing colon.
- S4#10: scripts/check_sandbox_gate.py exits 1 on a wrong status and on a state naming another endpoint.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.browser_sandbox import (
    BROWSER_SANDBOX_STATE_FILENAME,
    SECCOMP_PROFILE_PATH,
    _Line,
    browser_sandbox_status,
    load_seccomp_profile,
    read_sandbox_state,
)

REPO = Path(__file__).resolve().parents[2]
SECRET_PATH = "0123456789abcdef0123456789abcdef"
EVIDENCE = {
    "renderers": 1,
    "renderer_userns_differs": True,
    "browser_seccomp_filters": 2,
    "renderer_seccomp_filters": [3],
}


class NoEndpointTests(unittest.TestCase):
    def test_only_a_refusal_is_taken_without_an_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_file = Path(tempdir) / BROWSER_SANDBOX_STATE_FILENAME
            settings = SimpleNamespace(
                browser_sandbox="on",
                session_isolation_mode="shared_browser_node",
                browser_ws_endpoint_file=str(Path(tempdir) / "browser-ws-endpoint.txt"),
                isolated_browser_seccomp_profile=str(SECCOMP_PROFILE_PATH),
            )
            state_file.write_text(json.dumps({"sandbox": "on", "verified": EVIDENCE}), encoding="utf-8")
            self.assertEqual(browser_sandbox_status(settings)["browser_node"]["sandbox"], "unknown")
            state_file.write_text(json.dumps({"sandbox": "refused", "cause": "userns_refused"}), encoding="utf-8")
            self.assertEqual(browser_sandbox_status(settings)["browser_node"]["sandbox"], "refused")


class ProfileReadTests(unittest.TestCase):
    def test_the_shipped_profile_loads(self) -> None:
        self.assertIn('"syscalls"', load_seccomp_profile(SECCOMP_PROFILE_PATH))

    def test_a_directory_or_an_oversized_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaises((OSError, ValueError)):
                load_seccomp_profile(tempdir)
            big = Path(tempdir) / "big.json"
            big.write_bytes(b" " * ((1 << 20) + 1))
            with self.assertRaisesRegex(ValueError, "larger than"):
                load_seccomp_profile(big)

    @unittest.skipUnless(hasattr(os, "mkfifo") and sys.platform == "linux", "Linux")
    def test_a_fifo_does_not_block_and_a_symlink_to_a_file_loads(self) -> None:
        # Item 5: the operator's path follows links (a ConfigMap mount); a FIFO is still refused, and never blocks.
        with tempfile.TemporaryDirectory() as tempdir:
            fifo = Path(tempdir) / "profile.json"
            os.mkfifo(fifo)
            outcome: list = []

            def load_fifo() -> None:
                try:
                    load_seccomp_profile(fifo)
                    outcome.append("loaded")
                except (OSError, ValueError) as exc:
                    outcome.append(exc)

            worker = threading.Thread(target=load_fifo, daemon=True)
            worker.start()
            worker.join(5)
            self.assertFalse(worker.is_alive(), "load_seccomp_profile blocked on a FIFO")
            self.assertIsInstance(outcome[0], ValueError)
            self.assertIn("not a regular file", str(outcome[0]))
            link = Path(tempdir) / "link.json"
            link.symlink_to(SECCOMP_PROFILE_PATH)
            self.assertIn('"syscalls"', load_seccomp_profile(link))


class PrintableTests(unittest.TestCase):
    def test_c1_and_format_characters_are_stripped_method_too(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / BROWSER_SANDBOX_STATE_FILENAME
            raw = {
                "sandbox": "off",
                "detail": "a\x7fb\x85c‮d​e f g",
                "verified": {"method": "proc\nFORGED\x1b[31m‮"},
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            state = read_sandbox_state(path)
        self.assertEqual(state["detail"], "a b c d e f g")
        self.assertEqual(state["verified"]["method"], "proc FORGED [31m ")


class DescribeTests(unittest.TestCase):
    def test_no_none_and_a_trailing_colon_kept(self) -> None:
        self.assertEqual(_Line("browser-node", {"sandbox": "off"}).describe(), "browser-node: sandbox off")
        self.assertEqual(
            _Line("browser-node", {"sandbox": "refused", "detail": "x"}).describe(), "browser-node: sandbox refused: x"
        )
        self.assertEqual(
            _Line("browser-node", {"sandbox": "off", "cause": "c", "detail": "ends with colon:"}).describe(),
            "browser-node: sandbox off (c): ends with colon:",
        )


def _load_gate_script():
    spec = importlib.util.spec_from_file_location(
        "check_sandbox_gate_under_test", REPO / "scripts" / "check_sandbox_gate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Api(BaseHTTPRequestHandler):
    status = 200
    delete_status = 200
    error_detail = "x"

    def log_message(self, *args) -> None:
        return None

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # /healthz
        self._send(200, {"ok": True})

    def _drain(self) -> None:
        # Read the request body before answering: an unread body can reset the connection under the client (S5#2).
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)

    def do_POST(self) -> None:  # /sessions
        self._drain()
        self._send(type(self).status, {"id": "s1"} if type(self).status == 200 else {"detail": type(self).error_detail})

    def do_DELETE(self) -> None:
        self._drain()
        self._send(type(self).delete_status, {})


@unittest.skipUnless((REPO / "scripts").is_dir(), "scripts/ is not in the controller image")
class GateScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_gate_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "api-token").write_text("t", encoding="utf-8")
        (self.data / "browser-profile").mkdir()

    def _run(self, status: int, *args: str, delete_status: int = 200, env: dict | None = None) -> int:
        handler = type("Api", (_Api,), {"status": status, "delete_status": delete_status})
        return self._run_with(handler, *args, env=env)

    def _run_with(self, handler: type, *args: str, env: dict | None = None) -> int:
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        argv = ["x", "--api-port", str(server.server_address[1]), "--data", str(self.data), *args]
        printed = io.StringIO()
        with redirect_stdout(printed), patch.dict(os.environ, env or {}):
            code = self.module.main(argv)
        self.output = printed.getvalue()
        return code

    def _files(self, state_endpoint: str, file_endpoint: str) -> None:
        profile = self.data / "browser-profile"
        (profile / "browser-sandbox.json").write_text(json.dumps({"endpoint": state_endpoint}), encoding="utf-8")
        (profile / "browser-ws-endpoint.txt").write_text(file_endpoint, encoding="utf-8")

    def test_w9_6_a_redirect_is_reported_and_not_followed(self) -> None:
        # urllib's default handler copied the Authorization header onto a redirect to any origin.
        seen: list = []

        class Elsewhere(_Api):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self._send(200, {"id": "s1"})

            do_POST = do_GET

        elsewhere = HTTPServer(("127.0.0.1", 0), Elsewhere)
        threading.Thread(target=elsewhere.serve_forever, daemon=True).start()
        self.addCleanup(elsewhere.server_close)
        self.addCleanup(elsewhere.shutdown)
        location = f"http://127.0.0.1:{elsewhere.server_address[1]}/stolen"

        class Redirecting(_Api):
            def do_POST(self) -> None:
                self._drain()
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("content-length", "0")
                self.end_headers()

        self.assertEqual(self._run_with(Redirecting, "--expect", "200"), 1)
        self.assertIn("create session: HTTP 302", self.output)
        self.assertEqual(seen, [])

    def test_w9_6_no_request_goes_through_an_environment_proxy(self) -> None:
        seen: list = []

        class Proxy(_Api):
            def do_GET(self) -> None:
                seen.append(self.path)
                self._send(200, {"ok": True})

            do_POST = do_DELETE = do_GET

        proxy = HTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        names = ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")
        with patch.dict(os.environ, dict.fromkeys(names, proxy_url)):
            for name in ("no_proxy", "NO_PROXY"):
                os.environ.pop(name, None)
            # Loaded with the proxy set, as an operator's shell would have it from the start.
            self.module = _load_gate_script()
            self._files("ws://a/1", "ws://a/1")
            self.assertEqual(self._run(200, "--expect", "200"), 0)
        self.assertEqual(seen, [])

    def test_200_with_matching_endpoints_passes_and_mismatched_fails(self) -> None:
        self._files("ws://a/1", "ws://a/1")
        self.assertEqual(self._run(200, "--expect", "200"), 0)
        self._files("ws://a/1", "ws://a/2")
        self.assertEqual(self._run(200, "--expect", "200"), 1)

    def test_a_mismatch_names_the_endpoints_without_their_path(self) -> None:
        self._files(f"ws://browser-node:9223/{SECRET_PATH}", f"ws://browser-node:9223/{SECRET_PATH}-older")

        self.assertEqual(self._run(200, "--expect", "200"), 1)

        self.assertIn("FAIL: the state names", self.output)
        self.assertNotIn(SECRET_PATH, self.output)
        self.assertEqual(self.output.count("ws://browser-node:9223/<redacted>"), 2, self.output)

    def test_an_isolated_failure_prints_the_containers_detail_without_the_connect_path(self) -> None:
        state = self.data / "browser-sessions" / "s1" / "profile" / "browser-sandbox.json"
        state.parent.mkdir(parents=True)
        detail = f"the sandbox check failed: WebSocket error: ws://browser-node:9223/{SECRET_PATH} 502"
        state.write_text(
            json.dumps({"setting": "on", "sandbox": "refused", "cause": "not_verified", "detail": detail}),
            encoding="utf-8",
        )

        self.assertEqual(self._run(200, "--expect", "200", "--isolated"), 1)

        self.assertIn("FAIL: the isolated container's state", self.output)
        self.assertNotIn(SECRET_PATH, self.output)
        self.assertIn("ws://browser-node:9223/<redacted>", self.output)

    def test_an_unexpected_status_prints_the_body_without_the_connect_path(self) -> None:
        detail = f"connect to ws://browser-node:9223/{SECRET_PATH} failed"
        handler = type("Api", (_Api,), {"status": 500, "error_detail": detail})

        self.assertEqual(self._run_with(handler, "--expect", "409"), 1)

        self.assertIn("got 500", self.output)
        self.assertNotIn(SECRET_PATH, self.output)
        self.assertIn("ws://browser-node:9223/<redacted>", self.output)

    def test_isolated_needs_the_containers_own_verified_state(self) -> None:
        state = self.data / "browser-sessions" / "s1" / "profile" / "browser-sandbox.json"
        state.parent.mkdir(parents=True)
        verified = {"setting": "on", "sandbox": "on", "verified": EVIDENCE}
        state.write_text(json.dumps(verified), encoding="utf-8")
        self.assertEqual(self._run(200, "--expect", "200", "--isolated"), 0)
        state.write_text(json.dumps({**verified, "verified": {"renderers": 1}}), encoding="utf-8")
        self.assertEqual(self._run(200, "--expect", "200", "--isolated"), 1)

    def test_isolated_refuses_chrome_sandbox_evidence(self) -> None:
        # GS4: --isolated applies --require-proc: the old-kernel evidence is not enough on CI's runners.
        state = self.data / "browser-sessions" / "s1" / "profile" / "browser-sandbox.json"
        state.parent.mkdir(parents=True)
        page = {"renderers": 1, "renderer_userns_differs": True, "method": "chrome://sandbox"}
        state.write_text(json.dumps({"setting": "on", "sandbox": "on", "verified": page}), encoding="utf-8")
        self.assertEqual(self._run(200, "--expect", "200", "--isolated"), 1)

    def test_a_close_that_fails_fails(self) -> None:
        # GS5: the DELETE's status counts.
        self._files("ws://a/1", "ws://a/1")
        self.assertEqual(self._run(200, "--expect", "200", delete_status=500), 1)

    def test_an_isolated_close_that_fails_fails(self) -> None:
        # GS6: under --isolated the DELETE's status counts too, next to a verified state.
        state = self.data / "browser-sessions" / "s1" / "profile" / "browser-sandbox.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"setting": "on", "sandbox": "on", "verified": EVIDENCE}), encoding="utf-8")
        self.assertEqual(self._run(200, "--expect", "200", "--isolated"), 0)
        self.assertEqual(self._run(200, "--expect", "200", "--isolated", delete_status=500), 1)

    def test_log_must_say_reads_the_controller_log(self) -> None:
        # GS3/GS6: a stub `docker` on PATH stands in for `docker compose logs controller`; S6#5: it writes the
        # arguments it was given to a file.
        bin_dir = self.data / "bin"
        bin_dir.mkdir()
        argv_file = self.data / "docker-argv.txt"
        if os.name == "nt":
            (bin_dir / "docker.cmd").write_text(
                f'@echo off\r\necho %*> "{argv_file}"\r\necho WARNING the gate said no\r\n', encoding="utf-8"
            )
        else:
            stub = bin_dir / "docker"
            stub.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_file}'\necho 'WARNING the gate said no'\n", encoding="utf-8"
            )
            stub.chmod(0o755)
        path = {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}
        began = datetime.now(UTC).replace(microsecond=0)
        self.assertEqual(self._run(409, "--expect", "409", "--log-must-say", "the gate said no", env=path), 0)
        ended = datetime.now(UTC)
        # S5#12, S6#5: only the lines logged since the check began, by an RFC3339 UTC time taken during the check.
        words = argv_file.read_text(encoding="utf-8").split()
        self.assertEqual(words[:3], ["compose", "logs", "--since"], words)
        self.assertRegex(words[3], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        since = datetime.strptime(words[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self.assertTrue(began <= since <= ended, (began, since, ended))
        self.assertEqual(words[4:], ["controller"], words)
        self.assertEqual(self._run(409, "--expect", "409", "--log-must-say", "something else", env=path), 1)

    def test_forty_runs_in_a_row(self) -> None:
        # Item 18: the race the reviewer saw (an unread body) is gone.
        self._files("ws://a/1", "ws://a/1")
        failures = sum(self._run(200, "--expect", "200") != 0 for _ in range(40))
        self.assertEqual(failures, 0)

    def test_a_wrong_status_fails(self) -> None:
        self.assertEqual(self._run(500, "--expect", "409"), 1)
        self.assertEqual(self._run(409, "--expect", "409"), 0)
        self.assertEqual(self._run(409, "--expect", "200"), 1)


@unittest.skipUnless((REPO / "browser-node").is_dir(), "browser-node/ is not in the controller image")
class BrowserNodeWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = (REPO / "browser-node" / "server.mjs").read_text(encoding="utf-8")
        self.entrypoint = (REPO / "browser-node" / "entrypoint.sh").read_text(encoding="utf-8")

    def test_the_endpoint_file_holds_the_advertised_string(self) -> None:
        # N9: the endpoint file and the state's endpoint are the same string, advertised(browserServer).
        self.assertRegex(self.server, r"(?m)^const advertisedEndpoint = advertised\(browserServer\);$")
        self.assertIn("await writeFile(tmpFile, advertisedEndpoint, ", self.server)
        self.assertIn("endpoint: advertised(browserServer) }", self.server)

    def test_make_verify_is_sandbox_mjs_own(self) -> None:
        # R2d: an import alias or a local makeVerify could wrap the verdict; server.mjs imports it plainly.
        start = self.server.index("import {\n  declaredProfile,")
        imports = self.server[start : self.server.index('} from "./sandbox.mjs";')]
        self.assertRegex(imports, r"(?m)^  makeVerify,$")
        self.assertNotIn(" as ", imports)
        self.assertNotRegex(self.server, r"(const|let|var|function)\s+makeVerify\b")

    def test_a_directory_named_like_the_endpoint_or_state_file_is_removed(self) -> None:
        # D11: rm -f fails on a directory of that name, and browser-node could then never start again. The
        # entrypoint's own line, run under bash against planted directories.
        from tests.test_scripts_token_argv import BASH, _bash_path

        line = next(
            line for line in self.entrypoint.splitlines() if '"$WS_ENDPOINT_FILE" "$SANDBOX_STATE_FILE"' in line
        )
        self.assertTrue(line.startswith("rm -rf -- "), line)
        if not BASH:
            self.skipTest("no bash")
        with tempfile.TemporaryDirectory() as tempdir:
            endpoint = Path(tempdir) / "browser-ws-endpoint.txt"
            state = Path(tempdir) / "browser-sandbox.json"
            (endpoint / "inner").mkdir(parents=True)
            state.mkdir()
            script = (
                f'set -euo pipefail; WS_ENDPOINT_FILE="{_bash_path(endpoint)}"; '
                f'SANDBOX_STATE_FILE="{_bash_path(state)}"; {line}'
            )
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(endpoint.exists() or state.exists())


@unittest.skipUnless((REPO / "scripts" / "assert_browser_sandbox.py").is_file(), "scripts/ is not in the image")
class EndpointRedactionTests(unittest.TestCase):
    """The Playwright connect path is browser-node's only secret, and CI logs are public: nothing CI runs prints it."""

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "assert_browser_sandbox_under_test", REPO / "scripts" / "assert_browser_sandbox.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_redact_endpoints_is_the_twin_of_the_node_helper(self) -> None:
        redact = self.module.redact_endpoints
        state = json.dumps({"sandbox": "on", "endpoint": f"ws://browser-node:9223/{SECRET_PATH}"})
        out = redact(f"state: {state}; retry ws://127.0.0.1:9223/{SECRET_PATH}?x=1 now")

        self.assertNotIn(SECRET_PATH, out)
        self.assertIn("ws://browser-node:9223/<redacted>", out)
        self.assertIn("ws://127.0.0.1:9223/<redacted>", out)
        failure = f"browserType.connect: WebSocket error: wss://h.example:443/{SECRET_PATH} 502"
        self.assertEqual(redact(failure), "browserType.connect: WebSocket error: wss://h.example:443/<redacted> 502")
        self.assertEqual(redact("no endpoint here"), "no endpoint here")
        self.assertEqual(redact(None), "None")

    def test_the_assertion_prints_the_state_and_its_failures_without_the_connect_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state = Path(tempdir) / "browser-sandbox.json"
            detail = f"the sandbox check failed: WebSocket error: ws://browser-node:9223/{SECRET_PATH} 502"
            state.write_text(
                json.dumps(
                    {
                        "setting": "on",
                        "sandbox": "refused",
                        "cause": "not_verified",
                        "detail": detail,
                        "endpoint": f"ws://browser-node:9223/{SECRET_PATH}",
                    }
                ),
                encoding="utf-8",
            )
            printed = io.StringIO()
            with redirect_stdout(printed):
                code = self.module.main(["assert_browser_sandbox.py", str(state), "--require-proc"])

        self.assertEqual(code, 1)
        self.assertIn("FAIL: sandbox is 'refused'", printed.getvalue())
        self.assertNotIn(SECRET_PATH, printed.getvalue())
        self.assertGreaterEqual(printed.getvalue().count("ws://browser-node:9223/<redacted>"), 2)


CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


@unittest.skipUnless(CI_WORKFLOW.is_file(), ".github/workflows/ci.yml is not in the controller image")
class CiSandboxStepsRedactionTests(unittest.TestCase):
    def test_no_ci_step_prints_the_state_file_raw(self) -> None:
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        job = ci[ci.index("  compose-smoke:") :]

        self.assertNotRegex(job, r"print\(\s*(s|state)\s*\)")
        self.assertNotIn("got {state}", job)
        # Each inline snippet that reads the state file imports the redaction.
        for snippet in (
            'python3 -c \'import json,sys; sys.path.insert(0,"scripts"); from assert_browser_sandbox import',
        ):
            self.assertIn(snippet, job)
        poll = job[job.index("BROWSER_SANDBOX=on BROWSER_SECCOMP_PROFILE=builtin docker compose up") :]
        self.assertIn("from assert_browser_sandbox import redact_endpoints", poll[: poll.index("PY\n")])
        self.assertIn("print(redact_endpoints(json.dumps(state)))", poll)

    def test_the_ci_comment_says_the_node_tests_need_npm_ci(self) -> None:
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        step = ci[ci.index("Test browser-node's sandbox policy") :]
        step = step[: step.index("working-directory: browser-node")]

        self.assertIn("npm ci", step)
        self.assertNotIn("imports only node: modules.\n", step)


if __name__ == "__main__":
    unittest.main()
