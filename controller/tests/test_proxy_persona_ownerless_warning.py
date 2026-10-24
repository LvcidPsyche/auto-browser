"""The startup warning about proxy personas that every operator can still use.

Personas saved before owners existed, or saved with the shared token, have no owner and stay open
to every operator, including overwrite and delete, until the host adds "owner" by hand. The
warning at startup is the only sign of that, so it has to say how many there are, what to do,
and catch the hand edit that looks like a fix and is not: `"owner": ""` reads as no owner.
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.proxy_personas import ProxyPersonaStore, warn_about_ownerless_personas
from app.startup import extensions

SERVER = "http://p.example:1"


def write_personas(path: Path, personas: dict) -> ProxyPersonaStore:
    path.write_text(json.dumps({name: {"server": SERVER, **fields} for name, fields in personas.items()}), "utf-8")
    return ProxyPersonaStore(path)


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "personas.json"


def warnings_for(store: ProxyPersonaStore, caplog, *, named_tokens: bool = True) -> list[str]:
    with caplog.at_level(logging.WARNING, logger="app.proxy_personas"):
        warn_about_ownerless_personas(store, named_tokens=named_tokens)
    return [record.getMessage() for record in caplog.records]


# ── one warning, with the count and the fix ─────────────────────────────────


def test_one_warning_names_how_many_personas_are_open_and_how_to_add_an_owner(path, caplog) -> None:
    store = write_personas(
        path, {"legacy-a": {}, "legacy-b": {"owner": None}, "legacy-c": {}, "mine": {"owner": "alice"}}
    )

    messages = warnings_for(store, caplog)

    assert len(messages) == 1
    assert "3 proxy personas" in messages[0]
    assert all(name in messages[0] for name in ("legacy-a", "legacy-b", "legacy-c"))
    assert "mine" not in messages[0]
    assert '"owner": "<operator id>"' in messages[0]


def test_a_single_persona_is_counted_as_one(path, caplog) -> None:
    messages = warnings_for(write_personas(path, {"only": {}}), caplog)

    assert len(messages) == 1
    assert "1 proxy persona " in messages[0]


def test_no_warning_when_every_persona_has_an_owner(path, caplog) -> None:
    assert warnings_for(write_personas(path, {"a": {"owner": "alice"}, "b": {"owner": "bob"}}), caplog) == []


def test_no_warning_without_a_file(path, caplog) -> None:
    assert warnings_for(ProxyPersonaStore(path), caplog) == []


@pytest.mark.parametrize("fields", [{}, {"owner": ""}])
def test_no_warning_when_operators_cannot_be_told_apart(path, caplog, fields) -> None:
    store = write_personas(path, {"a": fields})

    assert warnings_for(store, caplog, named_tokens=False) == []


# ── an empty owner is not an owner ──────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_empty_owner_is_called_out_by_name(path, caplog, blank) -> None:
    store = write_personas(path, {"typo": {"owner": blank}, "legacy-open": {}, "mine": {"owner": "alice"}})

    messages = warnings_for(store, caplog)

    assert len(messages) == 2
    assert "2 proxy personas" in messages[0]  # an empty owner is still no owner
    assert "typo" in messages[1]
    assert "legacy-open" not in messages[1]
    assert "empty" in messages[1]
    assert "no owner" in messages[1]


def test_a_missing_owner_and_a_null_owner_are_not_called_empty(path, caplog) -> None:
    store = write_personas(path, {"a": {}, "b": {"owner": None}})

    assert len(warnings_for(store, caplog)) == 1


def test_the_empty_owner_report_skips_entries_the_store_ignores(path) -> None:
    path.write_text(
        json.dumps({"ok": {"server": SERVER, "owner": ""}, "no-server": {"owner": ""}, "not-an-object": ""}), "utf-8"
    )

    assert ProxyPersonaStore(path).blank_owner_names() == ["ok"]


def test_the_empty_owner_report_is_empty_without_a_readable_file(path) -> None:
    assert ProxyPersonaStore(path).blank_owner_names() == []
    path.write_text("{not json", "utf-8")
    assert ProxyPersonaStore(path).blank_owner_names() == []
    assert ProxyPersonaStore(None).blank_owner_names() == []


def test_the_warning_never_carries_a_secret(path, caplog) -> None:
    store = write_personas(path, {"a": {"password": "hunter2", "username": "u"}, "b": {"owner": "", "password": "pw2"}})

    messages = warnings_for(store, caplog)

    assert messages
    assert not any(secret in message for message in messages for secret in ("hunter2", "pw2", SERVER))


# ── the controller's own startup says it ────────────────────────────────────
# The tests above call the function. The advisory tells operators to treat this warning as the
# signal, so what has to hold is that app.main's lifespan calls it, with the right deployment mode.


def start_controller(personas: ProxyPersonaStore, **credentials: str) -> None:
    """Run the real app.main lifespan once, with the services that need a browser or a disk stubbed out."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(main_module, "validate_runtime_policy", return_value=SimpleNamespace(errors=[], warnings=[]))
        )
        stack.enter_context(patch.object(extensions, "register_extensions"))
        for service in (main_module.manager, main_module.job_queue, main_module.cron_service, main_module.maintenance):
            for name in ("startup", "shutdown"):
                stack.enter_context(patch.object(service, name, new=AsyncMock()))
        stack.enter_context(patch.object(main_module, "proxy_store", personas))
        for field, value in {"api_bearer_tokens": "", "api_bearer_token": None, **credentials}.items():
            stack.enter_context(patch.object(main_module.settings, field, value))
        with TestClient(main_module.app):
            pass


def test_startup_warns_about_ownerless_personas_when_tokens_are_named(path, caplog) -> None:
    store = write_personas(path, {"legacy-open": {}, "mine": {"owner": "alice"}})

    with caplog.at_level(logging.WARNING, logger="app.proxy_personas"):
        start_controller(store, api_bearer_tokens="alice:token-a,bob:token-b")

    messages = [record.getMessage() for record in caplog.records if record.name == "app.proxy_personas"]
    assert len(messages) == 1
    assert "1 proxy persona " in messages[0]
    assert "legacy-open" in messages[0]
    assert "mine" not in messages[0]


def test_startup_stays_quiet_when_one_shared_token_cannot_tell_operators_apart(path, caplog) -> None:
    store = write_personas(path, {"legacy-open": {}})

    with caplog.at_level(logging.WARNING, logger="app.proxy_personas"):
        start_controller(store, api_bearer_token="shared-token")

    assert [record for record in caplog.records if record.name == "app.proxy_personas"] == []
