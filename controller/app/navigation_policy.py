"""Which URLs the browser may be sent to.

Two modes (``NAVIGATION_POLICY``):

``allowlist`` (the default, unchanged behaviour)
    Only hosts named in ``ALLOWED_HOSTS`` (exact match, ``*`` = any).

``public_internet``
    Any public http(s) site -- for a single-owner stack whose owner wants his
    assistants to open whatever site he names. ``ALLOWED_HOSTS`` is ignored.
    The safety blocks stay, and are stricter than the allowlist ever was:

    * only ``http`` / ``https`` (no ``file:``, ``chrome:``, ``data:``,
      ``javascript:``, ``view-source:``, ``ws:`` ...);
    * never a private, loopback, link-local (cloud metadata 169.254.169.254),
      CGNAT, multicast, reserved or unspecified address -- including the
      spellings Chromium accepts and urllib does not recognise as an IP
      (``http://2130706433/``, ``http://0x7f.1/``, ``http://127.1/``,
      ``http://%31%32%37.0.0.1/``, full-width digits, IPv4-mapped IPv6);
    * never an internal name: single-label hosts (``controller``,
      ``browser-node``, ``approval-broker`` -- every Docker service name),
      ``localhost``, ``*.localhost``, ``*.local``, ``*.internal``,
      ``*.home.arpa``, ``*.lan``, ``*.intranet``, ``*.corp``;
    * ``NAVIGATION_DENY_HOSTS``: an extra, configurable denylist (a name
      blocks that host and every subdomain of it);
    * DNS-rebinding guard (:func:`assert_resolves_public`): a public-looking
      name that resolves to any non-public address is refused -- checked
      before a navigation and again on the page's final URL after every
      action (redirects included).

    Residual risk, stated plainly: the DNS check and Chromium's own lookup are
    two lookups, so an attacker DNS server that answers "public" to us and
    "private" to Chromium a moment later is not caught here. The post-action
    re-check narrows that window, and Chromium's own Local Network Access
    protection (public pages cannot reach private addresses without a
    permission prompt) covers sub-resources.

Host parsing always goes through :func:`app.url_safety.browser_equivalent_url`
first so we judge the host Chromium will load, not the one urllib thinks it
sees.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import unicodedata
from collections.abc import Iterable
from urllib.parse import unquote, urlparse

from .host_policy import host_is_allowed
from .url_safety import browser_equivalent_url

MODE_ALLOWLIST = "allowlist"
MODE_PUBLIC_INTERNET = "public_internet"
MODES = frozenset({MODE_ALLOWLIST, MODE_PUBLIC_INTERNET})

PUBLIC_SCHEMES = frozenset({"http", "https"})

# Suffixes that never name a public site (RFC 6761/6762/8375 and common
# private-network conventions).
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".lan",
    ".intranet",
    ".corp",
    ".localdomain",
)
_INTERNAL_NAMES = frozenset({"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"})

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

DNS_TIMEOUT_SECONDS = 3.0
DNS_CACHE_SECONDS = 30.0


class NavigationRefused(PermissionError):
    """A URL the navigation policy will not let the browser load."""


def parse_host_list(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [item.strip().lower().rstrip(".") for item in items if item and item.strip()]


def normalize_host(raw_host: str) -> str:
    """The host as Chromium will see it: percent-decoded, NFKC-folded
    (full-width digits and dots become ASCII), lower-cased, no trailing dot."""
    host = unquote(raw_host or "")
    host = unicodedata.normalize("NFKC", host)
    # Ideographic/full-width full stops are label separators to Chromium.
    for dot in ("。", "．", "｡"):
        host = host.replace(dot, ".")
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def _whatwg_ipv4_part(part: str) -> int | None:
    if part == "":
        return None
    try:
        if part.startswith(("0x", "0X")):
            return int(part[2:], 16) if part[2:] else 0
        if len(part) > 1 and part.startswith("0"):
            return int(part[1:], 8)
        return int(part, 10)
    except ValueError:
        return None


def _looks_numeric(label: str) -> bool:
    if label == "":
        return False
    if label.startswith(("0x", "0X")):
        return all(ch in "0123456789abcdefABCDEF" for ch in label[2:])
    return label.isdigit()


def whatwg_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address Chromium reads ``host`` as, or None if it is a name.

    WHATWG: a host whose LAST label is numeric (decimal, 0x-hex or 0-octal)
    is an IPv4 address in 1-4 parts; a malformed one is a parse failure, which
    we report as ``ValueError`` so the caller refuses it.
    """
    labels = host.split(".")
    if not labels or not _looks_numeric(labels[-1]):
        return None
    if len(labels) > 4:
        raise ValueError("malformed IPv4 host")
    numbers = [_whatwg_ipv4_part(label) for label in labels]
    if any(number is None for number in numbers):
        raise ValueError("malformed IPv4 host")
    values = [int(number) for number in numbers if number is not None]
    if any(value > 255 for value in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):
        raise ValueError("IPv4 part out of range")
    address = values[-1]
    for index, value in enumerate(values[:-1]):
        address += value * 256 ** (3 - index)
    return ipaddress.IPv4Address(address)


