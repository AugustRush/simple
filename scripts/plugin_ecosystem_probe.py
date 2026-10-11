#!/usr/bin/env python3
"""Install Claude Code, Codex and DeepSeek Harness plugins and report what survives.

Why this exists
---------------
Three agent ecosystems now ship "plugins", and they are not the same thing:

* **Claude Code** -- ``.claude-plugin/plugin.json`` plus convention directories
  (``skills/``, ``commands/``, ``agents/``, ``hooks/hooks.json``, ``.mcp.json``),
  distributed through ``.claude-plugin/marketplace.json``;
* **Codex** -- two live layouts: the portable *Agent Plugins v1* root
  ``plugin.json`` (``$schema`` + ``skills``/``mcp.json`` defaults) and the legacy
  ``.codex-plugin/plugin.json`` with ``skills`` / ``mcpServers`` / ``apps`` /
  ``hooks`` path fields; distributed through
  ``.agents/plugins/marketplace.json``;
* **DeepSeek Harness** -- a Cordis (Node) profile: npm packages named in
  ``cordis.yml`` and installed with pnpm.  There is no JSON manifest to read.

This project claims to load the first two.  A claim like that is worth exactly as
much as the measurement behind it, and the fixtures here are written the way each
ecosystem's own documentation says a plugin is laid out -- default locations, the
real marketplace paths, the real field names -- then pushed through the project's
**own** loader and installer.  Nothing is reimplemented: the numbers come from
``PluginCatalog`` and the ``install_plugin`` tool.

Usage
-----
    uv run python scripts/plugin_ecosystem_probe.py
    uv run python scripts/plugin_ecosystem_probe.py --keep-home

Exit code is 0 when nothing failed.  A fixture that loads *nothing* is a failure
for that ecosystem; an asset the ecosystem has no counterpart for is reported as
``-`` and is not a failure by itself (see the caveat lines the report prints).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OK = "ok"
FAIL = "fail"
SKIP = "skip"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ── the fixtures, exactly as each ecosystem documents them ───────────────────

CC_MANIFEST = {
    # Every default location is used, and the manifest deliberately does NOT
    # declare `hooks` -- Claude Code's standard layout puts them in
    # hooks/hooks.json, so a reader that only looks at the manifest key misses
    # them.
    "name": "cc-demo",
    "displayName": "Claude Code Demo",
    "version": "1.0.0",
    "description": "A skill, a command, an agent, a hook and an MCP server",
    "author": {"name": "Example Team", "email": "dev@example.com"},
}

CC_SKILL = """---
name: cc-hello
description: Say hello the Claude Code way.
---

# Hello

Say hello, in the plugin's own voice.
"""

CC_COMMAND = """---
description: Show the status of the demo plugin
argument-hint: [name]
allowed-tools: Read
---

Report the status for $ARGUMENTS, or everything when no name was given.
"""

CC_AGENT = """---
name: reviewer
description: Review a diff for injection, auth and secret-handling risks
tools: Read, Grep
---

You are a security reviewer. Read the changed files and report risks.
"""

CC_HOOKS = {
    "hooks": {
        # dispatched by this project
        "PreToolUse": [
            {
                "matcher": "shell",
                "hooks": [
                    {
                        "type": "command",
                        "command": '"${CLAUDE_PLUGIN_ROOT}/scripts/guard.sh"',
                        "timeout": 10,
                    }
                ],
            }
        ],
        # translated but never run here (no counterpart in this codebase)
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": '"${CLAUDE_PLUGIN_ROOT}/scripts/start.sh"',
                    }
                ]
            }
        ],
    }
}

CODEX_MANIFEST = {
    "name": "codex-demo",
    "version": "1.0.0",
    "description": "A Codex plugin: one skill, one MCP server, one app connector",
    # Codex path fields are top-level strings that must start with ./
    "skills": "./skills",
    "mcpServers": "./.mcp.json",
    "apps": "./.app.json",
    "interface": {
        "displayName": "Codex Demo",
        "defaultPrompt": ["Summarise this repository"],
    },
}

CODEX_MCP = {
    "mcpServers": {
        "codex-demo-mcp": {"command": "python3", "args": ["-c", "print(1)"]}
    }
}

CODEX_APPS = {"apps": {"demo-connector": {"connector": "https://example.com"}}}

CODEX_MARKETPLACE = {
    "name": "codex-market",
    "interface": {"displayName": "Codex Market"},
    "plugins": [
        {
            "name": "beta",
            "source": {"source": "local", "path": "./plugins/beta"},
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
                "products": ["CODEX"],
            },
            "category": "Productivity",
        }
    ],
}

CC_MARKETPLACE = {
    "name": "cc-market",
    "owner": {"name": "Example"},
    "plugins": [
        {
            "name": "alpha",
            "source": "./plugins/alpha",
            "description": "A local marketplace entry",
        }
    ],
}

DSH_PACKAGE = {
    # A DeepSeek Harness plugin is an npm package exporting a Cordis plugin; it
    # is declared in the profile's cordis.yml, not in a plugin.json.
    "name": "@acme/dsh-demo",
    "version": "1.0.0",
    "description": "A DSH Cordis plugin (npm package)",
    "main": "lib/index.js",
    "type": "module",
}

DSH_ENTRY = """// The package's Cordis entry point, as DSH's own plugins ship it.
export const name = 'acme-dsh-demo'
export const inject = ['tools']

