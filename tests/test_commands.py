from __future__ import annotations

from dataclasses import FrozenInstanceError
import asyncio
from types import SimpleNamespace

import pytest

from agent.commands import (
    CommandCoordinator,
    CommandContext,
    CommandDescriptor,
    CommandRequest,
    CommandResult,
    CommandRouter,
    parse_command,
)
from agent.runtime import RuntimeSessionState, TurnInput, TurnResult
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
    assert result.forward_text is None
    assert result.action is None
    assert result.level == "info"
    assert result.error is None


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


@pytest.mark.parametrize("invocation", ["/review Focus HERE", "/skill review Focus HERE"])
def test_classify_recognizes_explicit_user_invocable_skill(
    tmp_path, invocation: str
) -> None:
    router = CommandRouter(
        skill_catalog=_skill_catalog(
            tmp_path, ("review", True), ("internal", False)
        )
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
                (
                    bundle
                    for bundle in (internal, public)
                    if bundle.id == skill_ref
                ),
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
    router = CommandRouter(
        skill_catalog=_skill_catalog(tmp_path, ("internal", False))
    )

    assert router.classify(invocation).kind == "unknown_slash"


def test_unknown_slash_has_close_command_suggestions() -> None:
    router = CommandRouter(
        core_commands=[CommandDescriptor("help", _noop_handler)]
    )

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
    router = CommandRouter(
        skill_catalog=_skill_catalog(tmp_path, ("review", True))
    )

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
        router.register_plugin(CommandDescriptor("other", _noop_handler, aliases=("h",)))


def test_out_of_scope_core_name_cannot_be_shadowed_by_direct_skill(tmp_path) -> None:
    router = CommandRouter(
        core_commands=[
            CommandDescriptor(
                "quit", _noop_handler, scopes=frozenset({"cli"})
            )
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
        router.register_plugin(CommandDescriptor("debug", _noop_handler, aliases=("d",)))


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
) -> tuple[CommandCoordinator, _CoordinatorCore]:
    fake_core = core or _CoordinatorCore()
    coordinator = CommandCoordinator(
        fake_core,  # type: ignore[arg-type]
        router or CommandRouter(),
        components={"dependency": "available"},
        config={"mode": "test"},
        event_hook=None if events is None else events.append,
    )
    return coordinator, fake_core


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
        state = RuntimeSessionState(ctx=SimpleNamespace(metadata={}), cancel_token=old_token)
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
        await coordinator.handle(_turn("follow up", message_id="m-2"), state, second_sink)  # type: ignore[arg-type]

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
        await coordinator.handle(_turn("second", message_id="m-2"), state, queued_sinks[0])  # type: ignore[arg-type]
        await coordinator.handle(_turn("third", message_id="m-3"), state, queued_sinks[1])  # type: ignore[arg-type]

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
        await idle_coordinator.handle(_turn("/now Do This"), idle_state, _CoordinatorSink())  # type: ignore[arg-type]
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
        await idle_coordinator.handle(_turn("/now urgent"), capable_state, _CoordinatorSink())  # type: ignore[arg-type]
        assert capable_state.pending_interjections[0]["text"] == "urgent"
        assert capable_state.pending_interjections[0]["urgency"] == "now"

        non_capable_state = RuntimeSessionState(
            ctx=SimpleNamespace(metadata={}),
            operation_state="active",
            accepts_interjections=False,
            cancel_token=CancelToken(),
        )
        await idle_coordinator.handle(_turn("/now later"), non_capable_state, _CoordinatorSink())  # type: ignore[arg-type]
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
        await coordinator.handle(_turn("/cancel newest task"), state, _CoordinatorSink())  # type: ignore[arg-type]
        tail_sink = _CoordinatorSink()
        await coordinator.handle(_turn("tail"), state, tail_sink)  # type: ignore[arg-type]

        assert state.operation_state == "cancelling"
        assert [entry["text"] for entry in state.restart_queue] == ["newest task", "tail"]
        release.set()
        await running

        assert [call.text for call in core.calls] == ["first", "newest task", "tail"]
        assert state.pending_interjections == []
        assert any("1" in text and "unapplied" in text.lower() for text, _ in original_sink.statuses)

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
    asyncio.run(coordinator.handle(_turn("/cancel new work"), state, _CoordinatorSink()))  # type: ignore[arg-type]

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


def test_explicit_skill_forwards_only_while_idle_and_is_busy_otherwise(tmp_path) -> None:
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
        core_commands=[
            CommandDescriptor("expand", handler, accepts_interjections=True)
        ]
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
