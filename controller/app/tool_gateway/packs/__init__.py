"""Pack registration modules for MCP tools."""

from . import core, diagnostics, dom, harness, operations, session_ops, sitemap, storage

__all__ = ["core", "diagnostics", "dom", "harness", "operations", "session_ops", "sitemap", "storage"]


def register_all(registry, gateway) -> None:
    core.register(registry, gateway)
    harness.register(registry, gateway)
    diagnostics.register(registry, gateway)
    session_ops.register(registry, gateway)
    dom.register(registry, gateway)
    storage.register(registry, gateway)
    operations.register(registry, gateway)
    sitemap.register(registry, gateway)
