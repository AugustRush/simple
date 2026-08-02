from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any, Mapping

from agent import shared

from .models import CommandContext, CommandDescriptor, CommandRequest, CommandResult
from .router import CommandRouter


def _ralph_service(context: CommandContext) -> Any:
    return _component(context, "ralph_service")


def _ralph_observer(context: CommandContext):
    def observe(event: Any) -> None:
        status = getattr(event, "status", "")
        status_text = getattr(status, "value", str(status))
        context.sink.on_status(
            f"Ralph {event.task_id} | {status_text} | "
            f"iteration {event.iteration}/{event.max_iterations}"
        )

    return observe


async def _ralph_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    from agent.ralph import (
        RalphParseError,
        RalphListCommand,
        RalphResumeCommand,
        RalphRunResult,
        RalphStoreError,
        RalphValidationError,
        parse_ralph_command,
    )

    service = _ralph_service(context)
    if service is None:
        return _error("Ralph service is unavailable.")
    try:
        parsed = parse_ralph_command(request.args)
        if isinstance(parsed, RalphListCommand):
            tasks = service.list_tasks()
            if not tasks:
                return CommandResult(response_text="No Ralph tasks found.")
            lines = ["Ralph tasks:"]
            lines.extend(
                f"- `{task.id}` | {task.status.value} | "
                f"{task.current_iteration}/{task.max_iterations} | {task.goal}"
                for task in tasks
            )
            return CommandResult(response_text="\n".join(lines))
        observer = _ralph_observer(context)
        if isinstance(parsed, RalphResumeCommand):
            outcome = await service.resume(
                parsed.task_id_prefix,
                context.session_state,
                observer=observer,
            )
        else:
            outcome = await service.start(
                parsed.goal,
                context.session_state,
                max_iterations=parsed.max_iterations,
                verify_command=parsed.verify_command,
                observer=observer,
            )
        if not isinstance(outcome, RalphRunResult):
            return _error("Ralph service returned an invalid result.")
        if outcome.durability_error:
            return _error(outcome.durability_error)
        task = outcome.task
        return CommandResult(
            response_text=(
                f"Ralph `{task.id}` {task.status.value} after "
                f"{task.current_iteration}/{task.max_iterations} iteration(s)."
            )
        )
    except (RalphParseError, RalphStoreError, RalphValidationError) as exc:
        return _error(str(exc))

_DEFAULT_MAX_SEND_SNAPSHOT_BYTES = 32 * 1024 * 1024


class _SnapshotTooLarge(Exception):
    pass


class _SnapshotChanged(Exception):
    pass


class _UnsafeSendPath(Exception):
    pass


def _error(message: str) -> CommandResult:
    return CommandResult(response_text=message, level="error")


def _warning(message: str) -> CommandResult:
    return CommandResult(response_text=message, level="warning")


def _component(context: CommandContext, name: str) -> Any:
    return context.components.get(name)


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _markdown_inline(value: Any) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    text = text.replace("\\", "\\\\")
    for character in ("`", "*", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, f"\\{character}")
    return text


def _require_no_args(request: CommandRequest, usage: str) -> CommandResult | None:
    if request.args:
        return _error(f"Usage: {usage}")
    return None


def _context_manager(context: CommandContext) -> Any:
    manager = getattr(context.session_state, "context_manager", None)
    return manager if manager is not None else _component(context, "context_manager")


def _sessions_file(context: CommandContext) -> Path:
    configured = context.components.get("sessions_file")
    if configured is None:
        configured = context.config.get("sessions_file")
    return Path(configured) if configured is not None else shared.SESSIONS_FILE


def _load_sessions(context: CommandContext) -> list[dict[str, Any]]:
    path = _sessions_file(context)
    if not path.is_file():
        return []
    sessions: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    sessions.append(item)
    except OSError:
        return []
    return sessions


def _score(value: Mapping[str, Any]) -> str:
    raw = value.get("objective_score")
    if raw is None:
        raw = value.get("score")
    if raw is None:
        return "?"
    try:
        return f"{float(raw):.1f}"
    except (TypeError, ValueError):
        return "?"


async def _memory_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/memory")
    if invalid is not None:
        return invalid
    memory = _component(context, "memory")
    read_index = getattr(memory, "read_index", None)
    if not callable(read_index):
        return _error("Memory is not available.")
    try:
        entries = sum(1 for line in str(read_index()).splitlines() if line.strip())
    except Exception:
        return _error("Memory is not available.")
    return CommandResult(
        response_text=(
            "## Memory Export\n\n"
            "- Projection: `memory/memory.jsonl`\n"
            f"- Entries: {entries}"
        )
    )


async def _context_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/context")
    if invalid is not None:
        return invalid
    manager = _context_manager(context)
    stats_method = getattr(manager, "stats", None)
    if not callable(stats_method):
        return _error("Context manager is not available.")
    try:
        stats = stats_method()
        categories = stats.get("category_names", ())
        category_names = (
            ", ".join(_markdown_inline(item) for item in categories) or "none"
        )
        needs_consolidation = "yes" if stats.get("needs_consolidation") else "no"
        text = (
            "## Context Manager (LTM)\n\n"
            f"- Dynamic Categories: {stats.get('dynamic_categories', 0)}/"
            f"{stats.get('max_categories', 0)}\n"
            f"- Total Categories: {stats.get('total_categories', 0)}\n"
            f"- Total Entries: {stats.get('total_entries', 0)}\n"
            f"- Category Names: {category_names}\n"
            f"- Staged Turns: {stats.get('staged_turns', 0)}\n"
            f"- Needs Consolidation: {needs_consolidation}\n"
            f"- Idle: {stats.get('idle_elapsed_s', 0)}s / "
            f"{stats.get('idle_threshold_s', 0)}s"
        )
    except Exception:
        return _error("Context manager is not available.")
    return CommandResult(response_text=text)


