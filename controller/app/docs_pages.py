"""/docs and /redoc with pinned, integrity-checked assets.

FastAPI's built-in pages load Swagger UI and ReDoc from a CDN by floating major
version (``swagger-ui-dist@5``, ``redoc@2``) with no Subresource Integrity, plus
Google Fonts and a favicon from fastapi.tiangolo.com. Swagger's Authorize box
takes the API token, so a changed CDN file could read it. These pages load exact
versions with SRI hashes, which the browser checks before it runs a byte, and
nothing else from another origin.

The hashes were computed from the npm registry tarballs (checked against the
registry's own integrity) and match jsDelivr's copies byte for byte. To move a
version: fetch the new files, recompute the sha384, and change both together.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse

_CDN = "https://cdn.jsdelivr.net/npm"
SWAGGER_UI_VERSION = "5.34.0"
REDOC_VERSION = "2.5.4"

SWAGGER_JS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui-bundle.js",
    "sha384-9Td7MKiIr2S83E1kZyyVi8KdJw/S8CpAvu0llLyMeg0xq/hDGiZJfVn0fV4llCmK",
)
SWAGGER_CSS = (
    f"{_CDN}/swagger-ui-dist@{SWAGGER_UI_VERSION}/swagger-ui.css",
    "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF",
)
REDOC_JS = (
    f"{_CDN}/redoc@{REDOC_VERSION}/bundles/redoc.standalone.js",
    "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5",
)
# No favicon request at all, rather than one to a third party.
_NO_FAVICON = "data:,"


def _with_integrity(html: str, attribute: str, asset: tuple[str, str]) -> str:
    url, digest = asset
    needle = f'{attribute}="{url}"'
    if html.count(needle) != 1:
        # FastAPI changed its page template: fail loudly rather than serve an unchecked asset.
        raise RuntimeError(f"docs page template no longer has exactly one {needle}")
    return html.replace(needle, f'{needle} integrity="{digest}" crossorigin="anonymous"')


def _openapi_url(application: FastAPI, request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/") + (application.openapi_url or "")


def install_docs_pages(application: FastAPI) -> None:
    """Serve /docs and /redoc; create the app with docs_url=None and redoc_url=None.

    Plain routes, as FastAPI's own are, so the app's dependencies don't run on
    them. No OAuth2 redirect page: the API takes a bearer token only.
    """

    async def swagger_ui(request: Request) -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url=_openapi_url(application, request),
            title=f"{application.title} - Swagger UI",
            swagger_js_url=SWAGGER_JS[0],
            swagger_css_url=SWAGGER_CSS[0],
            swagger_favicon_url=_NO_FAVICON,
            oauth2_redirect_url=None,
        )
        html = page.body.decode("utf-8")
        html = _with_integrity(html, "src", SWAGGER_JS)
        html = _with_integrity(html, "href", SWAGGER_CSS)
        return HTMLResponse(html)

    async def redoc(request: Request) -> HTMLResponse:
        page = get_redoc_html(
            openapi_url=_openapi_url(application, request),
            title=f"{application.title} - ReDoc",
            redoc_js_url=REDOC_JS[0],
            redoc_favicon_url=_NO_FAVICON,
            with_google_fonts=False,
        )
        return HTMLResponse(_with_integrity(page.body.decode("utf-8"), "src", REDOC_JS))

    application.add_route("/docs", swagger_ui, include_in_schema=False)
    application.add_route("/redoc", redoc, include_in_schema=False)
