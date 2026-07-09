from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...models import BrowserActionDecision

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
        safe_path = self.safe_path(file_path, session=session)
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

        async def operation() -> None:
            locator = session.page.locator(target["selector"]).first
            await locator.set_input_files(str(safe_path))
            await self.manager._settle(session.page)

        result = await self.manager._run_action(
            session,
            "upload",
            {**target, "file_path": str(safe_path), "approved": bool(approval), "approval_id": approval_id},
            operation,
        )
        if approval is not None:
            await self.manager.approvals.mark_executed(approval.id)
        return result

    def safe_path(self, file_path: str, *, session: "BrowserSession" | None = None) -> Path:
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
            if not os.path.exists(candidate_str):
                raise FileNotFoundError(candidate_str)
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

        if not os.path.exists(candidate_str):
            raise FileNotFoundError(candidate_str)
        return Path(candidate_str)

    @staticmethod
    def path_is_contained_by(candidate: Path, root: Path) -> bool:
        root_str = os.path.normcase(os.path.realpath(os.fspath(root)))
        candidate_str = os.path.normcase(os.path.realpath(os.fspath(candidate)))
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        return candidate_str == root_str or candidate_str.startswith(root_prefix)
