"""A proxy persona belongs to the token-verified operator who first saved it.

Personas hold proxy credentials and decide which network a session goes out
through, so a persona one operator could list, overwrite, delete or open a
session with was a path to another operator's paid proxy, and to redirecting
their traffic through a server of the attacker's choosing. Ownership follows the
rest of the controller (tests/test_memory_ownership.py): only a named credential
(API_BEARER_TOKENS) owns anything, so a shared-token deployment and every persona
written before owners existed stay open to everyone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_manager import BrowserManager
from app.models import McpToolCallRequest, OperatorIdentity
from app.proxy_personas import ProxyPersonaStore
from app.routes.operations import create_operations_router
from app.session_ownership import acting_as, as_system, owner_identity
from app.tool_gateway.gateway import McpToolGateway
from tests.test_browser_manager_create_session import FakeBrowser, FakeContext, FakePage, _settings

ALICE = OperatorIdentity(id="alice", source="token")
BOB = OperatorIdentity(id="bob", source="token")
ALICE_BY_HEADER = OperatorIdentity(id="alice", source="header")
PASSWORD = "alice-proxy-password"
ALICE_SERVER = "http://alice.example:8080"
EVIL_SERVER = "http://evil-proxy.example:3128"


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "personas.json"


@pytest.fixture()
def store(path: Path) -> ProxyPersonaStore:
    return ProxyPersonaStore(path)


def stored(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_alice_persona(store: ProxyPersonaStore) -> None:
    with acting_as(ALICE):
        store.set_persona("alice-us", server=ALICE_SERVER, username="au", password=PASSWORD)


def test_a_persona_records_the_verified_operator_who_first_saved_it_and_keeps_it(store, path) -> None:
    save_alice_persona(store)
    assert stored(path)["alice-us"]["owner"] == "alice"

    with acting_as(ALICE):
        store.set_persona("alice-us", server="http://alice2.example:8080")

    assert stored(path)["alice-us"]["owner"] == "alice"


def test_another_operator_cannot_list_get_resolve_overwrite_or_delete_it(store, path) -> None:
    save_alice_persona(store)

    with acting_as(BOB):
        assert store.list_personas() == []
        with pytest.raises(KeyError):
            store.get_persona("alice-us")
        with pytest.raises(KeyError):
            store.resolve_proxy("alice-us")  # would hand Bob Alice's proxy credentials
        with pytest.raises(PermissionError):
            store.set_persona("alice-us", server=EVIL_SERVER)
        assert store.delete_persona("alice-us") is False

    on_disk = stored(path)["alice-us"]
    assert (on_disk["server"], on_disk["password"], on_disk["owner"]) == (ALICE_SERVER, PASSWORD, "alice")


def test_another_operators_persona_looks_exactly_like_a_missing_one(store) -> None:
    save_alice_persona(store)

    with acting_as(BOB):
        with pytest.raises(KeyError) as owned_by_alice:
            store.resolve_proxy("alice-us")
        with pytest.raises(KeyError) as missing:
            store.resolve_proxy("nobody-has-this")

    assert str(owned_by_alice.value).replace("alice-us", "X") == str(missing.value).replace("nobody-has-this", "X")


def test_the_owner_keeps_full_access(store) -> None:
    save_alice_persona(store)

    with acting_as(ALICE):
        assert [persona["name"] for persona in store.list_personas()] == ["alice-us"]
        assert store.get_persona("alice-us")["server"] == ALICE_SERVER
        assert store.resolve_proxy("alice-us")["password"] == PASSWORD
        assert store.delete_persona("alice-us") is True
        assert store.list_personas() == []


@pytest.mark.parametrize("intruder", [ALICE_BY_HEADER, None], ids=["header-only", "anonymous"])
def test_a_header_or_anonymous_caller_is_not_the_owner(store, intruder) -> None:
    save_alice_persona(store)

    with acting_as(intruder):
        assert store.list_personas() == []
        with pytest.raises(KeyError):
            store.resolve_proxy("alice-us")
        with pytest.raises(PermissionError):
            store.set_persona("alice-us", server=EVIL_SERVER)


def test_a_header_asserted_identity_never_owns_a_new_persona(store, path) -> None:
    with acting_as(ALICE_BY_HEADER):
        store.set_persona("shared", server="http://shared.example:8080")

    assert stored(path)["shared"]["owner"] is None
    with acting_as(BOB):
        assert [persona["name"] for persona in store.list_personas()] == ["shared"]
        assert store.resolve_proxy("shared")["server"] == "http://shared.example:8080"


def test_the_system_actor_may_use_every_persona(store) -> None:
    save_alice_persona(store)

    with as_system():
        assert [persona["name"] for persona in store.list_personas()] == ["alice-us"]
        assert store.resolve_proxy("alice-us")["server"] == ALICE_SERVER


def test_a_cron_run_resolves_as_the_job_owner(store) -> None:
    """Cron fires inside acting_as(owner_identity(job owner)); that identity decides."""
    save_alice_persona(store)

    with acting_as(owner_identity("alice")):
        assert store.resolve_proxy("alice-us")["server"] == ALICE_SERVER
    with acting_as(owner_identity("bob")), pytest.raises(KeyError):
        store.resolve_proxy("alice-us")
    with acting_as(owner_identity(None)), pytest.raises(KeyError):
        store.resolve_proxy("alice-us")


def test_the_summary_does_not_name_the_owner_or_the_password(store) -> None:
    save_alice_persona(store)

    with acting_as(ALICE):
        listed = store.list_personas()[0]
        saved = store.set_persona("alice-us", server=ALICE_SERVER, password=PASSWORD)
        fetched = store.get_persona("alice-us")

    for summary in (listed, saved, fetched):
        assert "owner" not in summary
        assert "password" not in summary
        assert PASSWORD not in json.dumps(summary)


# ── existing deployments ────────────────────────────────────────────────────


def write_legacy_file(path: Path) -> None:
    """A file written before owners existed: no persona has an owner key."""
    path.write_text(
        json.dumps({"old": {"server": "http://old.example:1", "username": "ou", "password": "legacy-pw"}}),
        encoding="utf-8",
    )


def test_a_persona_from_before_owners_existed_stays_usable_by_everyone(path) -> None:
    write_legacy_file(path)
    store = ProxyPersonaStore(path)

    for operator in (ALICE, BOB, ALICE_BY_HEADER, None):
        with acting_as(operator):
            assert [persona["name"] for persona in store.list_personas()] == ["old"]
            assert store.resolve_proxy("old")["password"] == "legacy-pw"


def test_updating_a_legacy_persona_does_not_claim_it(path) -> None:
    write_legacy_file(path)
    store = ProxyPersonaStore(path)

    with acting_as(BOB):
        store.set_persona("old", server="http://old.example:2", password="legacy-pw")

    assert stored(path)["old"]["owner"] is None
    with acting_as(ALICE):
        assert store.resolve_proxy("old")["server"] == "http://old.example:2"


def test_an_owner_added_to_the_file_by_hand_takes_effect_and_survives_saves(path) -> None:
    write_legacy_file(path)
    data = stored(path)
    data["old"]["owner"] = "alice"
    path.write_text(json.dumps(data), encoding="utf-8")
    store = ProxyPersonaStore(path)

    with acting_as(BOB):
        assert store.list_personas() == []
        with pytest.raises(KeyError):
            store.resolve_proxy("old")
    with acting_as(ALICE):
        store.set_persona("alice-us", server=ALICE_SERVER)  # a save rewrites the whole file

    assert stored(path)["old"]["owner"] == "alice"
    assert stored(path)["alice-us"]["owner"] == "alice"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_owner_in_the_file_means_unowned(path, value) -> None:
    path.write_text(json.dumps({"p": {"server": "http://p.example:1", "owner": value}}), encoding="utf-8")

    with acting_as(BOB):
        assert [persona["name"] for persona in ProxyPersonaStore(path).list_personas()] == ["p"]


def test_ownerless_personas_are_reported_for_the_startup_warning(path) -> None:
    write_legacy_file(path)
    store = ProxyPersonaStore(path)
    with acting_as(ALICE):
        store.set_persona("alice-us", server=ALICE_SERVER)

    assert store.ownerless_names() == ["old"]


def test_ownerless_personas_are_reported_even_to_a_caller_who_cannot_use_owned_ones(path) -> None:
    write_legacy_file(path)

    with acting_as(BOB):
        assert ProxyPersonaStore(path).ownerless_names() == ["old"]


# ── the other doors: HTTP, MCP and session creation ─────────────────────────


def test_http_routes_apply_the_same_rule(store) -> None:
    save_alice_persona(store)
    app = FastAPI()
    app.include_router(create_operations_router(manager=MagicMock(), proxy_store=store, cron_service=MagicMock()))

    # The HTTP client runs in another thread, where acting_as() does not reach.
    with patch("app.session_ownership.get_current_operator") as identity, TestClient(app) as client:
        identity.return_value = BOB
        assert client.get("/proxy-personas").json() == []
        assert client.get("/proxy-personas/alice-us").status_code == 404
        assert client.delete("/proxy-personas/alice-us").status_code == 404
        overwrite = client.post("/proxy-personas", json={"name": "alice-us", "server": EVIL_SERVER})
        assert overwrite.status_code == 409
        assert EVIL_SERVER not in overwrite.text

        identity.return_value = ALICE
        assert [persona["name"] for persona in client.get("/proxy-personas").json()] == ["alice-us"]
        assert client.get("/proxy-personas/alice-us").status_code == 200
        assert client.post("/proxy-personas", json={"name": "alice-us", "server": ALICE_SERVER}).status_code == 200
        assert client.delete("/proxy-personas/alice-us").status_code == 200


async def test_mcp_tools_apply_the_same_rule(store) -> None:
    save_alice_persona(store)
    gateway = McpToolGateway(
        manager=MagicMock(), orchestrator=MagicMock(), job_queue=MagicMock(), proxy_store=store, tool_profile="full"
    )

    with acting_as(BOB):
        listed = await gateway.call_tool(McpToolCallRequest(name="browser.list_proxy_personas", arguments={}))
        overwritten = await gateway.call_tool(
            McpToolCallRequest(
                name="browser.create_proxy_persona", arguments={"name": "alice-us", "server": EVIL_SERVER}
            )
        )
        deleted = await gateway.call_tool(
            McpToolCallRequest(name="browser.delete_proxy_persona", arguments={"name": "alice-us"})
        )

    assert listed.structuredContent == []
    assert overwritten.isError
    assert deleted.structuredContent == {"name": "alice-us", "deleted": False}
    with acting_as(ALICE):
        assert store.resolve_proxy("alice-us")["server"] == ALICE_SERVER


async def test_another_operator_cannot_open_a_session_through_the_persona(store, tmp_path) -> None:
    save_alice_persona(store)
    manager = BrowserManager(_settings(tmp_path), proxy_store=store)
    manager._settle = AsyncMock()  # type: ignore[method-assign]
    manager._maybe_provision_session_tunnel = AsyncMock()  # type: ignore[method-assign]
    browser = FakeBrowser(FakeContext(FakePage()))
    manager._acquire_session_browser = AsyncMock(return_value=(browser, None))  # type: ignore[method-assign]

    with acting_as(BOB), pytest.raises(KeyError):
        await manager.create_session(name="bob-on-alice-proxy", proxy_persona="alice-us")
    assert browser.new_context.await_count == 0

    with acting_as(ALICE):
        await manager.create_session(name="alice-on-alice-proxy", proxy_persona="alice-us")
    proxy = browser.new_context.await_args.kwargs["proxy"]
    assert (proxy["server"], proxy["password"]) == (ALICE_SERVER, PASSWORD)


def test_startup_warns_when_named_tokens_meet_ownerless_personas(path, caplog) -> None:
    from app.proxy_personas import warn_about_ownerless_personas

    write_legacy_file(path)
    store = ProxyPersonaStore(path)

    with caplog.at_level(logging.WARNING, logger="app.proxy_personas"):
        warn_about_ownerless_personas(store, named_tokens=False)
        assert caplog.records == []
        warn_about_ownerless_personas(store, named_tokens=True)

    assert len(caplog.records) == 1
    assert "old" in caplog.records[0].getMessage()
    assert "owner" in caplog.records[0].getMessage()
    assert "legacy-pw" not in caplog.records[0].getMessage()
