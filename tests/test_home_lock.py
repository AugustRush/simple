"""Tests for single-owner enforcement over one agent home."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.runtime.lock import (
    BYPASS_ENV_VAR,
    LOCK_FILENAME,
    AgentHomeBusyError,
    AgentHomeLock,
    acquire_agent_home_lock,
    read_lock_holder,
)


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch):
    """The bypass env var must not leak in from the developer's shell."""
    monkeypatch.delenv(BYPASS_ENV_VAR, raising=False)


def test_acquire_creates_lock_file_with_holder_metadata(tmp_path):
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    try:
        assert lock.held
        payload = json.loads((tmp_path / LOCK_FILENAME).read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["mode"] == "cli"
        assert payload["started_at"]
    finally:
        lock.release()


def test_second_acquire_on_same_home_is_rejected(tmp_path):
    first = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    try:
        with pytest.raises(AgentHomeBusyError) as excinfo:
            AgentHomeLock(home=tmp_path, mode="gateway").acquire()
        assert excinfo.value.holder is not None
        assert excinfo.value.holder.pid == os.getpid()
        assert excinfo.value.holder.mode == "cli"
    finally:
        first.release()


def test_busy_error_message_names_the_home_and_the_holder(tmp_path):
    first = AgentHomeLock(home=tmp_path, mode="gateway").acquire()
    try:
        with pytest.raises(AgentHomeBusyError) as excinfo:
            AgentHomeLock(home=tmp_path, mode="cli").acquire()
        message = str(excinfo.value)
        assert str(tmp_path) in message
        assert "gateway" in message
        assert BYPASS_ENV_VAR in message
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path):
    first = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    first.release()
    assert not first.held

    second = AgentHomeLock(home=tmp_path, mode="gateway").acquire()
    try:
        assert second.held
    finally:
        second.release()


def test_release_blanks_metadata_so_no_stale_holder_is_reported(tmp_path):
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    lock.release()
    assert read_lock_holder(tmp_path / LOCK_FILENAME) is None


def test_release_is_idempotent(tmp_path):
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    lock.release()
    lock.release()  # must not raise
    assert not lock.held


def test_distinct_homes_do_not_conflict(tmp_path):
    """``--name`` instances are isolated because the lock lives in the home."""
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    lock_a = AgentHomeLock(home=home_a, mode="cli").acquire()
    lock_b = AgentHomeLock(home=home_b, mode="gateway").acquire()
    try:
        assert lock_a.held and lock_b.held
    finally:
        lock_a.release()
        lock_b.release()


def test_bypass_env_var_disables_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv(BYPASS_ENV_VAR, "1")
    first = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    second = AgentHomeLock(home=tmp_path, mode="gateway").acquire()
    try:
        # Opting out means no file is taken at all, so neither reports held.
        assert not first.held
        assert not second.held
    finally:
        first.release()
        second.release()


def test_context_manager_releases_on_exit(tmp_path):
    with AgentHomeLock(home=tmp_path, mode="cli") as lock:
        assert lock.held
    assert not lock.held
    AgentHomeLock(home=tmp_path, mode="gateway").acquire().release()


def test_double_acquire_on_same_object_is_a_noop(tmp_path):
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    try:
        assert lock.acquire() is lock
        assert lock.held
    finally:
        lock.release()


def test_lock_file_is_created_with_owner_only_permissions(tmp_path):
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    try:
        mode = (tmp_path / LOCK_FILENAME).stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        lock.release()


def test_acquire_creates_missing_home_directory(tmp_path):
    home = tmp_path / "not-yet-there"
    lock = AgentHomeLock(home=home, mode="cli").acquire()
    try:
        assert (home / LOCK_FILENAME).exists()
    finally:
        lock.release()


def test_read_lock_holder_tolerates_missing_and_corrupt_files(tmp_path):
    assert read_lock_holder(tmp_path / "absent.lock") is None
    corrupt = tmp_path / "corrupt.lock"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_lock_holder(corrupt) is None


def test_convenience_helper_defaults_to_current_agent_home(tmp_path, monkeypatch):
    from agent import shared

    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path)
    lock = acquire_agent_home_lock("cli")
    try:
        assert lock.home == tmp_path
        assert (tmp_path / LOCK_FILENAME).exists()
    finally:
        lock.release()


def test_home_is_resolved_lazily_so_set_agent_home_is_honoured(tmp_path, monkeypatch):
    """``--name`` rewrites AGENT_HOME after import, so binding must be late."""
    from agent import shared

    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / "before")
    lock = AgentHomeLock(mode="gateway")
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path / "after")
    lock.acquire()
    try:
        assert lock.home == tmp_path / "after"
        assert (tmp_path / "after" / LOCK_FILENAME).exists()
        assert not (tmp_path / "before" / LOCK_FILENAME).exists()
    finally:
        lock.release()


def test_lock_file_survives_release_so_waiters_never_see_a_dead_inode(tmp_path):
    """Unlinking would let a waiter hold a lock on an inode nobody else sees."""
    path: Path = tmp_path / LOCK_FILENAME
    lock = AgentHomeLock(home=tmp_path, mode="cli").acquire()
    lock.release()
    assert path.exists()
