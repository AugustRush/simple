import socket

import pytest


def _resolver_for(*addresses):
    def resolve(host, port, **kwargs):
        results = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            results.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return results

    return resolve


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "192.0.2.1",
        "0.0.0.0",
        "::1",
        "::",
        "::ffff:127.0.0.1",
    ],
)
def test_resolve_public_endpoint_rejects_non_public_literals(host):
    from agent.security.network import UnsafeNetworkTarget, resolve_public_endpoint

    url = f"http://[{host}]/" if ":" in host else f"http://{host}/"
    with pytest.raises(UnsafeNetworkTarget):
        resolve_public_endpoint(url)


def test_resolve_public_endpoint_rejects_mixed_dns_answers():
    from agent.security.network import UnsafeNetworkTarget, resolve_public_endpoint

    resolver = _resolver_for("93.184.216.34", "127.0.0.1")
    with pytest.raises(UnsafeNetworkTarget):
        resolve_public_endpoint("https://example.com/", resolver=resolver)


@pytest.mark.parametrize(
    "url",
    ["", "example.com", "file:///etc/passwd", "ftp://example.com/a", "http:///missing"],
)
def test_resolve_public_endpoint_rejects_invalid_urls(url):
    from agent.security.network import UnsafeNetworkTarget, resolve_public_endpoint

    with pytest.raises(UnsafeNetworkTarget):
        resolve_public_endpoint(url, resolver=_resolver_for("93.184.216.34"))


class _FakeResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status = status
        self._body = body
        self._offset = 0
        self.headers = headers or {}
        self.closed = False

    def read(self, size):
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, path, headers=None):
        self.requests.append((method, path, headers or {}))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_fetch_rejects_redirect_to_loopback():
    from agent.security.network import UnsafeNetworkTarget, fetch_public_http_url

    public = _resolver_for("93.184.216.34")
    connections = []

    def factory(endpoint, address, timeout):
        response = _FakeResponse(302, headers={"Location": "http://127.0.0.1/private"})
        connection = _FakeConnection(response)
        connections.append((endpoint, address, connection))
        return connection

    with pytest.raises(UnsafeNetworkTarget):
        fetch_public_http_url(
            "https://example.com/start",
            resolver=public,
            connection_factory=factory,
        )
    assert connections[0][1] == "93.184.216.34"


def test_fetch_connects_only_to_a_validated_address_under_unstable_dns():
    """Pinning, not answer-set comparison, is what defeats DNS rebinding.

    The socket is pinned to an address that already passed validation, so a
    later lookup cannot redirect it.  This previously resolved twice and
    rejected any change in the answer set — which protected nothing beyond
    pinning and *did* reject safe traffic, since a round-robin CDN legitimately
    returns a different public address on consecutive queries.
    """
    from agent.security.network import fetch_public_http_url

    rotation = iter(
        [
            _resolver_for("93.184.216.34"),
            _resolver_for("93.184.216.35"),
        ]
    )

    def round_robin(host, port, **kwargs):
        return next(rotation)(host, port, **kwargs)

    connections = []

    def factory(endpoint, address, timeout):
        connections.append(address)
        return _FakeConnection(_FakeResponse())

    result = fetch_public_http_url(
        "https://cdn.example.com/",
        resolver=round_robin,
        connection_factory=factory,
    )
    assert result.status == 200
    # Whatever answer was used, it is one that passed public-address validation.
    assert connections == ["93.184.216.34"]


def test_fetch_refuses_a_private_address_without_connecting():
    """The invariant that matters: no socket is opened to a non-public address."""
    from agent.security.network import UnsafeNetworkTarget, fetch_public_http_url

    connections = []

    def factory(endpoint, address, timeout):
        connections.append(address)
        return _FakeConnection(_FakeResponse())

    for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1"):
        with pytest.raises(UnsafeNetworkTarget):
            fetch_public_http_url(
                "https://evil.example.com/",
                resolver=_resolver_for(address),
                connection_factory=factory,
            )
    assert connections == [], "a connection was attempted to a private address"


def test_fetch_follows_relative_redirect_and_pins_validated_address():
    from agent.security.network import fetch_public_http_url

    resolver = _resolver_for("93.184.216.34")
    responses = iter(
        [
            _FakeResponse(302, headers={"Location": "/final"}),
            _FakeResponse(200, body=b"done"),
        ]
    )
    calls = []

    def factory(endpoint, address, timeout):
        connection = _FakeConnection(next(responses))
        calls.append((endpoint, address, connection))
        return connection

    result = fetch_public_http_url(
        "https://example.com/start",
        resolver=resolver,
        connection_factory=factory,
    )

    assert result.body == b"done"
    assert result.final_url == "https://example.com/final"
    assert [address for _, address, _ in calls] == ["93.184.216.34"] * 2
    assert calls[0][2].requests[0][2]["Host"] == "example.com"
    assert calls[1][2].requests[0][1] == "/final"
    assert all(connection.closed for _, _, connection in calls)


