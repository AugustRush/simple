from __future__ import annotations

import base64
import http.client
import ipaddress
import os
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional


class UnsafeNetworkTarget(ValueError):
    """Raised when a URL cannot be fetched without crossing a private boundary."""


@dataclass(frozen=True)
class ResolvedEndpoint:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    request_target: str


@dataclass(frozen=True)
class ProxyConfig:
    """An HTTP proxy to route a fetch through.

    Only the HTTP-proxy wire form is supported — plain requests carry an
    absolute URI and HTTPS requests are tunnelled with CONNECT.  That is what a
    mixed port (Clash's ``mixed-port``, most corporate proxies) accepts from an
    HTTP client, and it is the only form ``http.client`` can speak.
    """

    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def authorization(self) -> Optional[str]:
        """``Proxy-Authorization`` value, or None when the proxy is open."""
        if self.username is None:
            return None
        raw = f"{self.username}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode()


@dataclass(frozen=True)
class FetchResponse:
    body: bytes
    final_url: str
    status: int
    headers: Mapping[str, str]


Resolver = Callable[..., list[tuple[Any, ...]]]
ConnectionFactory = Callable[[ResolvedEndpoint, str, float], Any]
ProxiedConnectionFactory = Callable[[ResolvedEndpoint, ProxyConfig, float], Any]

# Headers that carry caller authority and must not survive a hop to a different
# origin: a redirect target is chosen by the *server*, not by the caller, so
# replaying credentials to it hands them to whoever controls that redirect.
_ORIGIN_BOUND_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization", "www-authenticate"}
)


def _origin(endpoint: ResolvedEndpoint) -> tuple[str, str, int]:
    return (endpoint.scheme, endpoint.hostname.lower(), endpoint.port)


