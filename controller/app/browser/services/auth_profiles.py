from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...audit import get_current_operator
from ...session_ownership import may_use, verified_operator
from ...utils import PRIVATE_FILE_MODE, UTC, atomic_write_text, replace_retrying, utc_now
from ...witness import WitnessActionContext
from ..page_read import title_or_blank

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

# Files an export leaves out, by name, before it reads them: decrypted copies
# earlier releases made while a session opened ("auth-state-*.json"), and the
# temp, staged and kept-for-rollback files a save writes beside the profile's
# own (".<name>.<random>.tmp", ".tmp.json", ".tmp.json.enc"), which come and go
# while a save runs and which a process that died at the wrong moment left behind.
_NOT_EXPORTED = re.compile(r"auth-state-[^/\\]*\.json|\.[^/\\]*\.tmp(\.json(\.enc)?)?")


# Written beside the state a session saves in AUTH_ROOT/<session_id>/ when that
# session has an owner. It outlives the session record and the process, so the
# ownership of saved state does not depend on anything still being in memory.
SESSION_OWNER_MARKER = ".owner.json"
PROFILES_DIRNAME = "profiles"
# Beside each archive an operator exports: {"exporter": "<operator id>"}. Kept out
# of the archive so its layout (one top-level profile directory) is unchanged and
# archives from older releases or other boxes still import.
EXPORTER_SIDECAR_SUFFIX = ".exporter.json"
_FOREIGN_SESSION_STATE = "storage_state_path points into the saved auth state of another operator's session"


def _staged_beside(path: Path) -> Path:
    """A fresh name beside ``path`` for a file written before it is put in place (".<name>.<random>.tmp.json")."""
    return path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp.json")


def _put_in_place(staged_state: Path, state_path: Path, staged_metadata: Path, metadata_path: Path) -> None:
    """Move a profile's new state and metadata onto its own, both or neither.

    The state file is there throughout: the previous state is kept by a second link to it (a copy where
    links fail) for the rollback, and the new state replaces the file in one rename. Moving it aside first
    left the profile with no state file between the renames, which a reader in another thread saw (an
    export, Playwright reading a plaintext state). No await in here, so nothing on the event loop sees
    one file without the other.

    The metadata goes first on a first save, and on a re-save that names an owner the metadata on disk does
    not (a named operator's re-save of a profile saved under the shared token or before owners). A state
    beside metadata that names no owner is open to every operator under named credentials, and a process
    killed between the renames, or a rollback that fails, left just that; now it
    leaves at most metadata naming the owner beside no state or the previous one. On a failure the previous
    metadata goes back (none, on a first save) once the state is as it was. Any other re-save keeps the
    state first, the order that keeps the previous login on a failure: its metadata names the owner already,
    or neither the old nor the new names one, and the order changes nothing about who may open it.
    """
    metadata_first = not metadata_path.exists() or (
        _names_an_owner(staged_metadata) and not _names_an_owner(metadata_path)
    )
    previous_metadata = metadata_path.read_text(encoding="utf-8") if metadata_first and metadata_path.exists() else None
    previous = _keep_previous(state_path)
    try:
        if metadata_first:
            replace_retrying(staged_metadata, metadata_path)
            replace_retrying(staged_state, state_path)
        else:
            replace_retrying(staged_state, state_path)
            replace_retrying(staged_metadata, metadata_path)
    except BaseException:
        # Metadata put first goes back only once the state is as it was: kept, it names the owner.
        if _roll_back(previous, state_path) and metadata_first:
            _put_metadata_back(previous_metadata, metadata_path)
        raise
    if previous is not None:
        _remove_quietly(previous, "the previous state after a save")


def _names_an_owner(metadata_path: Path) -> bool:
    """Whether the profile metadata on disk names an owner: not when it is missing, unreadable or has none."""
    try:
        owner = json.loads(metadata_path.read_text(encoding="utf-8")).get("owner")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(owner, str) and bool(owner.strip())


def _put_metadata_back(previous: str | None, metadata_path: Path) -> None:
    """After a failed save whose metadata went first: the previous metadata back, or none after a first save."""
    if previous is None:
        _remove_quietly(metadata_path, "the metadata of a first save that failed")
        return
    try:
        atomic_write_text(metadata_path, previous, mode=PRIVATE_FILE_MODE)
    except OSError as exc:
        logger.error(
            "could not put back the metadata of an auth profile's failed save, %s (%s): it names the saving "
            "operator as owner of the previous login",
            metadata_path,
            exc,
        )


