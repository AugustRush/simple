from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import asyncio
from datetime import datetime as RealDatetime
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace

import pytest

import agent.commands.builtin as builtin_commands
from agent.commands import (
    CommandCoordinator,
    CommandContext,
    CommandDescriptor,
    CommandRequest,
    CommandResult,
    CommandRouter,
    parse_command,
    register_builtin_commands,
)
from agent.plugins.catalog import PluginCatalog
from agent.runtime import AgentCore, RuntimeSessionState, TurnInput, TurnResult
from agent.shared import CancelToken
from agent.skills.catalog import SkillCatalog


async def _noop_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    return CommandResult()


def test_parse_command_preserves_argument_case() -> None:
    request = parse_command("  /MoDeL DeepSeek-Chat  ")

    assert request is not None
    assert request.original_text == "  /MoDeL DeepSeek-Chat  "
    assert request.name == "model"
    assert request.args == "DeepSeek-Chat"


@pytest.mark.parametrize("text", ["hello", "please /help", "  ", ""])
def test_parse_command_returns_none_for_non_command_text(text: str) -> None:
    assert parse_command(text) is None


def test_parse_command_carries_transport_identity_and_immutable_metadata() -> None:
    metadata = {"message_id": "m-1"}

    request = parse_command(
        " /help ",
        channel_name="feishu",
        session_id="s-1",
        metadata=metadata,
    )
    metadata["message_id"] = "changed"

    assert request is not None
    assert request.channel_name == "feishu"
    assert request.session_id == "s-1"
    assert request.metadata == {"message_id": "m-1"}
    with pytest.raises(TypeError):
        request.metadata["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize("contract", ["request", "context"])
def test_command_metadata_is_recursively_frozen_and_detached(contract: str) -> None:
    metadata = {
        "nested": {
            "items": [{"value": "original"}],
            "labels": {"alpha"},
        }
    }
    value = (
        CommandRequest("/help", "help", metadata=metadata)
        if contract == "request"
        else CommandContext({}, {}, object(), object(), metadata=metadata)
    )

    metadata["nested"]["items"][0]["value"] = "changed"
    metadata["nested"]["items"].append({"value": "new"})
    metadata["nested"]["labels"].add("beta")

    nested = value.metadata["nested"]
    assert nested["items"] == ({"value": "original"},)
    assert nested["labels"] == frozenset({"alpha"})
    with pytest.raises(TypeError):
        nested["new"] = True
    with pytest.raises(AttributeError):
        nested["items"].append({"value": "new"})


def test_command_contracts_are_immutable_and_normalize_collections() -> None:
    request = CommandRequest(original_text="/help", name="help")
    context = CommandContext(
        components={"agent": object()},
        config={"provider": "test"},
        session_state=object(),
        sink=object(),
        channel_name="cli",
        session_id="s-1",
        metadata={"message_id": "m-1"},
    )
    descriptor = CommandDescriptor(
        name="HELP",
        handler=_noop_handler,
        aliases=("H",),
        scopes={"CLI"},
    )
    result = CommandResult(attachments=["report.txt"])

    assert descriptor.name == "help"
    assert descriptor.aliases == ("h",)
    assert descriptor.scopes == frozenset({"cli"})
    assert result.attachments == ("report.txt",)
    for value in (request, context, descriptor, result):
        with pytest.raises(FrozenInstanceError):
            value.extra = True  # type: ignore[attr-defined]


def test_descriptor_and_result_defaults() -> None:
    descriptor = CommandDescriptor(name="status", handler=_noop_handler)
    result = CommandResult()

    assert descriptor.aliases == ()
    assert descriptor.usage == ""
    assert descriptor.description == ""
    assert descriptor.scopes == frozenset({"all"})
    assert descriptor.concurrency == "idle_only"
    assert descriptor.accepts_interjections is False
    assert result.handled is True
    assert result.response_text is None
    assert result.attachments == ()
    assert result.temporary_attachments == ()
    assert result.forward_text is None
    assert result.action is None
    assert result.level == "info"
    assert result.error is None


def test_command_result_requires_temporary_attachments_to_be_attachments() -> None:
    result = CommandResult(
        attachments=["temporary.txt", "export.md"],
        temporary_attachments=["temporary.txt"],
    )

    assert result.temporary_attachments == ("temporary.txt",)
    with pytest.raises(ValueError, match="temporary attachments"):
        CommandResult(
            attachments=["export.md"],
            temporary_attachments=["temporary.txt"],
        )


def test_command_result_temporary_field_preserves_positional_compatibility() -> None:
    result = CommandResult(
        True,
        "response",
        ("report.md",),
        "forward",
        "exit_cli",
        "warning",
        "stable error",
    )

    assert result.forward_text == "forward"
    assert result.action == "exit_cli"
    assert result.level == "warning"
    assert result.error == "stable error"
    assert result.temporary_attachments == ()


def test_descriptor_rejects_invalid_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        CommandDescriptor(
            name="status", handler=_noop_handler, scopes=frozenset({"web"})
        )


@pytest.mark.parametrize(
    ("name", "aliases"),
    [
        ("/help", ()),
        ("two words", ()),
        (" help", ()),
        ("help", ("/h",)),
        ("help", ("two words",)),
        ("help", ("h\talias",)),
    ],
)
def test_descriptor_rejects_unreachable_names_and_aliases(
    name: str, aliases: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="command (name|alias)"):
        CommandDescriptor(name=name, handler=_noop_handler, aliases=aliases)


def test_descriptor_accepts_reachable_plugin_name_conventions() -> None:
    descriptor = CommandDescriptor(
        name="Git-Helper:Deploy.V2",
        handler=_noop_handler,
        aliases=("GH:Deploy-V2",),
    )

    assert descriptor.name == "git-helper:deploy.v2"
    assert descriptor.aliases == ("gh:deploy-v2",)


def _skill_catalog(tmp_path, *skills: tuple[str, bool]) -> SkillCatalog:
    root = tmp_path / "skills"
    for skill_id, user_invocable in skills:
        skill_dir = root / skill_id
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: {skill_id}\n"
            f"user-invocable: {'true' if user_invocable else 'false'}\n"
            "---\n"
            "Instructions.\n",
            encoding="utf-8",
        )
    catalog = SkillCatalog(user_root=root, builtin_root=tmp_path / "builtin")
    catalog.load_all()
    return catalog


def test_parse_command_accepts_any_whitespace_between_name_and_args() -> None:
    request = parse_command("\t/echo\tKeep This Case\n")

    assert request is not None
    assert request.name == "echo"
    assert request.args == "Keep This Case"


def test_classify_does_not_execute_command_handler() -> None:
    called = False

    async def handler(
        request: CommandRequest, context: CommandContext
    ) -> CommandResult:
        nonlocal called
        called = True
        return CommandResult(response_text="called")

    router = CommandRouter(core_commands=[CommandDescriptor("status", handler)])
    route = router.classify("/STATUS")

    assert route.kind == "command"
    assert route.descriptor is not None
    assert route.descriptor.name == "status"
    assert called is False


def test_execute_invokes_only_the_classified_command() -> None:
    async def handler(
        request: CommandRequest, context: CommandContext
    ) -> CommandResult:
        return CommandResult(response_text=f"args={request.args}")

    router = CommandRouter(core_commands=[CommandDescriptor("echo", handler)])
    route = router.classify("/echo Mixed CASE")
    context = CommandContext({}, {}, object(), object())

    result = asyncio.run(router.execute(route, context))

    assert result == CommandResult(response_text="args=Mixed CASE")


def test_execute_does_not_expose_handler_exception_details() -> None:
    secret = "token=super-secret at /private/path"

    async def handler(
        request: CommandRequest, context: CommandContext
    ) -> CommandResult:
        raise RuntimeError(secret)

    router = CommandRouter(core_commands=[CommandDescriptor("explode", handler)])
    route = router.classify("/explode")

    result = asyncio.run(
        router.execute(route, CommandContext({}, {}, object(), object()))
    )

    assert result.response_text == "Command /explode failed."
    assert result.error == "Command /explode failed."
    assert secret not in result.response_text
    assert secret not in result.error


def test_core_alias_is_classified_as_core_command() -> None:
    descriptor = CommandDescriptor("status", _noop_handler, aliases=("st",))
    router = CommandRouter(core_commands=[descriptor])

    route = router.classify("/ST")

    assert route.kind == "command"
    assert route.source == "core"
    assert route.descriptor is descriptor


def test_plugin_command_takes_precedence_over_same_named_skill(tmp_path) -> None:
    catalog = _skill_catalog(tmp_path, ("deploy", True))
    descriptor = CommandDescriptor("deploy", _noop_handler)
    router = CommandRouter(
        plugin_commands=[descriptor],
        skill_catalog=catalog,
    )

    route = router.classify("/deploy Production")

    assert route.kind == "command"
    assert route.source == "plugin"
    assert route.descriptor is descriptor
    assert route.request is not None
    assert route.request.args == "Production"


@pytest.mark.parametrize("async_handler", [False, True])
@pytest.mark.parametrize(
    ("handler_result", "expected"),
    [
        (
            CommandResult(response_text="portable reply"),
            CommandResult(response_text="portable reply"),
        ),
        ("legacy model input", CommandResult(forward_text="legacy model input")),
        (None, CommandResult()),
    ],
)
def test_plugin_adapter_normalizes_legacy_handler_results(
    tmp_path, async_handler: bool, handler_result: object, expected: CommandResult
) -> None:
    if async_handler:

        async def handler(raw_cmd: str, components: dict) -> object:
            return handler_result

    else:

        def handler(raw_cmd: str, components: dict) -> object:
            return handler_result

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands["deploy"] = handler
    router = CommandRouter()
    router.register_plugin_catalog(catalog)

    route = router.classify("/deploy Keep THIS Case")
    result = asyncio.run(
        router.execute(route, CommandContext({}, {}, object(), object()))
    )

    assert result == expected


@pytest.mark.parametrize("async_handler", [False, True])
def test_plugin_adapter_returns_stable_error_without_exception_details(
    tmp_path, async_handler: bool
) -> None:
    secret = "token=plugin-secret at /private/plugin.py"
    if async_handler:

        async def handler(raw_cmd: str, components: dict) -> None:
            raise RuntimeError(secret)

    else:

        def handler(raw_cmd: str, components: dict) -> None:
            raise RuntimeError(secret)

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands["explode"] = handler
    router = CommandRouter()
    router.register_plugin_catalog(catalog)

    result = asyncio.run(
        router.execute(
            router.classify("/explode"),
            CommandContext({}, {}, object(), object()),
        )
    )

    assert result.response_text == "Command /explode failed."
    assert result.error == "Command /explode failed."
    assert secret not in result.response_text
    assert secret not in result.error