export function apply(ctx) {
  ctx.provide('acme-dsh-demo', { ready: true })
}
"""

DSH_CORDIS_ENTRY = """# The profile entry that activates the package (cordis.patch.yml):
- insert:
    - id: acme-dsh-demo
      name: "@acme/dsh-demo"
      config: {}
"""


#: Which ecosystem each fixture is written for.  ``dsh`` is expected NOT to be a
#: plugin here: a Cordis plugin is a Node module, so the honest result for that
#: row is "the concept does not apply", not a compatibility failure.
ECOSYSTEM = {
    "cc-demo": "claude-code",
    "cc-hooks-only": "claude-code",
    "cc-market": "claude-code",
    "codex-demo": "codex",
    "codex-hooks": "codex",
    "agent-plugins-demo": "codex",
    "codex-market": "codex",
    "codex-market-at-root": "codex",
    "dsh-demo": "dsh",
}

DSH_NOTE = (
    "no plugin format for this runtime: a DSH plugin is a Node/Cordis module. "
    "Interop is read-only -- its MCP declarations and skill roots (agent/plugins/dsh.py)"
)


def report_dsh_profile() -> None:
    """What this runtime can take from a real DSH profile, measured.

    Read-only and best-effort: the profile is the operator's, the file may use
    YAML outside the subset reader, and both are reported rather than treated as
    failures.  Without this section the DSH row above is a note; with it the
    claim "MCP servers are the interop" has a number behind it.
    """
    from agent.plugins.dsh import read_dsh_profile

    profiles = sorted((Path.home() / ".dsh" / "profiles").glob("*"))
    if not profiles:
        print("  (no ~/.dsh/profiles on this machine)")
        return
    for profile_dir in profiles:
        if not profile_dir.is_dir():
            continue
        profile = read_dsh_profile(profile_dir)
        servers = ", ".join(server["name"] for server in profile.mcp_servers) or "-"
        print(f"  {profile_dir.name}: bundles={len(profile.bundles)} mcp={servers}")
        for reason in profile.unsupported:
            print(f"    not convertible: {reason}")
        for warning in profile.warnings:
            print(f"    unreadable: {warning}")


#: Whether each fixture ships something its own ecosystem defines as executable
#: (hook commands, stdio MCP servers, or a language-runtime package).  The
#: installer is expected to require human consent for exactly these.
EXECUTABLE = {
    "cc-demo": True,          # hooks/hooks.json + .mcp.json
    "cc-hooks-only": True,    # hooks/hooks.json only
    "cc-market": False,       # a marketplace: skills only
    "codex-demo": True,       # .mcp.json
    "codex-hooks": True,      # hooks/hooks.json
    "agent-plugins-demo": True,  # mcp.json spawns a process
    "codex-market": False,
    "codex-market-at-root": False,
    "dsh-demo": True,         # an npm package is code by definition
}


def _skill(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{description}\n"


def write_fixtures(root: Path) -> None:
    """Materialise every fixture under *root* (one directory per entry)."""

    def dump(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def text(path: Path, body: str, *, executable: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        if executable:
            path.chmod(0o755)

    # ── Claude Code ──────────────────────────────────────────────────────────
    cc = root / "cc-demo"
    dump(cc / ".claude-plugin" / "plugin.json", CC_MANIFEST)
    text(cc / "skills" / "cc-hello" / "SKILL.md", CC_SKILL)
    text(cc / "commands" / "status.md", CC_COMMAND)
    text(cc / "agents" / "reviewer.md", CC_AGENT)
    dump(cc / "hooks" / "hooks.json", CC_HOOKS)
    dump(
        cc / ".mcp.json",
        {"mcpServers": {"cc-demo-mcp": {"command": "python3", "args": ["-c", "print(1)"]}}},
    )
    text(cc / "scripts" / "guard.sh", "#!/bin/sh\nexit 0\n", executable=True)
    text(cc / "scripts" / "start.sh", "#!/bin/sh\nexit 0\n", executable=True)
    text(cc / "bin" / "cc-tool", "#!/bin/sh\necho hi\n", executable=True)
    text(cc / "output-styles" / "terse.md", "---\nname: terse\n---\nBe terse.\n")
    dump(cc / "settings.json", {"agent": "reviewer"})

    # A Claude Code plugin whose only executable content is hooks/hooks.json --
    # the default location, and the one an installer has to look at to know it
    # is about to enable arbitrary shell.
    cc_hooks_only = root / "cc-hooks-only"
    dump(
        cc_hooks_only / ".claude-plugin" / "plugin.json",
        {"name": "cc-hooks-only", "version": "1.0.0", "description": "hooks only"},
    )
    text(
        cc_hooks_only / "skills" / "hooked" / "SKILL.md",
        _skill("hooked", "A plugin that also runs a hook on every tool call."),
    )
    dump(
        cc_hooks_only / "hooks" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"${CLAUDE_PLUGIN_ROOT}/scripts/audit.sh"',
                            }
                        ],
                    }
                ]
            }
        },
    )
    text(
        cc_hooks_only / "scripts" / "audit.sh",
        "#!/bin/sh\necho audited >&2\nexit 0\n",
        executable=True,
    )

    # ── Codex ───────────────────────────────────────────────────────────────
    codex = root / "codex-demo"
    dump(codex / ".codex-plugin" / "plugin.json", CODEX_MANIFEST)
    text(codex / "skills" / "codex-hello" / "SKILL.md", _skill("codex-hello", "Say hello, Codex style."))
    dump(codex / ".mcp.json", CODEX_MCP)
    dump(codex / ".app.json", CODEX_APPS)
    (codex / "assets").mkdir(parents=True, exist_ok=True)
    (codex / "assets" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    # Codex legacy plugin WITH hooks: Codex supports them and defaults to
    # hooks/hooks.json, and it runs them with PLUGIN_ROOT in the environment.
    codex_hooks = root / "codex-hooks"
    dump(
        codex_hooks / ".codex-plugin" / "plugin.json",
        {"name": "codex-hooks", "version": "1.0.0", "description": "Codex hooks"},
    )
    dump(
        codex_hooks / "hooks" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "shell",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"$PLUGIN_ROOT/hooks/guard.sh"',
                            }
                        ],
                    }
                ]
            }
        },
    )
    text(codex_hooks / "hooks" / "guard.sh", "#!/bin/sh\nexit 0\n", executable=True)

    # Agent Plugins v1: the portable manifest is a ROOT plugin.json carrying a
    # $schema, component defaults are ./skills and ./mcp.json (no dot).
    agent_plugins = root / "agent-plugins-demo"
    dump(
        agent_plugins / "plugin.json",
        {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": "agent.plugins.demo",  # Codex plugin names may contain dots
            "version": "1.0.0",
            "description": "Portable Agent Plugins v1 layout",
            "author": {"name": "Example"},
        },
    )
    text(
        agent_plugins / "skills" / "portable" / "SKILL.md",
        _skill("portable", "A skill from an Agent Plugins v1 package."),
    )
    dump(
        agent_plugins / "mcp.json",
        {"mcpServers": {"portable-mcp": {"command": "python3", "args": ["-c", "print(1)"]}}},
    )

    # ── DeepSeek Harness ────────────────────────────────────────────────────
    dsh = root / "dsh-demo"
    dump(dsh / "package.json", DSH_PACKAGE)
    text(dsh / "lib" / "index.js", DSH_ENTRY)
    text(dsh / "cordis.patch.yml", DSH_CORDIS_ENTRY)
    text(dsh / "README.md", "# @acme/dsh-demo\n\nInstall: pnpm add @acme/dsh-demo\n")

    # ── marketplaces ────────────────────────────────────────────────────────
    cc_market = root / "cc-market"
    dump(cc_market / ".claude-plugin" / "marketplace.json", CC_MARKETPLACE)
    dump(
        cc_market / "plugins" / "alpha" / ".claude-plugin" / "plugin.json",
        {"name": "alpha", "version": "0.1.0", "description": "marketplace sub-plugin"},
    )
    text(
        cc_market / "plugins" / "alpha" / "skills" / "alpha" / "SKILL.md",
        _skill("alpha", "A skill that arrived through Claude Code's marketplace."),
    )

    codex_market = root / "codex-market"
    dump(codex_market / ".agents" / "plugins" / "marketplace.json", CODEX_MARKETPLACE)
    dump(
        codex_market / "plugins" / "beta" / ".codex-plugin" / "plugin.json",
        {"name": "beta", "version": "0.1.0", "skills": "./skills"},
    )
    text(
        codex_market / "plugins" / "beta" / "skills" / "beta" / "SKILL.md",
        _skill("beta", "A skill that arrived through Codex's marketplace."),
    )

    # The same marketplace content at the root path this project used to look
    # for only.  A different plugin name on purpose: two fixtures declaring the
    # same plugin name make the loader's name-keyed bookkeeping attribute one of
    # them to the other, which reads as a failure that is not there.
    codex_root_market = root / "codex-market-at-root"
    root_marketplace = dict(CODEX_MARKETPLACE)
    root_marketplace["name"] = "root-market"
    root_marketplace["plugins"] = [
        {
            "name": "gamma",
            "source": {"source": "local", "path": "./plugins/gamma"},
            "policy": {"installation": "AVAILABLE"},
        }
    ]
    dump(codex_root_market / "marketplace.json", root_marketplace)
    dump(
        codex_root_market / "plugins" / "gamma" / ".codex-plugin" / "plugin.json",
        {"name": "gamma", "version": "0.1.0", "skills": "./skills"},
    )
    text(
        codex_root_market / "plugins" / "gamma" / "skills" / "gamma" / "SKILL.md",
        _skill("gamma", "A skill that arrived through a root marketplace.json."),
    )


# ── measurements ─────────────────────────────────────────────────────────────


def measure_load(root: Path) -> list[dict[str, Any]]:
    """Discover and load *root* with the project's own loader; report per fixture."""
    from agent import PluginCatalog

    catalog = PluginCatalog(builtin_dir=root)
    loaded = catalog.discover_and_load()

    def _fireable(event_name: str) -> bool:
        # A command hook runs only where a dispatcher exists; ask the catalog
        # rather than duplicating the list that tests/test_plugin_hook_dispatch
        # owns.
        return hasattr(catalog, "fire_" + event_name.removeprefix("on_"))

    skills: dict[str, list[str]] = {}
    for plugin_name, skills_root in catalog.get_bundled_skills():
        found: list[str] = []
        if skills_root.is_dir():
            found = sorted(
                child.name
                for child in skills_root.iterdir()
                if (child / "SKILL.md").is_file()
            )
            if not found and (skills_root / "SKILL.md").is_file():
                found = [skills_root.name]
        skills.setdefault(plugin_name, []).extend(found)

    owners = catalog._commands.owners()  # {command_key: plugin_name}
    agents = catalog.list_agent_definitions()
    mcp = catalog.get_bundled_mcp()
    metas = {str(meta.name): meta for meta in catalog.list_plugins()}

    rows: list[dict[str, Any]] = []
    for fixture in sorted(p.name for p in root.iterdir() if p.is_dir()):
        names = sorted(catalog.get_loaded_names_for_directory(fixture))
        plugins: list[dict[str, Any]] = []
        for name in names:
            meta = metas.get(name)
            hooks = {
                event: ("runs" if _fireable(event) else "inert")
                for event in sorted(meta.hooks_config)
            } if meta is not None else {}
            plugins.append(
                {
                    "name": name,
                    "version": meta.version if meta is not None else "",
                    "skills": sorted(skills.get(name, [])),
                    "commands": sorted(k for k, owner in owners.items() if owner == name),
                    "agents": sorted(
                        str(item.get("key", ""))
                        for item in agents
                        if str(item.get("plugin", "")) == name
                    ),
                    "mcp": sorted(
                        str(cfg.get("name", "")) for owner, cfg in mcp if owner == name
                    ),
                    "hooks": hooks,
                }
            )
        rows.append({"fixture": fixture, "plugins": plugins})
    return rows, catalog, loaded