def _keep_previous(state_path: Path) -> Path | None:
    if not state_path.exists():
        return None
    previous = _staged_beside(state_path)
    try:
        os.link(state_path, previous)
    except OSError:
        try:
            shutil.copy2(state_path, previous)
        except BaseException:
            previous.unlink(missing_ok=True)  # a copy cut short is not the previous state
            raise
    return previous


def _roll_back(previous: Path | None, state_path: Path) -> bool:
    """Put the previous state back after a failed save (none on a first save): whether it is as it was.

    If not, an error is logged with where the previous state is kept, or, on a first save, that the new
    state stays.
    """
    try:
        if previous is None:
            state_path.unlink(missing_ok=True)
        elif state_path.exists() and os.path.samefile(previous, state_path):
            previous.unlink()  # the new state never landed: the link is the state file itself
        else:
            replace_retrying(previous, state_path)
    except OSError as exc:
        if previous is None:
            logger.error(
                "could not remove the new state of an auth profile's failed first save, %s (%s)", state_path, exc
            )
        else:
            logger.error(
                "could not put an auth profile's previous state back after a failed save; it is kept at %s (%s)",
                previous,
                exc,
            )
        return False
    return True


def _add_tree(tar: tarfile.TarFile, path: Path, arcname: str, skipped) -> None:
    """tar.add(path) and what is under it, in tar.add's order, leaving out what ``skipped(arcname, name)`` names."""
    tar.add(str(path), arcname=arcname, recursive=False)
    if path.is_symlink() or not path.is_dir():
        return
    for name in sorted(os.listdir(path)):
        child = f"{arcname}/{name}"
        if not skipped(child, name):
            _add_tree(tar, path / name, child, skipped)


