"""MCP Tools for Sitemap Memory.

Exposes sitemap operations as browser tools.
"""

from typing import Literal
from pydantic import BaseModel, Field

from .manager import SitemapManager
from .models import SiteMemory


# Input models for MCP tools

class SitemapContextInput(BaseModel):
    """Load site memory for a URL."""
    url: str = Field(description="URL to load memory for")


class SitemapShowInput(BaseModel):
    """Show all memory for a site."""
    hostname: str = Field(description="Site hostname, e.g. 'github.com'")


class SitemapNoteInput(BaseModel):
    """Add a note about a site."""
    hostname: str
    text: str = Field(description="Note content (markdown supported)")
    author: str = "agent"


class SitemapSelectorInput(BaseModel):
    """Record a page selector."""
    url: str = Field(description="Page URL")
    selectors: dict[str, str] = Field(description="Named selectors: {'login_btn': '#submit'}")
    form_fields: list[dict] | None = None


class SitemapEndpointInput(BaseModel):
    """Record an API endpoint."""
    url: str = Field(description="Full endpoint URL")
    name: str = Field(description="Endpoint name, e.g. 'search'")
    method: str = "GET"
    params: dict | None = None
    response_path: str | None = Field(None, description="JSONPath to data array")
    auth_required: bool = False
    notes: str | None = None


class SitemapObserveInput(BaseModel):
    """Record an observation for review."""
    hostname: str
    kind: Literal["action_space", "better_path", "access", "high_consequence", "repeated_mistake"]
    claim: str = Field(description="What was observed")
    evidence: str = Field(description="Supporting details")
    consequence: str = Field(description="Why this matters")


class SitemapDeleteInput(BaseModel):
    """Delete all memory for a site."""
    hostname: str
    confirm: bool = Field(description="Must be true to delete")


class SitemapToolHandler:
    """Handler for sitemap MCP tools."""

    def __init__(self, manager: SitemapManager | None = None):
        self.manager = manager or SitemapManager()

    async def context(self, input: SitemapContextInput) -> dict:
        """Load site memory context for a URL."""
        context = self.manager.get_context(input.url)
        if context:
            return {"status": "found", "context": context}
        return {"status": "not_found", "context": None}

    async def show(self, input: SitemapShowInput) -> dict:
        """Show all memory for a site."""
        memory = self.manager.get_memory(input.hostname)
        if not memory:
            return {"status": "not_found"}

        return {
            "status": "found",
            "hostname": memory.hostname,
            "pages": len(memory.pages),
            "endpoints": len(memory.endpoints),
            "navigation_paths": len(memory.navigation_paths),
            "notes": len(memory.notes),
            "memory": memory.model_dump(),
        }

    async def list_sites(self) -> dict:
        """List all sites with stored memory."""
        sites = self.manager.list_sites()
        return {"sites": sites, "count": len(sites)}

    async def note(self, input: SitemapNoteInput) -> dict:
        """Add a note about a site."""
        self.manager.add_note(input.hostname, input.text, input.author)
        return {"status": "added"}

    async def selector(self, input: SitemapSelectorInput) -> dict:
        """Record page selectors."""
        page = self.manager.learn_page(
            input.url,
            input.selectors,
            input.form_fields,
        )
        return {"status": "recorded", "pattern": page.url_pattern}

    async def endpoint(self, input: SitemapEndpointInput) -> dict:
        """Record an API endpoint."""
        ep = self.manager.learn_endpoint(
            input.url,
            input.name,
            input.method,
            input.params,
            input.response_path,
            input.auth_required,
            input.notes,
        )
        return {"status": "recorded", "endpoint": ep.name}

    async def observe(self, input: SitemapObserveInput) -> dict:
        """Record an observation for review."""
        candidate = self.manager.observe(
            input.hostname,
            input.kind,
            input.claim,
            input.evidence,
            input.consequence,
        )
        return {"status": "recorded", "id": candidate.id}

    async def delete(self, input: SitemapDeleteInput) -> dict:
        """Delete all memory for a site."""
        if not input.confirm:
            return {"status": "error", "message": "Must set confirm=true"}

        deleted = self.manager.delete(input.hostname)
        return {"status": "deleted" if deleted else "not_found"}


def get_tool_specs() -> list[dict]:
    """Return MCP tool specifications for registration."""
    return [
        {
            "name": "browser.sitemap_context",
            "description": "Load site memory for a URL. Returns cached knowledge about page structure, selectors, and APIs.",
            "input_model": SitemapContextInput,
            "read_only": True,
        },
        {
            "name": "browser.sitemap_show",
            "description": "Show all stored memory for a site.",
            "input_model": SitemapShowInput,
            "read_only": True,
        },
        {
            "name": "browser.sitemap_list",
            "description": "List all sites with stored memory.",
            "input_model": None,
            "read_only": True,
        },
        {
            "name": "browser.sitemap_note",
            "description": "Add a freeform note about a site.",
            "input_model": SitemapNoteInput,
            "governed": True,
        },
        {
            "name": "browser.sitemap_selector",
            "description": "Record verified page selectors for reuse.",
            "input_model": SitemapSelectorInput,
            "governed": True,
        },
        {
            "name": "browser.sitemap_endpoint",
            "description": "Record a verified API endpoint.",
            "input_model": SitemapEndpointInput,
            "governed": True,
        },
        {
            "name": "browser.sitemap_observe",
            "description": "Record an observation for later review (action discovered, auth required, etc).",
            "input_model": SitemapObserveInput,
            "governed": True,
        },
        {
            "name": "browser.sitemap_delete",
            "description": "Delete all memory for a site. Requires confirm=true.",
            "input_model": SitemapDeleteInput,
            "destructive": True,
        },
    ]