def test_plugin_adapter_passes_raw_command_and_per_invocation_overlay(tmp_path) -> None:
    shared_components = {"registry": object(), "ctx": "shared sentinel"}
    observed: list[tuple[str, dict]] = []

    async def handler(raw_cmd: str, components: dict) -> None:
        observed.append((raw_cmd, components))
        components["plugin_local"] = raw_cmd

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands["deploy"] = handler
    router = CommandRouter()
    router.register_plugin_catalog(catalog)
    first_ctx = object()
    second_ctx = object()

    async def invoke(command: str, session_id: str, current_ctx: object) -> None:
        state = SimpleNamespace(ctx=current_ctx)
        context = CommandContext(
            shared_components,
            {},
            state,
            object(),
            channel_name="feishu",
            session_id=session_id,
        )
        await router.execute(
            router.classify(
                command,
                channel_name="feishu",
                session_id=session_id,
            ),
            context,
        )

    async def invoke_both() -> None:
        await asyncio.gather(
            invoke("/Deploy Keep THIS Case", "session-1", first_ctx),
            invoke("/deploy Other Args", "session-2", second_ctx),
        )

    asyncio.run(invoke_both())

    assert [raw for raw, _ in observed] == [
        "Deploy Keep THIS Case",
        "deploy Other Args",
    ]
    assert observed[0][1] is not observed[1][1]
    assert observed[0][1]["ctx"] is first_ctx
    assert observed[1][1]["ctx"] is second_ctx
    assert observed[0][1]["command_context"].session_id == "session-1"
    assert observed[1][1]["command_context"].session_id == "session-2"
    assert observed[0][1]["command_sink"] is observed[0][1]["command_context"].sink
    assert observed[0][1]["channel_name"] == "feishu"
    assert observed[1][1]["session_id"] == "session-2"
    assert shared_components == {"registry": shared_components["registry"], "ctx": "shared sentinel"}


def test_plugin_adapter_offloads_sync_handler_without_losing_async_context(
    tmp_path,
) -> None:
    from contextvars import ContextVar

    marker = ContextVar("plugin_marker", default="missing")
    observed: dict[str, str] = {}

    def handler(raw_cmd: str, components: dict):
        observed["sync"] = marker.get()
        time.sleep(0.15)

        async def finish():
            observed["awaitable"] = marker.get()
            return "forwarded"

        return finish()

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands["slow"] = handler
    router = CommandRouter()
    router.register_plugin_catalog(catalog)

    async def scenario() -> tuple[CommandResult, float]:
        token = marker.set("session-context")
        started = time.perf_counter()
        heartbeat_at = 0.0

        async def heartbeat() -> None:
            nonlocal heartbeat_at
            await asyncio.sleep(0.01)
            heartbeat_at = time.perf_counter() - started

        try:
            result, _ = await asyncio.gather(
                router.execute(
                    router.classify("/slow"),
                    CommandContext({}, {}, object(), object()),
                ),
                heartbeat(),
            )
            return result, heartbeat_at
        finally:
            marker.reset(token)

    result, heartbeat_at = asyncio.run(scenario())

    assert heartbeat_at < 0.08
    assert observed == {
        "sync": "session-context",
        "awaitable": "session-context",
    }
    assert result.forward_text == "forwarded"


def test_plugin_adapter_bounds_sync_work_after_caller_cancellation(tmp_path) -> None:
    release = threading.Event()
    all_started = threading.Event()
    started = 0
    started_lock = threading.Lock()
    capacity = 4

    def handler(raw_cmd: str, components: dict) -> None:
        nonlocal started
        with started_lock:
            started += 1
            if started == capacity:
                all_started.set()
        release.wait(2)

    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands["block"] = handler
    router = CommandRouter()
    router.register_plugin_catalog(catalog)

    async def scenario() -> CommandResult:
        tasks = [
            asyncio.create_task(
                router.execute(
                    router.classify("/block"),
                    CommandContext({}, {}, object(), object()),
                )
            )
            for _ in range(capacity)
        ]
        try:
            assert await asyncio.to_thread(all_started.wait, 1)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            return await asyncio.wait_for(
                router.execute(
                    router.classify("/block"),
                    CommandContext({}, {}, object(), object()),
                ),
                0.2,
            )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    overflow = asyncio.run(scenario())

    assert overflow.level == "error"
    assert overflow.error == "Command /block failed."
    assert started == capacity


def test_plugin_adapter_preserves_metadata_and_core_precedence(tmp_path) -> None:
    async def handler(raw_cmd: str, components: dict) -> None:
        return None

    handler.__command_aliases__ = ("ship",)  # type: ignore[attr-defined]
    handler.__command_scopes__ = ("feishu",)  # type: ignore[attr-defined]
    handler.__command_usage__ = "/deploy <target>"  # type: ignore[attr-defined]
    handler.__command_description__ = "Deploy a target"  # type: ignore[attr-defined]
    catalog = PluginCatalog(builtin_dir=tmp_path)
    catalog._slash_commands.update({"deploy": handler, "help": handler})
    router = CommandRouter()
    register_builtin_commands(router)

    with pytest.raises(ValueError, match="reserved core command"):
        router.register_plugin_catalog(catalog)

    assert router.classify("/help").source == "core"

    catalog._slash_commands.pop("help")
    router.register_plugin_catalog(catalog)
    route = router.classify("/ship prod", channel_name="feishu")

    assert route.kind == "command"
    assert route.source == "plugin"
    assert route.descriptor is not None
    assert route.descriptor.name == "deploy"
    assert route.descriptor.aliases == ("ship",)
    assert route.descriptor.scopes == frozenset({"feishu"})
    assert route.descriptor.usage == "/deploy <target>"
    assert route.descriptor.description == "Deploy a target"
    assert router.classify("/deploy prod", channel_name="cli").kind == "unknown_slash"


def test_skill_prompt_dirty_refreshes_shared_and_evolved_sessions(monkeypatch) -> None:
    import agent as agent_module

    class DirtyOnceSkills:
        def __init__(self) -> None:
            self.dirty = True

        def consume_dirty(self) -> bool:
            dirty, self.dirty = self.dirty, False
            return dirty

    class CapturingRunner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run(self, turn_input, ctx, stream_callback=None):
            self.prompts.append(ctx.system_prompt)
            return TurnResult(text="ok")

        async def complete_turn(self, turn_input, state, result):
            return []

    monkeypatch.setattr(
        agent_module,
        "_compose_system_prompt",
        lambda base, registry, workspace_root, output_dir, **kwargs: (
            f"REFRESHED::{base}"
        ),
    )
    runner = CapturingRunner()
    components = {
        "base_system_prompt": "shared base",
        "system_prompt": "OLD::shared base",
        "registry": object(),
        "workspace_root": "/workspace",
        "output_dir": "/output",
        "skill_catalog": DirtyOnceSkills(),
        "turn_runner": runner,
    }
    core = AgentCore(components)
    evolved = RuntimeSessionState(
        ctx=SimpleNamespace(system_prompt="OLD::evolved", metadata={}),
        base_system_prompt_override="evolved base",
        system_prompt_override="OLD::evolved",
    )
    ordinary = RuntimeSessionState(
        ctx=SimpleNamespace(system_prompt="OLD::shared base", metadata={})
    )

    async def run_sessions() -> None:
        await core.handle_turn(TurnInput("evolved task"), evolved)
        await core.handle_turn(TurnInput("ordinary task"), ordinary)

    asyncio.run(run_sessions())

    assert components["system_prompt"] == "REFRESHED::shared base"
    assert evolved.system_prompt_override == "REFRESHED::evolved base"
    assert runner.prompts[0].startswith("REFRESHED::evolved base")
    assert "evolved task" in runner.prompts[0]
    assert runner.prompts[1].startswith("REFRESHED::shared base")
    assert "ordinary task" in runner.prompts[1]


@pytest.mark.parametrize(
    "invocation", ["/review Focus HERE", "/skill review Focus HERE"]
)
def test_classify_recognizes_explicit_user_invocable_skill(
    tmp_path, invocation: str
) -> None:
    router = CommandRouter(
        skill_catalog=_skill_catalog(tmp_path, ("review", True), ("internal", False))
    )

    route = router.classify(invocation)

    assert route.kind == "skill"
    assert route.skill_id == "review"
    assert route.skill_args == "Focus HERE"


def test_direct_skill_invocation_is_case_insensitive_for_namespaced_id(
    tmp_path,
) -> None:
    router = CommandRouter(
        skill_catalog=_skill_catalog(tmp_path, ("quality/review", True))
    )

    route = router.classify("/Quality/ReView Keep THIS Case")

    assert route.kind == "skill"
    assert route.skill_id == "quality/review"
    assert route.skill_args == "Keep THIS Case"


@pytest.mark.parametrize(
    "invocation",
    [
        "/QUALITY/REVIEW Keep THIS Case",
        "/quality/review Keep THIS Case",
        "/skill quality/review Keep THIS Case",
    ],
)
def test_skill_invocation_preserves_uppercase_canonical_id_and_argument_case(
    tmp_path, invocation: str
) -> None:
    router = CommandRouter(
        skill_catalog=_skill_catalog(tmp_path, ("Quality/Review", True))
    )

    route = router.classify(invocation)

    assert route.kind == "skill"
    assert route.skill_id == "Quality/Review"
    assert route.skill_args == "Keep THIS Case"


def test_casefold_colliding_skill_ids_are_ambiguous() -> None:
    bundles = [
        SimpleNamespace(id="Quality/Review", user_invocable=True),
        SimpleNamespace(id="quality/review", user_invocable=True),
    ]

    class CollisionCatalog:
        def get(self, skill_ref: str):
            return next((bundle for bundle in bundles if bundle.id == skill_ref), None)

        def list_skills(self):
            return list(reversed(bundles))

    router = CommandRouter(skill_catalog=CollisionCatalog())

    route = router.classify("/quality/review Task")

    assert route.kind == "unknown_slash"
    assert route.skill_id is None
    assert route.skill_error == "ambiguous"
    assert route.skill_ref == "quality/review"
    result = asyncio.run(
        router.execute(route, CommandContext({}, {}, object(), object()))
    )
    assert result.response_text == (
        "Ambiguous skill invocation 'quality/review'. "
        "Matches: Quality/Review, quality/review."
    )


@pytest.mark.parametrize(
    "invocation",
    ["/REVIEW Keep THIS Case", "/skill REVIEW Keep THIS Case"],
)
def test_non_invocable_casefold_collision_does_not_shadow_public_skill(
    invocation: str,
) -> None:
    public = SimpleNamespace(id="review", user_invocable=True)
    internal = SimpleNamespace(id="Review", user_invocable=False)

    class CollisionCatalog:
        def get(self, skill_ref: str):
            return next(
                (bundle for bundle in (internal, public) if bundle.id == skill_ref),
                None,
            )

        def list_skills(self):
            return [internal, public]

    router = CommandRouter(skill_catalog=CollisionCatalog())

    route = router.classify(invocation)

    assert route.kind == "skill"
    assert route.skill_id == "review"
    assert route.skill_args == "Keep THIS Case"
    assert route.suggestions == ()