def address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return address_is_public(mapped)
        if address in _NAT64:
            return address_is_public(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None and not address_is_public(address.sixtofour):
            return False
        if address.teredo is not None:
            return False
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """``example.com`` matches ``example.com`` and any ``*.example.com``."""
    for pattern in patterns:
        pattern = pattern.lstrip("*.") if pattern.startswith("*.") else pattern
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    return whatwg_ipv4(host)


def check_public_url(url: str, *, deny_hosts: Iterable[str] = ()) -> str:
    """Syntactic public-internet check. Returns the normalized host, or raises
    :class:`NavigationRefused`. No network access."""
    normalized = browser_equivalent_url(url or "")
    parsed = urlparse(normalized)
    scheme = (parsed.scheme or "").lower()
    if scheme not in PUBLIC_SCHEMES:
        raise NavigationRefused(f"Only http(s) sites may be opened (got {scheme or 'no'} scheme)")
    raw_host = parsed.hostname
    if not raw_host:
        raise NavigationRefused(f"Could not determine hostname for URL: {url}")
    host = normalize_host(raw_host)
    if not host:
        raise NavigationRefused(f"Could not determine hostname for URL: {url}")
    try:
        address = literal_address(host)
    except ValueError as exc:
        raise NavigationRefused(f"Host {host!r} is not a valid address") from exc
    if address is not None:
        if not address_is_public(address):
            raise NavigationRefused(f"Host {host!r} is a private or reserved address")
        return host
    if any(ch.isspace() for ch in host) or "%" in host:
        raise NavigationRefused(f"Host {host!r} is not a valid name")
    if "." not in host:
        raise NavigationRefused(f"Host {host!r} is an internal (single-label) name")
    if host in _INTERNAL_NAMES or host.endswith(_INTERNAL_SUFFIXES):
        raise NavigationRefused(f"Host {host!r} is an internal name")
    if host_matches(host, parse_host_list(deny_hosts)):
        raise NavigationRefused(f"Host {host!r} is on the navigation denylist")
    return host


def check_allowlisted_url(url: str, *, allowed_patterns: Iterable[str]) -> str:
    host = urlparse(browser_equivalent_url(url)).hostname
    if not host:
        raise NavigationRefused(f"Could not determine hostname for URL: {url}")
    if host_is_allowed(host, list(allowed_patterns)):
        return host
    raise NavigationRefused(f"Host {host!r} is not allowlisted")


# --- DNS rebinding guard ----------------------------------------------------------------

_dns_cache: dict[str, tuple[float, bool]] = {}


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT_SECONDS)
    return [info[4][0] for info in infos]


def clear_dns_cache() -> None:
    _dns_cache.clear()


async def assert_resolves_public(url: str, *, deny_hosts: Iterable[str] = ()) -> None:
    """Refuse a public-looking name whose DNS answer includes any non-public
    address. Fails closed on a lookup error: Chromium could not load a name we
    cannot resolve either, so nothing legitimate is lost."""
    host = check_public_url(url, deny_hosts=deny_hosts)
    try:
        literal = literal_address(host)
    except ValueError:  # pragma: no cover - check_public_url already refused it
        raise NavigationRefused(f"Host {host!r} is not a valid address") from None
    if literal is not None:
        return
    now = time.monotonic()
    cached = _dns_cache.get(host)
    if cached is not None and cached[0] > now:
        if not cached[1]:
            raise NavigationRefused(f"Host {host!r} resolves to a private or reserved address")
        return
    try:
        addresses = await _resolve(host)
    except (OSError, asyncio.TimeoutError, UnicodeError) as exc:
        raise NavigationRefused(f"Host {host!r} could not be resolved") from exc
    public = bool(addresses)
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            public = False
            break
        if not address_is_public(address):
            public = False
            break
    if len(_dns_cache) > 512:
        _dns_cache.clear()
    _dns_cache[host] = (now + DNS_CACHE_SECONDS, public)
    if not public:
        raise NavigationRefused(f"Host {host!r} resolves to a private or reserved address")


async def await_public_dns_check(manager: object, url: str) -> None:
    """Run ``manager._assert_url_resolves_public(url)`` when the manager has
    one (the real BrowserManager does; lightweight test doubles may not)."""
    check = getattr(type(manager), "_assert_url_resolves_public", None)
    if check is None or not asyncio.iscoroutinefunction(check):
        return
    await check(manager, url)
