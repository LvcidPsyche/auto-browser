from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tarfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ...utils import UTC, utc_now
from ...witness import WitnessActionContext

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession


class BrowserAuthProfileService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

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
    ) -> dict[str, Any]:
        normalized = self.normalize_name(profile_name)
        profile_state_path = self.state_base_path(normalized, create=True)
        auth_info = await self.manager.auth_state.write_storage_state(session.context, profile_state_path)
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

        def _extract() -> str:
            with tarfile.open(str(src), "r:gz") as tar:
                members = tar.getmembers()
                if not members:
                    raise ValueError("archive is empty")

                safe_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
                top_level: str | None = None
                for member in members:
                    if member.issym() or member.islnk() or member.isdev():
                        raise ValueError("archive contains an unsupported member type")
                    if not member.isdir() and not member.isfile():
                        continue
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
                if dest_dir.exists() and not overwrite:
                    raise FileExistsError(f"profile '{profile_name}' already exists; pass overwrite=true")
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)

                for member, safe_path in safe_members:
                    relative = Path(*safe_path.parts)
                    target = self.resolve_contained_path(profile_root, relative)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise ValueError("archive member could not be read")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)

                return profile_name

        profile_name = await asyncio.to_thread(_extract)
        return {
            "profile_name": profile_name,
            "profile_path": str(profile_root / profile_name),
            "imported": True,
        }

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
