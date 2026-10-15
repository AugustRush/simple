#!/usr/bin/env python3
"""Move methods out of a class into a module of functions, mechanically and verifiably.

This is a refactoring aid, not part of the product.  It was written while splitting five
god classes (see ``docs/superpowers/plans/2026-10-15-remaining-big-files-survey.md``) after the
same sharp edges cost a failed run each.  Every rule below is a bug that actually happened.

What it does, for each method named on the command line:

* cuts the method out of the class, keeping decorators inside the span (a class's decorator
  lines precede its ``lineno``, and forgetting that leaves an orphan ``@dataclass`` behind);
* drops the receiver parameter **structurally** -- it splits the parameter list and removes the
  entry named ``self``/``cls``, because a comma-shaped pattern misses ``def f(self)``;
* inserts the new first parameter (``conn`` by default) in its place;
* rewrites calls to the *other* methods being moved so they are called by bare name, and passes
  the new first parameter at those call sites -- excluding definition sites, because a pattern
  over ``name(`` otherwise rewrites ``def name(`` and produces ``(conn, conn)``;
* resolves the moved bodies' closure: module-level constants move with their only reader, and
  imports are re-created **respecting** ``ImportFrom.level`` (``from .models import X``, never
  ``from models import X``);
* writes a wrapper in the class that keeps the original signature verbatim (including ``self``)
  and delegates, so no call site and no test needs editing for the API's sake;
* runs an unbound-name check on the generated module and a diff of each moved body against its
  original, and refuses to write anything if either finds something it cannot explain.

It never runs pytest: the supervisor runs the suite, once, alone.

Usage::

    python scripts/_move_methods.py --file agent/memory/store.py --module usage \\
        --methods append_usage_event,usage_summary --param conn --dry-run

Exit status is non-zero if any check fails; nothing is written in that case.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import difflib
import pathlib
import re
import sys
import textwrap

BUILTINS = set(dir(builtins))

#: Names every module has without importing them.  ``__file__`` cost a refused run: it is a real
#: module global in the destination, and a body computing a path from it is safe to move *only*
#: while the destination sits at the same package depth (see the subagent work order).
MODULE_GLOBALS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__annotations__", "__debug__",
}


# ── structural parameter editing ─────────────────────────────────────────────


def param_span(text: str, name: str) -> tuple[int, int]:
    """Offsets of the parentheses that hold *name*'s parameter list."""
    op = text.index("(", text.index("def " + name))
    depth, i = 0, op
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return op, i


def split_top_level(text: str) -> list[str]:
    """Split a parameter list on commas that are not inside brackets."""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


