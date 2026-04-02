from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProtectionMode = Literal["normal", "confidential"]
WitnessRemoteStatus = Literal["disabled", "idle", "healthy", "delivered", "failed"]


class _WithApproval(BaseModel):
    """Mixin that adds an optional approval_id field to action request models."""

    approval_id: str | None = None


class CreateSessionRequest(BaseModel):
    name: str | None = None
    start_url: str | None = None
    storage_state_path: str | None = None
    auth_profile: str | None = None
    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    user_agent: str | None = None
    protection_mode: ProtectionMode | None = None
    totp_secret: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_auth_source(self) -> "CreateSessionRequest":
        if self.storage_state_path and self.auth_profile:
            raise ValueError("Provide auth_profile or storage_state_path, not both")
        return self


class ClickRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    x: float | None = None
    y: float | None = None


class TypeRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    text: str
    clear_first: bool = True
    sensitive: bool = False


class PressRequest(BaseModel):
    key: str


class ScrollRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 600


class SelectOptionRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    value: str | None = None
    label: str | None = None
    index: int | None = Field(default=None, ge=0)


class HoverRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    x: float | None = None
    y: float | None = None


class WaitRequest(BaseModel):
    wait_ms: int = Field(default=0, ge=0, le=30000, description="Milliseconds to wait (max 30s)")


class NavigateRequest(BaseModel):
    url: str


class UploadRequest(_WithApproval):
    selector: str | None = None
    element_id: str | None = None
    file_path: str
    approved: bool = False


class SaveStorageStateRequest(BaseModel):
    path: str = Field(description="Relative path inside /data/auth")


class SaveAuthProfileRequest(BaseModel):
    profile_name: str = Field(min_length=1, max_length=120)


class HumanTakeoverRequest(BaseModel):
    reason: str = "Manual review requested"


class ScreenshotRequest(BaseModel):
    label: str = Field(default="manual", min_length=1, max_length=120)


class ExecuteActionRequest(BaseModel):
    approval_id: str | None = None
    action: "BrowserActionDecision"


class TabIndexRequest(BaseModel):
    index: int = Field(ge=0)


class OpenTabRequest(BaseModel):
    url: str | None = None
    activate: bool = True


class SessionEnvelope(BaseModel):
    session: dict[str, Any]


class ActionEnvelope(BaseModel):
    action: str
    session: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    target: dict[str, Any]


PerceptionPreset = Literal["fast", "normal", "rich"]


class ObserveRequest(BaseModel):
    preset: PerceptionPreset = "normal"
    limit: int = Field(default=40, ge=1, le=200)


class ImportAuthProfileRequest(BaseModel):
    archive_path: str
    overwrite: bool = False


class ScreenshotDiffResponse(BaseModel):
    changed_pixels: int
    changed_pct: float
    diff_url: str | None
    diff_path: str | None
    a_url: str
    b_url: str
    width: int
    height: int


ActionName = Literal[
    "navigate",
    "click",
    "hover",
    "select_option",
    "type",
    "press",
    "scroll",
    "wait",
    "reload",
    "go_back",
    "go_forward",
    "upload",
    "social_login",
    "social_post",
    "social_comment",
    "social_like",
    "social_follow",
    "social_unfollow",
    "social_repost",
    "social_dm",
    "request_human_takeover",
    "done",
]
ProviderName = Literal["openai", "claude", "gemini"]
RiskCategory = Literal[
    "read",
    "write",
    "upload",
    "post",
    "payment",
    "account_change",
    "destructive",
]
ApprovalKind = Literal["upload", "post", "payment", "account_change", "destructive"]
ApprovalStatus = Literal["pending", "approved", "rejected", "executed"]
SessionStatus = Literal["active", "closed", "interrupted", "failed"]
AgentJobKind = Literal["agent_step", "agent_run"]
AgentJobStatus = Literal["queued", "running", "completed", "failed", "interrupted"]


class BrowserActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk_category: RiskCategory | None = None
    element_id: str | None = None
    selector: str | None = None
    x: float | None = None
    y: float | None = None
    text: str | None = None
    clear_first: bool = True
    sensitive: bool = False
    key: str | None = None
    value: str | None = None
    label: str | None = None
    index: int | None = Field(default=None, ge=0)
    delta_x: float = 0
    delta_y: float = 600
    wait_ms: int = Field(default=1000, ge=0, le=30000)
    url: str | None = None
    file_path: str | None = None
    recipient: str | None = None
    platform: str | None = None
    username: str | None = None

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "BrowserActionDecision":
        if self.risk_category is None:
            if self.action in {"navigate", "hover", "scroll", "wait", "reload", "go_back", "go_forward", "done"}:
                self.risk_category = "read"
            elif self.action == "social_login":
                self.risk_category = "account_change"
            elif self.action in {
                "social_post",
                "social_comment",
                "social_like",
                "social_follow",
                "social_unfollow",
                "social_repost",
                "social_dm",
            }:
                self.risk_category = "post"
            elif self.action == "upload":
                self.risk_category = "upload"
            elif self.action == "request_human_takeover":
                self.risk_category = "write"
            else:
                self.risk_category = "write"

        has_click_target = bool(self.element_id or self.selector or (self.x is not None and self.y is not None))
        has_locator_target = bool(self.element_id or self.selector)

        if self.action in {"click", "hover"} and not has_click_target:
            raise ValueError(f"{self.action} requires element_id, selector, or x+y coordinates")
        if self.action == "select_option":
            if not has_locator_target:
                raise ValueError("select_option requires element_id or selector")
            if self.value is None and self.label is None and self.index is None:
                raise ValueError("select_option requires value, label, or index")
        if self.action in {"type", "social_post", "social_comment"}:
            if not has_locator_target:
                if self.action == "type":
                    raise ValueError("type requires element_id or selector")
            if not self.text:
                raise ValueError(f"{self.action} requires text")
        if self.action == "social_dm":
            if not self.text:
                raise ValueError("social_dm requires text")
            if not self.recipient:
                raise ValueError("social_dm requires recipient")
        if self.action == "social_login":
            if not self.platform:
                raise ValueError("social_login requires platform")
            if not self.username:
                raise ValueError("social_login requires username")
        if self.action == "press" and not self.key:
            raise ValueError("press requires key")
        if self.action == "navigate" and not self.url:
            raise ValueError("navigate requires url")
        if self.action == "upload":
            if not has_locator_target:
                raise ValueError("upload requires element_id or selector")
            if not self.file_path:
                raise ValueError("upload requires file_path")
        return self


class AgentStepRequest(BaseModel):
    provider: ProviderName
    goal: str = Field(min_length=1, max_length=4000)
    provider_model: str | None = None
    observation_limit: int = Field(default=40, ge=1, le=100)
    context_hints: str | None = Field(default=None, max_length=4000)
    upload_approved: bool = False
    approval_id: str | None = None


class AgentRunRequest(AgentStepRequest):
    max_steps: int = Field(default=6, ge=1, le=20)


class ProviderInfo(BaseModel):
    provider: ProviderName
    configured: bool
    model: str | None = None
    auth_mode: str = "api"
    detail: str | None = None
    login_command: str | None = None


class ProviderDecisionEnvelope(BaseModel):
    provider: ProviderName
    model: str
    decision: BrowserActionDecision
    usage: dict[str, Any] | None = None
    raw_text: str | None = None


class AgentStepResult(BaseModel):
    provider: ProviderName
    model: str
    goal: str
    status: Literal["acted", "done", "takeover", "approval_required", "error"]
    observation: dict[str, Any]
    decision: dict[str, Any]
    execution: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None
    error_code: int | None = None


