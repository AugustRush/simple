"""Memory and context: what the agent writes down, what it reads back, and the retrieval it can ask for by name. Split out of BuiltinTools._register (see the remaining-big-files survey, section 2c)."""

from __future__ import annotations

def register_memory_tools(
    r,
    *,
    memory_tools, context_tools,
) -> None:
        r.register(
            "memory_write",
            "Write or append content to the memory palace.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Palace locus or legacy alias",
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "append": {
                        "type": "boolean",
                        "description": "Append instead of overwrite",
                        "default": False,
                    },
                },
                "required": ["chapter", "name", "content"],
            },
            memory_tools.write,
            source="builtin",
        )
    
        r.register(
            "set_identity",
            "Set who the assistant is (or record who the user is). Use this "
            "whenever the user names, renames, or redefines the persona — the "
            "newest setting replaces the old one.",
            {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "enum": ["assistant", "user"],
                        "description": "Whose identity this is. Defaults to assistant.",
                        "default": "assistant",
                    },
                    "name": {
                        "type": "string",
                        "description": "What they are called. Omit to leave unchanged.",
                    },
                    "role": {
                        "type": "string",
                        "description": "Their role, e.g. '编程助手'. Omit to leave unchanged.",
                    },
                    "persona": {
                        "type": "string",
                        "description": (
                            "Free-text persona/setting in the user's own wording, "
                            "e.g. '一位可爱的小女孩'. Omit to leave unchanged."
                        ),
                    },
                },
                "required": [],
            },
            memory_tools.set_identity,
            source="builtin",
        )
    
        r.register(
            "memory_read",
            "Read a memory chapter file.",
            {
                "type": "object",
                "properties": {
                    "chapter": {
                        "type": "string",
                        "description": "Palace locus or legacy alias",
                    },
                    "name": {
                        "type": "string",
                        "description": "File name (without .md)",
                    },
                },
                "required": ["chapter", "name"],
            },
            memory_tools.read,
            source="builtin",
        )
    
        r.register(
            "memory_search",
            "Search across all memory files.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            memory_tools.search,
            source="builtin",
        )
    
        r.register(
            "memory_index",
            "Show the memory palace index.",
            {"type": "object", "properties": {}, "required": []},
            memory_tools.index,
            source="builtin",
        )
    
        r.register(
            "memory_clear",
            (
                "Permanently clear all durable long-term memory, retrievable "
                "conversation history, facts, working-state snapshots, and pending "
                "memory consolidation. Use when the user asks to clear/erase/forget "
                "all memory. This tool asks the human for interactive approval itself; "
                "do not use shell/SQLite and do not ask for a separate chat confirmation."
            ),
            {"type": "object", "properties": {}, "required": []},
            memory_tools.clear,
            source="builtin",
        )
    
        r.register(
            "context_retrieve",
            (
                "Search long-term context memory for relevant information. "
                "Use to recall past facts, user preferences, project context, "
                "or any information consolidated from previous sessions. "
                "ALSO use it when the context already injected into this turn "
                "does not contain what you need — do not tell the user you "
                "have no record until you have searched.\n"
                "The index is LEXICAL, not semantic: it finds memories that "
                "share WORDS with your query, not ones that merely mean the "
                "same thing. So pass SEVERAL phrasings in `queries`, not just "
                "the user's question verbatim:\n"
                "- the other language (a memory written in English will not "
                "match a Chinese question, and vice versa)\n"
                "- the concrete words the memory itself would use "
                "(for 'which company do I work at', try the employer's name)\n"
                "- distinctive nouns rather than the question's phrasing"
            ),
            {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Two to four different phrasings of what you are "
                            "looking for. More phrasings find more; they are "
                            "unioned, so a wrong guess costs nothing."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Single query (legacy). Prefer `queries` — one "
                            "phrasing is what makes lexical search miss."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": [],
            },
            context_tools.retrieve,
            source="builtin",
        )