async def _sessions_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/sessions")
    if invalid is not None:
        return invalid
    sessions = _load_sessions(context)
    if not sessions:
        return _warning("No session history found.")
    lines = ["## Recent Sessions", "", "Session ID | Timestamp | Score | Summary"]
    lines.append("--- | --- | --- | ---")
    for session in reversed(sessions[-20:]):
        session_id = _markdown_inline(str(session.get("session_id", "?"))[:12])
        timestamp = _markdown_inline(str(session.get("timestamp", "?"))[:19])
        summary = _markdown_inline(str(session.get("task_summary", ""))[:60]) or "-"
        lines.append(f"{session_id} | {timestamp} | {_score(session)} | {summary}")
    return CommandResult(response_text="\n".join(lines))


def _session_turns(context: CommandContext, prefix: str) -> list[Any]:
    manager = _context_manager(context)
    store = getattr(manager, "store", None)
    get_turns = getattr(store, "get_turns_for_session", None)
    if not callable(get_turns):
        return []
    try:
        turns = get_turns(prefix)
    except Exception:
        return []
    return list(turns or ())


async def _session_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    prefix = request.args.strip()
    if not prefix:
        return _error("Usage: /session <session_id_prefix>")
    found = next(
        (
            session
            for session in _load_sessions(context)
            if str(session.get("session_id", "")).startswith(prefix)
        ),
        None,
    )
    if found is not None:
        tools = found.get("tools_used", ())
        if not isinstance(tools, (list, tuple)):
            tools = ()
        lines = [
            "## Session Details",
            "",
            f"- Session: {_markdown_inline(found.get('session_id', '?'))}",
            f"- Timestamp: {_markdown_inline(found.get('timestamp', '?'))}",
            f"- Score: {_score(found)}",
            f"- Summary: {_markdown_inline(found.get('task_summary', '?'))}",
            f"- Tools Used: {', '.join(_markdown_inline(tool) for tool in tools) or 'none'}",
            f"- Corrections: {_markdown_inline(found.get('correction_count', 0))}",
        ]
        return CommandResult(response_text="\n".join(lines))

    turns = _session_turns(context, prefix)
    if turns:
        lines = [f"## Session {_markdown_inline(prefix)} Turns", ""]
        for turn in turns[-10:]:
            role = _markdown_inline(_field(turn, "role", "?"))
            content = _markdown_inline(str(_field(turn, "content", ""))[:120])
            lines.append(f"- **{role}:** {content}")
        return CommandResult(response_text="\n".join(lines))
    return _error(f"Session not found: {_markdown_inline(prefix)}")


def _message_content_text(content: Any) -> str:
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    media_found = False
    for block in content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
        else:
            media_found = True
    if media_found:
        parts.append("[media content]")
    return "\n".join(parts) or "[media content]"


def _write_unique_export(output_dir: Path, stem: str, content: str) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    for collision_index in range(10_000):
        suffix = "" if collision_index == 0 else f"_{collision_index}"
        path = output_dir / f"{stem}{suffix}.md"
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path
    raise OSError("unable to reserve a unique export filename")


