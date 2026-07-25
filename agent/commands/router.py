from __future__ import annotations

from dataclasses import dataclass
import difflib
import logging
from typing import Any, Iterable, Literal, Mapping, TypeAlias

from agent.skills.catalog import parse_explicit_skill_request

from .models import CommandContext, CommandDescriptor, CommandRequest, CommandResult

logger = logging.getLogger(__name__)

ClassificationKind: TypeAlias = Literal["command", "skill", "unknown_slash", "text"]
CommandSource: TypeAlias = Literal["core", "plugin"]


@dataclass(frozen=True)
class CommandClassification:
    """Pure routing decision produced before command side effects are allowed."""

    kind: ClassificationKind
    text: str
    request: CommandRequest | None = None
    descriptor: CommandDescriptor | None = None
    source: CommandSource | None = None
    skill_id: str | None = None
    skill_args: str = ""
    skill_ref: str | None = None
    skill_error: Literal["unknown", "ambiguous"] | None = None
    suggestions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "suggestions", tuple(self.suggestions))


def parse_command(
    text: str,
    *,
    channel_name: str = "cli",
    session_id: str = "default",
    metadata: Mapping[str, Any] | None = None,
) -> CommandRequest | None:
    """Parse slash-prefixed text without changing argument casing."""

    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    command_text = stripped[1:]
    parts = command_text.split(maxsplit=1)
    name = parts[0] if parts else ""
    args = parts[1] if len(parts) == 2 else ""
    return CommandRequest(
        original_text=text,
        name=name,
        args=args,
        channel_name=channel_name,
        session_id=session_id,
        metadata=metadata or {},
    )


def _in_scope(descriptor: CommandDescriptor, channel_name: str) -> bool:
    return "all" in descriptor.scopes or channel_name.casefold() in descriptor.scopes


class _AmbiguousSkillReference(Exception):
    def __init__(self, skill_ref: str, candidates: tuple[str, ...]) -> None:
        self.skill_ref = skill_ref
        self.candidates = candidates
        super().__init__(skill_ref)


