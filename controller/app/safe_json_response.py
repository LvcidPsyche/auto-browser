from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.requests import Request

from .tool_gateway.gateway import _json_safe
from .utils import without_lone_surrogates


class SafeJSONResponse(JSONResponse):
    """A JSON response that neither a lone surrogate from a page nor a non-finite number can fail.

    A page can hand over a lone surrogate (a JSON string's escape for one decodes to it), and UTF-8 cannot carry it,
    so a response holding one raised UnicodeEncodeError after its handler had run. Only a body that fails the normal
    rendering is cleaned, so a normal response is not walked. U+FFFD, and not the JSON escape, because a strict JSON
    parser refuses a lone surrogate's escape, and a page should not be able to break a client's parser.

    A NaN or an infinity is no JSON either, and json.dumps(allow_nan=False) raises ValueError on one, again after
    the handler has run. It is answered as null, by the walk a tool result goes through (_json_safe): what FastAPI's
    own JSON path, which the app's default response class takes typed routes off, wrote for a model's float field.
    """

    def render(self, content: Any) -> bytes:
        try:
            return super().render(content)
        except UnicodeEncodeError:
            return super().render(without_lone_surrogates(content))
        except ValueError:
            # json.dumps comes before the encoding, so the body can still hold a lone surrogate after this. Its
            # other ValueError (an int too long to print) is raised again below, as it was.
            content = _json_safe(content)
        try:
            return super().render(content)
        except UnicodeEncodeError:
            return super().render(without_lone_surrogates(content))


async def safe_http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """FastAPI's own answer, unless the detail holds a lone surrogate: a route's ``str(exc)`` can quote a page."""
    try:
        return await http_exception_handler(request, exc)
    except UnicodeEncodeError:
        clean = HTTPException(exc.status_code, without_lone_surrogates(exc.detail), exc.headers)
        return await http_exception_handler(request, clean)


async def safe_validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """FastAPI's own 422, unless what was refused holds a lone surrogate or a non-finite number: its errors quote
    the input."""
    try:
        return await request_validation_exception_handler(request, exc)
    except UnicodeEncodeError:
        clean = RequestValidationError(without_lone_surrogates(list(exc.errors())), body=exc.body)
        return await request_validation_exception_handler(request, clean)
    except ValueError:
        # Python's JSON reader takes NaN, Infinity and a number too large for a float (1e999) from a body, and the 422
        # quotes it back: no JSON, so the answer itself failed, a 500. FastAPI's content, written as any answer
        # holding one is (null in its place, and U+FFFD for a lone surrogate beside it).
        return SafeJSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
