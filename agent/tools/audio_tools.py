"""The audio transcription tool.

Why this module exists
----------------------
One tool (``transcribe_audio``) and the two helpers only it uses: resolving the
configured command, and turning that command into an argument vector.  They were
methods on ``BuiltinTools``, a 3181-line class.

The argv builder is the part that earns the file, and it is a security
boundary rather than a convenience.  The command template comes from
configuration, and a template containing shell operators would be a way to run
arbitrary shell from a *configuration value* -- so ``shell_command_uses_shell_features``
refuses it outright instead of letting a shell parse it.  What is left is split
with ``shlex`` and substituted literally (``{path}``, ``{language}``); the file
path is appended when the template did not place it.  A template that cannot be
split or that splits to nothing is refused with the reason, not silently
skipped.

The subprocess itself runs in the agent's output directory with
``AGENT_OUTPUT_DIR`` and ``AGENT_WORKSPACE_ROOT`` in its environment, so a
transcription script can write where the agent expects to find files.  Both
values are resolved *at call time* through the injected callables, not captured
at construction: a session's own output dir has to win over the composition's.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Callable

from agent.security.shell import shell_command_uses_shell_features


class AudioTools:
    """The audio transcription tool, with the callables it needs."""

    def __init__(
        self,
        registry: Any,
        active_workspace_root: Callable[[], Path],
        process_output_dir: Callable[[], Path],
        resolve_tool_path: Callable[[str], tuple[Path, str]],
        terminate_process: Callable[[Any], Any],
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own audio command configuration.
        self.registry = registry
        self._active_workspace_root = active_workspace_root
        self._process_output_dir = process_output_dir
        self._resolve_tool_path = resolve_tool_path
        self._terminate_process = terminate_process
        self._ok = ok
        self._error = error

    def transcription_command(self) -> str:
        raw = self.registry.get_context("audio_transcription_command", "")
        if isinstance(raw, str) and raw.startswith("$"):
            return os.environ.get(raw[1:], "")
        if raw:
            return str(raw)
        return os.environ.get("SIMPLE_AUDIO_TRANSCRIBE_COMMAND", "")

    def build_transcription_argv(
        self, command_template: str, audio_path: Path, language: str
    ) -> list[str]:
        if shell_command_uses_shell_features(command_template):
            raise ValueError(
                "unsafe audio transcription command: shell operators are not allowed"
            )
        try:
            template_parts = shlex.split(command_template)
        except ValueError as e:
            raise ValueError(
                f"unsafe audio transcription command: invalid quoting ({e})"
            ) from e
        if not template_parts:
            raise ValueError("unsafe audio transcription command: empty command")

        path_was_used = False
        argv: list[str] = []
        for part in template_parts:
            if "{path}" in part:
                path_was_used = True
                part = part.replace("{path}", str(audio_path))
            if "{language}" in part:
                part = part.replace("{language}", language)
            if part:
                argv.append(part)
        if not path_was_used:
            argv.append(str(audio_path))
        return argv

    async def transcribe(
        self,
        path: str,
        language: str = "",
        timeout: int = 300,
    ) -> dict[str, Any]:
        try:
            resolved, _root_kind = self._resolve_tool_path(path)
            if not resolved.exists():
                return self._error(f"'{path}' does not exist", path=str(resolved))
            if not resolved.is_file():
                return self._error(f"'{path}' is not a regular file", path=str(resolved))
            command_template = self.transcription_command().strip()
            if not command_template:
                return self._error(
                    "Audio transcription is not configured. Set audio.transcription_command "
                    "in config.json or SIMPLE_AUDIO_TRANSCRIBE_COMMAND. Use {path} as the "
                    "audio-file placeholder.",
                    path=str(resolved),
                )
            language = str(language or "").strip()
            argv = self.build_transcription_argv(
                command_template, resolved, language
            )
            timeout = max(1, min(int(timeout), 900))
            proc = None
            output_dir = self._process_output_dir()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    **os.environ.copy(),
                    "AGENT_OUTPUT_DIR": str(output_dir),
                    "AGENT_WORKSPACE_ROOT": str(self._active_workspace_root()),
                },
                cwd=str(output_dir),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            transcript = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()
            if proc.returncode != 0:
                return self._error(
                    f"Audio transcription failed with exit code {proc.returncode}",
                    path=str(resolved),
                    stderr=err[-4000:],
                    exit_code=proc.returncode,
                )
            return self._ok(
                path=str(resolved),
                transcript=transcript,
                stderr=err[-4000:] if err else "",
                exit_code=proc.returncode,
            )
        except asyncio.TimeoutError:
            await self._terminate_process(proc)
            return self._error(
                f"Audio transcription timed out after {timeout}s",
                path=path,
                timed_out=True,
            )
        except asyncio.CancelledError:
            await self._terminate_process(proc)
            raise
        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Error transcribing audio: {e}")
