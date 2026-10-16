#!/usr/bin/env python
"""Split a run of `r.register(...)` statements out of a long registration method.

Why this exists
---------------
`BuiltinTools._register` was 1650 lines: `r = self.registry` followed by 42 independent
`r.register(name, description, schema, handler)` statements, each 7-180 lines.  The survey
(`docs/superpowers/plans/2026-10-15-remaining-big-files-survey.md`, section 2c) measured it
and recorded why this is a split rather than a method move: the statements are not
self-contained -- they read the collaborator objects built in `__init__`
(`self._file_tools`, `self._schedule_tools`, ...) -- and the registry is a shared mutable
object, so the **order** of registration has to be preserved exactly.

This tool moves one contiguous run of those statements into a module-level
`def <function>(r, *, <collaborators>) -> None:` in a new module, and leaves a call behind.
It refuses rather than guessing:

* a statement in the range that is not an `r.register(...)` call
* a `self.X` in the range that is not in `--collaborators`
* a collaborator declared but never referenced (a dead parameter, and a sign of a typo)
* a free name in the moved statements that cannot be resolved from the source file's imports
* an invalid result (`compile()` on both files)

Every moved statement's string literals are compared as a multiset against the original, so a
JSON schema that lost indentation inside a string is caught rather than shipped.

Usage
-----
    scripts/_split_registrations.py \\
        --file agent/tools/builtin_tools.py --class BuiltinTools --method _register \\
        --statements 1-8 --module agent/tools/builtin_file_tools.py \\
        --function register_file_tools \\
        --collaborators current_time,shell_tools,file_tools,audio_tools \\
        --doc "..." [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys
from collections import Counter


def out_lines_after_removal(lines: list[str], picked: list[ast.stmt]) -> list[str]:
    """The file's lines with the moved statements removed, for the constant check."""
    return lines[:picked[0].lineno - 1] + lines[picked[-1].end_lineno:]


