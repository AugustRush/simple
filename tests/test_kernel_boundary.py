"""The kernel's boundary, asserted structurally rather than described.

The claim -- "no privileged core: the kernel knows Context, Plugin, Service, Event, Scope
and Lifecycle, and nothing about agents or tools" -- has been in the README and the status
document since Phase 1.  It had no test.  A documented property with no guard is one that
becomes false quietly, and this one is the whole point of the design: the moment the kernel
can name ``Agent``, that name is privileged, because the kernel is what everything else is
composed *by*.

Two rules, because they fail differently.  The first is structural and exact: the kernel
imports nothing from the rest of the project.  The second is a vocabulary check, which is
weaker on its own -- a name could be spelled indirectly -- but catches the obvious route,
and it is the route a person actually takes.
"""

from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KERNEL = REPO_ROOT / "agent" / "kernel"

#: Things the kernel is *for*.  Anything else here would be a participant with a
#: privileged name, which is the thing the design exists to avoid.
_KERNEL_VOCABULARY = {
    "context",
    "plugin",
    "pluginhost",
    "service",
    "serviceregistry",
    "servicescope",
    "event",
    "eventbus",
    "eventtype",
    "scope",
    "install",
    "lifecycle",
    "dispose",
    "unwind",
    "unload",
    "load",
    "inject",
    "provide",
    "pending",
    "host",
}

#: Domain concepts.  A kernel that names one has made it privileged.
_FORBIDDEN = (
    "agent",
    "tool",
    "session",
    "memory",
    "scheduler",
    "channel",
    "runtime",
    "model",
    "llm",
    "prompt",
    "skill",
    "command",
    "ralph",
    "orchestration",
)


def _stdlib_names(tree: ast.AST) -> set[str]:
    """Names that came from outside the project.

    ``runtime_checkable`` is a ``typing`` decorator, and no rule over identifiers alone can
    tell it from a project concept spelled the same way.  Rather than loosening the rule --
    which would also stop catching a real ``runtime`` -- the *provenance* is used: a name
    imported from a module that is not ``agent`` is not this project's vocabulary.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "agent":
                continue
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] != "agent":
                    names.add(alias.asname or alias.name.split(".")[0])
    return names


def _words(name: str) -> list[str]:
    """The parts of an identifier, splitting underscores *and* CamelCase.

    The first version of this rule compared whole identifiers and missed
    ``ToolRegistryProxy``: a compound name does not equal, start with ``tool_``, or end
    with ``_tool``, so the check passed on exactly the spelling a domain concept would
    arrive in.  A blind spot is worse than an omission -- an omission gets fixed, and a
    blind spot is believed to be covered.
    """
    import re

    parts: list[str] = []
    for chunk in name.split("_"):
        parts.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+", chunk))
    return [part.lower() for part in parts if part]


def _identifiers(tree: ast.AST) -> list[tuple[str, int]]:
    """Every name the module *uses*: definitions, references, attributes, arguments.

    Not docstrings or comments -- those are prose about the kernel, and the module
    docstrings argue *why* these words are absent, which requires writing them down.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            found.append((node.attr, node.lineno))
        elif isinstance(node, ast.arg):
            found.append((node.arg, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, node.lineno))
    return found


def _kernel_modules() -> list[pathlib.Path]:
    modules = sorted(KERNEL.glob("*.py"))
    assert modules, "the scan must find the kernel"
    return modules


def test_the_kernel_imports_nothing_from_the_rest_of_the_project() -> None:
    """The exact rule.  ``agent.kernel`` may use itself, the stdlib and ``typing``.

    Structurally checkable with no vocabulary list and no judgement: an import of
    ``agent.core.agent`` or ``agent.tools.runtime`` would be the kernel reaching for a
    participant, whichever words it happened to use.
    """
    offenders = []
    for path in _kernel_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                if module.split(".")[0] == "agent" and not module.startswith("agent.kernel"):
                    offenders.append(f"{path.name}:{node.lineno} imports {module}")
    assert not offenders, (
        "the kernel must not import the rest of the project:\n  " + "\n  ".join(offenders)
    )


def test_the_kernel_names_no_domain_concept() -> None:
    """No participant gets a privileged name."""
    offenders = []
    for path in _kernel_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        from_elsewhere = _stdlib_names(tree)
        for name, lineno in _identifiers(tree):
            # Provenance again: `RuntimeError` is Python's, and `_words` splits it into
            # "runtime" and "error" the same way it splits a project concept.  A name that
            # *is* a builtin cannot be this project's vocabulary.
            if name in from_elsewhere or hasattr(builtins, name):
                continue
            for word in _words(name):
                if word in _FORBIDDEN:
                    offenders.append(f"{path.name}:{lineno} {name}")
                    break
    assert not offenders, (
        "the kernel named a domain concept; that name is now privileged:\n  "
        + "\n  ".join(offenders)
    )


def test_the_vocabulary_rule_is_not_vacuous() -> None:
    """Control: a bare name *and* a compound one must both be caught.

    The compound case is the one the first version of this rule missed, so it is asserted
    explicitly rather than trusted.
    """
    for source, expected in (
        ("class Agent:\n    pass\n", "Agent"),
        ("class ToolRegistryProxy:\n    pass\n", "ToolRegistryProxy"),
        ("def session_state():\n    pass\n", "session_state"),
        ("value = agent_context\n", "agent_context"),
    ):
        found = [name for name, _ in _identifiers(ast.parse(source)) if _words(name) and set(_words(name)) & set(_FORBIDDEN)]
        assert found == [expected], (source, found)


def test_the_import_rule_is_not_vacuous() -> None:
    """Control: an import of a domain module is caught, and a kernel one is not."""
    def offenders(source: str) -> list[str]:
        out = []
        for node in ast.walk(ast.parse(source)):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            for module in modules:
                if module.split(".")[0] == "agent" and not module.startswith("agent.kernel"):
                    out.append(module)
        return out

    assert offenders("from agent.core.agent import BaseAgent") == ["agent.core.agent"]
    assert offenders("from agent.kernel.service import ServiceRegistry") == []
    assert offenders("import agent.tools.runtime") == ["agent.tools.runtime"]
