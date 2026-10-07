"""The built-in channels, as plugins rather than branches in the composition root.

``build_gateway_channels`` used to hold a branch per channel: read
``cfg["channels"]["web"]["enabled"]``, import ``WebChannel``, filter the config against
its dataclass fields, construct it, and repeat for Feishu.  Two channels meant two
branches, and a third meant a third.

As plugins they are the first users of the ``channels`` registry rather than special
cases in it, and a third party contributes one the same way -- which is the thing that
was impossible before, not an aesthetic difference.

Why these do not fail when disabled
-----------------------------------
Each plugin is mounted unconditionally and contributes nothing unless its config section
says ``enabled``.  The alternative -- mount it only when enabled -- puts the switch back
in the composition root, which is where it was.  A mounted-but-inert plugin costs one
``setup`` call and keeps "which channels exist" a question about the composition, while
"which are on" stays a question about the config.

A channel whose dependency is missing is skipped with a report, not raised: a gateway
with a broken Feishu install should still serve its web channel.  That behaviour was in
the old branches and is the one thing about them worth keeping.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FeishuChannelPlugin", "WebChannelPlugin"]


def _section(cfg: dict, name: str) -> dict:
    return (cfg.get("channels", {}) or {}).get(name, {}) or {}


def _filtered(cfg_section: dict, config_cls: Any) -> dict:
    """The config keys the channel's dataclass declares, and no others.

    A stale key in ``config.json`` is ignored rather than raising at construction, which
    is what made it safe to add a field and later remove it.
    """
    known = config_cls.__dataclass_fields__
    return {key: value for key, value in cfg_section.items() if key in known}


class WebChannelPlugin:
    """Contributes the web channel when ``channels.web.enabled`` is set."""

    name = "channel.web"
    inject = ("channels", "cfg")

    def setup(self, ctx: Any) -> None:
        section = _section(ctx.get("cfg", None) or {}, "web")
        if not section.get("enabled"):
            return
        try:
            from agent.channels.web import WebChannel, WebConfig
        except ImportError as exc:
            _report("Web", f"requires starlette/uvicorn: {exc}")
            return
        except Exception as exc:
            _report("Web", f"init failed: {exc}")
            return
        try:
            ctx.channels.add(WebChannel(WebConfig(**_filtered(section, WebConfig))))
        except Exception as exc:
            _report("Web", f"init failed: {exc}")
            return
        _enabled("Web")


class FeishuChannelPlugin:
    """Contributes the Feishu channel when ``channels.feishu.enabled`` is set."""

    name = "channel.feishu"
    inject = ("channels", "cfg")

    def setup(self, ctx: Any) -> None:
        section = _section(ctx.get("cfg", None) or {}, "feishu")
        if not section.get("enabled"):
            return
        try:
            from agent.channels.feishu import FeishuChannel, FeishuConfig
        except ImportError as exc:
            _report("Feishu", f"requires lark-oapi: {exc}")
            return
        except Exception as exc:
            _report("Feishu", f"init failed: {exc}")
            return
        try:
            ctx.channels.add(FeishuChannel(FeishuConfig(**_filtered(section, FeishuConfig))))
        except Exception as exc:
            _report("Feishu", f"init failed: {exc}")
            return
        _enabled("Feishu")


def _report(which: str, why: str) -> None:
    from agent import shared

    shared.CONSOLE.print(f"[red]{which} channel {why}[/red]")


def _enabled(which: str) -> None:
    from agent import shared

    shared.CONSOLE.print(f"[dim]{which} channel enabled[/dim]")
