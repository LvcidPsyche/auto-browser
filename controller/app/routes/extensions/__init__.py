"""
routes.extensions — FastAPI route definitions for all 1.0 pillars.

Registers: /mesh, /network, /cdp, /workflow, /dashboard

One module per pillar; this package facade preserves the original
``app.routes.extensions`` import surface.
"""
from __future__ import annotations

import logging

from .cdp import cdp_router
from .dashboard import dashboard_router
from .dashboard_html import _DASHBOARD_HTML
from .mesh import MeshReceiveRequest, mesh_router
from .network import network_router
from .workflow import WorkflowRunRequest, workflow_router

logger = logging.getLogger(__name__)

__all__ = [
    "_DASHBOARD_HTML",
    "MeshReceiveRequest",
    "WorkflowRunRequest",
    "cdp_router",
    "dashboard_router",
    "mesh_router",
    "network_router",
    "register_all_routers",
    "workflow_router",
]


def register_all_routers(app) -> None:
    """Call from main.py startup to register all 1.0 routers."""
    app.include_router(mesh_router)
    app.include_router(network_router)
    app.include_router(cdp_router)
    app.include_router(workflow_router)
    app.include_router(dashboard_router)
    logger.info("routes.extensions: 1.0 routers registered")