async def measure_install(root: Path, home: Path) -> list[dict[str, Any]]:
    """Install each fixture through the real ``install_plugin`` tool."""
    from agent import shared as _shared
    from agent.tools.builtin_tools import BuiltinTools
    from agent.tools.runtime import ToolRegistry

    previous = _shared.USER_PLUGINS_DIR
    installed_dir = home / "plugins"
    # The gateway phase copies the fixtures in beside whatever the install phase
    # left, so start from an empty directory.
    shutil.rmtree(installed_dir, ignore_errors=True)
    _shared.USER_PLUGINS_DIR = installed_dir
    rows: list[dict[str, Any]] = []
    try:
        from agent import PluginCatalog

        registry = ToolRegistry()
        tools = BuiltinTools(memory=None, registry=registry)
        # The install path reloads through the tool's registry contexts; without
        # them the reload reports "activation failed" for a plugin that loads
        # perfectly well on its own (which is how a probe gets a false finding).
        catalog = PluginCatalog(builtin_dir=home / "empty", user_dir=installed_dir)
        catalog.discover_and_load()
        registry.set_context("plugin_catalog", catalog)
        registry.set_context("components", {"plugin_catalog": catalog})
        for fixture in sorted(p.name for p in root.iterdir() if p.is_dir()):
            result = await tools._plugin_tools.install_plugin(str(root / fixture))
            rows.append(
                {
                    "fixture": fixture,
                    "ok": bool(result.get("ok")),
                    "error": str(result.get("error", ""))[:70],
                    "cancelled": bool(result.get("cancelled")),
                    "requires_confirmation": bool(result.get("requires_confirmation")),
                    "installed": (installed_dir / fixture).is_dir(),
                }
            )
    finally:
        _shared.USER_PLUGINS_DIR = previous
    return rows


