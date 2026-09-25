from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...audit import get_current_operator
from ...persistent_profiles import PersistentProfileError
from ...utils import UTC, utc_now
from ...witness import WitnessActionContext

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession


# Import limits. An auth profile is cookies plus storage state — kilobytes in
# practice — so these are generous. Without them a decompression bomb dropped in
# AUTH_ROOT could exhaust the disk: extraction streamed every member with no cap
# on member count, member size, or total expanded bytes.
MAX_ARCHIVE_MEMBERS = 2_000
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024


class ProfileInUseError(RuntimeError):
    """A live session holds this profile's persistent browser; close it first."""


class BrowserAuthProfileService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def _record_profile_receipt(
        self,
        *,
        action: str,
        profile_name: str,
        metadata: dict[str, Any],
    ) -> None:
        """Witness receipt for an operation that moves credentials but has no session.

        Receipts were previously session-scoped only, which is why auth-profile
        export and import — the two operations that move credentials on and off
        the box — had none. `session_id` is optional on the receipt model, so
        these record under a dedicated `auth-profiles` scope with their own
        signed chain.

        Never allowed to break the operation it is recording: a witness outage
        must not make exporting a profile fail. It is logged loudly instead, and
        verify() reports the gap.
        """
        witness = getattr(self.manager, "witness", None)
        if witness is None:
            return
        try:
            await witness.record(
                "auth-profiles",
                profile="normal",
                event_type="auth_profile",
                status="ok",
                action=action,
                action_class="auth",
                session_id=None,
                operator=get_current_operator(),
                target={"profile_name": profile_name},
                metadata=metadata,
            )
        except Exception:
            logger.exception("witness: failed to record %s receipt for profile %s", action, profile_name)

    @staticmethod
    def _host_matches(host: str, *domains: str) -> bool:
        host = host.lower().rstrip(".")
        for domain in domains:
            domain = domain.lower().rstrip(".")
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def current_platform(self, session: "BrowserSession") -> str | None:
        host = (urlparse(session.page.url).hostname or "").lower()
        if self._host_matches(host, "x.com", "twitter.com"):
            return "x"
        if self._host_matches(host, "instagram.com"):
            return "instagram"
        if self._host_matches(host, "linkedin.com"):
            return "linkedin"
        if self._host_matches(host, "outlook.live.com", "outlook.office.com", "outlook.office365.com"):
            return "outlook"
        return None

    def session_auth_state_info(self, session: "BrowserSession") -> dict[str, Any]:
        info = self.manager.auth_state.inspect(session.last_auth_state_path)
        info["session_auth_root"] = str(session.auth_dir)
        info["profile_name"] = session.auth_profile_name
        return info

    async def auth_state_info(self, session_id: str) -> dict[str, Any]:
        session = self.manager.sessions.get(session_id)
        if session is not None:
            return self.session_auth_state_info(session)
        record = await self.manager.session_store.get(session_id)
        return record.auth_state

    async def save_storage_state(self, session_id: str, path: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        safe_path = self.safe_session_auth_path(session, path)
        async with session.lock:
            try:
                await self.manager._ensure_witness_remote_ready(session, action="save_storage_state")
            except PermissionError:
                await self.manager._record_witness_receipt(
                    session,
                    event_type="auth_state",
                    status="blocked",
                    action="save_storage_state",
                    action_class="auth",
                    target={"path": path},
                    metadata={"error": "hosted Witness preflight failed"},
                )
                raise
            witness_outcome = self.manager.witness_policy.evaluate_action(
                session=self.manager._witness_session_context(session),
                action=WitnessActionContext(
                    action="save_storage_state",
                    action_class="auth",
                    stores_auth_material=True,
                ),
            )
            if witness_outcome.should_block:
                await self.manager._record_witness_receipt(
                    session,
                    event_type="auth_state",
                    status="blocked",
                    action="save_storage_state",
                    action_class="auth",
                    outcome=witness_outcome,
                    target={"path": path},
                    metadata={"error": witness_outcome.block_reason},
                )
                raise PermissionError(witness_outcome.block_reason or "Witness policy blocked save_storage_state")
            auth_info = await self.manager.auth_state.write_storage_state(session.context, safe_path)
            session.last_auth_state_path = Path(auth_info["path"]) if auth_info["path"] else None
            payload = {
                "saved_to": auth_info["path"],
                "auth_state": auth_info,
                "session": await self.manager._session_summary(session),
            }
            await self.manager._append_jsonl(
                session.artifact_dir / "actions.jsonl",
                {"timestamp": utc_now(), "action": "save_storage_state", **payload},
            )
            await self.manager.audit.append(
                event_type="auth_state_saved",
                status="ok",
                action="save_storage_state",
                session_id=session.id,
                details={"saved_to": auth_info["path"], "encrypted": auth_info["encrypted"]},
            )
            await self.manager._record_witness_receipt(
                session,
                event_type="auth_state",
                status="ok",
                action="save_storage_state",
                action_class="auth",
                outcome=witness_outcome,
                target={"path": path},
                metadata={"saved_to": auth_info["path"], "encrypted": auth_info["encrypted"]},
            )
            payload["session"] = await self.manager._session_summary(session)
            await self.manager._persist_session(session, status="active")
            return payload

    async def save_for_session(
        self,
        session: "BrowserSession",
        profile_name: str,
        *,
        metadata: dict[str, Any] | None = None,
        track_on_session: bool = True,
    ) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        # Before writing anything: saving over a profile you do not own is the
        # same takeover as reading it, and this method rewrites the metadata
        # file wholesale, so the owner has to be carried across explicitly.
        owner = self.require_access(normalized, action="saving an auth profile")
        profile_state_path = self.state_base_path(normalized, create=True)
        auth_info = await self.manager.auth_state.write_storage_state(session.context, profile_state_path)
        if track_on_session:
            # Only a caller-named save (the manual "save my login" feature,
            # or an explicit request) should change what this session
            # considers *its* profile. The background auto-persist writer
            # below shares this code path but must not relabel a session that
            # was opened from (or explicitly saved to) a different named
            # profile just because it also keeps the default "remember me"
            # profile warm.
            session.last_auth_state_path = Path(auth_info["path"]) if auth_info["path"] else None
            session.auth_profile_name = normalized

        profile_payload = {
            "profile_name": normalized,
            "last_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "saved_from_session_id": session.id,
            "saved_from_url": session.page.url,
            "saved_from_title": await session.page.title(),
            "platform": self.current_platform(session),
        }
        if owner:
            profile_payload["owner"] = owner
        if metadata:
            profile_payload.update(metadata)

        metadata_path = self.metadata_path(normalized, create=True)
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        metadata_path_str = os.path.realpath(os.fspath(metadata_path))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not metadata_path_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile metadata path must stay inside auth profile root")

        with open(metadata_path_str, "w", encoding="utf-8") as handle:
            json.dump(profile_payload, handle, indent=2, sort_keys=True)
        return {
            "profile_name": normalized,
            "saved_to": auth_info["path"],
            "auth_state": auth_info,
            "metadata": profile_payload,
        }

    async def save_auto_persist(self, session: "BrowserSession", profile_name: str) -> dict[str, Any]:
        """Silently refresh the "remember me" profile from a live session.

        Used by the automatic-persistence background writer (periodic tick
        and session close) — never by an operator-triggered API call, which
        goes through :meth:`save` and gets the full witness/audit treatment.
        This still enforces profile ownership (`require_access`) and still
        encrypts at rest via `write_storage_state`; it only skips the witness
        policy gate and the audit/receipt trail that would otherwise fire
        every few minutes for a purely internal write, and it never changes
        which profile the session itself is considered to belong to.

        Guarded against the incident this exists to prevent: a session that
        never actually held the saved logins (auto-load failed, a site kicked
        it, a fresh context that just never signed in) silently overwriting a
        good profile with a worse one. See `_check_auto_persist_downgrade`.
        """
        normalized = self.normalize_name(profile_name)
        downgrade = await self._check_auto_persist_downgrade(session, normalized)
        if downgrade is not None:
            return downgrade
        return await self.save_for_session(
            session,
            profile_name,
            metadata={"auto_persisted": True},
            track_on_session=False,
        )

    async def _check_auto_persist_downgrade(
        self, session: "BrowserSession", normalized: str
    ) -> dict[str, Any] | None:
        """Refuse an auto-persist write that would erase a signed-in site.

        Compares the sites the *currently saved* profile is signed into
        against the sites the live context is signed into right now. If the
        live context would lose a site the saved profile has -- the session
        failed to load the remembered login, or a site kicked the session out
        -- the write is skipped and the old file is kept untouched. Returns a
        skip payload when the write must not happen, else ``None``.
        """
        try:
            existing_path = self.resolve_state_path(normalized, must_exist=True)
        except FileNotFoundError:
            return None  # nothing saved yet -- nothing to lose
        old_sites = self.manager.auth_state.signed_in_sites(
            self.manager.auth_state.read_cookies(existing_path)
        )
        if not old_sites:
            return None
        try:
            new_state = await session.context.storage_state()
        except Exception:
            logger.warning(
                "auto-persist: could not read live storage state for profile '%s' to compare", normalized,
                exc_info=True,
            )
            return None
        new_cookies = new_state.get("cookies") if isinstance(new_state, dict) else None
        new_sites = self.manager.auth_state.signed_in_sites(new_cookies)
        lost = sorted(old_sites - new_sites)
        if not lost:
            return None
        logger.warning(
            "auto-persist: skipping save into profile '%s' -- would lose signed-in site(s): %s",
            normalized,
            ", ".join(lost),
        )
        return {
            "profile_name": normalized,
            "skipped": True,
            "reason": "would_lose_signed_in_sites",
            "lost_sites": lost,
        }

    async def save(self, session_id: str, profile_name: str) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        async with session.lock:
            try:
                await self.manager._ensure_witness_remote_ready(session, action="save_auth_profile")
            except PermissionError:
                await self.manager._record_witness_receipt(
                    session,
                    event_type="auth_profile",
                    status="blocked",
                    action="save_auth_profile",
                    action_class="auth",
                    target={"profile_name": profile_name},
                    metadata={"error": "hosted Witness preflight failed"},
                )
                raise
            witness_outcome = self.manager.witness_policy.evaluate_action(
                session=self.manager._witness_session_context(session),
                action=WitnessActionContext(
                    action="save_auth_profile",
                    action_class="auth",
                    stores_auth_material=True,
                ),
            )
            if witness_outcome.should_block:
                await self.manager._record_witness_receipt(
                    session,
                    event_type="auth_profile",
                    status="blocked",
                    action="save_auth_profile",
                    action_class="auth",
                    outcome=witness_outcome,
                    target={"profile_name": profile_name},
                    metadata={"error": witness_outcome.block_reason},
                )
                raise PermissionError(witness_outcome.block_reason or "Witness policy blocked save_auth_profile")
            payload = await self.save_for_session(session, profile_name)
            payload["session"] = await self.manager._session_summary(session)
            await self.manager._append_jsonl(
                session.artifact_dir / "actions.jsonl",
                {"timestamp": utc_now(), "action": "save_auth_profile", **payload},
            )
            await self.manager.audit.append(
                event_type="auth_profile_saved",
                status="ok",
                action="save_auth_profile",
                session_id=session.id,
                details={"profile_name": payload["profile_name"], "saved_to": payload["saved_to"]},
            )
            await self.manager._record_witness_receipt(
                session,
                event_type="auth_profile",
                status="ok",
                action="save_auth_profile",
                action_class="auth",
                outcome=witness_outcome,
                target={"profile_name": payload["profile_name"]},
                metadata={"saved_to": payload["saved_to"]},
            )
            payload["session"] = await self.manager._session_summary(session)
            await self.manager._persist_session(session, status="active")
            return payload

    async def get(self, profile_name: str) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        self.require_access(normalized, action="reading an auth profile")
        profile_dir = self.dir(normalized, create=False)
        metadata = self.read_metadata(normalized)
        state_path = self.resolve_state_path(normalized, must_exist=False)
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        state_path_str = os.path.realpath(os.fspath(state_path))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not state_path_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile state path must stay inside auth profile root")

        state_exists = os.path.exists(state_path_str)
        if not state_exists and not metadata:
            raise KeyError(normalized)
        return {
            "profile_name": normalized,
            "profile_dir": str(profile_dir),
            "auth_state": self.manager.auth_state.inspect(Path(state_path_str) if state_exists else None),
            "metadata": metadata,
        }

    async def list(self) -> list[dict[str, Any]]:
        root = self.root()
        if not root.exists():
            return []
        profiles: list[dict[str, Any]] = []
        for directory in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
            # Listing another operator's profiles would leak both their existence
            # and the sites they hold logins for. Checked before `get` so that a
            # genuine containment error still raises instead of being skipped.
            if not self.accessible(directory.name):
                continue
            try:
                profiles.append(await self.get(directory.name))
            except KeyError:
                continue
        profiles.sort(
            key=lambda item: (item.get("metadata") or {}).get("last_saved_at") or "",
            reverse=True,
        )
        return profiles

    async def export(self, profile_name: str) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        self.require_access(normalized, action="exporting an auth profile")
        auth_root = Path(self.manager.settings.auth_root).resolve()
        profile_root = self.root()
        profile_dir = self.dir(normalized, create=False)
        profile_root_str = os.path.realpath(os.fspath(profile_root))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        profile_dir_str = os.path.realpath(os.fspath(profile_dir))
        if not profile_dir_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile path must stay inside auth profile root")

        if not os.path.isdir(profile_dir_str):
            raise FileNotFoundError(f"auth profile '{normalized}' not found")

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_name = f"{normalized}-{ts}.tar.gz"
        auth_root_str = os.path.realpath(os.fspath(auth_root))
        auth_root_prefix = auth_root_str if auth_root_str.endswith(os.sep) else auth_root_str + os.sep
        archive_path_str = os.path.realpath(os.path.join(auth_root_str, archive_name))
        if not archive_path_str.startswith(auth_root_prefix):
            raise PermissionError("auth profile archive path must stay inside auth root")
        archive_path = Path(archive_path_str)

        await asyncio.to_thread(self.write_tar, Path(profile_dir_str), archive_path)

        # Export packages every cookie and localStorage entry for a logged-in
        # account into a downloadable archive. save_storage_state — which merely
        # *writes* that material — is wrapped in witness policy, an audit event
        # and a receipt; this path, which lets the material leave the box, had
        # none of it. Nothing in /audit/events recorded that it happened.
        await self.manager.audit.append(
            event_type="auth_profile_exported",
            status="ok",
            action="export_auth_profile",
            session_id=None,
            details={
                "profile_name": normalized,
                "archive_name": archive_name,
                "encrypted_at_rest": bool(self.manager.settings.auth_state_encryption_key),
            },
        )
        await self._record_profile_receipt(
            action="export_auth_profile",
            profile_name=normalized,
            metadata={
                "archive_name": archive_name,
                "encrypted_at_rest": bool(self.manager.settings.auth_state_encryption_key),
            },
        )

        return {
            "profile_name": normalized,
            "archive_path": str(archive_path),
            "archive_name": archive_name,
            "download_url": f"/auth-export/{archive_name}",
        }

    async def import_profile(self, archive_path: str, *, overwrite: bool = False) -> dict[str, Any]:
        archive_name = PurePosixPath(str(archive_path).replace("\\", "/")).name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.tar\.gz", archive_name):
            raise ValueError("auth profile archive name is invalid")
        auth_root = Path(self.manager.settings.auth_root).resolve()
        auth_root_str = os.path.realpath(os.fspath(auth_root))
        auth_root_prefix = auth_root_str if auth_root_str.endswith(os.sep) else auth_root_str + os.sep
        src_str = os.path.realpath(os.path.join(auth_root_str, archive_name))
        if not src_str.startswith(auth_root_prefix):
            raise PermissionError("auth profile archive path must stay inside auth root")
        src = Path(src_str)

        if not os.path.exists(src_str):
            raise FileNotFoundError(f"archive not found: {archive_name}")

        profile_root = self.root()

        if self._persistent_profiles_active():
            await self._reset_persistent_for_import(src, profile_root, overwrite=overwrite)

        def _extract() -> str:
            with tarfile.open(str(src), "r:gz") as tar:
                members = tar.getmembers()
                if not members:
                    raise ValueError("archive is empty")

                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ValueError(f"archive contains too many members (limit {MAX_ARCHIVE_MEMBERS})")

                safe_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
                top_level: str | None = None
                declared_total = 0
                for member in members:
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError("archive contains an unsupported member type")
                    if not member.isdir() and not member.isfile():
                        continue
                    declared_total += max(0, member.size)
                    if declared_total > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ValueError(f"archive expands beyond the {MAX_ARCHIVE_TOTAL_BYTES} byte limit")
                    safe_path = self.safe_archive_member_name(member.name)
                    if len(safe_path.parts) == 1 and not member.isdir():
                        raise ValueError("archive must contain a top-level profile directory")
                    top = safe_path.parts[0]
                    if top_level is None:
                        top_level = top
                    elif top != top_level:
                        raise ValueError("archive must contain a single top-level profile directory")
                    safe_members.append((member, safe_path))

                if top_level is None:
                    raise ValueError("archive contains no importable files")

                profile_name = self.normalize_name(top_level)
                dest_dir = self.resolve_contained_path(profile_root, profile_name)
                if dest_dir.exists():
                    if not overwrite:
                        raise FileExistsError(f"profile '{profile_name}' already exists; pass overwrite=true")
                    # Overwriting is a write to somebody's stored logins.
                    self.require_access(profile_name, action="overwriting an auth profile")

                # Extract into a staging directory and swap it in only once every
                # member is written. The old profile used to be deleted first, so
                # an archive that failed part-way — a member over the size cap, a
                # truncated gzip — destroyed a working login and left a partial
                # one in its place. Staging sits beside profiles/, on the same
                # filesystem so the swap is a rename, and outside it so list()
                # never sees it.
                staging_root = Path(tempfile.mkdtemp(prefix=".import-", dir=auth_root_str))
                try:
                    for member, safe_path in safe_members:
                        relative = Path(*safe_path.parts)
                        target = self.resolve_contained_path(staging_root, relative)
                        if member.isdir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = tar.extractfile(member)
                        if source is None:
                            raise ValueError("archive member could not be read")
                        # Bounded copy. A tar header's declared size is attacker
                        # controlled, so the pre-scan above is a cheap early reject,
                        # not a guarantee — this is what actually stops a gzip bomb
                        # from filling the disk mid-extraction.
                        with source, target.open("wb") as output:
                            written = 0
                            while True:
                                chunk = source.read(_COPY_CHUNK_BYTES)
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > MAX_ARCHIVE_MEMBER_BYTES:
                                    raise ValueError(
                                        f"archive member '{member.name}' exceeds the "
                                        f"{MAX_ARCHIVE_MEMBER_BYTES} byte limit"
                                    )
                                output.write(chunk)

                    staged = staging_root / top_level
                    staged.mkdir(exist_ok=True)
                    if dest_dir.exists():
                        retired = staging_root / ".replaced"
                        os.replace(dest_dir, retired)
                        try:
                            os.replace(staged, dest_dir)
                        except OSError:
                            os.replace(retired, dest_dir)
                            raise
                    else:
                        os.replace(staged, dest_dir)
                finally:
                    shutil.rmtree(staging_root, ignore_errors=True)

                return profile_name

        profile_name = await asyncio.to_thread(_extract)

        # Import installs credentials the controller will later replay into a
        # real browser. Like export, it had no audit record at all.
        await self.manager.audit.append(
            event_type="auth_profile_imported",
            status="ok",
            action="import_auth_profile",
            session_id=None,
            details={"profile_name": profile_name, "archive_name": archive_name, "overwrite": overwrite},
        )
        await self._record_profile_receipt(
            action="import_auth_profile",
            profile_name=profile_name,
            metadata={"archive_name": archive_name, "overwrite": overwrite},
        )

        return {
            "profile_name": profile_name,
            "profile_path": str(profile_root / profile_name),
            "imported": True,
        }

    async def _reset_persistent_for_import(self, src: Path, profile_root: Path, *, overwrite: bool) -> None:
        """Retire the on-disk browser profile an import is about to replace.

        An imported archive is "this is the account for that name": the next
        Open must seed from it, not reopen whatever browser profile already
        sits under the name. Validated (name, overwrite rule, ownership, no
        live session) before anything moves; the old directory goes to
        trash, never deleted, so a failed import loses nothing.
        """

        def _top_level_name() -> str:
            with tarfile.open(str(src), "r:gz") as tar:
                top_level: str | None = None
                for member in tar.getmembers():
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError("archive contains an unsupported member type")
                    safe_path = self.safe_archive_member_name(member.name)
                    if top_level is None:
                        top_level = safe_path.parts[0]
                    elif safe_path.parts[0] != top_level:
                        raise ValueError("archive must contain a single top-level profile directory")
                if top_level is None:
                    raise ValueError("archive contains no importable files")
                return self.normalize_name(top_level)

        profile_name = await asyncio.to_thread(_top_level_name)
        dest_dir = self.resolve_contained_path(profile_root, profile_name)
        if dest_dir.exists():
            if not overwrite:
                raise FileExistsError(f"profile '{profile_name}' already exists; pass overwrite=true")
            owner = self.require_access(profile_name, action="overwriting an auth profile")
        else:
            owner = self.require_access(profile_name, action="importing an auth profile")
        self._refuse_if_in_use(profile_name)
        await self._trash_persistent(profile_name, reason="replaced-by-import", owner=owner)

    async def delete(self, profile_name: str) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        owner = self.require_access(normalized, action="deleting an auth profile")
        profile_dir = self.dir(normalized, create=False)
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        profile_dir_str = os.path.realpath(os.fspath(profile_dir))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not profile_dir_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile path must stay inside auth profile root")
        export_exists = os.path.isdir(profile_dir_str)
        browser_profile_trashed = False
        if self._persistent_profiles_active():
            # The on-disk browser profile goes first (to trash): if that
            # fails, the export stays too, rather than leaving a directory
            # that would reopen the "deleted" account under this name.
            self._refuse_if_in_use(normalized)
            result = await self._trash_persistent(normalized, reason="deleted", owner=owner)
            browser_profile_trashed = bool(result.get("trashed"))
        if not export_exists and not browser_profile_trashed:
            raise FileNotFoundError(f"auth profile '{normalized}' not found")
        if export_exists:
            await asyncio.to_thread(shutil.rmtree, profile_dir_str)
        await self.manager.audit.append(
            event_type="auth_profile_deleted",
            status="ok",
            action="delete_auth_profile",
            session_id=None,
            details={"profile_name": normalized, "browser_profile_trashed": browser_profile_trashed},
        )
        await self._record_profile_receipt(
            action="delete_auth_profile",
            profile_name=normalized,
            metadata={"browser_profile_trashed": browser_profile_trashed},
        )
        payload: dict[str, Any] = {"profile_name": normalized, "deleted": True}
        if self._persistent_profiles_active():
            payload["browser_profile_trashed"] = browser_profile_trashed
        return payload

    async def rename(self, profile_name: str, new_name: str) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        normalized_new = self.normalize_name(new_name)
        if normalized == normalized_new:
            raise ValueError("new auth profile name must differ from the current name")
        # Renaming rewrites where the credentials live, so it needs the same
        # proof of ownership as saving or exporting them.
        owner = self.require_access(normalized, action="renaming an auth profile")
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        source_dir_str = os.path.realpath(os.fspath(self.dir(normalized, create=False)))
        source_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not source_dir_str.startswith(source_root_prefix):
            raise PermissionError("auth profile path must stay inside auth profile root")
        if not os.path.isdir(source_dir_str):
            raise FileNotFoundError(f"auth profile '{normalized}' not found")
        destination_dir = self.dir(normalized_new, create=False)
        if destination_dir.exists():
            # Renaming onto an existing name is the same takeover as saving
            # over it, so the destination's own ownership must allow the write.
            self.require_access(normalized_new, action="overwriting an auth profile")
        persistent_renamed = False
        if self._persistent_profiles_active():
            # Move the on-disk browser profile first; a stale directory under
            # the new name goes to trash. If this fails nothing has changed.
            self._refuse_if_in_use(normalized, normalized_new)
            result = await self._rename_persistent(normalized, normalized_new, owner=owner)
            persistent_renamed = bool(result.get("renamed"))
        try:
            if destination_dir.exists():
                await asyncio.to_thread(shutil.rmtree, os.path.realpath(os.fspath(destination_dir)))
            profile_root = self.root()
            source = self.resolve_contained_path(profile_root, normalized)
            destination = self.resolve_contained_path(profile_root, normalized_new)
            await asyncio.to_thread(shutil.move, str(source), str(destination))
        except Exception:
            if persistent_renamed:
                # Keep the pair together: put the browser profile back under
                # its old name so it still matches the export that stayed.
                try:
                    await self._rename_persistent(normalized_new, normalized, owner=owner)
                except Exception as exc:
                    logger.error(
                        "rename rollback: browser profile '%s' could not be moved back to '%s': %s",
                        normalized_new, normalized, exc,
                    )
            raise
        metadata = self.read_metadata(normalized_new)
        metadata["profile_name"] = normalized_new
        if owner:
            metadata["owner"] = owner
        metadata_path_str = os.path.realpath(os.fspath(self.metadata_path(normalized_new, create=False)))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not metadata_path_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile metadata path must stay inside auth profile root")
        with open(metadata_path_str, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, sort_keys=True)
        await self.manager.audit.append(
            event_type="auth_profile_renamed",
            status="ok",
            action="rename_auth_profile",
            session_id=None,
            details={"profile_name": normalized, "new_profile_name": normalized_new},
        )
        await self._record_profile_receipt(
            action="rename_auth_profile",
            profile_name=normalized_new,
            metadata={"previous_name": normalized},
        )
        return {"profile_name": normalized_new, "previous_name": normalized}

    # ── Persistent browser profile lifecycle ─────────────────────────────────
    #
    # With PERSISTENT_PROFILES_ENABLED the real login lives in browser-node's
    # on-disk Chromium profile (/data/browser-profiles/<name>), not only in
    # this encrypted export. Delete, rename and import must move that
    # directory with the export, or the old account keeps opening under the
    # name. The directory is 0700 for browser-node's uid and this container
    # runs with cap_drop ALL, so it is changed through browser-node's
    # authenticated API -- and it is only ever moved to trash, never deleted.

    def _persistent_profiles_active(self) -> bool:
        settings = self.manager.settings
        return bool(
            getattr(settings, "persistent_profiles_enabled", False)
            and getattr(settings, "session_isolation_mode", None) == "shared_browser_node"
        )

    def _refuse_if_in_use(self, *profile_names: str) -> None:
        lifecycle = getattr(self.manager, "session_lifecycle", None)
        if lifecycle is None:
            return
        for name in profile_names:
            holder = lifecycle.session_holding_profile(name)
            if holder is not None:
                raise ProfileInUseError(
                    f"auth profile '{name}' is open in live session {holder.id}; close that session first"
                )

    @staticmethod
    def _translate_persistent_error(exc: PersistentProfileError) -> Exception:
        if exc.status_code == 403:
            return PermissionError(str(exc))
        if exc.status_code == 409:
            return ProfileInUseError(str(exc))
        return exc

    async def _trash_persistent(self, profile_name: str, *, reason: str, owner: str | None) -> dict[str, Any]:
        try:
            return await self.manager.persistent_profiles.trash(profile_name, reason=reason, owner=owner)
        except PersistentProfileError as exc:
            raise self._translate_persistent_error(exc) from exc

    async def _rename_persistent(self, profile_name: str, new_name: str, *, owner: str | None) -> dict[str, Any]:
        try:
            return await self.manager.persistent_profiles.rename(profile_name, new_name, owner=owner)
        except PersistentProfileError as exc:
            raise self._translate_persistent_error(exc) from exc

    def owner_of(self, profile_name: str) -> str | None:
        owner = self.read_metadata(self.normalize_name(profile_name)).get("owner")
        return owner.strip() if isinstance(owner, str) and owner.strip() else None

    @staticmethod
    def verified_operator() -> str | None:
        """The current operator, but only when it was actually proven.

        `source: "header"` is a self-asserted label (see app/auth_policy.py), so
        it can never grant access to a profile — anyone able to reach the API
        could set it to any value.
        """
        operator = get_current_operator()
        return operator.id if operator.source == "token" else None

    def require_access(self, profile_name: str, *, action: str) -> str | None:
        """Authorize this operator against a profile; return who now owns it.

        A profile is owned only if it was saved by a caller with a proven
        identity. Deployments using the shared `API_BEARER_TOKEN` therefore
        record no owner and are unaffected, and profiles that pre-date this
        release stay usable — ownership starts applying the moment named
        credentials do, with no migration and no risk of locking anyone out of
        their own logins.
        """
        normalized = self.normalize_name(profile_name)
        owner = self.owner_of(normalized)
        operator = self.verified_operator()
        if owner is None:
            return operator
        if operator is not None and operator == owner:
            return owner
        raise PermissionError(
            f"auth profile '{normalized}' belongs to operator '{owner}'; "
            f"{action} requires authenticating as that operator"
        )

    def accessible(self, profile_name: str) -> bool:
        try:
            self.require_access(profile_name, action="access")
        except PermissionError:
            return False
        return True

    def root(self) -> Path:
        root = Path(self.manager.settings.auth_root).resolve() / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def dir(self, profile_name: str, *, create: bool) -> Path:
        normalized = self.normalize_name(profile_name)
        root = self.root()
        root_str = os.path.realpath(os.fspath(root))
        directory_str = os.path.realpath(os.path.join(root_str, normalized))
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        if not directory_str.startswith(root_prefix):
            raise PermissionError("auth profile path must stay inside auth profile root")
        directory = Path(directory_str)
        if create:
            directory.mkdir(parents=True, exist_ok=True)
        return directory

    def metadata_path(self, profile_name: str, *, create: bool) -> Path:
        return self.dir(profile_name, create=create) / "profile.json"

    def state_base_path(self, profile_name: str, *, create: bool) -> Path:
        return self.dir(profile_name, create=create) / "state.json"

    def resolve_state_path(self, profile_name: str, *, must_exist: bool) -> Path:
        base_path = self.state_base_path(profile_name, create=not must_exist)
        candidates = [base_path.with_name(f"{base_path.name}.enc"), base_path]
        existing = [candidate for candidate in candidates if candidate.exists()]
        if existing:
            existing.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
            return existing[0]
        if must_exist:
            raise FileNotFoundError(base_path)
        return base_path

    def read_metadata(self, profile_name: str) -> dict[str, Any]:
        metadata_path = self.metadata_path(profile_name, create=False)
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        metadata_path_str = os.path.realpath(os.fspath(metadata_path))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not metadata_path_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile metadata path must stay inside auth profile root")

        if not os.path.exists(metadata_path_str):
            return {}
        try:
            with open(metadata_path_str, encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def safe_session_auth_path(
        self,
        session: "BrowserSession",
        relative_path: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        root_str = os.path.realpath(os.fspath(session.auth_dir.resolve()))
        candidate_str = os.path.realpath(os.path.join(root_str, os.fspath(relative_path)))
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        if not candidate_str.startswith(root_prefix):
            raise PermissionError("path must stay inside the session auth root")

        os.makedirs(os.path.dirname(candidate_str), exist_ok=True)

        if must_exist and not os.path.exists(candidate_str):
            raise FileNotFoundError(candidate_str)
        return Path(candidate_str)

    def safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        root_str = os.path.realpath(os.fspath(Path(self.manager.settings.auth_root).resolve()))
        candidate_str = os.path.realpath(os.path.join(root_str, os.fspath(relative_path)))
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        if not candidate_str.startswith(root_prefix):
            raise PermissionError("path must stay inside the auth root")

        os.makedirs(os.path.dirname(candidate_str), exist_ok=True)

        if must_exist and not os.path.exists(candidate_str):
            raise FileNotFoundError(candidate_str)
        return Path(candidate_str)

    @staticmethod
    def resolve_contained_path(root: Path, candidate_path: str | Path, *, allow_absolute: bool = False) -> Path:
        root_str = os.path.normcase(os.path.realpath(os.fspath(root)))
        raw_path = os.fspath(candidate_path)
        if os.path.isabs(raw_path):
            if not allow_absolute:
                raise PermissionError("path must be relative")
            candidate_str = os.path.normcase(os.path.realpath(raw_path))
        else:
            candidate_str = os.path.normcase(os.path.realpath(os.path.join(root_str, raw_path)))

        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        if candidate_str != root_str and not candidate_str.startswith(root_prefix):
            raise PermissionError("path must stay inside the configured root")
        return Path(candidate_str)

    @staticmethod
    def normalize_name(profile_name: str) -> str:
        normalized = profile_name.strip()
        if not normalized:
            raise ValueError("auth profile name is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", normalized):
            raise ValueError("auth profile names may contain letters, numbers, dots, underscores, and hyphens")
        return normalized

    @staticmethod
    def write_tar(source_dir: Path, dest: Path) -> None:
        with tarfile.open(str(dest), "w:gz") as tar:
            tar.add(str(source_dir), arcname=source_dir.name)

    @staticmethod
    def safe_archive_member_name(member_name: str) -> PurePosixPath:
        candidate = PurePosixPath(member_name.replace("\\", "/"))
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise ValueError("archive contains an unsafe path")
        return candidate
