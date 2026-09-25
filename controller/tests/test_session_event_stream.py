from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app import events
from app.routes.session_diagnostics import create_session_diagnostics_router


class FakeManager:
    async def get_session(self, session_id: str) -> object:
        return SimpleNamespace(id=session_id)


class FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


class SessionEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_ends_when_the_session_closes(self) -> None:
        """The stream stayed open on keepalives after its session closed,
        holding the connection until the client gave up."""
        router = create_session_diagnostics_router(
            manager=FakeManager(), settings=SimpleNamespace(sse_keepalive_seconds=0.05)
        )
        endpoint = next(r.endpoint for r in router.routes if r.path == "/sessions/{session_id}/events")
        response = await endpoint("session-1", FakeRequest())

        async def drain() -> list[str]:
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if len(chunks) == 1:
                    events.emit_action("session-1", "click", "ok")
                    events.emit_session("session-1", "closed")
            return chunks

        chunks = await asyncio.wait_for(drain(), timeout=5)

        self.assertIn('"status": "closed"', chunks[-1])
        self.assertNotIn("session-1", events._SESSION_QUEUES)


if __name__ == "__main__":
    unittest.main()
