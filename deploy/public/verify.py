#!/usr/bin/env python3
"""Post-deployment verification for the public Auto Browser boundary.

This script is intentionally dependency-free and read-only.  It validates the
two public HTTPS virtual hosts, checks that private browser services did not
become reachable, searches every observed response (including linked static
assets) for likely credential material, and exercises the existing private
Hermes verification scripts without echoing their output.

Examples::

    python3 deploy/public/verify.py
    python3 deploy/public/verify.py --env-file /opt/auto-browser/.env
    python3 deploy/public/verify.py --skip-hermes
    python3 deploy/public/verify.py --self-test

Defaults:
  portal host:  secure-browser.fareeqk.com
  MCP host:     mcp-browser.fareeqk.com
  server IPv4:  204.168.150.160

Any FAIL line makes the process exit non-zero.  Secret values, response
bodies, redirect targets, and child-process output are never printed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html.parser
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

DEFAULT_PORTAL_HOST = "secure-browser.fareeqk.com"
DEFAULT_MCP_HOST = "mcp-browser.fareeqk.com"
DEFAULT_SERVER_IP = "204.168.150.160"
DEFAULT_FORBIDDEN_PORTS = (8000, 18000, 18001, 5900, 15900, 6080, 16080, 9222, 9223)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ASSETS = 32

JSON_HEADERS = {"Accept": "application/json"}
MCP_UNAUTHENTICATED_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": "public-verification",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "public-verifier", "version": "1"},
        },
    },
    separators=(",", ":"),
).encode("utf-8")


class VerificationError(RuntimeError):
    """A safe-to-display verification error containing no response content."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.casefold()
        for key, value in self.headers:
            if key.casefold() == wanted:
                return value
        return None


class Reporter:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def passed(self, label: str) -> None:
        self.checks += 1
        print(f"PASS {label}", flush=True)

    def failed(self, label: str, detail: str | None = None) -> None:
        self.checks += 1
        self.failures += 1
        suffix = f" ({detail})" if detail else ""
        print(f"FAIL {label}{suffix}", flush=True)

    def expect(self, condition: bool, label: str, detail: str | None = None) -> bool:
        if condition:
            self.passed(label)
            return True
        self.failed(label, detail)
        return False


class AssetParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append(values["src"] or "")
        elif tag == "link" and values.get("href"):
            rel = (values.get("rel") or "").casefold().split()
            if any(item in rel for item in ("stylesheet", "modulepreload", "preload")):
                self.references.append(values["href"] or "")


SENSITIVE_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY|TOTP|BEARER)",
    re.IGNORECASE,
)
GENERIC_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private key material", re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("JWT-like credential", re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    (
        "bearer credential",
        re.compile(rb"\bBearer\s+(?!resource_metadata\s*=)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    ),
    (
        "sensitive session cookie",
        re.compile(
            rb"\bSet-Cookie\s*:\s*(?:portal_session|session|auth|access_token|refresh_token)="
            rb"[^;\s]{12,}",
            re.IGNORECASE,
        ),
    ),
    (
        "provider credential",
        re.compile(
            rb"(?:\bAKIA[A-Z0-9]{16}\b|\bghp_[A-Za-z0-9]{30,}\b|"
            rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b|\bxox[baprs]-[A-Za-z0-9-]{20,}\b|"
            rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b)"
        ),
    ),
    (
        "named secret assignment",
        re.compile(
            rb"(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
            rb"owner[_-]?(?:token|credential)|broker[_-]?(?:token|credential)|"
            rb"totp[_-]?(?:secret|seed)|private[_-]?key|password)"
            rb"\s*[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"']",
            re.IGNORECASE,
        ),
    ),
)


class LeakScanner:
    """Scan bytes without retaining or displaying matched credential values."""

    def __init__(self, exact_secrets: Sequence[bytes]) -> None:
        self.exact_secrets = tuple(dict.fromkeys(value for value in exact_secrets if len(value) >= 8))

    def findings(self, payload: bytes) -> list[str]:
        found: list[str] = []
        for label, pattern in GENERIC_SECRET_PATTERNS:
            if pattern.search(payload):
                found.append(label)
        if any(secret in payload for secret in self.exact_secrets):
            found.append("exact configured secret")
        return found


