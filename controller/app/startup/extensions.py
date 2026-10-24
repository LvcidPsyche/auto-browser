"""
startup.extensions — Initialize the optional subsystems (mesh, harness, network
inspector, curator) at app startup.

Call register_extensions(app) from main.py after app creation.
All clients are initialized from environment variables.
Missing credentials = subsystem disabled with a warning (never crash on startup).
"""

from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)


def register_extensions(app) -> None:
    """
    Wire the optional subsystems into app.state.

    Subsystems initialized:
        mesh_identity       — NodeIdentity
        peer_registry       — PeerRegistryFile
        delegation_manager  — DelegationManager
        network_inspectors  — dict[session_id, NetworkInspector]
        cdp_sessions        — dict[session_id, CDPPassthrough]
    """
    _init_mesh(app)
    _init_harness(app)
    _init_network_stores(app)
    _disable_extracted_social_state(app)
    _init_curator(app)
    _register_session_hooks(app)
    logger.info("startup.extensions: optional subsystems registered")


# ---------------------------------------------------------------------------
# Skills Curator adapter
# ---------------------------------------------------------------------------


def _init_curator(app) -> None:
    """Initialize the Skills Curator LLM adapter, when CURATOR_ENABLED asks for it.

    It used to start whenever a provider key was present — and ANTHROPIC_API_KEY
    is there for anyone running the Claude agent provider — so every closed
    session paid for an LLM call (claude-opus-4-7 by default) on a placeholder
    transcript and wrote whatever came back to skills-staging.
    """
    settings = getattr(app.state, "settings", None)
    if not getattr(settings, "curator_enabled", False):
        app.state.curator_adapter = None
        logger.info("startup.curator: disabled (set CURATOR_ENABLED=true to turn it on)")
        return
    try:
        from app.curator_llm import build_curator_adapter

        adapter = build_curator_adapter()
        app.state.curator_adapter = adapter
        if adapter is not None and adapter.ready:
            # The post-session review has no transcript to send yet: it used to send a
            # placeholder string on every session close, so the call is not made at all.
            logger.warning(
                "startup.curator: %s adapter ready (model=%s), but the post-session review is not "
                "wired to a session transcript yet, so no model call is made on session close",
                adapter.provider,
                adapter.model,
            )
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
# Convergence harness
# ---------------------------------------------------------------------------


def _init_harness(app) -> None:
    try:
        from app.harness.iterate import HarnessService
        from app.harness.register import mesh_identity_signer, mesh_identity_verifier

        settings = getattr(app.state, "settings", None)
        root = Path(getattr(settings, "harness_root", None) or os.environ.get("HARNESS_ROOT", "/data/harness"))
        identity = getattr(app.state, "mesh_identity", None)
        signer = mesh_identity_signer(identity) if identity is not None else None
        service = HarnessService(
            root,
            verifier=_build_harness_verifier(settings),
            signer=signer,
            envelope_verifier=mesh_identity_verifier(identity) if identity is not None else None,
            model_tiers=_harness_model_tiers(settings),
        )
        app.state.harness_service = service
        gateway = getattr(app.state, "tool_gateway", None)
        if gateway is not None:
            gateway.harness_service = service
        logger.info("startup.harness: initialized root=%s signed=%s", root, bool(signer))
    except Exception as exc:
        logger.error("startup.harness: initialization failed — %s", exc)
        app.state.harness_service = None


def _build_harness_verifier(settings):
    from app.harness.verifier import EnsembleVerifier, ProgrammaticVerifier, UniversalVerifierAdapter

    mode = str(getattr(settings, "harness_verifier", "") or os.environ.get("HARNESS_VERIFIER", "programmatic")).lower()
    uv_command = str(getattr(settings, "harness_uv_command", "") or os.environ.get("HARNESS_UV_COMMAND", ""))
    uv = UniversalVerifierAdapter(command=shlex.split(uv_command) if uv_command else None)
    programmatic = ProgrammaticVerifier()
    if mode == "uv":
        return uv
    if mode == "ensemble":
        return EnsembleVerifier([programmatic, uv])
    return programmatic


def _harness_model_tiers(settings) -> dict[str, str]:
    return {
        "explorer": str(
            getattr(settings, "harness_explorer_model", "") or os.environ.get("HARNESS_EXPLORER_MODEL", "")
        ),
        "verifier": str(
            getattr(settings, "harness_verifier_model", "") or os.environ.get("HARNESS_VERIFIER_MODEL", "")
        ),
        "executor": str(
            getattr(settings, "harness_executor_model", "") or os.environ.get("HARNESS_EXECUTOR_MODEL", "")
        ),
    }


# ---------------------------------------------------------------------------
# Network / CDP stores (populated per-session by session manager)
# ---------------------------------------------------------------------------


def _init_network_stores(app) -> None:
    app.state.network_inspectors = {}  # session_id → NetworkInspector
    app.state.cdp_sessions = {}  # session_id → CDPPassthrough
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
            text = "".join(getattr(item, "text", None) or "" for item in response.content)
            result = {"text": text} if text else {}

        if response.isError and result.get("status") != "approval_required":
            result["_mesh_error"] = True
        return result

    return _call


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

    async def _create_failed(session_id: str) -> None:
        _release_session_state(app, session_id)

    manager.register_extension_hooks(
        session_created=_created, session_closed=_closed, session_create_failed=_create_failed
    )


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


def _release_session_state(app, session_id: str) -> None:
    """Drop what on_session_created registered. Shared by close and a failed create."""
    app.state.network_inspectors.pop(session_id, None)
    app.state.cdp_sessions.pop(session_id, None)


async def on_session_closed(app, session_id: str) -> None:
    """Called when a browser session is closed. Cleans up per-session resources.

    No curator review runs here: nothing builds a session transcript yet, and the
    review used to send a placeholder string to the model on every close.
    """
    _release_session_state(app, session_id)
