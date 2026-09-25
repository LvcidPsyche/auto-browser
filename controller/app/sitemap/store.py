"""Sitemap Storage Layer.

Persists site memory to disk as JSON files.
Thread-safe with atomic writes.
"""

import json
import os
import re
import tempfile
import shutil
import fcntl
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

from .models import SiteMemory, PageStructure, ApiEndpoint, NavigationPath, Candidate


@contextmanager
def file_lock(lock_path: Path):
    """Simple file-based lock using fcntl."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# Patterns that indicate secrets - reject from storage
SECRET_PATTERNS = [
    re.compile(r'password\s*(?:is|[:=])', re.I),
    re.compile(r'secret\s*(?:is|[:=])', re.I),
    re.compile(r'api[_-]?key\s*(?:is|[:=])', re.I),
    re.compile(r'bearer\s+\S{8,}', re.I),
    re.compile(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.'),  # JWT
    re.compile(r'cookie\s*(?:is|[:=])', re.I),
    re.compile(r'token\s+\S{8,}', re.I),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),  # OpenAI keys
    re.compile(r'sk-ant-[a-zA-Z0-9-]{20,}'),  # Anthropic keys
    re.compile(r'ghp_[a-zA-Z0-9]{30,}'),  # GitHub PAT
]


def reject_secrets(text: str, field: str) -> None:
    """Raise if text contains credential-like patterns."""
    if not text:
        return
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"Sitemap {field} cannot include secret-bearing content")


def sanitize_hostname(hostname: str) -> str:
    """Convert hostname to filesystem-safe key."""
    return hostname.lower().replace(":", "_").replace("/", "_")


class SitemapStore:
    """File-based sitemap storage."""

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            base_dir = Path.home() / ".auto-browser" / "sites"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _site_dir(self, hostname: str) -> Path:
        return self.base_dir / sanitize_hostname(hostname)

    def _lock_path(self, hostname: str) -> Path:
        return self._site_dir(hostname) / ".lock"

    def _atomic_write(self, path: Path, data: dict) -> None:
        """Write JSON atomically via temp file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode='w',
            dir=path.parent,
            suffix='.tmp',
            delete=False
        ) as f:
            json.dump(data, f, indent=2, default=str)
            temp_path = f.name
        shutil.move(temp_path, path)

    def get(self, hostname: str) -> Optional[SiteMemory]:
        """Load site memory."""
        site_dir = self._site_dir(hostname)
        manifest_path = site_dir / "manifest.json"

        if not manifest_path.exists():
            return None

        with file_lock(self._lock_path(hostname)):
            with open(manifest_path) as f:
                data = json.load(f)

            # Load sub-files
            pages_path = site_dir / "pages.json"
            if pages_path.exists():
                with open(pages_path) as f:
                    data["pages"] = json.load(f)

            endpoints_path = site_dir / "endpoints.json"
            if endpoints_path.exists():
                with open(endpoints_path) as f:
                    data["endpoints"] = json.load(f)

            nav_path = site_dir / "navigation.json"
            if nav_path.exists():
                with open(nav_path) as f:
                    data["navigation_paths"] = json.load(f)

            notes_path = site_dir / "notes.json"
            if notes_path.exists():
                with open(notes_path) as f:
                    data["notes"] = json.load(f)

        return SiteMemory(**data)

    def save(self, memory: SiteMemory) -> None:
        """Persist site memory."""
        site_dir = self._site_dir(memory.hostname)

        with file_lock(self._lock_path(memory.hostname)):
            # Update timestamp
            memory.updated_at = datetime.utcnow().isoformat()

            # Save manifest (core fields)
            manifest = {
                "schema_version": memory.schema_version,
                "hostname": memory.hostname,
                "display_name": memory.display_name,
                "login_selectors": memory.login_selectors,
                "session_indicators": memory.session_indicators,
                "sensitive_patterns": memory.sensitive_patterns,
                "field_mappings": memory.field_mappings,
                "created_at": memory.created_at,
                "updated_at": memory.updated_at,
            }
            self._atomic_write(site_dir / "manifest.json", manifest)

            # Save pages
            if memory.pages:
                self._atomic_write(
                    site_dir / "pages.json",
                    [p.model_dump() for p in memory.pages]
                )

            # Save endpoints
            if memory.endpoints:
                self._atomic_write(
                    site_dir / "endpoints.json",
                    [e.model_dump() for e in memory.endpoints]
                )

            # Save navigation
            if memory.navigation_paths:
                self._atomic_write(
                    site_dir / "navigation.json",
                    [n.model_dump() for n in memory.navigation_paths]
                )

            # Save notes
            if memory.notes:
                self._atomic_write(site_dir / "notes.json", memory.notes)

    def list_sites(self) -> list[str]:
        """List all sites with stored memory."""
        sites = []
        for item in self.base_dir.iterdir():
            if item.is_dir() and (item / "manifest.json").exists():
                sites.append(item.name)
        return sorted(sites)

    def add_page(self, hostname: str, page: PageStructure) -> None:
        """Add or update a page structure."""
        memory = self.get(hostname) or SiteMemory(hostname=hostname)

        # Update existing or append
        for i, p in enumerate(memory.pages):
            if p.url_pattern == page.url_pattern:
                memory.pages[i] = page
                self.save(memory)
                return

        memory.pages.append(page)
        self.save(memory)

    def add_endpoint(self, hostname: str, endpoint: ApiEndpoint) -> None:
        """Add or update an API endpoint."""
        # Reject secrets in notes
        reject_secrets(endpoint.notes or "", "endpoint.notes")

        memory = self.get(hostname) or SiteMemory(hostname=hostname)

        for i, e in enumerate(memory.endpoints):
            if e.name == endpoint.name:
                memory.endpoints[i] = endpoint
                self.save(memory)
                return

        memory.endpoints.append(endpoint)
        self.save(memory)

    def add_note(self, hostname: str, text: str, author: str = "agent") -> None:
        """Add a freeform note."""
        reject_secrets(text, "note")

        memory = self.get(hostname) or SiteMemory(hostname=hostname)
        memory.notes.append({
            "date": datetime.utcnow().isoformat(),
            "author": author,
            "text": text,
        })
        self.save(memory)

    def mark_stale(self, hostname: str, endpoint_name: str) -> None:
        """Mark an endpoint as stale."""
        memory = self.get(hostname)
        if not memory:
            return

        for e in memory.endpoints:
            if e.name == endpoint_name:
                e.stale = True
                self.save(memory)
                return

    # Candidate management
    def _candidates_dir(self, hostname: str) -> Path:
        return self._site_dir(hostname) / "candidates"

    def add_candidate(self, hostname: str, candidate: Candidate) -> None:
        """Store a pending observation."""
        reject_secrets(candidate.claim, "candidate.claim")
        reject_secrets(candidate.evidence, "candidate.evidence")
        reject_secrets(candidate.consequence, "candidate.consequence")

        candidates_dir = self._candidates_dir(hostname)
        candidates_dir.mkdir(parents=True, exist_ok=True)

        path = candidates_dir / f"{candidate.id}.json"
        self._atomic_write(path, candidate.model_dump())

    def list_candidates(self, hostname: str, status: str | None = None) -> list[Candidate]:
        """List candidates, optionally filtered by status."""
        candidates_dir = self._candidates_dir(hostname)
        if not candidates_dir.exists():
            return []

        candidates = []
        for path in candidates_dir.glob("*.json"):
            with open(path) as f:
                c = Candidate(**json.load(f))
                if status is None or c.status == status:
                    candidates.append(c)

        return sorted(candidates, key=lambda c: c.observed_at, reverse=True)

    def review_candidate(
        self,
        hostname: str,
        candidate_id: str,
        accept: bool,
        rejection_reason: str | None = None
    ) -> None:
        """Accept or reject a candidate."""
        path = self._candidates_dir(hostname) / f"{candidate_id}.json"
        if not path.exists():
            raise ValueError(f"Candidate {candidate_id} not found")

        with open(path) as f:
            candidate = Candidate(**json.load(f))

        candidate.status = "accepted" if accept else "rejected"
        candidate.reviewed_at = datetime.utcnow().isoformat()
        if not accept:
            candidate.rejection_reason = rejection_reason

        self._atomic_write(path, candidate.model_dump())

    def delete(self, hostname: str) -> bool:
        """Delete all memory for a site."""
        site_dir = self._site_dir(hostname)
        if site_dir.exists():
            shutil.rmtree(site_dir)
            return True
        return False
