"""Sitemap Memory Data Models.

Stores learned knowledge about websites:
- Page structures (selectors, forms)
- API endpoints
- Navigation paths
- Observations (candidates)
"""

from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime


class PageStructure(BaseModel):
    """Learned structure of a page type."""
    url_pattern: str = Field(description="URL pattern, e.g. '/login', '/users/*'")
    selectors: dict[str, str] = Field(default_factory=dict, description="Named selectors: {'login_btn': '#submit'}")
    form_fields: list[dict] = Field(default_factory=list, description="Form field metadata")
    dynamic_regions: list[str] = Field(default_factory=list, description="Selectors for regions that change")
    verified_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ApiEndpoint(BaseModel):
    """Discovered API endpoint."""
    name: str = Field(description="Endpoint name, e.g. 'search', 'login'")
    url: str
    method: str = "GET"
    params: dict | None = None
    response_path: str | None = Field(None, description="JSONPath to data array")
    sample_fields: list[str] | None = None
    auth_required: bool = False
    notes: str | None = None
    verified_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    stale: bool = False


class NavigationPath(BaseModel):
    """A learned route between pages."""
    name: str = Field(description="Path name, e.g. 'login_flow', 'checkout'")
    steps: list[dict] = Field(description="[{action: 'click', selector: '#login'}, ...]")
    start_url: str
    end_url: str
    verified_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    success_rate: float = 1.0


class Candidate(BaseModel):
    """An observation pending review."""
    id: str
    kind: Literal["action_space", "better_path", "access", "high_consequence", "repeated_mistake"]
    claim: str = Field(description="What was observed")
    evidence: str = Field(description="Supporting details")
    consequence: str = Field(description="Why this matters")
    status: Literal["pending", "accepted", "rejected"] = "pending"
    observed_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    reviewed_at: str | None = None
    rejection_reason: str | None = None


class SiteMemory(BaseModel):
    """Complete memory for one site."""
    schema_version: int = 1
    hostname: str = Field(description="Site hostname, e.g. 'github.com'")
    display_name: str | None = None

    # Learned knowledge
    pages: list[PageStructure] = Field(default_factory=list)
    endpoints: list[ApiEndpoint] = Field(default_factory=list)
    navigation_paths: list[NavigationPath] = Field(default_factory=list)

    # Freeform notes
    notes: list[dict] = Field(default_factory=list, description="[{date, author, text}]")
    field_mappings: dict[str, str] = Field(default_factory=dict, description="{'p': 'price in cents'}")

    # Auth patterns
    login_selectors: dict[str, str] = Field(default_factory=dict)
    session_indicators: list[str] = Field(default_factory=list)

    # High-risk areas
    sensitive_patterns: list[str] = Field(default_factory=list, description="URLs requiring human takeover")

    # Metadata
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_context(self, max_items: int = 10) -> str:
        """Format memory for LLM context injection."""
        parts = [f"[Site Memory: {self.hostname}]"]

        if self.pages:
            selectors = "\n".join(
                f"  {p.url_pattern}: {p.selectors}"
                for p in self.pages[:max_items]
            )
            parts.append(f"Known pages:\n{selectors}")

        if self.endpoints:
            endpoints = "\n".join(
                f"  {e.name}: {e.method} {e.url}"
                for e in self.endpoints[:max_items] if not e.stale
            )
            parts.append(f"API endpoints:\n{endpoints}")

        if self.navigation_paths:
            paths = "\n".join(
                f"  {p.name}: {len(p.steps)} steps ({p.start_url} → {p.end_url})"
                for p in self.navigation_paths[:5]
            )
            parts.append(f"Navigation paths:\n{paths}")

        if self.login_selectors:
            parts.append(f"Login selectors: {self.login_selectors}")

        if self.sensitive_patterns:
            parts.append(f"Sensitive URLs (require approval): {self.sensitive_patterns}")

        return "\n\n".join(parts)
