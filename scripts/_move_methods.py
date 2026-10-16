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
    if not rest.strip(" \n,"):
        inner = param
    elif rest.startswith("\n"):
        inner = f"\n    {param}," + rest      # keep the author's one-parameter-per-line shape
    else:
        inner = f"{param}, " + rest
    return text[:op + 1] + inner + text[i:]


def string_occupied_lines(text: str) -> set[int]:
    """0-based indexes of every line a multi-line string literal occupies.

    All of them matter: the opening line carries code indentation, the content lines are the
    value, and the *closing* line's leading whitespace is also part of the value (the text
    between the last newline and the terminator).  Dedenting a content line or a closing line
    therefore edits the string.  For SQL that is invisible; for a docstring it silently rewrites
    committed prose, and this is how that was found.
    """
    occupied: set[int] = set()
    try:
        tree = ast.parse(text)
    except IndentationError:
        try:                       # a fragment carries its block indentation
            tree = ast.parse(textwrap.dedent(text))
        except SyntaxError:
            return occupied
    except SyntaxError:
        return occupied
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.end_lineno and node.end_lineno > node.lineno):
            occupied |= set(range(node.lineno, node.end_lineno))
    return occupied


def dedent_outside_strings(text: str, width: int = 4, skip: frozenset[int] = frozenset()) -> str:
    """Remove up to *width* leading spaces from each line, except string-occupied lines."""
    protected = string_occupied_lines(text) | set(skip)
    out = []
    for index, line in enumerate(text.split("\n")):
        if index in protected or not line.strip():
            out.append(line)
            continue
        cut = 0
        while cut < width and line[cut:cut + 1] == " ":
            cut += 1
        out.append(line[cut:])
    return "\n".join(out)


def dedent_uniform(text: str, width: int = 4) -> str:
    """Remove up to *width* leading spaces from every non-blank line."""
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        cut = 0
        while cut < width and line[cut:cut + 1] == " ":
            cut += 1
        out.append(line[cut:])
    return "\n".join(out)


def already_imported(header: str) -> set[str]:
    """Names the header's *import statements* bind -- not words that merely appear in it.

    A regex over the whole header counts a module named in a docstring as already imported, which
    silently skips the `from . import x` line and leaves the generated module with an unbound name.
    That is exactly what happened with `normalize`.
    """
    try:
        tree = ast.parse(header)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names}
    return names


def add_conn_to_calls(text: str, names: set[str], param: str) -> str:
    """Pass the connection at calls to functions that take one but were called without it.

    A function moved by an *earlier* run already takes the connection; the caller in this batch
    still calls it the old way (`facts._fact_assertions_from_entry(entry)`).  Without this the
    failure is a TypeError in the moved module, one call site at a time.
    """
    for name in sorted(names):
        text = re.sub(
            rf"(?<![\w.])(?<!def ){re.escape(name)}\(\n(\s+)(?!{param}\b)",
            lambda m: f"{name}(\n{m.group(1)}{param}, ", text,
        )
        text = re.sub(
            rf"(?<![\w.])(?<!def ){re.escape(name)}\((?!\n)(?!{param}\b)(?!\s*\))",
            f"{name}({param}, ", text,
        )
        text = re.sub(
            rf"(?<![\w.])(?<!def ){re.escape(name)}\(\s*\)", f"{name}({param})", text,
        )
    return text