@pytest.mark.parametrize("invocation", ["/internal", "/skill internal task"])
def test_non_user_invocable_skill_is_an_unknown_slash(
    tmp_path, invocation: str
) -> None:
    router = CommandRouter(skill_catalog=_skill_catalog(tmp_path, ("internal", False)))

    assert router.classify(invocation).kind == "unknown_slash"


def test_unknown_slash_has_close_command_suggestions() -> None:
    router = CommandRouter(core_commands=[CommandDescriptor("help", _noop_handler)])

    route = router.classify("/hep")

    assert route.kind == "unknown_slash"
    assert route.suggestions == ("help",)
    result = asyncio.run(
        router.execute(route, CommandContext({}, {}, object(), object()))
    )
    assert result.level == "error"
    assert result.response_text is not None
    assert "/help" in result.response_text


def test_unknown_explicit_skill_reports_ref_and_skill_suggestion(tmp_path) -> None:
    router = CommandRouter(skill_catalog=_skill_catalog(tmp_path, ("review", True)))

    route = router.classify("/skill revie Keep Case")

    assert route.kind == "unknown_slash"
    assert route.skill_ref == "revie"
    assert route.suggestions == ("review",)
    result = asyncio.run(
        router.execute(route, CommandContext({}, {}, object(), object()))
    )
    assert result.response_text == (
        "Unknown skill 'revie'. Did you mean /skill review?"
    )


def test_ordinary_text_falls_through_without_becoming_a_command() -> None:
    router = CommandRouter()

    route = router.classify("Keep /this as ordinary text")

    assert route.kind == "text"
    assert route.text == "Keep /this as ordinary text"


def test_core_command_names_and_aliases_are_reserved() -> None:
    router = CommandRouter(
        core_commands=[CommandDescriptor("help", _noop_handler, aliases=("h",))]
    )

    with pytest.raises(ValueError, match="reserved core command"):
        router.register_plugin(CommandDescriptor("HELP", _noop_handler))
    with pytest.raises(ValueError, match="reserved core command"):
        router.register_plugin(
            CommandDescriptor("other", _noop_handler, aliases=("h",))
        )


def test_out_of_scope_core_name_cannot_be_shadowed_by_direct_skill(tmp_path) -> None:
    router = CommandRouter(
        core_commands=[
            CommandDescriptor("quit", _noop_handler, scopes=frozenset({"cli"}))
        ],
        skill_catalog=_skill_catalog(tmp_path, ("quit", True)),
    )

    route = router.classify("/quit", channel_name="feishu")

    assert route.kind == "unknown_slash"


def test_duplicate_plugin_names_and_aliases_are_rejected() -> None:
    router = CommandRouter(
        plugin_commands=[CommandDescriptor("deploy", _noop_handler, aliases=("d",))]
    )

    with pytest.raises(ValueError, match="duplicate plugin command"):
        router.register_plugin(CommandDescriptor("DEPLOY", _noop_handler))
    with pytest.raises(ValueError, match="duplicate plugin command"):
        router.register_plugin(
            CommandDescriptor("debug", _noop_handler, aliases=("d",))
        )


def test_help_is_generated_from_descriptors_and_filtered_by_scope() -> None:
    router = CommandRouter(
        core_commands=[
            CommandDescriptor(
                "help",
                _noop_handler,
                usage="/help",
                description="Show commands",
            ),
            CommandDescriptor(
                "quit",
                _noop_handler,
                usage="/quit",
                description="Exit the CLI",
                scopes=frozenset({"cli"}),
            ),
            CommandDescriptor(
                "send",
                _noop_handler,
                usage="/send <path>",
                description="Send a file",
                scopes=frozenset({"feishu"}),
            ),
        ]
    )

    cli_help = router.help_text("cli")
    feishu_help = router.help_text("feishu")

    assert "/help - Show commands" in cli_help
    assert "/quit - Exit the CLI" in cli_help
    assert "/send <path>" not in cli_help
    assert "/help - Show commands" in feishu_help
    assert "/send <path> - Send a file" in feishu_help
    assert "/quit" not in feishu_help


def test_help_escapes_external_descriptor_markdown() -> None:
    router = CommandRouter(
        plugin_commands=[
            CommandDescriptor(
                "report",
                _noop_handler,
                usage="/report [name](url)<tag>\\pipe | admin\n# forged",
                description="desc\\tail\n## forged",
            )
        ]
    )

    help_text = router.help_text("cli")

    assert "\n# forged" not in help_text
    assert "\n## forged" not in help_text
    assert r"\|" in help_text
    assert r"\#" in help_text
    assert r"\\" in help_text
    for character in "[]()<>":
        assert f"\\{character}" in help_text


class _BuiltinSink:
    def __init__(self) -> None:
        self.attachments: list[Path] = []

    def queue_attachment(self, path: Path) -> None:
        self.attachments.append(path)


def _builtin_router() -> CommandRouter:
    router = CommandRouter()
    register_builtin_commands(router)
    return router


def _run_builtin(
    router: CommandRouter,
    command: str,
    *,
    channel_name: str = "cli",
    components: dict | None = None,
    config: dict | None = None,
    state: object | None = None,
) -> CommandResult:
    route = router.classify(command, channel_name=channel_name, session_id="s-1")
    assert route.kind == "command"
    context = CommandContext(
        components or {},
        config or {},
        state or SimpleNamespace(ctx=SimpleNamespace(messages=[]), model_override=None),
        _BuiltinSink(),
        channel_name=channel_name,
        session_id="s-1",
    )
    return asyncio.run(router.execute(route, context))


def test_builtin_registration_defines_portable_scope_and_concurrency() -> None:
    router = _builtin_router()

    expected = {
        "help": ("anytime", frozenset({"all"})),
        "memory": ("anytime", frozenset({"all"})),
        "context": ("anytime", frozenset({"all"})),
        "sessions": ("anytime", frozenset({"all"})),
        "session": ("anytime", frozenset({"all"})),
        "export": ("idle_only", frozenset({"all"})),
        "tools": ("anytime", frozenset({"all"})),
        "skills": ("anytime", frozenset({"all"})),
        "plugins": ("anytime", frozenset({"all"})),
        "model": ("anytime", frozenset({"all"})),
        "quit": ("anytime", frozenset({"cli"})),
        "send": ("anytime", frozenset({"feishu"})),
        "cancel": ("interrupt", frozenset({"all"})),
        "now": ("interrupt", frozenset({"all"})),
        "ralph": ("idle_only", frozenset({"all"})),
    }

    for name, policy in expected.items():
        route = router.classify(
            f"/{name}", channel_name="feishu" if name == "send" else "cli"
        )
        assert route.kind == "command"
        assert route.descriptor is not None
        assert (route.descriptor.concurrency, route.descriptor.scopes) == policy
    assert router.classify("/exit", channel_name="cli").descriptor.name == "quit"
    assert router.classify("/q", channel_name="cli").descriptor.name == "quit"
    assert router.classify("/history", channel_name="cli").descriptor.name == "sessions"
    ralph = router.classify("/ralph", channel_name="cli")
    assert ralph.kind == "command"
    assert ralph.descriptor.accepts_interjections is True


def test_builtin_help_uses_live_descriptors_and_channel_scope() -> None:
    router = _builtin_router()

    cli_help = _run_builtin(router, "/help").response_text
    feishu_help = _run_builtin(router, "/help", channel_name="feishu").response_text

    assert cli_help is not None
    assert "/quit" in cli_help
    assert "/send <path>" not in cli_help
    assert "Ctrl+C" in cli_help
    assert feishu_help is not None
    assert "/send <path>" in feishu_help
    assert "/quit" not in feishu_help


def test_builtin_ralph_maps_start_list_resume_and_parse_errors():
    from agent.ralph import (
        RalphRunResult,
        RalphTask,
        RalphTaskAmbiguousError,
        RalphTaskStatus,
    )

    class Service:
        def __init__(self):
            self.calls = []

        async def start(self, goal, session_state, *, max_iterations, verify_command, observer=None):
            self.calls.append(("start", goal, max_iterations, verify_command, session_state, observer))
            task = RalphTask(id="new-task", goal=goal, max_iterations=max_iterations)
            task.status = RalphTaskStatus.COMPLETE
            return RalphRunResult(task)

        async def resume(self, task_ref, session_state, *, observer=None):
            self.calls.append(("resume", task_ref, session_state, observer))
            if task_ref == "abc":
                raise RalphTaskAmbiguousError("abc", ("abc1", "abc2"))
            return RalphRunResult(RalphTask(id="resumed", goal="old"))

        def list_tasks(self):
            return [RalphTask(id="listed", goal="existing")]

    service = Service()
    router = _builtin_router()
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        model_override=None,
        cancel_token=None,
        pending_interjections=[],
    )

    started = _run_builtin(
        router,
        "/ralph Build Feature --max 4 --verify 'pytest -q'",
        components={"ralph_service": service},
        state=state,
    )
    listed = _run_builtin(
        router,
        "/ralph list",
        components={"ralph_service": service},
        state=state,
    )
    resumed = _run_builtin(
        router,
        "/ralph resume resumed",
        components={"ralph_service": service},
        state=state,
    )
    ambiguous = _run_builtin(
        router,
        "/ralph resume abc",
        components={"ralph_service": service},
        state=state,
    )
    invalid = _run_builtin(
        router,
        "/ralph goal --max nope",
        components={"ralph_service": service},
        state=state,
    )

    assert service.calls[0][0:4] == ("start", "Build Feature", 4, "pytest -q")
    assert service.calls[1][0:2] == ("resume", "resumed")
    assert "complete" in started.response_text.lower()
    assert "listed" in listed.response_text and "existing" in listed.response_text
    assert "resumed" in resumed.response_text.lower()
    assert ambiguous.level == "error" and "ambiguous" in ambiguous.response_text.lower()
    assert invalid.level == "error" and "--max" in invalid.response_text


