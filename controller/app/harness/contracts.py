from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..url_safety import refuse_ambiguous_host

PostconditionKind = Literal[
    "url_contains",
    "url_matches",
    "text_contains",
    "dom_contains",
    "network_response_shape",
    "extracted_data_schema",
]
ForbiddenStateKind = Literal[
    "url_contains",
    "url_matches",
    "text_contains",
    "url_status",
    "captcha_screen",
    "payment_screen",
    "login_redirect",
]
EvidenceKind = Literal["trace", "actions", "screenshots", "network", "console", "model_decisions"]


class HarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Precondition(HarnessModel):
    kind: Literal["start_url", "auth_profile", "fixture", "note"]
    value: str = Field(min_length=1, max_length=2000)
    required: bool = True

    @model_validator(mode="after")
    def validate_start_url(self) -> "Precondition":
        # The run loads it as a browser reads it; the approval shows it as urllib does.
        if self.kind == "start_url":
            refuse_ambiguous_host(self.value, field_name="start_url")
        return self


class Postcondition(HarnessModel):
    kind: PostconditionKind
    value: str = Field(min_length=1, max_length=4000)
    description: str = Field(default="", max_length=1000)
    required: bool = True
    weight: float = Field(default=1.0, ge=0.0, le=10.0)

    @model_validator(mode="after")
    def validate_matcher(self) -> "Postcondition":
        if self.kind == "url_matches":
            try:
                re.compile(self.value)
            except re.error as err:
                raise ValueError(f"url_matches value must be a valid regex: {err}") from None
        return self


class ForbiddenState(HarnessModel):
    kind: ForbiddenStateKind
    value: str = Field(default="", max_length=2000)
    description: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_value(self) -> "ForbiddenState":
        if self.kind in {"url_contains", "url_matches", "text_contains", "url_status"} and not self.value:
            raise ValueError(f"{self.kind} forbidden state requires value")
        if self.kind == "url_matches":
            try:
                re.compile(self.value)
            except re.error as err:
                raise ValueError(f"url_matches value must be a valid regex: {err}") from None
        return self


class EvidenceRequirement(HarnessModel):
    kind: EvidenceKind
    required: bool = True


class Budget(HarnessModel):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_wall_seconds: int = Field(default=300, ge=1, le=86400)
    max_model_calls: int = Field(default=30, ge=0, le=10000)
    max_usd: float = Field(default=5.0, ge=0.0, le=100000.0)


class TaskContract(HarnessModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    goal: str = Field(min_length=1, max_length=5000)
    task_class: str = Field(default="generic", min_length=1, max_length=120)
    preconditions: list[Precondition] = Field(default_factory=list)
    postconditions: list[Postcondition] = Field(default_factory=list)
    forbidden_states: list[ForbiddenState] = Field(default_factory=list)
    evidence_required: list[EvidenceRequirement] = Field(
        default_factory=lambda: [
            EvidenceRequirement(kind="trace"),
            EvidenceRequirement(kind="actions"),
            EvidenceRequirement(kind="screenshots", required=False),
        ]
    )
    budget: Budget = Field(default_factory=Budget)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> "TaskContract":
        if not self.postconditions:
            raise ValueError("task contract requires at least one postcondition")
        return self

    def canonical_json(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def start_url(self) -> str | None:
        for precondition in self.preconditions:
            if precondition.kind == "start_url":
                return precondition.value
        return None

    @property
    def required_evidence(self) -> set[str]:
        return {item.kind for item in self.evidence_required if item.required}

    @property
    def required_evidence_kinds(self) -> set[str]:
        return {item.kind for item in self.evidence_required if item.required}
