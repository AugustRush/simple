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
            if node.level:
                # Relative: the names came from inside this package.  Classifying
                # them as external is how the identifier rule went blind to the
                # same relative import the module rule used to miss.
                continue
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


def _import_offenders(tree: ast.AST, filename: str = "module.py") -> list[str]:
    """Every import in *tree* that reaches outside ``agent.kernel``.

    One function, used by both the rule and its control.  When the control restated
    the rule it could not catch the rule's own blind spot -- which is exactly what
    happened: ``from ..core.agent import X`` parses as ``module="core.agent",
    level=2``, so a rule that only looked for a leading ``agent`` passed it, and a
    control written the same way passed it too.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom):
            # A relative import resolves against the *kernel package*, so the module
            # name alone cannot be judged.  The level decides: one dot stays inside
            # ``agent.kernel``; two or more leave it.
            if node.level and node.level > 1:
                offenders.append(
                    f"{filename}:{node.lineno} relative import escapes the kernel "
                    f"(level={node.level}, module={node.module!r})"
                )
                continue
            if node.level == 1:
                continue
            if node.module:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            if module.split(".")[0] == "agent" and not module.startswith("agent.kernel"):
                offenders.append(f"{filename}:{node.lineno} imports {module}")
    return offenders


def test_the_kernel_imports_nothing_from_the_rest_of_the_project() -> None:
    """The exact rule.  ``agent.kernel`` may use itself, the stdlib and ``typing``.

    Structurally checkable with no vocabulary list and no judgement: an import of
    ``agent.core.agent`` or ``agent.tools.runtime`` would be the kernel reaching for a
    participant, whichever words it happened to use.
    """
    offenders: list[str] = []
    for path in _kernel_modules():
        offenders.extend(
            _import_offenders(
                ast.parse(path.read_text(encoding="utf-8")), path.name
            )
        )
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
    """Control: the rule is exercised through the function the rule itself uses.

    Including the relative form, which is the case the first version missed: a
    ``from ..core.agent import X`` inside ``agent/kernel/`` reaches a participant
    while its parsed module name starts with "core", not "agent".
    """
    def offenders(source: str) -> list[str]:
        return _import_offenders(ast.parse(source))

    assert any("agent.core.agent" in o for o in offenders("from agent.core.agent import BaseAgent"))
    assert offenders("from agent.kernel.service import ServiceRegistry") == []
    assert any("agent.tools.runtime" in o for o in offenders("import agent.tools.runtime"))
    # Inside the package: one dot is fine.
    assert offenders("from .service import ServiceRegistry") == []
    assert offenders("from . import service") == []
    # Out of it: two dots reach ``agent``, and then a participant.
    escaped = offenders("from ..core.agent import BaseAgent")
    assert escaped and "escapes the kernel" in escaped[0], escaped