def test_ralph_command_coordinator_regression_does_not_crash():
    from agent.ralph import RalphRunResult, RalphTask

    class Service:
        async def start(self, goal, session_state, **kwargs):
            assert goal == "demo task"
            return RalphRunResult(RalphTask(id="demo", goal=goal))

    router = _builtin_router()
    coordinator, core = _coordinator(
        router,
        components={"ralph_service": Service()},
    )
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[], metadata={}))
    sink = _CoordinatorSink()

    asyncio.run(
        coordinator.handle(
            TurnInput.from_text("/ralph demo task", session_id="s-1"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert core.calls == []
    assert sink.errors == []
    assert any("demo" in text for text, _ in sink.statuses)


def test_builtin_memory_and_context_render_read_only_summaries() -> None:
    class Memory:
        def read_index(self) -> str:
            return '{"one": 1}\n\n{"two": 2}\n'

    class ContextManager:
        def stats(self) -> dict:
            return {
                "dynamic_categories": 2,
                "max_categories": 8,
                "total_categories": 3,
                "total_entries": 11,
                "category_names": ["work", "people"],
                "staged_turns": 4,
                "needs_consolidation": True,
                "idle_elapsed_s": 9,
                "idle_threshold_s": 60,
            }

    router = _builtin_router()
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        context_manager=ContextManager(),
        model_override=None,
    )
    memory = _run_builtin(
        router, "/memory", components={"memory": Memory()}, state=state
    )
    context = _run_builtin(router, "/context", state=state)

    assert memory.response_text is not None
    assert "Memory Export" in memory.response_text
    assert "Entries: 2" in memory.response_text
    assert context.response_text is not None
    assert "Dynamic Categories: 2/8" in context.response_text
    assert "Needs Consolidation: yes" in context.response_text


def test_builtin_memory_and_context_report_unavailable_dependencies() -> None:
    router = _builtin_router()

    memory = _run_builtin(router, "/memory")
    context = _run_builtin(router, "/context")

    assert memory.level == "error"
    assert memory.response_text == "Memory is not available."
    assert context.level == "error"
    assert context.response_text == "Context manager is not available."


def test_builtin_sessions_and_session_prefix_lookup(tmp_path) -> None:
    sessions_file = tmp_path / "sessions.jsonl"
    sessions = [
        {
            "session_id": "abc123-first",
            "timestamp": "2026-07-25T10:00:00Z",
            "objective_score": 8.5,
            "task_summary": "First task",
            "tools_used": ["Read", "Write"],
            "correction_count": 1,
        },
        {
            "session_id": "def456-second",
            "timestamp": "2026-07-25T11:00:00Z",
            "score": 7,
            "task_summary": "Second task",
        },
    ]
    sessions_file.write_text(
        "not-json\n" + "\n".join(json.dumps(item) for item in sessions),
        encoding="utf-8",
    )
    router = _builtin_router()
    components = {"sessions_file": sessions_file}

    history = _run_builtin(router, "/history", components=components)
    detail = _run_builtin(router, "/session abc", components=components)
    usage = _run_builtin(router, "/session", components=components)
    missing = _run_builtin(router, "/session missing", components=components)

    assert history.response_text is not None
    assert "Recent Sessions" in history.response_text
    assert history.response_text.index("def456-secon") < history.response_text.index(
        "abc123-first"
    )
    assert detail.response_text is not None
    assert "abc123-first" in detail.response_text
    assert "Score: 8.5" in detail.response_text
    assert "Tools Used: Read, Write" in detail.response_text
    assert usage.response_text == "Usage: /session <session_id_prefix>"
    assert usage.level == "error"
    assert missing.response_text == "Session not found: missing"
    assert missing.level == "error"


def test_builtin_export_writes_markdown_and_returns_attachment(tmp_path) -> None:
    state = SimpleNamespace(
        ctx=SimpleNamespace(
            messages=[
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Here it is"},
                        {"type": "image_url", "image_url": {"url": "data:..."}},
                    ],
                },
            ]
        ),
        model_override=None,
    )
    router = _builtin_router()

    result = _run_builtin(
        router,
        "/export",
        components={"output_dir": tmp_path},
        state=state,
    )

    assert result.level == "info"
    assert len(result.attachments) == 1
    path = result.attachments[0]
    assert isinstance(path, Path)
    assert path.parent == tmp_path
    assert result.response_text == f"Exported 2 messages to {path}"
    content = path.read_text(encoding="utf-8")
    assert "## USER\n\nHello" in content
    assert "Here it is" in content
    assert "[media content]" in content


def test_builtin_export_rejects_arguments_and_empty_session(tmp_path) -> None:
    router = _builtin_router()

    usage = _run_builtin(router, "/export extra", components={"output_dir": tmp_path})
    empty = _run_builtin(router, "/export", components={"output_dir": tmp_path})

    assert usage.response_text == "Usage: /export"
    assert usage.level == "error"
    assert empty.response_text == "No messages to export."
    assert empty.level == "warning"


def test_builtin_export_uses_unique_exclusive_files_for_same_timestamp(
    tmp_path, monkeypatch
) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls, tz=None):
            return RealDatetime(2026, 7, 25, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(builtin_commands, "datetime", FixedDatetime)
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[{"role": "user", "content": "complete"}]),
        model_override=None,
    )
    router = _builtin_router()

    def export_one() -> CommandResult:
        return _run_builtin(
            router,
            "/export",
            components={"output_dir": tmp_path},
            state=state,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: export_one(), range(2)))

    paths = [result.attachments[0] for result in results]
    assert len(set(paths)) == 2
    assert all(
        path.read_text(encoding="utf-8").endswith("complete\n") for path in paths
    )


def test_builtin_export_does_not_follow_existing_symlink(tmp_path, monkeypatch) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls, tz=None):
            return RealDatetime(2026, 7, 25, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(builtin_commands, "datetime", FixedDatetime)
    victim = tmp_path / "victim.txt"
    victim.write_text("do not replace", encoding="utf-8")
    collision = tmp_path / "session_20260725_120000.md"
    collision.symlink_to(victim)
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[{"role": "user", "content": "exported"}]),
        model_override=None,
    )

    result = _run_builtin(
        _builtin_router(),
        "/export",
        components={"output_dir": tmp_path},
        state=state,
    )

    attachment = result.attachments[0]
    assert victim.read_text(encoding="utf-8") == "do not replace"
    assert attachment != collision
    assert not attachment.is_symlink()
    assert attachment.read_text(encoding="utf-8").endswith("exported\n")


def test_builtin_tools_skills_and_plugins_render_catalogs() -> None:
    registry = SimpleNamespace(list_tools=lambda: ["Read", "Write"])
    skill_catalog = SimpleNamespace(
        list_skills=lambda: [
            SimpleNamespace(id="review", source="user", description="Review code")
        ]
    )
    plugin_catalog = SimpleNamespace(
        list_plugins=lambda: [
            SimpleNamespace(
                name="stats", version="1.2", source="builtin", description="Metrics"
            )
        ]
    )
    router = _builtin_router()
    components = {
        "registry": registry,
        "skill_catalog": skill_catalog,
        "plugin_catalog": plugin_catalog,
    }

    tools = _run_builtin(router, "/tools", components=components)
    skills = _run_builtin(router, "/skills", components=components)
    plugins = _run_builtin(router, "/plugins", components=components)

    assert tools.response_text == "## Available Tools\n\n- Read\n- Write"
    assert skills.response_text is not None
    assert "review | user | Review code" in skills.response_text
    assert plugins.response_text is not None
    assert "stats | 1.2 | builtin | Metrics" in plugins.response_text


def test_builtin_model_lists_validates_and_updates_session_override() -> None:
    router = _builtin_router()
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        model_override=None,
    )
    config = {
        "active_provider": "test",
        "providers": {
            "test": {"default_model": "model-a", "models": ["model-a", "model-b"]}
        },
    }
    agent = SimpleNamespace(model="model-a")
    components = {"agent": agent}

    listed = _run_builtin(
        router, "/model", config=config, components=components, state=state
    )
    switched = _run_builtin(
        router, "/model model-b", config=config, components=components, state=state
    )
    rejected = _run_builtin(
        router, "/model unknown", config=config, components=components, state=state
    )

    assert listed.response_text is not None
    assert "model-a (active)" in listed.response_text
    assert switched.response_text == "Switched to model: model-b (session only)"
    assert state.model_override == "model-b"
    assert (
        rejected.response_text
        == "Unknown model: unknown. Available models: model-a, model-b"
    )
    assert rejected.level == "error"
    assert state.model_override == "model-b"
    assert agent.model == "model-a"


def test_builtin_model_uses_active_provider_default_when_models_are_absent() -> None:
    router = _builtin_router()
    state = SimpleNamespace(
        ctx=SimpleNamespace(messages=[]),
        model_override=None,
    )
    config = {
        "active_provider": "primary",
        "providers": {
            "primary": {"default_model": "primary-default"},
            "secondary": {
                "default_model": "secondary-default",
                "models": ["secondary-default", "secondary-extra"],
            },
        },
    }
    agent = SimpleNamespace(model="primary-default")

    listed = _run_builtin(
        router,
        "/model",
        config=config,
        components={"agent": agent},
        state=state,
    )
    switched = _run_builtin(
        router,
        "/model primary-default",
        config=config,
        components={"agent": agent},
        state=state,
    )
    rejected = _run_builtin(
        router,
        "/model secondary-extra",
        config=config,
        components={"agent": agent},
        state=state,
    )

    assert listed.response_text == "## Models\n\n- primary-default (active)"
    assert switched.response_text == (
        "Switched to model: primary-default (session only)"
    )
    assert rejected.level == "error"
    assert state.model_override == "primary-default"
    assert agent.model == "primary-default"


def test_builtin_quit_is_cli_only_and_returns_exit_action() -> None:
    router = _builtin_router()

    result = _run_builtin(router, "/q", channel_name="cli")

    assert result.action == "exit_cli"
    assert router.classify("/quit", channel_name="feishu").kind == "unknown_slash"