class AgentRunResult(BaseModel):
    provider: ProviderName
    model: str
    goal: str
    status: Literal["acted", "done", "takeover", "approval_required", "error", "max_steps_reached"]
    steps: list[AgentStepResult]
    final_session: dict[str, Any]


class ApprovalRecord(BaseModel):
    id: str
    session_id: str
    kind: ApprovalKind
    status: ApprovalStatus
    created_at: str
    updated_at: str
    reason: str
    action: BrowserActionDecision
    observation: dict[str, Any] | None = None
    decision_comment: str | None = None
    decided_at: str | None = None
    approved_expires_at: str | None = None
    executed_at: str | None = None


class ApprovalDecisionRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class OperatorIdentity(BaseModel):
    id: str
    name: str | None = None
    source: str = "anonymous"


class WitnessRemoteState(BaseModel):
    configured: bool = False
    required: bool = False
    tenant_id: str | None = None
    status: WitnessRemoteStatus = "disabled"
    last_error: str | None = None
    last_checked_at: str | None = None
    last_attempted_at: str | None = None
    last_delivered_at: str | None = None


class SessionRecord(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    status: SessionStatus
    live: bool = False
    current_url: str
    title: str
    artifact_dir: str
    takeover_url: str
    remote_access: dict[str, Any]
    isolation: dict[str, Any] = Field(default_factory=dict)
    auth_state: dict[str, Any] = Field(default_factory=dict)
    downloads: list[dict[str, Any]] = Field(default_factory=list)
    last_action: str | None = None
    trace_path: str | None = None
    protection_mode: ProtectionMode = "normal"
    witness_remote: WitnessRemoteState = Field(default_factory=WitnessRemoteState)


class AgentJobRecord(BaseModel):
    id: str
    session_id: str
    kind: AgentJobKind
    status: AgentJobStatus
    created_at: str
    updated_at: str
    request: dict[str, Any]
    operator: OperatorIdentity | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class AuditEvent(BaseModel):
    id: str
    timestamp: str
    event_type: str
    status: str
    action: str | None = None
    session_id: str | None = None
    approval_id: str | None = None
    job_id: str | None = None
    operator: OperatorIdentity
    details: dict[str, Any] = Field(default_factory=dict)


class McpToolDescriptor(BaseModel):
    name: str
    description: str
    inputSchema: dict[str, Any]


class McpToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpToolCallContent(BaseModel):
    type: Literal["text"] = "text"
    text: str


class McpToolCallResponse(BaseModel):
    content: list[McpToolCallContent]
    structuredContent: Any | None = None
    isError: bool = False


class SocialScrollRequest(BaseModel):
    direction: Literal["down", "up"] = "down"
    screens: int = Field(default=3, ge=1, le=20)


class SocialScrapeRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class SocialPostRequest(_WithApproval):
    text: str = Field(min_length=1, max_length=5000)


class SocialCommentRequest(_WithApproval):
    text: str = Field(min_length=1, max_length=5000)
    post_index: int = Field(default=0, ge=0, le=50)


class SocialLikeRequest(_WithApproval):
    post_index: int = Field(default=0, ge=0, le=50)


class SocialFollowRequest(_WithApproval):
    pass


class SocialUnfollowRequest(_WithApproval):
    pass


class SocialRepostRequest(_WithApproval):
    post_index: int = Field(default=0, ge=0, le=50)


class SocialDmRequest(_WithApproval):
    recipient: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)


class SocialLoginRequest(_WithApproval):
    platform: Literal["x", "twitter", "instagram", "linkedin", "outlook", "microsoft", "live"]
    username: str = Field(min_length=1, max_length=500)
    password: str = Field(min_length=1, max_length=5000, repr=False)
    auth_profile: str | None = Field(default=None, max_length=120)
    totp_secret: str | None = Field(default=None, max_length=500, repr=False)


class SocialSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class SocialScrapeCommentsRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


BROWSER_ACTION_SCHEMA = BrowserActionDecision.model_json_schema()