async def _export_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/export")
    if invalid is not None:
        return invalid
    session_context = getattr(context.session_state, "ctx", None)
    messages = getattr(session_context, "messages", None)
    if not messages:
        return _warning("No messages to export.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    lines = [f"# Session Export - {timestamp}", ""]
    try:
        for message in messages:
            role = _markdown_inline(str(_field(message, "role", "?")).upper())
            content = _message_content_text(_field(message, "content", ""))
            lines.extend((f"## {role}", "", content, ""))
        configured_dir = _component(context, "output_dir") or shared.DEFAULT_OUTPUT_DIR
        output_dir = Path(configured_dir).expanduser().resolve(strict=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = _write_unique_export(
            output_dir,
            f"session_{timestamp}",
            "\n".join(lines),
        )
    except Exception:
        return _error("Unable to export session.")
    return CommandResult(
        response_text=f"Exported {len(messages)} messages to {_markdown_inline(path)}",
        attachments=(path,),
    )


async def _tools_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/tools")
    if invalid is not None:
        return invalid
    list_tools = getattr(_component(context, "registry"), "list_tools", None)
    if not callable(list_tools):
        return _error("Tool registry is not available.")
    try:
        tools = [str(tool) for tool in list_tools()]
    except Exception:
        return _error("Tool registry is not available.")
    if not tools:
        return _warning("No tools found.")
    return CommandResult(
        response_text="## Available Tools\n\n"
        + "\n".join(f"- {_markdown_inline(tool)}" for tool in tools)
    )


async def _skills_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/skills")
    if invalid is not None:
        return invalid
    list_skills = getattr(_component(context, "skill_catalog"), "list_skills", None)
    if not callable(list_skills):
        return _error("Skill catalog is not available.")
    try:
        skills = list(list_skills())
    except Exception:
        return _error("Skill catalog is not available.")
    if not skills:
        return _warning("No skills found.")
    lines = ["## Available Skills", "", "ID | Source | Description", "--- | --- | ---"]
    for skill in skills:
        lines.append(
            f"{_markdown_inline(_field(skill, 'id', '?'))} | "
            f"{_markdown_inline(_field(skill, 'source', '-'))} | "
            f"{_markdown_inline(_field(skill, 'description', '') or '-')}"
        )
    return CommandResult(response_text="\n".join(lines))


async def _plugins_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, "/plugins")
    if invalid is not None:
        return invalid
    list_plugins = getattr(_component(context, "plugin_catalog"), "list_plugins", None)
    if not callable(list_plugins):
        return _error("Plugin catalog is not available.")
    try:
        plugins = list(list_plugins())
    except Exception:
        return _error("Plugin catalog is not available.")
    if not plugins:
        return _warning("No plugins loaded.")
    lines = [
        "## Loaded Plugins",
        "",
        "Name | Version | Source | Description",
        "--- | --- | --- | ---",
    ]
    for plugin in plugins:
        lines.append(
            f"{_markdown_inline(_field(plugin, 'name', '?'))} | "
            f"{_markdown_inline(_field(plugin, 'version', '') or '-')} | "
            f"{_markdown_inline(_field(plugin, 'source', '-'))} | "
            f"{_markdown_inline(_field(plugin, 'description', '') or '-')}"
        )
    return CommandResult(response_text="\n".join(lines))


def _configured_models(context: CommandContext) -> list[str]:
    providers = context.config.get("providers", {})
    if not isinstance(providers, Mapping):
        return []
    active_provider = context.config.get("active_provider", "")
    provider = providers.get(active_provider, {})
    if not isinstance(provider, Mapping):
        return []
    configured = provider.get("models", ())
    if isinstance(configured, str):
        configured = [configured]
    models = [str(model) for model in configured if str(model)]
    default = provider.get("default_model")
    if not models and default:
        models.append(str(default))
    return list(dict.fromkeys(models))


def _active_model(context: CommandContext, models: list[str]) -> str:
    override = getattr(context.session_state, "model_override", None)
    agent = _component(context, "agent")
    return str(
        override or getattr(agent, "model", None) or (models[0] if models else "")
    )


async def _model_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    models = _configured_models(context)
    active = _active_model(context, models)
    if not request.args:
        if not models:
            return _warning("No models configured for the active provider.")
        lines = ["## Models", ""]
        for model in models:
            lines.append(
                f"- {_markdown_inline(model)}{' (active)' if model == active else ''}"
            )
        return CommandResult(response_text="\n".join(lines))
    requested = request.args.strip()
    if requested not in models:
        available = ", ".join(models) or "none"
        return _error(
            f"Unknown model: {_markdown_inline(requested)}. "
            f"Available models: {_markdown_inline(available)}"
        )
    try:
        context.session_state.model_override = requested
    except Exception:
        return _error("Session model selection is not available.")
    return CommandResult(
        response_text=f"Switched to model: {_markdown_inline(requested)} (session only)"
    )


async def _quit_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    invalid = _require_no_args(request, f"/{request.name}")
    if invalid is not None:
        return invalid
    return CommandResult(action="exit_cli")


def _copy_file_descriptor(source: int, destination: int, max_bytes: int) -> int:
    copied = 0
    while True:
        remaining_limit = max_bytes + 1 - copied
        if remaining_limit <= 0:
            raise _SnapshotTooLarge
        chunk = os.read(source, min(1024 * 1024, remaining_limit))
        if not chunk:
            break
        copied += len(chunk)
        if copied > max_bytes:
            raise _SnapshotTooLarge
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(destination, remaining)
            if written <= 0:
                raise OSError("unable to write file snapshot")
            remaining = remaining[written:]
    return copied


def _relative_send_components(
    raw_path: str, output_root: Path
) -> tuple[str, ...]:
    expanded = os.path.expanduser(os.path.expandvars(raw_path))
    candidate = Path(expanded)
    if candidate.is_absolute():
        absolute_components = tuple(expanded.split(os.sep))
        root_components = tuple(os.fspath(output_root).split(os.sep))
        if absolute_components[: len(root_components)] != root_components:
            raise _UnsafeSendPath
        components = absolute_components[len(root_components) :]
    else:
        components = tuple(expanded.split(os.sep))
    if not components or any(part in ("", ".", "..") for part in components):
        raise _UnsafeSendPath
    return components


def _open_relative_source(
    output_descriptor: int, components: tuple[str, ...], common_flags: int
) -> int:
    parent_descriptor = os.dup(output_descriptor)
    try:
        for component in components[:-1]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | common_flags,
                dir_fd=parent_descriptor,
            )
            os.close(parent_descriptor)
            parent_descriptor = next_descriptor
        return os.open(
            components[-1],
            os.O_RDONLY | common_flags,
            dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)