def read_response(response, limit: int = MAX_RESPONSE_BYTES) -> Response:  # noqa: ANN001
    body = response.read(limit + 1)
    if len(body) > limit:
        raise VerificationError("response exceeded safe size limit")
    return Response(response.status, tuple(response.headers.items()), body)


def request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> Response:
    """Fetch one URL with certificate validation and redirects disabled."""

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect(),
    )
    outgoing = dict(headers or {})
    outgoing.setdefault("User-Agent", "auto-browser-public-verifier/1")
    req = urllib.request.Request(url, data=data, headers=outgoing, method=method)
    try:
        with opener.open(req, timeout=timeout) as result:
            return read_response(result)
    except urllib.error.HTTPError as error:
        try:
            return read_response(error)
        finally:
            error.close()
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
        raise VerificationError(type(exc).__name__) from None


def response_bytes(response: Response) -> bytes:
    lines = [f"{key}: {value}".encode("utf-8", "replace") for key, value in response.headers]
    return b"\n".join(lines) + b"\n\n" + response.body


def scan_response(
    reporter: Reporter,
    scanner: LeakScanner,
    response: Response,
    label: str,
) -> None:
    findings = scanner.findings(response_bytes(response))
    reporter.expect(not findings, f"no credential leak in {label}", ", ".join(findings) if findings else None)


def load_exact_secrets(paths: Sequence[Path], reporter: Reporter) -> tuple[bytes, ...]:
    """Load sensitive dotenv values in memory; never print values or fingerprints."""

    values: list[bytes] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            reporter.failed("exact-secret source is readable", "could not read configured env file")
            continue
        count_before = len(values)
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip()
            if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
                value = value[1:-1]
            if not SENSITIVE_ENV_NAME.search(name) or not value or "${" in value:
                continue
            candidates = [value]
            if name == "BROKER_AGENT_TOKENS":
                candidates.extend(item.split(":", 1)[-1] for item in value.split(",") if ":" in item)
            for candidate in candidates:
                encoded = candidate.strip().encode("utf-8")
                if len(encoded) >= 8:
                    values.append(encoded)
        reporter.passed(
            "exact-secret source loaded"
            if len(values) > count_before
            else "exact-secret source loaded with no eligible values"
        )
    return tuple(dict.fromkeys(values))


def validate_host(value: str, label: str) -> str:
    if not re.fullmatch(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value):
        raise argparse.ArgumentTypeError(f"{label} must be a DNS hostname")
    return value.lower()


def validate_ip(value: str) -> str:
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("server IP must be a literal address") from exc
    return str(parsed)


def verify_certificate(host: str, timeout: float, reporter: Reporter) -> None:
    label = f"valid HTTPS certificate for {host}"
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                certificate = secure.getpeercert()
        not_after = certificate.get("notAfter")
        not_before = certificate.get("notBefore")
        if not not_after or not not_before:
            raise VerificationError("certificate validity window missing")
        now = time.time()
        valid = ssl.cert_time_to_seconds(not_before) <= now < ssl.cert_time_to_seconds(not_after)
        reporter.expect(valid, label, "certificate is outside its validity window")
    except (OSError, ssl.SSLError, ValueError, VerificationError) as exc:
        reporter.failed(label, type(exc).__name__)


def verify_http_redirect(
    host: str,
    path: str,
    timeout: float,
    reporter: Reporter,
    scanner: LeakScanner,
) -> None:
    label = f"HTTP redirects safely to HTTPS for {host}"
    try:
        response = request(f"http://{host}{path}", timeout=timeout)
        scan_response(reporter, scanner, response, f"HTTP redirect response on {host}")
        location = response.header("Location") or ""
        parsed = urllib.parse.urlsplit(location)
        valid = (
            response.status in {301, 302, 307, 308}
            and parsed.scheme.casefold() == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold() == host.casefold()
            and parsed.port in (None, 443)
            and parsed.path == path
        )
        reporter.expect(valid, label, f"HTTP {response.status} without a same-host HTTPS redirect")
    except (VerificationError, ValueError):
        reporter.failed(label, "request failed")


