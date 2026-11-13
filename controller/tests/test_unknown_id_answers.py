"""An MCP call that names an agent job or an auth profile the controller does not hold is told so.

It was answered with the id alone, the text of the store's KeyError: nothing said the job or the profile was unknown,
or what to do next. An unknown session id has been answered "No session with id ... List sessions to find a live
one, or create a new session." since SessionNotFoundError; these two misses now read the same way, in every tool that
looks a job or an auth profile up by what the call sent.

Only that miss is reworded: a KeyError that names something else, an error of another kind, and the tools whose
job_id or profile_name is a cron job's or a memory profile's keep the text they had.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agent_jobs import AgentJobQueue
from app.models import McpToolCallRequest
from app.tool_gateway import McpToolGateway
from app.tool_gateway import gateway as gateway_module
from app.tool_inputs import AgentJobIdInput, AuthProfileNameInput
from tests.test_agent_jobs import every_session_usable
from tests.test_approval_spend import SESSION, SpendBase

UNKNOWN = "nope123"
NO_JOB = f"No agent job with id {UNKNOWN}. List agent jobs to find one."
NO_PROFILE = f"No auth profile named {UNKNOWN}. List auth profiles to find one."
JOB_TOOLS = (
    "browser.get_agent_job",
    "browser.resume_agent_job",
    "browser.discard_agent_job",
    "browser.cancel_agent_job",
)
LONG_ID = "j" * 60_000


async def no_session_usable(_session_id: str) -> bool:
    return False


class UnknownIdAnswerTests(SpendBase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.queue = AgentJobQueue(
            orchestrator=SimpleNamespace(),
            store_root=Path(self.tempdir.name) / "jobs",
            session_accessible=every_session_usable,
        )
        await self.queue.store.startup()
        self.gateway.job_queue = self.queue

    async def told(self, name: str, **arguments) -> str:
        answer = await self.call(name, **arguments)
        text = answer.content[0].text
        self.assertTrue(answer.isError, text)
        self.assertEqual(answer.structuredContent, {"error": text})
        return text

    async def test_each_tool_that_takes_a_job_id_says_no_job_has_it(self) -> None:
        for name in JOB_TOOLS:
            with self.subTest(name):
                self.assertEqual(await self.told(name, job_id=UNKNOWN), NO_JOB)

    async def test_an_unknown_auth_profile_is_named_as_one(self) -> None:
        self.assertEqual(await self.told("browser.get_auth_profile", profile_name=UNKNOWN), NO_PROFILE)

    async def test_a_profile_name_sent_with_spaces_around_it_is_named_as_it_was_looked_up(self) -> None:
        # The store strips the name before it looks, and its KeyError names the stripped one.
        self.assertEqual(await self.told("browser.get_auth_profile", profile_name=f"  {UNKNOWN}\n"), NO_PROFILE)

    async def test_a_job_in_a_session_the_caller_may_not_use_reads_as_one_that_never_was(self) -> None:
        record = await self.queue.store.create(session_id=SESSION, kind="agent_step", request={})
        self.assertEqual((await self.call("browser.get_agent_job", job_id=record.id)).isError, False)
        self.queue.session_accessible = no_session_usable

        for name in JOB_TOOLS:
            with self.subTest(name):
                self.assertEqual(
                    await self.told(name, job_id=record.id),
                    f"No agent job with id {record.id}. List agent jobs to find one.",
                )

    async def test_every_tool_that_takes_a_job_id_or_an_auth_profile_name_alone_is_reworded(self) -> None:
        # By the input model, so a tool added on one of them is noticed here.
        by_model = {
            name
            for name, spec in self.gateway._registry.tools.items()
            if issubclass(spec.input_model, (AgentJobIdInput, AuthProfileNameInput))
        }

        self.assertEqual(by_model, {*JOB_TOOLS, "browser.get_auth_profile"})
        self.assertEqual(set(gateway_module.UNKNOWN_ID_ANSWERS), by_model)

    # ── what keeps its own text ──────────────────────────────────────────────

    async def test_a_keyerror_that_names_something_else_keeps_its_text(self) -> None:
        self.queue.get_job = AsyncMock(side_effect=KeyError("status"))

        self.assertEqual(await self.told("browser.get_agent_job", job_id=UNKNOWN), "status")

    async def test_an_error_of_another_kind_that_carries_the_id_keeps_its_text(self) -> None:
        self.queue.get_job = AsyncMock(side_effect=ValueError(UNKNOWN))

        self.assertEqual(await self.told("browser.get_agent_job", job_id=UNKNOWN), UNKNOWN)

    async def test_a_job_that_cannot_be_resumed_is_still_told_why(self) -> None:
        record = await self.queue.store.create(session_id=SESSION, kind="agent_step", request={})

        self.assertEqual(await self.told("browser.resume_agent_job", job_id=record.id), "Job is not resumable")

    async def test_a_cron_jobs_id_is_not_called_an_agent_jobs(self) -> None:
        # job_id is a cron job's argument too. The cron service words its own miss; one that named the id alone
        # would stay as it was, and not be told of agent jobs.
        self.gateway.cron_service = SimpleNamespace(trigger_job=AsyncMock(side_effect=KeyError(UNKNOWN)))

        self.assertEqual(await self.told("browser.trigger_cron_job", job_id=UNKNOWN), UNKNOWN)

    async def test_an_unknown_memory_profile_keeps_its_own_wording(self) -> None:
        text = await self.told("browser.get_memory_profile", profile_name=UNKNOWN)

        self.assertEqual(text, f"Memory profile not found: '{UNKNOWN}'")

    # ── the id, cut ──────────────────────────────────────────────────────────

    async def test_an_id_longer_than_the_tools_allow_never_reaches_the_lookup(self) -> None:
        self.assertEqual(
            await self.told("browser.get_agent_job", job_id="j" * 121),
            "Invalid arguments for browser.get_agent_job: job_id: String should have at most 120 characters",
        )

    def test_a_long_id_is_cut_as_a_session_id_is(self) -> None:
        # No call reaches this today (the test above); the wording cuts the id itself, as the session's does.
        request = McpToolCallRequest(name="browser.get_agent_job", arguments={"job_id": LONG_ID})

        self.assertEqual(
            McpToolGateway._unknown_id_message(request, LONG_ID),
            f"No agent job with id {LONG_ID[:200] + '...'}. List agent jobs to find one.",
        )
