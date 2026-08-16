"""Pack registration modules for MCP tools."""

from . import core, diagnostics, dom, harness, operations, session_ops, storage, youcom

__all__ = ["core", "diagnostics", "dom", "harness", "operations", "session_ops", "storage", "youcom"]


def register_all(registry, gateway) -> None:
    core.register(registry, gateway)
    harness.register(registry, gateway)
    diagnostics.register(registry, gateway)
    session_ops.register(registry, gateway)
    dom.register(registry, gateway)
    storage.register(registry, gateway)
    operations.register(registry, gateway)
    youcom.register(registry, gateway)
