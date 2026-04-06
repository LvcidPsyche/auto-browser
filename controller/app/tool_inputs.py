"""
tool_inputs.py — Pydantic input models for McpToolGateway tool handlers.

Kept in a separate module so tool_gateway.py stays focused on dispatch
logic rather than schema definitions.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    AgentJobStatus,
    AgentRunRequest,
    AgentStepRequest,
    ApprovalStatus,
    BrowserActionDecision,
    CDP_URL_SCHEMES,
    CreateSessionRequest,
    HTTP_URL_SCHEMES,
    PROXY_URL_SCHEMES,
    PerceptionPreset,
    ProviderName,
    StrictInputModel,
    validate_coordinate_pair,
    validate_url,
)

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
    "WaitForSelectorInput",
]


class EmptyInput(StrictInputModel):
    pass


class SessionIdInput(StrictInputModel):
    session_id: str = Field(min_length=1, max_length=120)


class ObserveInput(SessionIdInput):
    preset: PerceptionPreset = "normal"
    limit: int = Field(default=40, ge=1, le=200)


class SessionTailInput(SessionIdInput):
    limit: int = Field(default=20, ge=1, le=100)


class ScreenshotInput(SessionIdInput):
    label: str = Field(default="manual", min_length=1, max_length=120)


class ExecuteActionInput(SessionIdInput):
    approval_id: str | None = None
    action: BrowserActionDecision


class SaveAuthStateInput(SessionIdInput):
    path: str = Field(min_length=1, max_length=500)


class SaveAuthProfileInput(SessionIdInput):
    profile_name: str = Field(min_length=1, max_length=120)


class TakeoverInput(SessionIdInput):
    reason: str = "Manual review requested"


class ListDownloadsInput(SessionIdInput):
    pass


class AuthProfileNameInput(StrictInputModel):
    profile_name: str = Field(min_length=1, max_length=120)


class ListAuthProfilesInput(StrictInputModel):
    pass


class ListTabsInput(SessionIdInput):
    pass


class TabActionInput(SessionIdInput):
    index: int = Field(ge=0)


class ApprovalIdInput(StrictInputModel):
    approval_id: str = Field(min_length=1, max_length=120)


class ApprovalDecisionInput(ApprovalIdInput):
    comment: str | None = Field(default=None, max_length=2000)


class ListApprovalsInput(StrictInputModel):
    status: ApprovalStatus | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class ListAgentJobsInput(StrictInputModel):
    status: AgentJobStatus | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class GetRemoteAccessInput(StrictInputModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class AgentJobIdInput(StrictInputModel):
    job_id: str = Field(min_length=1, max_length=120)


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

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        method = value.upper()
        if not method.isalpha():
            raise ValueError("method must contain only letters")
        return method


class ForkSessionInput(SessionIdInput):
    name: str | None = Field(default=None, max_length=200)
    start_url: str | None = Field(default=None, max_length=2000)

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_url(value, field_name="start_url", allowed_schemes=HTTP_URL_SCHEMES)


class EvalJsInput(SessionIdInput):
    expression: str = Field(min_length=1, max_length=50000)


class WaitForSelectorInput(SessionIdInput):
    selector: str = Field(min_length=1, max_length=2000)
    timeout_ms: int = Field(default=10000, ge=100, le=60000)
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"


class GetCookiesInput(SessionIdInput):
    urls: list[str] | None = Field(default=None)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("urls must not be empty when provided")
        return [
            validate_url(item, field_name=f"urls[{index}]", allowed_schemes=HTTP_URL_SCHEMES)
            for index, item in enumerate(value)
        ]


class SetCookiesInput(SessionIdInput):
    cookies: list[dict[str, Any]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_cookies(self) -> "SetCookiesInput":
        for index, cookie in enumerate(self.cookies):
            if not isinstance(cookie, dict):
                raise ValueError(f"cookies[{index}] must be an object")
            name = str(cookie.get("name") or "").strip()
            value = cookie.get("value")
            domain = str(cookie.get("domain") or "").strip()
            url = str(cookie.get("url") or "").strip()
            if not name:
                raise ValueError(f"cookies[{index}] requires name")
            if value is None:
                raise ValueError(f"cookies[{index}] requires value")
            if not domain and not url:
                raise ValueError(f"cookies[{index}] requires domain or url")
            if url:
                validate_url(
                    url,
                    field_name=f"cookies[{index}].url",
                    allowed_schemes=HTTP_URL_SCHEMES,
                )
        return self


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

    @model_validator(mode="after")
    def validate_targets(self) -> "DragDropInput":
        validate_coordinate_pair(self.source_x, self.source_y, field_name="drag source coordinates")
        validate_coordinate_pair(self.target_x, self.target_y, field_name="drag target coordinates")
        if not (self.source_selector or (self.source_x is not None and self.source_y is not None)):
            raise ValueError("drag_drop requires source_selector or source_x+source_y")
        if not (self.target_selector or (self.target_x is not None and self.target_y is not None)):
            raise ValueError("drag_drop requires target_selector or target_x+target_y")
        return self


class ExportScriptInput(SessionIdInput):
    pass


class CdpAttachInput(StrictInputModel):
    cdp_url: str = Field(min_length=1, max_length=500)

    @field_validator("cdp_url")
    @classmethod
    def validate_cdp_url(cls, value: str) -> str:
        return validate_url(value, field_name="cdp_url", allowed_schemes=CDP_URL_SCHEMES)


class ForkCdpInput(StrictInputModel):
    cdp_url: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=200)
    start_url: str | None = Field(default=None, max_length=2000)

    @field_validator("cdp_url")
    @classmethod
    def validate_cdp_url(cls, value: str) -> str:
        return validate_url(value, field_name="cdp_url", allowed_schemes=CDP_URL_SCHEMES)

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_url(value, field_name="start_url", allowed_schemes=HTTP_URL_SCHEMES)


class VisionFindInput(SessionIdInput):
    description: str = Field(min_length=1, max_length=500)
    take_screenshot: bool = True


class ShareSessionInput(SessionIdInput):
    ttl_minutes: int = Field(default=60, ge=1, le=1440)


class ValidateShareTokenInput(StrictInputModel):
    token: str = Field(min_length=1, max_length=500)


class ShadowBrowseInput(SessionIdInput):
    pass


class ProxyPersonaNameInput(StrictInputModel):
    name: str = Field(min_length=1, max_length=200)


class CreateProxyPersonaInput(StrictInputModel):
    name: str = Field(min_length=1, max_length=200)
    server: str = Field(min_length=1, max_length=500)
    username: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=500, repr=False)
    description: str = Field(default="", max_length=500)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        return validate_url(value, field_name="server", allowed_schemes=PROXY_URL_SCHEMES)


class CronJobIdInput(StrictInputModel):
    job_id: str = Field(min_length=1, max_length=50)


class CreateCronJobInput(StrictInputModel):
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=5000)
    provider: ProviderName = "openai"
    schedule: str | None = Field(default=None, max_length=100)
    start_url: str | None = Field(default=None, max_length=2000)
    auth_profile: str | None = Field(default=None, max_length=200)
    proxy_persona: str | None = Field(default=None, max_length=200)
    max_steps: int = Field(default=20, ge=1, le=100)
    enabled: bool = True
    webhook_enabled: bool = False

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_url(value, field_name="start_url", allowed_schemes=HTTP_URL_SCHEMES)

    @model_validator(mode="after")
    def validate_trigger_mode(self) -> "CreateCronJobInput":
        if not self.schedule and not self.webhook_enabled:
            raise ValueError("create_cron_job requires schedule or webhook_enabled=true")
        return self


class TriggerCronJobInput(CronJobIdInput):
    webhook_key: str | None = Field(default=None, max_length=200)


class GetPageHtmlInput(SessionIdInput):
    full_page: bool = False
    text_only: bool = False
