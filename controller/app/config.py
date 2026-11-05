import math
import random
import re
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .browser_sandbox import SECCOMP_PROFILE_PATH
from .stealth.fingerprint import CHROME_UA_POOL

# The one place the Anthropic API base is written. It ends in /v1: every caller appends "/messages".
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

# USER_AGENT_POOL separates its entries with "|" or newlines. Not commas: every Chrome UA contains
# "(KHTML, like Gecko)", so a comma split handed contexts half a UA.
USER_AGENT_POOL_SEPARATOR = "|"
_USER_AGENT_POOL_SPLIT = re.compile(r"[|\r\n]")


class Settings(BaseSettings):
    app_env: str = Field("development", validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"))
    # repr=False and exclude=True: a loopback controller now writes its generated token
    # here, so neither a logged Settings nor model_dump() may carry it.
    api_bearer_token: str | None = Field(None, alias="API_BEARER_TOKEN", repr=False, exclude=True)
    # Named credentials: `alice:token-a,bob:token-b`. A request authenticating
    # against one of these has a *verified* operator identity, as opposed to the
    # self-asserted X-Operator-Id header. The shared API_BEARER_TOKEN above
    # still works and still leaves identity unverified.
    api_bearer_tokens: str = Field("", alias="API_BEARER_TOKENS", repr=False, exclude=True)
    # Where the controller keeps its own token when neither setting above is set.
    # A loopback controller generates one here on first start and demands it, and
    # a token placed here is used in any scope. Compose pins it to /data/api-token,
    # which is ./data/api-token on the host, where clients read it from.
    # See app/api_token.py.
    api_token_file: str = Field("/data/api-token", alias="API_TOKEN_FILE")
    # The old default: a loopback controller with no token serves every
    # route unauthenticated. "Loopback" is the host publish mapping, not who can
    # reach port 8000, so any container on the compose network can drive the API;
    # hence off by default. Never applies to an exposed or production controller.
    api_allow_tokenless_loopback: bool = Field(False, alias="API_ALLOW_TOKENLESS_LOOPBACK")
    # Whether this API is reachable off-box. Defaults to `exposed` on purpose:
    # an undeclared deployment is assumed reachable and must carry a token, so a
    # hand-rolled compose file that publishes on 0.0.0.0 fails closed. The base
    # compose declares `loopback` to match its 127.0.0.1 publish mapping.
    # See app/auth_policy.py.
    api_bind_scope: Literal["loopback", "exposed"] = Field("exposed", alias="API_BIND_SCOPE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    browser_ws_endpoint: str | None = Field(
        None,
        validation_alias=AliasChoices("BROWSER_WS_ENDPOINT", "BROWSER_CDP_ENDPOINT"),
    )
    browser_ws_endpoint_file: str = Field(
        "/data/browser-profile/browser-ws-endpoint.txt",
        validation_alias=AliasChoices("BROWSER_WS_ENDPOINT_FILE", "BROWSER_CDP_WS_ENDPOINT_FILE"),
    )
    takeover_url: str = Field(
        "http://localhost:6080/vnc.html?autoconnect=true&resize=scale",
        alias="TAKEOVER_URL",
    )
    remote_access_info_path: str = Field(
        "/data/tunnels/reverse-ssh.json",
        alias="REMOTE_ACCESS_INFO_PATH",
    )
    remote_access_stale_after_seconds: float = Field(
        45.0,
        alias="REMOTE_ACCESS_STALE_AFTER_SECONDS",
    )
    artifact_root: str = Field("/data/artifacts", alias="ARTIFACT_ROOT")
    upload_root: str = Field("/data/uploads", alias="UPLOAD_ROOT")
    auth_root: str = Field("/data/auth", alias="AUTH_ROOT")
    approval_root: str = Field("/data/approvals", alias="APPROVAL_ROOT")
    audit_root: str = Field("/data/audit", alias="AUDIT_ROOT")
    witness_root: str = Field("/data/witness", alias="WITNESS_ROOT")
    state_db_path: str | None = Field(None, alias="STATE_DB_PATH")
    audit_max_events: int = Field(10000, alias="AUDIT_MAX_EVENTS")
    session_store_root: str = Field("/data/sessions", alias="SESSION_STORE_ROOT")
    job_store_root: str = Field("/data/jobs", alias="JOB_STORE_ROOT")
    compliance_template: str | None = Field(None, alias="COMPLIANCE_TEMPLATE")
    compliance_manifest_path: str = Field(
        "/data/compliance-manifest.json",
        alias="COMPLIANCE_MANIFEST_PATH",
    )
    redis_url: str | None = Field(None, alias="REDIS_URL")
    session_store_redis_prefix: str = Field(
        "auto_browser:sessions",
        alias="SESSION_STORE_REDIS_PREFIX",
    )
    agent_job_worker_count: int = Field(1, alias="AGENT_JOB_WORKER_COUNT")
    auth_state_encryption_key: str | None = Field(None, alias="AUTH_STATE_ENCRYPTION_KEY", repr=False)
    require_auth_state_encryption: bool = Field(False, alias="REQUIRE_AUTH_STATE_ENCRYPTION")
    auth_state_max_age_hours: float = Field(72.0, alias="AUTH_STATE_MAX_AGE_HOURS")
    harness_root: str = Field("/data/harness", alias="HARNESS_ROOT")
    harness_verifier: str = Field("programmatic", alias="HARNESS_VERIFIER")
    harness_uv_command: str = Field("", alias="HARNESS_UV_COMMAND")
    harness_explorer_model: str = Field("", alias="HARNESS_EXPLORER_MODEL")
    harness_verifier_model: str = Field("", alias="HARNESS_VERIFIER_MODEL")
    harness_executor_model: str = Field("", alias="HARNESS_EXECUTOR_MODEL")
    # Mesh: signed delegation between auto-browser nodes (docs/mesh.md). Off by default.
    mesh_enabled: bool = Field(False, alias="MESH_ENABLED")
    mesh_identity_dir: str = Field("/data/mesh/identity", alias="MESH_IDENTITY_DIR")
    mesh_peers_path: str = Field("/data/mesh/peers.json", alias="MESH_PEERS_PATH")
    mesh_timestamp_window: float = Field(30.0, alias="MESH_TIMESTAMP_WINDOW")
    memory_root: str = Field("/data/memory", alias="MEMORY_ROOT")
    memory_enabled: bool = Field(True, alias="MEMORY_ENABLED")
    ocr_enabled: bool = Field(True, alias="OCR_ENABLED")
    ocr_language: str = Field("eng", alias="OCR_LANGUAGE")
    ocr_max_blocks: int = Field(20, alias="OCR_MAX_BLOCKS")
    ocr_text_limit: int = Field(1200, alias="OCR_TEXT_LIMIT")
    operator_id_header: str = Field("X-Operator-Id", alias="OPERATOR_ID_HEADER")
    operator_name_header: str = Field("X-Operator-Name", alias="OPERATOR_NAME_HEADER")
    require_operator_id: bool = Field(False, alias="REQUIRE_OPERATOR_ID")
    mcp_allowed_origins: str = Field("", alias="MCP_ALLOWED_ORIGINS")
    controller_allowed_hosts: str = Field("", alias="CONTROLLER_ALLOWED_HOSTS")
    # lean, curated or full; MCP_TOOL_PACKS adds packs (name) or removes them (-name) on top. Both are
    # checked when the tool gateway is built: an unknown pack stops startup, an unknown profile warns.
    mcp_tool_profile: str = Field("curated", alias="MCP_TOOL_PROFILE")
    mcp_tool_packs: str = Field("", alias="MCP_TOOL_PACKS")
    metrics_enabled: bool = Field(True, alias="METRICS_ENABLED")
    session_isolation_mode: Literal["shared_browser_node", "docker_ephemeral"] = Field(
        "shared_browser_node", alias="SESSION_ISOLATION_MODE"
    )
    isolated_browser_image: str = Field(
        "auto-browser-browser-node:latest",
        alias="ISOLATED_BROWSER_IMAGE",
    )
    isolated_browser_container_prefix: str = Field(
        "browser-session",
        alias="ISOLATED_BROWSER_CONTAINER_PREFIX",
    )
    isolated_browser_wait_timeout_seconds: int = Field(
        45,
        alias="ISOLATED_BROWSER_WAIT_TIMEOUT_SECONDS",
    )
    isolated_browser_keep_containers: bool = Field(
        False,
        alias="ISOLATED_BROWSER_KEEP_CONTAINERS",
    )
    isolated_browser_bind_host: str = Field("127.0.0.1", alias="ISOLATED_BROWSER_BIND_HOST")
    # Handed to each isolated browser container, whose noVNC port is published
    # on ISOLATED_BROWSER_BIND_HOST. See browser-node/entrypoint.sh.
    vnc_password: str | None = Field(None, alias="VNC_PASSWORD", repr=False)
    # Chromium's own sandbox in browser-node and every isolated container (browser-node/sandbox.mjs). auto runs
    # it sandboxed and falls back to unsandboxed with a WARNING naming the cause; on refuses to run unsandboxed;
    # off never tries and applies no custom seccomp profile to isolated containers (Docker's default).
    browser_sandbox: Literal["auto", "on", "off"] = Field("auto", alias="BROWSER_SANDBOX")
    # The seccomp profile isolated containers get under auto and on (sent to the daemon as JSON). Under on, a
    # missing file refuses the session; under auto the container starts with Docker's default and falls back.
    isolated_browser_seccomp_profile: str = Field(
        str(SECCOMP_PROFILE_PATH),
        alias="ISOLATED_BROWSER_SECCOMP_PROFILE",
    )
    isolated_browser_mem_limit: str = Field("4g", alias="ISOLATED_BROWSER_MEM_LIMIT")
    isolated_browser_pids_limit: int = Field(2048, alias="ISOLATED_BROWSER_PIDS_LIMIT")
    isolated_browser_cpus: float = Field(0.0, alias="ISOLATED_BROWSER_CPUS")
    isolated_takeover_host: str = Field("127.0.0.1", alias="ISOLATED_TAKEOVER_HOST")
    isolated_takeover_scheme: str = Field("http", alias="ISOLATED_TAKEOVER_SCHEME")
    isolated_takeover_path: str = Field(
        "/vnc.html?autoconnect=true&resize=scale",
        alias="ISOLATED_TAKEOVER_PATH",
    )
    # per_session gives each docker_ephemeral browser its own bridge network,
    # joined by the controller, so one session's browser cannot reach another's
    # Playwright server (9223), noVNC (6080) or the shared browser-node. shared
    # is the explicit opt-out: every browser on one network, the old behavior.
    isolated_browser_network_mode: Literal["per_session", "shared"] = Field(
        "per_session",
        alias="ISOLATED_BROWSER_NETWORK_MODE",
    )
    # Only read in shared mode; per_session ignores it (with a startup warning).
    isolated_browser_network: str | None = Field(None, alias="ISOLATED_BROWSER_NETWORK")
    isolated_host_data_root: str | None = Field(None, alias="ISOLATED_HOST_DATA_ROOT")
    isolated_docker_host: str | None = Field(None, alias="ISOLATED_DOCKER_HOST")
    isolated_tunnel_enabled: bool = Field(False, alias="ISOLATED_TUNNEL_ENABLED")
    isolated_tunnel_host: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_HOST", "REVERSE_SSH_HOST"),
    )
    isolated_tunnel_port: int = Field(
        22,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PORT", "REVERSE_SSH_PORT"),
    )
    isolated_tunnel_user: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_USER", "REVERSE_SSH_USER"),
    )
    isolated_tunnel_key_path: str = Field("/data/ssh/id_ed25519", alias="ISOLATED_TUNNEL_KEY_PATH")
    isolated_tunnel_known_hosts_path: str = Field(
        "/data/ssh/known_hosts",
        alias="ISOLATED_TUNNEL_KNOWN_HOSTS_PATH",
    )
    isolated_tunnel_strict_host_key_checking: str = Field(
        "yes",
        alias="ISOLATED_TUNNEL_STRICT_HOST_KEY_CHECKING",
    )
    isolated_tunnel_remote_bind_address: str = Field(
        "127.0.0.1",
        alias="ISOLATED_TUNNEL_REMOTE_BIND_ADDRESS",
    )
    isolated_tunnel_remote_port_start: int = Field(16181, alias="ISOLATED_TUNNEL_REMOTE_PORT_START")
    isolated_tunnel_remote_port_end: int = Field(16240, alias="ISOLATED_TUNNEL_REMOTE_PORT_END")
    isolated_tunnel_server_alive_interval: int = Field(
        30,
        alias="ISOLATED_TUNNEL_SERVER_ALIVE_INTERVAL",
    )
    isolated_tunnel_server_alive_count_max: int = Field(
        3,
        alias="ISOLATED_TUNNEL_SERVER_ALIVE_COUNT_MAX",
    )
    isolated_tunnel_info_interval_seconds: float = Field(
        10.0,
        alias="ISOLATED_TUNNEL_INFO_INTERVAL_SECONDS",
    )
    isolated_tunnel_startup_grace_seconds: float = Field(
        1.0,
        alias="ISOLATED_TUNNEL_STARTUP_GRACE_SECONDS",
    )
    isolated_tunnel_access_mode: str = Field(
        "private",
        validation_alias=AliasChoices("ISOLATED_TUNNEL_ACCESS_MODE", "REVERSE_SSH_ACCESS_MODE"),
    )
    isolated_tunnel_public_host: str | None = Field(
        None,
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PUBLIC_HOST", "REVERSE_SSH_PUBLIC_HOST"),
    )
    isolated_tunnel_public_scheme: str = Field(
        "http",
        validation_alias=AliasChoices("ISOLATED_TUNNEL_PUBLIC_SCHEME", "REVERSE_SSH_PUBLIC_SCHEME"),
    )
    isolated_tunnel_local_host: str = Field("host.docker.internal", alias="ISOLATED_TUNNEL_LOCAL_HOST")
    isolated_tunnel_info_root: str = Field("/data/tunnels/sessions", alias="ISOLATED_TUNNEL_INFO_ROOT")
    allowed_hosts: str = Field("example.com,localhost,127.0.0.1,::1", alias="ALLOWED_HOSTS")
    # Stop a top-level navigation to a host ALLOWED_HOSTS refuses before the browser sends it
    # (app/browser/request_gate.py). The post-commit guard stays on either way; off, it is the only one.
    pre_request_blocking: bool = Field(True, alias="PRE_REQUEST_BLOCKING")
    default_viewport_width: int = Field(1280, alias="DEFAULT_VIEWPORT_WIDTH")
    default_viewport_height: int = Field(800, alias="DEFAULT_VIEWPORT_HEIGHT")
    connect_retries: int = Field(60, alias="CONNECT_RETRIES")
    connect_retry_delay_seconds: float = Field(1.0, alias="CONNECT_RETRY_DELAY_SECONDS")
    max_sessions: int = Field(1, alias="MAX_SESSIONS")
    require_approval_for_uploads: bool = Field(True, alias="REQUIRE_APPROVAL_FOR_UPLOADS")
    approval_ttl_minutes: int = Field(15, alias="APPROVAL_TTL_MINUTES")
    # Lets agents approve their own pending actions: MCP browser.approve_approval
    # and the built-in agent loop. Off, an operator approves every one.
    autonomous_approvals: bool = Field(False, alias="AUTONOMOUS_APPROVALS")
    # Runs every MCP tool that has a governed_kind, and every step of the agent
    # loop, as `governed` whatever workflow_profile the caller passed. Off,
    # governed approvals are the calling agent's choice. See
    # docs/production-hardening.md.
    force_governed_approvals: bool = Field(False, alias="FORCE_GOVERNED_APPROVALS")
    witness_enabled: bool = Field(True, alias="WITNESS_ENABLED")
    witness_protection_mode_default: Literal["normal", "confidential"] = Field(
        "normal",
        alias="WITNESS_PROTECTION_MODE_DEFAULT",
    )
    witness_remote_url: str | None = Field(None, alias="WITNESS_REMOTE_URL")
    witness_remote_api_key: str | None = Field(None, alias="WITNESS_REMOTE_API_KEY", repr=False)
    witness_remote_tenant_id: str | None = Field(None, alias="WITNESS_REMOTE_TENANT_ID")
    witness_remote_timeout_seconds: float = Field(0.75, alias="WITNESS_REMOTE_TIMEOUT_SECONDS")
    witness_remote_verify_tls: bool = Field(True, alias="WITNESS_REMOTE_VERIFY_TLS")
    witness_remote_required_for_confidential: bool = Field(
        False,
        alias="WITNESS_REMOTE_REQUIRED_FOR_CONFIDENTIAL",
    )
    enable_tracing: bool = Field(False, alias="ENABLE_TRACING")
    action_timeout_ms: int = Field(15000, alias="ACTION_TIMEOUT_MS")
    request_rate_limit_enabled: bool = Field(True, alias="REQUEST_RATE_LIMIT_ENABLED")
    request_rate_limit_requests: int = Field(120, alias="REQUEST_RATE_LIMIT_REQUESTS")
    request_rate_limit_window_seconds: int = Field(60, alias="REQUEST_RATE_LIMIT_WINDOW_SECONDS")
    request_rate_limit_max_buckets: int = Field(4096, alias="REQUEST_RATE_LIMIT_MAX_BUCKETS")
    request_rate_limit_exempt_paths: str = Field(
        "/healthz,/readyz,/docs,/openapi.json,/redoc,/artifacts,/metrics",
        alias="REQUEST_RATE_LIMIT_EXEMPT_PATHS",
    )
    mcp_session_store_path: str = Field("/data/mcp/sessions.json", alias="MCP_SESSION_STORE_PATH")
    cleanup_on_startup: bool = Field(True, alias="CLEANUP_ON_STARTUP")
    cleanup_interval_seconds: float = Field(3600.0, alias="CLEANUP_INTERVAL_SECONDS")
    artifact_retention_hours: float = Field(168.0, alias="ARTIFACT_RETENTION_HOURS")
    # A page starts these downloads, not the operator: past either cap the file is not kept.
    download_max_bytes: int = Field(268435456, alias="DOWNLOAD_MAX_BYTES")
    downloads_max_per_session: int = Field(100, alias="DOWNLOADS_MAX_PER_SESSION")
    upload_retention_hours: float = Field(168.0, alias="UPLOAD_RETENTION_HOURS")
    auth_retention_hours: float = Field(168.0, alias="AUTH_RETENTION_HOURS")

    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY", repr=False)
    openai_base_url: str = Field("https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-6.1-sol", alias="OPENAI_MODEL")
    openai_auth_mode: str = Field("api", alias="OPENAI_AUTH_MODE")
    openai_cli_path: str = Field("codex", alias="OPENAI_CLI_PATH")
    openai_cli_model: str | None = Field(None, alias="OPENAI_CLI_MODEL")
    openai_host_bridge_socket: str = Field(
        "/data/host-bridge/codex.sock",
        alias="OPENAI_HOST_BRIDGE_SOCKET",
    )
    # How the Codex CLI is allowed to execute on the host. Both Codex paths used
    # to pass --dangerously-bypass-approvals-and-sandbox unconditionally; they
    # now run sandboxed unless a deployment opts back in. See app/codex_sandbox.py.
    codex_sandbox_mode: str = Field("read-only", alias="CODEX_SANDBOX_MODE")
    codex_allow_host_exec: bool = Field(False, alias="CODEX_BRIDGE_ALLOW_HOST_EXEC")

    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY", repr=False)
    anthropic_base_url: str = Field(ANTHROPIC_DEFAULT_BASE_URL, alias="ANTHROPIC_BASE_URL")
    anthropic_version: str = Field("2023-06-01", alias="ANTHROPIC_VERSION")
    claude_model: str = Field("claude-sonnet-4-6", alias="CLAUDE_MODEL")
    vision_model: str = Field("claude-haiku-4-5-20251001", alias="VISION_MODEL")
    claude_auth_mode: str = Field("api", alias="CLAUDE_AUTH_MODE")
    claude_cli_path: str = Field("claude", alias="CLAUDE_CLI_PATH")
    claude_cli_model: str | None = Field(None, alias="CLAUDE_CLI_MODEL")

    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY", repr=False)
    gemini_base_url: str = Field(
        "https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_BASE_URL",
    )
    gemini_model: str = Field("gemini-3.5-flash", alias="GEMINI_MODEL")
    gemini_auth_mode: str = Field("api", alias="GEMINI_AUTH_MODE")
    gemini_cli_path: str = Field("gemini", alias="GEMINI_CLI_PATH")
    gemini_cli_model: str | None = Field(None, alias="GEMINI_CLI_MODEL")
    cli_home: str | None = Field("/data/cli-home", alias="CLI_HOME")
    # Extra environment variable names (comma-separated) handed to provider CLI children as-is.
    cli_provider_env_passthrough: str = Field("", alias="CLI_PROVIDER_ENV_PASSTHROUGH")

    # OpenAI-compatible providers, all served by one generic adapter. Set the API key
    # (and, for openrouter/openai_compatible, a model) to enable each. base_url defaults
    # are stable; model defaults are current-but-overridable.
    openrouter_api_key: str | None = Field(None, alias="OPENROUTER_API_KEY", repr=False)
    openrouter_base_url: str = Field("https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
    openrouter_model: str = Field("", alias="OPENROUTER_MODEL")

    xai_api_key: str | None = Field(None, alias="XAI_API_KEY", repr=False)
    xai_base_url: str = Field("https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field("grok-4", alias="XAI_MODEL")

    deepseek_api_key: str | None = Field(None, alias="DEEPSEEK_API_KEY", repr=False)
    deepseek_base_url: str = Field("https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")

    minimax_api_key: str | None = Field(None, alias="MINIMAX_API_KEY", repr=False)
    minimax_base_url: str = Field("https://api.minimax.io/v1", alias="MINIMAX_BASE_URL")
    minimax_model: str = Field("MiniMax-M3", alias="MINIMAX_MODEL")

    openai_compatible_api_key: str | None = Field(None, alias="OPENAI_COMPATIBLE_API_KEY", repr=False)
    openai_compatible_base_url: str = Field("", alias="OPENAI_COMPATIBLE_BASE_URL")
    openai_compatible_model: str = Field("", alias="OPENAI_COMPATIBLE_MODEL")

    model_request_timeout_seconds: float = Field(60.0, alias="MODEL_REQUEST_TIMEOUT_SECONDS")
    model_max_retries: int = Field(2, alias="MODEL_MAX_RETRIES")
    model_retry_backoff_seconds: float = Field(1.0, alias="MODEL_RETRY_BACKOFF_SECONDS")

    # Interaction pacing
    human_typing_min_delay_ms: int = Field(40, alias="HUMAN_TYPING_MIN_DELAY_MS")
    human_typing_max_delay_ms: int = Field(130, alias="HUMAN_TYPING_MAX_DELAY_MS")

    # Stealth / anti-bot
    stealth_enabled: bool = Field(False, alias="STEALTH_ENABLED")
    stealth_profile: Literal["off", "light", "aggressive"] = Field("off", alias="STEALTH_PROFILE")
    # Defaults to the single authoritative pool in stealth.fingerprint so the
    # context-level UA and the fingerprint layer cannot drift apart.
    user_agent_pool: str = Field(
        USER_AGENT_POOL_SEPARATOR.join(CHROME_UA_POOL),
        alias="USER_AGENT_POOL",
    )

    # Approval webhooks — notified when approvals are created or decided
    approval_webhook_url: str | None = Field(None, alias="APPROVAL_WEBHOOK_URL")
    approval_webhook_secret: str | None = Field(None, alias="APPROVAL_WEBHOOK_SECRET", repr=False)

    # Perception presets — default mode for /observe
    # text: no screenshot, no OCR — accessibility tree + text extraction only
    # fast: screenshot only (skip OCR and accessibility tree)
    # normal: screenshot + OCR + accessibility tree (current default)
    # rich: normal + extended text + full DOM outline
    perception_preset_default: str = Field("normal", alias="PERCEPTION_PRESET_DEFAULT")

    # Skip OCR on normal/rich observes when text extraction already produced usable
    # content (non-empty text_excerpt + available accessibility tree). Never skipped
    # while screenshot PII scrubbing is active, since scrubbing consumes OCR blocks.
    # Escape hatch: set to false to always run OCR.
    ocr_skip_when_text_available: bool = Field(True, alias="OCR_SKIP_WHEN_TEXT_AVAILABLE")

    # SSE keepalive — how often to send a comment to prevent proxy timeouts
    sse_keepalive_seconds: float = Field(15.0, alias="SSE_KEEPALIVE_SECONDS")

    # Proxy (session-level override supported in CreateSessionRequest)
    default_proxy_server: str | None = Field(None, alias="DEFAULT_PROXY_SERVER")
    default_proxy_username: str | None = Field(None, alias="DEFAULT_PROXY_USERNAME")
    default_proxy_password: str | None = Field(None, alias="DEFAULT_PROXY_PASSWORD", repr=False)

    # Proxy personas — path to JSON file mapping name → {server, username, password}
    proxy_persona_file: str | None = Field(None, alias="PROXY_PERSONA_FILE")

    # PII scrubbing
    pii_scrub_enabled: bool = Field(True, alias="PII_SCRUB_ENABLED")
    pii_scrub_screenshot: bool = Field(True, alias="PII_SCRUB_SCREENSHOT")
    # With screenshot scrubbing on but no OCR to drive it, withhold the screenshot (and fail
    # /readiness) instead of returning it unscrubbed with a warning.
    pii_scrub_screenshot_strict: bool = Field(False, alias="PII_SCRUB_SCREENSHOT_STRICT")
    # Also redact the before/after snapshot of every action. It runs OCR twice
    # per action; false makes actions much faster and leaves those images
    # unredacted (observations stay redacted).
    pii_scrub_action_screenshots: bool = Field(True, alias="PII_SCRUB_ACTION_SCREENSHOTS")
    pii_scrub_network: bool = Field(True, alias="PII_SCRUB_NETWORK")
    pii_scrub_console: bool = Field(True, alias="PII_SCRUB_CONSOLE")
    pii_scrub_patterns: str = Field("", alias="PII_SCRUB_PATTERNS")  # "" = all patterns
    pii_scrub_replacement: str = Field("[REDACTED]", alias="PII_SCRUB_REPLACEMENT")
    pii_scrub_audit_report: bool = Field(True, alias="PII_SCRUB_AUDIT_REPORT")

    # Network inspector
    network_inspector_enabled: bool = Field(True, alias="NETWORK_INSPECTOR_ENABLED")
    network_inspector_max_entries: int = Field(500, alias="NETWORK_INSPECTOR_MAX_ENTRIES")
    network_inspector_capture_bodies: bool = Field(True, alias="NETWORK_INSPECTOR_CAPTURE_BODIES")
    network_inspector_body_max_bytes: int = Field(16384, alias="NETWORK_INSPECTOR_BODY_MAX_BYTES")

    # CDP attach mode — connect to an already-running Chrome instance
    cdp_connect_url: str | None = Field(None, alias="CDP_CONNECT_URL")
    # The operator's declaration that the Chrome reached over CDP (CDP_CONNECT_URL, browser.cdp_attach) was
    # started with --disable-features=BlockOriginHeaderModificationOnRedirect,AvoidCorsURLLoaderRestartOnRedirect
    # (request_gate.REDIRECT_ORIGIN_SWITCH). Without the first, older Chrome fails a cross-origin POST redirect the
    # pre-request gate let through, so the gate stops it itself and says why; without the second, Chrome 155+
    # aborts credentialed, preflighted cross-origin redirects in every tab while the gate is attached.
    cdp_block_origin_on_redirect_disabled: bool = Field(False, alias="CDP_BLOCK_ORIGIN_ON_REDIRECT_DISABLED")

    # Shadow browsing — enable headed mode for debugging
    # Off by default: it launches a headed Chromium inside the controller (its
    # sandbox required, so it does not start without one, but not verified),
    # next to every secret the controller holds, and the shipped image has
    # neither a browser nor a display for it anyway.
    shadow_browse_enabled: bool = Field(False, alias="SHADOW_BROWSE_ENABLED")
    # Post-session skill review by an LLM. Off unless asked for: it calls a paid
    # model on every session close.
    curator_enabled: bool = Field(False, alias="CURATOR_ENABLED")
    curator_provider: str = Field("claude", alias="CURATOR_PROVIDER")
    curator_model: str | None = Field(None, alias="CURATOR_MODEL")

    # Cron / webhook triggers
    cron_store_path: str = Field("/data/crons/crons.json", alias="CRON_STORE_PATH")
    cron_max_jobs: int = Field(50, alias="CRON_MAX_JOBS")

    # Shared session links
    share_token_secret: str | None = Field(None, alias="SHARE_TOKEN_SECRET", repr=False)
    share_token_ttl_minutes: int = Field(60, alias="SHARE_TOKEN_TTL_MINUTES")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator(
        "api_bearer_token",
        "witness_remote_api_key",
        "openai_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "openrouter_api_key",
        "xai_api_key",
        "deepseek_api_key",
        "minimax_api_key",
        "openai_compatible_api_key",
        mode="before",
    )
    @classmethod
    def _strip_secret(cls, value: object) -> object:
        # A key pasted with a space or newline around it went out as-is, and the provider
        # answered 401 (OpenRouter: "Missing Authentication header").
        # A whitespace-only API_BEARER_TOKEN was a credential that "Bearer" plus the same
        # whitespace satisfied (40 spaces even cleared the 32-character floor); it is unset now.
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("*")
    @classmethod
    def _floats_are_finite(cls, value: object, info: ValidationInfo) -> object:
        # nan compares false with everything, so a nan retention swept every artifact and a nan timeout never fired.
        # Zero and negative stay valid: ARTIFACT_RETENTION_HOURS <= 0 means "keep forever".
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be a finite number, got {value!r}")
        return value

    @field_validator("session_isolation_mode", mode="before")
    @classmethod
    def _isolation_mode_trimmed_and_lowercased(cls, value: object) -> object:
        # An unknown value used to mean the shared browser, silently: it is now refused at startup.
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("browser_sandbox", mode="before")
    @classmethod
    def _browser_sandbox_like_browser_node(cls, value: object) -> object:
        # browser-node/sandbox.mjs parseSetting trims and lowercases, and treats empty as auto: so does this, or
        # " On " would start the browsers and stop the controller.
        if isinstance(value, str):
            return value.strip().lower() or "auto"
        return value

    @field_validator("isolated_browser_seccomp_profile", mode="before")
    @classmethod
    def _empty_profile_is_the_shipped_one(cls, value: object) -> object:
        # Uncommenting `ISOLATED_BROWSER_SECCOMP_PROFILE=` in .env must not mean "no profile".
        if value is None or (isinstance(value, str) and not value.strip()):
            return str(SECCOMP_PROFILE_PATH)
        return value

    @property
    def cli_provider_env_passthrough_names(self) -> list[str]:
        return [item.strip() for item in self.cli_provider_env_passthrough.split(",") if item.strip()]

    @property
    def allowed_host_patterns(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def mcp_allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_origins.split(",") if item.strip()]

    @property
    def controller_allowed_host_patterns(self) -> list[str]:
        return [item.strip() for item in self.controller_allowed_hosts.split(",") if item.strip()]

    @property
    def request_rate_limit_exempt_path_list(self) -> list[str]:
        return [item.strip() for item in self.request_rate_limit_exempt_paths.split(",") if item.strip()]

    @property
    def environment_name(self) -> str:
        return self.app_env.strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment_name == "production"

    @property
    def random_user_agent(self) -> str:
        agents = [a.strip() for a in _USER_AGENT_POOL_SPLIT.split(self.user_agent_pool) if a.strip()]
        return random.choice(agents) if agents else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
