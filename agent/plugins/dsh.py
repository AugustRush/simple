"""Reading a DeepSeek Harness profile: its declarative half, and nothing else.

Why read-only
-------------
A DSH plugin is a Node/Cordis package.  This runtime is Python: it cannot execute
one, and pretending otherwise would be the dishonest kind of interop -- a
"supported" format that fails at the first call.  What it *can* do is read what a
profile **declares** -- which MCP servers it configures, which bundles it loads,
which skill roots it scans -- and hand the parts that have an equivalent here to
the code that already consumes them.

That is also why this is not a :class:`~agent.plugins.formats.PluginFormat`: a
format describes a plugin *directory* the loader can mount, and a DSH profile is
not one.  What it produces is configuration for services this runtime already has.

The subset reader, and why there is no PyYAML dependency
-------------------------------------------------------
A cordis patch file is YAML, ``!!js`` expressions and all.  This project has no
YAML parser, and adding a dependency for a read-only interop feature is a decision
for the operator, not for a probe.  So :func:`load_yaml_subset` reads the subset a
patch file actually uses -- block sequences and mappings, quoted and plain
scalars, inline flow lists, comments, ``!!js`` -- and **refuses** everything else
with the line number, because a parser that guesses at a config file produces a
plausible wrong answer.  If PyYAML is installed, :func:`read_dsh_profile` uses it
instead, automatically; nothing here needs to change for that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from agent.plugins.formats import SHARED_SKILL_ROOTS

__all__ = [
    "DshProfile",
    "JsExpression",
    "YamlSubsetError",
    "load_yaml",
    "load_yaml_subset",
    "read_dsh_profile",
]

#: The Cordis plugin name a profile uses to declare an MCP server.
DSH_MCP_PLUGIN = "@deepseek-ai/dsh-mcp-client"

#: Skill roots DSH scans that this runtime can also read.  Taken from the format
#: adapters rather than copied: the conventions belong there (see
#: :data:`agent.plugins.formats.SHARED_SKILL_ROOTS`), and a second copy here is a
#: second thing to keep in step.
DSH_SKILL_ROOTS = tuple(root.path for root in SHARED_SKILL_ROOTS)


class YamlSubsetError(ValueError):
    """The file uses YAML this reader deliberately does not understand."""


@dataclass(frozen=True)
class JsExpression:
    """A ``!!js`` value, kept as source text.

    Not evaluated, and not silently turned into a string that looks like a value:
    a caller has to decide what an unevaluated expression means for it.  The
    profile reader records one as a warning when an MCP row depends on it.
    """

    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class DshProfile:
    """What a DSH profile declares, in this runtime's terms."""

    path: Path
    bundles: tuple[str, ...] = ()
    #: MCP servers in the shape this runtime's client consumes.
    mcp_servers: tuple[dict, ...] = ()
    #: Skill roots that exist on this machine.
    skill_roots: tuple[Path, ...] = ()
    #: Declarations that have no equivalent here (an sse transport, an
    #: unevaluated expression), each with the reason.
    unsupported: tuple[str, ...] = ()
    #: What could not be read at all, and why.
    warnings: tuple[str, ...] = ()
    #: The raw patch entries, for a caller that wants to look for more.
    patch_entries: tuple[dict, ...] = ()


# ── the YAML subset ──────────────────────────────────────────────────────────

_SCALAR_INT = re.compile(r"^-?\d+$")
_SCALAR_FLOAT = re.compile(r"^-?\d+\.\d+$")


@dataclass(frozen=True)
class _Line:
    indent: int
    content: str
    number: int


def _significant_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlSubsetError(f"tab indentation is not YAML (line {number})")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(_Line(len(raw) - len(raw.lstrip()), stripped, number))
    return lines


