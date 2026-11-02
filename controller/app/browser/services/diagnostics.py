from __future__ import annotations

import asyncio
import codecs
import logging
import mimetypes
import shutil
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from ...action_errors import SessionNotFoundError
from ...downloads import DownloadCaptureService
from ...network_inspector import scrub_url
from ...pii_scrub import PiiScrubber
from ...utils import UTC, spawn_background_task
from ..page_provenance import is_http, redirect_chain

logger = logging.getLogger(__name__)

# Larger downloads are fetched from their artifact URL, not read into a model's context.
DOWNLOAD_READ_MAX_BYTES = 10 * 1024 * 1024

# A fixed type table, not the host's: mimetypes.guess_type reads /etc/mime.types
# or the Windows registry, and Windows with Office installed maps .csv to
# application/vnd.ms-excel, so every CSV download read as binary there. Python's
# built-in table, plus the Office formats it lacks.
_MIME_TYPES = mimetypes.MimeTypes()
for _type, _extension in (
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
):
    _MIME_TYPES.add_type(_type, _extension)
# Console text is written by the page and kept for the whole session, and every
# read scrubbed all of it on the event loop. One message is cut to this length.
CONSOLE_TEXT_MAX_CHARS = 8_000


def _top_level_navigation(request: Any) -> bool:
    """Whether a request navigates its page's main frame (a frame Playwright cannot name counts)."""
    if not request.is_navigation_request() or request.resource_type != "document":
        return False
    try:
        return request.frame.parent_frame is None
    except PlaywrightError:
        return True


def _unreported_page_download_refused(session: Any, url: str) -> bool:
    """Whether a download at ``url`` is where a navigation through a refused URL of a page the context has
    not reported went: Playwright reports such a page's download on its opener, whose own verdict says
    nothing about it. That navigation is still kept (the download came first) or has failed."""
    return url in session.refused_failures or any(hop.url == url for hop in session.unframed_hops)


def checked_screenshot(session: Any) -> dict[str, str] | None:
    """The last screenshot a checked read returned (capture_session_screenshot), if it is still on disk."""
    shot = getattr(session, "last_screenshot", None)
    return shot if shot and Path(shot["path"]).is_file() else None


def console_entry(message: Any) -> dict[str, Any]:
    text = message.text
    if len(text) > CONSOLE_TEXT_MAX_CHARS:
        text = f"{text[:CONSOLE_TEXT_MAX_CHARS]}… [{len(text) - CONSOLE_TEXT_MAX_CHARS} more characters]"
    return {"type": message.type, "text": text, "location": message.location}


if TYPE_CHECKING:
    from playwright.async_api import Page

    from ...browser_manager import BrowserSession