def get_json(response: Response) -> dict[str, object] | None:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def verify_gateway(
    host: str,
    timeout: float,
    reporter: Reporter,
    scanner: LeakScanner,
    responses: list[tuple[str, Response, str]],
) -> None:
    base = f"https://{host}"
    metadata_specs = (
        ("/.well-known/oauth-authorization-server", "authorization metadata"),
        ("/.well-known/oauth-protected-resource", "protected-resource metadata"),
    )
    metadata: dict[str, dict[str, object]] = {}
    for path, name in metadata_specs:
        try:
            response = request(base + path, headers=JSON_HEADERS, timeout=timeout)
            responses.append((base + path, response, name))
            body = get_json(response)
            reporter.expect(response.status == 200 and body is not None, f"MCP {name} is public JSON", f"HTTP {response.status}")
            if body is not None:
                metadata[path] = body
        except VerificationError:
            reporter.failed(f"MCP {name} is public JSON", "request failed")

    issuer = metadata.get("/.well-known/oauth-authorization-server", {})
    expected = {
        "issuer": base,
        "authorization_endpoint": base + "/authorize",
        "token_endpoint": base + "/token",
        "registration_endpoint": base + "/register",
    }
    auth_valid = all(issuer.get(key) == value for key, value in expected.items())
    auth_valid = auth_valid and issuer.get("code_challenge_methods_supported") == ["S256"]
    auth_valid = auth_valid and issuer.get("token_endpoint_auth_methods_supported") == ["none"]
    reporter.expect(auth_valid, "MCP authorization metadata is bound to the canonical origin")

    resource = metadata.get("/.well-known/oauth-protected-resource", {})
    resource_valid = resource.get("resource") == base + "/mcp"
    servers = resource.get("authorization_servers")
    resource_valid = resource_valid and isinstance(servers, list) and base in servers
    reporter.expect(resource_valid, "MCP protected-resource metadata is canonical")

    try:
        response = request(
            base + "/mcp",
            method="POST",
            data=MCP_UNAUTHENTICATED_BODY,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
        responses.append((base + "/mcp", response, "unauthenticated MCP challenge"))
        challenge = response.header("WWW-Authenticate") or ""
        challenge_valid = response.status == 401 and challenge.startswith("Bearer ")
        challenge_valid = challenge_valid and base + "/.well-known/oauth-protected-resource" in challenge
        reporter.expect(challenge_valid, "unauthenticated MCP POST is rejected with OAuth challenge", f"HTTP {response.status}")
    except VerificationError:
        reporter.failed("unauthenticated MCP POST is rejected with OAuth challenge", "request failed")

    public_method_paths = ("/authorize", "/register", "/token", "/mcp")
    for path in public_method_paths:
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, f"MCP allowlist probe {path}"))
            expected_status = response.status in ({400, 422} if path == "/authorize" else {405})
            reporter.expect(expected_status, f"MCP public path allowlist includes {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"MCP public path allowlist includes {path}", "request failed")

    forbidden_paths = (
        "/", "/healthz", "/docs", "/redoc", "/openapi.json", "/internal/active-user",
        "/internal/connected-clients", "/browser", "/api/session", "/owner/sessions",
        "/requests", "/vnc.html", "/websockify", "/json/version", "/devtools/browser",
        "/.env", "/server-status", "/__public-verifier-not-a-route__",
    )
    verify_nothing_served(base, forbidden_paths, "MCP", timeout, reporter, responses)


def is_signin_redirect(response: Response, host: str) -> bool:
    if response.status not in {302, 303, 307, 308}:
        return False
    location = response.header("Location") or ""
    parsed = urllib.parse.urlsplit(location)
    if parsed.scheme and (parsed.scheme.casefold() != "https" or parsed.hostname != host):
        return False
    return parsed.path == "/signin"


def verify_portal(
    host: str,
    timeout: float,
    reporter: Reporter,
    responses: list[tuple[str, Response, str]],
) -> None:
    base = f"https://{host}"
    public_gets = ("/", "/signin", "/invite/public-verifier-invalid-token")
    for path in public_gets:
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, f"portal public page {path}"))
            reporter.expect(response.status == 200, f"portal public page is reachable: {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"portal public page is reachable: {path}", "request failed")

    # These routes are intentionally public POST handlers.  GET proves the
    # route boundary without submitting credentials or changing enrollment.
    public_post_paths = (
        "/enroll", "/enroll/confirm",
        "/api/invitations/redeem", "/api/enrollments/confirm",
        "/api/recovery/begin", "/api/recovery/confirm",
    )
    for path in public_post_paths:
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, f"portal public method boundary {path}"))
            reporter.expect(response.status == 405, f"portal public enrollment route has POST-only boundary: {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"portal public enrollment route has POST-only boundary: {path}", "request failed")

    protected_gets = ("/browser", "/api/session", "/api/connections", "/oauth/authorize")
    for path in protected_gets:
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, f"portal protected page {path}"))
            protected = response.status in {401, 403} or is_signin_redirect(response, host)
            reporter.expect(protected, f"portal authentication is required for {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"portal authentication is required for {path}", "request failed")

    protected_posts = (
        "/logout", "/api/browser/open", "/api/browser/close",
        "/api/connections/public-verifier/disconnect", "/api/recovery-codes/regenerate",
        "/oauth/authorize",
    )
    for path in protected_posts:
        try:
            response = request(
                base + path,
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json", "Origin": base},
                timeout=timeout,
            )
            responses.append((base + path, response, f"portal protected action {path}"))
            reporter.expect(response.status in {401, 403}, f"portal authentication is required for action {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"portal authentication is required for action {path}", "request failed")

    forbidden_paths = (
        "/healthz", "/docs", "/redoc", "/openapi.json", "/mcp", "/register", "/token",
        "/internal/active-user", "/owner/sessions", "/requests", "/controller", "/vnc.html",
        "/websockify", "/json/version", "/devtools/browser", "/.env", "/server-status",
        "/__public-verifier-not-a-route__",
    )
    verify_nothing_served(base, forbidden_paths, "portal", timeout, reporter, responses)


def verify_nothing_served(
    base: str,
    paths: Iterable[str],
    surface: str,
    timeout: float,
    reporter: Reporter,
    responses: list[tuple[str, Response, str]],
) -> None:
    for path in paths:
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, f"{surface} forbidden path {path}"))
            reporter.expect(response.status in {403, 404, 410}, f"{surface} serves nothing at {path}", f"HTTP {response.status}")
        except VerificationError:
            reporter.failed(f"{surface} serves nothing at {path}", "request failed")


