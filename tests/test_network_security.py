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


def test_fetch_rejects_dns_rebinding_before_connect():
    from agent.security.network import UnsafeNetworkTarget, fetch_public_http_url

    answers = iter(
        [
            _resolver_for("93.184.216.34"),
            _resolver_for("93.184.216.35"),
        ]
    )

    def changing_resolver(host, port, **kwargs):
        return next(answers)(host, port, **kwargs)

    called = False

    def factory(endpoint, address, timeout):
        nonlocal called
        called = True
        return _FakeConnection(_FakeResponse())

    with pytest.raises(UnsafeNetworkTarget, match="DNS answers changed"):
        fetch_public_http_url(
            "https://example.com/",
            resolver=changing_resolver,
            connection_factory=factory,
        )
    assert called is False


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
