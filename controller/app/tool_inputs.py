"""
tool_inputs.py — Pydantic input models for McpToolGateway tool handlers.

Kept in a separate module so tool_gateway.py stays focused on dispatch
logic rather than schema definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import AgentRunRequest, AgentStepRequest, BrowserActionDecision, CreateSessionRequest

__all__ = [
    "AgentJobIdInput",
    "AgentRunRequest",
    "AgentStepRequest",
    "ApprovalDecisionInput",
    "ApprovalIdInput",
    "AuthProfileNameInput",
    "CdpAttachInput",
    "CreateCronJobInput",
    "CreateProxyPersonaInput",
    "CreateSessionRequest",
    "CronJobIdInput",
    "DragDropInput",
    "EmptyInput",
    "EvalJsInput",
    "ExecuteActionInput",
    "ExportScriptInput",
    "FindElementsInput",
    "ForkCdpInput",
    "ForkSessionInput",
    "GetCookiesInput",
    "GetNetworkLogInput",
    "GetPageHtmlInput",
    "GetRemoteAccessInput",
    "GetStorageInput",
    "ListAgentJobsInput",
    "ListApprovalsInput",
    "ListAuthProfilesInput",
    "ListDownloadsInput",
    "ListTabsInput",
    "ObserveInput",
    "ProxyPersonaNameInput",
    "QueueAgentRunInput",
    "QueueAgentStepInput",
    "SaveAuthProfileInput",
    "SaveAuthStateInput",
    "ScreenshotInput",
    "SessionIdInput",
    "SessionTailInput",
    "SetCookiesInput",
    "SetStorageInput",
    "SetViewportInput",
    "ShadowBrowseInput",
    "ShareSessionInput",
    "SocialCommentInput",
    "SocialDmInput",
    "SocialFollowInput",
    "SocialLikeInput",
    "SocialLoginInput",
    "SocialPostInput",
    "SocialRepostInput",
    "SocialScrapeInput",
    "SocialScrollInput",
    "SocialSearchInput",
    "SocialUnfollowInput",
    "TabActionInput",
    "TakeoverInput",
    "TriggerCronJobInput",
    "ValidateShareTokenInput",
    "VisionFindInput",
]


class EmptyInput(BaseModel):
    pass


class SessionIdInput(BaseModel):
    session_id: str


class ObserveInput(SessionIdInput):
    limit: int = Field(default=40, ge=1, le=100)


class SessionTailInput(SessionIdInput):
    limit: int = Field(default=20, ge=1, le=100)


class ScreenshotInput(SessionIdInput):
    label: str = Field(default="manual", min_length=1, max_length=120)


class ExecuteActionInput(SessionIdInput):
    approval_id: str | None = None
    action: BrowserActionDecision


class SaveAuthStateInput(SessionIdInput):
    path: str


class SaveAuthProfileInput(SessionIdInput):
    profile_name: str = Field(min_length=1, max_length=120)


class TakeoverInput(SessionIdInput):
    reason: str = "Manual review requested"


class ListDownloadsInput(SessionIdInput):
    pass


class AuthProfileNameInput(BaseModel):
    profile_name: str = Field(min_length=1, max_length=120)


class ListAuthProfilesInput(BaseModel):
    pass


class ListTabsInput(SessionIdInput):
    pass


class TabActionInput(SessionIdInput):
    index: int = Field(ge=0)


class ApprovalIdInput(BaseModel):
    approval_id: str


class ApprovalDecisionInput(ApprovalIdInput):
    comment: str | None = Field(default=None, max_length=2000)


class ListApprovalsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class ListAgentJobsInput(BaseModel):
    status: str | None = None
    session_id: str | None = None


class GetRemoteAccessInput(BaseModel):
    session_id: str | None = None


class AgentJobIdInput(BaseModel):
    job_id: str


class QueueAgentStepInput(SessionIdInput):
    request: AgentStepRequest


class QueueAgentRunInput(SessionIdInput):
    request: AgentRunRequest


class SocialScrollInput(SessionIdInput):
    direction: Literal["down", "up"] = "down"
    screens: int = Field(default=3, ge=1, le=20)


class SocialScrapeInput(SessionIdInput):
    limit: int = Field(default=20, ge=1, le=100)


class SocialPostInput(SessionIdInput):
    text: str = Field(min_length=1, max_length=5000)
    approval_id: str | None = None


class SocialCommentInput(SessionIdInput):
    text: str = Field(min_length=1, max_length=5000)
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialLikeInput(SessionIdInput):
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialFollowInput(SessionIdInput):
    approval_id: str | None = None


class SocialUnfollowInput(SessionIdInput):
    approval_id: str | None = None


class SocialRepostInput(SessionIdInput):
    post_index: int = Field(default=0, ge=0, le=50)
    approval_id: str | None = None


class SocialDmInput(SessionIdInput):
    recipient: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    approval_id: str | None = None


class SocialLoginInput(SessionIdInput):
    platform: Literal["x", "twitter", "instagram", "linkedin", "outlook", "microsoft", "live"]
    username: str = Field(min_length=1, max_length=500)
    password: str = Field(min_length=1, max_length=5000, repr=False)
    auth_profile: str | None = Field(default=None, max_length=120)
    approval_id: str | None = None
    totp_secret: str | None = Field(default=None, repr=False)


class SocialSearchInput(SessionIdInput):
    query: str = Field(min_length=1, max_length=500)


class GetNetworkLogInput(SessionIdInput):
    limit: int = Field(default=100, ge=1, le=1000)
    method: str | None = Field(default=None, max_length=10)
    url_contains: str | None = Field(default=None, max_length=500)


class ForkSessionInput(SessionIdInput):
    name: str | None = Field(default=None, max_length=200)
    start_url: str | None = Field(default=None, max_length=2000)


class EvalJsInput(SessionIdInput):
    expression: str = Field(min_length=1, max_length=50000)


class WaitForSelectorInput(SessionIdInput):
    selector: str = Field(min_length=1, max_length=2000)
    timeout_ms: int = Field(default=10000, ge=100, le=60000)
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"


class GetCookiesInput(SessionIdInput):
    urls: list[str] | None = Field(default=None)


class SetCookiesInput(SessionIdInput):
    cookies: list[dict[str, Any]]


class GetStorageInput(SessionIdInput):
    storage_type: Literal["local", "session"] = "local"
    key: str | None = Field(default=None, max_length=500)


class SetStorageInput(SessionIdInput):
    storage_type: Literal["local", "session"] = "local"
    key: str = Field(min_length=1, max_length=500)
    value: str = Field(max_length=100000)


class SetViewportInput(SessionIdInput):
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=240, le=2160)


class FindElementsInput(SessionIdInput):
    selector: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=20, ge=1, le=100)


class DragDropInput(SessionIdInput):
    source_selector: str | None = Field(default=None, max_length=2000)
    source_x: float | None = None
    source_y: float | None = None
    target_selector: str | None = Field(default=None, max_length=2000)
    target_x: float | None = None
    target_y: float | None = None


class ExportScriptInput(SessionIdInput):
    pass


class CdpAttachInput(BaseModel):
    cdp_url: str = Field(min_length=1, max_length=500)


class ForkCdpInput(BaseModel):
    cdp_url: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=200)
    start_url: str | None = Field(default=None, max_length=2000)


class VisionFindInput(SessionIdInput):
    description: str = Field(min_length=1, max_length=500)
    take_screenshot: bool = True


class ShareSessionInput(SessionIdInput):
    ttl_minutes: int = Field(default=60, ge=1, le=1440)


class ValidateShareTokenInput(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class ShadowBrowseInput(SessionIdInput):
    pass


class ProxyPersonaNameInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateProxyPersonaInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    server: str = Field(min_length=1, max_length=500)
    username: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=500, repr=False)
    description: str = Field(default="", max_length=500)


class CronJobIdInput(BaseModel):
    job_id: str = Field(min_length=1, max_length=50)


class CreateCronJobInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=5000)
    schedule: str | None = Field(default=None, max_length=100)
    start_url: str | None = Field(default=None, max_length=2000)
    auth_profile: str | None = Field(default=None, max_length=200)
    proxy_persona: str | None = Field(default=None, max_length=200)
    max_steps: int = Field(default=20, ge=1, le=100)
    enabled: bool = True
    webhook_enabled: bool = False


class TriggerCronJobInput(CronJobIdInput):
    webhook_key: str | None = Field(default=None, max_length=200)


class GetPageHtmlInput(SessionIdInput):
    full_page: bool = False
    text_only: bool = False
