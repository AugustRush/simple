"""A daemon's trail: ``simple gateway`` and ``simple scheduler`` write a log file.

Why this is a test and not two lines in ``cli.py``
--------------------------------------------------
The gateway is meant to run for days, and until this existed its only output was the
terminal it was started from.  "What happened at 3am?" had no answer and the honest one
was "nothing was kept".  Three properties make the difference between a useful trail and
a new failure mode, and each is pinned here:

* it **never kills the process** -- a home that cannot be written yields ``None``, not an
  exception, because losing logs is bad and refusing to serve is worse;
* it is **idempotent** -- starting twice in one process must not leave two handlers on
  the same file (every line twice, two descriptors);
* it is **bounded** -- rotation is configured, because "I have logs" must not become "I
  have a full disk".
"""

from __future__ import annotations

import logging

import pytest

from agent import logs


@pytest.fixture()
def clean_root_logger():
    """A known process state: no file handler from an earlier test, and one at the end.

    The root logger is process-wide and ``attach_file_log`` is *deliberately* attached
    once per process, so a test that runs after another test started a gateway in-process
    would otherwise find that gateway's handler and write into **its** file.  Found
    exactly that way: these tests passed alone and three of them failed in the full run.
    Detaching at setup is not papering over it -- the contract under test is "one handler
    per process", and a test has to arrive at the process with none.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    for handler in list(root.handlers):
        if getattr(handler, "name", "") == logs._HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()
    yield root
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
            root.removeHandler(handler)
    root.setLevel(before_level)


def test_the_log_lands_in_the_agent_home(tmp_path, clean_root_logger) -> None:
    path = logs.attach_file_log("gateway", home=tmp_path)
    assert path == tmp_path / "logs" / "gateway.log"

    logging.getLogger("agent.probe").info("the scheduler tick failed")
    for handler in clean_root_logger.handlers:
        handler.flush()

    text = path.read_text(encoding="utf-8")
    assert "the scheduler tick failed" in text
    assert "agent.probe" in text, "the logger name is what makes a trail attributable"
    # ...and the line is timestamped, which is the whole point of a trail.
    assert text.split()[0][:2].isdigit()


def test_attaching_twice_does_not_double_every_line(tmp_path, clean_root_logger) -> None:
    """Two handlers on one file: every record written twice, two descriptors, no warning."""
    first = logs.attach_file_log("gateway", home=tmp_path)
    second = logs.attach_file_log("gateway", home=tmp_path)

    assert first == second
    attached = [
        h for h in clean_root_logger.handlers if getattr(h, "name", "") == logs._HANDLER_NAME
    ]
    assert len(attached) == 1, f"{len(attached)} file handlers attached"

    logging.getLogger("agent.probe").info("once")
    for handler in attached:
        handler.flush()
    assert first.read_text(encoding="utf-8").count("once") == 1


def test_rotation_is_configured(tmp_path, clean_root_logger) -> None:
    logs.attach_file_log("gateway", home=tmp_path)
    handler = next(
        h for h in clean_root_logger.handlers if getattr(h, "name", "") == logs._HANDLER_NAME
    )

    assert handler.maxBytes == logs.MAX_BYTES
    assert handler.backupCount == logs.BACKUPS
    assert logs.MAX_BYTES > 0 and logs.BACKUPS >= 1


def test_a_home_that_cannot_be_written_is_reported_not_raised(tmp_path, clean_root_logger) -> None:
    """A logging failure must never be the reason the gateway does not start."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("this is a file, so mkdir under it cannot work")

    assert logs.attach_file_log("gateway", home=blocked) is None
    assert logs.describe(None).startswith("log file unavailable")
    # ...and nothing was attached in the process.
    assert not [
        h for h in clean_root_logger.handlers if getattr(h, "name", "") == logs._HANDLER_NAME
    ]


def test_describe_names_the_file_it_would_write(tmp_path) -> None:
    path = tmp_path / "logs" / "gateway.log"
    assert logs.describe(path) == str(path)
