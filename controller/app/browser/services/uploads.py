from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...approvals import effect_started, held_for_execution
from ...models import BrowserActionDecision
from ...session_ownership import may_use
from .sessions import SESSION_ID_HEX_DIGITS, is_session_id

if TYPE_CHECKING:
    from ...browser_manager import BrowserSession


class BrowserUploadService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def upload(
        self,
        session_id: str,
        *,
        file_path: str,
        approved: bool,
        approval_id: str | None = None,
        selector: str | None = None,
        element_id: str | None = None,
    ) -> dict[str, Any]:
        session = await self.manager.get_session(session_id)
        safe_path = await self.safe_path(file_path, session=session)
        approval = await self.manager.actions.require_decision_approval(
            session_id,
            BrowserActionDecision(
                action="upload",
                reason="Manual upload request",
                selector=selector,
                element_id=element_id,
                file_path=file_path,
                risk_category="upload",
            ),
            approval_id=approval_id,
            fallback_reason="Upload actions require approval",
        )

        target = self.manager.actions.resolve_target(selector=selector, element_id=element_id)
        payload = {**target, "file_path": str(safe_path), "approved": approval is not None, "approval_id": approval_id}
        transferred = False
        began = False

        def begin_transfer() -> None:
            # The effect first: refused there, the upload has not begun, and a block has nothing to roll back.
            nonlocal began
            effect_started()
            began = True

        def mark_transferred() -> None:
            # Called the moment the raw transfer returns, before anything else is awaited: from then
            # on the pipeline reports a step that fails with the upload, never as a failure to retry.
            nonlocal transferred
            transferred = True

        async def operation() -> None:
            # effect_started just before each transfer, not once it returns: a file can go in and the
            # call still raise (a timeout, a lost reply), and the approval held for it, here or by an
            # outer layer, must stay spent, since executing it again would upload the file again. A
            # refusal before the transfer (not a file input, a marker moved) gives it back.
            if target.get("element_id"):
                element = self.manager.element_ids.element(session.page, target["element_id"])
                # Recorded with the action: whether the registered input holds the file (best-effort).
                payload["delivery"] = await element.set_input_files(
                    str(safe_path), before_transfer=begin_transfer, on_transfer=mark_transferred
                )
            else:
                locator = session.page.locator(target["selector"]).first
                begin_transfer()
                await locator.set_input_files(str(safe_path))
                mark_transferred()
            await self.manager._settle(session.page)

        def change_made() -> bool:
            return transferred

        def has_begun() -> bool:
            return began

        if approval is None:
            return await self.manager._run_action(
                session, "upload", payload, operation, change_made=change_made, began=has_begun
            )
        async with held_for_execution(self.manager.approvals, approval.id, may_give_back=True):
            return await self.manager._run_action(
                session, "upload", payload, operation, change_made=change_made, began=has_begun
            )

    async def safe_path(self, file_path: str, *, session: "BrowserSession" | None = None) -> Path:
        """Resolve a staged file: inside UPLOAD_ROOT, and not in another operator's session directory.

        A session may use its own directory (UPLOAD_ROOT/<session_id>/) and the
        files directly in UPLOAD_ROOT, which every operator shares. The path
        check alone would also let `<other_session_id>/file` through, since that
        is inside UPLOAD_ROOT, so a directory belonging to another session is
        refused unless the caller may use that session. The owner comes from the
        session record, live or persisted, so the answer outlives the session.
        Refused before the file is looked up, so its existence is not revealed.
        """
        candidate = self._resolve(file_path, session)
        await self._require_directory_access(candidate, session)
        if not os.path.exists(candidate):
            if not os.path.isabs(file_path):
                # Resolution fell back to the session's own directory, but the
                # name may also point into another session's directory under
                # UPLOAD_ROOT: answer that the same way whether or not it exists.
                shared = os.path.realpath(os.path.join(self.manager.settings.upload_root, file_path))
                await self._require_directory_access(Path(shared), session)
            raise FileNotFoundError(_missing_upload(file_path))
        return candidate

    def _resolve(self, file_path: str, session: "BrowserSession" | None) -> Path:
        """The path file_path names, confined to UPLOAD_ROOT; it may not exist."""
        root = Path(self.manager.settings.upload_root).resolve()
        allowed_roots = [root]
        if session is not None:
            allowed_roots.append(session.upload_dir.resolve())

        raw_path = os.fspath(file_path)
        if os.path.isabs(file_path):
            candidate_str = os.path.realpath(raw_path)
            for allowed_root in allowed_roots:
                root_str = os.path.realpath(os.fspath(allowed_root))
                root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
                if candidate_str.startswith(root_prefix):
                    break
            else:
                raise PermissionError("file_path must stay inside upload root")
            return Path(candidate_str)

        preferred_roots: list[Path] = []
        if session is not None:
            preferred_roots.append(session.upload_dir.resolve())
        preferred_roots.append(root)

        for candidate_root in preferred_roots:
            root_str = os.path.realpath(os.fspath(candidate_root))
            root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
            candidate_str = os.path.realpath(os.path.join(root_str, raw_path))
            if not candidate_str.startswith(root_prefix):
                raise PermissionError("file_path must stay inside upload root")

            if os.path.exists(candidate_str):
                return Path(candidate_str)
        else:
            root_str = os.path.realpath(os.fspath(preferred_roots[0]))
            root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
            candidate_str = os.path.realpath(os.path.join(root_str, raw_path))
            if not candidate_str.startswith(root_prefix):
                raise PermissionError("file_path must stay inside upload root")

        return Path(candidate_str)

    async def _require_directory_access(self, candidate: Path, session: "BrowserSession" | None) -> None:
        if session is not None and candidate.is_relative_to(os.path.realpath(os.fspath(session.upload_dir))):
            return
        root = Path(os.path.realpath(os.fspath(self.manager.settings.upload_root)))
        parts = candidate.relative_to(root).parts if candidate.is_relative_to(root) else ()
        if len(parts) < 2:
            return  # directly in UPLOAD_ROOT: shared with every operator
        # A subfolder named like a session id is that session's, and under named credentials one with no
        # session and no record has no owner to check, so no one gets it (the rule ensure_accessible
        # applies). Any other subfolder is shared, as the root is.
        if is_session_id(parts[0]):
            if not await self.manager.session_accessible(parts[0]):
                raise PermissionError(
                    f"file_path is in a folder of UPLOAD_ROOT whose name has the shape of a session id "
                    f"({SESSION_ID_HEX_DIGITS} lowercase hexadecimal characters, digits-only names included), so it "
                    "counts as that session's upload folder, which this caller "
                    "may not use. If it is not a session's folder, rename it."
                )
            return
        if not may_use(await self.manager.session_owner(parts[0])):
            raise PermissionError("file_path must stay inside upload root")


def _missing_upload(file_path: str) -> str:
    # In the caller's terms: the resolved path under UPLOAD_ROOT means nothing
    # to an agent, and REST answers a bare 404 either way.
    return f"No file {os.fspath(file_path)!r} in the upload directory (UPLOAD_ROOT); put the file there first."
