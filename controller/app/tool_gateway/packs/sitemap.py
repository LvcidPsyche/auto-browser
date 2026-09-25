"""Sitemap Memory tools — persistent site knowledge for agents."""

from __future__ import annotations

from ...sitemap import SitemapManager
from ...sitemap.tools import (
    SitemapContextInput,
    SitemapDeleteInput,
    SitemapEndpointInput,
    SitemapNoteInput,
    SitemapObserveInput,
    SitemapSelectorInput,
    SitemapShowInput,
)
from ...tool_inputs import EmptyInput
from ..registry import ToolSpec

_manager = None


def _get_manager() -> SitemapManager:
    global _manager
    if _manager is None:
        _manager = SitemapManager()
    return _manager


async def _sitemap_context(payload: SitemapContextInput) -> dict:
    mgr = _get_manager()
    ctx = mgr.get_context(payload.url)
    return {"status": "found" if ctx else "not_found", "context": ctx}


async def _sitemap_show(payload: SitemapShowInput) -> dict:
    mgr = _get_manager()
    mem = mgr.get_memory(payload.hostname)
    if not mem:
        return {"status": "not_found"}
    return {
        "status": "found",
        "hostname": mem.hostname,
        "pages": len(mem.pages),
        "endpoints": len(mem.endpoints),
        "navigation_paths": len(mem.navigation_paths),
        "notes": len(mem.notes),
        "memory": mem.model_dump(),
    }


async def _sitemap_list(_: EmptyInput) -> dict:
    sites = _get_manager().list_sites()
    return {"sites": sites, "count": len(sites)}


async def _sitemap_note(payload: SitemapNoteInput) -> dict:
    _get_manager().add_note(payload.hostname, payload.text, payload.author)
    return {"status": "added"}


async def _sitemap_selector(payload: SitemapSelectorInput) -> dict:
    page = _get_manager().learn_page(payload.url, payload.selectors, payload.form_fields)
    return {"status": "recorded", "pattern": page.url_pattern}


async def _sitemap_endpoint(payload: SitemapEndpointInput) -> dict:
    ep = _get_manager().learn_endpoint(
        payload.url,
        payload.name,
        payload.method,
        payload.params,
        payload.response_path,
        payload.auth_required,
        payload.notes,
    )
    return {"status": "recorded", "endpoint": ep.name}


async def _sitemap_observe(payload: SitemapObserveInput) -> dict:
    cand = _get_manager().observe(
        payload.hostname,
        payload.kind,
        payload.claim,
        payload.evidence,
        payload.consequence,
    )
    return {"status": "recorded", "id": cand.id}


async def _sitemap_delete(payload: SitemapDeleteInput) -> dict:
    if not payload.confirm:
        return {"status": "error", "message": "Must set confirm=true"}
    deleted = _get_manager().delete(payload.hostname)
    return {"status": "deleted" if deleted else "not_found"}


def register(registry, gateway):
    for spec in [
        ToolSpec(
            name="browser.sitemap_context",
            description="Load cached site memory for a URL. Returns learned selectors, APIs, notes.",
            input_model=SitemapContextInput,
            handler=_sitemap_context,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="browser.sitemap_show",
            description="Show all stored memory for a site.",
            input_model=SitemapShowInput,
            handler=_sitemap_show,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="browser.sitemap_list",
            description="List all sites with stored memory.",
            input_model=EmptyInput,
            handler=_sitemap_list,
            profiles=("curated", "full"),
        ),
        ToolSpec(
            name="browser.sitemap_note",
            description="Add a freeform note about a site (rate limits, quirks, tips).",
            input_model=SitemapNoteInput,
            handler=_sitemap_note,
            profiles=("curated", "full"),
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.sitemap_selector",
            description="Record verified page selectors for reuse on future visits.",
            input_model=SitemapSelectorInput,
            handler=_sitemap_selector,
            profiles=("curated", "full"),
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.sitemap_endpoint",
            description="Record a verified API endpoint.",
            input_model=SitemapEndpointInput,
            handler=_sitemap_endpoint,
            profiles=("curated", "full"),
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.sitemap_observe",
            description="Record an observation for human review (auth needed, better path, etc).",
            input_model=SitemapObserveInput,
            handler=_sitemap_observe,
            profiles=("curated", "full"),
            governed_kind="write",
        ),
        ToolSpec(
            name="browser.sitemap_delete",
            description="Delete all memory for a site. Requires confirm=true.",
            input_model=SitemapDeleteInput,
            handler=_sitemap_delete,
            profiles=("full",),
            governed_kind="delete",
        ),
    ]:
        registry.register(spec)
