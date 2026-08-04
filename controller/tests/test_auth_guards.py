"""Guards that were correct in code but had zero test coverage.

None of these tests found a bug — that is the point. Each one pins a security
guard whose failure mode is *permitting* something, so a refactor that weakened
it would have shipped green. Measured coverage showed every rejection branch in
`require_approved` and every rejection branch in `SessionShareService.validate`
never executing under the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.approvals import ApprovalStore
from app.models import BrowserActionDecision
from app.session_share import SessionShareManager


def _action(element_id: str = "el-1") -> BrowserActionDecision:
    return BrowserActionDecision(action="click", element_id=element_id, reason="test")


async def _pending(store: ApprovalStore, *, session_id: str = "s1", kind: str = "upload", element_id: str = "el-1"):
    return await store.create_or_reuse_pending(
        session_id=session_id,
        kind=kind,
        reason="test",
        action=_action(element_id),
    )


@pytest.fixture
async def store(tmp_path: Path) -> ApprovalStore:
    s = ApprovalStore(tmp_path)
    await s.startup()
    return s


# ── Confused-deputy guards on require_approved ─────────────────────────────


@pytest.mark.asyncio
async def test_approval_for_another_session_is_rejected(store: ApprovalStore) -> None:
    approval = await _pending(store, session_id="s1")
    await store.approve(approval.id)

    with pytest.raises(PermissionError, match="does not belong to session"):
        await store.require_approved(
            approval_id=approval.id,
            session_id="s2",
            kind="upload",
            action=_action(),
        )


@pytest.mark.asyncio
async def test_approval_of_another_kind_is_rejected(store: ApprovalStore) -> None:
    approval = await _pending(store, kind="upload")
    await store.approve(approval.id)

    with pytest.raises(PermissionError, match="does not cover"):
        await store.require_approved(
            approval_id=approval.id,
            session_id="s1",
            kind="navigation",
            action=_action(),
        )


@pytest.mark.asyncio
async def test_mutating_the_action_after_approval_is_rejected(store: ApprovalStore) -> None:
    """Approve a click on element A, then try to execute a click on element B.

    This is the realistic bypass: get a human to approve something benign, then
    swap the payload before execution.
    """
    approval = await _pending(store, element_id="el-benign")
    await store.approve(approval.id)

    with pytest.raises(PermissionError, match="does not match the requested action"):
        await store.require_approved(
            approval_id=approval.id,
            session_id="s1",
            kind="upload",
            action=_action("el-malicious"),
        )


@pytest.mark.asyncio
async def test_unapproved_and_rejected_approvals_cannot_execute(store: ApprovalStore) -> None:
    pending = await _pending(store)
    with pytest.raises(PermissionError, match="is not approved"):
        await store.require_approved(approval_id=pending.id, session_id="s1", kind="upload", action=_action())

    rejected = await _pending(store, element_id="el-2")
    await store.reject(rejected.id)
    with pytest.raises(PermissionError, match="is not approved"):
        await store.require_approved(approval_id=rejected.id, session_id="s1", kind="upload", action=_action("el-2"))
    with pytest.raises(PermissionError):
        await store.mark_executed(rejected.id)


@pytest.mark.asyncio
async def test_an_approval_cannot_be_executed_twice(store: ApprovalStore) -> None:
    approval = await _pending(store)
    await store.approve(approval.id)
    await store.mark_executed(approval.id)

    with pytest.raises(PermissionError):
        await store.mark_executed(approval.id)


@pytest.mark.asyncio
async def test_matching_approval_is_accepted(store: ApprovalStore) -> None:
    """Guard the guards: the happy path must still work."""
    approval = await _pending(store)
    await store.approve(approval.id)

    accepted = await store.require_approved(approval_id=approval.id, session_id="s1", kind="upload", action=_action())
    assert accepted.id == approval.id


# ── Share-token unforgeability and expiry ──────────────────────────────────


def _svc(secret: str = "secret-one", ttl_minutes: int = 60) -> SessionShareManager:
    return SessionShareManager(secret=secret, ttl_minutes=ttl_minutes)


def test_share_token_roundtrips() -> None:
    svc = _svc()
    token = svc.create_token("sess-1")["token"]
    assert svc.validate_token(token)["session_id"] == "sess-1"


def test_tampered_share_token_payload_is_rejected() -> None:
    """Flip a payload byte, keep the signature — the HMAC must catch it."""
    svc = _svc()
    token = svc.create_token("sess-1")["token"]
    payload_b64, _, signature = token.partition(".")

    mutated = list(payload_b64)
    index = len(mutated) // 2
    mutated[index] = "A" if mutated[index] != "A" else "B"
    forged = f"{''.join(mutated)}.{signature}"

    with pytest.raises(ValueError):
        svc.validate_token(forged)


def test_share_token_signed_with_another_secret_is_rejected() -> None:
    """A token minted by a different deployment must not validate here."""
    token = _svc(secret="secret-two").create_token("sess-1")["token"]

    with pytest.raises(ValueError, match="signature"):
        _svc(secret="secret-one").validate_token(token)


def test_expired_share_token_is_rejected(monkeypatch) -> None:
    """Expiry is enforced. Time is patched forward — never slept."""
    import app.session_share as share_module

    svc = _svc(ttl_minutes=1)
    token = svc.create_token("sess-1")["token"]
    assert svc.validate_token(token)["session_id"] == "sess-1"

    real_time = share_module.time.time()
    monkeypatch.setattr(share_module.time, "time", lambda: real_time + 3600)

    with pytest.raises(ValueError):
        svc.validate_token(token)


def test_malformed_share_tokens_are_rejected() -> None:
    svc = _svc()
    for bad in ("", "no-dot", "not-base64.sig", "."):
        with pytest.raises(ValueError):
            svc.validate_token(bad)


# ── Auth state must fail closed ────────────────────────────────────────────


def _auth_manager(tmp_path: Path, *, require_encryption: bool, with_key: bool = True):
    from cryptography.fernet import Fernet

    from app.auth_state import AuthStateManager

    key = Fernet.generate_key().decode() if with_key else None
    return AuthStateManager(encryption_key=key, require_encryption=require_encryption, max_age_hours=0), key


_STATE = {"cookies": [{"name": "session", "value": "SUPER-SECRET"}], "origins": []}


def _write_plaintext(path: Path) -> Path:
    import json

    path.write_text(json.dumps(_STATE), encoding="utf-8")
    return path


def _write_envelope(path: Path, key: str) -> Path:
    import json

    from cryptography.fernet import Fernet

    path.write_text(
        json.dumps(
            {
                "version": 1,
                "format": "fernet-json",
                "ciphertext": Fernet(key.encode()).encrypt(json.dumps(_STATE).encode()).decode(),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_require_encryption_refuses_plaintext_auth_state(tmp_path: Path) -> None:
    """REQUIRE_AUTH_STATE_ENCRYPTION was only enforced at construction.

    It checked "is a key configured?" and never checked the file being read, so
    a plaintext state file loaded happily and its cookies went straight into a
    live browser context. The setting promised encrypted-at-rest and silently
    did not deliver it.
    """
    manager, _ = _auth_manager(tmp_path, require_encryption=True)
    plain = _write_plaintext(tmp_path / "state.json")

    with pytest.raises(PermissionError, match="not encrypted"):
        manager.prepare_for_context(plain)


def test_plaintext_is_allowed_when_encryption_is_not_required(tmp_path: Path) -> None:
    """Guard the guard: the refusal is conditional on the setting."""
    manager, _ = _auth_manager(tmp_path, require_encryption=False, with_key=False)
    plain = _write_plaintext(tmp_path / "state.json")

    prepared = manager.prepare_for_context(plain)
    assert prepared.path == plain


def test_encrypted_content_is_detected_without_the_enc_suffix(tmp_path: Path) -> None:
    """Encryption status came from the filename, not the content.

    An encrypted envelope named without `.enc` was classified as plaintext and
    handed to Playwright verbatim — so no cookies loaded and the agent carried
    on believing the auth profile had been applied.
    """
    import json

    manager, key = _auth_manager(tmp_path, require_encryption=False)
    mislabelled = _write_envelope(tmp_path / "state-no-suffix.json", key)

    assert manager.inspect(mislabelled)["encrypted"] is True

    prepared = manager.prepare_for_context(mislabelled)
    body = json.loads(Path(prepared.path).read_text(encoding="utf-8"))
    assert "cookies" in body, "the envelope must be decrypted, not passed through"
    assert body["cookies"][0]["value"] == "SUPER-SECRET"
    prepared.cleanup()


def test_plaintext_named_enc_is_detected_as_plaintext(tmp_path: Path) -> None:
    """The mislabelling is caught in both directions."""
    manager, _ = _auth_manager(tmp_path, require_encryption=False)
    mislabelled = _write_plaintext(tmp_path / "state.json.enc")

    assert manager.inspect(mislabelled)["encrypted"] is False
    assert manager.prepare_for_context(mislabelled).path == mislabelled


def test_encrypted_state_without_a_key_is_refused(tmp_path: Path) -> None:
    manager_with_key, key = _auth_manager(tmp_path, require_encryption=False)
    encrypted = _write_envelope(tmp_path / "state.json.enc", key)

    keyless, _ = _auth_manager(tmp_path, require_encryption=False, with_key=False)
    with pytest.raises(RuntimeError, match="AUTH_STATE_ENCRYPTION_KEY"):
        keyless.prepare_for_context(encrypted)


def test_require_encryption_without_a_key_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="AUTH_STATE_ENCRYPTION_KEY"):
        _auth_manager(tmp_path, require_encryption=True, with_key=False)


def test_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    """A corrupted envelope must raise, not silently yield empty auth state."""
    import json

    from cryptography.fernet import InvalidToken

    manager, key = _auth_manager(tmp_path, require_encryption=False)
    path = _write_envelope(tmp_path / "state.json.enc", key)

    body = json.loads(path.read_text(encoding="utf-8"))
    body["ciphertext"] = body["ciphertext"][:-6] + "AAAAAA"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(InvalidToken):
        manager.prepare_for_context(path)


# ── Credential movement leaves a record ────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_profile_export_and_import_are_audited(tmp_path: Path) -> None:
    """Exporting a profile ships every cookie for a logged-in account.

    `save_storage_state` — which merely *writes* that material — is wrapped in
    witness policy, an audit event and a receipt. Export, which lets it leave
    the box, and import, which installs credentials the controller will replay
    into a real browser, had no audit record whatsoever: nothing in
    /audit/events showed it had happened.
    """
    from types import SimpleNamespace

    from app.browser.services import BrowserAuthProfileService

    class _Audit:
        def __init__(self) -> None:
            self.events: list[dict] = []

        async def append(self, **kwargs) -> None:
            self.events.append(kwargs)

    audit = _Audit()
    auth_root = tmp_path / "auth"
    service = BrowserAuthProfileService(
        SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(auth_root), auth_state_encryption_key=None),
            audit=audit,
        )
    )

    profile_dir = service.dir("demo", create=True)
    (profile_dir / "state.json").write_text('{"cookies": []}', encoding="utf-8")

    exported = await service.export("demo")
    assert [e["event_type"] for e in audit.events] == ["auth_profile_exported"]
    assert audit.events[0]["details"]["profile_name"] == "demo"

    await service.import_profile(exported["archive_name"], overwrite=True)
    assert [e["event_type"] for e in audit.events] == [
        "auth_profile_exported",
        "auth_profile_imported",
    ]


@pytest.mark.asyncio
async def test_auth_profile_operations_emit_signed_witness_receipts(tmp_path: Path) -> None:
    """Credential movement now leaves a signed receipt, not just an audit line.

    Receipts used to be session-scoped only, which is why export and import —
    the two operations that move credentials on and off the box — had none.
    They record under a dedicated `auth-profiles` scope with their own chain.
    """
    from types import SimpleNamespace

    from app.browser.services import BrowserAuthProfileService
    from app.witness import WitnessRecorder
    from app.witness_signing import WitnessSigner

    class _Audit:
        async def append(self, **kwargs) -> None:
            return None

    recorder = WitnessRecorder(tmp_path / "witness", signer=WitnessSigner(tmp_path / "keys"))
    await recorder.startup()

    auth_root = tmp_path / "auth"
    service = BrowserAuthProfileService(
        SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(auth_root), auth_state_encryption_key=None),
            audit=_Audit(),
            witness=recorder,
        )
    )

    profile_dir = service.dir("demo", create=True)
    (profile_dir / "state.json").write_text('{"cookies": []}', encoding="utf-8")

    exported = await service.export("demo")
    await service.import_profile(exported["archive_name"], overwrite=True)

    receipts = await recorder.list("auth-profiles", limit=10)
    actions = sorted(r.action for r in receipts)
    assert actions == ["export_auth_profile", "import_auth_profile"]
    assert all(r.chain_signature for r in receipts), "profile receipts must be signed like session receipts"

    signatures = await recorder.verify_signatures("auth-profiles")
    assert signatures["valid"] is True
    assert signatures["signed"] == 2


@pytest.mark.asyncio
async def test_a_witness_outage_does_not_break_the_export(tmp_path: Path) -> None:
    """Recording evidence must never be able to fail the operation it records."""
    from types import SimpleNamespace

    from app.browser.services import BrowserAuthProfileService

    class _Audit:
        async def append(self, **kwargs) -> None:
            return None

    class _BrokenWitness:
        async def record(self, *args, **kwargs):
            raise RuntimeError("witness store unavailable")

    auth_root = tmp_path / "auth"
    service = BrowserAuthProfileService(
        SimpleNamespace(
            settings=SimpleNamespace(auth_root=str(auth_root), auth_state_encryption_key=None),
            audit=_Audit(),
            witness=_BrokenWitness(),
        )
    )
    profile_dir = service.dir("demo", create=True)
    (profile_dir / "state.json").write_text('{"cookies": []}', encoding="utf-8")

    exported = await service.export("demo")
    assert exported["profile_name"] == "demo"