def add_kwargs_to_calls(text: str, kwargs_for: dict[str, list[str]]) -> str:
    """Append `name=name` to calls of the given siblings that do not already pass them.

    Inserting only the connection at sibling call sites was half the job: a function that also
    takes a collaborator was called without it ("missing 1 required keyword-only argument").  The
    kwargs are per callee -- passing all of them everywhere made the caller ask for a parameter the
    callee does not take ("unexpected keyword argument").  Edits run last-to-first so offsets hold.
    """
    if not any(kwargs_for.values()):
        return text
    edits: list[tuple[int, list[str]]] = []
    for sibling, kwargs in kwargs_for.items():
        if not kwargs:
            continue
        for match in re.finditer(rf"(?<![\w.])(?<!def ){re.escape(sibling)}\(", text):
            start = match.end() - 1
            depth, index, quote = 0, start, None
            while index < len(text):
                ch = text[index]
                if quote:
                    if ch == "\\":
                        index += 2
                        continue
                    if ch == quote:
                        quote = None
                elif ch in "\"'":
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        inner = text[start + 1:index]
                        missing = [
                            spec for spec in kwargs
                            if f"{spec.split('=', 1)[0]}=" not in inner
                        ]
                        if missing:
                            trimmed = inner.rstrip()
                            if not trimmed:
                                # An empty call `f()` -- skipping it left a wrapper calling a
                                # function whose collaborators were never passed, and the caller
                                # swallowed the TypeError (`getattr(..., None)` probe).
                                edits.append((start + 1, [", ".join(missing)]))
                            else:
                                # Insert after the last argument, not before the closing paren: a
                                # call across lines with a trailing comma ends `...,\n    )`, and
                                # inserting at the paren produced `...,\n    , store=store)`.
                                at = start + 1 + len(trimmed)
                                lead = " " if trimmed.endswith(",") else ", "
                                edits.append((at, [lead + ", ".join(missing)]))
                        break
                index += 1
    for index, pieces in sorted(edits, reverse=True):
        text = text[:index] + "".join(pieces) + text[index:]
    return text