def _remove_quietly(path: Path, what: str) -> None:
    """Best effort once a save has landed: what is left is logged, the save stands."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove %s, %s: %s", what, path, exc)


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
            # Marker first: state must never sit in the directory without it.
            self.mark_session_auth_dir(session.auth_dir, session.owner)
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
    ) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        # Before writing anything: saving over a profile you do not own is the
        # same takeover as reading it, and this method rewrites the metadata
        # file wholesale, so the owner has to be carried across explicitly.
        owner = self.require_access(normalized, action="saving an auth profile")
        # Read before the state is written: a page call that fails afterwards left the cookies on
        # disk with no metadata, so with no owner, for any caller to load.
        saved_from_url = self.manager._shown_url(session.page)
        saved_from_title = await title_or_blank(self.manager, session)
        profile_state_path = self.state_base_path(normalized, create=True)
        metadata_path = self.metadata_path(normalized, create=True)
        profile_root_str = os.path.realpath(os.fspath(self.root()))
        metadata_path_str = os.path.realpath(os.fspath(metadata_path))
        profile_root_prefix = profile_root_str if profile_root_str.endswith(os.sep) else profile_root_str + os.sep
        if not metadata_path_str.startswith(profile_root_prefix):
            raise PermissionError("auth profile metadata path must stay inside auth profile root")
        # The new state and metadata are written beside the profile's own and put in place together
        # (_put_in_place), so a save that fails or is cancelled at any point leaves the profile's
        # previous state and metadata as they were, and a first save leaves neither. Replacing the state
        # first and deleting it on a failure lost the previous login. The staged names match
        # _NOT_EXPORTED, so an export leaves out those a dead process left behind.
        staged_state = _staged_beside(profile_state_path)
        staged_metadata = _staged_beside(Path(metadata_path_str))
        staged = [staged_state, staged_state.with_name(f"{staged_state.name}.enc"), staged_metadata]
        try:
            auth_info = await self.manager.auth_state.write_storage_state(session.context, staged_state)
            written_state = Path(auth_info["path"])
            state_path = profile_state_path.with_name(
                profile_state_path.name + (".enc" if written_state.name.endswith(".enc") else "")
            )
            profile_payload = {
                "profile_name": normalized,
                "last_saved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "saved_from_session_id": session.id,
                "saved_from_url": saved_from_url,
                "saved_from_title": saved_from_title,
                "platform": self.current_platform(session),
            }
            if owner:
                profile_payload["owner"] = owner
            if metadata:
                profile_payload.update(metadata)
            # Atomic: open("w") truncated first, and a reader in that window got
            # {} from read_metadata, which require_access took for "no owner".
            atomic_write_text(
                staged_metadata, json.dumps(profile_payload, indent=2, sort_keys=True), mode=PRIVATE_FILE_MODE
            )
            _put_in_place(written_state, state_path, staged_metadata, Path(metadata_path_str))
            if state_path != profile_state_path:
                # Saved encrypted: the plaintext state saved before the key was set has no reason to stay.
                _remove_quietly(profile_state_path, "the plaintext state saved before the encryption key was set")
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
        auth_info = {**auth_info, "path": str(state_path)}
        session.last_auth_state_path = state_path
        session.auth_profile_name = normalized
        return {
            "profile_name": normalized,
            "saved_to": auth_info["path"],
            "auth_state": auth_info,
            "metadata": profile_payload,
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

        # Bound to the exporter before the archive exists, so there is never an
        # archive in AUTH_ROOT that another operator could import unchecked.
        sidecar = archive_path.with_name(archive_path.name + EXPORTER_SIDECAR_SUFFIX)
        exporter = self.verified_operator()
        if exporter:
            atomic_write_text(sidecar, json.dumps({"exporter": exporter}))
        try:
            await asyncio.to_thread(self.write_tar, Path(profile_dir_str), archive_path)
        except BaseException:
            sidecar.unlink(missing_ok=True)
            raise

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
        self._require_archive_access(src, archive_name)

        profile_root = self.root()

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

    @staticmethod
    def _require_archive_access(archive: Path, archive_name: str) -> None:
        """Only the operator who exported an archive may import it.

        An archive with no recorded exporter (made without named credentials,
        by an earlier release, or copied in from another box) is not bound, and
        the owner recorded inside the profile still decides who can use it. A
        sidecar that exists but cannot be read raises, for the reason
        _read_session_dir_owner does: unreadable must not mean unbound.
        """
        sidecar = archive.with_name(archive.name + EXPORTER_SIDECAR_SUFFIX)
        if not sidecar.exists():
            return
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        exporter = payload.get("exporter") if isinstance(payload, dict) else None
        if not isinstance(exporter, str) or not exporter.strip():
            raise PermissionError(
                f"the exporter record for archive '{archive_name}' is unreadable, so who may import it "
                f"cannot be checked; restore or delete {sidecar.name}"
            )
        if not may_use(exporter.strip()):
            raise PermissionError(f"archive '{archive_name}' was exported by another operator")

    def owner_of(self, profile_name: str) -> str | None:
        owner = self.read_metadata(self.normalize_name(profile_name), strict=True).get("owner")
        return owner.strip() if isinstance(owner, str) and owner.strip() else None

    @staticmethod
    def verified_operator() -> str | None:
        """The current operator, but only when it was actually proven (see app/session_ownership.py)."""
        return verified_operator()

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
        root = Path(self.manager.settings.auth_root).resolve() / PROFILES_DIRNAME
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
            # Named for the caller, not by the path under AUTH_ROOT: this is
            # what an agent sees after a mistyped auth_profile.
            raise FileNotFoundError(
                f"No saved auth profile {profile_name!r}. browser.list_auth_profiles lists the saved ones."
            )
        return base_path

    def read_metadata(self, profile_name: str, *, strict: bool = False) -> dict[str, Any]:
        """The profile's metadata, {} when it has none.

        With strict, an unreadable file raises PermissionError instead: for an
        ownership check, {} means "unowned, open to anyone", so a damaged file
        must not read as that.
        """
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            return payload
        if strict:
            raise PermissionError(
                f"auth profile '{profile_name}' has unreadable metadata, so its owner cannot be "
                "checked; restore or delete its profile.json"
            )
        return {}

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
            raise FileNotFoundError(f"No file {os.fspath(relative_path)!r} in this session's auth directory")
        return Path(candidate_str)

    def storage_state_source(self, relative_path: str) -> Path:
        """Resolve a create-session `storage_state_path`, with the owner check.

        The path is relative to AUTH_ROOT, which holds two kinds of stored
        logins besides loose files, and each is authorized as its owner's:

        - Auth profiles live under AUTH_ROOT/profiles, so
          `profiles/<name>/state.json.enc` loads a profile without naming it as
          `auth_profile` — which skipped `require_access` and opened a browser
          logged in as the profile's owner.
        - A session saves its own state under AUTH_ROOT/<session_id>/, so
          `<session_id>/state.json` did the same with a session's cookies. That
          directory carries an owner marker (see mark_session_auth_dir);
          directories without one are settled by require_session_record_access.
        """
        source = self.safe_auth_path(relative_path, must_exist=True)
        profile_root = Path(os.path.realpath(os.fspath(self.root())))
        if source.is_relative_to(profile_root) and source != profile_root:
            profile_name = source.relative_to(profile_root).parts[0]
            self.require_access(profile_name, action="opening a session from an auth profile")
            return source
        session_dir = self._session_auth_dir_of(source)
        if session_dir is not None:
            owner = self._read_session_dir_owner(session_dir)
            if owner is not None and not may_use(owner):
                raise PermissionError(_FOREIGN_SESSION_STATE)
        return source

    async def require_session_record_access(self, source: Path) -> None:
        """For state under a session's auth directory that has no owner marker.

        State saved before the marker existed: its session record, live or
        persisted, still says who owned it. With neither, nobody can be named
        and the path stays open, as it was.
        """
        session_dir = self._session_auth_dir_of(source)
        if session_dir is None or (session_dir / SESSION_OWNER_MARKER).exists():
            return
        owner = await self.manager.session_owner(session_dir.name)
        if owner is not None and not may_use(owner):
            raise PermissionError(_FOREIGN_SESSION_STATE)

    def _session_auth_dir_of(self, source: Path) -> Path | None:
        """AUTH_ROOT/<first component> for a file inside a session-style directory, else None."""
        auth_root = Path(os.path.realpath(os.fspath(Path(self.manager.settings.auth_root).resolve())))
        if not source.is_relative_to(auth_root):
            return None
        parts = source.relative_to(auth_root).parts
        if len(parts) < 2 or parts[0] == PROFILES_DIRNAME:
            return None
        return auth_root / parts[0]

    @staticmethod
    def mark_session_auth_dir(auth_dir: Path, owner: str | None) -> None:
        """Record who owns the state about to be written into a session's auth directory.

        Unowned sessions (shared token, no token) record nothing, so those
        deployments are unchanged.
        """
        if not owner:
            return
        auth_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(auth_dir / SESSION_OWNER_MARKER, json.dumps({"owner": owner}))

    @staticmethod
    def _read_session_dir_owner(session_dir: Path) -> str | None:
        """The marker's owner, None when there is no marker.

        A marker that exists but cannot be read raises: None means "no owner,
        open to anyone", which a damaged file must not read as (the same rule
        as read_metadata(strict=True) for profiles).
        """
        marker = session_dir / SESSION_OWNER_MARKER
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        owner = payload.get("owner") if isinstance(payload, dict) else None
        if isinstance(owner, str) and owner.strip():
            return owner.strip()
        raise PermissionError(
            f"the owner marker in session auth directory '{session_dir.name}' is unreadable, so its owner "
            f"cannot be checked; restore or delete {SESSION_OWNER_MARKER}"
        )

    def safe_auth_path(self, relative_path: str, must_exist: bool = False) -> Path:
        root_str = os.path.realpath(os.fspath(Path(self.manager.settings.auth_root).resolve()))
        candidate_str = os.path.realpath(os.path.join(root_str, os.fspath(relative_path)))
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        if not candidate_str.startswith(root_prefix):
            raise PermissionError("path must stay inside the auth root")

        os.makedirs(os.path.dirname(candidate_str), exist_ok=True)

        if must_exist and not os.path.exists(candidate_str):
            raise FileNotFoundError(f"No file {os.fspath(relative_path)!r} in the auth directory")
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
        # Encrypted state supersedes the plaintext copy beside it, and the archive
        # is what leaves the box: a state.json saved before the key was configured
        # stays beside state.json.enc until an encrypted save removes it. A save
        # keeps state.json.enc present throughout (_put_in_place), so an export
        # during one never finds that copy unsuperseded. A profile with only a
        # plaintext state exports it, key or no key.
        superseded = {
            path.relative_to(source_dir.parent).as_posix().removesuffix(".enc")
            for path in source_dir.rglob("*.enc")
            if path.is_file()
        }

        def skipped(arcname: str, name: str) -> bool:
            # By name, before the file is read: a save's own files come and go meanwhile.
            return arcname in superseded or _NOT_EXPORTED.fullmatch(name) is not None

        with tarfile.open(str(dest), "w:gz") as tar:
            _add_tree(tar, source_dir, source_dir.name, skipped)

    @staticmethod
    def safe_archive_member_name(member_name: str) -> PurePosixPath:
        candidate = PurePosixPath(member_name.replace("\\", "/"))
        if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
            raise ValueError("archive contains an unsafe path")
        return candidate
