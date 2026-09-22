"""Provider configuration as a first-class setting, not a JSON chore.

The settings page used to offer a handful of fields and a raw JSON box: adding
a provider, changing a base URL, or giving one gateway extra headers meant
editing the file by hand.  These tests hold the two properties that make the
replacement generic rather than a new hard-coded form:

* the field vocabulary is the set of keys the loader actually reads -- so a
  field cannot exist in the code and be missing from the form, or vice versa;
* the page is told the vocabulary by the backend, so it renders a field it has
  never heard of without a frontend change.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent.config import (
    PROVIDER_FIELDS,
    _validate_config,
    provider_fields_payload,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _provider_keys_read_by_the_loader() -> dict[str, set[str]]:
    """Every ``<provider dict>.get("key")`` in the agent package.

    A source scan rather than a runtime probe because the question is about
    code that has not run: the key a loader would read for a config nobody has
    written yet.  The variable names are the codebase's convention for a
    provider entry; files that reuse one for something else are listed in
    ``_NOT_A_PROVIDER``.
    """
    pattern = re.compile(r'(?:provider_cfg|pcfg|provider)\.get\(\s*"([a-z_]+)"\s*[,)]')
    found: dict[str, set[str]] = {}
    for path in (_REPO_ROOT / "agent").rglob("*.py"):
        relative = str(path.relative_to(_REPO_ROOT))
        for match in pattern.finditer(path.read_text()):
            key = match.group(1)
            if key in _NOT_A_PROVIDER.get(relative, set()):
                continue
            found.setdefault(key, set()).add(relative)
    return found


#: ``pcfg`` there is a *plugin's* config, not a provider entry.
_NOT_A_PROVIDER = {"agent/plugins/catalog.py": {"enabled"}}


def test_provider_fields_are_the_keys_the_code_reads():
    """The table and the loader describe the same config, in both directions.

    One direction catches a field the form offers and the loader ignores ("a
    setting the user believes in"); the other catches a key the loader reads
    and the table never declared -- which would make a working config report
    an unknown key, and would keep the field out of the form for ever.
    """
    declared = {field.key for field in PROVIDER_FIELDS}
    read = set(_provider_keys_read_by_the_loader())
    assert read - declared == set(), (
        "the loader reads provider keys the field table does not declare: "
        f"{sorted(read - declared)}"
    )
    assert declared - read == set(), (
        "the field table declares provider keys nothing reads: "
        f"{sorted(declared - read)}"
    )


def test_the_example_config_uses_only_declared_keys():
    """A shipped example that warns on startup is a bad first impression."""
    example = json.loads((_REPO_ROOT / "config.example.json").read_text())
    unknown = [
        warning
        for warning in _validate_config(example)
        if "unknown provider key" in warning
    ]
    assert unknown == []
    assert _validate_config(example) == []


def test_the_schema_payload_describes_every_field():
    payload = provider_fields_payload()
    assert [row["key"] for row in payload] == [f.key for f in PROVIDER_FIELDS]
    headers = next(row for row in payload if row["key"] == "headers")
    assert headers["kind"] == "string_map"
    # Values under a secret-looking name must be hidden when the block is sent
    # to a browser -- an Authorization header is a credential.
    assert headers["secret_values"] is True
    api_key = next(row for row in payload if row["key"] == "api_key")
    assert api_key["secret"] is True and api_key["required"] is True


def test_a_typed_field_is_checked_from_the_table_alone():
    """Adding a field to the table is what makes it validated."""
    cfg = {
        "providers": {
            "p": {
                "api_format": "openai",
                "api_key": "k",
                "default_model": "m",
                "max_tokens": "many",
                "supports_vision": "yes",
                "models": [1, 2],
            }
        }
    }
    warnings = _validate_config(cfg)
    assert any("max_tokens: must be an integer" in w for w in warnings)
    assert any("supports_vision: must be true or false" in w for w in warnings)
    assert any("models: must be a list of strings" in w for w in warnings)


def test_an_undeclared_provider_key_is_reported():
    cfg = {
        "providers": {
            "p": {"api_format": "openai", "api_key": "k", "default_model": "m", "opencode": True}
        }
    }
    warnings = _validate_config(cfg)
    assert any("unknown provider key" in w and "opencode" in w for w in warnings)


# ── The HTTP surface ──────────────────────────────────────────────────────


def _channel():
    from agent.channels.web import WebChannel, WebConfig

    channel = WebChannel(
        WebConfig(
            enabled=True, host="127.0.0.1", port=8787, auth_token="", cors_origins=()
        )
    )
    channel.bind_runtime({}, {})
    return channel


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Point the config at a temp file and return a reader for it."""
    import agent.shared as shared

    path = tmp_path / "config.json"
    monkeypatch.setattr(shared, "resolve_config_file", lambda: path)
    return path


def _seed(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_config_endpoint_publishes_the_provider_vocabulary(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {"active_provider": "a", "providers": {"a": {"api_format": "openai", "api_key": "sk-1", "default_model": "m"}}},
    )
    with TestClient(_channel().app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert [row["key"] for row in body["provider_fields"]] == [
        f.key for f in PROVIDER_FIELDS
    ]


def test_a_provider_can_be_added_without_touching_any_other_one(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "a",
            "providers": {"a": {"api_format": "openai", "api_key": "sk-1", "default_model": "m"}},
        },
    )
    with TestClient(_channel().app) as client:
        resp = client.post(
            "/api/providers/opencode-go",
            json={
                "provider": {
                    "api_format": "openai",
                    "api_key": "$OPENCODE_GO_API_KEY",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "default_model": "kimi-k2.7-code",
                    "models": ["kimi-k2.7-code"],
                    "headers": {"x-opencode-session": "{session}"},
                }
            },
        )
    assert resp.status_code == 200, resp.text
    cfg = _read(config_file)
    assert cfg["providers"]["opencode-go"]["headers"] == {
        "x-opencode-session": "{session}"
    }
    # The provider that was already there is untouched.
    assert cfg["providers"]["a"] == {
        "api_format": "openai",
        "api_key": "sk-1",
        "default_model": "m",
    }


def test_a_patch_changes_only_what_it_names(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "a",
            "providers": {
                "a": {
                    "api_format": "openai",
                    "api_key": "sk-1",
                    "default_model": "m",
                    "max_tokens": 4096,
                    "models": ["m"],
                }
            },
        },
    )
    with TestClient(_channel().app) as client:
        resp = client.post("/api/providers/a", json={"provider": {"max_tokens": 8192}})
    assert resp.status_code == 200, resp.text
    provider = _read(config_file)["providers"]["a"]
    assert provider["max_tokens"] == 8192
    assert provider["api_key"] == "sk-1"
    assert provider["models"] == ["m"]


def test_a_write_that_would_be_ignored_at_runtime_is_refused(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {"active_provider": "a", "providers": {"a": {"api_format": "openai", "api_key": "sk-1", "default_model": "m"}}},
    )
    with TestClient(_channel().app) as client:
        unknown = client.post("/api/providers/a", json={"provider": {"opencode": True}})
        bad_format = client.post("/api/providers/a", json={"provider": {"api_format": "grpc"}})
        bad_type = client.post("/api/providers/a", json={"provider": {"max_tokens": "lots"}})
        empty = client.post(
            "/api/providers/new-one", json={"provider": {"api_format": "openai"}}
        )
        no_model = client.post(
            "/api/providers/new-two",
            json={"provider": {"api_format": "openai", "api_key": "k"}},
        )
    assert unknown.status_code == 400 and "opencode" in unknown.json()["error"]
    assert bad_format.status_code == 400 and "api_format" in bad_format.json()["error"]
    assert bad_type.status_code == 400
    # Required-ness comes from the field table, so the message names whichever
    # required field is missing rather than a special case per field.
    assert empty.status_code == 400 and "api_key" in empty.json()["error"]
    assert no_model.status_code == 400 and "default_model" in no_model.json()["error"]
    # None of it was written.
    cfg = _read(config_file)
    assert "new-one" not in cfg["providers"]
    assert cfg["providers"]["a"]["api_format"] == "openai"


def test_credentials_are_masked_on_read_and_survive_a_round_trip(config_file):
    """A masked value that is saved back must not overwrite the real one."""
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "opencode-go",
            "providers": {
                "opencode-go": {
                    "api_format": "openai",
                    "api_key": "sk-real",
                    "default_model": "m",
                    "headers": {
                        "Authorization": "Bearer real-token",
                        "User-Agent": "zcode/1.0",
                    },
                }
            },
        },
    )
    with TestClient(_channel().app) as client:
        shown = client.get("/api/config").json()["config"]["providers"]["opencode-go"]
        assert shown["api_key"] == "******"
        assert shown["headers"]["Authorization"] == "******"
        # Not a credential by name, so it stays readable: the page is a form,
        # and a form whose values are all stars cannot be edited.
        assert shown["headers"]["User-Agent"] == "zcode/1.0"

        saved = client.post(
            "/api/providers/opencode-go",
            json={"provider": {"headers": shown["headers"], "api_key": "******"}},
        )
    assert saved.status_code == 200, saved.text
    provider = _read(config_file)["providers"]["opencode-go"]
    assert provider["api_key"] == "sk-real"
    assert provider["headers"]["Authorization"] == "Bearer real-token"