def test_fetch_rejects_more_than_five_redirects():
    from agent.security.network import UnsafeNetworkTarget, fetch_public_http_url

    resolver = _resolver_for("93.184.216.34")

    def factory(endpoint, address, timeout):
        return _FakeConnection(_FakeResponse(302, headers={"Location": "/again"}))

    with pytest.raises(UnsafeNetworkTarget, match="too many redirects"):
        fetch_public_http_url(
            "http://example.com/start",
            resolver=resolver,
            connection_factory=factory,
        )


# ── Caller credentials must not follow a server-chosen redirect ─────────────


def _recording_redirect_factory(seen, location):
    """Connection factory whose first response redirects, second succeeds."""
    state = {"hops": 0}

    class _Conn:
        def __init__(self, address, port, timeout):
            self.port = port

        def request(self, method, target, headers=None):
            seen.append(dict(headers or {}))

        def getresponse(self):
            state["hops"] += 1
            if state["hops"] == 1:
                return _FakeResponse(302, headers={"Location": location})
            return _FakeResponse(200, body=b"ok")

        def close(self):
            return None

    return lambda endpoint, address, timeout: _Conn(address, endpoint.port, timeout)


def test_authorization_is_dropped_on_a_cross_origin_redirect():
    """A redirect target is chosen by the server, not the caller.

    Replaying caller credentials to it hands them to whoever controls the
    redirect — so Authorization/Cookie stop at an origin change.
    """
    from agent.security.network import fetch_public_http_url

    seen: list[dict] = []
    dropped: list[tuple] = []

    def resolver(host, port, **kwargs):
        return _resolver_for("93.184.216.34")(host, port, **kwargs)

    fetch_public_http_url(
        "https://trusted.example.com/x",
        resolver=resolver,
        connection_factory=_recording_redirect_factory(
            seen, "https://attacker.example.net/steal"
        ),
        headers={"Authorization": "Bearer SECRET", "Cookie": "session=abc"},
        on_headers_dropped=lambda names, url: dropped.append((names, url)),
    )

    assert seen[0]["Host"] == "trusted.example.com"
    assert "SECRET" in seen[0]["Authorization"]
    assert seen[1]["Host"] == "attacker.example.net"
    assert "Authorization" not in seen[1]
    assert "Cookie" not in seen[1]
    assert dropped and set(dropped[0][0]) == {"Authorization", "Cookie"}


def test_authorization_survives_a_same_origin_redirect():
    from agent.security.network import fetch_public_http_url

    seen: list[dict] = []
    fetch_public_http_url(
        "https://same.example.com/a",
        resolver=_resolver_for("93.184.216.34"),
        connection_factory=_recording_redirect_factory(
            seen, "https://same.example.com/b"
        ),
        headers={"Authorization": "Bearer KEEP"},
    )
    assert "KEEP" in seen[1]["Authorization"]


def test_redirect_to_a_private_address_is_rejected():
    from agent.security.network import UnsafeNetworkTarget, fetch_public_http_url

    seen: list[dict] = []

    def resolver(host, port, **kwargs):
        address = "169.254.169.254" if "internal" in host else "93.184.216.34"
        return _resolver_for(address)(host, port, **kwargs)

    with pytest.raises(UnsafeNetworkTarget, match="not globally routable"):
        fetch_public_http_url(
            "https://ok.example.com/a",
            resolver=resolver,
            connection_factory=_recording_redirect_factory(
                seen, "https://internal.example.com/admin"
            ),
        )


# ── Proxy routing ───────────────────────────────────────────────────────────