def _scalar(token: str, number: int) -> Any:
    token = token.strip()
    if token.startswith("!!js"):
        return JsExpression(token[len("!!js") :].strip())
    if token.startswith("&") or token.startswith("*"):
        raise YamlSubsetError(f"anchors and aliases are outside the subset (line {number})")
    if token.startswith("|") or token.startswith(">"):
        raise YamlSubsetError(f"block scalars are outside the subset (line {number})")
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    if token.startswith("[") and token.endswith("]"):
        body = token[1:-1].strip()
        if not body:
            return []
        return [_scalar(part, number) for part in _split_flow(body)]
    if token == "{}":
        return {}
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token in ("null", "~", ""):
        return None
    if _SCALAR_INT.match(token):
        return int(token)
    if _SCALAR_FLOAT.match(token):
        return float(token)
    return token


def _split_flow(body: str) -> list[str]:
    """Split an inline flow list on commas that are not inside quotes."""
    parts: list[str] = []
    current = ""
    quote = ""
    for char in body:
        if quote:
            current += char
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char == ",":
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _parse_block(lines: list[_Line], index: int, indent: int) -> tuple[Any, int]:
    content = lines[index].content
    if content.startswith("-"):
        return _parse_sequence(lines, index, indent)
    if ":" not in content:
        # A bare flow value where a block would otherwise start -- a patch file
        # that is entirely ``[]`` is a real file (it is this machine's
        # ``cordis.yml``), and reading it as a malformed mapping would report a
        # warning about a file that is perfectly valid.
        return _scalar(content, lines[index].number), index + 1
    return _parse_mapping(lines, index, indent)


def _parse_sequence(lines: list[_Line], index: int, indent: int) -> tuple[list, int]:
    items: list[Any] = []
    while index < len(lines) and lines[index].indent == indent and lines[index].content.startswith("-"):
        line = lines[index]
        rest = line.content[1:].strip()
        if not rest:
            # ``-`` alone: the item is the block that follows, one level deeper.
            index += 1
            if index >= len(lines) or lines[index].indent <= indent:
                items.append(None)
                continue
            value, index = _parse_block(lines, index, lines[index].indent)
            items.append(value)
            continue
        if ":" in rest and not rest.startswith(("[", "{", '"', "'")):
            # ``- key: value``: a mapping item whose first key is on this line.
            key, _, inline = rest.partition(":")
            item: dict[str, Any] = {}
            item_indent = line.indent + 2
            if inline.strip():
                item[key.strip()] = _scalar(inline, line.number)
                index += 1
            else:
                index += 1
                if index < len(lines) and lines[index].indent > line.indent:
                    value, index = _parse_block(lines, index, lines[index].indent)
                    item[key.strip()] = value
                    item_indent = lines[index - 1].indent if index else item_indent
                else:
                    item[key.strip()] = None
            # Keys that continue the same mapping, indented under the dash.
            while index < len(lines) and lines[index].indent > line.indent:
                nested = lines[index]
                if nested.content.startswith("-"):
                    break
                key2, _, inline2 = nested.content.partition(":")
                if inline2.strip():
                    item[key2.strip()] = _scalar(inline2, nested.number)
                    index += 1
                else:
                    index += 1
                    if index < len(lines) and lines[index].indent > nested.indent:
                        value, index = _parse_block(lines, index, lines[index].indent)
                        item[key2.strip()] = value
                    else:
                        item[key2.strip()] = None
            items.append(item)
            continue
        items.append(_scalar(rest, line.number))
        index += 1
    return items, index


def _parse_mapping(lines: list[_Line], index: int, indent: int) -> tuple[dict, int]:
    mapping: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent and not lines[index].content.startswith("-"):
        line = lines[index]
        key, sep, inline = line.content.partition(":")
        if not sep:
            raise YamlSubsetError(f"expected 'key: value' (line {line.number})")
        key = key.strip()
        if inline.strip():
            mapping[key] = _scalar(inline, line.number)
            index += 1
            continue
        index += 1
        if index < len(lines) and lines[index].indent > indent:
            value, index = _parse_block(lines, index, lines[index].indent)
            mapping[key] = value
        else:
            mapping[key] = None
    return mapping, index


