"""The repo's scripts never put the API token on a command line.

bearer_curl (scripts/load_env.sh) and doctor.sh's api_curl passed
-H "Authorization: Bearer <token>" to curl, and smoke_reverse_ssh.sh built the
same header into the `sh -lc` string it hands `docker compose exec`. A command
line is readable by every local user (ps, /proc/<pid>/cmdline), so the token
leaked for as long as each curl ran. The header now reaches curl as a config
read from stdin (`curl --config -`): nothing in argv, nothing on disk.

The dynamic test runs the real bearer_curl in bash against a stand-in curl that
records its argv and stdin. The static test covers every other script, the
Makefile and the workflows. Repo-layout checks: skipped where scripts/ is absent
(the controller image) or bash is missing.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from tests._bash import BASH, path_after  # one resolution of bash for every script test (R5C-27)

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
TOKEN = "tok3n-" + "Q" * 37

# A token-bearing header on a command line: in a script, -H/--header with a
# Bearer value built from a variable, or such a header string assigned for later use.
ARGV_TOKEN = re.compile(r"""(-H|--header)\s+["']?Authorization:\s*Bearer\s+\$|["']Authorization:\s*Bearer\s+\$\{?\w""")
# The token reaches curl one way: as a config piped from curl_auth_config (load_env.sh). curl reads a .curlrc unless its
# first argument is -q (or --disable), and that file, found through CURL_HOME, XDG_CONFIG_HOME or HOME, can add a
# proxy or a header to the request that carries the token. So every use of curl_auth_config must pipe it into a curl
# whose first argument is exactly one of those, however that curl then reads its config. That curl must also pass
# --noproxy (W4-5): curl otherwise sends a loopback request through HTTP_PROXY or ALL_PROXY, headers and all.
TOKEN_USE = re.compile(r"\bcurl_auth_config\b(?!\s*\(\))")  # a use, not the definition
TOKEN_PIPE = re.compile(r"\bcurl_auth_config\s*\|(?P<rest>.*)")
PIPED_CURL = re.compile(r"""\bcurl\s+(?P<first>[^\s"';|&)`]+)""")


def _token_curls_that_read_a_curlrc(text: str) -> list[str]:
    """Each command (continued lines joined) that hands curl_auth_config's output to anything but a curl whose first
    argument is -q or --disable and that passes --noproxy: another curl, a program behind a variable, a process
    substitution, or a curl that would follow the environment's proxy."""
    offenders = []
    for line in re.sub(r"\\\r?\n", " ", text).splitlines():
        if line.lstrip().startswith("#") or not TOKEN_USE.search(line):
            continue
        pipe = TOKEN_PIPE.search(line)
        curl = PIPED_CURL.search(pipe.group("rest")) if pipe else None
        if curl is None or curl.group("first") not in ("-q", "--disable"):
            offenders.append(line.strip())
        elif not re.search(r"\s--noproxy\s", pipe.group("rest")[curl.start() :]):
            offenders.append(line.strip())
    return offenders


def _bash_path(path: Path) -> str:
    """A path bash on this host understands (Git Bash on Windows wants /c/...)."""
    text = path.as_posix()
    if os.name == "nt" and re.match(r"^[A-Za-z]:/", text):
        return f"/{text[0].lower()}{text[2:]}"
    return text


@unittest.skipUnless(SCRIPTS.is_dir() and BASH, "scripts/ or bash is not available here")
class BearerCurlTests(unittest.TestCase):
    def run_bearer_curl(self, *, token: str | None, xtrace: bool = False) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            argv_log, stdin_log = temp / "argv.log", temp / "stdin.log"
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$@" > "{_bash_path(argv_log)}"\n'
                f'cat > "{_bash_path(stdin_log)}"\n'
                "echo ok\n",
                encoding="utf-8",
                newline="\n",
            )
            curl.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if key != "API_BEARER_TOKEN"}
            env["PATH"] = path_after()  # the stub's #!/usr/bin/env bash finds this bash (review of c5)
            if token is not None:
                env["API_BEARER_TOKEN"] = token
            script = (
                f'export PATH="{_bash_path(fake_bin)}:$PATH"; '
                f'ROOT_DIR="{_bash_path(temp)}"; '
                f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
                + ("set -x; " if xtrace else "")
                + "bearer_curl -fsS http://127.0.0.1:8000/sessions"
            )
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = argv_log.read_text(encoding="utf-8")
            stdin = stdin_log.read_text(encoding="utf-8") if stdin_log.exists() else ""
            self.trace = result.stderr
            return result.stdout, argv, stdin

    def test_a_set_x_trace_never_shows_the_token(self) -> None:
        """bash -x traces builtins and function arguments: a debug run must not print the token."""
        _, argv, stdin = self.run_bearer_curl(token=TOKEN, xtrace=True)

        self.assertIn("+ ", self.trace)  # the trace is on
        self.assertIn("curl -q --noproxy", self.trace)
        self.assertNotIn(TOKEN, self.trace)
        self.assertNotIn(TOKEN, argv)
        self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', stdin)

    def test_the_token_reaches_curl_on_stdin_never_in_argv(self) -> None:
        stdout, argv, stdin = self.run_bearer_curl(token=TOKEN)

        self.assertEqual(stdout.strip(), "ok")
        self.assertNotIn(TOKEN, argv)
        self.assertNotIn("Authorization", argv)
        self.assertIn("--config", argv.splitlines())
        self.assertEqual(argv.splitlines()[argv.splitlines().index("--config") + 1], "-")
        self.assertIn(f'header = "Authorization: Bearer {TOKEN}"', stdin)
        # The caller's own arguments still reach curl.
        self.assertIn("http://127.0.0.1:8000/sessions", argv.splitlines())

    def test_curl_is_told_to_skip_its_own_config_file(self) -> None:
        # -q must be curl's first argument: a .curlrc (found through CURL_HOME, XDG_CONFIG_HOME or HOME) could
        # otherwise add a proxy or another header to a request that carries the token.
        _, argv, _ = self.run_bearer_curl(token=TOKEN)

        self.assertEqual(argv.splitlines()[0], "-q")

    def test_without_a_token_no_authorization_is_sent(self) -> None:
        _, argv, stdin = self.run_bearer_curl(token=None)

        self.assertNotIn("Authorization", argv + stdin)

    def test_a_token_with_a_line_break_is_refused_loudly_and_never_sent(self) -> None:
        # No HTTP header can carry one, and in a curl config it would split the line.
        for broken in (TOKEN[:20] + "\n" + TOKEN[20:], TOKEN + "\r"):
            with self.subTest(broken=repr(broken[-3:])):
                _, argv, stdin = self.run_bearer_curl(token=broken)

                self.assertNotIn("Authorization", argv + stdin)
                self.assertIn("line break", self.trace)
                self.assertNotIn(TOKEN[:20], self.trace)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers 200 and records the headers of every request it gets."""

    seen: list[dict[str, str]]

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        type(self).seen.append({name.lower(): value for name, value in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:
        pass


def _serve(seen: list) -> http.server.ThreadingHTTPServer:
    handler = type("Handler", (_Recorder,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@unittest.skipUnless(
    SCRIPTS.is_dir() and BASH and shutil.which("curl") and os.name != "nt", "scripts/, bash or curl is not here"
)
class LoopbackNoProxyTests(unittest.TestCase):
    """W2-24: bearer_curl and takeover_curl never send their headers through a proxy for a loopback controller."""

    def _run(self, function: str, url: str, extra_env: dict[str, str]) -> subprocess.CompletedProcess:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tempdir.name,
            "API_BEARER_TOKEN": TOKEN,
            "TAKEOVER_TOKEN": "takeover-" + TOKEN,
            **extra_env,
        }
        script = (
            f'ROOT_DIR="{_bash_path(Path(tempdir.name))}"; source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
            f'{function} -fsS "{url}"'
        )
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)

    def test_a_loopback_request_goes_direct_whatever_the_proxy_variables_say(self) -> None:
        reached, proxied = [], []
        target, proxy = _serve(reached), _serve(proxied)
        self.addCleanup(target.shutdown)
        self.addCleanup(proxy.shutdown)
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        environ = {name: proxy_url for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "ALL_PROXY")}
        port = target.server_address[1]
        for function in ("bearer_curl", "takeover_curl"):
            for host in ("127.0.0.1", "localhost"):
                with self.subTest(function=function, host=host):
                    result = self._run(function, f"http://{host}:{port}/sessions", environ)
                    self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(proxied, [], "a request carrying the tokens went through the proxy")
        self.assertEqual(len(reached), 4)
        self.assertTrue(all(headers.get("authorization") == f"Bearer {TOKEN}" for headers in reached))

    def test_the_environments_no_proxy_list_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            fake_bin = Path(tempdir) / "bin"
            fake_bin.mkdir()
            argv_log = Path(tempdir) / "argv.log"
            curl = fake_bin / "curl"
            curl.write_text(
                f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{_bash_path(argv_log)}"\ncat > /dev/null\n',
                encoding="utf-8",
                newline="\n",
            )
            curl.chmod(0o755)
            path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            result = self._run("bearer_curl", "http://127.0.0.1:8000/", {"PATH": path, "NO_PROXY": "hooks.internal"})
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[0], "-q")
        self.assertEqual(argv[argv.index("--noproxy") + 1], "localhost,127.0.0.1,::1,hooks.internal")

    def _noproxy_list(self, extra_env: dict[str, str]) -> str:
        """The --noproxy value bearer_curl hands a stand-in curl."""
        with tempfile.TemporaryDirectory() as tempdir:
            fake_bin = Path(tempdir) / "bin"
            fake_bin.mkdir()
            argv_log = Path(tempdir) / "argv.log"
            curl = fake_bin / "curl"
            curl.write_text(
                f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{_bash_path(argv_log)}"\ncat > /dev/null\n',
                encoding="utf-8",
                newline="\n",
            )
            curl.chmod(0o755)
            path = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            result = self._run("bearer_curl", "http://127.0.0.1:8000/", {"PATH": path, **extra_env})
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = argv_log.read_text(encoding="utf-8").splitlines()
        return argv[argv.index("--noproxy") + 1]

    def test_w4_6_the_base_urls_host_is_listed_when_it_is_another_loopback_address(self) -> None:
        cases = {
            "http://127.0.0.2:8000": "localhost,127.0.0.1,::1,127.0.0.2",
            "http://user@127.10.20.30:8000/x": "localhost,127.0.0.1,::1,127.10.20.30",
            "http://LOCALHOST:8000": "localhost,127.0.0.1,::1",
            "http://[::1]:8000": "localhost,127.0.0.1,::1",
            "http://127.0.0.1:8000": "localhost,127.0.0.1,::1",
            "http://0.0.0.0:8000": "localhost,127.0.0.1,::1,0.0.0.0",
            "http://10.0.0.5:8000": "localhost,127.0.0.1,::1",
            "http://127.0.0.2.example.com:8000": "localhost,127.0.0.1,::1",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self._noproxy_list({"AUTO_BROWSER_BASE_URL": url}), expected)
        self.assertEqual(
            self._noproxy_list({"API_BASE_URL": "http://127.0.0.3:8000", "NO_PROXY": "hooks.internal"}),
            "localhost,127.0.0.1,::1,127.0.0.3,hooks.internal",
        )

    def test_w4_6_an_inherited_star_is_printed_alone(self) -> None:
        for inherited in ("*", " * "):
            with self.subTest(inherited=inherited):
                self.assertEqual(self._noproxy_list({"NO_PROXY": inherited}), "*")
        self.assertEqual(self._noproxy_list({"no_proxy": "*", "NO_PROXY": "hooks.internal"}), "*")

    def test_w4_6_a_controller_on_another_loopback_address_is_reached_directly(self) -> None:
        reached, proxied = [], []
        try:
            target = http.server.ThreadingHTTPServer(("127.0.0.2", 0), type("Handler", (_Recorder,), {"seen": reached}))
        except OSError as exc:
            self.skipTest(f"cannot listen on 127.0.0.2 here: {exc}")
        threading.Thread(target=target.serve_forever, daemon=True).start()
        proxy = _serve(proxied)
        self.addCleanup(target.shutdown)
        self.addCleanup(proxy.shutdown)
        base_url = f"http://127.0.0.2:{target.server_address[1]}"
        environ = {
            **_proxy_environment(f"http://127.0.0.1:{proxy.server_address[1]}"),
            "AUTO_BROWSER_BASE_URL": base_url,
        }
        result = self._run("bearer_curl", f"{base_url}/sessions", environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(proxied, [], "a request carrying the tokens went through the proxy")
        self.assertEqual([headers.get("authorization") for headers in reached], [f"Bearer {TOKEN}"])

    def test_w4_6_with_no_proxy_star_a_remote_controller_is_not_reached_through_the_proxy(self) -> None:
        # 1.15.0's curl read NO_PROXY='*' as every host and went direct; appended to the loopback list it no longer
        # meant that, and the tokens went to the proxy.
        proxied: list = []
        proxy = _serve(proxied)
        self.addCleanup(proxy.shutdown)
        environ = {**_proxy_environment(f"http://127.0.0.1:{proxy.server_address[1]}"), "NO_PROXY": "*"}
        # Direct, the request fails fast instead of reaching the proxy. 192.0.2.1 is TEST-NET-1 (RFC 5737), never
        # routed, so the sentinel cannot reach a host the way a private address on a CI or office network could.
        self._run("bearer_curl --connect-timeout 1 --max-time 2", "http://192.0.2.1:8000/sessions", environ)
        self.assertEqual(proxied, [], "a request carrying the tokens went through the proxy")


def _proxy_environment(proxy_url: str) -> dict[str, str]:
    return {name: proxy_url for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")}


@unittest.skipUnless(
    SCRIPTS.is_dir() and BASH and shutil.which("curl") and os.name != "nt", "scripts/, bash or curl is not here"
)
class DoctorApiCurlNoProxyTests(unittest.TestCase):
    """W4-5: doctor.sh's api_curl sent its bearer header through an environment proxy."""

    def test_doctors_api_curl_reaches_a_loopback_controller_directly(self) -> None:
        doctor = (SCRIPTS / "doctor.sh").read_text(encoding="utf-8")
        match = re.search(r"^api_curl\(\) \{\n.*?^\}\n", doctor, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "doctor.sh no longer defines api_curl")
        reached, proxied = [], []
        target, proxy = _serve(reached), _serve(proxied)
        self.addCleanup(target.shutdown)
        self.addCleanup(proxy.shutdown)
        with tempfile.TemporaryDirectory() as tempdir:
            definition = Path(tempdir) / "api_curl.sh"
            definition.write_text(match.group(0), encoding="utf-8", newline="\n")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": tempdir,
                "API_BEARER_TOKEN": TOKEN,
                **_proxy_environment(f"http://127.0.0.1:{proxy.server_address[1]}"),
            }
            script = (
                f'ROOT_DIR="{_bash_path(Path(tempdir))}"; source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
                f'source "{_bash_path(definition)}"; OPERATOR_ID_ARGS=(); '
                f'api_curl -fsS "http://127.0.0.1:{target.server_address[1]}/sessions"'
            )
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(proxied, [], "doctor.sh's bearer header went through the proxy")
        self.assertEqual([headers.get("authorization") for headers in reached], [f"Bearer {TOKEN}"])


