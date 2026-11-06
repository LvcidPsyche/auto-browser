"""A loopback controller with no configured credential generates one and enforces it.

Until this release a loopback controller with no API_BEARER_TOKEN served every
route unauthenticated. Loopback described the host publish mapping, not who
could reach port 8000: every container on the compose network, browser-node
included, could drive the API by sending `Host: localhost`. The controller now
loads its token from API_TOKEN_FILE, or generates one there, unless
API_ALLOW_TOKENLESS_LOOPBACK=true keeps the old behaviour.

tests/__init__.py turns the escape hatch on for the rest of the suite, which
models the old default, so every test here sets it explicitly.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import api_token
from app.api_token import ensure_api_credential
from app.auth_policy import resolve_auth_policy
from app.config import Settings
from app.middleware.http import install_controller_http_middleware
from app.runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH, validate_runtime_policy

STRONG_TOKEN = "s" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
LOGGER = "app.api_token"


def make_settings(token_file: Path, *, hatch: bool = False, **overrides: str) -> Settings:
    values = {
        "API_BIND_SCOPE": "loopback",
        "API_ALLOW_TOKENLESS_LOOPBACK": "true" if hatch else "false",
        "API_TOKEN_FILE": str(token_file),
        **overrides,
    }
    return Settings(_env_file=None, **values)


def served_client(settings: Settings) -> TestClient:
    application = FastAPI()

    @application.get("/sessions")
    def sessions() -> dict:
        return {"ok": True}

    @application.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    install_controller_http_middleware(
        application,
        settings=settings,
        rate_limiter=None,
        metrics=SimpleNamespace(enabled=False),
    )
    return TestClient(application)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


# --- Policy -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "hatch", "token", "required", "satisfiable"),
    [
        ("loopback", False, None, True, False),
        ("loopback", True, None, False, True),
        ("loopback", False, STRONG_TOKEN, True, True),
        ("loopback", True, STRONG_TOKEN, True, True),
        ("exposed", False, None, True, False),
        ("exposed", True, None, True, False),
        ("exposed", True, STRONG_TOKEN, True, True),
    ],
)
def test_policy_table(tmp_path: Path, scope: str, hatch: bool, token: str | None, required: bool, satisfiable: bool):
    overrides = {"API_BIND_SCOPE": scope, **({"API_BEARER_TOKEN": token} if token else {})}
    policy = resolve_auth_policy(make_settings(tmp_path / "api-token", hatch=hatch, **overrides))

    assert (policy.required, policy.satisfiable) == (required, satisfiable)


def test_the_policy_reads_the_escape_hatch_only_when_it_is_really_true(tmp_path: Path):
    # Some tests pass MagicMock settings, whose every attribute is truthy.
    settings = SimpleNamespace(
        api_bind_scope="loopback",
        model_fields_set=frozenset({"api_bind_scope"}),
        api_allow_tokenless_loopback="yes",
    )
    assert resolve_auth_policy(settings).required


@pytest.mark.parametrize(
    ("app_env", "scope", "hatch", "token", "error", "warning"),
    [
        ("development", "loopback", True, None, None, "API_BEARER_TOKEN is unset"),
        ("development", "loopback", False, None, "API_TOKEN_FILE", None),
        ("development", "loopback", False, STRONG_TOKEN, None, None),
        ("development", "exposed", True, None, "API_BEARER_TOKEN is required", None),
        ("production", "loopback", True, None, "API_BEARER_TOKEN is required", None),
        ("production", "loopback", False, None, "API_BEARER_TOKEN is required", None),
        ("production", "loopback", False, STRONG_TOKEN, None, None),
    ],
)
def test_runtime_policy_table(
    tmp_path: Path,
    app_env: str,
    scope: str,
    hatch: bool,
    token: str | None,
    error: str | None,
    warning: str | None,
):
    overrides = {"APP_ENV": app_env, "API_BIND_SCOPE": scope, **({"API_BEARER_TOKEN": token} if token else {})}
    report = validate_runtime_policy(make_settings(tmp_path / "api-token", hatch=hatch, **overrides))

    auth_errors = [item for item in report.errors if "API_BEARER_TOKEN" in item or "API_TOKEN_FILE" in item]
    if error is None:
        assert auth_errors == []
    else:
        assert len(auth_errors) == 1 and error in auth_errors[0], report.errors
    tokenless = [item for item in report.warnings if "API_BEARER_TOKEN is unset" in item]
    if warning is None:
        assert tokenless == []
    else:
        assert len(tokenless) == 1 and "compose network" in tokenless[0], report.warnings


# --- The token file -----------------------------------------------------------


def test_a_loopback_controller_generates_a_private_token(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "data" / "api-token"  # the directory does not exist yet
    settings = make_settings(path)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_api_credential(settings)

    saved = path.read_text(encoding="utf-8")
    assert saved.endswith("\n")
    assert settings.api_bearer_token == saved.strip()
    assert len(settings.api_bearer_token) >= MIN_PRODUCTION_BEARER_TOKEN_LENGTH
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any(str(path) in record.getMessage() for record in warnings)
    assert settings.api_bearer_token not in caplog.text


@pytest.mark.parametrize(("os_name", "says_0600"), [("posix", True), ("nt", False)])
def test_the_generation_warning_only_promises_0600_where_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, os_name: str, says_0600: bool
):
    real_os = api_token.os
    monkeypatch.setattr(api_token, "os", SimpleNamespace(**{**vars(real_os), "name": os_name}))
    # Skip the POSIX-only ownership step: the stand-in's name is the only thing under test.
    monkeypatch.setattr(api_token, "_match_parent_owner", lambda _path: None)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_api_credential(make_settings(tmp_path / "api-token"))

    assert ("mode 0600" in caplog.text) is says_0600
    if not says_0600:
        assert "inherits the permissions of its folder" in caplog.text
        assert "API_TOKEN_FILE under your user profile" in caplog.text


def test_a_second_start_reuses_the_saved_token(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "api-token"
    first = make_settings(path)
    ensure_api_credential(first)

    second = make_settings(path)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_api_credential(second)

    assert second.api_bearer_token == first.api_bearer_token
    assert fingerprint(second.api_bearer_token) in caplog.text
    assert second.api_bearer_token not in caplog.text


def test_the_saved_token_is_read_without_the_whitespace_around_it(tmp_path: Path):
    path = tmp_path / "api-token"
    path.write_text(f"  {STRONG_TOKEN}\r\n\n", encoding="utf-8")
    settings = make_settings(path)

    ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN


def test_an_empty_file_is_replaced_with_a_new_token(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "api-token"
    path.write_text(" \n", encoding="utf-8")
    settings = make_settings(path)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_api_credential(settings)

    assert len(settings.api_bearer_token) >= MIN_PRODUCTION_BEARER_TOKEN_LENGTH
    assert path.read_text(encoding="utf-8").strip() == settings.api_bearer_token
    assert "empty" in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        "short-token",
        "has spaces " * 4,
        "é" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
        "tab\tinside-" + "x" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH,
    ],
)
def test_an_unusable_token_is_refused_not_overwritten(tmp_path: Path, content: str):
    path = tmp_path / "api-token"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeError, match="API_TOKEN_FILE") as excinfo:
        ensure_api_credential(make_settings(path))

    assert path.read_text(encoding="utf-8") == content
    assert content.strip() not in str(excinfo.value)


def test_an_unwritable_location_stops_startup_and_names_the_ways_out(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    path = blocker / "api-token"

    with pytest.raises(RuntimeError) as excinfo:
        ensure_api_credential(make_settings(path))

    message = str(excinfo.value)
    for needle in (str(path), "API_TOKEN_FILE", "API_BEARER_TOKEN", "API_ALLOW_TOKENLESS_LOOPBACK"):
        assert needle in message


def test_a_configured_token_wins_and_no_file_is_written(tmp_path: Path):
    path = tmp_path / "api-token"
    settings = make_settings(path, API_BEARER_TOKEN=STRONG_TOKEN)

    ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN
    assert not path.exists()


def test_a_configured_token_ignores_an_existing_file_and_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "api-token"
    stale = "f" * 43
    path.write_text(stale + "\n", encoding="utf-8")
    settings = make_settings(path, API_BEARER_TOKEN=STRONG_TOKEN)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN
    assert path.read_text(encoding="utf-8") == stale + "\n"
    assert "ignored" in caplog.text and str(path) in caplog.text
    assert stale not in caplog.text


def test_named_tokens_mean_no_file(tmp_path: Path):
    path = tmp_path / "api-token"
    settings = make_settings(path, API_BEARER_TOKENS=f"alice:{STRONG_TOKEN}")

    ensure_api_credential(settings)

    assert settings.api_bearer_token is None
    assert not path.exists()


@pytest.mark.parametrize("overrides", [{"API_BIND_SCOPE": "exposed"}, {"APP_ENV": "production"}])
def test_exposed_or_production_creates_nothing(tmp_path: Path, overrides: dict[str, str]):
    path = tmp_path / "api-token"
    settings = make_settings(path, **overrides)

    ensure_api_credential(settings)

    assert settings.api_bearer_token is None
    assert not path.exists()


@pytest.mark.parametrize("overrides", [{"API_BIND_SCOPE": "exposed"}, {"APP_ENV": "production"}])
def test_an_existing_file_is_used_in_any_scope(tmp_path: Path, overrides: dict[str, str]):
    path = tmp_path / "api-token"
    path.write_text(STRONG_TOKEN + "\n", encoding="utf-8")
    settings = make_settings(path, **overrides)

    ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN


def test_the_escape_hatch_serves_tokenless_and_leaves_the_file_alone(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "api-token"
    settings = make_settings(path, hatch=True)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_api_credential(settings)

    assert settings.api_bearer_token is None
    assert not path.exists()
    assert "API_ALLOW_TOKENLESS_LOOPBACK" in caplog.text

    path.write_text(STRONG_TOKEN + "\n", encoding="utf-8")
    ensure_api_credential(settings)
    assert settings.api_bearer_token is None


def test_a_settings_dump_never_carries_a_token(tmp_path: Path):
    settings = make_settings(
        tmp_path / "api-token", API_BEARER_TOKEN=STRONG_TOKEN, API_BEARER_TOKENS="alice:" + STRONG_TOKEN
    )

    dumped = settings.model_dump()

    assert "api_bearer_token" not in dumped and "api_bearer_tokens" not in dumped
    assert STRONG_TOKEN not in settings.model_dump_json()
    assert STRONG_TOKEN not in repr(settings)
    assert settings.api_bearer_token == STRONG_TOKEN  # still readable on the object


def test_the_escape_hatch_does_not_apply_in_production(tmp_path: Path):
    path = tmp_path / "api-token"
    path.write_text(STRONG_TOKEN + "\n", encoding="utf-8")
    settings = make_settings(path, hatch=True, APP_ENV="production")

    ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN


def test_the_escape_hatch_does_not_apply_in_production_at_the_gate_either(tmp_path: Path):
    # Without the lifespan: the policy and the served gate must refuse on their own.
    settings = make_settings(tmp_path / "api-token", hatch=True, APP_ENV="production")

    assert resolve_auth_policy(settings).required is True
    client = served_client(settings)
    assert client.get("/sessions").status_code == 401
    assert client.get("/sessions", headers={"Host": "localhost"}).status_code == 401
    assert client.get("/healthz").status_code == 200


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX permission bits")
def test_a_token_file_others_can_read_is_made_private(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "api-token"
    path.write_text(STRONG_TOKEN + "\n", encoding="utf-8")
    os.chmod(path, 0o644)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_api_credential(make_settings(path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "0600" in caplog.text


# The two POSIX-only steps, run on every platform through a stand-in `os`.


@pytest.mark.parametrize(("mode", "tightened"), [(0o100644, True), (0o100640, True), (0o100600, False)])
def test_group_or_other_bits_are_removed_with_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: int,
    tightened: bool,
):
    path = tmp_path / "api-token"
    fchmods: list[tuple[int, int]] = []
    closed: list[int] = []
    fake_os = _posix_stand_in(
        open=lambda target, flags: 7,
        fstat=lambda fd: SimpleNamespace(st_mode=0o100600 if fchmods else mode),
        fchmod=lambda fd, bits: fchmods.append((fd, bits)),
        close=closed.append,
    )
    monkeypatch.setattr(api_token, "os", fake_os)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        api_token._restrict_mode(path)

    # Changed through the descriptor it opened without following a symlink, never by path.
    assert fchmods == ([(7, 0o600)] if tightened else [])
    assert closed == [7]
    assert ("changed it to 0600" in caplog.text) is tightened


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes: Windows applies the folder's ACL")
@pytest.mark.parametrize("which", ["API_TOKEN_FILE", "TAKEOVER_TOKEN_FILE"])
def test_w5_4_a_filesystem_that_ignores_chmod_is_warned_about_and_the_token_still_used(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, which: str
):
    # A repo on a Windows drive under Docker Desktop, or some FUSE and SMB mounts, accept a chmod and keep the mode:
    # the log said "changed it to 0600" while the file stayed readable beyond its owner.
    from app.api_token import ensure_takeover_token

    marker = "w5-4-marker-" + "m" * MIN_PRODUCTION_BEARER_TOKEN_LENGTH
    api_path, takeover_path = tmp_path / "api-token", tmp_path / "takeover-token"
    settings = make_settings(api_path, TAKEOVER_TOKEN_FILE=str(takeover_path))
    path = api_path if which == "API_TOKEN_FILE" else takeover_path
    for each, value in ((api_path, STRONG_TOKEN), (takeover_path, marker)):
        each.write_text(value + "\n", encoding="utf-8")
        each.chmod(0o600)
    path.chmod(0o644)

    with patch.object(api_token.os, "fchmod", lambda fd, bits: None), caplog.at_level(logging.INFO, logger=LOGGER):
        ensure_api_credential(settings)
        ensure_takeover_token(settings)

    assert "changed it" not in caplog.text
    warned = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warned) == 1
    assert which in warned[0] and str(path) in warned[0] and "did not keep" in warned[0]
    assert "644" in warned[0]
    assert settings.api_bearer_token == STRONG_TOKEN
    assert settings.takeover_token == marker


def _posix_stand_in(**functions) -> SimpleNamespace:
    """An os module stand-in for the POSIX branch, so it runs on a Windows test host too."""
    return SimpleNamespace(name="posix", O_RDONLY=0, O_NOFOLLOW=0o400000, **functions)


def test_a_symlinked_token_file_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # Kubernetes secret mounts are symlinks, and chmod would change the target (a file we may not own).
    # O_NOFOLLOW makes opening a symlink fail with ELOOP; the target is then opened only to read its mode.
    opened: list[int] = []

    def refuse_symlink(target, flags):
        opened.append(flags)
        if flags & fake_os.O_NOFOLLOW:
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return 9

    fchmods: list[int] = []
    fake_os = _posix_stand_in(
        open=refuse_symlink,
        fstat=lambda fd: SimpleNamespace(st_mode=0o100600),
        fchmod=lambda fd, bits: fchmods.append(fd),
        close=lambda fd: None,
    )
    monkeypatch.setattr(api_token, "os", fake_os)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        api_token._restrict_mode(tmp_path / "api-token")

    assert opened and opened[0] & fake_os.O_NOFOLLOW
    assert fchmods == []
    assert caplog.text == ""


requires_posix_symlinks = pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes, and creating a symlink on Windows needs a privilege CI's runner lacks"
)


@requires_posix_symlinks
@pytest.mark.parametrize(("mode", "warned"), [(0o644, True), (0o600, False)])
def test_w4_4_a_symlinked_token_file_is_used_and_warns_when_its_target_is_readable_beyond_its_owner(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: int, warned: bool
):
    target = tmp_path / "secret-mount" / "..data" / "api-token"
    target.parent.mkdir(parents=True)
    target.write_text(STRONG_TOKEN + "\n", encoding="utf-8")
    target.chmod(mode)
    link = tmp_path / "api-token"
    link.symlink_to(target)
    settings = make_settings(link)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        ensure_api_credential(settings)

    assert settings.api_bearer_token == STRONG_TOKEN
    assert stat.S_IMODE(target.stat().st_mode) == mode  # the target is not ours to change
    assert ("readable beyond its owner" in caplog.text) is warned
    if warned:
        assert str(link) in caplog.text and "644" in caplog.text
    assert STRONG_TOKEN not in caplog.text


def test_any_other_open_error_is_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def refuse(target, flags):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(api_token, "os", _posix_stand_in(open=refuse))

    with pytest.raises(PermissionError):
        api_token._restrict_mode(tmp_path / "api-token")


@pytest.mark.parametrize(
    ("euid", "parent_uid", "chowned"),
    [(0, 1001, True), (0, 0, False), (1001, 1001, False), (None, 1001, False)],
)
def test_a_root_written_token_goes_to_the_owner_of_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, euid: int | None, parent_uid: int, chowned: bool
):
    path = tmp_path / "api-token"
    chowns: list[tuple[Path, int, int]] = []
    fake_os = SimpleNamespace(
        stat=lambda _path: SimpleNamespace(st_uid=parent_uid, st_gid=parent_uid + 1),
        chown=lambda target, uid, gid, *, follow_symlinks=True: chowns.append((target, uid, gid, follow_symlinks)),
    )
    if euid is not None:  # Windows has no geteuid
        fake_os.geteuid = lambda: euid
    monkeypatch.setattr(api_token, "os", fake_os)

    api_token._match_parent_owner(path)

    # Never follow a symlink swapped in for the file between the write and the chown.
    assert chowns == ([(path, parent_uid, parent_uid + 1, False)] if chowned else [])


def test_a_failed_chown_warns_and_startup_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("operation not permitted")

    fake_os = SimpleNamespace(
        geteuid=lambda: 0,
        stat=lambda _path: SimpleNamespace(st_uid=1001, st_gid=1001),
        chown=refuse,
    )
    monkeypatch.setattr(api_token, "os", fake_os)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        api_token._match_parent_owner(tmp_path / "api-token")

    assert "operation not permitted" in caplog.text


# --- Served requests ------------------------------------------------------------


def test_without_a_credential_or_the_escape_hatch_loopback_requests_are_refused(tmp_path: Path):
    client = served_client(make_settings(tmp_path / "api-token"))

    assert client.get("/sessions").status_code == 401
    assert client.get("/sessions", headers={"Host": "localhost"}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_the_generated_token_is_demanded_and_accepted(tmp_path: Path):
    settings = make_settings(tmp_path / "api-token")
    ensure_api_credential(settings)
    client = served_client(settings)

    assert client.get("/sessions").status_code == 401
    assert client.get("/sessions", headers={"Authorization": f"Bearer {settings.api_bearer_token}"}).status_code == 200
    # browser-node, or any container on the compose network, forging a loopback Host.
    assert client.get("/sessions", headers={"Host": "localhost"}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_the_controller_lifespan_generates_and_enforces_the_token(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    import app.main as main_module

    path = tmp_path / "api-token"
    if main_module.rate_limiter is not None:
        main_module.rate_limiter._events.clear()
    with ExitStack() as stack:
        for service, method_name in (
            (main_module.manager, "startup"),
            (main_module.manager, "shutdown"),
            (main_module.job_queue, "startup"),
            (main_module.job_queue, "shutdown"),
            (main_module.cron_service, "startup"),
            (main_module.cron_service, "shutdown"),
            (main_module.maintenance, "startup"),
            (main_module.maintenance, "shutdown"),
        ):
            stack.enter_context(patch.object(service, method_name, new=AsyncMock()))
        stack.enter_context(patch.object(main_module.manager, "list_sessions", new=AsyncMock(return_value=[])))
        # patch.object puts each value back afterwards, the generated token included.
        stack.enter_context(patch.object(main_module.settings, "api_bearer_token", None))
        stack.enter_context(patch.object(main_module.settings, "api_bearer_tokens", ""))
        stack.enter_context(patch.object(main_module.settings, "api_allow_tokenless_loopback", False))
        stack.enter_context(patch.object(main_module.settings, "api_token_file", str(path)))
        caplog.set_level(logging.INFO)
        client = stack.enter_context(TestClient(main_module.app))

        token = path.read_text(encoding="utf-8").strip()
        assert client.get("/sessions").status_code == 401
        assert client.get("/sessions", headers={"Host": "localhost"}).status_code == 401
        assert client.get("/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        for public in ("/healthz", "/dashboard", "/openapi.json"):
            assert client.get(public).status_code == 200, public
        # The page asks for the token; the server never hands it over, or a forged
        # Host: localhost would get it.
        assert token not in client.get("/dashboard", headers={"Host": "localhost"}).text

    assert token not in caplog.text
    assert main_module.settings.api_bearer_token is None
