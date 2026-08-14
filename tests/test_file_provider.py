"""The file seam: injection must actually win.

`FileService` was already a provider in practice — bootstrap builds one and
hands it to the tools, which forward four calls to it.  What was missing was
a written interface and a guarantee that nothing quietly substitutes its own.

The hole these tests close: `BuiltinTools` built its own `FileService` with
default policy whenever the constructor argument was absent, ignoring one
already published on the registry.  A caller who injected through the
registry got a *second* service with a policy nobody chose — and since the
default policy is not the configured one, that is a security-relevant
substitution, not just an inefficiency.
"""

from __future__ import annotations

from typing import Any

from agent.tools.files import FileProvider, FileService


class RecordingFileProvider:
    """Answers every call the same way, and remembers being called."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_file(self, root, path, *, start_line=1, line_count=None):
        self.calls.append("read_file")
        return {"ok": True, "who": "recording"}

    def write_file(self, root, path, *, mode, content, expected_revision=None):
        self.calls.append("write_file")
        return {"ok": True, "who": "recording"}

    def edit_file(self, root, path, *, expected_revision, replacements):
        self.calls.append("edit_file")
        return {"ok": True, "who": "recording"}

    def list_files(
        self, root, path=".", *, recursive=False, pattern="*", cursor=None,
        max_results=None,
    ):
        self.calls.append("list_files")
        return {"ok": True, "who": "recording"}


def _tools(tmp_path, registry, **kwargs):
    from agent import BuiltinTools, MemoryPalace

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    return BuiltinTools(
        memory=MemoryPalace(
            base_dir=tmp_path / "memory",
            context_dir=tmp_path / "context",
        ),
        registry=registry,
        workspace_root=workspace,
        output_dir=output,
        **kwargs,
    )


def test_file_service_satisfies_the_provider_protocol():
    """The protocol describes the thing that exists, not an aspiration."""
    assert isinstance(FileService, type)
    assert issubclass(FileService, FileProvider)


def test_a_registry_injected_provider_is_not_bypassed(tmp_path):
    """The hole: tools used to build their own service and ignore this one."""
    from agent import ToolRegistry

    provider = RecordingFileProvider()
    registry = ToolRegistry()
    registry.set_context("file_service", provider)

    tools = _tools(tmp_path, registry)

    assert tools._read_file("workspace", "anything.txt")["who"] == "recording"
    assert tools._list_files("workspace", ".")["who"] == "recording"
    assert provider.calls == ["read_file", "list_files"]


def test_a_provider_published_after_construction_still_wins(tmp_path):
    """Why the lookup is late rather than in ``__init__``.

    Bootstrap passes the service as a constructor argument first and puts it
    on the registry afterwards.  Any resolution that ran at construction time
    would have to assume one ordering; resolving on use assumes none.
    """
    from agent import ToolRegistry

    registry = ToolRegistry()
    tools = _tools(tmp_path, registry)

    provider = RecordingFileProvider()
    registry.set_context("file_service", provider)

    assert tools._read_file("workspace", "anything.txt")["who"] == "recording"


def test_the_constructor_argument_outranks_the_registry(tmp_path):
    """An explicitly handed provider is the most specific instruction there is."""
    from agent import ToolRegistry

    explicit = RecordingFileProvider()
    ignored = RecordingFileProvider()
    registry = ToolRegistry()
    registry.set_context("file_service", ignored)

    tools = _tools(tmp_path, registry, file_service=explicit)
    tools._read_file("workspace", "anything.txt")

    assert explicit.calls == ["read_file"]
    assert ignored.calls == []


def test_the_fallback_is_built_once_not_per_call(tmp_path):
    """The fallback holds no per-call state, so rebuilding it would be waste."""
    from agent import ToolRegistry

    tools = _tools(tmp_path, ToolRegistry())

    first = tools._file_service
    second = tools._file_service

    assert first is second
    assert isinstance(first, FileService)


def test_the_fallback_still_honours_the_live_write_scope(tmp_path):
    """The fallback's policy is immutable; its write scope is not.

    Sub-agents narrow the scope through registry context mid-session, so a
    fallback that froze the scope at construction would hand a sub-agent
    wider write access than it was granted.
    """
    from agent import ToolRegistry

    registry = ToolRegistry()
    tools = _tools(tmp_path, registry)
    service: Any = tools._file_service

    registry.set_context("write_scope", ("reports",))
    assert service._effective_write_scope() == ("reports",)

    registry.set_context("write_scope", ("reports/q3",))
    assert service._effective_write_scope() == ("reports/q3",)