# Spellings of a loopback address curl and the resolver accept that are not four dotted decimals (W5-8): curl
# normalises a host before it matches --noproxy, so a spelling of another loopback address (127.2 is 127.0.0.2)
# missed the scripts' list and went through an environment proxy with both tokens, while a spelling of 127.0.0.1
# itself was normalised to it.
OTHER_IPV4_SPELLINGS = ("127.2", "0x7f.0.0.2", "2130706434", "0177.0.0.1")
# Base URLs whose host the guard read wrongly, or that curl decodes or normalises to another loopback address
# (W7-3): the authority ended at "?" or "#" too, curl decodes a percent-encoded host, drops a trailing dot, and reads
# an IPv4-mapped IPv6 literal as the IPv4 address. "{port}" is the recording proxy's port.
UNPLAIN_BASE_URLS = (
    "http://127.2?x",
    "http://127.2#x",
    "http://%31%32%37.0.0.1:9/",
    "http://%31%32%37.0.0.2:9/",
    "http://127.0.0.2.:{port}/",
    "http://127%2e0%2e0%2e2:{port}/",
    "http://[::ffff:127.0.0.2]:{port}/",
    "http://[::ffff:7f00:2]:{port}/",
    "http://[0:0:0:0:0:ffff:7f00:2]:{port}/",
    # R5C-24: an empty bracketed host, left to curl, which refuses the first two and globs the third.
    "http://[]:{port}/",
    "http://[]/",
    "http://[/",
)
PLAIN_HOST_MESSAGE = (
    "write the controller address in AUTO_BROWSER_BASE_URL (or API_BASE_URL) as a plain host name or address, such "
    "as 127.0.0.2"
)
# Base URLs curl reads another host from than the guard did (W9-4): curl connects to 127.0.0.2 for the first two,
# where the guard read an empty host and "http", so 127.0.0.2 missed the --noproxy list; the third has no host.
UNREADABLE_BASE_URLS = ("http:///127.0.0.2:{port}/", "http:/127.0.0.2:{port}/", "http://:{port}/")
URL_FORM_MESSAGE = "write AUTO_BROWSER_BASE_URL (or API_BASE_URL) as http://host:port, such as http://127.0.0.2:8000"


