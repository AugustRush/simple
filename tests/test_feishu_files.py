"""Naming and typing Feishu's downloaded resources, asserted directly.

These three helpers were ``@staticmethod``/methods on ``FeishuChannel`` with no
tests of their own -- the download path exercised them.  The first one is a
boundary rather than a convenience: the filename comes off the wire, and the
process is about to write to the path built from it.
"""

from __future__ import annotations

from pathlib import Path

from agent.channels.feishu_files import (
    attachment_mime_type,
    resource_fallback_filename,
    safe_resource_filename,
)


def test_a_wire_filename_cannot_choose_a_directory():
    """Only the basename survives: a resource name is not a path.

    Feishu supplies the name, so ``../../etc/passwd`` would otherwise let the
    sender pick where this process writes.
    """
    assert safe_resource_filename("../../etc/passwd", "fallback") == "passwd"
    assert safe_resource_filename("/tmp/absolute.png", "fallback") == "absolute.png"
    assert safe_resource_filename("dir/inner.png", "fallback") == "inner.png"


def test_an_empty_name_falls_back_rather_than_producing_nothing():
    assert safe_resource_filename("", "fallback") == "fallback"
    assert safe_resource_filename("   ", "fallback") == "fallback"
    assert safe_resource_filename(None, "fallback") == "fallback"


def test_the_name_wins_over_the_message_type():
    """``.png`` on a file Feishu called "image" is the better answer."""
    assert attachment_mime_type(Path("photo.jpeg"), "image") == "image/jpeg"
    assert attachment_mime_type(Path("clip.mp3"), "audio") == "audio/mpeg"


def test_the_message_type_is_used_when_the_name_says_nothing():
    assert attachment_mime_type(Path("resource"), "image") == "image/png"
    assert attachment_mime_type(Path("resource"), "audio") == "audio/mpeg"
    assert attachment_mime_type(Path("resource"), "media") == "video/mp4"
    assert attachment_mime_type(Path("resource"), "video") == "video/mp4"
    assert attachment_mime_type(Path("resource"), "file") == "application/octet-stream"


def test_a_nameless_resource_still_gets_a_name_on_disk():
    assert resource_fallback_filename("img_key", "image") == "img_key.png"
    assert resource_fallback_filename("aud_key", "audio") == "aud_key.mp3"
    assert resource_fallback_filename("vid_key", "media") == "vid_key.mp4"
    # An unknown type keeps the key as-is rather than inventing an extension.
    assert resource_fallback_filename("file_key", "file") == "file_key"