async def measure_gateway(home: Path, port: int) -> dict[str, Any]:
    """What a browser sees once the fixtures are in the user's plugin directory."""
    import httpx

    from scripts.functional_smoke import start_gateway, wait_for_health  # type: ignore

    base = f"http://127.0.0.1:{port}"
    process = start_gateway(home, port)
    try:
        await wait_for_health(base, process)
        async with httpx.AsyncClient(timeout=20) as client:
            plugins = (await client.get(f"{base}/api/plugins")).json().get("plugins", [])
            skills = (await client.get(f"{base}/api/skills")).json().get("skills", [])
            commands = (await client.get(f"{base}/api/commands")).json().get("commands", [])
        return {
            "plugins": sorted(str(item.get("name", "")) for item in plugins),
            "skills": sorted(
                str(item.get("id") or item.get("name") or "") for item in skills
            ),
            "commands": sorted(str(item.get("name", "")) for item in commands),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-home", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    from scripts.functional_smoke import make_scratch_home  # type: ignore

    root = Path(tempfile.mkdtemp(prefix="plugin-fixtures-"))
    # A bare temp directory is not an agent home: the gateway needs the config a
    # home carries -- including the web port it will listen on, which is why the
    # port is kept here rather than drawn again for the health check.
    port = free_port()
    home, _ = make_scratch_home(port)
    write_fixtures(root)

    print(f"fixtures: {root}")
    print(f"agent home: {home}")

    print("\n== load: what the project's own loader registers ==")
    rows, catalog, loaded = measure_load(root)
    for row in rows:
        if not row["plugins"]:
            if ECOSYSTEM.get(row["fixture"]) == "dsh":
                print(f"  {SKIP} {row['fixture']:<20} {DSH_NOTE}")
            else:
                print(f"  {FAIL} {row['fixture']:<20} not recognised as a plugin")
                failures.append(f"{row['fixture']}: not recognised")
            continue
        for plugin in row["plugins"]:
            hooks = ", ".join(f"{k}:{v}" for k, v in plugin["hooks"].items()) or "-"
            print(
                f"  {OK} {row['fixture']:<20} -> {plugin['name']:<20} "
                f"skills={plugin['skills'] or '-'} "
                f"commands={plugin['commands'] or '-'} "
                f"agents={plugin['agents'] or '-'} "
                f"mcp={plugin['mcp'] or '-'} hooks={hooks}"
            )

    print("\n== hook environment: what a declared hook command is given ==")
    for row in rows:
        for plugin in row["plugins"]:
            if not plugin["hooks"]:
                continue
            meta = next((m for m in catalog.list_plugins() if m.name == plugin["name"]), None)
            env = catalog._hook_env(meta, {}) if meta is not None else {}
            wanted = ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA")
            missing = [key for key in wanted if key not in env]
            status = OK if not missing else FAIL
            if missing:
                failures.append(
                    f"{plugin['name']}: hook env missing {', '.join(missing)}"
                )
            print(
                f"  {status} {plugin['name']:<20} "
                f"has={[k for k in wanted if k in env]} missing={missing or '-'}"
            )

    print("\n== install: the real install_plugin tool ==")
    for row in asyncio.run(measure_install(root, home)):
        if row["ok"]:
            status = OK
        elif row["cancelled"]:
            status = OK  # refused for consent, which is the designed behaviour
        elif ECOSYSTEM.get(row["fixture"]) == "dsh":
            status = SKIP
        else:
            status = FAIL
            failures.append(f"{row['fixture']}: install failed ({row['error']})")
        detail = "installed" if row["ok"] else (
            f"needs human consent ({'pending' if row['requires_confirmation'] else 'no record'})"
            if row["cancelled"] else (DSH_NOTE if status == SKIP else row["error"])
        )
        executable = EXECUTABLE.get(row["fixture"])
        if row["ok"] and executable:
            # Installed without asking, for something its own ecosystem defines
            # as executable: the consent gate missed the location it ships.
            failures.append(f"{row['fixture']}: executable content installed without consent")
            status = FAIL
            detail += " -- BUT it ships executable content"
        print(f"  {status} {row['fixture']:<22} {detail}")

    print("\n== gateway: the surface a reader sees (fixtures in <home>/plugins) ==")
    try:
        for fixture in sorted(p.name for p in root.iterdir() if p.is_dir()):
            shutil.copytree(root / fixture, home / "plugins" / fixture, dirs_exist_ok=True)
        seen = asyncio.run(measure_gateway(home, port))
        print(f"  plugins:  {seen['plugins'] or '-'}")
        print(f"  commands: {[c for c in seen['commands'] if ':' in c] or '-'}")
        print(f"  skills:   {seen['skills'] or '-'}")
        if not any(name.startswith(("cc-", "codex-", "alpha", "beta")) for name in seen["plugins"]):
            failures.append("gateway: no fixture plugin listed")
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"  {SKIP} gateway check: {type(exc).__name__}: {str(exc)[:80]}")

    print("\n== DSH (read-only): what a real profile declares ==")
    try:
        report_dsh_profile()
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"  {SKIP} DSH profile: {type(exc).__name__}: {str(exc)[:80]}")

    print("\ncaveats (not failures, but they bound the numbers above)")
    print("  - Codex has no agent or slash-command concept: those files are a")
    print("    Claude Code convention it does not read (Codex migrates commands/")
    print("    into skills at install time)")
    print("  - a DSH plugin is an npm/Cordis package: only its MCP declarations")
    print("    and skill roots have a counterpart here, read-only; the code cannot run")
    print("  - hook events with no dispatcher are reported as 'inert'")

    if not args.keep_home:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    else:
        print(f"\nkept: {root} {home}")

    print(f"\n{len(failures)} failure(s)")
    for item in failures:
        print(f"  {FAIL} {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
