from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from .utils import without_lone_surrogates


class SafeJSONResponse(JSONResponse):
    """A JSON response that a lone surrogate from a page cannot fail.

    A page can hand over a lone surrogate (a JSON string's escape for one decodes to it), and UTF-8 cannot carry it,
    so a response holding one raised UnicodeEncodeError after its handler had run. Only a body that fails the normal
    rendering is rewritten, so a normal response pays nothing. U+FFFD, and not the JSON escape, because a strict JSON
    parser refuses a lone surrogate's escape, and a page should not be able to break a client's parser.
    """

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except UnicodeEncodeError:
            return super().render(without_lone_surrogates(content))
