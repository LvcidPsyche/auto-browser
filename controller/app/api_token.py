"""The controller's own API token, for a deployment that configured none.

A loopback controller with no API_BEARER_TOKEN used to serve every route
unauthenticated. Loopback described the host publish mapping (127.0.0.1:8000),
not who could reach port 8000: every container on the compose network,
browser-node included, could drive the API and export stored logins by sending
`Host: localhost`. Now the controller loads its token from API_TOKEN_FILE, or
generates one there on first start, and the middleware demands it like any
configured API_BEARER_TOKEN.

Precedence, decided once at startup:

1. API_BEARER_TOKEN or API_BEARER_TOKENS set: used as before; the file is ignored.
2. Loopback, not production, API_ALLOW_TOKENLESS_LOOPBACK=true: no token, as before.
3. The file holds a token: it is used, in any scope.
4. Loopback, not production, no file: a token is generated and saved there.
5. Otherwise (exposed or production, no token anywhere): nothing here; the
   runtime policy refuses to start, as it always has.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from .auth_policy import BIND_SCOPE_LOOPBACK, credentials_for, resolve_bind_scope, tokenless_loopback_allowed
from .runtime_policy import MIN_PRODUCTION_BEARER_TOKEN_LENGTH
from .utils import PRIVATE_FILE_MODE, atomic_write_text

logger = logging.getLogger(__name__)

# token_urlsafe(32): 256 bits as 43 characters of [A-Za-z0-9_-], which a header,
# a URL fragment (the dashboard's #token=) and a shell all take unquoted.
GENERATED_TOKEN_BYTES = 32
# Bits a token file must not grant beyond its owner.
_GROUP_OR_OTHER_BITS = 0o077
_WAYS_OUT = (
    "set API_TOKEN_FILE to a path the controller can write, set API_BEARER_TOKEN, "
    "or set API_ALLOW_TOKENLESS_LOOPBACK=true to serve loopback without a token"
)


def ensure_api_credential(settings: Any) -> None:
    """Load or generate the token, and put it in ``settings.api_bearer_token``.

    Called first in the lifespan, before the runtime policy checks credentials,
    and never at import (tests and dev harnesses import the app). Never logs the
    token; the line for a reused one carries a sha256 fingerprint so a client and
    the server can be matched.
    """
    path = Path(settings.api_token_file)
    if credentials_for(settings):
        if os.path.exists(path):
            logger.info("API_BEARER_TOKEN or API_BEARER_TOKENS is set, so the token in %s is ignored", path)
        return

    loopback = resolve_bind_scope(settings) == BIND_SCOPE_LOOPBACK
    may_generate = loopback and not settings.is_production
    if file_to_read(settings) is None:
        logger.warning(
            "API_ALLOW_TOKENLESS_LOOPBACK=true: serving loopback without a token; %s is neither read nor written",
            path,
        )
        return

    token = _read_token_file(path)
    if token:
        _restrict_mode(path)
        logger.info("Using the API token in %s (sha256 fingerprint %s)", path, _fingerprint(token))
        settings.api_bearer_token = token
        return
    if token == "":
        logger.warning("API_TOKEN_FILE %s is empty", path)
    if not may_generate:
        return

    token = _generate_token_file(path)
    _warn_if_the_mode_was_not_kept(path, "API_TOKEN_FILE")
    settings.api_bearer_token = token
    logger.warning(
        "No API credential was configured, so the controller generated a bearer token and saved it to %s "
        "(%s; with the shipped compose it is ./data/api-token on the host; sha256 fingerprint %s). "
        "Every API and MCP request must now send 'Authorization: Bearer <token>'. Point clients at the file "
        "(auto-browser-mcp --token-file / AUTO_BROWSER_TOKEN_FILE), set API_BEARER_TOKEN, or set "
        "API_ALLOW_TOKENLESS_LOOPBACK=true to serve loopback without one.",
        path,
        _permissions_note(),
        _fingerprint(token),
    )


# The fixed text GET /sessions/{session_id}/takeover-link answers with when ensure_takeover_token loaded none.
TAKEOVER_LINK_UNAVAILABLE_DETAIL = (
    "The controller has no takeover token: set TAKEOVER_TOKEN, or fix TAKEOVER_TOKEN_FILE as the startup log says (it "
    "names the cause), and restart."
)


def ensure_takeover_token(settings: Any) -> None:
    """Load or generate the takeover token, and put it in ``settings.takeover_token`` (None when there is none).

    GET /sessions/{session_id}/takeover-link returns an isolated session's noVNC password, so it demands this
    second secret beside the bearer token, which agents hold. Precedence: TAKEOVER_TOKEN when set (the file is
    then ignored); else the token in TAKEOVER_TOKEN_FILE; else one generated there, in every scope.

    A configured TAKEOVER_TOKEN that is short, or equal to an API credential (a token an agent already holds
    protects nothing), refuses startup. A file that cannot be read or written, holds an unusable token or an API
    credential, or stays readable beyond its owner because its mode cannot be tightened (a root-owned 0444 mount),
    only warns, naming that cause: the controller starts and that endpoint answers 503. Never logs the token; a
    sha256 fingerprint at most.
    Called after ensure_api_credential, so a token it loaded from API_TOKEN_FILE counts as an API credential.
    """
    api_tokens = {credential.token for credential in credentials_for(settings)}
    configured = settings.takeover_token
    path = Path(settings.takeover_token_file)
    if configured:
        if len(configured) < MIN_PRODUCTION_BEARER_TOKEN_LENGTH:
            raise RuntimeError(
                f"TAKEOVER_TOKEN must be at least {MIN_PRODUCTION_BEARER_TOKEN_LENGTH} characters "
                f"(found {len(configured)}). Set a longer one, or unset it to have the controller generate one."
            )
        if not _printable_ascii(configured):
            # Not the value: it is a secret. The token file's rule, so the variable and the file accept the same
            # tokens, and one that every client sends unchanged: curl sends a UTF-8 .env value that Starlette reads
            # as latin-1, so a non-ASCII token can fail for one client and pass for another.
            raise RuntimeError(
                "TAKEOVER_TOKEN must be printable ASCII with no spaces (letters, digits and punctuation), as a "
                "token file must. Set another, or unset it to have the controller generate one."
            )
        if configured in api_tokens:
            raise RuntimeError(
                "TAKEOVER_TOKEN equals API_BEARER_TOKEN or an API_BEARER_TOKENS entry: agents hold those, so it "
                "would protect nothing. Set a different TAKEOVER_TOKEN, or unset it to have one generated."
            )
        if os.path.exists(path):
            logger.info("TAKEOVER_TOKEN is set, so the token in %s is ignored", path)
        return

    _refuse_a_served_token_file(settings, path)
    settings.takeover_token = None
    try:
        token = _read_takeover_token_file(path)
        if token is None:
            token = secrets.token_urlsafe(GENERATED_TOKEN_BYTES)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, token + "\n", mode=PRIVATE_FILE_MODE)
            _match_parent_owner(path)
            _warn_if_the_mode_was_not_kept(path, "TAKEOVER_TOKEN_FILE")
            logger.warning(
                "Generated a takeover token and saved it to %s (%s; with the shipped compose it is "
                "./data/takeover-token on the host; sha256 fingerprint %s). GET /sessions/{session_id}/takeover-link "
                "demands it in the X-Takeover-Token header; the dashboard asks for it when it first opens an isolated "
                "session's noVNC in a tab and again after a refusal, and scripts/takeover_link.sh reads it. Never give "
                "it to an agent.",
                path,
                _permissions_note(),
                _fingerprint(token),
            )
        else:
            try:
                _restrict_mode(path, name="TAKEOVER_TOKEN_FILE")
            except OSError as exc:
                # It reads fine but stays readable beyond its owner (a root-owned 0444 secret mount): fail closed,
                # naming that cause rather than the read-or-write one below (W2-23).
                logger.warning(
                    "No takeover token: TAKEOVER_TOKEN_FILE %s is readable beyond its owner and its mode could not "
                    "be tightened (%s). Make it owned by the controller's user with mode 0600 (or 0400), or set "
                    "TAKEOVER_TOKEN; GET /sessions/{session_id}/takeover-link answers 503 until then and a restart.",
                    path,
                    exc,
                )
                return
            logger.info("Using the takeover token in %s (sha256 fingerprint %s)", path, _fingerprint(token))
    except (OSError, ValueError) as exc:
        logger.warning(
            "No takeover token: %s. GET /sessions/{session_id}/takeover-link answers 503 until TAKEOVER_TOKEN is "
            "set or TAKEOVER_TOKEN_FILE %s holds a usable token (or can be written) and the controller restarts.",
            exc,
            path,
        )
        return
    if token in api_tokens:
        logger.warning(
            "TAKEOVER_TOKEN_FILE %s holds an API credential, which agents hold, so it is not used: "
            "GET /sessions/{session_id}/takeover-link answers 503 until the file holds a different token.",
            path,
        )
        return
    settings.takeover_token = token


def _printable_ascii(token: str) -> bool:
    """Printable ASCII with no spaces: what a header carries unchanged, whatever the client and the server."""
    return all("!" <= char <= "~" for char in token)


# The data roots whose files the API exposes by path (setting name to Settings attribute): /artifacts serves
# ARTIFACT_ROOT, UPLOAD_ROOT's files can be uploaded into a page, GET /auth-profiles/{name}/export packages AUTH_ROOT,
# and maintenance sweeps all three. The other roots are read by fixed names (W5-3), so a token file there reaches no
# agent, and one of them set to the data root itself must not refuse the default /data/takeover-token.
TAKEOVER_TOKEN_REFUSED_ROOTS = {
    "ARTIFACT_ROOT": "artifact_root",
    "UPLOAD_ROOT": "upload_root",
    "AUTH_ROOT": "auth_root",
}
# The directories compose mounts into containers, under the parent of ARTIFACT_ROOT (/data in the image), where a
# compromised browser or helper container could read or replace the file, with the name the refusal gives each.
TAKEOVER_TOKEN_REFUSED_MOUNTS = {
    "browser-profile": "the browser-profile directory browser-node mounts",
    "downloads": "the downloads directory browser-node mounts",
    "browser-sessions": "the browser-sessions directory isolated sessions mount",
    "tunnels": "the tunnels directory reverse-ssh mounts",
}


def _refused_token_directories(settings: Any) -> list[tuple[str, Path]]:
    """Each directory a takeover token must not be in, with the name the refusal gives it.

    The data roots above, then the directories mounted into the browser, session and helper containers, found as
    session_isolation finds them, under the parent of ARTIFACT_ROOT: never guessed from another setting, which can
    name the data root itself.
    """
    directories = [
        (name, Path(getattr(settings, attribute))) for name, attribute in TAKEOVER_TOKEN_REFUSED_ROOTS.items()
    ]
    data_root = Path(settings.artifact_root).resolve().parent
    directories += [(name, data_root / relative) for relative, name in TAKEOVER_TOKEN_REFUSED_MOUNTS.items()]
    return directories


def _refuse_a_served_token_file(settings: Any, path: Path) -> None:
    """RuntimeError when TAKEOVER_TOKEN_FILE resolves inside a data root or a container-mounted directory."""
    target = path.resolve()
    for name, root in _refused_token_directories(settings):
        if target.is_relative_to(root.resolve()):
            raise RuntimeError(
                f"TAKEOVER_TOKEN_FILE {path} is inside {name} ({root}), whose files can be read or replaced through "
                "the API or by a container. Point it outside, such as /data/takeover-token, or set TAKEOVER_TOKEN."
            )


def _read_takeover_token_file(path: Path) -> str | None:
    """The stripped token, None when there is no file; ValueError (naming no token) when it is unusable."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except UnicodeDecodeError as exc:
        raise ValueError(f"TAKEOVER_TOKEN_FILE {path} is not UTF-8 text") from exc
    if len(token) < MIN_PRODUCTION_BEARER_TOKEN_LENGTH or not _printable_ascii(token):
        raise ValueError(
            f"TAKEOVER_TOKEN_FILE {path} does not hold a usable token: it must be at least "
            f"{MIN_PRODUCTION_BEARER_TOKEN_LENGTH} printable ASCII characters with no spaces "
            f"(found {len(token)} characters); replace it, or delete it to have the controller generate one"
        )
    return token


