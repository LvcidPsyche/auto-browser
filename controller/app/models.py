from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateSessionRequest(BaseModel):
    name: str | None = None
    start_url: str | None = None
    storage_state_path: str | None = None


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


class PressRequest(BaseModel):
    key: str


class ScrollRequest(BaseModel):
    delta_x: float = 0
    delta_y: float = 600


class NavigateRequest(BaseModel):
    url: str


class UploadRequest(BaseModel):
    selector: str | None = None
    element_id: str | None = None
    file_path: str
    approved: bool = False


class SaveStorageStateRequest(BaseModel):
    path: str = Field(description="Relative path inside /data/auth")


class HumanTakeoverRequest(BaseModel):
    reason: str = "Manual review requested"


class SessionEnvelope(BaseModel):
    session: dict[str, Any]


class ActionEnvelope(BaseModel):
    action: str
    session: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]
    target: dict[str, Any]


ActionName = Literal[
    "navigate",
    "click",
    "type",
    "press",
    "scroll",
    "upload",
    "request_human_takeover",
    "done",
]
ProviderName = Literal["openai", "claude", "gemini"]


class BrowserActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ActionName
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    element_id: str | None = None
    selector: str | None = None
    x: float | None = None
    y: float | None = None
    text: str | None = None
    clear_first: bool = True
    key: str | None = None
    delta_x: float = 0
    delta_y: float = 600
    url: str | None = None
    file_path: str | None = None

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "BrowserActionDecision":
        has_click_target = bool(self.element_id or self.selector or (self.x is not None and self.y is not None))
        has_locator_target = bool(self.element_id or self.selector)

        if self.action == "click" and not has_click_target:
            raise ValueError("click requires element_id, selector, or x+y coordinates")
        if self.action == "type":
            if not has_locator_target:
                raise ValueError("type requires element_id or selector")
            if not self.text:
                raise ValueError("type requires text")
        if self.action == "press" and not self.key:
            raise ValueError("press requires key")
        if self.action == "navigate" and not self.url:
            raise ValueError("navigate requires url")
        if self.action == "upload":
            if not has_locator_target:
                raise ValueError("upload requires element_id or selector")
            if not self.file_path:
                raise ValueError("upload requires file_path")
        if self.action in {"done", "request_human_takeover"}:
            return self
        return self


class AgentStepRequest(BaseModel):
    provider: ProviderName
    goal: str = Field(min_length=1, max_length=4000)
    provider_model: str | None = None
    observation_limit: int = Field(default=40, ge=1, le=100)
    context_hints: str | None = Field(default=None, max_length=4000)
    upload_approved: bool = False


class AgentRunRequest(AgentStepRequest):
    max_steps: int = Field(default=6, ge=1, le=20)


class ProviderInfo(BaseModel):
    provider: ProviderName
    configured: bool
    model: str | None = None
    detail: str | None = None


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
    status: Literal["acted", "done", "takeover", "error"]
    observation: dict[str, Any]
    decision: dict[str, Any]
    execution: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None


class AgentRunResult(BaseModel):
    provider: ProviderName
    model: str
    goal: str
    status: Literal["acted", "done", "takeover", "error", "max_steps_reached"]
    steps: list[AgentStepResult]
    final_session: dict[str, Any]


BROWSER_ACTION_SCHEMA = BrowserActionDecision.model_json_schema()
