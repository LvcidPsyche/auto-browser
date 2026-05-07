"""
startup.extensions — Initialize all 1.0 subsystems at app startup.

Call register_extensions(app) from main.py after app creation.
All clients are initialized from environment variables.
Missing credentials = subsystem disabled with a warning (never crash on startup).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def register_extensions(app) -> None:
    """
    Wire all auto-browser 1.0 subsystems into app.state.

    Subsystems initialized:
        mesh_identity       — NodeIdentity
        peer_registry       — PeerRegistryFile
        delegation_manager  — DelegationManager
        network_inspectors  — dict[session_id, NetworkInspector]
        cdp_sessions        — dict[session_id, CDPPassthrough]
        workflow_engine     — WorkflowEngine (with all action handlers)
    """
    _init_mesh(app)
    _init_network_stores(app)
    _init_workflow_engine(app)
    _disable_extracted_social_state(app)
    _init_curator(app)
    _register_session_hooks(app)
    logger.info("startup.extensions: all 1.0 subsystems registered")


# ---------------------------------------------------------------------------
# Skills Curator adapter
# ---------------------------------------------------------------------------

def _init_curator(app) -> None:
    """Initialize the Skills Curator LLM adapter. None when no API key is set."""
    try:
        from app.curator_llm import build_curator_adapter
        adapter = build_curator_adapter()
        app.state.curator_adapter = adapter
        if adapter is not None and adapter.ready:
            logger.info("startup.curator: %s adapter ready (model=%s)", adapter.provider, adapter.model)
        else:
            logger.info("startup.curator: no API key — degraded mode (raw-skill passthrough only)")
    except Exception as exc:
        logger.warning("startup.curator: adapter init failed — %s", exc)
        app.state.curator_adapter = None


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------

def _init_mesh(app) -> None:
    mesh_enabled = os.environ.get("MESH_ENABLED", "false").lower() == "true"
    if not mesh_enabled:
        app.state.mesh_identity = None
        app.state.peer_registry = None
        app.state.delegation_manager = None
        logger.info("startup.mesh: disabled (set MESH_ENABLED=true to enable)")
        return

    try:
        from app.mesh.delegation import DelegationManager
        from app.mesh.identity import NodeIdentity
        from app.mesh.peers import PeerRegistryFile

        identity_dir = Path(os.environ.get("MESH_IDENTITY_DIR", "/data/mesh/identity"))
        peers_path = Path(os.environ.get("MESH_PEERS_PATH", "/data/mesh/peers.json"))
        timestamp_window = float(os.environ.get("MESH_TIMESTAMP_WINDOW", "30"))

        identity = NodeIdentity(identity_dir)
        peers = PeerRegistryFile(peers_path)
        mgr = DelegationManager(
            identity=identity,
            peers=peers,
            timestamp_window=timestamp_window,
            tool_gateway=_build_mesh_tool_gateway(app),
        )

        app.state.mesh_identity = identity
        app.state.peer_registry = peers
        app.state.delegation_manager = mgr
        logger.info("startup.mesh: initialized node_id=%s", identity.node_id[:16])
    except Exception as exc:
        logger.error("startup.mesh: initialization failed — %s", exc)
        app.state.mesh_identity = None
        app.state.peer_registry = None
        app.state.delegation_manager = None


# ---------------------------------------------------------------------------
# Network / CDP stores (populated per-session by session manager)
# ---------------------------------------------------------------------------

def _init_network_stores(app) -> None:
    app.state.network_inspectors = {}   # session_id → NetworkInspector
    app.state.cdp_sessions = {}         # session_id → CDPPassthrough
    logger.debug("startup.extensions: network/CDP stores initialized")


def _build_mesh_tool_gateway(app):
    gateway = getattr(app.state, "tool_gateway", None)
    if gateway is None:
        return None

    async def _call(tool_name: str, arguments: dict, session_id: str) -> dict:
        from app.models import McpToolCallRequest

        payload_args = dict(arguments or {})
        if session_id and "session_id" not in payload_args:
            payload_args["session_id"] = session_id
        response = await gateway.call_tool(McpToolCallRequest(name=tool_name, arguments=payload_args))

        structured = response.structuredContent
        if isinstance(structured, dict):
            result = dict(structured)
        elif structured is not None:
            result = {"result": structured}
        else:
            text = "".join(item.text or "" for item in response.content)
            result = {"text": text} if text else {}

        if response.isError and result.get("status") != "approval_required":
            result["_mesh_error"] = True
        return result

    return _call


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

def _init_workflow_engine(app) -> None:
    from app.workflow.engine import WorkflowEngine
    wf_root = Path(os.environ.get("WORKFLOWS_ROOT", "/data/workflows"))
    engine = WorkflowEngine(workflows_root=wf_root)
    app.state.workflow_engine = engine
    logger.info("startup.workflow: engine initialized root=%s", wf_root)


# ---------------------------------------------------------------------------
# Extracted social/Veo3 state
# ---------------------------------------------------------------------------

def _disable_extracted_social_state(app) -> None:
    """Keep removed social/Veo3 app.state names inert for old extensions/tests."""
    app.state.youtube_client = None
    app.state.instagram_client = None
    app.state.reddit_client = None
    app.state.x_client = None
    app.state.veo3_client = None
    app.state.viral_engine = None
    logger.info("startup.social: social/Veo3 integrations are extracted from the controller")


def _register_session_hooks(app) -> None:
    manager = getattr(app.state, "browser_manager", None)
    if manager is None or not hasattr(manager, "register_extension_hooks"):
        logger.debug("startup.extensions: browser manager hook registration unavailable")
        return

    async def _created(session_id: str, page) -> None:
        await on_session_created(app, session_id, page)

    async def _closed(session_id: str) -> None:
        await on_session_closed(app, session_id)

    manager.register_extension_hooks(session_created=_created, session_closed=_closed)


# ---------------------------------------------------------------------------
# Session lifecycle hooks (called by browser manager)
# ---------------------------------------------------------------------------

async def on_session_created(app, session_id: str, page) -> None:
    """
    Called when a new browser session is created.
    Attaches NetworkInspector and CDPPassthrough to the session.
    """
    manager = getattr(app.state, "browser_manager", None)
    session = manager.sessions.get(session_id) if manager is not None else None
    if session is not None and getattr(session, "network_inspector", None) is not None:
        app.state.network_inspectors[session_id] = session.network_inspector

    try:
        from app.cdp.passthrough import CDPPassthrough

        app.state.cdp_sessions[session_id] = await CDPPassthrough.from_page(page)
    except Exception as exc:
        logger.warning("on_session_created: cdp session failed — %s", exc)

    try:
        settings = getattr(app.state, "settings", None)
        stealth_profile = os.environ.get("STEALTH_PROFILE", "off")
        if getattr(settings, "stealth_enabled", False) and stealth_profile != "off":
            from app.stealth.fingerprint import FingerprintConfig, apply_fingerprint
            config = FingerprintConfig(session_id, stealth_profile)
            await apply_fingerprint(page.context, config)
            logger.debug("on_session_created: stealth profile=%s applied", stealth_profile)
    except Exception as exc:
        logger.warning("on_session_created: stealth fingerprint failed — %s", exc)


async def on_session_closed(app, session_id: str) -> None:
    """Called when a browser session is closed. Cleans up per-session resources."""
    app.state.network_inspectors.pop(session_id, None)
    app.state.cdp_sessions.pop(session_id, None)

    # Post-session Curator review.
    # Fire-and-forget — never blocks session close, never raises.
    try:
        curator = getattr(app.state, "curator_adapter", None)
        if curator is not None and getattr(curator, "ready", False):
            import asyncio as _asyncio
            _asyncio.create_task(_curator_review_session(app, session_id, curator))
    except Exception as exc:
        logger.debug("on_session_closed: curator review skipped — %s", exc)


async def _curator_review_session(app, session_id: str, curator) -> None:
    """
    Curator reviews the session's audit trail and optionally drafts a skill
    into /data/skills-staging/<session_id>/. Errors are swallowed; this hook
    must never disturb the foreground close path.
    """
    try:
        from pathlib import Path
        staging = Path(os.environ.get("SKILLS_STAGING_ROOT", "/data/skills-staging")) / session_id
        staging.mkdir(parents=True, exist_ok=True)
        # Build a lightweight transcript stub — callers that wire a richer
        # audit hook can override this path.
        transcript = f"Session {session_id}: (audit-trail transcript would go here)"
        synthesis_prompt = (
            "You are a Skills Curator. Given a browser-agent session transcript,"
            " write a short reusable `interaction-skill` markdown snippet (title,"
            " when-to-use, steps) if-and-only-if the session contains a repeatable"
            " pattern worth saving. Otherwise, reply exactly: NO_SKILL."
        )
        reply = await curator.complete(prompt=transcript, system=synthesis_prompt)
        if reply and reply.strip() != "NO_SKILL":
            (staging / "draft.md").write_text(reply)
            logger.info("curator: drafted staging skill for session %s", session_id)
    except Exception as exc:  # pragma: no cover — defensive, fire-and-forget
        logger.debug("curator review failed for session %s: %s", session_id, exc)
