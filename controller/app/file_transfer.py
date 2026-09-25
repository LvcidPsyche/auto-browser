"""Move files between the live browser session and the caller.

Two directions, both through the session the caller already drives:

* DOWNLOAD -- a file the page produces (a click that starts a browser
  download, an <img>/<video>/<audio>/<a> the caller points at, a blob: or
  data: URL the page built, or a direct link) is captured, checked and parked
  as a *transfer* the caller then pulls with GET /sessions/{id}/files/{tid}.
* UPLOAD -- the caller pushes bytes (PUT /sessions/{id}/files); they are
  checked, parked in the session's own upload folder, and
  POST /sessions/{id}/files/{tid}/attach sets them on the page's file input
  (the input itself, a label/button/drop zone that opens the file chooser, or
  the hidden input a drag-and-drop uploader keeps next to its drop zone).

Every file is typed by its first bytes, never by its name or the server's
Content-Type: images, video, audio and PDF only (no HTML, SVG, archives or
executables), each kind under its own size ceiling. Nothing here ever opens,
renders or executes a file -- only the leading bytes are read to type it, and
it is handed out as an attachment.

In persistent-profile mode (the owner's real browser in browser-node) the
downloads belong to browser-node's own Playwright; they are listed and pulled
over browser-node's authenticated control API (see browser-node/server.mjs).
Otherwise the controller's own download capture (app/downloads.py) has them.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from .action_errors import BrowserActionError
from .persistent_profiles import PersistentProfileError

if TYPE_CHECKING:  # pragma: no cover
    from .browser_manager import BrowserSession

logger = logging.getLogger(__name__)

MB = 1024 * 1024
# Per-kind ceilings; FILE_TRANSFER_MAX_BYTES caps all of them.
KIND_MAX_BYTES = {"image": 50 * MB, "audio": 100 * MB, "pdf": 50 * MB, "video": 200 * MB}
ALLOWED_MIME_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic", "image/avif",
    "video/mp4", "video/quicktime", "video/webm",
    "audio/mpeg", "audio/aac", "audio/mp4", "audio/wav", "audio/ogg", "audio/flac",
    "application/pdf",
})
_EXTENSIONS = {
    "image/jpeg": ("jpg", {"jpg", "jpeg", "jfif"}),
    "image/png": ("png", {"png"}),
    "image/gif": ("gif", {"gif"}),
    "image/webp": ("webp", {"webp"}),
    "image/heic": ("heic", {"heic", "heif"}),
    "image/avif": ("avif", {"avif"}),
    "video/mp4": ("mp4", {"mp4", "m4v"}),
    "video/quicktime": ("mov", {"mov", "qt"}),
    "video/webm": ("webm", {"webm", "mkv"}),
    "audio/mpeg": ("mp3", {"mp3"}),
    "audio/aac": ("aac", {"aac"}),
    "audio/mp4": ("m4a", {"m4a", "m4b", "mp4"}),
    "audio/wav": ("wav", {"wav", "wave"}),
    "audio/ogg": ("ogg", {"ogg", "oga", "opus"}),
    "audio/flac": ("flac", {"flac"}),
    "application/pdf": ("pdf", {"pdf"}),
}
DOWNLOAD_MODES = frozenset({"click", "element", "media", "url", "latest"})
MEDIA_KINDS = frozenset({"any", "image", "video", "audio"})
_SNIFF_BYTES = 64
_MAX_TRANSFERS_PER_SESSION = 40
# A download that has not even started this long after the click/trigger
# means the click did not produce one.
_START_GRACE_SECONDS = 25.0
_LATEST_MAX_AGE_SECONDS = 30 * 60
_POLL_SECONDS = 0.5

# Resolve the file URL an element shows: the element itself, a media/link
# inside it (a card wrapping a picture), or its CSS background image.
_ELEMENT_SOURCE_JS = """
(el) => {
  const pick = (node) => {
    if (!node || !node.tagName) return null;
    const tag = node.tagName.toUpperCase();
    if (tag === 'IMG') return node.currentSrc || node.src || null;
    if (tag === 'VIDEO' || tag === 'AUDIO') {
      if (node.currentSrc || node.src) return node.currentSrc || node.src;
      const source = node.querySelector('source[src]');
      return source ? source.src : null;
    }
    if (tag === 'SOURCE') return node.src || null;
    if (tag === 'A') return node.href || null;
    return null;
  };
  let url = pick(el);
  if (!url && el.querySelector) url = pick(el.querySelector('video, img, audio, a[download], a[href]'));
  if (!url) {
    const background = getComputedStyle(el).backgroundImage || '';
    const match = /url\\(["']?(.*?)["']?\\)/.exec(background);
    if (match) url = new URL(match[1], document.baseURI).href;
  }
  return { url };
}
"""

# The page's main picture/video: the largest visible <video>/<img> (video
# first when any kind will do), or the first <audio> with a source.
_PAGE_MEDIA_JS = """
(kind) => {
  const selector = kind === 'image' ? 'img' : kind === 'video' ? 'video' : kind === 'audio' ? 'audio' : 'video, img';
  let best = null;
  let bestWeight = -1;
  for (const el of document.querySelectorAll(selector)) {
    const source = el.currentSrc || el.src || ((el.querySelector && el.querySelector('source[src]')) || {}).src;
    if (!source) continue;
    if (kind === 'audio') return source;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const rect = el.getBoundingClientRect();
    const area = rect.width * rect.height;
    if (area < 16) continue;
    const weight = el.tagName === 'VIDEO' && kind === 'any' ? area * 4 : area;
    if (weight > bestWeight) { best = source; bestWeight = weight; }
  }
  return best;
}
"""

# Start a real browser download of `url` from inside the page/frame: same
# origin, blob: and data: go straight through an <a download>; another origin
# is fetched with the page's own credentials into a blob first (a plain link
# there would navigate instead of download).
_TRIGGER_JS = """
async (el, args) => {
  const { url, name } = args;
  const clickLink = (href) => {
    const a = document.createElement('a');
    a.href = href; a.download = name || ''; a.rel = 'noopener'; a.style.display = 'none';
    (document.body || document.documentElement).appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 2000);
  };
  let target;
  try { target = new URL(url, document.baseURI); } catch (e) { return { ok: false, reason: 'bad_url' }; }
  if (target.protocol === 'blob:' || target.protocol === 'data:' || target.origin === location.origin) {
    clickLink(target.href);
    return { ok: true, via: 'link' };
  }
  for (const credentials of ['include', 'omit']) {
    try {
      const response = await fetch(target.href, { credentials });
      if (!response.ok) return { ok: false, reason: 'http_' + response.status };
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      clickLink(href);
      setTimeout(() => URL.revokeObjectURL(href), 120000);
      return { ok: true, via: 'fetch' };
    } catch (e) { /* CORS: try without credentials, then give up */ }
  }
  return { ok: false, reason: 'cors' };
}
"""

# From a target that is not itself a file input: its label's control, or a
# file input kept inside the nearest few ancestors (drag-and-drop zones hide one).
_NEARBY_INPUT_JS = """
(el) => {
  const isFile = (n) => !!n && n.tagName === 'INPUT' && (n.type || '').toLowerCase() === 'file';
  if (isFile(el)) return el;
  if (el.tagName === 'LABEL') {
    if (isFile(el.control)) return el.control;
    if (el.htmlFor) { const t = document.getElementById(el.htmlFor); if (isFile(t)) return t; }
  }
  let node = el;
  for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
    const found = node.querySelector && node.querySelector('input[type=file]');
    if (found) return found;
  }
  return null;
}
"""


def sniff(head: bytes) -> tuple[str, str] | None:
    """(mime type, kind) from a file's leading bytes, or None when it is not
    one of the allowed media/PDF types."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav", "audio"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}:
            return "image/heic", "image"
        if brand in {b"avif", b"avis"}:
            return "image/avif", "image"
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return "audio/mp4", "audio"
        if brand == b"qt  ":
            return "video/quicktime", "video"
        return "video/mp4", "video"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "video/webm", "video"
    if head.startswith(b"ID3"):
        return "audio/mpeg", "audio"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return "audio/aac", "audio"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg", "audio"
    if head[:4] == b"OggS":
        return "audio/ogg", "audio"
    if head[:4] == b"fLaC":
        return "audio/flac", "audio"
    if head[:5] == b"%PDF-":
        return "application/pdf", "pdf"
    return None


