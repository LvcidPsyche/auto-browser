"""`upload_approved` on the agent request models is accepted and ignored (STORE-26).

Nothing read it: the orchestrator took it and passed it to `_execute_decision`, which never looked. An upload that needs
an approval gets one through the approval queue (`approval_id`). So the field stays on the request models, so a client
that sends it is not refused, marked deprecated in the schema and warned about once per process, and the orchestrator
no longer takes it.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from app.models import AgentRunRequest, AgentStepRequest
from app.orchestrator import BrowserOrchestrator
from app.routes import agent as agent_routes


@pytest.fixture(autouse=True)
def _warn_afresh():
    """Each test starts a process that has not warned yet."""
    from app import models

    models.warn_upload_approved_is_ignored.cache_clear()
    yield
    models.warn_upload_approved_is_ignored.cache_clear()


@pytest.mark.parametrize("model", [AgentStepRequest, AgentRunRequest])
def test_a_request_that_sets_it_still_validates(model) -> None:
    request = model.model_validate({"provider": "openai", "goal": "g", "upload_approved": True})

    assert request.model_dump()["upload_approved"] is True
    assert model.model_validate({"provider": "openai", "goal": "g"}).model_dump()["upload_approved"] is False


@pytest.mark.parametrize("model", [AgentStepRequest, AgentRunRequest])
def test_the_schema_marks_it_deprecated_and_says_it_is_ignored(model) -> None:
    field = model.model_json_schema()["properties"]["upload_approved"]

    assert field["deprecated"] is True
    assert "ignored" in field["description"].lower()
    assert "approval_id" in field["description"]


def test_it_warns_once_per_process_when_true_and_never_when_false(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, "app.models"):
        AgentRunRequest.model_validate({"provider": "openai", "goal": "g"})
        AgentRunRequest.model_validate({"provider": "openai", "goal": "g", "upload_approved": False})
        assert caplog.records == []

        AgentRunRequest.model_validate({"provider": "openai", "goal": "g", "upload_approved": True})
        AgentStepRequest.model_validate({"provider": "openai", "goal": "g", "upload_approved": True})

    warned = [record for record in caplog.records if "upload_approved" in record.getMessage()]
    assert len(warned) == 1
    assert "deprecated" in warned[0].getMessage()
    assert warned[0].levelno == logging.WARNING


def test_validating_and_dumping_a_request_that_has_it_raises_no_python_deprecation_warning() -> None:
    # A job record that holds the field is validated again by the worker, and dumped when it is queued: neither reads
    # the attribute, which is what pydantic's own DeprecationWarning is raised for.
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        request = AgentRunRequest.model_validate({"provider": "openai", "goal": "g", "upload_approved": True})
        AgentRunRequest.model_validate(request.model_dump())


@pytest.mark.parametrize("method", ["step", "run", "_execute_decision"])
def test_the_orchestrator_no_longer_takes_it(method: str) -> None:
    assert "upload_approved" not in inspect.signature(getattr(BrowserOrchestrator, method)).parameters


def test_nothing_passes_it_on_from_the_routes_or_the_queue() -> None:
    import app.agent_jobs as agent_jobs

    for module in (agent_routes, agent_jobs):
        assert "upload_approved" not in inspect.getsource(module), module.__name__
