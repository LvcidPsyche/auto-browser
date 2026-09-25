"""Sitemap Manager - Main interface for site memory.

Provides high-level operations:
- Load memory for a URL
- Auto-learn from browser actions
- Inject context into agent prompts
"""

import uuid
from urllib.parse import urlparse
from typing import Optional
from datetime import datetime

from .models import SiteMemory, PageStructure, ApiEndpoint, NavigationPath, Candidate
from .store import SitemapStore


class SitemapManager:
    """High-level sitemap memory management."""

    def __init__(self, store: SitemapStore | None = None):
        self.store = store or SitemapStore()

    def get_context(self, url: str) -> Optional[str]:
        """Get site memory context for a URL, formatted for LLM injection."""
        hostname = urlparse(url).hostname
        if not hostname:
            return None

        memory = self.store.get(hostname)
        if not memory:
            return None

        return memory.to_context()

    def get_memory(self, hostname: str) -> Optional[SiteMemory]:
        """Get raw site memory."""
        return self.store.get(hostname)

    def list_sites(self) -> list[str]:
        """List all sites with stored memory."""
        return self.store.list_sites()

    # Learning operations

    def learn_page(
        self,
        url: str,
        selectors: dict[str, str],
        form_fields: list[dict] | None = None,
    ) -> PageStructure:
        """Record a page structure."""
        hostname = urlparse(url).hostname
        path = urlparse(url).path or "/"

        # Generalize path to pattern
        pattern = self._generalize_path(path)

        page = PageStructure(
            url_pattern=pattern,
            selectors=selectors,
            form_fields=form_fields or [],
        )

        self.store.add_page(hostname, page)
        return page

    def learn_endpoint(
        self,
        url: str,
        name: str,
        method: str = "GET",
        params: dict | None = None,
        response_path: str | None = None,
        auth_required: bool = False,
        notes: str | None = None,
    ) -> ApiEndpoint:
        """Record an API endpoint."""
        parsed = urlparse(url)
        hostname = parsed.hostname

        endpoint = ApiEndpoint(
            name=name,
            url=f"{parsed.path}{'?' + parsed.query if parsed.query else ''}",
            method=method,
            params=params,
            response_path=response_path,
            auth_required=auth_required,
            notes=notes,
        )

        self.store.add_endpoint(hostname, endpoint)
        return endpoint

    def learn_navigation(
        self,
        hostname: str,
        name: str,
        steps: list[dict],
        start_url: str,
        end_url: str,
    ) -> NavigationPath:
        """Record a navigation path."""
        nav = NavigationPath(
            name=name,
            steps=steps,
            start_url=start_url,
            end_url=end_url,
        )

        memory = self.store.get(hostname) or SiteMemory(hostname=hostname)

        # Update existing or append
        for i, p in enumerate(memory.navigation_paths):
            if p.name == name:
                memory.navigation_paths[i] = nav
                self.store.save(memory)
                return nav

        memory.navigation_paths.append(nav)
        self.store.save(memory)
        return nav

    def add_note(self, hostname: str, text: str, author: str = "agent") -> None:
        """Add a freeform note about a site."""
        self.store.add_note(hostname, text, author)

    def mark_endpoint_stale(self, hostname: str, endpoint_name: str) -> None:
        """Mark an endpoint as potentially outdated."""
        self.store.mark_stale(hostname, endpoint_name)

    # Candidate observations

    def observe(
        self,
        hostname: str,
        kind: str,
        claim: str,
        evidence: str,
        consequence: str,
    ) -> Candidate:
        """Record an observation for later review."""
        candidate = Candidate(
            id=f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
            kind=kind,
            claim=claim,
            evidence=evidence,
            consequence=consequence,
        )

        self.store.add_candidate(hostname, candidate)
        return candidate

    def list_candidates(self, hostname: str, status: str | None = None) -> list[Candidate]:
        """List pending observations."""
        return self.store.list_candidates(hostname, status)

    def review_candidate(
        self,
        hostname: str,
        candidate_id: str,
        accept: bool,
        rejection_reason: str | None = None,
    ) -> None:
        """Accept or reject a candidate observation."""
        self.store.review_candidate(hostname, candidate_id, accept, rejection_reason)

    # Utility

    def _generalize_path(self, path: str) -> str:
        """Convert specific path to pattern (e.g., /users/123 → /users/*)."""
        import re
        # Replace numeric IDs with *
        pattern = re.sub(r'/\d+', '/*', path)
        # Replace UUIDs with *
        pattern = re.sub(r'/[a-f0-9-]{36}', '/*', pattern, flags=re.I)
        return pattern

    def delete(self, hostname: str) -> bool:
        """Delete all memory for a site."""
        return self.store.delete(hostname)