def verify_bare_ip_paths(
    server_ip: str,
    timeout: float,
    reporter: Reporter,
    responses: list[tuple[str, Response, str]],
) -> None:
    base = f"http://{server_ip}"
    paths = (
        "/mcp", "/internal/active-user", "/owner/sessions", "/requests", "/browser",
        "/vnc.html", "/websockify", "/json/version", "/devtools/browser", "/healthz",
        "/.env", "/__public-verifier-not-a-route__",
    )
    for path in paths:
        label = f"bare IP serves nothing at {path}"
        try:
            response = request(base + path, timeout=timeout)
            responses.append((base + path, response, label))
            # A generic Traefik redirect/default rejection is safe here; a
            # successful or authenticated application response is not.
            reporter.expect(
                response.status in {301, 302, 303, 307, 308, 404, 410},
                label,
                f"HTTP {response.status}",
            )
        except VerificationError:
            # No listener or a dropped request is also a safe result on the IP.
            reporter.passed(label)


@dataclass(frozen=True)
class PortProbeResult:
    target: str
    port: int
    safe: bool
    outcome: str


def probe_forbidden_port(target: str, port: int, timeout: float) -> PortProbeResult:
    """Fail only if a service responds; refusal and bounded timeout are safe."""

    request_bytes = (
        f"GET / HTTP/1.0\r\nHost: {target}\r\nUser-Agent: auto-browser-public-verifier/1\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((target, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(request_bytes)
            try:
                prefix = connection.recv(16)
            except socket.timeout:
                return PortProbeResult(target, port, True, "timed out without response")
            if not prefix:
                return PortProbeResult(target, port, True, "closed without response")
            if prefix.startswith(b"HTTP/"):
                return PortProbeResult(target, port, False, "HTTP service responded")
            return PortProbeResult(target, port, False, "non-HTTP service responded")
    except (ConnectionRefusedError, TimeoutError, socket.timeout):
        return PortProbeResult(target, port, True, "refused or timed out")
    except OSError:
        return PortProbeResult(target, port, True, "unreachable")


def verify_forbidden_ports(
    targets: Sequence[str],
    ports: Sequence[int],
    timeout: float,
    reporter: Reporter,
) -> None:
    jobs = [(target, port) for target in targets for port in ports]
    workers = min(16, max(1, len(jobs)))
    results: list[PortProbeResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe_forbidden_port, target, port, timeout) for target, port in jobs]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    for result in sorted(results, key=lambda item: (item.target, item.port)):
        reporter.expect(
            result.safe,
            f"private service port is absent on {result.target}:{result.port}",
            result.outcome,
        )


