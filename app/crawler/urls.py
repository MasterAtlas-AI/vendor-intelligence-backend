"""URL normalisation and the network safety check.

Crawled URLs come from web search results and AI output, i.e. from the internet. Before any request (and after every
redirect) the target host is resolved and refused if it points inside a private network, so a crafted link can't make
the server fetch internal services or cloud metadata (SSRF).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.domain.normalization import registrable_domain

# Query parameters that only track clicks; dropping them de-duplicates URLs.
TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "_ga", "_hsenc", "_hsmi"}
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".msi", ".dmg", ".iso",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf", ".eot",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".dwg", ".step", ".stp", ".igs",
)  # fmt: skip

Resolver = Callable[[str], Awaitable[list[str]]]


class BlockedURL(Exception):
    """The URL is not allowed (scheme, credentials, port, or it resolves to a private address)."""


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Absolute, canonical form used as the cache key: http(s) only, lower-case host, no fragment, no default port,
    no tracking parameters, sorted query. None for anything else (mailto:, javascript:, files we never fetch, ...)."""
    url = (url or "").strip()
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    path = parts.path or "/"
    if path.lower().endswith(SKIP_EXTENSIONS):
        return None
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
               if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_"))
    )  # fmt: skip
    return urlunsplit((scheme, netloc, path, query, ""))


def same_site(url: str, domain: str) -> bool:
    """True if the URL belongs to the registrable domain (www., subdomains and country sites of it included)."""
    host = urlsplit(url).hostname or ""
    return registrable_domain(host) == domain


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )  # fmt: skip


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def check_url(url: str, resolve: Resolver = system_resolver) -> None:
    """Raises BlockedURL unless the URL is a plain public http(s) address."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"not an http(s) URL: {url}")
    if parts.username or parts.password:
        raise BlockedURL("URLs with credentials are not fetched")
    if parts.port not in (None, 80, 443, 8080, 8443):
        raise BlockedURL(f"port {parts.port} is not allowed")
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise BlockedURL(f"{host} is a local name")
        try:
            addresses = await resolve(host)
        except OSError as exc:
            raise BlockedURL(f"{host} doesn't resolve") from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise BlockedURL(f"{host} resolves to a non-public address")