def test_activating_a_provider_switches_the_model_that_moved_with_it(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "a",
            "model": "model-a",
            "providers": {
                "a": {"api_format": "openai", "api_key": "k", "default_model": "model-a", "models": ["model-a"]},
                "b": {"api_format": "openai", "api_key": "k", "default_model": "model-b", "models": ["model-b"]},
            },
        },
    )
    with TestClient(_channel().app) as client:
        resp = client.post("/api/providers/b/activate")
    assert resp.status_code == 200, resp.text
    cfg = _read(config_file)
    assert cfg["active_provider"] == "b"
    # The stale id would have routed to whichever provider owns it.
    assert cfg["model"] == "model-b"


def test_deleting_is_refused_for_the_active_and_the_last_provider(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "a",
            "providers": {
                "a": {"api_format": "openai", "api_key": "k", "default_model": "m"},
                "b": {"api_format": "openai", "api_key": "k", "default_model": "m2"},
            },
        },
    )
    with TestClient(_channel().app) as client:
        active = client.delete("/api/providers/a")
        removed = client.delete("/api/providers/b")
        # Now only `a` is left, and it is also the active one: both reasons
        # refuse, and the config still holds a provider either way.
        last = client.delete("/api/providers/a")
    assert active.status_code == 400 and "当前使用" in active.json()["error"]
    assert removed.status_code == 200
    assert last.status_code == 400
    assert list(_read(config_file)["providers"]) == ["a"]