def test_builtin_send_queues_only_existing_files_inside_output_dir(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    inside = output_dir / "report.txt"
    inside.write_text("report", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    router = _builtin_router()
    components = {"output_dir": output_dir}

    sent = _run_builtin(
        router, "/send report.txt", channel_name="feishu", components=components
    )
    traversal = _run_builtin(
        router, "/send ../secret.txt", channel_name="feishu", components=components
    )
    absolute = _run_builtin(
        router, f"/send {outside}", channel_name="feishu", components=components
    )
    missing = _run_builtin(
        router, "/send absent.txt", channel_name="feishu", components=components
    )
    usage = _run_builtin(router, "/send", channel_name="feishu", components=components)

    assert len(sent.attachments) == 1
    assert sent.temporary_attachments == sent.attachments
    assert sent.attachments[0].parent.parent == output_dir
    assert sent.attachments[0].parent.name.startswith(".send-")
    assert sent.attachments[0].read_text(encoding="utf-8") == "report"
    assert sent.response_text == f"Sending file: {inside.resolve()}"
    assert traversal.response_text == "File is outside the output directory."
    assert traversal.level == "error"
    assert absolute.response_text == "File is outside the output directory."
    assert missing.response_text == "File not found: absent.txt"
    assert usage.response_text == "Usage: /send <path>"
    assert (
        router.classify("/send report.txt", channel_name="cli").kind == "unknown_slash"
    )


def test_builtin_send_accepts_absolute_path_inside_output_dir(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")

    result = _run_builtin(
        _builtin_router(),
        f"/send {source.resolve()}",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert len(result.attachments) == 1
    assert result.attachments[0].read_text(encoding="utf-8") == "report"
    assert result.response_text == f"Sending file: {source.resolve()}"


def test_builtin_send_rejects_absolute_path_with_symlink_component(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")
    alias = output_dir / "alias.txt"
    alias.symlink_to(source)

    result = _run_builtin(
        _builtin_router(),
        f"/send {alias}",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert result.level == "error"
    assert result.attachments == ()
    assert source.read_text(encoding="utf-8") == "report"


def test_builtin_send_rejects_absolute_path_with_parent_component(tmp_path) -> None:
    output_dir = tmp_path / "output"
    (output_dir / "nested").mkdir(parents=True)
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")
    explicit_parent = f"{output_dir}/nested/../report.txt"

    result = _run_builtin(
        _builtin_router(),
        f"/send {explicit_parent}",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert result.response_text == "File is outside the output directory."
    assert result.level == "error"
    assert result.attachments == ()


@pytest.mark.parametrize("component", [".", ""])
def test_builtin_send_rejects_absolute_path_with_unsafe_component(
    tmp_path, component
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")
    unsafe_path = f"{output_dir}/{component}/report.txt"

    result = _run_builtin(
        _builtin_router(),
        f"/send {unsafe_path}",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert result.response_text == "File is outside the output directory."
    assert result.level == "error"
    assert result.attachments == ()


def test_builtin_send_attaches_immutable_inside_snapshot(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("original report", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("outside secret", encoding="utf-8")

    result = _run_builtin(
        _builtin_router(),
        "/send report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )
    source.unlink()
    source.symlink_to(victim)

    attachment = result.attachments[0]
    assert attachment != source
    assert attachment.parent.parent == output_dir
    assert attachment.parent.name.startswith(".send-")
    assert attachment.read_text(encoding="utf-8") == "original report"
    assert victim.read_text(encoding="utf-8") == "outside secret"


def test_builtin_send_fails_closed_without_nofollow_support(
    tmp_path, monkeypatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("original report", encoding="utf-8")
    monkeypatch.delattr(builtin_commands.os, "O_NOFOLLOW")

    result = _run_builtin(
        _builtin_router(),
        "/send report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert (
        result.response_text == "Secure file sending is not supported on this platform."
    )
    assert result.level == "error"
    assert result.attachments == ()
    assert source.read_text(encoding="utf-8") == "original report"
    assert not list(output_dir.glob(".send-*"))


def test_builtin_send_uses_private_snapshot_permissions(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_text("report", encoding="utf-8")

    result = _run_builtin(
        _builtin_router(),
        "/send report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert stat.S_IMODE(result.attachments[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.attachments[0].stat().st_mode) == 0o600


def test_builtin_send_rejects_ancestor_symlink_swap(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "output"
    nested = output_dir / "nested"
    nested.mkdir(parents=True)
    source = nested / "report.txt"
    source.write_text("inside report", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "report.txt"
    victim.write_text("outside secret", encoding="utf-8")
    moved = output_dir / "nested-original"
    real_open = builtin_commands.os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        path_text = os.fspath(path)
        if not swapped and (path_text == "nested" or Path(path_text) == source):
            swapped = True
            nested.rename(moved)
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtin_commands.os, "open", swap_before_open)

    result = _run_builtin(
        _builtin_router(),
        "/send nested/report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
    )

    assert swapped is True
    assert result.level == "error"
    assert result.attachments == ()
    assert victim.read_text(encoding="utf-8") == "outside secret"


def test_builtin_send_rejects_oversize_before_creating_temp_files(tmp_path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_bytes(b"12345")

    result = _run_builtin(
        _builtin_router(),
        "/send report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
        config={"send_max_snapshot_bytes": 4},
    )

    assert result.response_text == "File exceeds send snapshot limit (4 bytes)."
    assert result.level == "error"
    assert result.attachments == ()
    assert list(output_dir.iterdir()) == [source]


def test_builtin_send_rejects_source_growth_and_cleans_partial_temp(
    tmp_path, monkeypatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source = output_dir / "report.txt"
    source.write_bytes(b"start")
    original_copy = builtin_commands._copy_file_descriptor

    def grow_during_copy(*args, **kwargs):
        with source.open("ab") as handle:
            handle.write(b"-growth")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(builtin_commands, "_copy_file_descriptor", grow_during_copy)

    result = _run_builtin(
        _builtin_router(),
        "/send report.txt",
        channel_name="feishu",
        components={"output_dir": output_dir},
        config={"send_max_snapshot_bytes": 64},
    )

    assert result.response_text == "File changed while preparing attachment."
    assert result.level == "error"
    assert result.attachments == ()
    assert list(output_dir.iterdir()) == [source]


def test_builtin_send_snapshot_copy_does_not_block_event_loop(
    tmp_path, monkeypatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("report", encoding="utf-8")
    original_snapshot = builtin_commands._snapshot_send_file

    def slow_snapshot(*args, **kwargs):
        time.sleep(0.15)
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(builtin_commands, "_snapshot_send_file", slow_snapshot)
    router = _builtin_router()
    route = router.classify("/send report.txt", channel_name="feishu")
    context = CommandContext(
        {"output_dir": output_dir},
        {},
        SimpleNamespace(ctx=SimpleNamespace(messages=[]), model_override=None),
        _BuiltinSink(),
        channel_name="feishu",
    )

    async def scenario() -> None:
        started_at = time.perf_counter()
        task = asyncio.create_task(router.execute(route, context))
        await asyncio.sleep(0.01)
        tick_elapsed = time.perf_counter() - started_at
        result = await task

        assert tick_elapsed < 0.08
        assert result.level == "info"

    asyncio.run(scenario())


def test_builtin_send_cancellation_during_copy_cleans_completed_snapshot(
    tmp_path, monkeypatch
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("report", encoding="utf-8")
    copy_started = threading.Event()
    release_copy = threading.Event()
    original_copy = builtin_commands._copy_file_descriptor

    def gated_copy(*args, **kwargs):
        copy_started.set()
        release_copy.wait(timeout=1)
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(builtin_commands, "_copy_file_descriptor", gated_copy)
    router = _builtin_router()
    route = router.classify("/send report.txt", channel_name="feishu")
    context = CommandContext(
        {"output_dir": output_dir},
        {},
        SimpleNamespace(ctx=SimpleNamespace(messages=[]), model_override=None),
        _BuiltinSink(),
        channel_name="feishu",
    )

    async def scenario() -> None:
        running = asyncio.create_task(router.execute(route, context))
        assert await asyncio.to_thread(copy_started.wait, 1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        release_copy.set()
        for _attempt in range(100):
            if not list(output_dir.glob(".send-*")):
                break
            await asyncio.sleep(0.01)

        assert not list(output_dir.glob(".send-*"))

    asyncio.run(scenario())


def test_builtin_external_markdown_values_cannot_forge_rows_or_headings(
    tmp_path,
) -> None:
    sessions_file = tmp_path / "sessions.jsonl"
    sessions_file.write_text(
        json.dumps(
            {
                "session_id": "abc\\id|admin\n# forged",
                "timestamp": "now|later\n## forged",
                "score": 1,
                "task_summary": "summary|extra\n# forged",
                "tools_used": ["Read|Write\n## forged"],
            }
        ),
        encoding="utf-8",
    )
    components = {
        "sessions_file": sessions_file,
        "registry": SimpleNamespace(list_tools=lambda: ["Read|Write\n# forged"]),
        "skill_catalog": SimpleNamespace(
            list_skills=lambda: [
                SimpleNamespace(
                    id="review|admin\n# forged",
                    source="user\\local",
                    description="desc|extra\n## forged",
                )
            ]
        ),
        "plugin_catalog": SimpleNamespace(
            list_plugins=lambda: [
                SimpleNamespace(
                    name="stats|admin\n# forged",
                    version="1\\2",
                    source="built|in",
                    description="desc\n## forged",
                )
            ]
        ),
    }
    router = _builtin_router()

    outputs = [
        _run_builtin(router, command, components=components).response_text or ""
        for command in ("/sessions", "/session abc", "/tools", "/skills", "/plugins")
    ]

    for output in outputs:
        assert "\n# forged" not in output
        assert "\n## forged" not in output
    combined = "\n".join(outputs)
    assert r"\|" in combined
    assert r"\#" in combined
    assert r"\\" in combined


def test_builtin_session_turn_heading_escapes_user_prefix(tmp_path) -> None:
    store = SimpleNamespace(
        get_turns_for_session=lambda _prefix: [{"role": "user", "content": "safe"}]
    )
    components = {
        "sessions_file": tmp_path / "missing.jsonl",
        "context_manager": SimpleNamespace(store=store),
    }

    result = _run_builtin(
        _builtin_router(),
        "/session abc\n# forged",
        components=components,
    )

    assert result.response_text is not None
    assert "\n# forged" not in result.response_text
    assert r"\# forged" in result.response_text


def test_builtin_interrupt_handlers_are_defensive_when_called_directly() -> None:
    router = _builtin_router()

    for command in ("/cancel", "/now urgent"):
        result = _run_builtin(router, command)
        assert result.level == "error"
        assert "coordinator" in result.response_text.lower()


def test_builtin_registration_is_atomic_when_late_descriptor_conflicts() -> None:
    router = CommandRouter(
        plugin_commands=[
            CommandDescriptor("send", _noop_handler, scopes=frozenset({"feishu"}))
        ]
    )

    with pytest.raises(ValueError, match="plugin command conflicts"):
        register_builtin_commands(router)

    for command in ("/help", "/memory", "/quit", "/cancel", "/now"):
        assert router.classify(command, channel_name="cli").kind == "unknown_slash"
    route = router.classify("/send report.txt", channel_name="feishu")
    assert route.kind == "command"
    assert route.source == "plugin"


def test_command_coordinator_is_exported_from_commands_package() -> None:
    from agent.commands import CommandCoordinator

    assert CommandCoordinator.__name__ == "CommandCoordinator"


class _CoordinatorSink:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.attachments: list[object] = []
        self.drain_count = 0

    def on_status(self, text: str, *, level: str = "info") -> None:
        self.statuses.append((text, level))

    def on_error(self, error: str) -> None:
        self.errors.append(error)

    def queue_attachment(self, attachment: object) -> None:
        self.attachments.append(attachment)

    async def drain(self) -> None:
        self.drain_count += 1


class _CoordinatorCore:
    def __init__(self, behavior=None) -> None:
        self.calls: list[TurnInput] = []
        self.state_snapshots: list[tuple[str, bool, object]] = []
        self.behavior = behavior

    async def handle_turn(self, turn_input, state, *, sink=None):
        self.calls.append(turn_input)
        self.state_snapshots.append(
            (state.operation_state, state.accepts_interjections, state.cancel_token)
        )
        if self.behavior is not None:
            await self.behavior(turn_input, state, sink)
        return SimpleNamespace(result=TurnResult(text="ok"), events=(), blocked=False)


def _coordinator(
    router: CommandRouter | None = None,
    *,
    core: _CoordinatorCore | None = None,
    events: list | None = None,
    components: dict | None = None,
    config: dict | None = None,
) -> tuple[CommandCoordinator, _CoordinatorCore]:
    fake_core = core or _CoordinatorCore()
    coordinator = CommandCoordinator(
        fake_core,  # type: ignore[arg-type]
        router or CommandRouter(),
        components=components or {"dependency": "available"},
        config=config or {"mode": "test"},
        event_hook=None if events is None else events.append,
    )
    return coordinator, fake_core


def test_coordinator_cleans_temporary_attachment_after_drain(tmp_path) -> None:
    class ConsumingSink(_CoordinatorSink):
        def __init__(self) -> None:
            super().__init__()
            self.consumed: list[tuple[Path, str]] = []

        async def drain(self) -> None:
            self.drain_count += 1
            pending = list(self.attachments)
            self.attachments.clear()
            for attachment in pending:
                path = Path(attachment)
                self.consumed.append((path, path.read_text(encoding="utf-8")))

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("report", encoding="utf-8")
    router = _builtin_router()
    coordinator, _ = _coordinator(
        router,
        components={"output_dir": output_dir},
    )
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))
    sink = ConsumingSink()

    asyncio.run(
        coordinator.handle(
            _turn("/send report.txt"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert len(sink.consumed) == 1
    attachment, content = sink.consumed[0]
    assert content == "report"
    assert not attachment.exists()
    assert not attachment.parent.exists()


def test_coordinator_keeps_normal_export_attachment_after_drain(tmp_path) -> None:
    class ConsumingSink(_CoordinatorSink):
        async def drain(self) -> None:
            self.drain_count += 1
            for attachment in self.attachments:
                assert Path(attachment).is_file()

    router = _builtin_router()
    coordinator, _ = _coordinator(
        router,
        components={"output_dir": tmp_path},
    )
    state = RuntimeSessionState(
        ctx=SimpleNamespace(messages=[{"role": "user", "content": "hello"}])
    )
    sink = ConsumingSink()

    asyncio.run(
        coordinator.handle(
            _turn("/export"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert len(sink.attachments) == 1
    assert Path(sink.attachments[0]).is_file()


def test_coordinator_flushes_all_attachments_before_drain() -> None:
    async def handler(request, context):
        return CommandResult(attachments=("report.md",))

    class LifecycleSink(_CoordinatorSink):
        def __init__(self) -> None:
            super().__init__()
            self.lifecycle: list[str] = []

        async def flush_attachments(self) -> None:
            assert self.attachments == ["report.md"]
            self.lifecycle.append("flush")

        async def drain(self) -> None:
            self.drain_count += 1
            self.lifecycle.append("drain")

    coordinator, _ = _coordinator(
        CommandRouter(core_commands=[CommandDescriptor("report", handler)])
    )
    sink = LifecycleSink()
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))

    asyncio.run(
        coordinator.handle(
            _turn("/report"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert sink.lifecycle[:2] == ["flush", "drain"]


def test_coordinator_drains_before_temp_cleanup_when_flush_raises(tmp_path) -> None:
    private_dir = tmp_path / ".send-test"
    private_dir.mkdir()
    attachment = private_dir / "report.txt"
    attachment.write_text("report", encoding="utf-8")

    async def handler(request, context):
        return CommandResult(
            attachments=(attachment,),
            temporary_attachments=(attachment,),
        )

    class FailingFlushSink(_CoordinatorSink):
        def __init__(self) -> None:
            super().__init__()
            self.exists_during_drain: list[bool] = []

        async def flush_attachments(self) -> None:
            assert attachment.is_file()
            raise RuntimeError("flush failed after scheduling")

        async def drain(self) -> None:
            self.drain_count += 1
            self.exists_during_drain.append(attachment.exists())

    coordinator, _ = _coordinator(
        CommandRouter(core_commands=[CommandDescriptor("report", handler)])
    )
    sink = FailingFlushSink()
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))

    asyncio.run(
        coordinator.handle(
            _turn("/report"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert sink.exists_during_drain[0] is True
    assert not attachment.exists()
    assert not private_dir.exists()


def test_coordinator_cleans_temp_when_sink_cleanup_handoff_raises(tmp_path) -> None:
    private_dir = tmp_path / ".send-test"
    private_dir.mkdir()
    attachment = private_dir / "report.txt"
    attachment.write_text("report", encoding="utf-8")

    async def handler(request, context):
        return CommandResult(
            attachments=(attachment,),
            temporary_attachments=(attachment,),
        )

    class RaisingHandoffSink(_CoordinatorSink):
        def defer_temporary_attachment_cleanup(self, path: Path) -> bool:
            raise RuntimeError("ownership was not transferred")

    coordinator, _ = _coordinator(
        CommandRouter(core_commands=[CommandDescriptor("report", handler)])
    )
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))

    asyncio.run(
        coordinator.handle(
            _turn("/report"),
            state,
            RaisingHandoffSink(),  # type: ignore[arg-type]
        )
    )

    assert not attachment.exists()
    assert not private_dir.exists()


def test_coordinator_matches_equal_temporary_attachments_to_distinct_receipts() -> None:
    first = Path("same-report.txt")
    second = Path("same-report.txt")
    assert first == second
    assert first is not second

    async def handler(request, context):
        return CommandResult(
            attachments=(first, second),
            temporary_attachments=(second, first),
        )

    class ReceiptSink(_CoordinatorSink):
        def __init__(self) -> None:
            super().__init__()
            self.receipts = [object(), object()]
            self.handoffs: list[object] = []

        def queue_attachment(self, attachment: object) -> object:
            self.attachments.append(attachment)
            return self.receipts[len(self.attachments) - 1]

        async def flush_attachments(self) -> None:
            return None

        def defer_temporary_attachment_cleanup(self, receipt: object) -> bool:
            self.handoffs.append(receipt)
            return True

    coordinator, _ = _coordinator(
        CommandRouter(core_commands=[CommandDescriptor("report", handler)])
    )
    sink = ReceiptSink()
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))

    asyncio.run(
        coordinator.handle(
            _turn("/report"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert sink.handoffs == [sink.receipts[1], sink.receipts[0]]


def test_coordinator_cleans_temporary_attachment_when_drain_raises(tmp_path) -> None:
    class RaisingDrainSink(_CoordinatorSink):
        async def drain(self) -> None:
            self.drain_count += 1
            raise RuntimeError("delivery failed")

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.txt").write_text("report", encoding="utf-8")
    coordinator, _ = _coordinator(
        _builtin_router(),
        components={"output_dir": output_dir},
    )
    state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))
    sink = RaisingDrainSink()

    asyncio.run(
        coordinator.handle(
            _turn("/send report.txt"),
            state,
            sink,  # type: ignore[arg-type]
        )
    )

    assert len(sink.attachments) == 1
    attachment = Path(sink.attachments[0])
    assert not attachment.exists()
    assert not attachment.parent.exists()


def test_coordinator_cleans_temporary_attachment_when_drain_is_cancelled(
    tmp_path,
) -> None:
    async def scenario() -> None:
        class BlockingDrainSink(_CoordinatorSink):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()

            async def drain(self) -> None:
                self.drain_count += 1
                if self.drain_count == 1:
                    self.started.set()
                    await asyncio.Event().wait()

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "report.txt").write_text("report", encoding="utf-8")
        coordinator, _ = _coordinator(
            _builtin_router(),
            components={"output_dir": output_dir},
        )
        state = RuntimeSessionState(ctx=SimpleNamespace(messages=[]))
        sink = BlockingDrainSink()
        running = asyncio.create_task(
            coordinator.handle(
                _turn("/send report.txt"),
                state,
                sink,  # type: ignore[arg-type]
            )
        )

        await sink.started.wait()
        attachment = Path(sink.attachments[0])
        assert attachment.is_file()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        assert not attachment.exists()
        assert not attachment.parent.exists()

    asyncio.run(scenario())


def _turn(text: str, *, message_id: str = "m-1") -> TurnInput:
    return TurnInput.from_text(
        text,
        session_id="s-1",
        channel_name="feishu",
        metadata={"message_id": message_id, "user_id": "u-1"},
    )


def test_coordinator_installs_fresh_active_operation_before_dispatch_await() -> None:
    async def scenario() -> None:
        old_token = CancelToken()
        state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}), cancel_token=old_token
        )
        coordinator, core = _coordinator()
        sink = _CoordinatorSink()

        await coordinator.handle(_turn("hello"), state, sink)  # type: ignore[arg-type]

        operation_state, accepts_interjections, token = core.state_snapshots[0]
        assert operation_state == "active"
        assert accepts_interjections is True
        assert token is not old_token
        assert state.operation_state == "idle"
        assert state.accepts_interjections is False
        assert state.cancel_token is None
        assert sink.drain_count >= 1

    asyncio.run(scenario())


def test_active_model_turn_queues_interjection_then_promotes_late_mailbox() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block_first(turn_input, state, sink):
            if turn_input.text == "first":
                started.set()
                await release.wait()

        core = _CoordinatorCore(block_first)
        coordinator, _ = _coordinator(core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        first_sink = _CoordinatorSink()
        second_sink = _CoordinatorSink()

        running = asyncio.create_task(
            coordinator.handle(_turn("first"), state, first_sink)  # type: ignore[arg-type]
        )
        await started.wait()
        await coordinator.handle(
            _turn("follow up", message_id="m-2"), state, second_sink
        )  # type: ignore[arg-type]

        assert [entry["text"] for entry in state.pending_interjections] == ["follow up"]
        assert state.restart_queue == []
        release.set()
        await running

        assert [call.text for call in core.calls] == ["first", "follow up"]
        assert state.pending_interjections == []
        assert state.restart_queue == []
        assert second_sink.drain_count >= 2

    asyncio.run(scenario())


def test_active_non_interjection_command_queues_restart_fifo() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_handler(request, context):
            assert context.components["dependency"] == "available"
            assert context.config["mode"] == "test"
            started.set()
            await release.wait()
            return CommandResult(response_text="command done")

        router = CommandRouter(
            core_commands=[CommandDescriptor("wait", blocking_handler)]
        )
        coordinator, core = _coordinator(router)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        command_sink = _CoordinatorSink()

        running = asyncio.create_task(
            coordinator.handle(_turn("/wait"), state, command_sink)  # type: ignore[arg-type]
        )
        await started.wait()
        queued_sinks = [_CoordinatorSink(), _CoordinatorSink()]
        await coordinator.handle(
            _turn("second", message_id="m-2"), state, queued_sinks[0]
        )  # type: ignore[arg-type]
        await coordinator.handle(
            _turn("third", message_id="m-3"), state, queued_sinks[1]
        )  # type: ignore[arg-type]

        assert state.pending_interjections == []
        assert [entry["text"] for entry in state.restart_queue] == ["second", "third"]
        release.set()
        await running

        assert [call.text for call in core.calls] == ["second", "third"]
        assert command_sink.statuses == [("command done", "info")]

    asyncio.run(scenario())


def test_now_routes_payload_for_idle_active_and_non_capable_operations() -> None:
    async def scenario() -> None:
        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("now", noop_interrupt, concurrency="interrupt")
            ]
        )
        idle_coordinator, idle_core = _coordinator(router)
        idle_state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        await idle_coordinator.handle(
            _turn("/now Do This"), idle_state, _CoordinatorSink()
        )  # type: ignore[arg-type]
        assert [call.text for call in idle_core.calls] == ["Do This"]

        blank_sink = _CoordinatorSink()
        await idle_coordinator.handle(_turn("/now"), idle_state, blank_sink)  # type: ignore[arg-type]
        assert blank_sink.statuses[-1][1] == "error"

        capable_state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state="active",
            accepts_interjections=True,
            cancel_token=CancelToken(),
        )
        await idle_coordinator.handle(
            _turn("/now urgent"), capable_state, _CoordinatorSink()
        )  # type: ignore[arg-type]
        assert capable_state.pending_interjections[0]["text"] == "urgent"
        assert capable_state.pending_interjections[0]["urgency"] == "now"

        non_capable_state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state="active",
            accepts_interjections=False,
            cancel_token=CancelToken(),
        )
        await idle_coordinator.handle(
            _turn("/now later"), non_capable_state, _CoordinatorSink()
        )  # type: ignore[arg-type]
        assert [entry["text"] for entry in non_capable_state.restart_queue] == ["later"]

    asyncio.run(scenario())


def test_cancel_graceful_upgrades_to_force_and_is_idempotent() -> None:
    async def scenario() -> None:
        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        coordinator, _ = _coordinator(router)
        token = CancelToken()
        state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state="active",
            accepts_interjections=True,
            cancel_token=token,
        )

        await coordinator.handle(_turn("/cancel graceful"), state, _CoordinatorSink())  # type: ignore[arg-type]
        assert state.operation_state == "cancelling"
        assert token.level == "graceful"

        await coordinator.handle(_turn("/cancel"), state, _CoordinatorSink())  # type: ignore[arg-type]
        await coordinator.handle(_turn("/cancel"), state, _CoordinatorSink())  # type: ignore[arg-type]
        assert token.level == "force"

    asyncio.run(scenario())


def test_cancel_restart_replaces_queue_and_discards_unapplied_interjections() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block_first(turn_input, state, sink):
            if turn_input.text == "first":
                started.set()
                await release.wait()

        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        core = _CoordinatorCore(block_first)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original_sink = _CoordinatorSink()
        running = asyncio.create_task(
            coordinator.handle(_turn("first"), state, original_sink)  # type: ignore[arg-type]
        )
        await started.wait()

        await coordinator.handle(_turn("unapplied"), state, _CoordinatorSink())  # type: ignore[arg-type]
        await coordinator.handle(_turn("/cancel old task"), state, _CoordinatorSink())  # type: ignore[arg-type]
        await coordinator.handle(_turn("after old"), state, _CoordinatorSink())  # type: ignore[arg-type]
        await coordinator.handle(
            _turn("/cancel newest task"), state, _CoordinatorSink()
        )  # type: ignore[arg-type]
        tail_sink = _CoordinatorSink()
        await coordinator.handle(_turn("tail"), state, tail_sink)  # type: ignore[arg-type]

        assert state.operation_state == "cancelling"
        assert [entry["text"] for entry in state.restart_queue] == [
            "newest task",
            "tail",
        ]
        release.set()
        await running

        assert [call.text for call in core.calls] == ["first", "newest task", "tail"]
        assert state.pending_interjections == []
        assert any(
            "1" in text and "unapplied" in text.lower()
            for text, _ in original_sink.statuses
        )

    asyncio.run(scenario())


def test_cancel_during_unwind_drain_discards_late_interjection_before_restart() -> None:
    async def scenario() -> None:
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()

        class BlockingDrainSink(_CoordinatorSink):
            async def drain(self) -> None:
                self.drain_count += 1
                if self.drain_count == 1:
                    drain_started.set()
                    await release_drain.wait()

        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        coordinator, core = _coordinator(router)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original_sink = BlockingDrainSink()
        running = asyncio.create_task(
            coordinator.handle(_turn("first"), state, original_sink)  # type: ignore[arg-type]
        )
        await drain_started.wait()

        await coordinator.handle(
            _turn("late interjection", message_id="m-2"),
            state,
            _CoordinatorSink(),  # type: ignore[arg-type]
        )
        await coordinator.handle(
            _turn("/cancel replacement", message_id="m-3"),
            state,
            _CoordinatorSink(),  # type: ignore[arg-type]
        )
        release_drain.set()
        await running

        assert [call.text for call in core.calls] == ["first", "replacement"]
        assert state.pending_interjections == []
        assert state.restart_queue == []
        assert any(
            "1" in text and "unapplied" in text.lower()
            for text, _ in original_sink.statuses
        )

    asyncio.run(scenario())


def test_cancellation_report_failures_cannot_bypass_cleanup_or_restart() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class RaisingCleanupSink(_CoordinatorSink):
            def on_status(self, text: str, *, level: str = "info") -> None:
                if "unapplied" in text.lower():
                    raise RuntimeError("status output failed")
                super().on_status(text, level=level)

            async def drain(self) -> None:
                self.drain_count += 1
                raise RuntimeError("sink drain failed")

        async def block_first(turn_input, state, sink):
            if turn_input.text == "first":
                started.set()
                await release.wait()

        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        core = _CoordinatorCore(block_first)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original_sink = RaisingCleanupSink()
        running = asyncio.create_task(
            coordinator.handle(_turn("first"), state, original_sink)  # type: ignore[arg-type]
        )
        await started.wait()
        await coordinator.handle(
            _turn("unapplied", message_id="m-2"),
            state,
            _CoordinatorSink(),  # type: ignore[arg-type]
        )
        await coordinator.handle(
            _turn("/cancel replacement", message_id="m-3"),
            state,
            _CoordinatorSink(),  # type: ignore[arg-type]
        )
        release.set()
        await running

        assert [call.text for call in core.calls] == ["first", "replacement"]
        assert state.operation_state == "idle"
        assert state.accepts_interjections is False
        assert state.cancel_token is None
        assert state.pending_interjections == []
        assert state.restart_queue == []
        assert original_sink.errors == []

    asyncio.run(scenario())


def test_replacement_waits_for_originating_sink_drain_before_dispatch() -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        first_returning = asyncio.Event()
        allow_replacement_return = asyncio.Event()

        class SnapshotClearingSink(_CoordinatorSink):
            def __init__(self) -> None:
                super().__init__()
                self._pending: list[asyncio.Task] = []
                self.snapshot_drained = asyncio.Event()
                self.allow_snapshot_clear = asyncio.Event()
                self.output_scheduled = asyncio.Event()
                self.finish_output = asyncio.Event()
                self.output_finished = asyncio.Event()
                self._paused_once = False

            def on_status(self, text: str, *, level: str = "info") -> None:
                super().on_status(text, level=level)

                async def deliver() -> None:
                    if text == "replacement output":
                        self.output_scheduled.set()
                        await self.finish_output.wait()
                        self.output_finished.set()

                self._pending.append(asyncio.create_task(deliver()))

            async def drain(self) -> None:
                self.drain_count += 1
                while self._pending:
                    snapshot = list(self._pending)
                    await asyncio.gather(*snapshot)
                    if not self._paused_once:
                        self._paused_once = True
                        self.snapshot_drained.set()
                        await self.allow_snapshot_clear.wait()
                    self._pending.clear()

        async def core_behavior(turn_input, state, sink):
            if turn_input.text == "first":
                first_started.set()
                await release_first.wait()
                first_returning.set()
            elif turn_input.text == "replacement":
                sink.on_status("replacement output")
                await allow_replacement_return.wait()

        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        core = _CoordinatorCore(core_behavior)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original_handle = asyncio.create_task(
            coordinator.handle(_turn("first"), state, _CoordinatorSink())  # type: ignore[arg-type]
        )
        await first_started.wait()

        replacement_sink = SnapshotClearingSink()
        replacement_handle = asyncio.create_task(
            coordinator.handle(
                _turn("/cancel replacement", message_id="m-2"),
                state,
                replacement_sink,  # type: ignore[arg-type]
            )
        )
        await replacement_sink.snapshot_drained.wait()

        release_first.set()
        await first_returning.wait()
        await asyncio.sleep(0)
        scheduled_before_originating_drain = replacement_sink.output_scheduled.is_set()

        replacement_sink.allow_snapshot_clear.set()
        await replacement_sink.output_scheduled.wait()
        allow_replacement_return.set()
        done, _ = await asyncio.wait(
            {original_handle, replacement_handle},
            timeout=0.05,
            return_when=asyncio.ALL_COMPLETED,
        )
        returned_before_output_finished = len(done) == 2

        replacement_sink.finish_output.set()
        await asyncio.gather(original_handle, replacement_handle)

        assert scheduled_before_originating_drain is False
        assert returned_before_output_finished is False
        assert replacement_sink.output_finished.is_set()

    asyncio.run(scenario())


def test_cancelled_originating_drain_always_releases_queued_owner() -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        first_returning = asyncio.Event()

        class BlockingDrainSink(_CoordinatorSink):
            def __init__(self) -> None:
                super().__init__()
                self.drain_started = asyncio.Event()
                self.release_drain = asyncio.Event()

            async def drain(self) -> None:
                self.drain_count += 1
                self.drain_started.set()
                await self.release_drain.wait()

        async def core_behavior(turn_input, state, sink):
            if turn_input.text == "first":
                first_started.set()
                await release_first.wait()
                first_returning.set()

        async def noop_interrupt(request, context):
            return CommandResult()

        router = CommandRouter(
            core_commands=[
                CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
            ]
        )
        core = _CoordinatorCore(core_behavior)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        owner = asyncio.create_task(
            coordinator.handle(_turn("first"), state, _CoordinatorSink())  # type: ignore[arg-type]
        )
        await first_started.wait()

        queued_sink = BlockingDrainSink()
        originating = asyncio.create_task(
            coordinator.handle(
                _turn("/cancel replacement", message_id="m-2"),
                state,
                queued_sink,  # type: ignore[arg-type]
            )
        )
        await queued_sink.drain_started.wait()

        release_first.set()
        await first_returning.wait()
        await asyncio.sleep(0)
        assert state.operation_state == "active"
        assert [call.text for call in core.calls] == ["first"]

        originating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await originating
        queued_sink.release_drain.set()

        done, _ = await asyncio.wait({owner}, timeout=0.05)
        if owner not in done:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

        assert owner in done
        assert [call.text for call in core.calls] == ["first", "replacement"]
        assert state.operation_state == "idle"
        assert state.accepts_interjections is False
        assert state.cancel_token is None
        assert state.restart_queue == []

    asyncio.run(scenario())


def test_idle_cancel_is_noop_but_cancel_payload_forwards_normally() -> None:
    async def noop_interrupt(request, context):
        return CommandResult()

    router = CommandRouter(
        core_commands=[
            CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt")
        ]
    )
    coordinator, core = _coordinator(router)
    state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
    no_op_sink = _CoordinatorSink()

    asyncio.run(coordinator.handle(_turn("/cancel"), state, no_op_sink))  # type: ignore[arg-type]
    asyncio.run(
        coordinator.handle(_turn("/cancel new work"), state, _CoordinatorSink())
    )  # type: ignore[arg-type]

    assert "no active" in no_op_sink.statuses[0][0].lower()
    assert [call.text for call in core.calls] == ["new work"]


@pytest.mark.parametrize("operation_state", ["idle", "active", "cancelling"])
def test_unknown_slash_is_deterministic_and_never_queues_or_forwards(
    operation_state: str,
) -> None:
    coordinator, core = _coordinator(
        CommandRouter(core_commands=[CommandDescriptor("help", _noop_handler)])
    )
    state = RuntimeSessionState(
        ctx=SimpleNamespace(metadata={}),
        operation_state=operation_state,  # type: ignore[arg-type]
        accepts_interjections=operation_state == "active",
        cancel_token=CancelToken() if operation_state != "idle" else None,
    )
    sink = _CoordinatorSink()

    asyncio.run(coordinator.handle(_turn("/hep"), state, sink))  # type: ignore[arg-type]

    assert sink.statuses == [("Unknown command '/hep'. Did you mean /help?", "error")]
    assert state.pending_interjections == []
    assert state.restart_queue == []
    assert core.calls == []


def test_explicit_skill_forwards_only_while_idle_and_is_busy_otherwise(
    tmp_path,
) -> None:
    router = CommandRouter(skill_catalog=_skill_catalog(tmp_path, ("review", True)))
    coordinator, core = _coordinator(router)
    idle = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))

    asyncio.run(coordinator.handle(_turn("/review focus"), idle, _CoordinatorSink()))  # type: ignore[arg-type]
    assert [call.text for call in core.calls] == ["/review focus"]

    for operation_state in ("active", "cancelling"):
        state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state=operation_state,  # type: ignore[arg-type]
            accepts_interjections=True,
            cancel_token=CancelToken(),
        )
        sink = _CoordinatorSink()
        asyncio.run(coordinator.handle(_turn("/review focus"), state, sink))  # type: ignore[arg-type]
        assert sink.statuses[-1][1] == "error"
        assert "busy" in sink.statuses[-1][0].lower()
        assert state.pending_interjections == []


def test_anytime_command_runs_while_busy_and_idle_only_command_is_rejected() -> None:
    calls: list[str] = []

    async def handler(request, context):
        calls.append(request.name)
        return CommandResult(response_text=request.name)

    router = CommandRouter(
        core_commands=[
            CommandDescriptor("status", handler, concurrency="anytime"),
            CommandDescriptor("change", handler, concurrency="idle_only"),
        ]
    )
    coordinator, _ = _coordinator(router)

    for operation_state in ("active", "cancelling"):
        state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state=operation_state,  # type: ignore[arg-type]
            cancel_token=CancelToken(),
        )
        status_sink = _CoordinatorSink()
        change_sink = _CoordinatorSink()
        asyncio.run(coordinator.handle(_turn("/status"), state, status_sink))  # type: ignore[arg-type]
        asyncio.run(coordinator.handle(_turn("/change"), state, change_sink))  # type: ignore[arg-type]
        assert status_sink.statuses == [("status", "info")]
        assert "busy" in change_sink.statuses[0][0].lower()

    assert calls == ["status", "status"]


def test_busy_anytime_forward_waits_for_unwind_and_its_own_sink_drain() -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()
        handler_returning = asyncio.Event()
        forwarded_started = asyncio.Event()

        class BlockingDrainSink(_CoordinatorSink):
            def __init__(self) -> None:
                super().__init__()
                self.drain_started = asyncio.Event()
                self.release_drain = asyncio.Event()

            async def drain(self) -> None:
                self.drain_count += 1
                self.drain_started.set()
                await self.release_drain.wait()

        async def core_behavior(turn_input, state, sink):
            if turn_input.text == "first":
                first_started.set()
                await release_first.wait()
            elif turn_input.text == "deferred prompt":
                forwarded_started.set()

        async def anytime_handler(request, context):
            handler_started.set()
            await release_handler.wait()
            handler_returning.set()
            return CommandResult(forward_text="deferred prompt")

        router = CommandRouter(
            core_commands=[
                CommandDescriptor(
                    "inspect",
                    anytime_handler,
                    concurrency="anytime",
                )
            ]
        )
        core = _CoordinatorCore(core_behavior)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original = asyncio.create_task(
            coordinator.handle(_turn("first"), state, _CoordinatorSink())  # type: ignore[arg-type]
        )
        await first_started.wait()

        command_sink = BlockingDrainSink()
        command = asyncio.create_task(
            coordinator.handle(
                _turn("/inspect", message_id="m-2"),
                state,
                command_sink,  # type: ignore[arg-type]
            )
        )
        await handler_started.wait()
        release_first.set()
        await original
        assert state.operation_state == "idle"

        release_handler.set()
        await handler_returning.wait()
        await asyncio.sleep(0)
        forwarded_before_sink_ready = forwarded_started.is_set()

        await command_sink.drain_started.wait()
        command_sink.release_drain.set()
        await command

        assert forwarded_before_sink_ready is False
        assert [call.text for call in core.calls] == ["first", "deferred prompt"]
        forwarded_state, accepts_interjections, forwarded_token = core.state_snapshots[
            1
        ]
        assert forwarded_state == "active"
        assert accepts_interjections is True
        assert forwarded_token is not None
        assert state.operation_state == "idle"
        assert state.cancel_token is None

    asyncio.run(scenario())


def test_busy_forward_survives_response_and_drain_sink_failures() -> None:
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        class RaisingSink(_CoordinatorSink):
            def on_status(self, text: str, *, level: str = "info") -> None:
                raise RuntimeError("status failed")

            def on_error(self, error: str) -> None:
                raise RuntimeError("error output failed")

            async def drain(self) -> None:
                raise RuntimeError("drain failed")

        async def core_behavior(turn_input, state, sink):
            if turn_input.text == "first":
                first_started.set()
                await release_first.wait()

        async def anytime_handler(request, context):
            return CommandResult(
                response_text="acknowledged",
                forward_text="deferred prompt",
            )

        router = CommandRouter(
            core_commands=[
                CommandDescriptor(
                    "inspect",
                    anytime_handler,
                    concurrency="anytime",
                )
            ]
        )
        core = _CoordinatorCore(core_behavior)
        coordinator, _ = _coordinator(router, core=core)
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
        original = asyncio.create_task(
            coordinator.handle(_turn("first"), state, _CoordinatorSink())  # type: ignore[arg-type]
        )
        await first_started.wait()

        await coordinator.handle(
            _turn("/inspect", message_id="m-2"),
            state,
            RaisingSink(),  # type: ignore[arg-type]
        )
        assert [entry["text"] for entry in state.restart_queue] == ["deferred prompt"]

        release_first.set()
        await original

        assert [call.text for call in core.calls] == ["first", "deferred prompt"]
        assert state.operation_state == "idle"
        assert state.restart_queue == []

    asyncio.run(scenario())


def test_command_result_is_rendered_forwarded_and_returns_action() -> None:
    async def handler(request, context):
        return CommandResult(
            response_text="portable response",
            attachments=["report.md"],
            forward_text="expanded prompt",
            action="exit_cli",
            level="warning",
        )

    router = CommandRouter(
        core_commands=[CommandDescriptor("expand", handler, accepts_interjections=True)]
    )
    coordinator, core = _coordinator(router)
    sink = _CoordinatorSink()
    state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))

    action = asyncio.run(coordinator.handle(_turn("/expand"), state, sink))  # type: ignore[arg-type]

    assert action == "exit_cli"
    assert sink.statuses == [("portable response", "warning")]
    assert sink.attachments == ["report.md"]
    assert [call.text for call in core.calls] == ["expanded prompt"]
    assert sink.drain_count >= 1


def test_coordinator_emits_command_lifecycle_events() -> None:
    async def ok(request, context):
        return CommandResult(response_text="ok", forward_text="prompt")

    async def fail(request, context):
        raise RuntimeError("private details")

    router = CommandRouter(
        core_commands=[
            CommandDescriptor("ok", ok),
            CommandDescriptor("fail", fail),
            CommandDescriptor("idle", ok),
        ]
    )
    events: list = []
    coordinator, _ = _coordinator(router, events=events)
    idle = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))

    asyncio.run(coordinator.handle(_turn("/ok"), idle, _CoordinatorSink()))  # type: ignore[arg-type]
    asyncio.run(coordinator.handle(_turn("/fail"), idle, _CoordinatorSink()))  # type: ignore[arg-type]
    busy = RuntimeSessionState(
        ctx=SimpleNamespace(metadata={}),
        operation_state="active",
        cancel_token=CancelToken(),
    )
    asyncio.run(coordinator.handle(_turn("/idle"), busy, _CoordinatorSink()))  # type: ignore[arg-type]

    names = [event.name for event in events]
    assert "command_received" in names
    assert "command_handled" in names
    assert "command_forwarded" in names
    assert "command_failed" in names
    assert "command_rejected" in names
    assert all(event.session_id == "s-1" for event in events)
    assert all(event.channel_name == "feishu" for event in events)


@pytest.mark.parametrize(
    ("invocation", "payload", "command_name"),
    [
        ("/now redirected work", "redirected work", "now"),
        ("/cancel replacement work", "replacement work", "cancel"),
    ],
)
def test_idle_interrupt_forward_events_preserve_originating_command(
    invocation: str,
    payload: str,
    command_name: str,
) -> None:
    async def noop_interrupt(request, context):
        return CommandResult()

    router = CommandRouter(
        core_commands=[
            CommandDescriptor("now", noop_interrupt, concurrency="interrupt"),
            CommandDescriptor("cancel", noop_interrupt, concurrency="interrupt"),
        ]
    )
    events: list = []
    coordinator, core = _coordinator(router, events=events)
    state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))

    asyncio.run(coordinator.handle(_turn(invocation), state, _CoordinatorSink()))  # type: ignore[arg-type]

    assert [call.text for call in core.calls] == [payload]
    assert [(event.name, dict(event.fields)) for event in events] == [
        ("command_received", {"command": command_name}),
        (
            "command_forwarded",
            {"command": command_name, "target": "model"},
        ),
        (
            "command_handled",
            {"command": command_name, "outcome": "forwarded"},
        ),
    ]


def test_coordinator_exception_is_stable_and_always_cleans_state() -> None:
    class ExplodingRouter:
        def classify(self, *args, **kwargs):
            raise RuntimeError("token=secret /private/path")

    coordinator, _ = _coordinator(ExplodingRouter())  # type: ignore[arg-type]
    state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}))
    sink = _CoordinatorSink()

    action = asyncio.run(coordinator.handle(_turn("hello"), state, sink))  # type: ignore[arg-type]

    assert action is None
    assert sink.errors == ["Command handling failed."]
    assert "secret" not in repr(sink.errors)
    assert state.operation_state == "idle"
    assert state.accepts_interjections is False
    assert sink.drain_count >= 1