def safe_filename(name: str | None, mime_type: str) -> str:
    """A plain display name whose extension matches what the bytes really are
    (a JPEG named `x.html` becomes `x.jpg`)."""
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"/\\|?*').strip(" .")
    stem, dot, extension = base.rpartition(".")
    if not dot:
        stem, extension = base, ""
    wanted, accepted = _EXTENSIONS.get(mime_type, ("bin", set()))
    if extension.lower() not in accepted:
        extension = wanted
    stem = stem.strip(" .")[:100] or "file"
    return f"{stem}.{extension.lower()}"


def kind_limit(kind: str, overall: int) -> int:
    return min(KIND_MAX_BYTES.get(kind, overall), overall)


def _error(code: str, message: str, status: int, *, action: str, **details: Any) -> BrowserActionError:
    return BrowserActionError(message, code=code, action=action, status_code=status, retryable=False, details=details)


def _host_of(url: str | None) -> str | None:
    try:
        return urlparse(url or "").hostname
    except ValueError:
        return None


class FileTransferService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        # session id -> transfer id -> record (with its private `path`).
        self._records: dict[str, dict[str, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ util

    @property
    def max_bytes(self) -> int:
        return int(getattr(self.manager.settings, "file_transfer_max_bytes", 200 * MB))

    def _session_records(self, session_id: str) -> dict[str, dict[str, Any]]:
        return self._records.setdefault(session_id, {})

    @staticmethod
    def public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "path"}

    def get(self, session_id: str, transfer_id: str) -> dict[str, Any]:
        record = self._records.get(session_id, {}).get(transfer_id or "")
        if record is None or not Path(record["path"]).is_file():
            raise _error("file_unknown_transfer", "Unknown or expired file transfer", 404, action="file")
        return record

    def _transfer_dir(self, session: BrowserSession, direction: str) -> tuple[str, Path]:
        transfer_id = secrets.token_hex(12)
        root = (session.artifact_dir if direction == "download" else session.upload_dir) / "transfers"
        folder = root / transfer_id
        folder.mkdir(parents=True, exist_ok=True)
        # browser-node's Chromium (another uid on the shared volume) must be
        # able to read a pushed upload: world-readable, never executable.
        for path in (root, folder):
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        self._sweep(root)
        return transfer_id, folder

    def _sweep(self, root: Path) -> None:
        retention = float(getattr(self.manager.settings, "file_transfer_retention_hours", 6.0)) * 3600
        cutoff = time.time() - retention
        try:
            for child in root.iterdir():
                try:
                    if child.is_dir() and child.stat().st_mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            return

    def _register(self, session_id: str, record: dict[str, Any]) -> dict[str, Any]:
        records = self._session_records(session_id)
        records[record["id"]] = record
        while len(records) > _MAX_TRANSFERS_PER_SESSION:
            oldest = min(records.values(), key=lambda item: item["created_at"])
            records.pop(oldest["id"], None)
            shutil.rmtree(Path(oldest["path"]).parent, ignore_errors=True)
        return self.public(record)

    def _finalize(
        self, *, transfer_id: str, raw: Path, direction: str, name: str | None, action: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Type the file by its bytes, enforce its kind's ceiling, give it a
        safe name and register it. Deletes it on any refusal."""
        try:
            size = raw.stat().st_size
            if size == 0:
                raise _error("file_empty", "The file is empty", 422, action=action)
            with raw.open("rb") as handle:
                head = handle.read(_SNIFF_BYTES)
            typed = sniff(head)
            if typed is None:
                raise _error(
                    "file_unsupported_type",
                    "Only images, video, audio and PDF files can be transferred",
                    415, action=action,
                )
            mime_type, kind = typed
            limit = kind_limit(kind, self.max_bytes)
            if size > limit:
                raise _error(
                    "file_too_large", "The file is larger than allowed for its type", 413,
                    action=action, max_bytes=limit, size_bytes=size, kind=kind,
                )
            digest = hashlib.sha256()
            with raw.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            filename = safe_filename(name, mime_type)
            final = raw.with_name(filename)
            if final != raw:
                os.replace(raw, final)
            try:
                os.chmod(final, 0o644)
            except OSError:
                pass
        except BrowserActionError:
            shutil.rmtree(raw.parent, ignore_errors=True)
            raise
        except OSError:
            shutil.rmtree(raw.parent, ignore_errors=True)
            raise _error("file_transfer_failed", "Could not store the file", 500, action=action) from None
        record = {
            "id": transfer_id,
            "direction": direction,
            "filename": filename,
            "mime_type": mime_type,
            "kind": kind,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "created_at": time.time(),
            "source": source or {},
            "path": str(final),
        }
        return record

    # -------------------------------------------------------------- download

    async def download(
        self,
        session_id: str,
        *,
        mode: str,
        selector: str | None = None,
        element_id: str | None = None,
        url: str | None = None,
        media_kind: str = "any",
        timeout_seconds: float | None = None,
        pace: str = "human",
    ) -> dict[str, Any]:
        action = "download_file"
        if mode not in DOWNLOAD_MODES or media_kind not in MEDIA_KINDS:
            raise _error("file_bad_request", "Unknown download mode", 400, action=action)
        session = await self.manager.get_session(session_id)
        timeout = float(timeout_seconds or self.manager.settings.file_transfer_download_timeout_seconds)
        timeout = max(5.0, min(timeout, 600.0))

        if mode == "latest":
            found = await self._latest_download(session)
            if found is None:
                raise _error("file_no_download", "No finished download in this browser session yet", 404, action=action)
            return await self._take_download(session, found, action=action, mode=mode)

        if mode in {"click", "element"} and not (selector or element_id):
            raise _error("file_bad_request", f"{mode} mode needs element_id or selector", 400, action=action)
        if mode == "url":
            url = (url or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                raise _error("file_bad_request", "url mode takes an http(s) link", 400, action=action)
            self._check_page_url(url)

        marker = await self._download_marker(session)
        if mode == "click":
            await self.manager.click(session_id, selector=selector, element_id=element_id, pace=pace)
        else:
            triggered = await self._trigger(
                session, mode=mode, selector=selector, element_id=element_id, url=url, media_kind=media_kind,
            )
            if not triggered["ok"]:
                fetch_url = str(triggered.get("url") or "")
                reason = str(triggered.get("reason") or "")
                if reason == "cors" and fetch_url.lower().startswith(("http://", "https://")):
                    # The page may not read that other site's file; fetch it here.
                    return await self._controller_fetch(session, fetch_url, action=action, timeout=timeout)
                raise _error(
                    "file_download_failed", "The page could not hand over this file", 502,
                    action=action, reason=reason[:40],
                )
        found = await self._wait_new_download(session, marker, timeout=timeout, action=action)
        return await self._take_download(session, found, action=action, mode=mode)

    async def _trigger(
        self, session: BrowserSession, *, mode: str, selector: str | None, element_id: str | None,
        url: str | None, media_kind: str,
    ) -> dict[str, Any]:
        """Start a real browser download of what the element / page shows."""
        action = "download_file"
        outcome: dict[str, Any] = {}
        if mode == "element":
            target = self.manager.actions.resolve_target(selector=selector, element_id=element_id)
        else:
            target = {"mode": "page", "media_kind": media_kind} if mode == "media" else {"mode": "page"}

        async def operation() -> None:
            page = session.page
            if mode == "element":
                locator = page.locator(target["selector"]).first
                source = await locator.evaluate(_ELEMENT_SOURCE_JS)
                source_url = source.get("url") if isinstance(source, dict) else None
            elif mode == "media":
                locator = None
                source_url = await page.evaluate(_PAGE_MEDIA_JS, media_kind)
            else:
                locator = None
                source_url = url
            if not isinstance(source_url, str) or not source_url:
                outcome.update(ok=False, reason="no_source")
                return
            if mode != "url":
                self._check_page_url(source_url)
            outcome["url"] = source_url
            if locator is not None:
                result = await locator.evaluate(_TRIGGER_JS, {"url": source_url, "name": ""})
            else:
                result = await page.evaluate(
                    "([u, n]) => (" + _TRIGGER_JS.strip() + ")(document.body, {url: u, name: n})", [source_url, ""]
                )
            if isinstance(result, dict):
                outcome.update({key: result[key] for key in ("ok", "reason", "via") if key in result})

        await self.manager._run_action(session, "download", target, operation)
        if outcome.get("reason") == "no_source":
            raise _error(
                "file_source_not_found",
                "No image, video, audio or file link found there" if mode != "media" else "No picture or video on this page",
                404, action=action,
            )
        outcome.setdefault("ok", False)
        return outcome

    def _check_page_url(self, url: str) -> None:
        scheme = (urlparse(url).scheme or "").lower()
        if scheme in {"blob", "data"}:
            return
        if scheme not in {"http", "https"}:
            raise _error("file_bad_request", "Unsupported link type", 400, action="download_file")
        try:
            self.manager._assert_url_allowed(url)
        except PermissionError:
            raise _error("file_fetch_blocked", "That file's address is not allowed", 403, action="download_file") from None

    async def _download_marker(self, session: BrowserSession) -> Any:
        if session.persistent_profile_name:
            records = await self._node_list(session, after_seq=0)
            return max((int(item.get("seq") or 0) for item in records), default=0)
        return {str(item.get("id")) for item in session.downloads}

    async def _node_list(self, session: BrowserSession, *, after_seq: int) -> list[dict[str, Any]]:
        try:
            return await self.manager.persistent_profiles.list_downloads(
                session.persistent_profile_name, after_seq=after_seq
            )
        except PersistentProfileError:
            raise _error("file_transfer_failed", "The browser could not list its downloads", 502, action="download_file") from None

    async def _wait_new_download(
        self, session: BrowserSession, marker: Any, *, timeout: float, action: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        seen_start = False
        while True:
            if session.persistent_profile_name:
                records = await self._node_list(session, after_seq=int(marker))
                records.sort(key=lambda item: int(item.get("seq") or 0))
                if records:
                    seen_start = True
                finished = [item for item in records if item.get("state") != "in_progress"]
                if finished:
                    return {"source": "node", **finished[0]}
            else:
                fresh = [item for item in session.downloads if str(item.get("id")) not in marker]
                if fresh:
                    return {"source": "local", **fresh[0]}
            elapsed = time.monotonic() - started
            if not seen_start and elapsed > min(_START_GRACE_SECONDS, timeout):
                raise _error("file_no_download", "No download started", 404, action=action)
            if elapsed > timeout:
                raise _error("file_download_timeout", "The download did not finish in time", 504, action=action)
            await asyncio.sleep(_POLL_SECONDS)

    async def _latest_download(self, session: BrowserSession) -> dict[str, Any] | None:
        now = time.time()
        if session.persistent_profile_name:
            records = await self._node_list(session, after_seq=0)
            done = [
                item for item in records
                if item.get("state") == "completed" and now - float(item.get("finished_at") or 0) < _LATEST_MAX_AGE_SECONDS
            ]
            if not done:
                return None
            return {"source": "node", **max(done, key=lambda item: int(item.get("seq") or 0))}
        done = [item for item in session.downloads if item.get("status") == "completed"]
        return {"source": "local", **done[-1]} if done else None

    async def _take_download(
        self, session: BrowserSession, found: dict[str, Any], *, action: str, mode: str,
    ) -> dict[str, Any]:
        """Copy one finished download into a new transfer."""
        if found["source"] == "node":
            state = found.get("state")
            if state == "too_large":
                raise _error("file_too_large", "The download was larger than allowed", 413, action=action,
                             max_bytes=self.max_bytes)
            if state != "completed":
                raise _error("file_download_failed", "The download failed in the browser", 502, action=action,
                             reason=str(found.get("failure") or state or "")[:60])
            name = found.get("suggested_filename")
        else:
            if found.get("status") != "completed":
                raise _error("file_download_failed", "The download failed in the browser", 502, action=action,
                             reason=str(found.get("failure") or "")[:60])
            name = found.get("suggested_filename") or found.get("filename")
        transfer_id, folder = self._transfer_dir(session, "download")
        raw = folder / ".incoming"
        try:
            if found["source"] == "node":
                await self.manager.persistent_profiles.fetch_download(str(found.get("id")), raw, max_bytes=self.max_bytes)
            else:
                await asyncio.to_thread(shutil.copyfile, found["path"], raw)
        except PersistentProfileError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            if exc.status_code == 413:
                raise _error("file_too_large", "The download was larger than allowed", 413, action=action,
                             max_bytes=self.max_bytes) from None
            raise _error("file_transfer_failed", "Could not collect the download", 502, action=action) from None
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise _error("file_transfer_failed", "Could not collect the download", 502, action=action) from None
        if found["source"] == "node":
            await self.manager.persistent_profiles.delete_download(str(found.get("id")))
        record = await asyncio.to_thread(
            self._finalize, transfer_id=transfer_id, raw=raw, direction="download", name=name, action=action,
            source={"mode": mode, "host": _host_of(found.get("url") or found.get("source_url"))},
        )
        await self._audit(session, "file_downloaded", record)
        return self._register(session.id, record)

    async def _controller_fetch(
        self, session: BrowserSession, url: str, *, action: str, timeout: float,
    ) -> dict[str, Any]:
        """Last resort when the page itself may not read a cross-origin file
        (no CORS): fetch it with the session's own cookies from here. Public
        addresses only, every redirect re-checked, size capped."""
        current = url
        response = None
        for _hop in range(5):
            self._assert_public_fetch(current)
            await self.manager._assert_url_resolves_public(current)
            response = await session.context.request.get(
                current, max_redirects=0, timeout=timeout * 1000, fail_on_status_code=False,
            )
            location = response.headers.get("location")
            if 300 <= response.status < 400 and location:
                current = urljoin(current, location)
                await response.dispose()
                continue
            break
        else:
            raise _error("file_download_failed", "Too many redirects", 502, action=action)
        assert response is not None
        try:
            if not response.ok:
                raise _error("file_download_failed", "The site refused the file", 502, action=action,
                             reason=f"http_{response.status}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_bytes:
                raise _error("file_too_large", "The file is larger than allowed", 413, action=action,
                             max_bytes=self.max_bytes)
            body = await response.body()
        finally:
            await response.dispose()
        if len(body) > self.max_bytes:
            raise _error("file_too_large", "The file is larger than allowed", 413, action=action, max_bytes=self.max_bytes)
        transfer_id, folder = self._transfer_dir(session, "download")
        raw = folder / ".incoming"
        await asyncio.to_thread(raw.write_bytes, body)
        name = PurePosixPath(urlparse(current).path).name
        record = await asyncio.to_thread(
            self._finalize, transfer_id=transfer_id, raw=raw, direction="download", name=name, action=action,
            source={"mode": "fetch", "host": _host_of(current)},
        )
        await self._audit(session, "file_downloaded", record)
        return self._register(session.id, record)

    def _assert_public_fetch(self, url: str) -> None:
        """The controller itself makes this request, so it must never reach
        anything inside the stack (controller, browser-node, the broker)."""
        try:
            self.manager._assert_url_allowed(url)
        except PermissionError:
            raise _error("file_fetch_blocked", "That file's address is not allowed", 403, action="download_file") from None
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip("[]").lower()
        if parsed.scheme not in {"http", "https"} or not host or host == "localhost" or "." not in host and ":" not in host:
            raise _error("file_fetch_blocked", "That file's address is not allowed", 403, action="download_file")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise _error("file_fetch_blocked", "That file's address is not allowed", 403, action="download_file")

    # ---------------------------------------------------------------- upload

    async def receive_upload(
        self, session_id: str, *, filename: str | None, chunks: AsyncIterator[bytes], declared_length: int | None,
    ) -> dict[str, Any]:
        action = "upload_file"
        session = await self.manager.get_session(session_id)
        if declared_length is not None and declared_length > self.max_bytes:
            raise _error("file_too_large", "The file is larger than allowed", 413, action=action, max_bytes=self.max_bytes)
        transfer_id, folder = self._transfer_dir(session, "upload")
        raw = folder / ".incoming"
        total = 0
        try:
            with raw.open("wb") as handle:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise _error("file_too_large", "The file is larger than allowed", 413, action=action,
                                     max_bytes=self.max_bytes)
                    await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        record = await asyncio.to_thread(
            self._finalize, transfer_id=transfer_id, raw=raw, direction="upload", name=filename, action=action,
        )
        await self._audit(session, "file_received", record)
        return self._register(session.id, record)

    async def attach(
        self, session_id: str, transfer_id: str, *, selector: str | None = None, element_id: str | None = None,
    ) -> dict[str, Any]:
        """Set a pushed file on the page: the targeted file input; a label,
        button or drop zone that opens the file chooser; the file input
        hidden next to a drop zone; or -- with no target -- the page's only
        (or only matching) file input."""
        action = "upload_file"
        session = await self.manager.get_session(session_id)
        record = self.get(session.id, transfer_id)
        if record["direction"] != "upload":
            raise _error("file_unknown_transfer", "Unknown or expired file transfer", 404, action=action)
        path = record["path"]
        target: dict[str, Any] = (
            self.manager.actions.resolve_target(selector=selector, element_id=element_id)
            if (selector or element_id) else {"mode": "page"}
        )
        method: dict[str, str] = {}

        async def operation() -> None:
            page = session.page
            if target.get("selector"):
                locator = page.locator(target["selector"]).first
                handle = await locator.element_handle(timeout=10_000)
                if handle is None:
                    raise _error("file_no_input", "Target element not found", 404, action=action)
                is_input = await handle.evaluate(
                    "(el) => el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'file'"
                )
                if is_input:
                    await handle.set_input_files(path)
                    method["via"] = "input"
                    return
                is_label = await handle.evaluate("(el) => el.tagName === 'LABEL'")
                if is_label:
                    nearby = (await handle.evaluate_handle(_NEARBY_INPUT_JS)).as_element()
                    if nearby is not None:
                        await nearby.set_input_files(path)
                        method["via"] = "label"
                        return
                try:
                    async with page.expect_file_chooser(timeout=6_000) as chooser_info:
                        await locator.click()
                    chooser = await chooser_info.value
                    await chooser.set_files(path)
                    method["via"] = "file_chooser"
                    return
                except Exception as exc:  # no chooser opened -- look for the hidden input
                    logger.debug("no file chooser after clicking upload target: %s", exc)
                nearby = (await handle.evaluate_handle(_NEARBY_INPUT_JS)).as_element()
                if nearby is not None:
                    await nearby.set_input_files(path)
                    method["via"] = "nearby_input"
                    return
            chosen = await self._page_file_input(page, record["mime_type"])
            if chosen is None:
                raise _error(
                    "file_no_input",
                    "Could not find where to put the file on this page; point at the upload button or drop zone",
                    404, action=action,
                )
            await chosen.set_input_files(path)
            method["via"] = "page_input"

        result = await self.manager._run_action(
            session, "upload", {**target, "transfer_id": record["id"], "filename": record["filename"]}, operation,
        )
        await self._audit(session, "file_attached", record, via=method.get("via"))
        return {**result, "transfer": self.public(record), "via": method.get("via")}

    @staticmethod
    async def _page_file_input(page: Any, mime_type: str) -> Any:
        """The page's (or its frames') only file input, or the only one whose
        `accept` takes this type."""
        candidates: list[tuple[Any, str]] = []
        for frame in page.frames:
            try:
                handles = await frame.query_selector_all("input[type=file]")
            except Exception:
                continue
            for handle in handles:
                try:
                    accept = await handle.get_attribute("accept") or ""
                    disabled = await handle.evaluate("(el) => el.disabled")
                except Exception:
                    continue
                if not disabled:
                    candidates.append((handle, accept))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0]
        family = mime_type.split("/", 1)[0]
        extension = _EXTENSIONS.get(mime_type, ("", set()))[0]

        def accepts(accept: str) -> bool:
            tokens = [token.strip().lower() for token in accept.split(",") if token.strip()]
            if not tokens:
                return False
            return any(
                token in {mime_type, f"{family}/*", f".{extension}"} for token in tokens
            )

        matching = [handle for handle, accept in candidates if accepts(accept)]
        return matching[0] if len(matching) == 1 else None

    # ---------------------------------------------------------------- common

    async def delete(self, session_id: str, transfer_id: str) -> dict[str, Any]:
        record = self._records.get(session_id, {}).pop(transfer_id or "", None)
        if record is None:
            raise _error("file_unknown_transfer", "Unknown or expired file transfer", 404, action="file")
        shutil.rmtree(Path(record["path"]).parent, ignore_errors=True)
        return {"deleted": True, "id": transfer_id}

    def forget_session(self, session_id: str) -> None:
        self._records.pop(session_id, None)

    async def _audit(self, session: BrowserSession, event: str, record: dict[str, Any], **extra: Any) -> None:
        try:
            await self.manager.audit.append(
                event_type=event,
                status="ok",
                action=event,
                session_id=session.id,
                details={
                    "transfer_id": record["id"],
                    "mime_type": record["mime_type"],
                    "size_bytes": record["size_bytes"],
                    "sha256": record["sha256"],
                    "host": (record.get("source") or {}).get("host"),
                    **extra,
                },
            )
        except Exception as exc:  # the audit trail must never break the transfer
            logger.warning("file transfer audit failed for session %s: %s", session.id, exc)
