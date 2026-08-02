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