def add_kwonly_params(text: str, name: str, extras: list[str]) -> str:
    """Append keyword-only parameters to a signature, creating the `*` separator if needed.

    A moved body sometimes needs a collaborator the class owned (`self._sync_after_mutation`).
    The wrapper passes it as an expression, so the function takes it as a keyword-only argument and
    the call sites inside the body are rewritten to the bare name.
    """
    op, i = param_span(text, name)
    parts = [p.strip() for p in split_top_level(text[op + 1:i])]
    if not any(p == "*" or p.startswith("*") for p in parts):
        parts.append("*")
    parts.extend(extras)
    inner = "\n    " + ",\n    ".join(parts) + ",\n"
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
        elif isinstance(node, ast.Lambda):
            bind_args(node.args)          # `key=lambda item: item[1]` binds `item`
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
        # `X: dict[...] = {...}` is an AnnAssign, not an Assign: the first version of this missed
        # annotated module constants entirely and then reported them as unresolvable free names.
        target_name = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target_name = node.target.id
        if target_name is not None and not any(
            node.lineno <= m.lineno <= node.end_lineno for m in ()
        ):
            constants[target_name] = (
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
    ap.add_argument(
        "--conn-expr", default="",
        help="expression the body reads the connection from instead of opening one, "
             "e.g. `self._conn` for a store whose callers own the transaction",
    )
    ap.add_argument("--doc", default="", help="docstring for a module that does not exist yet")
    ap.add_argument(
        "--rewrite", action="append", default=[], metavar="FROM=TO",
        help="rewrite one reference the tool cannot resolve on its own (repeatable).  Use it when "
             "a moved body calls something that was extracted earlier: e.g. "
             "`--rewrite self.normalize_category_name=normalize.normalize_category_name`",
    )
    ap.add_argument(
        "--extra-param", action="append", default=[], metavar="NAME=WRAPPER_EXPR",
        help="give the moved function one more keyword-only collaborator and have the wrapper "
             "pass it (repeatable), e.g. `--extra-param invalidate=self._sync_after_mutation`",
    )
    ap.add_argument(
        "--extend", action="store_true",
        help="the target module already exists and is being extended by this run",
    )
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
    used_rewrites: set[str] = set()
    extras_for: dict[str, list[str]] = {}
    block_names: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    needed: set[str] = set()
    problems: list[str] = []

    for method in klass.body:
        if not (isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in moving):
            continue
        start = min([d.lineno for d in method.decorator_list] + [method.lineno]) - 1
        spans[method.name] = (start, method.end_lineno)
        original = dedent_outside_strings("".join(lines[start:method.end_lineno])).strip("\n")
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
        # Two different questions, and conflating them cost a lock bug: `had_connection_param`
        # asks whether the *original method* was handed a connection (the wrapper must pass that
        # one on); `takes_param` asks whether the *moved function* now takes one (true after any
        # insertion here).
        had_connection_param = bool(args.param) and args.param in positional
        # A third acquisition shape: `conn = self._connect()` with no `with`.  Its semantics are
        # the *opposite* of the delegating case -- the original never committed here, it left the
        # transaction to the caller or to a later `with` on the same thread-local connection.  The
        # wrapper must therefore pass the connection without opening a `with` of its own.
        bare_acquire = bool(args.param) and bool(re.search(
            rf"(?m)^[ \t]*{re.escape(args.param)}[ \t]*=[ \t]*self\._connect\(\)[ \t]*$",
            original,
        ))
        extra_params = [p.split("=", 1)[0].strip() for p in args.extra_param if "=" in p]
        takes_param = had_connection_param
        if args.conn_expr:
            # The body reads the connection off the object (`self._conn`); the functions here
            # take it, so the expression becomes the parameter and the wrapper passes its own.
            text = text.replace(args.conn_expr, args.param)
            if not had_connection_param:
                text = with_first_param(text, method.name, args.param)
                takes_param = True
        if bare_acquire:
            # the parameter supplies the connection; the assignment is what opened it before
            text, dropped = re.subn(
                rf"(?m)^[ \t]*{re.escape(args.param)}[ \t]*=[ \t]*self\._connect\(\)[ \t]*\n",
                "", text,
            )
            assert dropped, f"{method.name}: bare acquisition was detected but not removed"
            if not had_connection_param:
                # ...and the parameter it used to create is now one it must be given
                text = with_first_param(text, method.name, args.param)
                takes_param = True
        elif args.param and not had_connection_param and not args.conn_expr:
            text = with_first_param(text, method.name, args.param)
            # Drop the local connection acquisition.  The dedent must be relative to the `with`
            # line: a fixed four-space cut also eats the statement that follows the block (which
            # sits at the same indent as `with`), pushing it to column 0 and out of the function.
            body_lines, base = [], None
            # Occupied lines are computed on the *whole function*: a fragment cannot always be
            # parsed, and when that fallback fails the indexes shift and code lines escape the
            # dedent ("unindent does not match any outer indentation level").
            occupied = string_occupied_lines(text)
            lines_in = text.split("\n")
            out_lines, index = [], 0
            while index < len(lines_in):
                line = lines_in[index]
                if line.strip() == f"with self._connect() as {args.param}:":
                    base = len(line) - len(line.lstrip())
                    index += 1
                    while index < len(lines_in) and (
                        not lines_in[index].strip()
                        or (len(lines_in[index]) - len(lines_in[index].lstrip())) > base
                    ):
                        out_lines.append(
                            lines_in[index] if index in occupied else lines_in[index][4:]
                        )
                        index += 1
                    continue
                out_lines.append(line)
                index += 1
            text = "\n".join(out_lines)
        for pair in args.rewrite:
            # Split at the first `=` that is not followed by a space: an expression may itself
            # contain `=` (`self._cache = None` -> `invalidate(set())`), and splitting on the very
            # first one silently produced `None=invalidate(set())= None`.
            cut = next(
                (i for i, ch in enumerate(pair) if ch == "=" and pair[i + 1:i + 2] != " "),
                pair.find("="),
            )
            if cut < 0:
                problems.append(f"--rewrite needs FROM=TO, got {pair!r}")
                continue
            source_expr, target_expr = pair[:cut], pair[cut + 1:]
            if source_expr in text:
                used_rewrites.add(pair)
                text = text.replace(source_expr, target_expr)
        for sibling in moving:
            if sibling == method.name:
                continue
            text = text.replace(f"self.{sibling}(", f"{sibling}(")
            # ...and a sibling handed over *without* being called (a sort key, a row mapper in a
            # list) is a bare attribute reference.  Rewriting only call shapes leaves `self.` in
            # the body, which the check below then reports as "still references self" -- and a
            # hand-fix is what that cost the first three times it happened.
            text = re.sub(
                rf"(?<![\w.])self\.{re.escape(sibling)}\b(?!\s*\()", sibling, text,
            )
            text = re.sub(
                rf"(?<![\w.])cls\.{re.escape(sibling)}\b(?!\s*\()", sibling, text,
            )
            if not args.param:
                continue
            # `(?!{param}\b)` and not `(?!{param},)`: a sibling that already took the
            # connection was called with it, so inserting another produced `f(conn, conn)`.
            text = re.sub(
                rf"(?<![\w.])(?<!def ){sibling}\(\n(\s+)(?!{args.param}\b)",
                lambda m: f"{sibling}(\n{m.group(1)}{args.param}, ", text,
            )
            text = re.sub(
                rf"(?<![\w.])(?<!def ){sibling}\((?!\n)(?!{args.param}\b)(?!\s*\))",
                f"{sibling}({args.param}, ", text,
            )
            # a call with no arguments at all (`with _immediate_transaction():`) needs it with no
            # comma -- the pattern above deliberately skips empty calls
            text = re.sub(
                rf"(?<![\w.])(?<!def ){sibling}\(\s*\)",
                f"{sibling}({args.param})", text,
            )
        # after the sibling rewriting: before it the calls still read `self.sibling(` and the
        # pattern (which refuses a `.` before the name) matched nothing
        pass  # sibling kwargs are added in a second pass, once every callee's needs are known
        # A batch-level declaration, applied per function by *usage*, and only now: a rewrite may be
        # what introduces the collaborator (`..._for_storage(conn, category, max_categories=...)`),
        # so computing the usage before the rewrites added no parameter at all.
        used_extras: list[str] = []      # decided after the batch, by the fixpoint below
        for extra in extra_params:
            # declaring a collaborator also re-points its uses: `self.store` -> `store`, with a
            # word boundary so `self.store_path` is left alone.  Non-trivial cases (a call that
            # becomes something else) still need an explicit --rewrite.
            text = re.sub(rf"(?<![\w.])self\.{re.escape(extra)}\b", extra, text)
            text = re.sub(rf"(?<![\w.])cls\.{re.escape(extra)}\b", extra, text)
        try:
            ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{method.name}: produced invalid syntax: {exc}")
            continue
        leftover = [ln.strip() for ln in text.split("\n") if re.search(r"(?<![\w.])self\.", ln)]
        if leftover:
            problems.append(
                f"{method.name}: still references self after the move -- {leftover[0][:90]}"
            )
            continue
        if args.param and not re.search(rf"(?<![\w.]){re.escape(args.param)}\b", text):
            problems.append(
                f"{method.name}: the moved body never reads `{args.param}`, so the parameter was "
                f"not needed -- pass --param '' for a body that opens no connection"
            )
            continue
        blocks.append(text)
        block_names.append(method.name)

        # the wrapper that stays in the class
        op, end = param_span(original, method.name)
        is_async = isinstance(method, ast.AsyncFunctionDef)
        decorators = [ast.unparse(d) for d in method.decorator_list]
        # `@contextmanager` is not a marker on the caller's side: it wraps a generator, and the
        # generator is the body.  Leaving it on the wrapper produced a plain function decorated as
        # a context manager, whose `gen` was None -- `next(self.gen)` then raised
        # "'NoneType' object is not an iterator" far away from the move.
        carried_decorators = [
            d for d in decorators if d.split("(")[0].strip().endswith("contextmanager")
        ]
        if carried_decorators:
            decorators = [d for d in decorators if d not in carried_decorators]
            # `text` was already appended to `blocks` above; decorate the block itself, or the
            # change is made to a string nobody reads again.
            blocks[-1] = "\n".join("@" + d for d in carried_decorators) + "\n" + blocks[-1]
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
        uses_connect = bool(args.param) and not args.conn_expr and any(
            line.strip() == f"with self._connect() as {args.param}:" for line in original.splitlines()
        )
        # A body that opened nothing but hands the connection to a sibling needs one too, and it
        # must be opened with `with`: without the commit-on-exit the write stays pending on the
        # thread-local connection and the next statement meets "database is locked".  For a read
        # the `with` commits an empty transaction, which costs nothing.
        delegates_connection = (
            bool(args.param) and not args.conn_expr
            and not had_connection_param and not uses_connect and not bare_acquire
        )
        opens_connection = uses_connect or delegates_connection
        stem = pathlib.Path(args.module).stem
        pad = " " * (12 if opens_connection else 8)
        # Three different ways a wrapper can come by a connection, and mixing them up costs a
        # TypeError: (a) the method already received one, (b) it opened its own, (c) it opened
        # nothing and merely delegated to a sibling that now needs one.
        if args.conn_expr:
            first = args.conn_expr
        elif had_connection_param:
            first = args.param                           # (a) pass on what it was given
        elif opens_connection:
            first = args.param                           # (b)/(c) opened by the `with` below
        elif args.param:
            first = "self._connect()"                    # fallback: no `with`
        else:
            first = ""
        call_args = ([first] if first else []) + [f"{n}={n}" for n in names if n != first]
        by_name = {p.split("=", 1)[0].strip(): p.split("=", 1)[1].strip() for p in args.extra_param}
        for pname in used_extras:
            call_args.append(f"{pname}={by_name[pname]}")
        if call_args:
            call = f"{stem}.{method.name}(\n"
            call += "".join(f"{pad}    {a},\n" for a in call_args)
            call += f"{pad})"
        else:
            call = f"{stem}.{method.name}()"
        returns_none = fn.returns is not None and ast.unparse(fn.returns) == "None"
        # Only an explicit `-> None` justifies dropping the return: an unannotated method may well
        # return something (`_immediate_transaction` is a @contextmanager with no annotation, and a
        # wrapper without `return` handed the caller None).
        prefix = "" if returns_none else "return "
        if is_async:
            prefix = "await " if not prefix else "return await "
        doc = ast.get_docstring(fn).strip().split("\n")[0] if ast.get_docstring(fn) else ""
        wrapper_lines = [textwrap.indent("@" + d, "    ") for d in decorators]
        wrapper_lines.append(textwrap.indent(head + ":", "    "))
        if doc:
            wrapper_lines.append(f'        """{doc}"""')
        if opens_connection:
            wrapper_lines.append(f"        with self._connect() as {args.param}:")
        wrapper_lines.append(f"{pad}{prefix}{call}")
        wrapper = "\n".join(wrapper_lines) + "\n"
        try:
            ast.parse("class _Wrapper:\n" + wrapper)
        except SyntaxError as exc:
            problems.append(f"{method.name}: wrapper is invalid: {exc}")
            continue
        wrappers.append(wrapper)

    # Names the target module already defines are resolvable too: a moved body may call a function
    # that was extracted into it earlier.  Without this the tool treats such a name as unbound and
    # refuses -- which is how `_fact_assertion_core_score` looked from `_fact_assertion_total_score`.
    target_module = pathlib.Path(args.module)
    target_defined: set[str] = set()
    if target_module.exists():
        for node in ast.parse(target_module.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                target_defined.add(node.name)
            elif isinstance(node, ast.Assign):
                target_defined |= {t.id for t in node.targets if isinstance(t, ast.Name)}
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target_defined.add(node.target.id)

    # Which collaborators each moved function must take: the ones it reads itself, plus -- by
    # fixpoint over the sibling calls -- the ones it must forward to a callee.  Taking the union of
    # the batch instead is simpler and was what the first version did, but it gives every function
    # every collaborator (`route_keywords`, `store`, ...) whether it can use them or not.
    direct = {
        name: [e for e in extra_params if re.search(rf"(?<![\w.]){re.escape(e)}\b", block)]
        for name, block in zip(block_names, blocks)
    }
    needs = {name: set(v) for name, v in direct.items()}
    changed = True
    while changed:
        changed = False
        for name, block in zip(block_names, blocks):
            for callee in moving:
                if callee == name or callee not in needs:
                    continue        # skipped earlier in this run
                if re.search(rf"(?<![\w.]){re.escape(callee)}\(", block):
                    if needs[callee] - needs[name]:
                        needs[name] |= needs[callee]
                        changed = True
    for index, name in enumerate(block_names):
        ordered = [e for e in extra_params if e in needs[name]]
        extras_for[name] = ordered
        if ordered:
            blocks[index] = add_kwonly_params(blocks[index], name, ordered)

    # Free names are collected only now: the collaborators became parameters just above, and
    # collecting before that reported every one of them as unbound.
    for name_of_block, block_item in zip(block_names, blocks):
        try:
            needed |= (free_names(block_item) - set(moving))
        except SyntaxError as exc:
            lines_of = block_item.split("\n")
            at = (exc.lineno or 1) - 1
            context = "\n".join(f"    {ln}" for ln in lines_of[max(0, at - 3):at + 3])
            problems.append(
                f"{name_of_block}: the transformed body does not parse ({exc.msg}); near:\n{context}"
            )

    # Second pass: now that every moved function's own needs are known, fill in the collaborators
    # at the sibling call sites -- per callee, so nobody is handed a parameter it cannot take.
    kwargs_for = {
        name: [f"{e}={e}" for e in extras_for.get(name, [])] for name in moving
    }
    earlier_run_callees: set[str] = set()
    # A function moved by an *earlier* run lives in the target module and takes the same kind of
    # collaborator; its callers in this batch must pass it.  Read the target's signatures rather
    # than guessing: only what the callee actually declares as keyword-only is offered.
    target_module = pathlib.Path(args.module)
    if target_module.exists():
        for node in ast.parse(target_module.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                declared = {a.arg for a in node.args.kwonlyargs}
                # NOT `needed`: that name already holds the set of free names collected above,
                # and rebinding it here made the unresolved-name report read a list of extras.
                declared_extras = [name for name in extra_params if name in declared]
                if declared_extras:
                    kwargs_for[node.name] = declared_extras
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                positional = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)]
                if positional and positional[0] == args.param:
                    earlier_run_callees.add(node.name)
    blocks = [add_kwargs_to_calls(block, kwargs_for) for block in blocks]
    # A `--rewrite self.x=mod.x` produces `mod.x(...)` calls, and those are outside both the
    # target module and the moving set -- so nothing inserted the connection they now need.  The
    # callee's own file is on disk next to the source module; read its signature.
    dotted_callees: set[str] = set()
    if args.param:
        source_dir = pathlib.Path(args.file).parent
        for block_item in blocks:
            for match in re.finditer(r"(?<![\w.])(\w+)\.(\w+)\(", block_item):
                module_name, func_name = match.group(1), match.group(2)
                candidate = source_dir / f"{module_name}.py"
                if not candidate.exists():
                    continue
                try:
                    callee_tree = ast.parse(candidate.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in callee_tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                        positional = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)]
                        if positional and positional[0] == args.param:
                            dotted_callees.add(f"{module_name}.{func_name}")
                        break
        if dotted_callees:
            print(f"passing the connection at cross-module calls: {sorted(dotted_callees)}")
    if args.param:
        blocks = [
            add_conn_to_calls(block, (earlier_run_callees | dotted_callees) - set(moving), args.param)
            for block in blocks
        ]
    # The wrapper calls the module function, so it must pass the *declared expression*
    # (`store=self.store`), not the bare local name the in-module calls use.
    expr_for = {
        pair.split("=", 1)[0].strip(): pair.split("=", 1)[1].strip()
        for pair in args.extra_param if "=" in pair
    }
    for index, name in enumerate(block_names):
        wanted = [f"{e}={expr_for[e]}" for e in extras_for.get(name, []) if e in expr_for]
        if wanted and index < len(wrappers):
            wrappers[index] = add_kwargs_to_calls(
                wrappers[index], {f"{pathlib.Path(args.module).stem}.{name}": wanted}
            )

    # A rewrite that matched nothing anywhere in the batch is almost certainly a typo, and a
    # silent no-op there was how a wrong `--rewrite` used to pass unnoticed.
    for pair in args.rewrite:
        cut = next((i for i, ch in enumerate(pair) if ch == "=" and pair[i + 1:i + 2] != " "), pair.find("="))
        if cut >= 0 and pair not in used_rewrites:
            problems.append(f"--rewrite matched nothing in any moved body: {pair!r}")

    consts = sorted(n for n in needed if n in constants)
    unresolved = [n for n in needed if n not in imports and n not in constants and n not in moving]
    unresolved = [n for n in unresolved if n not in MODULE_GLOBALS and n not in target_defined]
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

    header = f'"""{args.doc}"""\n\n' if not target.exists() else target.read_text(encoding="utf-8")
    if not target.exists():
        header += "from __future__ import annotations\n\n__all__ = []\n"
    present = already_imported(header)
    for name in sorted(needed):
        if name in consts or name in target_defined or name not in imports or name in present:
            continue
        statement = imports[name]
        if statement.startswith("from "):
            module = statement.split(" import ")[0]
            # Merge the *imported item*, not the bare name: `from . import rows as _row_mappers`
            # merged as `_row_mappers` produced an import of a module that does not exist, and the
            # failure surfaced as a circular-import error in an unrelated module.
            spec = statement.split(" import ", 1)[1].strip()
            block = re.search(rf"^{re.escape(module)} import \(([^)]*)\)$", header, re.M | re.S)
            if block:
                if spec not in block.group(1):
                    header = header[:block.end(1)] + f"    {spec},\n" + header[block.end(1):]
            else:
                single = re.search(rf"^{re.escape(module)} import (.+)$", header, re.M)
                if single and spec not in single.group(1):
                    header = header[:single.end(1)] + f", {spec}" + header[single.end(1):]
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
    # `agent/memory/retrieval.py` already existed -- BM25 retrieval over entries -- and a move
    # into that name silently replaced it.  Writing into an existing module is how this tool
    # appends to one it made earlier, so the check is not "does it exist" but "was that meant".
    if target.exists() and not args.extend:
        existing = sorted(
            n.name for n in ast.parse(target.read_text(encoding="utf-8")).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        print(
            f"REFUSING: {target} already exists and this run would replace it.  Pass --extend to "
            f"append to a module this tool created earlier.  It currently defines: "
            f"{existing[:8]}{' ...' if len(existing) > 8 else ''}",
            file=sys.stderr,
        )
        return 1

    const_text = "\n\n".join(constants[n][2] for n in consts)
    new_module = header.rstrip("\n") + "\n\n\n"
    if const_text:
        new_module += const_text + "\n\n\n"
    new_module += "\n\n\n".join(blocks) + "\n"

    # the unbound-name check, before any test run
    try:
        check = ast.parse(new_module)
    except SyntaxError as exc:
        lines_of = new_module.split("\n")
        at = (exc.lineno or 1) - 1
        context = "\n".join(
            f"  {n + 1}: {ln}" for n, ln in enumerate(lines_of[max(0, at - 4):at + 4], max(0, at - 4))
        )
        print(
            f"REFUSING to keep this: the generated module does not parse ({exc.msg}); "
            f"near line {exc.lineno}:\n{context}",
            file=sys.stderr,
        )
        return 1
    top = set()
    for node in check.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            top.add(node.name)
        elif isinstance(node, ast.Assign):
            top |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top.add(node.target.id)
    imported = set()
    for node in ast.walk(check):
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names}
    bad = set()
    # Only the module-level functions: a nested function closes over its enclosing scope's locals,
    # and scanning it on its own reports those as unbound (`search_entries`' inner `_merge` reads a
    # list the outer body built).  `free_names` on the outer function binds them.
    for node in check.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(new_module, node)
            if src:
                missing = {
                    n for n in free_names(src)
                    if n not in imported and n not in top and n not in MODULE_GLOBALS
                }
                if missing:
                    print(
                        f"  (unbound in {node.name}: {sorted(missing)})", file=sys.stderr,
                    )
                bad |= missing
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
    # A constant may only travel with its reader if it has no *other* readers.  Deleting it from
    # the source otherwise leaves the code that stays behind with a NameError at call time --
    # `compile()` cannot see that (it is a runtime lookup) and the unbound-name check inspects
    # the generated module, so the first version of this check was silent about it.  A constant
    # that both sides need cannot be re-imported from the new module (that cycles), so the only
    # honest answer is to refuse and let the operator give it a third home.
    for name in consts:
        if re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
            print(
                f"REFUSING: {name} is read by code that stays in {args.file} as well as by the "
                f"methods being moved; give it a home outside both modules first "
                f"(re-importing it from {target.name} would be a cycle)",
                file=sys.stderr,
            )
            return 1

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
    if args.dry_run:
        print(f"dry run: would write {target} and rewrite {args.file}")
        return 0
    target.write_text(new_module, encoding="utf-8")
    source.write_text(text, encoding="utf-8")
    print(f"wrote {target} and rewrote {args.file}")
    print("next: import both modules, run the unbound-name check, then the targeted tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
