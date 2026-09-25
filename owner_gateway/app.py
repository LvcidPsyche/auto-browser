"""Fail-closed owner-only HTTP and noVNC gateway for Cloudflare Access."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse, Response
from jwt.algorithms import RSAAlgorithm
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

OWNER_GET = re.compile(r"^/(?:owner|owner\.js|owner/totp|owner/sessions|owner/auth-profiles|requests)$")
OWNER_POST = re.compile(r"^/(?:owner/totp/(?:setup|activate)|owner/sessions|owner/sessions/[A-Za-z0-9_-]{1,120}/auth-profiles|requests/[A-Za-z0-9_-]{1,120}/(?:deny|revoke))$")
OWNER_DELETE = re.compile(r"^/owner/sessions/[A-Za-z0-9_-]{1,120}$")
BAD_RAW_PATH = re.compile(rb"%(?:2e|2f|5c)|\\", re.I)
UPGRADE_HEADERS = {"connection", "upgrade", "host", "cookie", "authorization", "cf-access-jwt-assertion", "content-length", "transfer-encoding", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
SAFE_RESPONSE_HEADERS = {"content-type", "content-disposition", "etag", "last-modified"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self' wss:; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


class AccessValidator:
    def __init__(self, team_domain: str, audience: str, owner_email: str, client: httpx.AsyncClient):
        if not re.fullmatch(r"[A-Za-z0-9-]+\.cloudflareaccess\.com", team_domain):
            raise ValueError("CF_ACCESS_TEAM_DOMAIN must be a Cloudflare Access team hostname")
        if not audience or not owner_email or "@" not in owner_email:
            raise ValueError("Access audience and owner email are required")
        self.issuer = f"https://{team_domain}"
        self.audience = audience
        self.owner_email = owner_email.casefold()
        self.client = client
        self._keys: dict[str, object] = {}
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with self._lock:
            if self._expires > time.monotonic():
                return
            response = await self.client.get(f"{self.issuer}/cdn-cgi/access/certs", timeout=5)
            response.raise_for_status()
            jwks = response.json()
            keys = {}
            for item in jwks.get("keys", []):
                if item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256" and item.get("kid"):
                    keys[item["kid"]] = RSAAlgorithm.from_jwk(json.dumps(item))
            if not keys:
                raise ValueError("No Access signing keys")
            self._keys = keys
            self._expires = time.monotonic() + 300

    async def verify(self, token: str | None) -> bool:
        if not token or len(token) > 16000:
            return False
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return False
            await self._refresh()
            key = self._keys.get(header["kid"])
            if key is None:
                self._expires = 0
                await self._refresh()
                key = self._keys.get(header["kid"])
            if key is None:
                return False
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud", "email"]})
            return isinstance(claims["email"], str) and claims["email"].casefold() == self.owner_email
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError):
            return False


def clean_path(scope: dict) -> bool:
    raw = scope.get("raw_path", scope["path"].encode())
    return b"%" not in raw and not BAD_RAW_PATH.search(raw) and "//" not in scope["path"] and ".." not in scope["path"]


def allowed_http(method: str, path: str) -> bool:
    if path.startswith("/vnc/"):
        return method in {"GET", "HEAD"}
    return bool((method in {"GET", "HEAD"} and OWNER_GET.fullmatch(path))
                or (method == "POST" and OWNER_POST.fullmatch(path))
                or (method == "DELETE" and OWNER_DELETE.fullmatch(path)))


def rewrite_owner_script(source: bytes) -> bytes:
    """Adapt the existing broker UI without sending the broker bearer to JS."""
    script = source.decode("utf-8")
    changes = {
        "let ownerToken = null;": 'let ownerToken = "gateway";',
        'const takeoverUrl = "http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale";':
            'const takeoverUrl = "/vnc/vnc.html?autoconnect=true&resize=scale&path=vnc/websockify";',
        "      Authorization: `Bearer ${ownerToken}`,\n": "",
        '    credentials: "omit",': '    credentials: "same-origin",',
        "    if (response.status === 401) ownerToken = null;\n": "",
        'document.getElementById("unlock").addEventListener("click", () => {\n  ownerToken = window.prompt("رمز المالك") || null;\n  refresh();\n});':
            'document.getElementById("unlock").hidden = true;\nrefresh();',
    }
    for old, new in changes.items():
        if script.count(old) != 1:
            raise ValueError("Broker owner UI changed; refusing unsafe adaptation")
        script = script.replace(old, new)
    return script.encode("utf-8")


def create_app(*, validator: AccessValidator | None = None, client: httpx.AsyncClient | None = None,
               owner_token: str | None = None, broker_url: str = "http://approval-broker:18001",
               novnc_url: str = "http://browser-node:6080", public_origin: str | None = None) -> FastAPI:
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
    if validator is None:
        validator = AccessValidator(os.environ["CF_ACCESS_TEAM_DOMAIN"], os.environ["CF_ACCESS_AUD"],
                                    os.environ["OWNER_EMAIL"], client)
    owner_token = owner_token if owner_token is not None else os.environ["BROKER_OWNER_TOKEN"]
    public_origin = (public_origin or os.environ.get("OWNER_PUBLIC_ORIGIN", "https://secure-browser.fareeqk.com")).rstrip("/")
    if not owner_token or urlsplit(public_origin).scheme != "https":
        raise ValueError("Owner token and HTTPS public origin required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def trusted_visual_session(expected_id: str | None = None) -> str | None:
        """Ask the broker, never the controller, for its TOTP-opened session."""
        try:
            guard = await client.get(
                broker_url + "/owner/visual-access",
                headers={"Authorization": f"Bearer {owner_token}"},
                timeout=3,
            )
            if guard.status_code != 200:
                return None
            session_id = guard.json().get("session_id")
            if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", session_id):
                return None
            if expected_id is not None and session_id != expected_id:
                return None
            return session_id
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        if not clean_path(request.scope) or not allowed_http(request.method, request.url.path):
            return PlainTextResponse("Not found", status_code=404, headers=SECURITY_HEADERS)
        if not await validator.verify(request.headers.get("cf-access-jwt-assertion")):
            return PlainTextResponse("Unauthorized", status_code=401, headers=SECURITY_HEADERS)
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != public_origin:
            return PlainTextResponse("Forbidden", status_code=403, headers=SECURITY_HEADERS)
        is_vnc = request.url.path.startswith("/vnc/")
        if is_vnc and await trusted_visual_session() is None:
            return PlainTextResponse("Forbidden", status_code=403, headers=SECURITY_HEADERS)
        target_path = request.url.path[4:] if is_vnc else request.url.path
        upstream_base = novnc_url if is_vnc else broker_url
        headers = {key: value for key, value in request.headers.items() if key.lower() not in UPGRADE_HEADERS}
        if not is_vnc:
            headers["Authorization"] = f"Bearer {owner_token}"
        try:
            response = await client.request(request.method, upstream_base + target_path,
                                            params=request.query_params, content=await request.body(), headers=headers)
        except httpx.HTTPError:
            return PlainTextResponse("Upstream unavailable", status_code=502, headers=SECURITY_HEADERS)
        body = response.content
        if request.url.path == "/owner.js" and response.status_code == 200:
            try:
                body = rewrite_owner_script(body)
            except (UnicodeError, ValueError):
                return PlainTextResponse("Owner UI incompatible", status_code=502, headers=SECURITY_HEADERS)
        response_headers = {key: value for key, value in response.headers.items() if key.lower() in SAFE_RESPONSE_HEADERS}
        response_headers.update(SECURITY_HEADERS)
        if request.url.path == "/owner.js":
            response_headers["Content-Type"] = "text/javascript; charset=utf-8"
            response_headers.pop("etag", None)
            response_headers.pop("last-modified", None)
        return Response(body if request.method != "HEAD" else b"", status_code=response.status_code,
                        headers=response_headers)

    @app.websocket("/{path:path}")
    async def vnc_socket(websocket: WebSocket, path: str):
        if (not clean_path(websocket.scope) or websocket.url.path != "/vnc/websockify"
                or websocket.headers.get("origin") != public_origin
                or not await validator.verify(websocket.headers.get("cf-access-jwt-assertion"))):
            await websocket.close(code=1008)
            return
        session_id = await trusted_visual_session()
        if session_id is None:
            await websocket.close(code=1008)
            return
        ws_url = "ws" + novnc_url.removeprefix("http") + "/websockify"
        try:
            async with ws_connect(ws_url, origin=public_origin, max_size=16 * 1024 * 1024,
                                  open_timeout=5) as upstream:
                if await trusted_visual_session(session_id) is None:
                    await websocket.close(code=1008)
                    return
                await websocket.accept()

                async def browser_to_vnc():
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                break
                            # Every client frame can contain keyboard or mouse input.
                            # A closed/replaced owner session must never retain VNC control.
                            if await trusted_visual_session(session_id) is None:
                                break
                            if message.get("bytes") is not None:
                                await upstream.send(message["bytes"])
                            elif message.get("text") is not None:
                                await upstream.send(message["text"])
                    except (WebSocketDisconnect, RuntimeError):
                        pass

                async def vnc_to_browser():
                    async for message in upstream:
                        if await trusted_visual_session(session_id) is None:
                            break
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                async def periodic_guard():
                    while True:
                        await asyncio.sleep(1)
                        if await trusted_visual_session(session_id) is None:
                            break

                tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser()),
                         asyncio.create_task(periodic_guard())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await websocket.close(code=1008)
        except Exception:
            if websocket.application_state.name != "DISCONNECTED":
                await websocket.close(code=1011)

    return app


app = create_app() if all(os.environ.get(name) for name in
                          ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "OWNER_EMAIL", "BROKER_OWNER_TOKEN")) else None