def _source_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot_send_file(
    output_root: Path,
    components: tuple[str, ...],
    max_bytes: int,
) -> Path:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise NotImplementedError("secure file snapshots require O_NOFOLLOW")
    common_flags = nofollow | getattr(os, "O_CLOEXEC", 0)
    output_descriptor = os.open(
        output_root,
        os.O_RDONLY | directory | common_flags,
    )
    source_descriptor = -1
    temporary_descriptor = -1
    destination_descriptor = -1
    temporary_name: str | None = None
    destination_name: str | None = None
    try:
        if not stat.S_ISDIR(os.fstat(output_descriptor).st_mode):
            raise ValueError("output root is not a directory")
        source_descriptor = _open_relative_source(
            output_descriptor, components, common_flags
        )
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source is not a regular file")
        if before.st_size > max_bytes:
            raise _SnapshotTooLarge

        for _attempt in range(100):
            candidate = f".send-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=output_descriptor)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise OSError("unable to reserve a private attachment directory")
        temporary_descriptor = os.open(
            temporary_name,
            os.O_RDONLY | directory | common_flags,
            dir_fd=output_descriptor,
        )
        os.fchmod(temporary_descriptor, 0o700)
        destination_name = components[-1]
        destination_descriptor = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=temporary_descriptor,
        )
        os.fchmod(destination_descriptor, 0o600)
        copied = _copy_file_descriptor(
            source_descriptor, destination_descriptor, max_bytes
        )
        after = os.fstat(source_descriptor)
        if copied != before.st_size or _source_identity(after) != _source_identity(
            before
        ):
            raise _SnapshotChanged
        os.fsync(destination_descriptor)
        os.close(destination_descriptor)
        destination_descriptor = -1
        return output_root / temporary_name / destination_name
    except Exception:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
            destination_descriptor = -1
        if temporary_descriptor >= 0 and destination_name is not None:
            try:
                os.unlink(destination_name, dir_fd=temporary_descriptor)
            except FileNotFoundError:
                pass
        if temporary_name is not None:
            try:
                os.rmdir(temporary_name, dir_fd=output_descriptor)
            except OSError:
                pass
        raise
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(output_descriptor)


def _send_snapshot_limit(context: CommandContext) -> int:
    configured = context.config.get(
        "send_max_snapshot_bytes", _DEFAULT_MAX_SEND_SNAPSHOT_BYTES
    )
    if (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or configured <= 0
    ):
        return _DEFAULT_MAX_SEND_SNAPSHOT_BYTES
    return configured


