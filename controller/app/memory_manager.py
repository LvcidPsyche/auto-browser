from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .utils import utc_now

logger = logging.getLogger(__name__)


class MemoryProfile(BaseModel):
    name: str
    created_at: str
    updated_at: str
    goal_summary: str = ""
    discovered_selectors: dict[str, str] = Field(default_factory=dict)
    completed_steps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_system_prompt(self) -> str:
        parts = [f"[Memory: {self.name}]"]
        if self.goal_summary:
            parts.append(f"Previous goal context: {self.goal_summary}")
        if self.completed_steps:
            steps = "\n".join(f"  - {step}" for step in self.completed_steps[-10:])
            parts.append(f"Previously completed steps:\n{steps}")
        if self.discovered_selectors:
            selectors = "\n".join(f"  {key}: {value}" for key, value in list(self.discovered_selectors.items())[:20])
            parts.append(f"Known selectors:\n{selectors}")
        if self.notes:
            notes = "\n".join(f"  - {note}" for note in self.notes[-5:])
            parts.append(f"Notes:\n{notes}")
        return "\n\n".join(parts)


class MemoryManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _profile_path(self, name: str) -> Path:
        safe_name = "".join(character if character.isalnum() or character in "-_" else "_" for character in name)
        return self.root / f"{safe_name}.json"

    async def save(
        self,
        name: str,
        *,
        goal_summary: str = "",
        completed_steps: list[str] | None = None,
        discovered_selectors: dict[str, str] | None = None,
        notes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryProfile:
        async with self._lock:
            existing = await self._load(name)
            now = utc_now()
            if existing:
                profile = existing.model_copy(
                    update={
                        "updated_at": now,
                        "goal_summary": goal_summary or existing.goal_summary,
                        "completed_steps": (existing.completed_steps + (completed_steps or []))[-100:],
                        "discovered_selectors": {
                            **existing.discovered_selectors,
                            **(discovered_selectors or {}),
                        },
                        "notes": (existing.notes + (notes or []))[-50:],
                        "metadata": {**existing.metadata, **(metadata or {})},
                    }
                )
            else:
                profile = MemoryProfile(
                    name=name,
                    created_at=now,
                    updated_at=now,
                    goal_summary=goal_summary,
                    completed_steps=completed_steps or [],
                    discovered_selectors=discovered_selectors or {},
                    notes=notes or [],
                    metadata=metadata or {},
                )

            path = self._profile_path(name)
            tmp_path = path.with_suffix(".json.tmp")
            await asyncio.to_thread(tmp_path.write_text, profile.model_dump_json(indent=2), "utf-8")
            await asyncio.to_thread(tmp_path.replace, path)
            logger.info("memory profile saved: %s", name)
            return profile

    async def get(self, name: str) -> MemoryProfile | None:
        return await self._load(name)

    async def list(self) -> list[dict[str, Any]]:
        def _list_sync() -> list[dict[str, Any]]:
            if not self.root.exists():
                return []
            profiles: list[dict[str, Any]] = []
            for path in sorted(self.root.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                profiles.append(
                    {
                        "name": data.get("name", path.stem),
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "goal_summary": data.get("goal_summary", ""),
                        "step_count": len(data.get("completed_steps", [])),
                    }
                )
            return profiles

        return await asyncio.to_thread(_list_sync)

    async def delete(self, name: str) -> bool:
        path = self._profile_path(name)
        if path.exists():
            await asyncio.to_thread(path.unlink)
            return True
        return False

    async def _load(self, name: str) -> MemoryProfile | None:
        path = self._profile_path(name)

        def _read_sync() -> MemoryProfile | None:
            if not path.exists():
                return None
            return MemoryProfile.model_validate_json(path.read_text(encoding="utf-8"))

        return await asyncio.to_thread(_read_sync)
