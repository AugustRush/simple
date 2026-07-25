from __future__ import annotations

from dataclasses import FrozenInstanceError
import asyncio

import pytest

from agent.commands import (
    CommandContext,
    CommandDescriptor,
    CommandRequest,
    CommandResult,
    CommandRouter,
    parse_command,
)
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
