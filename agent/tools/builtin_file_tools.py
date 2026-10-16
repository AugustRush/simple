"""Files, shell and audio: the tools that read, write, move and transcribe things on the user's machine. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

def register_file_tools(
    r,
    *,
    current_time, shell_tools, file_tools, audio_tools,
) -> None:
        r.register(
            "current_time",
            "Get the current local or requested timezone time as structured data. Use when the user asks about now, today, current date, or current time.",
            {
                "type": "object",
                "properties": {
                    "timezone_name": {
                        "type": "string",
                        "description": "IANA timezone name like 'Asia/Shanghai'. Default: local system timezone.",
                        "default": "local",
                    },
                },
                "required": [],
            },
            current_time,
            source="builtin",
        )
    
        r.register(
            "shell",
            "Execute a shell command and return stdout/stderr. Use for system operations, running scripts, etc. "
            "Medium-risk commands (ssh/curl/rm/interpreters and similar) run automatically. High-risk commands "
            "(disk/system destruction, destructive options, shell operators, pipe-to-shell patterns) require "
            "human approval: an interactive terminal shows an approval menu automatically; in non-terminal "
            "channels, show the exact command to the user and ask them to reply “同意” (or run /confirm <token>), "
            "then retry the identical command string after approval.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "intent": {
                        "type": "string",
                        "description": "Required. Explain what this exact command will do and why running it is necessary for the user's task.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 300, max 3600). Use 600+ for large downloads or long builds.",
                        "default": 300,
                    },
                    "root": {
                        "type": "string",
                        "enum": ["output_dir", "workspace"],
                        "default": "workspace",
                        "description": "Security domain for this shell call. workspace (default) is the selected project directory; output_dir is for generated deliverables, downloads, and temporary files.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory. Relative paths resolve inside root (default: the root itself). Absolute paths are accepted only when inside the workspace or output directory.",
                    },
                },
                "required": ["command", "intent"],
            },
            shell_tools.run,
            source="builtin",
        )
    
        r.register(
            "read_file",
            "Read a bounded line window of a UTF-8 text file. Returns an exact sha256 revision of the file; pass that revision back as expected_revision to write_file or edit_file before mutating. Use root=workspace for repository files and root=output_dir for generated artifacts.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain (default workspace)",
                        "default": "workspace",
                    },
                    "path": {
                        "type": "string",
                        "description": "Root-relative file path",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "One-based first line to return (default 1)",
                        "default": 1,
                    },
                    "line_count": {
                        "type": "integer",
                        "description": "Maximum number of complete lines to return (default 200)",
                        "default": 200,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            file_tools.read_file,
            source="builtin",
        )
    
        r.register(
            "write_file",
            "Atomically create or overwrite a whole UTF-8 text file under an explicit root. mode=create requires the target to be absent; mode=overwrite requires the exact expected_revision from read_file and preserves the file mode and UTF-8 BOM.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain: workspace requires startup write enablement plus write_scope; output_dir is always writable",
                    },
                    "path": {"type": "string", "description": "File path"},
                    "mode": {
                        "type": "string",
                        "enum": ["create", "overwrite"],
                        "description": "create fails if the target exists; overwrite requires expected_revision",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "expected_revision": {
                        "type": "string",
                        "description": "Required for overwrite: the revision returned by a previous read_file",
                    },
                },
                "required": ["root", "path", "mode", "content"],
                "additionalProperties": False,
            },
            file_tools.write_file,
            authorizer=file_tools.file_mutation_authorizer,
            source="builtin",
        )
    
        r.register(
            "send_file",
            "Queue an existing file to be sent back to the current user/channel when the turn completes. Use after generating or locating a file the user asked to receive. For generated images, queue each final deliverable once and omit source grids, previews, contact sheets, and other intermediate files unless the user explicitly requests them.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative file path within the workspace or output directory",
                    }
                },
                "required": ["path"],
            },
            file_tools.send_file,
            source="builtin",
        )
    
        r.register(
            "transcribe_audio",
            "Transcribe an audio file to text using the configured local speech-to-text command. Use this for audio attachments; do not use read_file on audio files.",
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to an audio file within the workspace or output directory",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional language hint such as zh, en, ja, or ko",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 300,
                    },
                },
                "required": ["path"],
            },
            audio_tools.transcribe,
            source="builtin",
        )
    
        r.register(
            "list_files",
            "List entries under an explicit root with bounded, deterministic results and an opaque cursor for continuation. Never follows symlinks.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain (default workspace)",
                        "default": "workspace",
                    },
                    "path": {
                        "type": "string",
                        "description": "Root-relative directory path (default: .)",
                        "default": ".",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Basename glob pattern with no path separator (default: *)",
                        "default": "*",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to recurse into subdirectories",
                        "default": False,
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque continuation token from a previous truncated listing; must be used with identical request parameters",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of paths to return",
                        "default": 1000,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            file_tools.list_files,
            source="builtin",
        )
    
        r.register(
            "edit_file",
            "Apply an ordered batch of exact, non-overlapping text replacements to one snapshot revision. Every replacement must declare the exact expected occurrence count; any mismatch leaves the file byte-for-byte unchanged. Preserves the original BOM, mode, and all bytes outside replaced spans.",
            {
                "type": "object",
                "properties": {
                    "root": {
                        "type": "string",
                        "enum": ["workspace", "output_dir"],
                        "description": "Security domain: workspace requires startup write enablement plus write_scope; output_dir is always writable",
                    },
                    "path": {"type": "string", "description": "Root-relative file path"},
                    "expected_revision": {
                        "type": "string",
                        "description": "Exact revision returned by a previous read_file",
                    },
                    "replacements": {
                        "type": "array",
                        "description": "Ordered exact replacements; each later replacement may match text inserted by an earlier one",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                                "expected_count": {"type": "integer"},
                            },
                            "required": ["old_text", "new_text", "expected_count"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["root", "path", "expected_revision", "replacements"],
                "additionalProperties": False,
            },
            file_tools.edit_file,
            authorizer=file_tools.file_mutation_authorizer,
            source="builtin",
        )