def file_to_read(settings: Any) -> Path | None:
    """The token file ensure_api_credential reads (or generates), or None when it reads none: an environment token
    (API_BEARER_TOKEN, API_BEARER_TOKENS), or tokenless loopback (loopback bind, not production, the setting on).
    entrypoint.sh asks this through `python -m app.api_token --file-to-read`, so its pre-check follows the controller."""
    if credentials_for(settings):
        return None
    loopback = resolve_bind_scope(settings) == BIND_SCOPE_LOOPBACK
    if loopback and not settings.is_production and tokenless_loopback_allowed(settings):
        return None
    return Path(settings.api_token_file)


def main(argv: list[str]) -> int:
    """`python -m app.api_token --file-to-read`: print the path the controller would read, or nothing."""
    if argv[1:] != ["--file-to-read"]:
        print("usage: python -m app.api_token --file-to-read", file=sys.stderr)
        return 64
    from .config import Settings

    path = file_to_read(Settings())
    print("" if path is None else str(path))
    return 0


def _permissions_note() -> str:
    if os.name == "nt":
        return (
            "the file inherits the permissions of its folder, which may let other local users read it: "
            "set API_TOKEN_FILE under your user profile"
        )
    return "mode 0600"


def _read_token_file(path: Path) -> str | None:
    """The stripped token, "" for an empty file, None when there is no file.

    A token an operator put there is refused rather than overwritten when it is
    unusable, so a typo stops startup instead of silently replacing their token.
    """
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read API_TOKEN_FILE {path}: {exc}. Fix the file, or {_WAYS_OUT}.") from exc
    if not token:
        return ""
    if len(token) < MIN_PRODUCTION_BEARER_TOKEN_LENGTH or not all("!" <= char <= "~" for char in token):
        # The token itself stays out of the message: it is about to reach a log.
        raise RuntimeError(
            f"API_TOKEN_FILE {path} does not hold a usable token: it must be at least "
            f"{MIN_PRODUCTION_BEARER_TOKEN_LENGTH} printable ASCII characters with no spaces "
            f"(found {len(token)} characters). Replace it, or delete it to have the controller generate one."
        )
    return token