def load_yaml_subset(text: str) -> Any:
    """Read the YAML subset a cordis patch file uses.

    Raises :class:`YamlSubsetError` for anything outside it -- anchors, block
    scalars, tabs -- rather than guessing, because the caller's next step is to
    hand the result to a process launcher.
    """
    lines = _significant_lines(text)
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise YamlSubsetError(f"trailing content the subset reader cannot place (line {lines[index].number})")
    return value


def load_yaml(text: str) -> Any:
    """The same, through PyYAML when it is installed.

    The dependency is optional and the reader above is the fallback, so a
    deployment that already has PyYAML gets a full parser and one that does not
    still gets the profile's declarative half.
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return load_yaml_subset(text)
    return yaml.safe_load(text)


# ── the profile ──────────────────────────────────────────────────────────────


def _walk_entries(entries: Any) -> Iterator[dict]:
    """Every patch entry, including the ones nested in ``insert:`` lists."""
    if isinstance(entries, dict):
        yield entries
        for value in entries.values():
            if isinstance(value, list):
                yield from _walk_entries(value)
    elif isinstance(entries, list):
        for item in entries:
            yield from _walk_entries(item)


def _to_mcp_config(config: dict) -> tuple[Optional[dict], str]:
    """One DSH MCP row as this runtime's client takes it, or why it cannot be."""
    name = str(config.get("serverName") or config.get("name") or "").strip()
    transport = str(config.get("transport") or ("stdio" if config.get("command") else "")).strip()
    if transport != "stdio":
        return None, (
            f"{name or '?'}: transport {transport or '?'} is not supported by this "
            "runtime's MCP client (stdio only)"
        )
    command = config.get("command")
    if isinstance(command, JsExpression):
        return None, f"{name or '?'}: the command is an unevaluated !!js expression"
    command = str(command or "").strip()
    if not command:
        return None, f"{name or '?'}: no command"
    converted: dict[str, Any] = {
        "name": name or Path(command).name or "mcp",
        "command": command,
    }
    if config.get("args"):
        converted["args"] = [str(arg) for arg in config["args"]]
    env = config.get("env")
    if isinstance(env, dict):
        converted["env"] = {str(key): str(value) for key, value in env.items()}
    if config.get("cwd"):
        converted["cwd"] = str(config["cwd"])
    return converted, ""


def read_dsh_profile(profile_dir: Path | str) -> DshProfile:
    """Read one DSH profile directory.  Never executes anything it finds."""
    path = Path(profile_dir).expanduser()
    bundles: tuple[str, ...] = ()
    patch_entries: list[dict] = []
    unsupported: list[str] = []
    warnings: list[str] = []

    package_json = path / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"package.json: {exc}")
            payload = {}
        declared = ((payload.get("dsh") or {}).get("profile") or {}).get("bundles") or []
        bundles = tuple(str(name) for name in declared)

    for filename in ("cordis.yml", "cordis.patch.yml"):
        source = path / filename
        if not source.is_file():
            continue
        try:
            parsed = load_yaml(source.read_text(encoding="utf-8"))
        except (OSError, YamlSubsetError, ValueError) as exc:
            warnings.append(f"{filename}: {exc}")
            continue
        patch_entries.extend(_walk_entries(parsed) if parsed else [])

    mcp_servers: list[dict] = []
    for entry in patch_entries:
        if str(entry.get("name") or "") != DSH_MCP_PLUGIN:
            continue
        config = entry.get("config")
        if not isinstance(config, dict):
            unsupported.append(f"{entry.get('id') or '?'}: no config block")
            continue
        converted, reason = _to_mcp_config(config)
        if converted is None:
            unsupported.append(reason)
        else:
            mcp_servers.append(converted)

    roots = [Path(root).expanduser() for root in DSH_SKILL_ROOTS]
    roots.append(path / "skills")
    skill_roots = tuple(root for root in roots if root.is_dir())

    return DshProfile(
        path=path,
        bundles=bundles,
        mcp_servers=tuple(mcp_servers),
        skill_roots=skill_roots,
        unsupported=tuple(unsupported),
        warnings=tuple(warnings),
        patch_entries=tuple(patch_entries),
    )