def crawl_and_scan_assets(
    responses: list[tuple[str, Response, str]],
    timeout: float,
    reporter: Reporter,
    scanner: LeakScanner,
) -> None:
    """Scan recorded responses and same-origin linked JS/CSS/font assets."""

    queue: list[tuple[str, str]] = []
    seen_urls = {url for url, _, _ in responses}
    for url, response, label in list(responses):
        scan_response(reporter, scanner, response, label)
        content_type = (response.header("Content-Type") or "").casefold()
        if "html" not in content_type and b"<html" not in response.body[:512].lower() and b"<!doctype" not in response.body[:512].lower():
            continue
        parser = AssetParser()
        try:
            parser.feed(response.body.decode("utf-8", "replace"))
        except (ValueError, AssertionError):
            reporter.failed(f"static asset discovery for {label}", "invalid HTML")
            continue
        origin = urllib.parse.urlsplit(url)
        for reference in parser.references:
            absolute = urllib.parse.urljoin(url, reference)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme == "https" and parsed.netloc == origin.netloc and absolute not in seen_urls:
                seen_urls.add(absolute)
                queue.append((absolute, parsed.path or "/"))

    if len(queue) > MAX_ASSETS:
        reporter.failed("static asset count is bounded", f"found more than {MAX_ASSETS}")
        queue = queue[:MAX_ASSETS]
    else:
        reporter.passed("static asset count is bounded")

    for url, path in queue:
        label = f"static asset {path}"
        try:
            response = request(url, timeout=timeout)
            reporter.expect(response.status == 200, f"linked {label} is reachable", f"HTTP {response.status}")
            scan_response(reporter, scanner, response, label)
        except VerificationError:
            reporter.failed(f"linked {label} is reachable", "request failed")


