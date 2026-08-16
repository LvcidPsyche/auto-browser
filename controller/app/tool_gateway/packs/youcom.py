"""You.com web search pack for MCP tools."""

from __future__ import annotations

from ...tool_inputs import YouContentsInput, YouSearchInput
from ..registry import ToolSpec


def register(registry, gateway):
    """Register You.com search tools."""
    for spec in [
        ToolSpec(
            name="youcom.search",
            description=(
                "Search the web using You.com's search API. Provides web search results "
                "with URLs, titles, and descriptions. Supports different search types "
                "(web, news, images, videos), safe search filtering, and pagination. "
                "Requires YDC_API_KEY environment variable to be set for full functionality."
            ),
            input_model=YouSearchInput,
            handler=gateway._youcom_search,
        ),
        ToolSpec(
            name="youcom.contents",
            description=(
                "Extract text content from a specific URL using You.com's contents API. "
                "Returns the main text content of a webpage, useful for getting full "
                "article text or detailed information from search results. "
                "Requires YDC_API_KEY environment variable to be set for full functionality."
            ),
            input_model=YouContentsInput,
            handler=gateway._youcom_contents,
        ),
    ]:
        registry.register(spec)