def _generate_token_file(path: Path) -> str:
    token = secrets.token_urlsafe(GENERATED_TOKEN_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, token + "\n", mode=PRIVATE_FILE_MODE)
    except OSError as exc:
        raise RuntimeError(
            f"No API credential was configured and the controller could not save a generated token "
            f"to API_TOKEN_FILE {path}: {exc}. To start, {_WAYS_OUT}."
        ) from exc
    _match_parent_owner(path)
    return token


def _restrict_mode(path: Path, *, name: str = "API_TOKEN_FILE") -> None:
    """Take group and other permissions off a token file. POSIX only: Windows applies the folder's ACL.

    The file is opened without following a symlink and changed through that
    descriptor, so swapping the path between the check and the chmod cannot point
    the chmod at another file. A symlinked token (a Kubernetes secret mount, 0644
    by default) is used as it is, since its target is not ours to change, with one
    warning naming the path and mode when the target is readable beyond its owner.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _warn_about_an_open_link_target(path, name)
            return
        raise
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if not mode & _GROUP_OR_OTHER_BITS:
            return
        os.fchmod(fd, PRIVATE_FILE_MODE)
        # Some filesystems accept a chmod and keep the mode (a Windows drive under Docker Desktop, some FUSE or SMB
        # mounts, W5-4): check that it held. Nobody can tighten such a file, so the token is still used.
        kept = stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    if kept & _GROUP_OR_OTHER_BITS:
        logger.warning(
            "%s %s is readable beyond its owner (mode %o) and its filesystem did not keep a change to 0600, so it "
            "stays readable beyond its owner where that filesystem enforces modes. Restrict it where it is mounted.",
            name,
            path,
            kept,
        )
        return
    logger.warning("%s %s was readable beyond its owner (mode %o); changed it to 0600", name, path, mode)


def _warn_if_the_mode_was_not_kept(path: Path, name: str) -> None:
    """After generating a token file 0600, warn when its filesystem did not keep that mode (W7-4), as
    _restrict_mode does for an existing file (W5-4): a Windows drive under Docker Desktop reports every file
    readable beyond its owner. The token is still used. POSIX only; never raises."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & _GROUP_OR_OTHER_BITS:
        logger.warning(
            "%s %s was written with mode 0600 but its filesystem did not keep 0600 (mode %o), so it stays readable "
            "beyond its owner where that filesystem enforces modes. Restrict it where it is mounted.",
            name,
            path,
            mode,
        )


