"""Publishing a refreshed system prompt to the component container.

The write side of a prompt refresh was performed in five places across four
modules -- the bootstrap, the evolution plugin twice, the runtime contracts
twice -- and each did the same three things:

    components["system_prompt"] = composed
    ctx = components.get("ctx")
    if ctx is not None:
        ctx.system_prompt = composed

Two of those also stored ``base_system_prompt`` first, and the *composition*
itself differs between callers in one real way (the evolution plugin falls back
to the working directory when no workspace is configured).  So what is shared is
not how the prompt is built -- that stays with each caller -- but **where a
composed prompt is published**.

Why that is worth naming
------------------------
``components["system_prompt"]`` has five readers and several writers, so "the
prompt changed" is a fact that has to reach all of them.  When the write is
copied, the *copies* drift: one updates ``ctx.system_prompt`` and another does
not, and the symptom is a stale prompt in whichever reader the author forgot.
One publisher makes "publishing a prompt" a single fact about what it means.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

from agent.kernel.service import resolve

__all__ = ["publish_system_prompt", "refresh_system_prompt"]

#: The component key readers agree on.
PROMPT_KEY = "system_prompt"
BASE_PROMPT_KEY = "base_system_prompt"
CTX_KEY = "ctx"


def publish_system_prompt(
    components: MutableMapping[str, Any] | Mapping[str, Any],
    composed: str,
) -> str:
    """Publish *composed* as the live system prompt.  Returns it.

    Written into the component mapping and, when a live context is present, onto
    that context as well -- because the two are read by different consumers and a
    refresh that reached only one of them is the bug this exists to prevent.

    A mapping that cannot be written returns the prompt unchanged rather than
    raising: a caller in a read-only composition still gets the composed value
    back and can pass it along.
    """
    try:
        components[PROMPT_KEY] = composed  # type: ignore[index]
    except TypeError:
        return composed
    ctx = components.get(CTX_KEY)
    if ctx is not None:
        ctx.system_prompt = composed
    return composed


def refresh_system_prompt(
    components: MutableMapping[str, Any] | Mapping[str, Any],
    *,
    base_prompt: str | None = None,
) -> str:
    """Recompose the prompt from the components and publish it.

    The base prompt defaults to whatever the container already holds, so the
    common refresh -- skills changed, so the prompt describing them is stale --
    does not have to fetch it first.  Passing ``base_prompt`` explicitly is for
    a caller that has just produced a new one and is storing it: the caller sets
    ``base_system_prompt`` itself, because that is a separate fact from the
    composed prompt and not every refresh wants to change it.
    """
    from agent.config import _compose_system_prompt

    base = (
        base_prompt
        if base_prompt is not None
        else components.get(BASE_PROMPT_KEY, "")
    )
    # The three services resolve through the container; ``workspace_root`` and
    # ``output_dir`` are paths the runtime looks up by name, not services, so they
    # keep the plain mapping read.  The vocabulary is in
    # ``agent/runtime/core_services.py``.
    composed = _compose_system_prompt(
        base,
        resolve(components, "registry"),
        components.get("workspace_root"),
        components.get("output_dir"),
        skill_catalog=resolve(components, "skill_catalog"),
        plugin_catalog=resolve(components, "plugin_catalog"),
    )
    return publish_system_prompt(components, composed)