def test_the_connection_test_goes_through_the_providers_own_configuration(
    config_file, tmp_path
):
    """The button has to exercise the real path, headers included.

    A check that built its own request would pass while the agent's own
    requests failed -- which is exactly what it is for.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            self.rfile.read(length)
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "kimi-k2.7-code",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "pong"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from starlette.testclient import TestClient

        _seed(
            config_file,
            {
                "active_provider": "a",
                "providers": {
                    "a": {"api_format": "openai", "api_key": "k", "default_model": "m"},
                    "gw": {
                        "api_format": "openai",
                        "api_key": "sk-gw",
                        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                        "default_model": "kimi-k2.7-code",
                        "headers": {"x-opencode-session": "{session}"},
                    },
                },
            },
        )
        with TestClient(_channel().app) as client:
            resp = client.post("/api/providers/gw/test")
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["ok"] is True, result
        assert result["model"] == "kimi-k2.7-code"
        # The per-conversation header still goes out; with no conversation to
        # name it carries the process's own stable id.
        from agent import shared

        assert seen and seen[0]["x-opencode-session"] == shared.instance_id()
        assert seen[0]["authorization"] == "Bearer sk-gw"
    finally:
        server.shutdown()
        server.server_close()


def test_the_connection_test_reports_a_failure_instead_of_raising(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {
            "active_provider": "a",
            "providers": {
                "a": {
                    "api_format": "openai",
                    "api_key": "sk-x",
                    # Nothing listens here: the failure has to come back as a
                    # result the page can show, not as a 500.
                    "base_url": "http://127.0.0.1:9/v1",
                    "default_model": "m",
                }
            },
        },
    )
    with TestClient(_channel().app) as client:
        resp = client.post("/api/providers/a/test")
    assert resp.status_code == 200
    result = resp.json()
    assert result["ok"] is False
    assert result["error"]
    assert result["latency_ms"] >= 0


def test_an_unknown_provider_is_a_404_everywhere(config_file):
    from starlette.testclient import TestClient

    _seed(
        config_file,
        {"active_provider": "a", "providers": {"a": {"api_format": "openai", "api_key": "k", "default_model": "m"}}},
    )
    with TestClient(_channel().app) as client:
        assert client.delete("/api/providers/nope").status_code == 404
        assert client.post("/api/providers/nope/activate").status_code == 404
        assert client.post("/api/providers/nope/test").status_code == 404