def _warn_about_an_open_link_target(path: Path, name: str) -> None:
    """Open a symlinked token file following the link and warn when its target grants group or other bits.

    Never raises: a target that vanished or turned unreadable since the token was read gets one warning that its mode
    could not be checked, and the token already read is used.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning(
            "%s %s is a symlink whose target's mode could not be checked (%s); the token already read from it is used.",
            name,
            path,
            exc,
        )
        return
    if mode & _GROUP_OR_OTHER_BITS:
        logger.warning(
            "%s %s is a symlink to a file readable beyond its owner (mode %o); it is used as it is, since its "
            "target is not the controller's to change. Restrict it where it is mounted (a Kubernetes secret's "
            "defaultMode 0400, say).",
            name,
            path,
            mode,
        )


def _match_parent_owner(path: Path) -> None:
    """Give a root-written token file to whoever owns its directory.

    The controller image runs as root and ./data belongs to the user who cloned
    the repo, so without this the host user, the stdio bridge and CI's runner
    could not read a 0600 root-owned file. Docker Desktop, rootless Docker and
    userns-remap map ownership their own way (not verified per platform).
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    parent = os.stat(path.parent)
    if parent.st_uid == 0:
        return
    try:
        os.chown(path, parent.st_uid, parent.st_gid, follow_symlinks=False)
    except OSError as exc:
        logger.warning(
            "Could not give %s to the owner of %s (uid %s): %s. Clients running as that user may not read it.",
            path,
            path.parent,
            parent.st_uid,
            exc,
        )


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