def test_parse_proxy_url_reads_host_port_and_credentials():
    from agent.security.network import parse_proxy_url

    plain = parse_proxy_url("http://127.0.0.1:7897")
    assert (plain.host, plain.port) == ("127.0.0.1", 7897)
    assert plain.authorization is None

    authed = parse_proxy_url("http://user:p%40ss@proxy.example:3128")
    assert (authed.host, authed.port) == ("proxy.example", 3128)
    assert authed.username == "user"
    assert authed.password == "p@ss"
    assert authed.authorization.startswith("Basic ")

    # A bare host:port is accepted; the scheme defaults to http.
    assert parse_proxy_url("proxy.example:3128").port == 3128


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "socks5://127.0.0.1:1080",
        "socks4://127.0.0.1:1080",
        "http://host-without-port",
        "not a url",
    ],
)
def test_parse_proxy_url_rejects_unusable_values(raw):
    """SOCKS is refused rather than misread: http.client cannot speak it."""
    from agent.security.network import parse_proxy_url

    assert parse_proxy_url(raw) is None


def test_proxy_from_environment_picks_the_scheme_specific_variable():
    from agent.security.network import proxy_from_environment

    env = {
        "https_proxy": "http://127.0.0.1:7897",
        "http_proxy": "http://127.0.0.1:7899",
        "all_proxy": "http://127.0.0.1:7898",
    }
    assert proxy_from_environment("https://example.com/", env).port == 7897
    assert proxy_from_environment("http://example.com/", env).port == 7899
    # all_proxy is the catch-all when no scheme-specific variable is set.
    assert proxy_from_environment("https://e.com/", {"ALL_PROXY": "http://p:1"}).port == 1
    assert proxy_from_environment("https://example.com/", {}) is None


def test_proxy_from_environment_honours_no_proxy():
    from agent.security.network import proxy_from_environment

    env = {"https_proxy": "http://127.0.0.1:7897", "no_proxy": "internal.example, .corp"}
    assert proxy_from_environment("https://internal.example/x", env) is None
    assert proxy_from_environment("https://a.corp/x", env) is None
    assert proxy_from_environment("https://public.example/x", env) is not None
    assert proxy_from_environment("https://e.com/", {**env, "no_proxy": "*"}) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/admin",
        "http://foo.localhost/admin",
        "http://router/",
        "http://intranet/admin",
        "http://printer.local/",
        "http://db.internal/",
    ],
)
def test_validate_proxy_target_rejects_local_destinations(url):
    """A proxy will happily reach these, so the check moves to the name."""
    from agent.security.network import UnsafeNetworkTarget, validate_proxy_target

    with pytest.raises(UnsafeNetworkTarget):
        validate_proxy_target(url)


def test_validate_proxy_target_never_resolves_the_name():
    """The point of the path: a fake-IP resolver must not be consulted at all.

    Under Clash's ``enhanced-mode: fake-ip`` every name answers 198.18.x.x,
    which the direct path correctly refuses.  A proxied fetch never asks.
    """
    from agent.security.network import validate_proxy_target

    endpoint = validate_proxy_target("https://example.com/a?b=1")
    assert (endpoint.scheme, endpoint.hostname, endpoint.port) == (
        "https",
        "example.com",
        443,
    )
    assert endpoint.request_target == "/a?b=1"
    assert endpoint.addresses == ()


def test_validate_proxy_target_accepts_a_public_literal():
    from agent.security.network import validate_proxy_target

    assert validate_proxy_target("http://93.184.216.34/").hostname == "93.184.216.34"


def _proxied_factory(seen, responses):
    """A proxied connection factory that records instead of opening a socket."""

    class _Conn:
        def __init__(self, endpoint, proxy):
            self.endpoint = endpoint
            self.proxy = proxy
            self.requests = []
            self.closed = False

        def request(self, method, target, headers=None):
            self.requests.append((method, target, headers or {}))

        def getresponse(self):
            return next(responses)

        def close(self):
            self.closed = True

    def factory(endpoint, proxy, timeout):
        connection = _Conn(endpoint, proxy)
        seen.append(connection)
        return connection

    return factory


def test_fetch_through_a_proxy_never_resolves_the_target():
    from agent.security.network import ProxyConfig, fetch_public_http_url

    seen: list = []
    result = fetch_public_http_url(
        "https://example.com/x",
        resolver=lambda *a, **k: pytest.fail("the proxy path must not resolve"),
        connection_factory=lambda *a: pytest.fail("the direct path must not run"),
        proxied_connection_factory=_proxied_factory(
            seen, iter([_FakeResponse(200, body=b"ok")])
        ),
        proxy=ProxyConfig(host="127.0.0.1", port=7897),
    )

    assert result.body == b"ok"
    assert len(seen) == 1
    assert seen[0].proxy.port == 7897
    # HTTPS carries only the path; the tunnel names the target.
    assert seen[0].requests[0][1] == "/x"
    assert seen[0].requests[0][2]["Host"] == "example.com"
    assert seen[0].closed is True


