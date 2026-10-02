"""
routes.extensions — FastAPI routes for the optional subsystems.

Registers: /mesh, /network, /cdp, /dashboard

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

logger = logging.getLogger(__name__)

__all__ = [
    "_DASHBOARD_HTML",
    "MeshReceiveRequest",
    "cdp_router",
    "dashboard_router",
    "mesh_router",
    "network_router",
    "register_all_routers",
]


def register_all_routers(app) -> None:
    """Call from main.py startup to register the extension routers."""
    app.include_router(mesh_router)
    app.include_router(network_router)
    app.include_router(cdp_router)
    app.include_router(dashboard_router)
    logger.info("routes.extensions: registered /mesh, /network, /cdp, /dashboard")