class CommandRouter:
    """Deterministic command and explicit-skill classifier and dispatcher."""

    def __init__(
        self,
        *,
        core_commands: Iterable[CommandDescriptor] = (),
        plugin_commands: Iterable[CommandDescriptor] = (),
        skill_catalog: Any = None,
    ) -> None:
        self._core_commands: list[CommandDescriptor] = []
        self._plugin_commands: list[CommandDescriptor] = []
        self._core_lookup: dict[str, CommandDescriptor] = {}
        self._plugin_lookup: dict[str, CommandDescriptor] = {}
        self._skill_catalog = skill_catalog
        for descriptor in core_commands:
            self.register_core(descriptor)
        for descriptor in plugin_commands:
            self.register_plugin(descriptor)

    @staticmethod
    def _registration_names(descriptor: CommandDescriptor) -> tuple[str, ...]:
        names = (descriptor.name, *descriptor.aliases)
        if len(names) != len(set(names)):
            raise ValueError(
                f"command {descriptor.name!r} repeats its canonical name or alias"
            )
        return names

    def register_core(self, descriptor: CommandDescriptor) -> None:
        names = self._registration_names(descriptor)
        conflict = next((name for name in names if name in self._core_lookup), None)
        if conflict is not None:
            raise ValueError(f"duplicate core command name or alias: /{conflict}")
        plugin_conflict = next(
            (name for name in names if name in self._plugin_lookup), None
        )
        if plugin_conflict is not None:
            raise ValueError(
                "plugin command conflicts with reserved core command: "
                f"/{plugin_conflict}"
            )
        self._core_commands.append(descriptor)
        for name in names:
            self._core_lookup[name] = descriptor

    def register_plugin(self, descriptor: CommandDescriptor) -> None:
        names = self._registration_names(descriptor)
        reserved = next((name for name in names if name in self._core_lookup), None)
        if reserved is not None:
            raise ValueError(f"reserved core command name or alias: /{reserved}")
        duplicate = next(
            (name for name in names if name in self._plugin_lookup), None
        )
        if duplicate is not None:
            raise ValueError(f"duplicate plugin command name or alias: /{duplicate}")
        self._plugin_commands.append(descriptor)
        for name in names:
            self._plugin_lookup[name] = descriptor

    def _command_match(
        self, request: CommandRequest
    ) -> tuple[CommandDescriptor, CommandSource] | None:
        descriptor = self._core_lookup.get(request.name)
        if descriptor is not None:
            if _in_scope(descriptor, request.channel_name):
                return descriptor, "core"
            return None
        descriptor = self._plugin_lookup.get(request.name)
        if descriptor is not None and _in_scope(descriptor, request.channel_name):
            return descriptor, "plugin"
        return None

    def _explicit_skill(self, request: CommandRequest) -> tuple[str, str] | None:
        if self._skill_catalog is None:
            return None
        parsed = parse_explicit_skill_request(request.original_text)
        if parsed is None:
            return None
        skill_ref = parsed.skill_ref if request.name == "skill" else request.name
        candidates = sorted(
            (
                bundle
                for bundle in self._skill_catalog.list_skills()
                if bundle.user_invocable
                and bundle.id.casefold() == skill_ref.casefold()
            ),
            key=lambda bundle: bundle.id,
        )
        if len(candidates) > 1:
            raise _AmbiguousSkillReference(
                skill_ref, tuple(bundle.id for bundle in candidates)
            )
        bundle = candidates[0] if candidates else self._skill_catalog.get(skill_ref)
        if bundle is None or not bundle.user_invocable:
            return None
        return bundle.id, parsed.remaining_text

    def _suggestions(self, name: str, channel_name: str) -> tuple[str, ...]:
        candidates = {
            descriptor.name
            for descriptor in (*self._core_commands, *self._plugin_commands)
            if _in_scope(descriptor, channel_name)
        }
        if self._skill_catalog is not None:
            candidates.update(
                bundle.id
                for bundle in self._skill_catalog.list_skills()
                if bundle.user_invocable
            )
        return self._close_matches(name, candidates)

    def _skill_suggestions(self, skill_ref: str) -> tuple[str, ...]:
        if self._skill_catalog is None:
            return ()
        return self._close_matches(
            skill_ref,
            (
                bundle.id
                for bundle in self._skill_catalog.list_skills()
                if bundle.user_invocable
            ),
        )

    @staticmethod
    def _close_matches(name: str, candidates: Iterable[str]) -> tuple[str, ...]:
        by_casefold: dict[str, list[str]] = {}
        for candidate in sorted(set(candidates)):
            by_casefold.setdefault(candidate.casefold(), []).append(candidate)
        matches = difflib.get_close_matches(
            name.casefold(), sorted(by_casefold), n=3, cutoff=0.6
        )
        return tuple(
            candidate
            for match in matches
            for candidate in by_casefold[match]
        )[:3]

    def classify(
        self,
        value: str | CommandRequest,
        *,
        channel_name: str = "cli",
        session_id: str = "default",
        metadata: Mapping[str, Any] | None = None,
    ) -> CommandClassification:
        if isinstance(value, CommandRequest):
            request = value
            text = value.original_text
        else:
            text = value
            request = parse_command(
                value,
                channel_name=channel_name,
                session_id=session_id,
                metadata=metadata,
            )
        if request is None:
            return CommandClassification(kind="text", text=text)

        match = self._command_match(request)
        if match is not None:
            descriptor, source = match
            return CommandClassification(
                kind="command",
                text=text,
                request=request,
                descriptor=descriptor,
                source=source,
            )

        # Core names remain reserved even where their transport scope excludes
        # execution, so a direct skill invocation cannot acquire that name.
        if request.name in self._core_lookup:
            return CommandClassification(
                kind="unknown_slash",
                text=text,
                request=request,
                suggestions=self._suggestions(request.name, request.channel_name),
            )

        try:
            skill = self._explicit_skill(request)
        except _AmbiguousSkillReference as exc:
            return CommandClassification(
                kind="unknown_slash",
                text=text,
                request=request,
                skill_ref=exc.skill_ref,
                skill_error="ambiguous",
                suggestions=exc.candidates,
            )
        if skill is not None:
            skill_id, skill_args = skill
            return CommandClassification(
                kind="skill",
                text=text,
                request=request,
                skill_id=skill_id,
                skill_args=skill_args,
            )

        parsed_skill = parse_explicit_skill_request(request.original_text)
        if request.name == "skill" and parsed_skill is not None:
            return CommandClassification(
                kind="unknown_slash",
                text=text,
                request=request,
                skill_ref=parsed_skill.skill_ref,
                skill_error="unknown",
                suggestions=self._skill_suggestions(parsed_skill.skill_ref),
            )
        return CommandClassification(
            kind="unknown_slash",
            text=text,
            request=request,
            suggestions=self._suggestions(request.name, request.channel_name),
        )

    async def execute(
        self,
        classification: CommandClassification,
        context: CommandContext,
    ) -> CommandResult:
        if classification.kind == "command":
            if classification.descriptor is None or classification.request is None:
                return CommandResult(
                    response_text="Invalid command classification.",
                    level="error",
                    error="missing command descriptor or request",
                )
            try:
                result = await classification.descriptor.handler(
                    classification.request, context
                )
                if not isinstance(result, CommandResult):
                    raise TypeError("command handler must return CommandResult")
                return result
            except Exception:
                logger.exception(
                    "command execution failed: command=%s session_id=%s",
                    classification.descriptor.name,
                    classification.request.session_id,
                )
                failure_message = (
                    f"Command /{classification.descriptor.name} failed."
                )
                return CommandResult(
                    response_text=failure_message,
                    level="error",
                    error=failure_message,
                )

        if classification.kind == "skill":
            return CommandResult(forward_text=classification.text)
        if classification.kind == "text":
            return CommandResult(handled=False, forward_text=classification.text)

        if classification.skill_error == "ambiguous":
            matches = ", ".join(classification.suggestions)
            response = (
                f"Ambiguous skill invocation '{classification.skill_ref}'. "
                f"Matches: {matches}."
            )
            return CommandResult(response_text=response, level="error")
        if classification.skill_error == "unknown":
            response = f"Unknown skill '{classification.skill_ref}'."
            if classification.suggestions:
                choices = ", ".join(
                    f"/skill {name}" for name in classification.suggestions
                )
                response += f" Did you mean {choices}?"
            return CommandResult(response_text=response, level="error")

        request_name = (
            classification.request.name if classification.request is not None else ""
        )
        response = f"Unknown command '/{request_name}'."
        if classification.suggestions:
            choices = ", ".join(f"/{name}" for name in classification.suggestions)
            response += f" Did you mean {choices}?"
        return CommandResult(response_text=response, level="error")

    def help_text(self, channel_name: str) -> str:
        """Render command help from registrations visible to one channel."""

        descriptors = (
            descriptor
            for descriptor in (*self._core_commands, *self._plugin_commands)
            if _in_scope(descriptor, channel_name)
        )
        lines = []
        for descriptor in sorted(descriptors, key=lambda item: item.name):
            usage = descriptor.usage or f"/{descriptor.name}"
            line = usage
            if descriptor.description:
                line += f" - {descriptor.description}"
            lines.append(line)
        return "\n".join(lines)
