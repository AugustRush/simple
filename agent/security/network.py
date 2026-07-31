from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
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
class FetchResponse:
    body: bytes
    final_url: str
    status: int
    headers: Mapping[str, str]


Resolver = Callable[..., list[tuple[Any, ...]]]
ConnectionFactory = Callable[[ResolvedEndpoint, str, float], Any]


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


def resolve_public_endpoint(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> ResolvedEndpoint:
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

    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM)
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
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return ResolvedEndpoint(
        url=urllib.parse.urlunsplit(parsed),
        scheme=scheme,
        hostname=hostname,
        port=port,
        addresses=ordered,
        request_target=path,
    )


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
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
    headers: Optional[Mapping[str, str]] = None,
) -> FetchResponse:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects cannot be negative")
    current_url = url
    request_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PersonalAgent/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        **dict(headers or {}),
    }

    for hop in range(max_redirects + 1):
        first = resolve_public_endpoint(current_url, resolver=resolver)
        endpoint = resolve_public_endpoint(current_url, resolver=resolver)
        if first.addresses != endpoint.addresses:
            raise UnsafeNetworkTarget("DNS answers changed before connection")

        connection = connection_factory(endpoint, endpoint.addresses[0], timeout)
        response = None
        try:
            hop_headers = {**request_headers, "Host": _host_header(endpoint)}
            connection.request("GET", endpoint.request_target, headers=hop_headers)
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