def _delete_temporary_snapshot(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


async def _send_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    raw_path = request.args.strip()
    if not raw_path:
        return _error("Usage: /send <path>")
    output_dir = _component(context, "output_dir")
    if output_dir is None:
        return _error("Output directory is not available.")
    try:
        output_root = Path(output_dir).expanduser().resolve(strict=False)
        components = _relative_send_components(raw_path, output_root)
    except (OSError, RuntimeError, ValueError):
        return _error("Invalid file path.")
    except _UnsafeSendPath:
        return _error("File is outside the output directory.")
    source_display = output_root.joinpath(*components)
    max_bytes = _send_snapshot_limit(context)
    snapshot_task = asyncio.create_task(
        asyncio.to_thread(
            _snapshot_send_file,
            output_root,
            components,
            max_bytes,
        )
    )
    try:
        snapshot = await asyncio.shield(snapshot_task)
    except asyncio.CancelledError:

        def cleanup_completed_snapshot(task: asyncio.Task[Path]) -> None:
            if task.cancelled():
                return
            try:
                completed_snapshot = task.result()
            except Exception:
                return
            _delete_temporary_snapshot(completed_snapshot)

        snapshot_task.add_done_callback(cleanup_completed_snapshot)
        raise
    except FileNotFoundError:
        return _error(f"File not found: {_markdown_inline(raw_path)}")
    except NotImplementedError:
        return _error("Secure file sending is not supported on this platform.")
    except _SnapshotTooLarge:
        return _error(f"File exceeds send snapshot limit ({max_bytes} bytes).")
    except _SnapshotChanged:
        return _error("File changed while preparing attachment.")
    except ValueError:
        return _error(f"File is not a regular file: {_markdown_inline(raw_path)}")
    except OSError:
        return _error(f"Unable to send file: {_markdown_inline(raw_path)}")
    return CommandResult(
        response_text=f"Sending file: {_markdown_inline(source_display)}",
        attachments=(snapshot,),
        temporary_attachments=(snapshot,),
    )


def _resolve_user_path(raw: str) -> Path | None:
    """Resolve a user-supplied path for /open or /reveal."""
    value = raw.strip().strip("'\"")
    if not value or value.startswith("-"):
        return None
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return None


async def _launch_path(path: Path, *, reveal: bool) -> CommandResult:
    """Open a local path, or reveal it in the system file manager."""
    if sys.platform == "darwin":
        args = ["open", "-R", str(path)] if reveal else ["open", str(path)]
        verb = "在访达中显示" if reveal else "打开"
    elif sys.platform.startswith("win"):
        args = (
            ["explorer", "/select,", str(path)]
            if reveal
            else ["cmd", "/c", "start", "", str(path)]
        )
        verb = "在资源管理器中显示" if reveal else "打开"
    else:
        args = ["xdg-open", str(path.parent if reveal else path)]
        verb = "在文件管理器中显示" if reveal else "打开"
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return_code = await process.wait()
    except (FileNotFoundError, OSError):
        return _error(f"无法{verb}：缺少系统打开命令。")
    if return_code != 0:
        return _error(f"无法{verb}：{_markdown_inline(str(path))}")
    return CommandResult(response_text=f"已{verb}：{_markdown_inline(str(path))}")


async def _open_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    target = _resolve_user_path(request.args)
    if target is None:
        return _error("用法：/open <文件或目录路径>")
    if not target.exists():
        return _error(f"文件或目录不存在：{_markdown_inline(str(target))}")
    return await _launch_path(target, reveal=False)


async def _reveal_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    target = _resolve_user_path(request.args)
    if target is None:
        return _error("用法：/reveal <文件或目录路径>")
    if not target.exists():
        return _error(f"文件或目录不存在：{_markdown_inline(str(target))}")
    return await _launch_path(target, reveal=True)


async def _coordinator_owned_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    return _error(
        f"Command /{request.name} must be handled by the command coordinator."
    )


async def _confirm_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    parts = request.args.split()
    if len(parts) != 1:
        return _error("Usage: /confirm <token>")

    from agent.security.shell import ShellAuthorizationScope, shell_command_confirm

    scope = ShellAuthorizationScope(
        context.session_id,
        context.channel_name,
        str(context.metadata.get("user_id") or ""),
    )
    if not shell_command_confirm(parts[0], scope=scope):
        return _error("Confirmation token is invalid, expired, or belongs to another user.")
    return CommandResult(
        response_text="Confirmation accepted. Retry the requested operation."
    )


def _shell_allowed_commands_from_config() -> list[str]:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    value = cfg.get("shell_allowed_commands") or []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _save_shell_allowed_commands(commands: list[str]) -> None:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    cfg["shell_allowed_commands"] = commands
    agent_module.save_config(cfg)


def _apply_shell_allowed_commands(
    context: CommandContext, commands: list[str]
) -> None:
    registry = context.components.get("registry")
    if registry is not None:
        try:
            registry.set_context("shell_allowed_commands", list(commands))
        except Exception:
            pass


async def _allow_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    command = request.args.strip()
    if not command:
        return _error("Usage: /allow <command 或命令名>")
    commands = _shell_allowed_commands_from_config()
    if command not in commands:
        commands.append(command)
    _save_shell_allowed_commands(commands)
    _apply_shell_allowed_commands(context, commands)
    return CommandResult(
        response_text=(
            f"已加入放行列表：{_markdown_inline(command)}"
            "（持久生效；完整命令按原文匹配，命令名匹配所有调用）"
        )
    )


async def _deny_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    command = request.args.strip()
    if not command:
        return _error("Usage: /deny <command 或命令名>")
    commands = _shell_allowed_commands_from_config()
    remaining = [item for item in commands if item != command]
    if len(remaining) == len(commands):
        return _warning(f"放行列表中不存在：{_markdown_inline(command)}")
    _save_shell_allowed_commands(remaining)
    _apply_shell_allowed_commands(context, remaining)
    return CommandResult(
        response_text=f"已从放行列表移除：{_markdown_inline(command)}"
    )


async def _auto_approve_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    from agent.security.shell import (
        ShellAuthorizationScope,
        shell_session_auto_approve_disable,
        shell_session_auto_approve_enable,
        shell_session_permission_clear,
        shell_session_permission_get,
    )

    registry = context.components.get("registry")
    scope = ShellAuthorizationScope(
        context.session_id,
        context.channel_name,
        str(context.metadata.get("user_id") or ""),
    )
    arg = request.args.strip().casefold()
    if arg in ("on", "1", "yes", "true", "开启"):
        _save_config_shell_permission_level("medium")
        shell_session_permission_clear(scope)
        if registry is not None:
            try:
                registry.set_context("shell_permission_level", "medium")
            except Exception:
                pass
        return CommandResult(
            response_text=(
                "已开启高危自动放行并持久化为默认（重启后保留，"
                "子代理继承）：高风险命令直接放行"
                "（操作符/管道模式仍需确认）。"
            )
        )
    if arg in ("off", "0", "no", "false", "关闭"):
        _save_config_shell_permission_level("ask")
        shell_session_permission_clear(scope)
        if registry is not None:
            try:
                registry.set_context("shell_permission_level", "ask")
            except Exception:
                pass
        return CommandResult(
            response_text=(
                "已关闭自动放行并持久化为默认（重启后保留）："
                "高风险命令需要确认。"
            )
        )
    if arg.startswith("session "):
        session_arg = arg[len("session ") :].strip()
        if session_arg in ("on", "1", "yes", "true", "开启"):
            shell_session_auto_approve_enable(scope)
            return CommandResult(
                response_text=(
                    "已开启会话自动放行（仅本会话生效，重启恢复默认）："
                    "高风险命令直接放行（操作符/管道模式仍需确认）。"
                )
            )
        if session_arg in ("off", "0", "no", "false", "关闭"):
            shell_session_auto_approve_disable(scope)
            return CommandResult(
                response_text=(
                    "已关闭会话自动放行（仅本会话生效）："
                    "高风险命令需要确认。"
                )
            )
        return _error("Usage: /auto-approve session on|off")
    if arg in ("status", "?", ""):
        config_level = _config_shell_permission_level()
        session_level = shell_session_permission_get(scope)
        effective_level = session_level or config_level
        state = "开启" if effective_level in ("medium", "high", "full") else "关闭"
        lines = [f"自动放行（medium 及以上）：{state}"]
        lines.append(f"配置默认：`{config_level}`")
        if session_level:
            lines.append(f"会话覆盖：`{session_level}`")
        lines.append(
            "用 `/auto-approve on|off` 持久化默认；"
            "`/auto-approve session on|off` 仅本会话。"
        )
        return CommandResult(response_text="\n".join(lines))
    return _error("Usage: /auto-approve on|off|session on|off|status")


def _permission_level_label(level: str) -> str:
    labels = {
        "ask": "低/中风险自动执行；高风险（破坏性命令/操作符）需确认",
        "medium": "高危命令与破坏性选项直接放行；操作符/管道模式仍需确认",
        "high": "全部自动放行（含操作符）；仅保留配置黑名单与结构拦截",
        "full": "同 high：全部自动放行（仅保留配置黑名单与结构拦截）",
    }
    return labels.get(level, level)


def _config_shell_permission_level() -> str:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions") or {}
    return str(permissions.get("shell_level", "ask") or "ask")


def _config_shell_sandbox_mode() -> str:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions") or {}
    return str(permissions.get("shell_sandbox", "read_all") or "read_all")


def _save_config_shell_permission_level(level: str) -> None:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        cfg["permissions"] = permissions
    permissions["shell_level"] = level
    agent_module.save_config(cfg)


def _save_config_shell_sandbox_mode(mode: str) -> None:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        cfg["permissions"] = permissions
    permissions["shell_sandbox"] = mode
    agent_module.save_config(cfg)


def _clear_config_shell_permission_level() -> None:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions")
    if not isinstance(permissions, dict) or "shell_level" not in permissions:
        return
    del permissions["shell_level"]
    if not permissions:
        cfg.pop("permissions", None)
    agent_module.save_config(cfg)


def _clear_config_shell_sandbox_mode() -> None:
    import agent as agent_module

    cfg, _first_run = agent_module.load_config()
    permissions = cfg.get("permissions")
    if not isinstance(permissions, dict) or "shell_sandbox" not in permissions:
        return
    del permissions["shell_sandbox"]
    if not permissions:
        cfg.pop("permissions", None)
    agent_module.save_config(cfg)


def _sandbox_mode_label(mode: str) -> str:
    labels = {
        "restricted": "仅系统目录 + workspace/output 可读，写限 output",
        "read_all": "全盘可读，写仍限 output/workspace 授权范围",
        "none": "无沙箱（危险：整机包括 GPU 均可访问）",
    }
    return labels.get(mode, mode)


async def _permissions_handler(
    request: CommandRequest, context: CommandContext
) -> CommandResult:
    from agent.security.shell import (
        PERMISSION_LEVELS,
        ShellAuthorizationScope,
        shell_effective_permission_level,
        shell_session_permission_clear,
        shell_session_permission_get,
        shell_session_permission_set,
        shell_session_sandbox_clear,
        shell_session_sandbox_get,
        shell_session_sandbox_set,
    )
    from agent.security.filesystem_sandbox import SANDBOX_MODES

    registry = context.components.get("registry")

    scope = ShellAuthorizationScope(
        context.session_id,
        context.channel_name,
        str(context.metadata.get("user_id") or ""),
    )
    config_level = _config_shell_permission_level()
    config_sandbox = _config_shell_sandbox_mode()
    session_level = shell_session_permission_get(scope)
    effective_level = session_level or config_level
    session_sandbox = shell_session_sandbox_get(scope)
    effective_sandbox = session_sandbox or config_sandbox
    args = request.args.strip()
    if not args:
        lines = ["## Shell 权限等级", ""]
        for level in PERMISSION_LEVELS:
            marker = "●" if level == effective_level else "○"
            lines.append(f"- {marker} `{level}`：{_permission_level_label(level)}")
        lines.append("")
        lines.append("## Shell 沙箱模式")
        for mode in SANDBOX_MODES:
            marker = "●" if mode == effective_sandbox else "○"
            lines.append(f"- {marker} `{mode}`：{_sandbox_mode_label(mode)}")
        if session_level:
            lines.append("")
            lines.append(
                f"会话覆盖：等级 `{session_level}`"
                f"（配置默认 `{config_level}`）"
            )
        if session_sandbox:
            lines.append("")
            lines.append(
                f"会话覆盖：沙箱 `{session_sandbox}`"
                f"（配置默认：`{config_sandbox}`）"
            )
        lines.append("")
        lines.append(
            f"当前生效：`{effective_level}` / `{effective_sandbox}`。"
            "用 `/permissions <level>`、`/permissions sandbox <mode>` "
            "持久化默认并立即生效；"
            "`/permissions session <level>`、`/permissions sandbox session <mode>` "
            "仅本会话；`/permissions reset` 恢复内置默认。"
        )
        return CommandResult(response_text="\n".join(lines))

    parts = args.split(maxsplit=1)
    first = parts[0].casefold()
    rest = parts[1].strip() if len(parts) == 2 else ""

    if first == "session":
        if not rest:
            return _error(
                "Usage: /permissions session <level> | sandbox <mode>"
            )
        session_parts = rest.split(maxsplit=1)
        session_first = session_parts[0].casefold()
        session_rest = (
            session_parts[1].strip() if len(session_parts) == 2 else ""
        )
        if session_first == "sandbox":
            mode = session_rest.casefold()
            if not mode:
                return _error(
                    "Usage: /permissions session sandbox "
                    f"{'|'.join(SANDBOX_MODES)}"
                )
            if mode not in SANDBOX_MODES:
                return _error(
                    "Usage: /permissions session sandbox "
                    f"{'|'.join(SANDBOX_MODES)}"
                )
            if mode == "none" and shell_effective_permission_level(
                scope, config_level
            ) != "full":
                return _error(
                    "沙箱模式 `none` 需要权限等级 `full`；请先设置 "
                    "/permissions full（或 /permissions session full）。"
                )
            shell_session_sandbox_set(scope, mode)
            return CommandResult(
                response_text=(
                    f"会话沙箱模式已设为 `{mode}`（仅本会话生效，"
                    f"{_sandbox_mode_label(mode)}）。"
                )
            )
        if session_first in PERMISSION_LEVELS:
            shell_session_permission_set(scope, session_first)
            return CommandResult(
                response_text=(
                    f"会话权限等级已设为 `{session_first}`（仅本会话生效，"
                    f"{_permission_level_label(session_first)}）。"
                )
            )
        return _error(
            "Usage: /permissions session <level> | sandbox <mode>"
        )

    if first == "sandbox":
        if not rest:
            return CommandResult(
                response_text=(
                    f"当前沙箱模式：`{effective_sandbox}`"
                    f"（配置默认 `{config_sandbox}`"
                    + (f"，会话覆盖 `{session_sandbox}`" if session_sandbox else "")
                    + "）。"
                )
            )
        sandbox_parts = rest.split(maxsplit=1)
        sandbox_first = sandbox_parts[0].casefold()
        sandbox_rest = (
            sandbox_parts[1].strip() if len(sandbox_parts) == 2 else ""
        )
        if sandbox_first == "session":
            mode = sandbox_rest.casefold()
            if not mode:
                return _error(
                    "Usage: /permissions sandbox session "
                    f"{'|'.join(SANDBOX_MODES)}"
                )
            if mode not in SANDBOX_MODES:
                return _error(
                    "Usage: /permissions sandbox session "
                    f"{'|'.join(SANDBOX_MODES)}"
                )
            if mode == "none" and shell_effective_permission_level(
                scope, config_level
            ) != "full":
                return _error(
                    "沙箱模式 `none` 需要权限等级 `full`；请先设置 "
                    "/permissions full（或 /permissions session full）。"
                )
            shell_session_sandbox_set(scope, mode)
            return CommandResult(
                response_text=(
                    f"会话沙箱模式已设为 `{mode}`（仅本会话生效，"
                    f"{_sandbox_mode_label(mode)}）。"
                )
            )
        mode = rest.strip().casefold()
        if not mode:
            return CommandResult(
                response_text=(
                    f"当前沙箱模式：`{effective_sandbox}`"
                    f"（配置默认 `{config_sandbox}`"
                    + (f"，会话覆盖 `{session_sandbox}`" if session_sandbox else "")
                    + "）。"
                )
            )
        if mode not in SANDBOX_MODES:
            return _error(
                f"Usage: /permissions sandbox {'|'.join(SANDBOX_MODES)}"
            )
        if mode == "none" and config_level != "full":
            return _error(
                "沙箱模式 `none` 需要权限等级 `full`；请先设置 "
                "/permissions full。"
            )
        _save_config_shell_sandbox_mode(mode)
        shell_session_sandbox_clear(scope)
        if registry is not None:
            try:
                registry.set_context("shell_sandbox_mode", mode)
            except Exception:
                pass
        return CommandResult(
            response_text=(
                f"已把默认沙箱模式持久化为 `{mode}`（立即生效，"
                "重启后保留，子代理继承该默认值）。"
            )
        )

    if first == "default":
        if not rest:
            return _error(
                "Usage: /permissions default "
                f"{'|'.join(PERMISSION_LEVELS)} | sandbox "
                f"{'|'.join(SANDBOX_MODES)}"
            )
        default_args = rest.split(maxsplit=1)
        if default_args[0].casefold() == "sandbox":
            if len(default_args) != 2 or default_args[1].strip().casefold() not in (
                SANDBOX_MODES
            ):
                return _error(
                    "Usage: /permissions default sandbox "
                    f"{'|'.join(SANDBOX_MODES)}"
                )
            mode = default_args[1].strip().casefold()
            if mode == "none" and config_level != "full":
                return _error(
                    "沙箱模式 `none` 需要权限等级 `full`；请先设置 "
                    "/permissions default full。"
                )
            _save_config_shell_sandbox_mode(mode)
            shell_session_sandbox_clear(scope)
            if registry is not None:
                try:
                    registry.set_context("shell_sandbox_mode", mode)
                except Exception:
                    pass
            return CommandResult(
                response_text=(
                    f"已把默认沙箱模式持久化为 `{mode}`（重启后保留，"
                    "子代理继承该默认值）。"
                )
            )
        level = default_args[0].strip().casefold()
        if level not in PERMISSION_LEVELS:
            return _error(
                "Usage: /permissions default "
                f"{'|'.join(PERMISSION_LEVELS)} | sandbox "
                f"{'|'.join(SANDBOX_MODES)}"
            )
        shell_session_permission_clear(scope)
        _save_config_shell_permission_level(level)
        if registry is not None:
            try:
                registry.set_context("shell_permission_level", level)
            except Exception:
                pass
        return CommandResult(
            response_text=(
                f"已把默认权限等级持久化为 `{level}`（重启后保留，"
                "子代理继承该默认值）。"
            )
        )

    if first == "reset":
        target = rest.casefold()
        if target not in ("", "all", "level", "sandbox"):
            return _error("Usage: /permissions reset [level|sandbox]")
        restored = []
        if target in ("", "all", "level"):
            _clear_config_shell_permission_level()
            shell_session_permission_clear(scope)
            if registry is not None:
                try:
                    registry.set_context("shell_permission_level", "ask")
                except Exception:
                    pass
            restored.append("权限等级 `ask`")
        if target in ("", "all", "sandbox"):
            _clear_config_shell_sandbox_mode()
            shell_session_sandbox_clear(scope)
            if registry is not None:
                try:
                    registry.set_context("shell_sandbox_mode", "read_all")
                except Exception:
                    pass
            restored.append("沙箱模式 `read_all`")
        return CommandResult(
            response_text=(
                "已恢复内置默认："
                + "、".join(restored)
                + "（配置已清除，会话覆盖已移除）。"
            )
        )

    level = first
    if level not in PERMISSION_LEVELS:
        return _error(
            f"Usage: /permissions {'|'.join(PERMISSION_LEVELS)} | session "
            f"{'|'.join(PERMISSION_LEVELS)} | sandbox <mode> | default ... | reset"
        )
    _save_config_shell_permission_level(level)
    shell_session_permission_clear(scope)
    if registry is not None:
        try:
            registry.set_context("shell_permission_level", level)
        except Exception:
            pass
    return CommandResult(
        response_text=(
            f"已把权限等级持久化为 `{level}`（立即生效，重启后保留，"
            f"子代理继承该默认值；{_permission_level_label(level)}）。"
        )
    )


def _builtin_descriptors(router: CommandRouter) -> tuple[CommandDescriptor, ...]:
    async def help_handler(
        request: CommandRequest, context: CommandContext
    ) -> CommandResult:
        invalid = _require_no_args(request, "/help")
        if invalid is not None:
            return invalid
        return CommandResult(response_text=router.help_text(request.channel_name))

    return (
        CommandDescriptor(
            "help",
            help_handler,
            usage="/help",
            description="Show commands available in this channel",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "memory",
            _memory_handler,
            usage="/memory",
            description="Show memory export summary",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "context",
            _context_handler,
            usage="/context",
            description="Show long-term context statistics",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "sessions",
            _sessions_handler,
            aliases=("history",),
            usage="/sessions",
            description="List recent session history",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "session",
            _session_handler,
            usage="/session <id>",
            description="View session details by ID prefix",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "export",
            _export_handler,
            usage="/export",
            description="Export the current session to Markdown",
            concurrency="idle_only",
        ),
        CommandDescriptor(
            "tools",
            _tools_handler,
            usage="/tools",
            description="List available tools",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "skills",
            _skills_handler,
            usage="/skills",
            description="List available skills",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "plugins",
            _plugins_handler,
            usage="/plugins",
            description="List loaded plugins",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "model",
            _model_handler,
            usage="/model [name]",
            description="Show or switch the session model",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "confirm",
            _confirm_handler,
            usage="/confirm <token>",
            description="Approve one pending restricted shell command",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "allow",
            _allow_handler,
            usage="/allow <command>",
            description="Add a command to the persistent shell allowlist (skip confirmation)",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "deny",
            _deny_handler,
            usage="/deny <command>",
            description="Remove a command from the persistent shell allowlist",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "auto-approve",
            _auto_approve_handler,
            usage="/auto-approve on|off|session on|off|status",
            description="Toggle automatic approval for high-risk shell commands (persistent by default)",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "permissions",
            _permissions_handler,
            usage=(
                "/permissions [<level>|sandbox <mode>|session <level>|"
                "sandbox session <mode>|default <level|sandbox <mode>>|"
                "reset [level|sandbox]]"
            ),
            description="Show or set shell permission level and sandbox mode (persistent by default)",
            concurrency="anytime",
        ),
        CommandDescriptor(
            "quit",
            _quit_handler,
            aliases=("exit", "q"),
            usage="/quit",
            description="Exit the CLI",
            scopes=frozenset({"cli"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "send",
            _send_handler,
            usage="/send <path>",
            description="Send a file from the output directory",
            scopes=frozenset({"feishu"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "open",
            _open_handler,
            usage="/open <文件或目录路径>",
            description="Open a file or directory with the default app",
            scopes=frozenset({"cli"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "reveal",
            _reveal_handler,
            aliases=("finder",),
            usage="/reveal <文件或目录路径>",
            description="Reveal a file in the system file manager (Finder on macOS)",
            scopes=frozenset({"cli"}),
            concurrency="anytime",
        ),
        CommandDescriptor(
            "cancel",
            _coordinator_owned_handler,
            usage="/cancel [graceful|new task]",
            description="Cancel the current task (Ctrl+C interrupts a blocking CLI operation)",
            concurrency="interrupt",
        ),
        CommandDescriptor(
            "now",
            _coordinator_owned_handler,
            usage="/now <message>",
            description="Apply an urgent interjection or start it next",
            concurrency="interrupt",
        ),
        CommandDescriptor(
            "ralph",
            _ralph_handler,
            usage='/ralph <goal> [--max N] [--verify "command"]',
            description="Run or resume an autonomous Ralph task",
            concurrency="idle_only",
            accepts_interjections=True,
        ),
    )


def register_builtin_commands(router: CommandRouter) -> CommandRouter:
    """Register transport-neutral built-ins on one router instance."""

    router.register_core_batch(_builtin_descriptors(router))
    return router


__all__ = ["register_builtin_commands"]