def test_plain_http_through_a_proxy_sends_the_absolute_uri():
    from agent.security.network import ProxyConfig, fetch_public_http_url

    seen: list = []
    fetch_public_http_url(
        "http://example.com/x?y=1",
        connection_factory=lambda *a: pytest.fail("the direct path must not run"),
        proxied_connection_factory=_proxied_factory(seen, iter([_FakeResponse(200)])),
        proxy=ProxyConfig(host="127.0.0.1", port=7897),
    )
    assert seen[0].requests[0][1] == "http://example.com/x?y=1"
    assert seen[0].requests[0][2]["Host"] == "example.com"


def test_proxy_credentials_travel_to_the_proxy_on_plain_http():
    from agent.security.network import ProxyConfig, fetch_public_http_url

    seen: list = []
    fetch_public_http_url(
        "http://example.com/",
        proxied_connection_factory=_proxied_factory(seen, iter([_FakeResponse(200)])),
        proxy=ProxyConfig(host="127.0.0.1", port=7897, username="u", password="p"),
    )
    assert seen[0].requests[0][2]["Proxy-Authorization"].startswith("Basic ")


def test_default_proxied_connection_tunnels_https_and_targets_the_proxy():
    import http.client

    from agent.security.network import (
        ProxyConfig,
        _default_proxied_connection_factory,
        validate_proxy_target,
    )

    proxy = ProxyConfig(host="127.0.0.1", port=7897, username="u", password="p")

    https = _default_proxied_connection_factory(
        validate_proxy_target("https://example.com/"), proxy, 5
    )
    assert isinstance(https, http.client.HTTPSConnection)
    assert (https.host, https.port) == ("127.0.0.1", 7897)
    assert (https._tunnel_host, https._tunnel_port) == ("example.com", 443)
    assert https._tunnel_headers["Proxy-Authorization"].startswith("Basic ")

    plain = _default_proxied_connection_factory(
        validate_proxy_target("http://example.com/"), proxy, 5
    )
    assert isinstance(plain, http.client.HTTPConnection)
    assert (plain.host, plain.port) == ("127.0.0.1", 7897)


def test_fetch_ignores_the_environment_unless_asked(monkeypatch):
    """A security boundary must not change its destination from ambient config."""
    from agent.security.network import fetch_public_http_url

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")

    seen: list = []
    direct: list = []

    def direct_factory(endpoint, address, timeout):
        direct.append(endpoint.hostname)
        return _FakeConnection(_FakeResponse(200))

    fetch_public_http_url(
        "https://example.com/",
        resolver=_resolver_for("93.184.216.34"),
        connection_factory=direct_factory,
        proxied_connection_factory=_proxied_factory(seen, iter([])),
    )
    assert direct == ["example.com"]
    assert seen == []

    fetch_public_http_url(
        "https://example.com/",
        trust_env=True,
        resolver=lambda *a, **k: pytest.fail("the proxy path must not resolve"),
        connection_factory=lambda *a: pytest.fail("the direct path must not run"),
        proxied_connection_factory=_proxied_factory(seen, iter([_FakeResponse(200)])),
    )
    assert len(seen) == 1


def test_proxied_redirect_to_another_public_host_is_followed():
    from agent.security.network import ProxyConfig, fetch_public_http_url

    seen: list = []
    responses = iter(
        [
            _FakeResponse(302, headers={"Location": "https://other.example.net/x"}),
            _FakeResponse(200, body=b"done"),
        ]
    )
    result = fetch_public_http_url(
        "https://example.com/start",
        proxied_connection_factory=_proxied_factory(seen, responses),
        proxy=ProxyConfig(host="127.0.0.1", port=7897),
    )
    assert result.body == b"done"
    assert result.final_url == "https://other.example.net/x"
    assert [c.endpoint.hostname for c in seen] == ["example.com", "other.example.net"]


def test_redirect_to_the_metadata_service_is_refused_on_the_proxy_path():
    """The proxy path still refuses a server-chosen hop to a local address."""
    from agent.security.network import ProxyConfig, UnsafeNetworkTarget, fetch_public_http_url

    seen: list = []
    responses = iter(
        [
            _FakeResponse(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        ]
    )
    with pytest.raises(UnsafeNetworkTarget):
        fetch_public_http_url(
            "https://example.com/start",
            proxied_connection_factory=_proxied_factory(seen, responses),
            proxy=ProxyConfig(host="127.0.0.1", port=7897),
        )
    assert len(seen) == 1, "the redirect must be refused before a second connection"
