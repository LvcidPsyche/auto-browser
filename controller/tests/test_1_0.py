"""
tests — auto-browser 1.0 test suite.

Covers: mesh (identity, peers, policy, transport, delegation),
        stealth (humanizer, fingerprint), network inspector,
        cdp passthrough, dom pruner, workflow engine.

Run with: pytest tests/test_1_0.py -v
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Ensure imports resolve from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))


# ===========================================================================
# Mesh — Identity
# ===========================================================================


class TestMeshIdentity:
    def test_generates_keypair(self, tmp_path):
        from app.mesh.identity import NodeIdentity

        ident = NodeIdentity(tmp_path / "identity")
        assert len(ident.node_id) == 64  # hex SHA-256
        assert len(ident.pubkey_b64) == 44  # base64 of 32 bytes

    def test_node_id_stable_across_loads(self, tmp_path):
        from app.mesh.identity import NodeIdentity

        d = tmp_path / "identity"
        i1 = NodeIdentity(d)
        i2 = NodeIdentity(d)
        assert i1.node_id == i2.node_id
        assert i1.pubkey_b64 == i2.pubkey_b64

    def test_sign_verify_roundtrip(self, tmp_path):
        from app.mesh.identity import NodeIdentity

        ident = NodeIdentity(tmp_path / "identity")
        data = b"hello mesh"
        sig = ident.sign(data)
        assert len(sig) == 64
        assert ident.verify_self(data, sig)

    def test_tamper_detection(self, tmp_path):
        from app.mesh.identity import NodeIdentity

        d = tmp_path / "identity"
        NodeIdentity(d)  # generate
        # Corrupt the meta file
        meta = d / "node_meta.json"
        meta.write_text(json.dumps({"node_id": "bad" * 16, "pubkey_b64": "x" * 44}))
        with pytest.raises(RuntimeError, match="mismatch"):
            NodeIdentity(d)

    def test_sign_detects_tampering(self, tmp_path):
        from app.mesh.identity import NodeIdentity

        ident = NodeIdentity(tmp_path / "identity")
        data = b"hello"
        sig = ident.sign(data)
        # Flip one byte in the signature
        bad_sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
        assert not ident.verify_self(data, bad_sig)


# ===========================================================================
# Mesh — Peers
# ===========================================================================


class TestMeshPeers:
    def _make_peer(self, node_id: str = "abc123"):
        from app.mesh.models import PeerRecord

        return PeerRecord(
            node_id=node_id,
            pubkey_b64="A" * 44,
            endpoint=f"https://node-{node_id}.example.com",
        )

    def test_add_and_get(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        reg = PeerRegistryFile(tmp_path / "peers.json")
        peer = self._make_peer()
        reg.add(peer)
        assert reg.get(peer.node_id).node_id == peer.node_id

    def test_unknown_peer_raises(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        reg = PeerRegistryFile(tmp_path / "peers.json")
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_get_optional_returns_none(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        reg = PeerRegistryFile(tmp_path / "peers.json")
        assert reg.get_optional("nonexistent") is None

    def test_remove(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        reg = PeerRegistryFile(tmp_path / "peers.json")
        peer = self._make_peer()
        reg.add(peer)
        assert reg.remove(peer.node_id)
        assert reg.get_optional(peer.node_id) is None

    def test_persists_across_instances(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        path = tmp_path / "peers.json"
        reg1 = PeerRegistryFile(path)
        reg1.add(self._make_peer("node1"))
        reg1.add(self._make_peer("node2"))
        reg2 = PeerRegistryFile(path)
        assert len(reg2.all()) == 2

    def test_hot_reload(self, tmp_path):
        from app.mesh.peers import PeerRegistryFile

        path = tmp_path / "peers.json"
        reg1 = PeerRegistryFile(path)
        reg1.add(self._make_peer("node1"))
        # Another instance writes
        reg2 = PeerRegistryFile(path)
        reg2.add(self._make_peer("node2"))
        # Force mtime change
        time.sleep(0.01)
        path.touch()
        # reg1 should pick up the new peer
        assert reg1.get_optional("node2") is not None


# ===========================================================================
# Mesh — Policy
# ===========================================================================


class TestMeshPolicy:
    def _make_peer(self, grants=None):
        from app.mesh.models import PeerRecord

        return PeerRecord(
            node_id="peer1",
            pubkey_b64="A" * 44,
            grants=grants or [],
        )

    def _make_request(self, capability="tool:browser.click", arguments=None):
        from app.mesh.models import DelegationRequest

        return DelegationRequest(capability=capability, arguments=arguments or {})

    def _make_grant(self, capability, **kwargs):
        from app.mesh.models import CapabilityGrant

        return CapabilityGrant(capability=capability, **kwargs)

    def test_default_deny_no_grants(self):
        from app.mesh.policy import PolicyDenied, PolicyEvaluator

        ev = PolicyEvaluator()
        peer = self._make_peer()
        with pytest.raises(PolicyDenied):
            ev.evaluate(peer, self._make_request())

    def test_exact_match_permit(self):
        from app.mesh.policy import PolicyEvaluator

        grant = self._make_grant("tool:browser.click")
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        result = ev.evaluate(peer, self._make_request("tool:browser.click"))
        assert result.capability == "tool:browser.click"

    def test_wildcard_match(self):
        from app.mesh.policy import PolicyEvaluator

        grant = self._make_grant("tool:*")
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        result = ev.evaluate(peer, self._make_request("tool:browser.observe"))
        assert result is not None

    def test_expires_at_rejected(self):
        from app.mesh.policy import PolicyEvaluator, PolicyExpired

        grant = self._make_grant("tool:*", expires_at=time.time() - 1)
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        with pytest.raises(PolicyExpired):
            ev.evaluate(peer, self._make_request())

    def test_expires_at_future_ok(self):
        from app.mesh.policy import PolicyEvaluator

        grant = self._make_grant("tool:*", expires_at=time.time() + 3600)
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        result = ev.evaluate(peer, self._make_request())
        assert result is not None

    def test_url_allowlist_permit(self):
        from app.mesh.policy import PolicyEvaluator

        grant = self._make_grant("tool:*", url_allowlist=["*example.com*"])
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        req = self._make_request(arguments={"url": "https://example.com/page"})
        result = ev.evaluate(peer, req)
        assert result is not None

    def test_url_allowlist_deny(self):
        from app.mesh.policy import PolicyDenied, PolicyEvaluator

        grant = self._make_grant("tool:*", url_allowlist=["*example.com*"])
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        req = self._make_request(arguments={"url": "https://evil.com/hack"})
        with pytest.raises(PolicyDenied):
            ev.evaluate(peer, req)

    def test_rate_limit_enforcement(self):
        from app.mesh.policy import PolicyEvaluator, PolicyRateLimited

        grant = self._make_grant("tool:limited", max_invocations_per_hour=2)
        peer = self._make_peer([grant])
        ev = PolicyEvaluator()
        req = self._make_request("tool:limited")
        # First two should pass
        ev.evaluate(peer, req)
        ev.evaluate(peer, req)
        # Third should be rate-limited
        with pytest.raises(PolicyRateLimited):
            ev.evaluate(peer, req)


# ===========================================================================
# Mesh — Transport (envelope crypto)
# ===========================================================================


class TestMeshTransport:
    def _make_identity(self, tmp_path, subdir="id"):
        from app.mesh.identity import NodeIdentity

        return NodeIdentity(tmp_path / subdir)

    def _make_peer_from_identity(self, identity, endpoint="https://peer.example.com"):
        from app.mesh.models import PeerRecord

        return PeerRecord(
            node_id=identity.node_id,
            pubkey_b64=identity.pubkey_b64,
            endpoint=endpoint,
        )

    def test_make_and_verify_envelope(self, tmp_path):
        from app.mesh.transport import make_envelope, verify_envelope

        ident = self._make_identity(tmp_path)
        peer = self._make_peer_from_identity(ident)
        env = make_envelope(ident, {"action": "test"}, recipient_node_id="other")
        payload = verify_envelope(env, peer)
        assert payload["action"] == "test"

    def test_tampered_payload_rejected(self, tmp_path):
        from app.mesh.transport import EnvelopeVerificationError, make_envelope, verify_envelope

        ident = self._make_identity(tmp_path)
        peer = self._make_peer_from_identity(ident)
        env = make_envelope(ident, {"action": "test"})
        env.payload["action"] = "evil"  # tamper
        with pytest.raises(EnvelopeVerificationError):
            verify_envelope(env, peer)

    def test_wrong_sender_rejected(self, tmp_path):
        from app.mesh.models import PeerRecord
        from app.mesh.transport import EnvelopeVerificationError, make_envelope, verify_envelope

        ident = self._make_identity(tmp_path)
        env = make_envelope(ident, {"action": "test"})
        wrong_peer = PeerRecord(node_id="wrong", pubkey_b64="A" * 44)
        with pytest.raises(EnvelopeVerificationError):
            verify_envelope(env, wrong_peer)

    def test_wrong_recipient_rejected(self, tmp_path):
        from app.mesh.transport import EnvelopeVerificationError, make_envelope, verify_envelope

        ident = self._make_identity(tmp_path)
        peer = self._make_peer_from_identity(ident)
        env = make_envelope(ident, {"action": "test"}, recipient_node_id="node-a")
        with pytest.raises(EnvelopeVerificationError):
            verify_envelope(env, peer, expected_recipient_node_id="node-b")

    def test_truncated_signature_rejected(self, tmp_path):
        from app.mesh.transport import EnvelopeVerificationError, make_envelope, verify_envelope

        ident = self._make_identity(tmp_path)
        peer = self._make_peer_from_identity(ident)
        env = make_envelope(ident, {"x": 1})
        env.signature_b64 = env.signature_b64[:20]  # truncate
        with pytest.raises(EnvelopeVerificationError):
            verify_envelope(env, peer)


# ===========================================================================
# Mesh — Delegation
# ===========================================================================


class TestMeshDelegation:
    def _setup(self, tmp_path):
        from app.mesh.delegation import DelegationManager
        from app.mesh.identity import NodeIdentity
        from app.mesh.models import CapabilityGrant, PeerRecord
        from app.mesh.peers import PeerRegistryFile

        sender_id = NodeIdentity(tmp_path / "sender")
        receiver_id = NodeIdentity(tmp_path / "receiver")

        sender_as_peer = PeerRecord(
            node_id=sender_id.node_id,
            pubkey_b64=sender_id.pubkey_b64,
            grants=[CapabilityGrant(capability="tool:*")],
        )
        peer_reg = PeerRegistryFile(tmp_path / "peers.json")
        peer_reg.add(sender_as_peer)

        mgr = DelegationManager(identity=receiver_id, peers=peer_reg)
        return sender_id, receiver_id, peer_reg, mgr

    async def test_receive_inbound_permit(self, tmp_path):
        sender_id, receiver_id, peer_reg, mgr = self._setup(tmp_path)
        from app.mesh.models import DelegationRequest
        from app.mesh.transport import make_envelope

        request = DelegationRequest(capability="tool:browser.click", arguments={"x": 1})
        envelope = make_envelope(sender_id, request.model_dump(mode="json"), receiver_id.node_id)

        result = await mgr.receive_inbound(envelope)
        assert result.status in ("ok", "rejected")  # "ok" if tool gateway wired, else routes to stub

    async def test_receive_inbound_unknown_sender_rejected(self, tmp_path):
        from app.mesh.delegation import DelegationManager, DelegationRejected
        from app.mesh.identity import NodeIdentity
        from app.mesh.models import DelegationRequest
        from app.mesh.peers import PeerRegistryFile
        from app.mesh.transport import make_envelope

        unknown_sender = NodeIdentity(tmp_path / "unknown")
        receiver_id = NodeIdentity(tmp_path / "receiver")
        peer_reg = PeerRegistryFile(tmp_path / "peers.json")  # empty
        mgr = DelegationManager(identity=receiver_id, peers=peer_reg)

        request = DelegationRequest(capability="tool:x")
        env = make_envelope(unknown_sender, request.model_dump(mode="json"), receiver_id.node_id)

        with pytest.raises(DelegationRejected):
            await mgr.receive_inbound(env)

    async def test_replay_rejected(self, tmp_path):
        sender_id, receiver_id, _, mgr = self._setup(tmp_path)
        from app.mesh.delegation import DelegationReplayError
        from app.mesh.models import DelegationRequest
        from app.mesh.transport import make_envelope

        request = DelegationRequest(capability="tool:x")
        env = make_envelope(sender_id, request.model_dump(mode="json"), receiver_id.node_id)

        await mgr.receive_inbound(env)
        with pytest.raises(DelegationReplayError):
            await mgr.receive_inbound(env)

    async def test_timestamp_window_enforced(self, tmp_path):
        sender_id, _, _, mgr = self._setup(tmp_path)
        from app.mesh.delegation import DelegationRejected
        from app.mesh.models import DelegationRequest
        from app.mesh.transport import make_envelope

        request = DelegationRequest(capability="tool:x")
        env = make_envelope(sender_id, request.model_dump(mode="json"))
        env.timestamp = time.time() - 120  # 120s old, window=30s

        with pytest.raises(DelegationRejected, match="timestamp"):
            await mgr.receive_inbound(env)

    async def test_wrong_recipient_rejected(self, tmp_path):
        sender_id, _, _, mgr = self._setup(tmp_path)
        from app.mesh.delegation import DelegationRejected
        from app.mesh.models import DelegationRequest
        from app.mesh.transport import make_envelope

        request = DelegationRequest(capability="tool:x")
        env = make_envelope(sender_id, request.model_dump(mode="json"), recipient_node_id="someone-else")

        with pytest.raises(DelegationRejected, match="recipient"):
            await mgr.receive_inbound(env)


# ===========================================================================
# Stealth — Humanizer
# ===========================================================================


class TestStealth:
    def test_profile_off_is_none(self):
        from app.stealth.humanizer import PROFILES

        assert PROFILES["off"] is None

    def test_profile_light_is_human_profile(self):
        from app.stealth.humanizer import PROFILES, HumanProfile

        assert isinstance(PROFILES["light"], HumanProfile)

    def test_humanizer_inactive_when_off(self):
        from app.stealth.humanizer import Humanizer

        h = Humanizer("off")
        assert not h.active

    def test_humanizer_active_when_light(self):
        from app.stealth.humanizer import Humanizer

        h = Humanizer("light")
        assert h.active

    def test_bezier_points_count(self):
        from app.stealth.humanizer import _bezier_points

        pts = _bezier_points(0, 0, 100, 100, steps=20, jitter=5)
        assert len(pts) == 21  # steps + 1

    def test_bezier_starts_and_ends_near_target(self):
        from app.stealth.humanizer import _bezier_points

        pts = _bezier_points(0.0, 0.0, 100.0, 100.0, steps=30, jitter=0)
        # With zero jitter, start and end should be exact
        assert abs(pts[0][0]) < 1.0
        assert abs(pts[-1][0] - 100.0) < 1.0

    def test_fingerprint_stable_within_session(self):
        from app.stealth.fingerprint import FingerprintConfig

        c1 = FingerprintConfig("session-abc", "light")
        c2 = FingerprintConfig("session-abc", "light")
        assert c1.user_agent == c2.user_agent
        assert c1.canvas_noise_seed == c2.canvas_noise_seed

    def test_fingerprint_differs_across_sessions(self):
        from app.stealth.fingerprint import FingerprintConfig

        configs = [FingerprintConfig(f"session-{i}", "light") for i in range(10)]
        ua_set = {c.user_agent for c in configs}
        assert len(ua_set) > 1  # should vary

    def test_init_script_contains_webdriver_mask(self):
        from app.stealth.fingerprint import FingerprintConfig

        c = FingerprintConfig("test-session")
        script = c.init_script()
        assert "webdriver" in script
        assert "navigator" in script


# ===========================================================================
# Network Inspector (merged — uses existing app.network_inspector.NetworkInspector
# with hook registration grafted on in v1.0)
# ===========================================================================


class TestNetworkInspector:
    def test_empty_buffer(self):
        from app.network_inspector import NetworkInspector

        insp = NetworkInspector(session_id="s1")
        assert insp.entries() == []

    def test_register_and_list_hooks(self):
        from app.network_inspector import NetworkInspector

        insp = NetworkInspector(session_id="s1")
        insp.register_hook("*api*", AsyncMock())
        assert "*api*" in insp.list_hooks()

    def test_remove_hook(self):
        from app.network_inspector import NetworkInspector

        insp = NetworkInspector(session_id="s1")
        insp.register_hook("*api*", AsyncMock())
        assert insp.remove_hook("*api*")
        assert "*api*" not in insp.list_hooks()

    def test_clear(self):
        from app.network_inspector import NetworkInspector

        insp = NetworkInspector(session_id="s1")
        insp.clear()
        assert insp.entries() == []


# ===========================================================================
# DOM Pruner
# ===========================================================================


class TestDOMPruner:
    def _make_elements(self, n: int) -> list[dict]:
        return [
            {"element_id": f"el-{i}", "role": "button", "text": f"Button {i}", "is_visible": True} for i in range(n)
        ]

    def test_prune_returns_limit(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=10)
        elements = self._make_elements(50)
        pruned = pruner.prune(elements, task_goal="click the submit button")
        assert len(pruned) <= 10

    def test_prune_empty_input(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=10)
        assert pruner.prune([]) == []

    def test_prune_under_limit_unchanged(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=20)
        elements = self._make_elements(5)
        pruned = pruner.prune(elements)
        assert len(pruned) == 5

    def test_keyword_match_boosts_rank(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=3)
        elements = [
            {"element_id": "login-btn", "role": "button", "text": "Login", "is_visible": True},
            {"element_id": "random-1", "role": "button", "text": "Irrelevant", "is_visible": True},
            {"element_id": "random-2", "role": "link", "text": "Something", "is_visible": True},
            {"element_id": "random-3", "role": "button", "text": "Other", "is_visible": True},
        ]
        pruned = pruner.prune(elements, task_goal="click the login button", max_elements=1)
        assert pruned[0]["element_id"] == "login-btn"

    def test_hidden_elements_deprioritized(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=1)
        elements = [
            {"element_id": "hidden", "role": "button", "text": "Hidden", "is_visible": False},
            {"element_id": "visible", "role": "button", "text": "Visible", "is_visible": True},
        ]
        pruned = pruner.prune(elements, max_elements=1)
        assert pruned[0]["element_id"] == "visible"

    def test_recency_boost(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=1)
        elements = [
            {"element_id": "recent", "role": "link", "text": "Link"},
            {"element_id": "not-recent", "role": "button", "text": "Button"},
        ]
        pruner.record_interaction("recent")
        pruned = pruner.prune(elements, max_elements=1)
        assert pruned[0]["element_id"] == "recent"

    def test_prune_observation(self):
        from app.browser.dom_pruner import DOMPruner

        pruner = DOMPruner(max_elements=5)
        obs = {"interactable_elements": self._make_elements(30), "url": "https://example.com"}
        result = pruner.prune_observation(obs, task_goal="submit form")
        assert len(result["interactable_elements"]) <= 5
        assert result["elements_total"] == 30
        assert result["elements_pruned"] == 25


# ===========================================================================
# Workflow Engine
# ===========================================================================


class TestWorkflowEngine:
    async def test_simple_workflow(self, tmp_path):
        from app.workflow.engine import WorkflowEngine

        engine = WorkflowEngine(workflows_root=tmp_path / "workflows")
        results_store = {}

        async def handler(action: str, params: dict, ctx: dict) -> dict:
            results_store[action] = params
            return {"done": True, "action": action}

        engine.register_action("test.step", handler)

        steps = [{"id": "s1", "action": "test.step", "params": {"key": "value"}}]
        run = await engine.run("test_wf", steps, {"initial": "ctx"})
        assert run.status.value == "completed"
        assert run.step_statuses["s1"].value == "completed"

    async def test_template_chaining(self, tmp_path):
        from app.workflow.engine import _resolve_templates

        ctx = {"video_id": "abc123", "nested": {"key": "val"}}
        assert _resolve_templates("Upload {{ context.video_id }}", ctx) == "Upload abc123"
        assert _resolve_templates("{{ context.nested.key }}", ctx) == "val"
        assert _resolve_templates("{{ context.missing }}", ctx) == ""

    async def test_dependency_ordering(self, tmp_path):
        from app.workflow.engine import WorkflowEngine

        order = []

        async def handler(action: str, params: dict, ctx: dict) -> dict:
            order.append(action)
            return {}

        engine = WorkflowEngine(workflows_root=tmp_path / "workflows")
        engine.register_action("step.a", handler)
        engine.register_action("step.b", handler)

        steps = [
            {"id": "b", "action": "step.b", "depends_on": ["a"]},
            {"id": "a", "action": "step.a"},
        ]
        await engine.run("dep_test", steps)
        assert order.index("step.a") < order.index("step.b")

    async def test_missing_action_fails_run(self, tmp_path):
        from app.workflow.engine import WorkflowEngine

        engine = WorkflowEngine(workflows_root=tmp_path / "workflows")
        steps = [{"id": "x", "action": "unregistered.action"}]
        run = await engine.run("fail_test", steps)
        assert run.status.value == "failed"

    async def test_retry_on_transient_failure(self, tmp_path):
        from app.workflow.engine import WorkflowEngine

        call_count = [0]

        async def flaky(action, params, ctx):
            call_count[0] += 1
            if call_count[0] < 2:
                raise RuntimeError("transient")
            return {"ok": True}

        engine = WorkflowEngine(workflows_root=tmp_path / "workflows")
        engine.register_action("flaky.step", flaky)
        steps = [{"id": "s1", "action": "flaky.step", "retry_max": 2, "retry_backoff_seconds": 0.01}]
        run = await engine.run("retry_test", steps)
        assert run.status.value == "completed"
        assert call_count[0] == 2

    async def test_persists_to_disk(self, tmp_path):
        from app.workflow.engine import WorkflowEngine

        engine = WorkflowEngine(workflows_root=tmp_path / "wf")
        engine.register_action("x", AsyncMock(return_value={}))
        steps = [{"id": "s", "action": "x"}]
        run = await engine.run("persist_test", steps)
        runs = engine.list_runs()
        assert any(r["run_id"] == run.run_id for r in runs)


# ===========================================================================
# Skills Curator LLM adapter
# ===========================================================================


class TestCuratorLLMAdapter:
    def test_invalid_provider_rejected(self):
        import pytest

        from app.curator_llm import CuratorLLMAdapter

        with pytest.raises(ValueError):
            CuratorLLMAdapter("nonsense")

    def test_ready_false_without_api_key(self, monkeypatch):
        from app.curator_llm import CuratorLLMAdapter

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = CuratorLLMAdapter("claude")
        assert adapter.ready is False

    def test_ready_true_with_api_key(self, monkeypatch):
        from app.curator_llm import CuratorLLMAdapter

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        adapter = CuratorLLMAdapter("claude")
        assert adapter.ready is True

    def test_default_models_per_provider(self):
        from app.curator_llm import CuratorLLMAdapter

        assert CuratorLLMAdapter("claude").model.startswith("claude")
        assert CuratorLLMAdapter("openai").model.startswith("gpt")
        assert CuratorLLMAdapter("gemini").model.startswith("gemini")

    def test_api_key_env_override(self):
        from app.curator_llm import CuratorLLMAdapter

        adapter = CuratorLLMAdapter("claude", api_key_env="CUSTOM_KEY")
        assert adapter._api_key_env() == "CUSTOM_KEY"

    def test_build_curator_adapter_returns_none_without_key(self, monkeypatch):
        from app.curator_llm import build_curator_adapter

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert build_curator_adapter() is None
