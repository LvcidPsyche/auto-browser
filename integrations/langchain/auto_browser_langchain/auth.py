from __future__ import annotations

import os
from pathlib import Path


def resolve_bearer_token(bearer_token: str | None, token_file: str | os.PathLike[str] | None) -> str | None:
    """The token to send: ``bearer_token`` when set, else the one in ``token_file``, read now.

    A local controller with no API_BEARER_TOKEN writes its generated token to
    data/api-token in the auto-browser folder. Callers read the file on every
    request, so a tool built before the controller first started, or kept across
    a regenerated token, sends the current one. A missing or empty file raises:
    the caller asked for a token, and a request without one would only 401.
    """
    token = (bearer_token or "").strip() or None
    if token or token_file is None:
        return token
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"token_file {os.fspath(token_file)} does not exist: start the controller first, which writes its "
            "token to data/api-token in the auto-browser folder"
        ) from exc
    if not token:
        raise ValueError(f"token_file {os.fspath(token_file)} is empty")
    return token
