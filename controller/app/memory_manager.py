from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .session_ownership import may_use, verified_operator
from .utils import atomic_write_text, quoted_for_log, run_to_completion, utc_now

logger = logging.getLogger(__name__)


class MemoryProfile(BaseModel):
    name: str
    # The token-verified operator who first saved it. None when that save had no
    # proven identity (shared token, header-only), and for profiles written before
    # owners existed: those stay open to every caller.
    owner: str | None = None
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
    """Profiles on disk, each usable only by its owner (see MemoryProfile.owner).

    Profile text goes into the agent's goal prompt, so a profile that another
    operator could rewrite, or load into their own session, would let them plant
    instructions for the owner's agent. Reading, appending to, deleting or
    loading another operator's profile raises PermissionError, and list() leaves
    it out.
    """

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
            # Checked against the file the name maps to, before the collision
            # guard below can name someone else's profile in its error.
            self._require_access(name, await self._read(name))
            existing = await self._load(name)
            if existing is None:
                stored_as = await self._stored_name(name)
                if stored_as is not None:
                    # File names fold every character outside [A-Za-z0-9_-]
                    # to "_", so "github.com" and "github_com" share a file.
                    # Saving one merged into the other.
                    raise ValueError(
                        f"memory profile name {quoted_for_log(name)} collides with the existing profile "
                        f"{quoted_for_log(stored_as)}; choose another name"
                    )
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
                    owner=verified_operator(),
                    created_at=now,
                    updated_at=now,
                    goal_summary=goal_summary,
                    completed_steps=completed_steps or [],
                    discovered_selectors=discovered_selectors or {},
                    notes=notes or [],
                    metadata=metadata or {},
                )

            path = self._profile_path(name)
            # Awaited to its end however often the save is cancelled, so the lock goes only once the write has landed:
            # let go at a cancel, a delete that took the lock next was undone by the write (reviews of c4 and c4b).
            await run_to_completion(asyncio.to_thread(atomic_write_text, path, profile.model_dump_json(indent=2)))
            logger.info("memory profile saved: %s", quoted_for_log(name))
            return profile

    async def get(self, name: str) -> MemoryProfile | None:
        profile = await self._load(name)
        self._require_access(name, profile)
        return profile

    async def list(self) -> list[dict[str, Any]]:
        def _list_sync() -> list[dict[str, Any]]:
            if not self.root.exists():
                return []
            profiles: list[dict[str, Any]] = []
            for path in sorted(self.root.glob("*.json")):
                # Only a file that loads as a profile, as get() loads one; any other is skipped and logged. One that
                # held a JSON list failed the whole listing, one that did not parse was skipped unsaid (STORE-36),
                # and other objects were listed though no get could load them (review of c4). The type only: a
                # parser's message quotes the file.
                try:
                    profile = MemoryProfile.model_validate_json(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning(
                        "memory profile file %s skipped: it is not a valid profile (%s)", path.name, type(exc).__name__
                    )
                    continue
                if not may_use(profile.owner):
                    continue
                profiles.append(
                    {
                        "name": profile.name,
                        "created_at": profile.created_at,
                        "updated_at": profile.updated_at,
                        "goal_summary": profile.goal_summary,
                        "step_count": len(profile.completed_steps),
                    }
                )
            return profiles

        return await asyncio.to_thread(_list_sync)

    async def delete(self, name: str) -> bool:
        # Under the lock a save holds: a delete between a save's read and its write was undone by that write.
        async with self._lock:
            profile = await self._load(name)
            if profile is None:
                return False
            self._require_access(name, profile)
            # To its end, as a save's write is: an unlink that landed after the lock went removed the next save's file.
            await run_to_completion(asyncio.to_thread(self._profile_path(name).unlink))
            return True

    @staticmethod
    def _require_access(name: str, profile: MemoryProfile | None) -> None:
        # Names the profile as the caller asked for it, never as stored: the
        # stored name of a colliding profile would reveal another operator's.
        if profile is not None and not may_use(profile.owner):
            raise PermissionError(f"memory profile {quoted_for_log(name)} belongs to another operator")

    async def _load(self, name: str) -> MemoryProfile | None:
        """The profile saved under exactly `name`; a file another name maps to is not it."""
        profile = await self._read(name)
        return profile if profile is not None and profile.name == name else None

    async def _stored_name(self, name: str) -> str | None:
        profile = await self._read(name)
        return profile.name if profile is not None else None

    async def _read(self, name: str) -> MemoryProfile | None:
        path = self._profile_path(name)

        def _read_sync() -> MemoryProfile | None:
            if not path.exists():
                return None
            try:
                return MemoryProfile.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                # Not the parser's message, which quotes the file, and the file can be another operator's (STORE-36).
                logger.warning("memory profile file %s cannot be read: %s", path.name, type(exc).__name__)
                raise ValueError(
                    f"memory profile {quoted_for_log(name)} cannot be read: its file is not a valid profile; fix or "
                    "remove it in MEMORY_ROOT"
                ) from None

        return await asyncio.to_thread(_read_sync)
