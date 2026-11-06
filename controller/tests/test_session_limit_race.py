"""MAX_SESSIONS holds for creates that await between the early limit check and the reservation (W4-9).

create_session checked the limit, then awaited (the storage-state access check, the memory profile read) before it
counted itself as creating, under a comment saying nothing awaited in between: two creates that both passed the early
check while the other was still reading went past MAX_SESSIONS together.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.session_ownership import acting_as
from tests.test_auth_state_ownership import ALICE, build_manager


class _SlowMemory:
    """A memory store whose read waits until both creates have passed the early limit check."""

    def __init__(self, readers: int) -> None:
        self.arrived = 0
        self.readers = readers
        self.all_arrived = asyncio.Event()

    async def get(self, name: str):
        self.arrived += 1
        if self.arrived >= self.readers:
            self.all_arrived.set()
        await self.all_arrived.wait()
        return None


class SessionLimitRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.manager, _ = build_manager(Path(self.tmp.name))
        self.manager.settings.max_sessions = 1

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_two_creates_whose_memory_read_awaits_cannot_both_pass_the_limit(self) -> None:
        self.manager.memory = _SlowMemory(readers=2)

        async def create(name: str):
            with acting_as(ALICE):
                return await self.manager.create_session(name=name, memory_profile="checkout")

        results = await asyncio.wait_for(
            asyncio.gather(create("first"), create("second"), return_exceptions=True), timeout=30
        )

        created = [result for result in results if isinstance(result, dict)]
        refused = [result for result in results if isinstance(result, BaseException)]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(len(refused), 1, results)
        self.assertIn("Session limit reached: max_sessions=1", str(refused[0]))
        self.assertEqual(len(self.manager.sessions), 1)
        self.assertEqual(self.manager.session_lifecycle._creating, 0)


if __name__ == "__main__":
    unittest.main()