@unittest.skipUnless(
    SCRIPTS.is_dir() and BASH and shutil.which("curl") and os.name != "nt", "scripts/, bash or curl is not here"
)
class OtherIpv4SpellingTests(unittest.TestCase):
    """W5-8, W7-3: a base URL whose host is not a plain name or address is refused before any token is sent."""

    def _run(
        self, function: str, host: str, *, variable: str = "AUTO_BROWSER_BASE_URL", direct: bool = True
    ) -> tuple[int, str, bool]:
        """Run `function` against http://<host>:<port>/ (or `host` itself when it is a URL, "{port}" filled in) with
        a recording proxy; (exit code, stderr, curl ran)."""
        proxied: list = []
        proxy = _serve(proxied)
        self.addCleanup(proxy.shutdown)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        temp = Path(tempdir.name)
        fake_bin, marker = temp / "bin", temp / "curl-ran"
        fake_bin.mkdir()
        curl = fake_bin / "curl"
        curl.write_text(
            f'#!/usr/bin/env bash\ntouch "{_bash_path(marker)}"\nexec "{shutil.which("curl")}" "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        curl.chmod(0o755)
        if "://" in host or "{port}" in host:
            url = host.replace("{port}", str(proxy.server_address[1]))
        else:
            url = f"http://{host}:{proxy.server_address[1]}/sessions"
        env = {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": tempdir.name,
            "API_BEARER_TOKEN": TOKEN,
            "TAKEOVER_TOKEN": "takeover-" + TOKEN,
            variable: url,
            **_proxy_environment(f"http://127.0.0.1:{proxy.server_address[1]}"),
        }
        prelude = f'ROOT_DIR="{_bash_path(temp)}"; source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
        if function == "api_curl":
            doctor = (SCRIPTS / "doctor.sh").read_text(encoding="utf-8")
            match = re.search(r"^api_curl\(\) \{\n.*?^\}\n", doctor, re.MULTILINE | re.DOTALL)
            self.assertIsNotNone(match, "doctor.sh no longer defines api_curl")
            definition = temp / "api_curl.sh"
            definition.write_text(match.group(0), encoding="utf-8", newline="\n")
            prelude += f'source "{_bash_path(definition)}"; OPERATOR_ID_ARGS=(); '
        result = subprocess.run(
            [BASH, "-c", prelude + f'{function} -sS --max-time 5 "$TARGET_URL"'],
            capture_output=True,
            text=True,
            env={**env, "TARGET_URL": url},
            timeout=60,
        )
        if direct:
            self.assertEqual(proxied, [], f"{function} sent the tokens through the proxy for {host}")
        return result.returncode, result.stderr, marker.exists()

    def test_each_other_spelling_is_refused_before_curl_runs(self) -> None:
        for function in ("bearer_curl", "takeover_curl"):
            for host in OTHER_IPV4_SPELLINGS + UNPLAIN_BASE_URLS:
                with self.subTest(function=function, host=host):
                    code, stderr, ran = self._run(function, host)
                    self.assertNotEqual(code, 0)
                    self.assertIn(PLAIN_HOST_MESSAGE, stderr)
                    self.assertFalse(ran, "curl ran")

    def test_w9_4_a_base_url_curl_reads_another_host_from_is_refused_before_curl_runs(self) -> None:
        for function in ("bearer_curl", "takeover_curl"):
            for url in UNREADABLE_BASE_URLS:
                with self.subTest(function=function, url=url):
                    code, stderr, ran = self._run(function, url)
                    self.assertNotEqual(code, 0)
                    self.assertIn(URL_FORM_MESSAGE, stderr)
                    self.assertFalse(ran, "curl ran")

    def test_w9_1_an_at_after_the_query_or_fragment_is_not_read_as_userinfo(self) -> None:
        # curl ends the authority at the first ?, # or /, so it connects to 127.0.0.2 for both; a guard that cut only
        # at / read the host as cafe.bad, 127.0.0.2 missed the --noproxy list, and the tokens went to the proxy.
        # Both are plain, so neither is refused; nothing listens on 127.0.0.2, and the proxy must see nothing.
        for function in ("bearer_curl", "takeover_curl"):
            for url in ("http://127.0.0.2:{port}#@cafe.bad/", "http://127.0.0.2:{port}?@cafe.bad/"):
                with self.subTest(function=function, url=url):
                    _code, stderr, ran = self._run(function, url)
                    self.assertNotIn(PLAIN_HOST_MESSAGE, stderr)
                    self.assertNotIn(URL_FORM_MESSAGE, stderr)
                    self.assertTrue(ran, "curl did not run")

    def test_w10_5_a_scheme_is_read_only_at_the_start_of_the_base_url(self) -> None:
        # curl guesses http:// for a scheme-less URL and connects to 127.0.0.2 (or localhost) for each; a guard that
        # cut through the first :// anywhere read the host as cafe.bad, 127.0.0.2 missed the --noproxy list, and the
        # tokens went to the proxy. The controller, on the URL's own address, must get each request, the proxy none.
        try:
            probe = http.server.ThreadingHTTPServer(("127.0.0.2", 0), _Recorder)
        except OSError:
            self.skipTest("127.0.0.2 cannot be bound here")
        probe.server_close()
        proxied: list = []
        proxy = _serve(proxied)
        self.addCleanup(proxy.shutdown)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        for function in ("bearer_curl", "takeover_curl"):
            for address, url in (
                ("127.0.0.2", "127.0.0.2:{port}/?next=http://cafe.bad/"),
                ("127.0.0.2", "127.0.0.2:{port}/#http://cafe.bad/"),
                ("127.0.0.1", "localhost:{port}/x?u=https://cafe.bad"),
                ("127.0.0.2", "http://127.0.0.2:{port}/?next=http://cafe.bad/"),
            ):
                with self.subTest(function=function, url=url):
                    seen: list = []
                    proxied.clear()
                    controller = http.server.ThreadingHTTPServer(
                        (address, 0), type("Handler", (_Recorder,), {"seen": seen})
                    )
                    threading.Thread(target=controller.serve_forever, daemon=True).start()
                    self.addCleanup(controller.server_close)
                    self.addCleanup(controller.shutdown)
                    target = url.replace("{port}", str(controller.server_address[1]))
                    result = subprocess.run(
                        [
                            BASH,
                            "-c",
                            f'ROOT_DIR="{_bash_path(Path(tempdir.name))}"; '
                            f'source "{_bash_path(SCRIPTS / "load_env.sh")}"; '
                            f'{function} -4 -sS --max-time 5 "$TARGET_URL"',
                        ],
                        capture_output=True,
                        text=True,
                        env={
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": tempdir.name,
                            "API_BEARER_TOKEN": TOKEN,
                            "TAKEOVER_TOKEN": "takeover-" + TOKEN,
                            "AUTO_BROWSER_BASE_URL": target,
                            "TARGET_URL": target,
                            **_proxy_environment(f"http://127.0.0.1:{proxy.server_address[1]}"),
                        },
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(len(seen), 1, "the controller did not get the request directly")
                    self.assertEqual(proxied, [], f"{function} sent the tokens through the proxy for {url}")

    def test_w9_4_a_scheme_less_base_url_is_not_refused(self) -> None:
        for url in ("localhost:{port}", "127.0.0.2:{port}/x"):
            with self.subTest(url=url):
                _code, stderr, ran = self._run("bearer_curl", url, direct=False)
                self.assertNotIn(URL_FORM_MESSAGE, stderr)
                self.assertNotIn(PLAIN_HOST_MESSAGE, stderr)
                self.assertTrue(ran)

    def test_api_base_url_is_checked_too(self) -> None:
        code, stderr, ran = self._run("bearer_curl", "127.2", variable="API_BASE_URL")
        self.assertNotEqual(code, 0)
        self.assertIn(PLAIN_HOST_MESSAGE, stderr)
        self.assertFalse(ran)

    def test_plain_names_and_addresses_are_not_refused(self) -> None:
        for host in (
            "127.0.0.1",
            "127.0.0.2",
            "cafe.bad",
            "localhost",
            "0x",
            "0X",
            "[::1]",
            "[2001:db8::5]",
            "http://operator@127.0.0.1:{port}/base/path",
        ):
            with self.subTest(host=host):
                # The recorder is both proxy and target here, so what reached it says nothing.
                _code, stderr, ran = self._run("bearer_curl", host, direct=False)
                self.assertNotIn(PLAIN_HOST_MESSAGE, stderr)
                self.assertTrue(ran)

    def test_doctor_does_not_check_a_base_url_it_never_requests(self) -> None:
        # W7-3: doctor.sh's api_curl only requests http://127.0.0.1:${API_PORT}, always in the --noproxy list.
        _code, stderr, ran = self._run("api_curl", "127.2", direct=False)
        self.assertNotIn(PLAIN_HOST_MESSAGE, stderr)
        self.assertTrue(ran)


class _JsonRecorder(_Recorder):
    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        type(self).seen.append({name.lower(): value for name, value in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"null")


@unittest.skipUnless((SCRIPTS / "smoke_download.py").is_file(), "scripts/ is not shipped inside the controller image")
class SmokeDownloadNoProxyTests(unittest.TestCase):
    """W4-5: smoke_download.py's urlopen followed http_proxy, with no loopback bypass, carrying the token."""

    def test_the_download_smoke_reaches_the_controller_directly(self) -> None:
        import importlib.util
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location("smoke_download_under_test", SCRIPTS / "smoke_download.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reached, proxied = [], []
        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Target", (_JsonRecorder,), {"seen": reached}))
        threading.Thread(target=target.serve_forever, daemon=True).start()
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("Proxy", (_JsonRecorder,), {"seen": proxied}))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(target.shutdown)
        self.addCleanup(proxy.shutdown)
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        with tempfile.TemporaryDirectory() as tempdir:
            (Path(tempdir) / "data").mkdir()
            (Path(tempdir) / "data" / "api-token").write_text(TOKEN + "\n", encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(tempdir)
            try:
                with (
                    patch.dict(os.environ, _proxy_environment(proxy_url), clear=True),
                    patch.object(module, "API", f"http://127.0.0.1:{target.server_address[1]}"),
                    # urlopen caches the opener it first builds, with the proxies of that moment: one an earlier
                    # test left would let a reverted urlopen pass here without the proxy above (W5-10).
                    patch.object(urllib.request, "_opener", None),
                ):
                    status, _ = module.api("GET", "/sessions")
            finally:
                os.chdir(cwd)
        self.assertEqual(status, 200)
        self.assertEqual(proxied, [], "the download smoke's bearer header went through the proxy")
        self.assertEqual([headers.get("authorization") for headers in reached], [f"Bearer {TOKEN}"])

    def test_w10_13_a_redirect_is_reported_and_not_followed(self) -> None:
        # urllib's default handler copied the Authorization header onto a redirect to any origin.
        import importlib.util
        from unittest.mock import patch

        spec = importlib.util.spec_from_file_location("smoke_download_under_test", SCRIPTS / "smoke_download.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reached, elsewhere_seen = [], []
        elsewhere = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), type("Elsewhere", (_JsonRecorder,), {"seen": elsewhere_seen})
        )
        threading.Thread(target=elsewhere.serve_forever, daemon=True).start()
        self.addCleanup(elsewhere.shutdown)
        location = f"http://127.0.0.1:{elsewhere.server_address[1]}/stolen"

        class Redirecting(_Recorder):
            seen = reached

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                type(self).seen.append({name.lower(): value for name, value in self.headers.items()})
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.shutdown)
        with tempfile.TemporaryDirectory() as tempdir:
            (Path(tempdir) / "data").mkdir()
            (Path(tempdir) / "data" / "api-token").write_text(TOKEN + "\n", encoding="utf-8")
            cwd = os.getcwd()
            os.chdir(tempdir)
            try:
                with (
                    patch.dict(os.environ, {}, clear=True),
                    patch.object(module, "API", f"http://127.0.0.1:{target.server_address[1]}"),
                ):
                    status, _ = module.api("GET", "/sessions")
            finally:
                os.chdir(cwd)
        self.assertEqual(status, 302)
        self.assertEqual(len(reached), 1)
        self.assertEqual(elsewhere_seen, [], "the download smoke followed a redirect with its bearer header")


@unittest.skipUnless(SCRIPTS.is_dir(), "scripts/ is not shipped inside the controller image")
class SmokeTakeoverResponseTests(unittest.TestCase):
    def test_w2_24_no_smoke_holds_the_takeover_link_response_in_a_shell_variable(self) -> None:
        # An assignment is printed by xtrace: the response carries the noVNC password. It is piped to Python.
        for name in ("smoke_isolated_session.sh", "smoke_isolated_session_tunnel.sh"):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertNotIn("TAKEOVER_LINK_RESPONSE", text)
                self.assertNotRegex(text, r'=\s*"\$\(\s*takeover_curl')
                self.assertRegex(text, r"takeover_curl -fsS -i [^\n|]*\| python3 -I -c")


@unittest.skipUnless(SCRIPTS.is_dir(), "scripts/ is not shipped inside the controller image")
class NoTokenInArgvTests(unittest.TestCase):
    def test_no_script_makefile_or_workflow_puts_a_bearer_token_in_argv(self) -> None:
        sources = [*SCRIPTS.glob("*.sh"), REPO / "Makefile", *(REPO / ".github" / "workflows").glob("*.yml")]
        offenders = []
        for path in sources:
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if line.lstrip().startswith("#"):
                    continue
                if ARGV_TOKEN.search(line):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a bearer token reaches a command line")

    def test_the_pattern_still_catches_the_old_forms(self) -> None:
        # Guards the regex: these are the three forms this change removed.
        for line in (
            '    curl -H "Authorization: Bearer ${token}" "$@"',
            '    args+=(-H "Authorization: Bearer ${token}")',
            """  BASTION_AUTH="-H 'Authorization: Bearer ${BASTION_TOKEN}'\"""",
        ):
            with self.subTest(line=line):
                self.assertRegex(line, ARGV_TOKEN)

    def test_every_curl_that_reads_the_token_from_stdin_skips_its_curlrc(self) -> None:
        # D3b-7: bearer_curl got -q; doctor.sh's api_curl and smoke_reverse_ssh.sh's bastion curl did not. W4-5: nor
        # --noproxy, which bearer_curl had.
        offenders = [
            f"{path.name}: {command}"
            for path in sorted(SCRIPTS.glob("*.sh"))
            for command in _token_curls_that_read_a_curlrc(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "the token reaches a curl that reads a .curlrc")
        self.assertTrue(
            any(TOKEN_PIPE.search(path.read_text(encoding="utf-8")) for path in SCRIPTS.glob("*.sh")),
            "no script pipes curl_auth_config any more: the check above would pass on nothing",
        )

    def test_the_check_catches_the_old_and_other_forms_and_passes_the_fixed_ones(self) -> None:
        for text in (
            '  curl_auth_config | curl --config - ${args[@]+"${args[@]}"} "$@"',
            '  curl_auth_config | "${COMPOSE[@]}" exec -T test-bastion sh -lc "curl --config - $1"',
            "  curl_auth_config | curl -fsS --config - http://127.0.0.1:8000/",
            '  curl_auth_config | curl -K - "$u"',
            '  curl_auth_config | curl -fsS "$u" \\\n    --config -',
            '  out="$(curl_auth_config | curl -fsS "$u" --config -)"',
            '  curl_auth_config | curl --disable-epsv --config - "$u"',
            '  curl_auth_config | "$CURL" -q --config - "$u"',
            '  curl -fsS --config <(curl_auth_config) "$u"',
            '  curl_auth_config | curl -q --config - "$@"',
            '  curl_auth_config | curl -q --config - ${args[@]+"${args[@]}"} "$@"',
        ):
            with self.subTest(text=text):
                self.assertEqual(len(_token_curls_that_read_a_curlrc(text)), 1)
        for text in (
            '  curl_auth_config | curl -q --noproxy "$(loopback_noproxy)" --config - "$@"',
            '  curl_auth_config | "${COMPOSE[@]}" exec -T test-bastion sh -lc "curl -q --noproxy 127.0.0.1 --config - $1"',
            '  out="$(curl_auth_config | curl -q --noproxy "$(loopback_noproxy)" --config - "$u")"',
            '  curl_auth_config | curl --disable --noproxy localhost --config - "$u"',
            "curl_auth_config() {",
            "# pipe it in: curl_auth_config | curl --config - ...",
        ):
            with self.subTest(text=text):
                self.assertEqual(_token_curls_that_read_a_curlrc(text), [])


if __name__ == "__main__":
    unittest.main()