class BrowserDiagnosticsService:
    """Encapsulates diagnostics helpers and download persistence hooks."""

    def __init__(self, manager: Any, pii_scrubber: PiiScrubber, download_capture: DownloadCaptureService) -> None:
        self.manager = manager
        self.pii_scrubber = pii_scrubber
        self.download_capture = download_capture
        # Whether each request's page was refused when the request started: a failure is
        # delivered later, when the page may have moved on to an allowed URL.
        self._started_refused: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()

    async def get_console_messages(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            messages = session.console_messages[-limit:]
            if self.pii_scrubber.console_enabled:
                # Off the event loop: the text is the page's, and a slow scrub
                # here stalled every session the controller was running.
                messages, hits = await asyncio.to_thread(self.pii_scrubber.console, messages)
                if hits and self.pii_scrubber.audit_report:
                    await self.manager.audit.append(
                        event_type="pii_redaction",
                        status="ok",
                        action="console_scrub",
                        session_id=session_id,
                        details=self.pii_scrubber.build_audit_report(session_id, "console", hits),
                    )
            return {
                "session": await self.manager._session_summary(session),
                "items": messages,
            }

    async def get_page_errors(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.page_errors[-limit:],
            }

    async def get_request_failures(self, session_id: str, *, limit: int = 20) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            return {
                "session": await self.manager._session_summary(session),
                "items": session.request_failures[-limit:],
            }

    async def get_network_log(
        self,
        session_id: str,
        *,
        limit: int = 100,
        method: str | None = None,
        url_contains: str | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            inspector = session.network_inspector
            if inspector is None:
                return {
                    "session": await self.manager._session_summary(session),
                    "enabled": False,
                    "entries": [],
                    "summary": {},
                }
            return {
                "session": await self.manager._session_summary(session),
                "enabled": True,
                "entries": inspector.entries(limit=limit, method=method, url_contains=url_contains),
                "summary": inspector.summary(),
            }

    def attach_popup(self, page: "Page", session: "BrowserSession") -> None:
        """Attach to a page the context reports, refused until its opener has judged it.

        A blank window a refused page opens is on about:blank, same-origin with that page
        and scriptable by it, so it is judged by its opener (see PageProvenance). Playwright
        emits the opener's "popup" event right after this one, synchronously, and only while
        the opener is open (_on_popup records the verdict then). A page that gets no verdict,
        and that the controller did not open itself (tabs.open), stays refused. A first navigation
        that went through a refused hop, which only the context saw, taints it too (_claim_unframed_hops).
        """
        self.manager.page_provenance.opener_lookup_started(page)
        self._claim_unframed_hops(session, page)
        self.attach_page_listeners(page, session)
        if getattr(session, "gate_context_id", None) is not None:
            # From its next navigation on: Playwright reports a popup only once its first has started.
            spawn_background_task(self.manager.request_gates.bypass_service_workers(page))

    def watch_context(self, context: Any, session: "BrowserSession") -> None:
        """Listen to a session's browser context: the pages it reports, and the first navigation of a
        popup it has not reported yet, which reaches only the context. Each session keeps its own hops
        (session.unframed_hops), so one context's pages never decide about another's."""
        context.on("page", lambda popup: self.manager._attach_popup(popup, session))
        context.on("request", lambda request: self.on_context_request(session, request))
        context.on("requestfailed", lambda request: self.on_context_request_failed(session, request))

    def on_context_request(self, session: "BrowserSession", request: Any) -> None:
        """A context's request: keep a top-level navigation through a URL off the list (to one, or redirected
        from one) whose frame is not available yet.

        The redirects are kept too: the download such a navigation turns into is reported on the page's
        opener, by its final URL, and can come before the navigation's failure (_on_download).
        """
        if not (request.is_navigation_request() and request.resource_type == "document"):
            return
        if not any(self.manager._url_off_list(hop.url) for hop in redirect_chain(request)):
            return
        try:
            request.frame
        except PlaywrightError:
            session.unframed_hops.append(request)

    def on_context_request_failed(self, session: "BrowserSession", request: Any) -> None:
        """A context's failed request: a navigation that failed commits nothing, so its kept hops go.

        The URL of a kept one is remembered first (session.refused_failures), for the download it may
        have turned into. Not on requestfinished: Chromium reports a redirect hop finished when it
        redirects, before the popup it leads to is reported.
        """
        chain = list(redirect_chain(request))
        if any(hop is failed for hop in session.unframed_hops for failed in chain):
            session.refused_failures.append(request.url)
        session.unframed_hops = [hop for hop in session.unframed_hops if not any(hop is failed for failed in chain)]

    def _claim_unframed_hops(self, session: "BrowserSession", page: "Page") -> None:
        """Mark a page the context reports with a kept hop whose frame is now its main frame.

        Its first commit was that refused hop's choice: attach_page_listeners replays it, tainted. A kept
        hop goes once it is claimed, or once it resolves to a page attached already (whose own listeners
        saw it). One that does not resolve yet, or resolves to a page not reported yet, is kept: two
        popups can open together, and the first reported must not drop the second's hop.
        """
        kept = []
        for hop in session.unframed_hops:
            try:
                frame = hop.frame
            except PlaywrightError:
                kept.append(hop)
                continue
            if frame.page is page:
                if frame.parent_frame is None:
                    self.manager.page_provenance.refused_hop_started(page, hop)
                continue
            if frame.page not in session.attached_pages:
                kept.append(hop)
        session.unframed_hops = kept

    def _on_popup(self, session: "BrowserSession", opener: "Page", popup: "Page") -> None:
        # The opener as it is when the popup opens: it can clear its own taint, or close,
        # before any later look at it, and the popup holds what it wrote either way.
        if self.manager.page_provenance.opener_judged(popup, opener_refused=self.manager._page_refused(opener)):
            spawn_background_task(self._return_to_blank(session, popup))

    def attach_page_listeners(self, page: "Page", session: "BrowserSession") -> None:
        if not hasattr(page, "on"):
            return
        if page in session.attached_pages:
            return
        session.attached_pages.add(page)

        page.on("console", lambda message: self._keep_output(page, session.console_messages, console_entry(message)))
        page.on("pageerror", lambda error: self._keep_output(page, session.page_errors, str(error)))
        page.on("request", lambda request: self._on_request_started(session, page, request))
        page.on("requestfailed", lambda request: self._record_request_failure(session, page, request))
        page.on("download", lambda download: self._on_download(session, page, download))
        page.on("popup", lambda popup: self._on_popup(session, page, popup))
        page.on("close", lambda _page: self._on_page_closed(session, page))
        page.on("framenavigated", lambda frame: self._on_frame_navigated(session, page, frame))
        # Playwright reports a popup only once its first navigation has committed, so the
        # listener above never saw where it opened: hold the page to the list where it is now.
        if hasattr(page, "main_frame"):
            self._on_frame_navigated(session, page, page.main_frame, reported=True)

    def _keep_output(self, page: "Page", items: list[Any], value: Any) -> None:
        """Keep a console message or page error, unless ALLOWED_HOSTS refuses its page (_page_refused).

        Both lists go into every observation, so a popup opened onto a disallowed host, or a
        page that moved itself there, reached the agent through its console and its errors
        until the page guard had sent it back to about:blank.
        """
        if self.manager._page_refused(page):
            return
        self._bounded_append(items, value)

    def _on_download(self, session: "BrowserSession", page: "Page", download: Any) -> None:
        # The page is judged while the event is delivered: by the time the download
        # is handled, the page guard may have sent it back to about:blank.
        # Or when it comes from a navigation through a refused URL whose failure, delivered first,
        # cleared the refused hop: the refused host chose its URL, which the record keeps.
        url = getattr(download, "url", "") or ""
        page_refused = (
            self.manager._page_refused(page)
            or self.manager.page_provenance.download_refused(page, url)
            or _unreported_page_download_refused(session, url)
        )
        spawn_background_task(self.manager._handle_download(session, download, page_refused=page_refused))

    def _on_request_started(self, session: "BrowserSession", page: "Page", request: Any) -> None:
        if _top_level_navigation(request):
            if self.manager._url_off_list(request.url):
                # The commit that follows is the refused host's choice, whatever its URL (a redirect).
                self.manager.page_provenance.refused_hop_started(page, request)
            # A refused page that caused this navigation can close before its commit is delivered. The
            # page's own taint is left out: its commits stay refused while it lasts (PageProvenance.committed).
            self.manager.page_provenance.navigation_started(
                page, request, session_refused=self.manager._session_has_refused_page(session, besides=page)
            )
        self._started_refused[request] = self.manager._page_refused(page)

    def _record_request_failure(self, session: "BrowserSession", page: "Page", request: Any) -> None:
        """Keep a failed request, its URL scrubbed as the network inspector scrubs the URL of a request.

        The list is served to the agent and goes into every observation, and a URL carries secrets in its
        query: the inspector's own scrub applies (query and fragment, the same size cap, and nothing
        scrubbed while PII_SCRUB_NETWORK is off), whatever the NETWORK_INSPECTOR_* settings say. A page
        ALLOWED_HOSTS refuses keeps none, as with its console messages: a failure is not listed when
        its page was refused as the request started, or is when the failure is delivered, or when it
        is a top-level navigation to a URL off the list (whose start may not have been seen).
        """
        top_level = _top_level_navigation(request)
        if top_level:
            # Remembered first, for the download it may have turned into: navigation_failed clears the hop.
            if any(self.manager._url_off_list(hop.url) for hop in redirect_chain(request)):
                self.manager.page_provenance.refused_navigation_failed(page, request.url)
            # Failed or aborted (a 204, a download) with no commit: that navigation's hop is no longer marked.
            self.manager.page_provenance.navigation_failed(page, request)
        started_refused = self._started_refused.pop(request, False)
        if (
            started_refused
            or self.manager._page_refused(page)
            or (top_level and self.manager._url_off_list(request.url))
        ):
            return
        url, _ = scrub_url(request.url, self.pii_scrubber, self.manager.settings.network_inspector_body_max_bytes)
        self._bounded_append(
            session.request_failures,
            {"url": url, "method": request.method, "failure": str(request.failure) if request.failure else None},
        )

    def _on_frame_navigated(
        self, session: "BrowserSession", page: "Page", frame: Any, *, reported: bool = False
    ) -> None:
        """Send a page that has moved itself off ALLOWED_HOSTS back to about:blank.

        The list is checked where the controller moves a page, so a page that
        navigates itself afterwards (a timer, a late redirect, a popup's own
        script) stayed on the disallowed host and the next observe returned its
        content. Only the top-level frame counts: iframes and subresources from
        other hosts are not filtered. Playwright's route() is not used for this,
        since it would put a Python round trip on every subresource request.
        A page that committed a refused URL stays tainted until it commits an
        allowed one, and a later commit of a tainted page goes back too (a
        refused opener can write into a blank window again).

        A top-level commit while a page of the session can run a refused host's
        code (manager._session_has_refused_page) is refused as well: a refused
        page can navigate another tab it holds (its opener) to
        about:blank, which then has its origin, and write into it, or to an
        allowed URL whose path and query carry what it read. That also refuses
        a tab's own navigations (the agent's clicks among them) while a refused
        tab is open; the agent's navigate clears it, and closing the refused tab
        stops it, though not for a navigation that started while it was open or
        within CLOSED_PAGE_GRACE_SECONDS of its close, nor for a commit made
        within that time (PageProvenance.navigation_started). Such a commit of
        a blank, blob: or data: document is a refused commit, as the refused
        tab's own would be: the document has the
        origin of the page that made it, which can be the refused one. Not the
        commit replayed for a page the context just ``reported`` (its opener
        judges that one), nor the controller's own blank (committed() sees that
        first).
        """
        if frame.parent_frame is not None:
            return
        url_refused = self.manager._url_off_list(frame.url)
        session_refused = (
            not url_refused
            and not reported
            and (
                self.manager._session_has_refused_page(session)
                or self.manager.page_provenance.started_while_refused(page)
            )
        )
        on_http = is_http(frame.url)
        provenance = self.manager.page_provenance
        # During the controller's navigation or escape of the page: whether this commit may be another page's choice.
        another_page_counted = provenance.recording_commits(page) and (
            self.manager._session_has_refused_page(session, besides=page) or provenance.started_while_refused(page)
        )
        if provenance.committed(
            page,
            frame.url,
            url_refused=url_refused or (session_refused and not on_http),
            by_another_page=session_refused and on_http,
            another_page_counted=another_page_counted,
        ):
            spawn_background_task(self._return_to_blank(session, page))

    async def _return_to_blank(self, session: "BrowserSession", page: "Page") -> None:
        # The session lock is what an action in flight holds. That action checks
        # where the page landed and handles it itself, so by the time this gets the
        # lock the page may already be somewhere allowed again.
        async with session.lock:
            if self.manager.sessions.get(session.id) is not session:
                logger.debug("session %s closed before its off-list navigation was corrected", session.id)
                return
            url = page.url
            if not (self.manager._url_off_list(url) or self.manager.page_provenance.needs_blank(page)):
                return
            # The audit log is read by agents too: the refused URL by its origin only.
            shown = self.manager._shown_url(page)
            self.manager.page_provenance.blank_started(page)
            try:
                await page.goto("about:blank")
            except PlaywrightError:
                self.manager.page_provenance.blank_failed(page)
                if page.is_closed():
                    logger.debug("page for session %s closed while it was moved off %s", session.id, url)
                    return
                raise
            except BaseException:
                # Cancelled: a blank still counted would exempt the page's own next about:blank commit.
                self.manager.page_provenance.blank_failed(page)
                raise
            await self.manager.audit.append(
                event_type="navigation_blocked",
                status="blocked",
                action="navigate",
                session_id=session.id,
                details={"url": shown, "source": "page"},
            )

    def _on_page_closed(self, session: "BrowserSession", page: "Page") -> None:
        """Move the session to another open tab when its active tab closes itself.

        A page that called window.close() (a sign-in popup the agent had switched
        to, say) left session.page pointing at a closed page: every later action
        failed and the session read as interrupted while its other tabs were fine.
        """
        provenance = self.manager.page_provenance
        if self.manager._url_off_list(page.url) or provenance.counts_for_its_session(page):
            # What it did just before closing (an opener it navigated, a blank it wrote) is delivered after this.
            provenance.closed_while_counting(session.id)
        provenance.forget(page)
        if session.page is not page:
            return
        context = getattr(session, "context", None)
        remaining = [p for p in getattr(context, "pages", []) if p is not page and not p.is_closed()]
        if remaining:
            session.page = remaining[-1]

    @staticmethod
    def _bounded_append(items: list[Any], value: Any, limit: int = 50) -> None:
        items.append(value)
        if len(items) > limit:
            del items[: len(items) - limit]

    async def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return list(session.downloads)
        try:
            record = await self.manager.session_store.get(session_id)
        except KeyError:
            raise SessionNotFoundError(session_id) from None
        return list(record.downloads)

    async def read_download_text(self, session_id: str, download_id: str | None = None) -> dict[str, Any]:
        """A captured download's contents as text.

        ``download_id`` defaults to the most recent completed download. Only
        files in the session's own downloads directory are read, and only text:
        binary files are refused with their size, type and artifact URL.
        """
        downloads = await self.list_downloads(session_id)
        record = self._select_download(session_id, downloads, download_id)
        downloads_dir = (Path(self.manager.settings.artifact_root) / session_id / "downloads").resolve()
        path = Path(str(record.get("path") or "")).resolve()
        if not path.is_relative_to(downloads_dir):
            logger.warning("refusing to read download %s: %s is outside %s", record.get("id"), path, downloads_dir)
            raise ValueError(f"Download {record.get('id')} is not in this session's downloads directory.")
        try:
            size = path.stat().st_size
        except OSError:
            raise ValueError(f"Download {record.get('id')} ({record.get('filename')}) is no longer on disk.") from None
        content_type = _MIME_TYPES.guess_type(path.name)[0]
        described = f"{record.get('filename')} ({content_type or 'unknown type'}, {size:,} bytes)"
        if size > DOWNLOAD_READ_MAX_BYTES:
            raise ValueError(
                f"Download {described} is larger than {DOWNLOAD_READ_MAX_BYTES:,} bytes; fetch it from {record.get('url')}."
            )
        data = await asyncio.to_thread(path.read_bytes)
        text, lossy = _decode_text(data, content_type)
        if text is None:
            raise ValueError(f"Download {described} is binary, not text; fetch it from {record.get('url')}.")
        return {
            "download_id": record.get("id"),
            "filename": record.get("filename"),
            "source_url": record.get("source_url"),
            "url": record.get("url"),
            "content_type": content_type,
            "size_bytes": size,
            "lossy": lossy,
            "text": text,
        }

    @staticmethod
    def _select_download(session_id: str, downloads: list[dict[str, Any]], download_id: str | None) -> dict[str, Any]:
        if download_id is None:
            completed = [item for item in downloads if item.get("status") == "completed"]
            if not completed:
                raise ValueError(f"Session {session_id} has no completed downloads.")
            return completed[-1]
        for item in downloads:
            if item.get("id") == download_id:
                if item.get("status") != "completed":
                    raise ValueError(
                        f"Download {download_id} did not complete ({item.get('failure') or item.get('status')})."
                    )
                return item
        known = ", ".join(str(item.get("id")) for item in downloads[-10:]) or "none"
        raise ValueError(f"No download {download_id} in session {session_id}. Recent downloads: {known}.")

    async def handle_download(self, session: "BrowserSession", download: Any, *, page_refused: bool = False) -> None:
        # A file a page generated (blob: and data: URLs) is judged by the page that reports it:
        # a blob names the origin that built it, which on an allowed page can be an iframe from
        # another host, and iframes on an allowed page are not filtered. A refused page is.
        url = getattr(download, "url", "") or ""
        if page_refused:
            refusal = "started by a page on a host ALLOWED_HOSTS does not allow"
        elif urlsplit(url).scheme.lower() != "blob" and self.manager._url_off_list(url):
            refusal = "from a host ALLOWED_HOSTS does not allow"
        else:
            refusal = None
        if refusal is not None:
            record = await self.download_capture.block(session, download, refusal)
            await self.manager.audit.append(
                event_type="download_blocked",
                status="blocked",
                action="download",
                session_id=session.id,
                details={"source_url": record["source_url"], "failure": refusal},
            )
        else:
            record = await self.download_capture.capture(session, download)
            await self.manager.audit.append(
                event_type="download_captured",
                status=record["status"],
                action="download",
                session_id=session.id,
                details={"filename": record["filename"], "url": record["url"], "failure": record["failure"]},
            )
        if session.id in self.manager.sessions:
            try:
                await self.manager._persist_session(session, status="active")
            except Exception as exc:
                logger.warning(
                    "failed to persist download metadata for session %s: %s",
                    session.id,
                    exc,
                )

    async def screenshot_diff(self, session_id: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            artifact_dir = session.artifact_dir
            # The last screenshot a checked read returned, not the newest PNG on disk: a capture
            # cut off while its page moved can leave a refused page's frame there.
            prior = checked_screenshot(session)

            new_shot = await self.manager._capture_screenshot(session, "diff-b")
            # Compared against what came before, not against the compare's own captures.
            session.last_screenshot = prior

            if prior is None:
                baseline_path = artifact_dir / "screenshot-baseline.png"
                shutil.copy2(new_shot["path"], str(baseline_path))
                baseline_url = f"/artifacts/{session_id}/screenshot-baseline.png"
                session.last_screenshot = {"path": str(baseline_path), "url": baseline_url}
                return {
                    "baseline_captured": True,
                    "baseline_url": baseline_url,
                    "message": "Baseline saved. Navigate to a new state and call compare again to see the diff.",
                }

            return await asyncio.to_thread(
                self.compute_diff,
                prior["path"],
                new_shot["path"],
                prior["url"],
                new_shot["url"],
                session.artifact_dir,
            )

    @staticmethod
    def compute_diff(
        a_path: str,
        b_path: str,
        a_url: str,
        b_url: str,
        artifact_dir: Path,
    ) -> dict[str, Any]:
        try:
            from PIL import Image, ImageChops  # type: ignore[import]

            img_a = Image.open(a_path).convert("RGB")
            img_b = Image.open(b_path).convert("RGB")

            if img_a.size != img_b.size:
                img_b = img_b.resize(img_a.size, Image.LANCZOS)

            diff = ImageChops.difference(img_a, img_b)
            total_pixels = img_a.width * img_a.height

            data = diff.tobytes()
            changed = sum(
                1 for index in range(0, len(data), 3) if data[index] > 8 or data[index + 1] > 8 or data[index + 2] > 8
            )
            changed_pct = round(changed / total_pixels * 100, 4) if total_pixels > 0 else 0.0

            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            diff_filename = f"{ts}-diff.png"
            diff_path = artifact_dir / diff_filename
            diff.save(str(diff_path))
            diff_url = f"/artifacts/{artifact_dir.name}/{diff_filename}"

            return {
                "changed_pixels": changed,
                "changed_pct": changed_pct,
                "diff_url": diff_url,
                "diff_path": str(diff_path),
                "a_url": a_url,
                "b_url": b_url,
                "width": img_a.width,
                "height": img_a.height,
            }
        except Exception as exc:
            logger.warning("screenshot diff failed: %s", exc)
            return {
                "error": "screenshot_diff_failed",
                "changed_pixels": -1,
                "changed_pct": -1.0,
                "diff_url": None,
                "diff_path": None,
                "a_url": a_url,
                "b_url": b_url,
                "width": 0,
                "height": 0,
            }


_TEXTUAL_APPLICATION_TYPES = frozenset(
    {
        "application/javascript",
        "application/json",
        "application/sql",
        "application/toml",
        "application/x-sh",
        "application/x-yaml",
        "application/xml",
        "application/yaml",
    }
)


def _is_textual_type(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type in _TEXTUAL_APPLICATION_TYPES
        or content_type.endswith(("+json", "+xml"))
    )


def _decode_text(data: bytes, content_type: str | None) -> tuple[str | None, bool]:
    """(text, lossy), or (None, False) for binary data.

    A file whose extension names a binary type (pdf, xlsx, png, zip ...) is
    binary whatever its first bytes are. Otherwise UTF-8 (with or without a
    BOM) and BOM-marked UTF-16 decode exactly; anything else without a NUL byte
    is text in some legacy encoding, decoded with replacement characters and
    reported as lossy rather than refused.
    """
    if content_type is not None and not _is_textual_type(content_type):
        return None, False
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return data.decode("utf-16"), False
        except UnicodeDecodeError:
            return None, False
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:8192]:
        return None, False
    return data.decode("utf-8", errors="replace"), True
