"""Installing plugins and authoring tools: the tools that change what this runtime can do. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

def register_plugin_tools(
    r,
    *,
    plugin_tools,
) -> None:
        r.register(
            "install_plugin",
            (
                "Install a plugin from a git URL or local path into ~/.agent/plugins/<name>/. "
                "Supports Claude Code / Codex layouts (with .claude-plugin/plugin.json) and "
                "marketplaces (a repo with .claude-plugin/marketplace.json listing multiple "
                "plugins).  Hot-reloads after install so the plugin's skills, commands, agents, "
                "MCP servers and hooks become available immediately without restart. "
                "Plugins with executable content (Python entry, MCP server, hooks) require "
                "human confirmation before activation; in non-terminal channels ask the "
                "user to reply 同意, then retry the identical source."
            ),
            {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "Git URL (https://, git@) or absolute local path. "
                            "git:// is normalized to https automatically."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional target directory name. Default: derived from source.",
                    },
                    "replace": {
                        "type": "boolean",
                        "description": (
                            "Replace an already-installed plugin with the same name "
                            "(upgrade). Default: false."
                        ),
                        "default": False,
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this plugin is being installed.",
                    },
                },
                "required": ["source", "intent"],
            },
            plugin_tools.install_plugin,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "uninstall_plugin",
            (
                "Remove a user-installed plugin directory and hot-reload the catalog. "
                "Note: MCP server subprocesses connected by the plugin remain running until "
                "the next agent restart; only their tool registrations are dropped."
            ),
            {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Plugin directory name under ~/.agent/plugins/.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this plugin is being removed.",
                    },
                },
                "required": ["name", "intent"],
            },
            plugin_tools.uninstall_plugin,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "list_installed_plugins",
            (
                "List all plugins installed under ~/.agent/plugins/ and which are currently "
                "loaded.  Use to verify install state or inspect available extensions."
            ),
            {"type": "object", "properties": {}, "required": []},
            plugin_tools.list_installed_plugins,
            source="builtin",
        )
    
        # ── User tool authoring ──────────────────────────────────────────
        #
        # This is the supported way to answer "build me a tool that ...".
        # Writing a .py file with write_file/shell does NOT create a tool,
        # and installing packages with pip/uv from the shell would mutate
        # the user's project — use install_tool_dependency instead.
    
        r.register(
            "create_tool",
            (
                "Create a new reusable user tool from Python source and activate it for "
                "this and future sessions. USE THIS whenever the user asks to build, add, "
                "or generate a tool/capability — never hand-write a .py file into the "
                "tools directory and never pip-install from the shell. The module must "
                "define `register(registry)` that calls "
                "`registry.register(name, description, json_schema, async_fn)` for each "
                "tool it provides. The source is syntax-checked, then imported in a "
                "separate process to prove it loads, then shown to the user for approval "
                "before anything runs in the agent. Third-party imports must be installed "
                "first with install_tool_dependency."
            ),
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": (
                            "Module id under ~/.agent/tools (lowercase letters, digits, "
                            "underscores), e.g. 'markdown_to_html'."
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Complete Python module source defining register(registry). "
                            "Tool functions must be async and accept keyword arguments "
                            "matching their JSON schema."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. What this tool does and why the user needs it.",
                    },
                },
                "required": ["tool_id", "code", "intent"],
            },
            plugin_tools.create_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "update_tool",
            (
                "Replace the source of an existing user tool and reactivate it. Same "
                "validation and approval path as create_tool; because approval is bound "
                "to the file's contents, a changed tool is re-confirmed."
            ),
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "Existing tool module id under ~/.agent/tools.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Complete replacement module source defining register(registry).",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. What is changing and why.",
                    },
                },
                "required": ["tool_id", "code", "intent"],
            },
            plugin_tools.update_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "delete_tool",
            "Delete a user tool, revoke its approval, and unload it from the session.",
            {
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "Tool module id under ~/.agent/tools.",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Why this tool is being removed.",
                    },
                },
                "required": ["tool_id", "intent"],
            },
            plugin_tools.delete_tool,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
    
        r.register(
            "list_tools",
            (
                "List user tools on disk with their approval and load state, plus the "
                "packages installed in the isolated tool dependency directory. Use before "
                "creating a tool to check whether one already exists."
            ),
            {"type": "object", "properties": {}, "required": []},
            plugin_tools.list_user_tools,
            source="builtin",
        )
    
        r.register(
            "install_tool_dependency",
            (
                "Install a Python package that a user tool needs. The package goes into "
                "the agent's isolated dependency directory (~/.agent/tools/_deps) and is "
                "importable by user tools. This is the ONLY supported way to add a Python "
                "dependency: running pip/uv/poetry from the shell would modify the "
                "user's project or the ambient interpreter and is blocked."
            ),
            {
                "type": "object",
                "properties": {
                    "package": {
                        "type": "string",
                        "description": (
                            "Distribution name with optional extras and one version "
                            "constraint, e.g. 'markdown', 'httpx[http2]', 'markdown>=3.5'."
                        ),
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Which tool needs this package and why.",
                    },
                },
                "required": ["package", "intent"],
            },
            plugin_tools.install_tool_dependency,
            source="builtin",
            capabilities=("state_write", "requires_intent"),
        )