def collect_imports(tree: ast.Module, lines: list[str]) -> tuple[dict[str, str], set[str]]:
    """name -> the import statement that binds it, and the module-level constants."""
    imports: dict[str, str] = {}
    constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = "." * (node.level or 0) + (node.module or "")
            for alias in node.names:
                statement = f"from {module} import {alias.name}"
                if alias.asname:
                    statement += f" as {alias.asname}"
                imports[alias.asname or alias.name] = statement
        elif isinstance(node, ast.Import):
            for alias in node.names:
                statement = f"import {alias.name}"
                if alias.asname:
                    statement += f" as {alias.asname}"
                imports[alias.asname or alias.name] = statement
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants.add(node.targets[0].id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants.add(node.target.id)
    return imports, constants


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--class", dest="klass", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--statements", required=True, metavar="A-B", help="inclusive index range")
    parser.add_argument("--module", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--collaborators", default="", help="comma-separated, in signature order")
    parser.add_argument("--doc", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = pathlib.Path(args.file)
    src = source.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    klass = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == args.klass), None)
    if klass is None:
        print(f"REFUSING: no class {args.klass} in {args.file}", file=sys.stderr)
        return 1
    method = next(
        (n for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == args.method),
        None,
    )
    if method is None:
        print(f"REFUSING: no method {args.method} in {args.klass}", file=sys.stderr)
        return 1

    match = re.fullmatch(r"(\d+)-(\d+)", args.statements)
    if match is None:
        print(f"REFUSING: --statements wants A-B, got {args.statements!r}", file=sys.stderr)
        return 1
    first, last = int(match.group(1)), int(match.group(2))
    if not (0 <= first <= last < len(method.body)):
        print(f"REFUSING: {args.statements} is outside the {len(method.body)} statements", file=sys.stderr)
        return 1
    picked = method.body[first:last + 1]

    # `--collaborators` names the *parameter*; the attribute behind it is resolved by trying
    # `_name` first, then `name`, because these objects live on the instance with a leading
    # underscore (`self._file_tools`).  An explicit `attr=param` wins over the guess.
    attrs_in_block = {
        node.attr
        for stmt in picked
        for node in ast.walk(stmt)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
    }
    collaborators: list[str] = []
    attrs_for: dict[str, str] = {}
    problems: list[str] = []
    for entry in (c.strip() for c in args.collaborators.split(",")):
        if not entry:
            continue
        attr, _, param = entry.partition("=")
        param = param or attr.lstrip("_")
        if not param:
            problems.append(f"--collaborators entry {entry!r} has no parameter name")
            continue
        attr = attr if attr in attrs_in_block else (f"_{param}" if f"_{param}" in attrs_in_block else attr)
        if attr not in attrs_in_block:
            problems.append(f"--collaborators {entry!r}: neither `self.{attr}` nor `self._{param}` appears")
        collaborators.append(param)
        attrs_for[param] = attr
    if problems:
        for problem in problems:
            print(f"REFUSING: {problem}", file=sys.stderr)
        return 1

    for stmt in picked:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            problems.append(f"statement at line {stmt.lineno} is a {type(stmt).__name__}, not a call")
            continue
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr not in set(attrs_for.values())
            ):
                problems.append(f"line {stmt.lineno}: `self.{node.attr}` is not in --collaborators")
                break
    if problems:
        for problem in problems:
            print(f"REFUSING: {problem}", file=sys.stderr)
        return 1

    # the statements' text, dedented outside string interiors: a multi-line JSON schema keeps
    # its indentation, because that indentation is part of the value
    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    def occupied(text: str) -> set[int]:
        try:
            tree_of = ast.parse(text)
        except SyntaxError:
            try:
                tree_of = ast.parse("\n".join(text.split("\n")) + "\n")
            except SyntaxError:
                return set()
        rows: set[int] = set()
        for node in ast.walk(tree_of):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                end = getattr(node, "end_lineno", node.lineno)
                for line in range(node.lineno, end + 1):
                    rows.add(line - 1)
        return rows

    block_lines = lines[picked[0].lineno - 1:picked[-1].end_lineno]
    protected = occupied("".join(block_lines))
    body_out: list[str] = []
    for index, line in enumerate(block_lines):
        if index in protected:
            body_out.append(line)
        else:
            cut = min(4, indent_of(line)) if line.strip() else 0
            body_out.append(line[cut:])
    body_text = "".join(body_out).rstrip("\n")
    for param, attr in attrs_for.items():
        body_text = re.sub(rf"(?<![\w.])self\.{re.escape(attr)}(?![\w])", param, body_text)
    if re.search(r"(?<![\w.])self\.", body_text):
        print("REFUSING: the moved text still references self", file=sys.stderr)
        return 1

    # what the moved statements need from outside
    fragment = f"def {args.function}({', '.join(collaborators) if collaborators else ''}):\n"
    try:
        fragment_tree = ast.parse(
            f"def _probe(r, *, {', '.join(collaborators)}):\n"
            + "".join(f"    {ln}\n" for ln in body_text.split("\n"))
        )
    except SyntaxError as exc:
        print(f"REFUSING: the moved statements do not parse on their own: {exc}", file=sys.stderr)
        return 1
    imports, constants = collect_imports(tree, lines)
    needed: set[str] = set()
    for node in ast.walk(fragment_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            needed.add(node.id)
    needed -= set(collaborators) | {"r"}
    unresolved = sorted(n for n in needed if n not in imports and n not in constants and n not in MODULE_GLOBALS)
    if unresolved:
        print(f"REFUSING: cannot resolve from {args.file}: {unresolved}", file=sys.stderr)
        return 1
    import_lines = sorted({imports[n] for n in needed if n in imports})

    # Module-level constants the moved statements read travel with their reader -- unless the
    # code that stays behind still reads them too, in which case copying is a duplicate and
    # deleting is a NameError, so the only honest answer is to refuse.
    carried = sorted(n for n in needed if n in constants)
    if carried:
        remaining = "".join(out_lines_after_removal(lines, picked))
        for name in carried:
            # the definition line is not a reader, and it stays behind by construction -- the
            # first version of this check counted it and refused a constant that nothing else
            # touched
            definition = next(
                (n for n in tree.body
                 if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                     and n.targets[0].id == name)
                 or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name)),
                None,
            )
            without_definition = "".join(
                line for number, line in enumerate(remaining.splitlines(keepends=True), 1)
                if definition is None or not (definition.lineno <= number <= definition.end_lineno)
            )
            if re.search(rf"(?<![\w.]){re.escape(name)}\b", without_definition):
                print(
                    f"REFUSING: {name} is read by the code that stays in {args.file} as well as "
                    f"by the statements being moved; give it a home outside both modules first",
                    file=sys.stderr,
                )
                return 1
    carried_text = ""
    for name in carried:
        node = next(
            (n for n in tree.body
             if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id == name)
             or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name)),
            None,
        )
        if node is None:
            print(f"REFUSING: {name} is a constant but its definition was not found", file=sys.stderr)
            return 1
        carried_text += "".join(lines[node.lineno - 1:node.end_lineno]).rstrip("\n") + "\n\n"

    signature = "r" + (f",\n    *,\n    {', '.join(collaborators)}" if collaborators else "")
    doc = args.doc or f"The tools this module registers, and what each one is for."
    new_module = (
        f'"""{doc}"""\n\n'
        "from __future__ import annotations\n\n"
        + ("\n".join(import_lines) + "\n\n" if import_lines else "")
        + (carried_text if carried_text else "")
        + f"def {args.function}(\n    {signature},\n) -> None:\n"
        + "".join(f"    {line}\n" for line in body_text.split("\n"))
    )
    try:
        compile(new_module, args.module, "exec")
    except SyntaxError as exc:
        print(f"REFUSING: the generated module does not compile: {exc}", file=sys.stderr)
        return 1

    # the call that replaces them, and the import for it
    stem = pathlib.Path(args.module).stem
    # the call passes what the *method* has: the instance's collaborator objects.  Passing the
    # bare parameter names (which `_register` does not have) silently registered nothing.
    call = f"        {stem}.{args.function}(r" + "".join(
        f", {param}=self.{attrs_for[param]}" for param in collaborators
    ) + ")\n"
    out = list(lines)
    out[picked[0].lineno - 1:picked[-1].end_lineno] = [call]
    text = "".join(out)
    for name in carried:
        node = next(
            (n for n in ast.parse(text).body
             if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id == name)
             or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name)),
            None,
        )
        if node is not None:
            text = "".join(text.splitlines(keepends=True)[:node.lineno - 1]
                           + text.splitlines(keepends=True)[node.end_lineno:])
    if f"from . import {stem}" not in text and f"from agent.tools import {stem}" not in text:
        anchor = re.search(r"^from \.", text, re.M)
        line_to_add = f"from . import {stem}\n"
        text = text[:anchor.start()] + line_to_add + text[anchor.start():] if anchor else line_to_add + text
    try:
        compile(text, args.file, "exec")
    except SyntaxError as exc:
        print(f"REFUSING: {args.file} does not compile afterwards: {exc}", file=sys.stderr)
        return 1

    # every moved statement's string literals, as a multiset
    def lits(node: ast.AST) -> list[str]:
        return sorted(x.value for x in ast.walk(node) if isinstance(x, ast.Constant) and isinstance(x.value, str))

    new_tree = ast.parse(new_module)
    new_fn = next(n for n in new_tree.body if isinstance(n, ast.FunctionDef) and n.name == args.function)
    before = Counter(s for stmt in picked for s in lits(stmt))
    after = Counter(s for stmt in new_fn.body for s in lits(stmt))
    if before != after:
        print("REFUSING: the string literals changed in the move", file=sys.stderr)
        for value in (before - after): print(f"  lost: {value[:70]!r}", file=sys.stderr)
        for value in (after - before): print(f"  new : {value[:70]!r}", file=sys.stderr)
        return 1

    print(f"moved {len(picked)} statements, {picked[-1].end_lineno - picked[0].lineno + 1} lines "
          f"-> {args.module}::{args.function} (string literals identical: {sum(before.values())})")
    if args.dry_run:
        print(f"dry run: would write {args.module} and rewrite {args.file}")
        return 0
    pathlib.Path(args.module).write_text(new_module, encoding="utf-8")
    source.write_text(text, encoding="utf-8")
    print(f"wrote {args.module} and rewrote {args.file}")
    return 0


MODULE_GLOBALS = {"Any", "Optional", "Callable", "Path", "dict", "list", "set", "tuple", "str", "int", "bool", "float"}


if __name__ == "__main__":
    raise SystemExit(main())
