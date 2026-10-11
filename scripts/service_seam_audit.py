#!/usr/bin/env python3
"""What would a replacement for this service have to implement?

Why this exists
---------------
The composition resolves services by name (``resolve(components, "ltm_store")``,
``ctx.get("memory")``) and a profile can *declare* that a service comes from a
different plugin (``service_replacements``).  What that declaration cannot tell
you is whether the replacement is **possible**: it says nothing about the
interface the consumers actually use.  A protocol nobody has written down is
whatever the call sites happen to touch, and the honest way to find that out is
to read the call sites.

So this walks the real source and answers, for each named service:

* **providers** -- who calls ``provide("<name>", ...)``, and the class it hands over
* **de facto interface** -- every member the consumers actually touch
* **concrete surface** -- every public member the class offers
* **burden** -- the first two numbers, side by side.  A seam whose used set is
  much smaller than its surface is replaceable *if* the used set is what gets
  written down as the protocol; one whose used set is as large as the surface is
  not a seam at all, it is a class with a name.

Usage
-----
    uv run python scripts/service_seam_audit.py                    # every core service
    uv run python scripts/service_seam_audit.py ltm_store memory   # named ones
    uv run python scripts/service_seam_audit.py --json             # machine-readable

What it cannot see
------------------
Members are attributed to a service by the name of the variable that received
it, and by construction that cannot tell two objects with the same variable name
apart: a scheduler store bound as ``store`` is counted against ``ltm_store``
too.  So the *used members* list is a lead, not a verdict -- the counts that
hold up are the per-method, cross-package ones (``.<method>(`` outside the
providing package), which is the second thing to run when deciding whether a
service is replaceable:

    grep -rn "\.record_turn\s*(" agent --include=*.py | grep -v agent/memory/

A service consumed only through method names that also exist elsewhere needs
that check before its number is quoted.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: Expressions that hand a consumer a service, by the callee's attribute name.
#: ``ctx.get("x")`` and ``resolve(mapping, "x")`` are the two ways this codebase
#: takes a service; ``require`` is the raising spelling of the second.
_ACCESSORS = {"get", "resolve", "require", "get_service"}


def _string_arg(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _service_name_of(call: ast.Call) -> str:
    """The service name a call reads or writes, or "" when it is neither."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else ""
    )
    if name not in _ACCESSORS and name != "provide":
        return ""
    if not call.args:
        return ""
    last = call.args[-1]
    if name == "provide":
        return _string_arg(call.args[0])
    for arg in call.args:
        found = _string_arg(arg)
        if found:
            return found
    return ""


def _calls_in(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _class_surface(node: ast.ClassDef) -> dict[str, Any]:
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not item.name.startswith("_")
    ]
    dunder = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name in {"__init__", "__enter__", "__exit__", "__iter__"}
    ]
    return {"name": node.name, "methods": sorted(methods), "dunder": sorted(dunder)}


def _module_facts(path: Path) -> dict[str, Any]:
    """Providers, consumers and the members each consumer touches, for one module."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    provides: dict[str, str] = {}
    reads: dict[str, list[str]] = {}
    # variable name -> service name, for this module.  Aliases are what make the
    # members attributable: `store = ctx.get("ltm_store")` then `store.add(...)`.
    aliases: dict[str, str] = {}
    members: dict[str, set[str]] = {}

    constructed: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            # ``store = LTMStore(...)`` / ``mgr = ContextManager(...)``: what a
            # provider hands over is usually a variable, so keeping this lets the
            # audit name the class instead of the variable.
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                for target in targets:
                    if isinstance(target, ast.Name):
                        constructed[target.id] = value.func.id
            if isinstance(value, ast.Call):
                service = _service_name_of(value)
                if service:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = service
                        elif isinstance(target, ast.Attribute):
                            aliases[target.attr] = service
        if isinstance(node, ast.Call):
            service = _service_name_of(node)
            if service:
                if (node.func.attr if isinstance(node.func, ast.Attribute) else "") == "provide":
                    provided = node.args[1] if len(node.args) > 1 else None
                    if isinstance(provided, ast.Call):
                        callee = provided.func
                        provides[service] = (
                            callee.id if isinstance(callee, ast.Name)
                            else (callee.attr if isinstance(callee, ast.Attribute) else "?")
                        )
                    elif isinstance(provided, ast.Name):
                        provides[service] = constructed.get(provided.id, provided.id)
                    else:
                        provides[service] = type(provided).__name__
                reads.setdefault(service, []).append(
                    f"{path.name}:{getattr(node, 'lineno', 0)}"
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            service = aliases.get(node.value.id)
            if service:
                members.setdefault(service, set()).add(node.attr)

    return {"provides": provides, "reads": reads, "members": members}


def audit(services: list[str]) -> dict[str, Any]:
    from agent.runtime.core_services import CORE_SERVICES

    declared = {entry.name: entry for entry in CORE_SERVICES}
    names = services or sorted(declared)

    files = sorted((REPO_ROOT / "agent").rglob("*.py"))
    facts = {}
    for path in files:
        if "__pycache__" in str(path):
            continue
        try:
            facts[path] = _module_facts(path)
        except SyntaxError:
            continue

    # Class surfaces, so "what is used" can be compared with "what exists".
    surfaces: dict[str, dict[str, Any]] = {}
    for path, fact in facts.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                surfaces.setdefault(node.name, _class_surface(node))

    report: dict[str, Any] = {}
    for name in names:
        entry = declared.get(name)
        providers: list[dict[str, str]] = []
        consumers: dict[str, list[str]] = {}
        used: set[str] = set()
        for path, fact in facts.items():
            rel = str(path.relative_to(REPO_ROOT))
            if name in fact["provides"]:
                providers.append({"module": rel, "class": fact["provides"][name]})
            if name in fact["reads"]:
                consumers[rel] = fact["reads"][name]
            used |= fact["members"].get(name, set())
        provider_class = ""
        for provider in providers:
            candidate = provider["class"]
            if candidate in surfaces:
                provider_class = candidate
                break
        surface = surfaces.get(provider_class, {"methods": [], "dunder": []})
        used_sorted = sorted(m for m in used if not m.startswith("_"))
        unused = sorted(set(surface["methods"]) - used)
        report[name] = {
            "declared": (
                {
                    "owners": list(entry.owners),
                    "consumers": list(entry.consumers),
                    "summary": entry.summary,
                }
                if entry is not None
                else None
            ),
            "providers": providers,
            "provider_class": provider_class,
            "consumer_modules": sorted(consumers),
            "used_members": used_sorted,
            "concrete_surface": surface["methods"],
            "surface_not_used": unused,
            "burden": {
                "used": len(used_sorted),
                "surface": len(surface["methods"]),
            },
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("services", nargs="*", help="service names (default: all declared)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = audit(args.services)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    for name, entry in report.items():
        burden = entry["burden"]
        print(f"\n== {name}  (used {burden['used']} / surface {burden['surface']})")
        for provider in entry["providers"]:
            print(f"   provided by {provider['module']} -> {provider['class']}")
        if not entry["providers"]:
            print("   provided by: (nobody in agent/)")
        print(f"   consumers: {len(entry['consumer_modules'])} modules")
        print(f"   used members: {', '.join(entry['used_members']) or '-'}")
        if entry["surface_not_used"]:
            print(
                "   surface a replacement may skip: "
                f"{', '.join(entry['surface_not_used'])}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
