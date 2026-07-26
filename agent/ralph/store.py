from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from agent.pathing import path_contains

from .models import RalphTask, RalphValidationError, validate_task_id


RALPH_MAX_TASK_FILE_BYTES = 2 * 1024 * 1024


class RalphStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RalphTaskNotFoundError(RalphStoreError):
    def __init__(self, task_ref: str) -> None:
        super().__init__("task_not_found", f"Ralph task not found: {task_ref}")
        self.task_ref = task_ref


class RalphTaskAmbiguousError(RalphStoreError):
    def __init__(self, task_ref: str, matches: tuple[str, ...]) -> None:
        super().__init__(
            "ambiguous_task_id",
            f"Ralph task prefix is ambiguous: {task_ref} ({', '.join(matches)})",
        )
        self.task_ref = task_ref
        self.matches = matches


class RalphTaskCorruptError(RalphStoreError):
    def __init__(self, task_id: str, detail: str) -> None:
        super().__init__("corrupt_task", f"Ralph task '{task_id}' is corrupt: {detail}")
        self.task_id = task_id
        self.detail = detail


class RalphTaskStoreIOError(RalphStoreError):
    def __init__(self, operation: str, detail: str) -> None:
        super().__init__("store_io_error", f"Unable to {operation} Ralph task store: {detail}")


# Short aliases are part of the stable domain surface for consumers that do not
# want the Ralph prefix repeated at every exception site.
TaskNotFoundError = RalphTaskNotFoundError
AmbiguousTaskIdError = RalphTaskAmbiguousError
CorruptTaskError = RalphTaskCorruptError


class RalphTaskStore:
    def __init__(self, tasks_dir: str | Path) -> None:
        self.tasks_dir = Path(tasks_dir).expanduser().resolve(strict=False)

    def save(self, task: RalphTask) -> None:
        if not isinstance(task, RalphTask):
            raise TypeError("task must be a RalphTask")
        validate_task_id(task.id)
        payload = json.dumps(
            task.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        encoded = payload.encode("utf-8")
        if len(encoded) > RALPH_MAX_TASK_FILE_BYTES:
            raise RalphTaskCorruptError(task.id, "serialized task exceeds the size limit")

        try:
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            target = self._path_for_id(task.id)
            if target.is_symlink():
                raise RalphTaskCorruptError(task.id, "task path is a symbolic link")
            temp = self.tasks_dir / f".{task.id}.{uuid.uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    fd = -1
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
                self._fsync_directory()
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
        except RalphStoreError:
            raise
        except OSError as exc:
            raise RalphTaskStoreIOError("save", str(exc)) from exc

    def load(self, task_ref: str) -> RalphTask:
        validate_task_id(task_ref, label="task ID or prefix")
        task_id = self.resolve_id(task_ref)
        return self._read_task(task_id)

    def resolve_id(self, task_ref: str) -> str:
        validate_task_id(task_ref, label="task ID or prefix")
        exact = self._path_for_id(task_ref)
        if exact.exists() or exact.is_symlink():
            return task_ref
        matches = tuple(
            task_id for task_id in self._task_ids() if task_id.startswith(task_ref)
        )
        if not matches:
            raise RalphTaskNotFoundError(task_ref)
        if len(matches) > 1:
            raise RalphTaskAmbiguousError(task_ref, matches)
        return matches[0]

    def list_tasks(self) -> list[RalphTask]:
        return [self._read_task(task_id) for task_id in self._task_ids()]

    def _task_ids(self) -> list[str]:
        if not self.tasks_dir.exists():
            return []
        try:
            if not self.tasks_dir.is_dir():
                raise RalphTaskStoreIOError("read", "tasks path is not a directory")
            task_ids: list[str] = []
            for path in self.tasks_dir.iterdir():
                if path.name.startswith(".") or path.suffix != ".json":
                    continue
                task_id = path.stem
                try:
                    validate_task_id(task_id)
                except RalphValidationError as exc:
                    raise RalphTaskCorruptError(task_id, exc.message) from exc
                task_ids.append(task_id)
            return sorted(task_ids)
        except RalphStoreError:
            raise
        except OSError as exc:
            raise RalphTaskStoreIOError("list", str(exc)) from exc

    def _path_for_id(self, task_id: str) -> Path:
        validate_task_id(task_id)
        path = self.tasks_dir / f"{task_id}.json"
        if not path_contains(self.tasks_dir, path):
            raise RalphValidationError("invalid_task_id", "task path escapes the task store")
        return path

    def _read_task(self, task_id: str) -> RalphTask:
        path = self._path_for_id(task_id)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RalphTaskCorruptError(task_id, "task path is not a regular file")
                if info.st_size > RALPH_MAX_TASK_FILE_BYTES:
                    raise RalphTaskCorruptError(task_id, "task file exceeds the size limit")
                with os.fdopen(fd, "rb", closefd=True) as handle:
                    fd = -1
                    raw = handle.read(RALPH_MAX_TASK_FILE_BYTES + 1)
            finally:
                if fd >= 0:
                    os.close(fd)
        except FileNotFoundError as exc:
            raise RalphTaskNotFoundError(task_id) from exc
        except RalphStoreError:
            raise
        except OSError as exc:
            # ELOOP from O_NOFOLLOW is a corrupt/suspicious task entry.
            if path.is_symlink():
                raise RalphTaskCorruptError(task_id, "task path is a symbolic link") from exc
            raise RalphTaskStoreIOError("load", str(exc)) from exc

        if len(raw) > RALPH_MAX_TASK_FILE_BYTES:
            raise RalphTaskCorruptError(task_id, "task file exceeds the size limit")

        try:
            data = json.loads(raw.decode("utf-8"))
            task = RalphTask.from_dict(data)
        except (UnicodeDecodeError, json.JSONDecodeError, RalphValidationError) as exc:
            raise RalphTaskCorruptError(task_id, str(exc)) from exc
        if task.id != task_id:
            raise RalphTaskCorruptError(task_id, "payload ID does not match filename")
        return task

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(self.tasks_dir, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


__all__ = [
    "AmbiguousTaskIdError",
    "CorruptTaskError",
    "RALPH_MAX_TASK_FILE_BYTES",
    "RalphStoreError",
    "RalphTaskAmbiguousError",
    "RalphTaskCorruptError",
    "RalphTaskNotFoundError",
    "RalphTaskStore",
    "RalphTaskStoreIOError",
    "TaskNotFoundError",
]