def _strip_origin_bound_headers(
    headers: Mapping[str, str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    kept = {
        name: value
        for name, value in headers.items()
        if name.lower() not in _ORIGIN_BOUND_HEADERS
    }
    dropped = tuple(
        sorted(
            name for name in headers if name.lower() in _ORIGIN_BOUND_HEADERS
        )
    )
    return kept, dropped


def _canonical_public_address(raw: str) -> str:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeNetworkTarget(f"invalid resolved address: {raw}") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        raise UnsafeNetworkTarget(f"network target is not globally routable: {address}")
    return str(address)


def _parse_target(url: str) -> ResolvedEndpoint:
    """Parse and check a URL's shape.  No name resolution, no addresses."""
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeNetworkTarget(f"invalid URL: {exc}") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeNetworkTarget("URL scheme must be http or https")
    if not hostname:
        raise UnsafeNetworkTarget("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeNetworkTarget("URL credentials are not allowed")
    port = port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return ResolvedEndpoint(
        url=urllib.parse.urlunsplit(parsed),
        scheme=scheme,
        hostname=hostname,
        port=port,
        addresses=(),
        request_target=path,
    )


def _is_literal_address(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return False
    return True


def resolve_public_endpoint(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> ResolvedEndpoint:
    endpoint = _parse_target(url)
    try:
        answers = resolver(
            endpoint.hostname, endpoint.port, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError) as exc:
        raise UnsafeNetworkTarget(f"could not resolve network target: {exc}") from exc
    if not answers:
        raise UnsafeNetworkTarget("network target resolved to no addresses")

    addresses = {
        _canonical_public_address(str(answer[4][0]))
        for answer in answers
        if len(answer) >= 5 and answer[4]
    }
    if not addresses:
        raise UnsafeNetworkTarget("network target resolved to no usable addresses")
    ordered = tuple(
        str(address)
        for address in sorted(
            (ipaddress.ip_address(item) for item in addresses),
            key=lambda item: (item.version, int(item)),
        )
    )
    return replace(endpoint, addresses=ordered)


# ─────────────────────────────────────────────────────────────────────────────
# Proxy selection
# ─────────────────────────────────────────────────────────────────────────────

# `all_proxy` is the conventional catch-all; `https_proxy` wins for https.
_PROXY_ENV_ORDER = {
    "https": ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"),
    "http": ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"),
}


def parse_proxy_url(raw: str) -> Optional[ProxyConfig]:
    """Parse ``http://[user:pass@]host:port``; None when unusable.

    A SOCKS URL is deliberately unusable rather than silently treated as HTTP:
    ``http.client`` cannot speak SOCKS, and sending a plain request to a SOCKS
    port would fail in a far more confusing way.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not host or port is None:
        return None
    return ProxyConfig(
        host=host,
        port=port,
        username=urllib.parse.unquote(parsed.username) if parsed.username else None,
        password=urllib.parse.unquote(parsed.password) if parsed.password else None,
    )


def _no_proxy_matches(hostname: str, raw: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    for entry in raw.split(","):
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry = entry.split(":", 1)[0].lstrip(".").rstrip(".")
        if entry and (hostname == entry or hostname.endswith("." + entry)):
            return True
    return False


def proxy_from_environment(
    url: str,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[ProxyConfig]:
    """The proxy the standard environment variables select for *url*.

    Reads ``https_proxy``/``http_proxy``/``all_proxy`` (upper- and lower-case)
    and honours ``no_proxy``.  Returns None when nothing applies — including
    when the value is unusable, since falling back to a direct connection is
    better than failing outright.
    """
    env = os.environ if environ is None else environ
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname or ""
    except ValueError:
        return None
    if scheme not in _PROXY_ENV_ORDER:
        return None
    no_proxy = env.get("no_proxy") or env.get("NO_PROXY") or ""
    if no_proxy and _no_proxy_matches(hostname, no_proxy):
        return None
    for key in _PROXY_ENV_ORDER[scheme]:
        value = env.get(key)
        if value:
            parsed_proxy = parse_proxy_url(value)
            if parsed_proxy is not None:
                return parsed_proxy
    return None


# Names that a proxy resolves to local infrastructure, and that therefore stay
# blocked even though the proxy — not us — does the resolving.
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".lan",
    ".home.arpa",
    ".arpa",
)


def _reject_local_name(hostname: str) -> None:
    name = hostname.lower().rstrip(".")
    if name == "localhost" or name.endswith(_LOCAL_HOST_SUFFIXES):
        raise UnsafeNetworkTarget(f"network target is local: {hostname}")
    if "." not in name:
        # A single label only resolves through a search domain, and search
        # domains point at the local network by definition.
        raise UnsafeNetworkTarget(f"network target is not fully qualified: {hostname}")


def validate_proxy_target(url: str) -> ResolvedEndpoint:
    """Validate a URL that a proxy will fetch on our behalf.

    The proxy resolves the name, so there is no local answer to check — and
    under a fake-IP resolver (Clash's ``enhanced-mode: fake-ip``) *every* name
    answers with a reserved address, which is exactly the case this path
    exists to serve.  What stays checkable is the name itself and any literal
    address, and those are what a proxy will happily connect to: ``127.0.0.1``,
    ``10.0.0.5``, ``169.254.169.254``, ``localhost``, and a bare ``intranet``.

    The residual gap is deliberate and is the price of using a proxy: a public
    name whose upstream resolution lands on a private address is only visible
    to the proxy, so egress policy for that case belongs to the proxy (Clash
    ships a private-IP ruleset for it).
    """
    endpoint = _parse_target(url)
    if _is_literal_address(endpoint.hostname):
        _canonical_public_address(endpoint.hostname)
    else:
        _reject_local_name(endpoint.hostname)
    return endpoint


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        address: str,
        *,
        timeout: float,
        context: Optional[ssl.SSLContext] = None,
    ) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=context or ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


def _default_connection_factory(
    endpoint: ResolvedEndpoint,
    address: str,
    timeout: float,
) -> Any:
    if endpoint.scheme == "https":
        return _PinnedHTTPSConnection(
            endpoint.hostname,
            endpoint.port,
            address,
            timeout=timeout,
        )
    return http.client.HTTPConnection(address, endpoint.port, timeout=timeout)


def _default_proxied_connection_factory(
    endpoint: ResolvedEndpoint,
    proxy: ProxyConfig,
    timeout: float,
) -> Any:
    """A connection to the proxy, tunnelled when the target is HTTPS.

    Nothing is pinned: the proxy resolves the target, so the address that
    matters is the proxy's own and the OS resolves that one.
    """
    if endpoint.scheme == "https":
        connection = http.client.HTTPSConnection(
            proxy.host, proxy.port, timeout=timeout
        )
        authorization = proxy.authorization
        connection.set_tunnel(
            endpoint.hostname,
            endpoint.port,
            headers=(
                {"Proxy-Authorization": authorization} if authorization else None
            ),
        )
        return connection
    return http.client.HTTPConnection(proxy.host, proxy.port, timeout=timeout)


def _host_header(endpoint: ResolvedEndpoint) -> str:
    hostname = endpoint.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if endpoint.scheme == "https" else 80
    return hostname if endpoint.port == default_port else f"{hostname}:{endpoint.port}"


def _header(response: Any, name: str) -> Optional[str]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return str(value) if value is not None else None


def _read_bounded(
    response: Any,
    *,
    max_bytes: int,
    on_progress: Optional[Callable[[int, Optional[int]], None]],
) -> bytes:
    total_raw = _header(response, "Content-Length")
    try:
        total = int(total_raw) if total_raw else None
    except (TypeError, ValueError):
        total = None
    chunks: list[bytes] = []
    bytes_done = 0
    while bytes_done < max_bytes:
        chunk = response.read(min(64 * 1024, max_bytes - bytes_done))
        if not chunk:
            break
        chunks.append(chunk)
        bytes_done += len(chunk)
        if on_progress is not None:
            on_progress(bytes_done, total)
    return b"".join(chunks)


def fetch_public_http_url(
    url: str,
    *,
    timeout: float = 20,
    max_bytes: int = 512 * 1024,
    max_redirects: int = 5,
    resolver: Resolver = socket.getaddrinfo,
    connection_factory: ConnectionFactory = _default_connection_factory,
    proxied_connection_factory: ProxiedConnectionFactory = (
        _default_proxied_connection_factory
    ),
    proxy: Optional[ProxyConfig] = None,
    trust_env: bool = False,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    headers: Optional[Mapping[str, str]] = None,
    on_headers_dropped: Optional[Callable[[tuple[str, ...], str], None]] = None,
) -> FetchResponse:
    """Fetch *url* directly, or through a proxy when one applies.

    ``proxy`` is used verbatim when given.  Otherwise ``trust_env`` decides
    whether the standard environment variables are consulted.  The default is
    to ignore them: this is a security boundary, so it must not silently change
    which address it dials because of ambient configuration — a caller that
    wants the machine's proxy asks for it.

    The two paths validate differently on purpose.  Direct fetches resolve
    first and refuse any non-global answer, pinning the socket to the address
    that passed.  Proxied fetches never see the target's address, so they check
    the name and any literal instead — see ``validate_proxy_target``.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects cannot be negative")
    current_url = url
    origin: Optional[tuple[str, str, int]] = None
    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersonalAgent/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        **dict(headers or {}),
    }

    for hop in range(max_redirects + 1):
        # The proxy is chosen per hop: a redirect may change the scheme or land
        # on a host that no_proxy exempts.
        hop_proxy = proxy
        if hop_proxy is None and trust_env:
            hop_proxy = proxy_from_environment(current_url)

        if hop_proxy is None:
            # Resolve once, validate, then connect to a validated address.  This
            # single step is what defeats DNS rebinding: the socket is pinned to an
            # address that passed _canonical_public_address, so no second lookup can
            # redirect it.  Resolving twice and comparing answer sets added no
            # protection on top of pinning — and did reject safe traffic, because a
            # round-robin CDN legitimately returns a different answer set on
            # consecutive queries.
            endpoint = resolve_public_endpoint(current_url, resolver=resolver)
            request_target = endpoint.request_target
            connection = connection_factory(endpoint, endpoint.addresses[0], timeout)
        else:
            endpoint = validate_proxy_target(current_url)
            # A proxy wants the absolute URI for a plain-HTTP request; an HTTPS
            # request carries only the path, inside the tunnel.
            request_target = (
                endpoint.url if endpoint.scheme == "http" else endpoint.request_target
            )
            connection = proxied_connection_factory(endpoint, hop_proxy, timeout)

        if origin is None:
            origin = _origin(endpoint)
        elif _origin(endpoint) != origin:
            # The server chose this destination, so caller credentials stop here.
            request_headers, dropped = _strip_origin_bound_headers(request_headers)
            if dropped and on_headers_dropped is not None:
                on_headers_dropped(dropped, current_url)
            origin = _origin(endpoint)

        response = None
        try:
            hop_headers = {**request_headers, "Host": _host_header(endpoint)}
            if hop_proxy is not None and endpoint.scheme == "http":
                # Proxy credentials belong to the proxy, not the target, so they
                # are added per hop and never travel in request_headers.
                authorization = hop_proxy.authorization
                if authorization:
                    hop_headers["Proxy-Authorization"] = authorization
            connection.request("GET", request_target, headers=hop_headers)
            response = connection.getresponse()
            status = int(response.status)
            if status in {301, 302, 303, 307, 308}:
                location = _header(response, "Location")
                if not location:
                    raise UnsafeNetworkTarget("redirect response is missing Location")
                if hop == max_redirects:
                    raise UnsafeNetworkTarget("too many redirects")
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            body = _read_bounded(
                response,
                max_bytes=max_bytes,
                on_progress=on_progress,
            )
            response_headers = {
                str(key): str(value)
                for key, value in getattr(response, "headers", {}).items()
            }
            return FetchResponse(
                body=body,
                final_url=current_url,
                status=status,
                headers=response_headers,
            )
        finally:
            if response is not None:
                response.close()
            connection.close()

    raise UnsafeNetworkTarget("too many redirects")