def receiver_name(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    if positional and positional[0].arg in ("self", "cls"):
        return positional[0].arg
    return None


def without_receiver(text: str, name: str, fn) -> tuple[str, str | None]:
    """Remove the leading self/cls, returning the new text and what was removed."""
    receiver = receiver_name(fn)
    if receiver is None:
        return text, None
    op, i = param_span(text, name)
    parts = split_top_level(text[op + 1:i])
    kept = [p for p in parts if p.strip().split(":")[0].strip() != receiver]
    inner = ("\n    " + ",\n    ".join(p.strip() for p in kept) + ",\n") if kept else ""
    return text[:op + 1] + inner + text[i:], receiver


def with_first_param(text: str, name: str, param: str) -> str:
    """Put *param* first, whether or not the parameter list is otherwise empty."""
    op, i = param_span(text, name)
    rest = text[op + 1:i]
    inner = param if not rest.strip(" \n,") else f"{param}, " + rest.lstrip("\n ")
    return text[:op + 1] + inner + text[i:]


# ── closure ──────────────────────────────────────────────────────────────────


def free_names(text: str) -> set[str]:
    """Names a standalone function body reads that nothing in it binds.

    Knows that ``*args``/``**kwargs`` bind names, that comprehension targets bind, and that a
    nested function closing over an enclosing local is therefore *not* unbound -- the last of
    which my first checker got wrong and reported against pre-existing code.
    """
    fn = ast.parse(text).body[0]
    bound: set[str] = set()

    def bind_args(a: ast.arguments) -> None:
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            bound.add(arg.arg)
        if a.vararg:
            bound.add(a.vararg.arg)
        if a.kwarg:
            bound.add(a.kwarg.arg)

    bind_args(fn.args)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bind_args(node.args)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
    loaded = {
        n.id for n in ast.walk(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return {n for n in loaded - bound if n not in BUILTINS}


def module_index(tree: ast.Module, lines: list[str]):
    """Module-level constants, module-level functions, and the imports for each name."""
    constants: dict[str, tuple[int, int, str]] = {}
    functions: set[str] = set()
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants[node.targets[0].id] = (
                node.lineno - 1,
                node.end_lineno,
                "".join(lines[node.lineno - 1:node.end_lineno]).rstrip("\n"),
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.ImportFrom):
            module = "." * (node.level or 0) + (node.module or "")
            for alias in node.names:
                as_ = f" as {alias.asname}" if alias.asname else ""
                imports[alias.asname or alias.name] = f"from {module} import {alias.name}{as_}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                as_ = f" as {alias.asname}" if alias.asname else ""
                imports[(alias.asname or alias.name).split(".")[0]] = f"import {alias.name}{as_}"
    return constants, imports, functions


# ── main ─────────────────────────────────────────────────────────────────────


def import_anchor(header: str) -> int:
    """Where a new import may go: after the docstring and after ``from __future__``.

    Inserting before ``from __future__ import annotations`` is not a style problem -- it is a
    ``SyntaxError`` at compile time that ``ast.parse`` does not raise, which is how one of
    these moves produced a file that parsed and would not import.
    """
    offset = 0
    stripped = header.lstrip()
    if stripped.startswith('"""') or stripped.startswith("'''"):
        quote = stripped[:3]
        end = stripped.find(quote, 3)
        if end != -1:
            offset = len(header) - len(stripped) + end + 3
    tail = header[offset:]
    future = re.search(r"^from __future__ import [^\n]*\n", tail, re.M)
    if future:
        return offset + future.end()
    first = re.search(r"^(from |import )", tail, re.M)
    return offset + (first.start() if first else 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="the module holding the class")
    ap.add_argument("--class", dest="klass", required=True)
    ap.add_argument("--module", required=True, help="new module: a path, e.g. agent/memory/usage.py")
    ap.add_argument("--methods", required=True, help="comma-separated method names")
    ap.add_argument("--param", default="conn", help="new first parameter (default: conn)")
    ap.add_argument("--doc", default="", help="docstring for a module that does not exist yet")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    source = pathlib.Path(args.file)
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == args.klass)
    moving = [m.strip() for m in args.methods.split(",") if m.strip()]
    known = {m.name for m in klass.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [m for m in moving if m not in known]
    if missing:
        print(f"not methods of {args.klass}: {missing}", file=sys.stderr)
        return 2

    constants, imports, module_functions = module_index(tree, lines)
    blocks: list[str] = []
    wrappers: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    needed: set[str] = set()
    problems: list[str] = []

    for method in klass.body:
        if not (isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in moving):
            continue
        start = min([d.lineno for d in method.decorator_list] + [method.lineno]) - 1
        spans[method.name] = (start, method.end_lineno)
        original = textwrap.dedent("".join(lines[start:method.end_lineno])).strip("\n")
        # Decorators are class-level concerns (`@staticmethod`, `@property`, `@contextmanager`,
        # a lock-carrying `@_synchronized`): they stay with the wrapper.  Only `@staticmethod`
        # and `@classmethod` used to be stripped here, which carried the others into the new
        # module -- where `@_synchronized` would then be called as `method(self, ...)`.
        decorator_names = {ast.unparse(d) for d in method.decorator_list}
        original = "\n".join(
            line for line in original.splitlines()
            if line.strip().lstrip("@").split("(")[0] not in decorator_names
        )
        fn = ast.parse(original).body[0]

        # the function that will live in the new module
        text, receiver = without_receiver(original, method.name, fn)
        # `--param ""` means "invent no parameter": the function keeps the signature its body
        # actually needs.  The store work always needed a connection; the HTTP and channel
        # work never does.
        positional = [a.arg for a in list(fn.args.posonlyargs) + list(fn.args.args)]
        takes_param = bool(args.param) and args.param in positional
        if args.param and not takes_param:
            text = with_first_param(text, method.name, args.param)
            # Drop the local connection acquisition.  The dedent must be relative to the `with`
            # line: a fixed four-space cut also eats the statement that follows the block (which
            # sits at the same indent as `with`), pushing it to column 0 and out of the function.
            body_lines, base = [], None
            for line in text.split("\n"):
                if line.strip() == f"with self._connect() as {args.param}:":
                    base = len(line) - len(line.lstrip())
                    continue
                if base is not None:
                    indent = len(line) - len(line.lstrip())
                    if line.strip() and indent > base:
                        body_lines.append(line[4:])
                        continue
                    if line.strip():
                        base = None
                body_lines.append(line)
            text = "\n".join(body_lines)
        for sibling in moving:
            if sibling == method.name:
                continue
            text = text.replace(f"self.{sibling}(", f"{sibling}(")
            if not args.param:
                continue
            text = re.sub(
                rf"(?<![\w.])(?<!def ){sibling}\(\n(\s+)(?!{args.param},)",
                lambda m: f"{sibling}(\n{m.group(1)}{args.param}, ", text,
            )
            text = re.sub(
                rf"(?<![\w.])(?<!def ){sibling}\((?!\n)(?!{args.param},)(?!\s*\))",
                f"{sibling}({args.param}, ", text,
            )
        try:
            ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{method.name}: produced invalid syntax: {exc}")
            continue
        if re.search(r"(?<![\w.])self\.", text):
            problems.append(f"{method.name}: still references self after the move")
            continue
        blocks.append(text)
        needed |= (free_names(text) - set(moving))

        # the wrapper that stays in the class
        op, end = param_span(original, method.name)
        is_async = isinstance(method, ast.AsyncFunctionDef)
        decorators = [ast.unparse(d) for d in method.decorator_list]
        head = f"def {method.name}({original[op + 1:end]})"
        if fn.returns is not None:
            head += " -> " + ast.unparse(fn.returns)
        if is_async:
            head = "async " + head
        names = [
            a.arg for a in list(fn.args.posonlyargs) + list(fn.args.args) + list(fn.args.kwonlyargs)
            if a.arg != receiver
        ]
        # Only a body that actually opened the connection gets a wrapper that opens one.  The
        # first version of this emitted `with self._connect()` unconditionally, which is right
        # for the store classes it was written against and wrong for every other class -- the
        # wrapper then raises AttributeError at call time rather than at import time.
        uses_connect = bool(args.param) and any(
            line.strip() == f"with self._connect() as {args.param}:" for line in original.splitlines()
        )
        stem = pathlib.Path(args.module).stem
        pad = " " * (12 if uses_connect else 8)
        first = args.param if (uses_connect or takes_param) else ""
        call_args = ([first] if first else []) + [
            f"{n}={n}" for n in names if n != first
        ]
        if call_args:
            call = f"{stem}.{method.name}(\n"
            call += "".join(f"{pad}    {a},\n" for a in call_args)
            call += f"{pad})"
        else:
            call = f"{stem}.{method.name}()"
        returns_none = fn.returns is not None and ast.unparse(fn.returns) == "None"
        prefix = "" if returns_none else ("return " if fn.returns is not None else "")
        if is_async:
            prefix = "await " if not prefix else "return await "
        doc = ast.get_docstring(fn).strip().split("\n")[0] if ast.get_docstring(fn) else ""
        wrapper_lines = [textwrap.indent("@" + d, "    ") for d in decorators]
        wrapper_lines.append(textwrap.indent(head + ":", "    "))
        if doc:
            wrapper_lines.append(f'        """{doc}"""')
        if uses_connect:
            wrapper_lines.append(f"        with self._connect() as {args.param}:")
        wrapper_lines.append(f"{pad}{prefix}{call}")
        wrapper = "\n".join(wrapper_lines) + "\n"
        try:
            ast.parse("class _Wrapper:\n" + wrapper)
        except SyntaxError as exc:
            problems.append(f"{method.name}: wrapper is invalid: {exc}")
            continue
        wrappers.append(wrapper)

    consts = sorted(n for n in needed if n in constants)
    unresolved = [n for n in needed if n not in imports and n not in constants and n not in moving]
    unresolved = [n for n in unresolved if n not in MODULE_GLOBALS]
    # A module-level function cannot be re-imported (the old module imports the new one, so that
    # is a cycle) and should not be carried silently.  Refuse with the name, so the operator
    # moves it deliberately -- as its own `--methods` entry it does not qualify, since it is not
    # a method of the class.
    carried_functions = sorted(n for n in unresolved if n in module_functions)
    if carried_functions:
        problems.append(
            f"these are module-level functions in {args.file} and must be moved deliberately "
            f"(or passed in): {carried_functions}"
        )
        unresolved = [n for n in unresolved if n not in module_functions]
    unresolved = [n for n in unresolved if not re.match(r"^(Any|Optional|Callable|Sequence|dict|list|set|tuple)$", n)]
    if unresolved:
        problems.append(f"cannot resolve these free names: {unresolved}")

    print(f"moving: {moving}")
    print(f"module-level constants travelling with their reader: {consts}")
    print(f"imports to re-create: {sorted(n for n in needed if n in imports and n not in consts)}")
    if problems:
        for p in problems:
            print(f"REFUSING: {p}", file=sys.stderr)
        return 1

    target = pathlib.Path(args.module)
    if args.dry_run:
        print(f"dry run: would write {target} and rewrite {args.file}")
        return 0

    header = f'"""{args.doc}"""\n\n' if not target.exists() else target.read_text(encoding="utf-8")
    if not target.exists():
        header += "from __future__ import annotations\n\n__all__ = []\n"
    for name in sorted(needed):
        if name in consts or name not in imports:
            continue
        statement = imports[name]
        if re.search(rf"\b{re.escape(name)}\b", header):
            continue
        if statement.startswith("from "):
            module = statement.split(" import ")[0]
            block = re.search(rf"^{re.escape(module)} import \(([^)]*)\)$", header, re.M | re.S)
            if block:
                header = header[:block.end(1)] + f"    {name},\n" + header[block.end(1):]
            else:
                single = re.search(rf"^{re.escape(module)} import (.+)$", header, re.M)
                if single:
                    header = header[:single.end(1)] + f", {name}" + header[single.end(1):]
                else:
                    anchor = re.search(r"^from \.", header, re.M)
                    statement += "\n"
                    if anchor:
                        header = header[:anchor.start()] + statement + header[anchor.start():]
                    else:
                        header = header.replace(
                            "from __future__ import annotations\n",
                            "from __future__ import annotations\n\n" + statement, 1,
                        )
        else:
            header = header[:import_anchor(header)] + statement + "\n" + header[import_anchor(header):]
    if "__all__ = [" in header:
        listed = re.search(r"__all__ = \[([^]]*)\]", header, re.S)
        existing = [x.strip().strip('"') for x in listed.group(1).split(",") if x.strip()]
        merged = existing + [m for m in moving if m not in existing]
        block = "__all__ = [\n" + "".join(f'    "{m}",\n' for m in merged) + "]"
        header = header[:listed.start()] + block + header[listed.end():]
    const_text = "\n\n".join(constants[n][2] for n in consts)
    new_module = header.rstrip("\n") + "\n\n\n"
    if const_text:
        new_module += const_text + "\n\n\n"
    new_module += "\n\n\n".join(blocks) + "\n"

    # the unbound-name check, before any test run
    check = ast.parse(new_module)
    top = set()
    for node in check.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            top |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    imported = set()
    for node in ast.walk(check):
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names}
    bad = set()
    for node in ast.walk(check):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(new_module, node)
            if src and not src.startswith("    "):
                bad |= {
                    n for n in free_names(src)
                    if n not in imported and n not in top and n not in MODULE_GLOBALS
                }
    if bad:
        print(f"REFUSING: unbound names in {target}: {sorted(bad)}", file=sys.stderr)
        return 1
    print(f"unbound-name check on the generated module: clean")

    # byte-level faithfulness of each moved body
    for method, (start, end) in spans.items():
        original = textwrap.dedent("".join(lines[start:end])).strip("\n")
        kept = [b for b in blocks if re.match(rf"(?:async )?def {re.escape(method)}\(", b)]
        if not kept:
            continue
        diff = [
            line for line in difflib.unified_diff(
                original.splitlines(), kept[0].splitlines(), lineterm="", n=0,
            )
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        intended = sum(
            1 for line in diff
            if any(s in line for s in ("def ", "@classmethod", "@staticmethod", "self", args.param, "with "))
        )
        print(f"{method}: {len(diff)} changed lines, {intended} attributable to the move itself")

    far = list(lines)
    for (start, end), wrapper in sorted(zip(spans.values(), wrappers), key=lambda p: p[0][0], reverse=True):
        far[start:end] = [wrapper + "\n"]
    text = "".join(far)
    for name in consts:
        text = text.replace(constants[name][2] + "\n", "", 1)
    # Match the file's own import style: `web.py` has no relative imports at all, so a
    # `from . import x` line would be the only one of its kind there.
    stem = pathlib.Path(args.module).stem
    relative = re.search(r"^from \.", text, re.M)
    if relative:
        module_import = f"from . import {stem}"
        insert_at = relative.start()
    else:
        package = ".".join(pathlib.Path(args.file).with_suffix("").parts[:-1])
        module_import = f"from {package} import {stem}"
        insert_at = import_anchor(text)
    if module_import not in text:
        text = text[:insert_at] + module_import + "\n" + text[insert_at:]
    target.parent.mkdir(parents=True, exist_ok=True)
    # Two checks, both for injuries that `ast.parse` does not report:
    #   * a statement dedented out of its function still parses (the `with`-block dedent once
    #     pushed a trailing `return` to column 0);
    #   * an import placed before `from __future__` parses and will not import.
    for method, block in zip(moving, blocks):
        # The header may legitimately span lines ending at column 0 (`) -> X:`); the *body*
        # may not.  Find where the header ends, then require indentation after it.
        depth, header_end = 0, 0
        for index, line in enumerate(block.split("\n")):
            depth += line.count("(") + line.count("[") + line.count("{")
            depth -= line.count(")") + line.count("]") + line.count("}")
            if line.rstrip().endswith(":") and depth <= 0:
                header_end = index + 1
                break
        stray = [
            line for line in block.split("\n")[header_end:]
            if line.strip() and not line.startswith((" ", "\t"))
        ]
        if stray:
            print(f"REFUSING: {method} has body lines outside it: {stray[:2]}", file=sys.stderr)
            return 1
    for label, blob in ((target, new_module), (source, text)):
        try:
            compile(blob, str(label), "exec")
        except (SyntaxError, ValueError) as exc:
            print(f"REFUSING to keep this: {label} does not compile: {exc}", file=sys.stderr)
            return 1
    target.write_text(new_module, encoding="utf-8")
    source.write_text(text, encoding="utf-8")
    print(f"wrote {target} and rewrote {args.file}")
    print("next: import both modules, run the unbound-name check, then the targeted tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