def run_hermes_checks(
    hermes_dir: Path,
    timeout: float,
    reporter: Reporter,
    scanner: LeakScanner,
) -> None:
    checks = (
        ("Hermes private topology check", ["bash", str(hermes_dir / "check-on-hetzner.sh")]),
        ("Hermes private MCP behavior check", [sys.executable, str(hermes_dir / "verify-on-hetzner.py")]),
    )
    for label, command in checks:
        if not Path(command[-1]).is_file():
            reporter.failed(label, "verification script is missing")
            continue
        try:
            completed = subprocess.run(
                command,
                cwd=str(hermes_dir.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired):
            reporter.failed(label, "subprocess failed or timed out")
            continue
        child_output = completed.stdout + b"\n" + completed.stderr
        findings = scanner.findings(child_output)
        if findings:
            reporter.failed(label, "child process emitted sensitive-looking output; output suppressed")
        else:
            reporter.expect(completed.returncode == 0, label, f"exit {completed.returncode}")


def self_test() -> int:
    reporter = Reporter()
    known = b"unit-test-secret-value-123456"
    scanner = LeakScanner((known,))
    reporter.expect(scanner.findings(b"ordinary public response") == [], "self-test accepts ordinary content")
    reporter.expect("exact configured secret" in scanner.findings(b"x=" + known), "self-test detects exact secret")
    reporter.expect(
        "private key material" in scanner.findings(b"-----BEGIN PRIVATE KEY-----"),
        "self-test detects private key material",
    )
    reporter.expect(
        scanner.findings(b'WWW-Authenticate: Bearer resource_metadata="https://example.test/meta"') == [],
        "self-test permits OAuth metadata challenge",
    )
    parser = AssetParser()
    parser.feed('<script src="/app.js"></script><link rel="stylesheet" href="/app.css">')
    reporter.expect(parser.references == ["/app.js", "/app.css"], "self-test discovers static assets")
    response = Response(303, (("Location", "/signin?next=%2Fbrowser"),), b"")
    reporter.expect(is_signin_redirect(response, "example.test"), "self-test recognizes safe sign-in redirect")
    unsafe = Response(303, (("Location", "https://attacker.invalid/signin"),), b"")
    reporter.expect(not is_signin_redirect(unsafe, "example.test"), "self-test rejects cross-origin sign-in redirect")
    print(f"SUMMARY {reporter.checks - reporter.failures} PASS, {reporter.failures} FAIL")
    return 1 if reporter.failures else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only post-deployment verification of the public Auto Browser boundary.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--portal-host", default=DEFAULT_PORTAL_HOST, help="public human portal DNS hostname")
    parser.add_argument("--mcp-host", default=DEFAULT_MCP_HOST, help="public OAuth MCP gateway DNS hostname")
    parser.add_argument("--server-ip", default=DEFAULT_SERVER_IP, help="bare deployment server IP")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-network-operation timeout in seconds")
    parser.add_argument("--hermes-timeout", type=float, default=45.0, help="timeout for each existing Hermes verifier")
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=[],
        help="optional dotenv file whose secret values are matched exactly without being printed",
    )
    parser.add_argument(
        "--forbidden-port",
        action="append",
        type=int,
        default=[],
        help="additional TCP port that must not expose a service",
    )
    parser.add_argument(
        "--hermes-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "hermes-integration",
        help="directory containing the existing read-only Hermes checks",
    )
    parser.add_argument("--skip-hermes", action="store_true", help="skip private Hermes checks intentionally")
    parser.add_argument("--self-test", action="store_true", help="run offline unit-like checks and make no network calls")
    args = parser.parse_args(argv)
    if args.self_test:
        return args
    args.portal_host = validate_host(args.portal_host, "portal host")
    args.mcp_host = validate_host(args.mcp_host, "MCP host")
    args.server_ip = validate_ip(args.server_ip)
    if not (0.1 <= args.timeout <= 60):
        parser.error("--timeout must be between 0.1 and 60 seconds")
    if not (1 <= args.hermes_timeout <= 600):
        parser.error("--hermes-timeout must be between 1 and 600 seconds")
    if any(port < 1 or port > 65535 for port in args.forbidden_port):
        parser.error("--forbidden-port values must be between 1 and 65535")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()

    reporter = Reporter()
    exact_secrets = load_exact_secrets(args.env_file, reporter)
    scanner = LeakScanner(exact_secrets)
    responses: list[tuple[str, Response, str]] = []

    for host in (args.portal_host, args.mcp_host):
        verify_certificate(host, args.timeout, reporter)
    verify_http_redirect(args.portal_host, "/", args.timeout, reporter, scanner)
    # The MCP ingress is intentionally an exact path allowlist, so its bare
    # root should remain a 404 rather than gaining a redirect route.
    verify_http_redirect(
        args.mcp_host,
        "/.well-known/oauth-authorization-server",
        args.timeout,
        reporter,
        scanner,
    )

    verify_gateway(args.mcp_host, args.timeout, reporter, scanner, responses)
    verify_portal(args.portal_host, args.timeout, reporter, responses)
    verify_bare_ip_paths(args.server_ip, args.timeout, reporter, responses)

    ports = tuple(dict.fromkeys((*DEFAULT_FORBIDDEN_PORTS, *args.forbidden_port)))
    verify_forbidden_ports(
        (args.portal_host, args.mcp_host, args.server_ip), ports, args.timeout, reporter
    )
    crawl_and_scan_assets(responses, args.timeout, reporter, scanner)

    if args.skip_hermes:
        reporter.passed("Hermes checks skipped by explicit operator option")
    else:
        run_hermes_checks(args.hermes_dir.resolve(), args.hermes_timeout, reporter, scanner)

    passed = reporter.checks - reporter.failures
    print(f"SUMMARY {passed} PASS, {reporter.failures} FAIL", flush=True)
    return 1 if reporter.failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("FAIL verification interrupted", flush=True)
        raise SystemExit(130) from None
    except Exception:
        # Never echo exception text: a library or child failure can contain a
        # URL, header, or other operator-supplied value.  The fixed message is
        # sufficient here; detailed diagnosis can happen locally afterward.
        print("FAIL unexpected internal verifier error", flush=True)
        raise SystemExit(2) from None
