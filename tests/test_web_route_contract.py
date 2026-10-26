"""Contract test: every ``/api/...`` endpoint the web frontend calls exists.

The browser is the one runtime this suite cannot drive: there is no
playwright/selenium here, and the frontend has no JS test runner.  That left a
whole class of defects uncovered -- a page that calls an endpoint the backend
does not (or no longer does) show up as "I clicked it and nothing happened".
This test closes exactly that gap without a browser and without any new
dependency: it tokenizes the TypeScript under ``frontend/src``, extracts every
``/api/...`` call site (method + path, with ``${...}`` template holes and
``a + b`` path concatenation folded together), builds the backend's *real*
route table from the production app factory (``WebChannel._build_app``, no
server is started, nothing is bound), and asserts every call matches a route.

**What it proves.** Each endpoint the frontend is written against exists on the
backend, under a method that route accepts, at a path whose literal segments
line up and whose parameters cover the frontend's holes.  Renaming a route,
dropping a handler, or typo'ing a path in a hook fails here with file:line
instead of silently 404ing in a browser.  Template holes are treated as "some
value goes here": a whole-segment hole must line up with a route parameter, and
one glued to literal text (the stream URL appends an optional ``?token=``
suffix this way) may expand to nothing, so only the literal characters around
it are asserted.

**What it does not prove.** It is a contract, not an end-to-end test.  It says
nothing about DOM rendering, event wiring, whether a click reaches the call,
response bodies/shapes, status codes, auth or the streaming protocol -- and it
only sees paths written as literals in the frontend.  URLs the backend itself
emits (e.g. scheduled-run artifact links) and endpoints other clients use are
outside its scope.  A request whose path is a bare function parameter
(``fetch(path)`` inside the generic wrapper) is skipped -- there is nothing to
check a parameter against; the call sites that pass a path into it are checked
instead.  The reverse direction (backend routes nobody in the frontend calls)
is reported for information only, never asserted: several routes exist for the
CLI or other clients.

The extractor is deliberately a small tokenizer rather than a regex over raw
text, because path expressions in this codebase are built from template holes,
``+`` concatenation across lines, ternaries and variables, and comments contain
``/api/...`` mentions that must not become expectations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

#: Functions whose first argument is a request path.  ``api`` arranges the auth
#: headers and defaults to GET (see ``hooks/useApiClient.tsx``); ``fetch`` is the
#: same default through the platform; ``refreshJson``/``readJson`` are local GET
#: helpers.  Any call not given an explicit ``method`` is a GET.
_HTTP_CALLS = frozenset({"api", "fetch", "refreshJson", "readJson"})

_HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE")

#: WebSocket calls carry no HTTP method; the backend registers a
#: ``WebSocketRoute`` for them, which this label matches.
_WS = "WS"


# --------------------------------------------------------------------------- #
# Backend: the real route table, from the production app factory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Route:
    """One (method, path) pair the backend actually serves."""

    method: str
    path: str


def _backend_routes() -> list[_Route]:
    """Build the production app and read its routes -- no server is started.

    ``WebChannel.__init__`` calls ``_build_app``, which is the same function the
    gateway serves from, so this table cannot drift from production.  The config
    is the minimal one: no CORS middleware, no auth token, and the app is never
    bound to a port (only ``start()`` does that).
    """
    from starlette.routing import Mount, Route, WebSocketRoute

    from agent.channels.web import WebChannel, WebConfig

    app = WebChannel(WebConfig()).app
    routes: list[_Route] = []
    for route in app.routes:
        if isinstance(route, Mount):
            continue  # static assets, not part of the JSON API surface
        if isinstance(route, WebSocketRoute):
            routes.append(_Route(_WS, route.path))
            continue
        if not isinstance(route, Route):
            continue
        for method in sorted(route.methods or ()):
            # GET implies HEAD on a Starlette Route; HEAD/OPTIONS are not what
            # any call site asks for, so leave them out of the table.
            if method in _HTTP_VERBS:
                routes.append(_Route(method, route.path))
    return routes


# --------------------------------------------------------------------------- #
# Frontend: a tokenizer, so paths survive templates/comments/concatenation
# --------------------------------------------------------------------------- #

_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
#: A ``${...}`` template hole inside a path literal.
_HOLE_RE = re.compile(r"\$\{[^{}]*\}")


@dataclass(frozen=True)
class _Tok:
    kind: str  # "str" | "template" | "ident" | "punct"
    text: str  # source text, for punctuation/identifiers
    value: str  # string/template contents (raw: ``${...}`` left intact)
    line: int


def _tokenize(text: str) -> list[_Tok]:
    """Split TypeScript into tokens, dropping comments.

    Comments are dropped because several frontend comments and docstrings
    mention ``/api/...`` by name; a comment is prose, not a call.  Strings and
    template literals become single tokens, so punctuation inside them (``{}``,
    ``?``, ``/``) cannot confuse the call/argument scanner.
    """
    toks: list[_Tok] = []
    n = len(text)
    i = 0
    line = 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            end = n if j < 0 else j + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if c in "'\"":
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c or ch == "\n":
                    break
                j += 1
            end = min(j + 1, n)
            toks.append(_Tok("str", text[i:end], text[i + 1 : min(j, n)], line))
            i = end
            continue
        if c == "`":
            end, value = _scan_template(text, i)
            toks.append(_Tok("template", text[i:end], value, line))
            line += text.count("\n", i, end)
            i = end
            continue
        if c.isalpha() or c in "_$":
            m = _IDENT_RE.match(text, i)
            j = m.end() if m else i + 1
            toks.append(_Tok("ident", text[i:j], text[i:j], line))
            i = j
            continue
        toks.append(_Tok("punct", c, c, line))
        i += 1
    return toks


def _scan_template(text: str, i: int) -> "tuple[int, str]":
    """Scan a template literal starting at the backtick; return (end, contents)."""
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            return j + 1, text[i + 1 : j]
        if c == "$" and text.startswith("${", j):
            j = _skip_braced(text, j + 2)
            continue
        j += 1
    return n, text[i + 1 : n]


def _skip_braced(text: str, i: int) -> int:
    """Return the index just past the ``}`` closing a brace opened before *i*."""
    depth = 1
    n = len(text)
    j = i
    while j < n and depth:
        c = text[j]
        if c in "'\"":
            quote = c
            j += 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            j += 1
        elif c == "`":
            j, _ = _scan_template(text, j)
        elif c == "{":
            depth += 1
            j += 1
        elif c == "}":
            depth -= 1
            j += 1
        else:
            j += 1
    return j


@dataclass(frozen=True)
class _Site:
    """One place in the frontend that calls an ``/api`` endpoint."""

    file: str
    line: int
    #: The helper that issues the request (``api``/``fetch``/``refreshJson``/
    #: ``readJson``/``WebSocket``), or ``"url"`` for a path literal that is not
    #: a call's argument -- a link built for the browser to GET.
    call: str
    #: Method candidates.  ``()`` means "a method we could not read" (the test
    #: then only requires *some* route for the path); ``("WS",)`` for sockets.
    methods: tuple[str, ...]
    #: Every path alternative in this call, normalized: template holes became
    #: ``*`` and query strings are gone.  A ternary path yields two entries.
    paths: tuple[str, ...]


def _frontend_files() -> list[Path]:
    files = sorted(
        p for p in FRONTEND_SRC.rglob("*") if p.suffix in {".ts", ".tsx"} and p.is_file()
    )
    return files


def _call_end(toks: list[_Tok], open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at *open_idx*."""
    depth = 0
    for k in range(open_idx, len(toks)):
        tok = toks[k]
        if tok.kind == "punct":
            if tok.text == "(":
                depth += 1
            elif tok.text == ")":
                depth -= 1
                if depth == 0:
                    return k
    return len(toks) - 1


def _split_args(toks: list[_Tok], open_idx: int, close_idx: int) -> list[list[_Tok]]:
    """Split a call's tokens into arguments at top-level commas."""
    args: list[list[_Tok]] = [[]]
    depth = 0
    for k in range(open_idx + 1, close_idx):
        tok = toks[k]
        if tok.kind == "punct":
            if tok.text in "([{":
                depth += 1
            elif tok.text in ")]}":
                depth -= 1
            elif tok.text == "," and depth == 0:
                args.append([])
                continue
        args[-1].append(tok)
    return [arg for arg in args if arg]


def _path_groups(arg: list[_Tok]) -> list[list[_Tok]]:
    """Group an argument's literals into path expressions.

    ``'a' + 'b'`` is one path (concatenation); ``cond ? 'a' : 'b'`` is two
    (alternatives).  Anything non-literal breaks the run.
    """
    groups: list[list[_Tok]] = []
    current: list[_Tok] = []
    join_next = False
    for tok in arg:
        if tok.kind in {"str", "template"}:
            if current and not join_next:
                groups.append(current)
                current = []
            current.append(tok)
            join_next = False
        elif tok.kind == "punct" and tok.text == "+" and current:
            join_next = True
        else:
            if current:
                groups.append(current)
                current = []
            join_next = False
    if current:
        groups.append(current)
    return groups


def _strip_query(raw: str) -> str:
    """Drop ``?...``/``#...`` from a path, ignoring literals inside template holes."""
    depth = 0
    i = 0
    while i < len(raw):
        if raw.startswith("${", i):
            depth += 1
            i += 2
            continue
        ch = raw[i]
        if ch == "}" and depth:
            depth -= 1
        elif ch in "?#" and depth == 0:
            return raw[:i]
        i += 1
    return raw


def _normalize_path(raw: str) -> "tuple[str, ...] | None":
    """Turn a frontend path expression into segments; ``None`` if not an API path.

    A string may embed the path after a prefix (``${proto}://host/api/...``), so
    the path starts at the first ``/api/``.  Query strings are dropped, and each
    template hole becomes ``*``: a hole that is a whole segment is a path
    parameter (an id), while one sharing a segment with literal text is only
    known to append to it.
    """
    start = raw.find("/api/")
    if start < 0:
        return None
    body = _strip_query(raw[start:]).rstrip("/")
    if not body:
        return None
    segments: list[str] = []
    for seg in body.split("/"):
        if not seg:
            continue
        if _HOLE_RE.fullmatch(seg):
            segments.append("*")
        else:
            segments.append(_HOLE_RE.sub("*", seg))
    return tuple(segments)


def _method_names(arg: list[_Tok]) -> tuple[str, ...]:
    """Read the HTTP verbs a request-init argument can produce.

    Returns ``("GET",)`` when there is no ``method`` key (the default of both
    ``api`` and ``fetch``), every literal verb for a ternary such as
    ``method: cond ? 'PUT' : 'POST'``, and ``()`` when a ``method`` key exists
    but carries no readable literal -- an honest "unknown", not a guess.
    """
    for k, tok in enumerate(arg):
        if tok.kind != "ident" or tok.text != "method":
            continue
        if k + 1 >= len(arg) or arg[k + 1].text != ":":
            continue
        verbs: list[str] = []
        depth = 0
        for tok2 in arg[k + 2 :]:
            if tok2.kind in {"str", "template"}:
                verb = tok2.value.strip().upper()
                if verb in _HTTP_VERBS:
                    verbs.append(verb)
            elif tok2.kind == "punct":
                if tok2.text in "([{":
                    depth += 1
                elif tok2.text in ")]}":
                    depth -= 1
                elif tok2.text == "," and depth == 0:
                    break
        return tuple(dict.fromkeys(verbs))
    return ("GET",)


_CONTINUES = frozenset(
    {"?", ":", "+", "&&", "||", "??", ",", "=", "=>", "(", "[", "{", ".", "?."}
)


def _continues(tok: _Tok) -> bool:
    return tok.kind == "punct" and tok.text in _CONTINUES


def _assignment_rhs(
    toks: list[_Tok], eq_idx: int
) -> "tuple[list[_Tok], set[int]]":
    """Tokens of the expression after ``=``, plus their indices.

    Statements in the frontend are often semicolon-less, so the end is the
    first token that neither continues the expression nor is continued by the
    previous one across a line break.
    """
    out: list[_Tok] = []
    indices: set[int] = set()
    depth = 0
    prev = toks[eq_idx]
    k = eq_idx + 1
    while k < len(toks):
        tok = toks[k]
        if depth == 0 and tok.kind == "punct" and tok.text == ";":
            break
        if (
            depth == 0
            and out
            and tok.line > prev.line
            and not _continues(prev)
            and not _continues(tok)
        ):
            break
        if tok.kind == "punct" and tok.text in "([{":
            depth += 1
        elif tok.kind == "punct" and tok.text in ")]}":
            if depth == 0:
                break
            depth -= 1
        out.append(tok)
        indices.add(k)
        prev = tok
        k += 1
    return out, indices


def _assignments(toks: list[_Tok]) -> dict[str, tuple[list[_Tok], set[int]]]:
    """``const NAME = <expression>`` declarations, keyed by name.

    Paths are sometimes built once and passed to the request later
    (``const target = ...; api(target, {...})``); without this the verb would
    be read as GET and the path would look like a standalone GET call.
    """
    found: dict[str, tuple[list[_Tok], set[int]]] = {}
    for k, tok in enumerate(toks):
        if tok.kind != "ident" or tok.text not in {"const", "let", "var"}:
            continue
        if k + 2 >= len(toks):
            continue
        name, eq = toks[k + 1], toks[k + 2]
        if name.kind != "ident" or eq.text != "=":
            continue
        expr, indices = _assignment_rhs(toks, k + 2)
        if expr:
            found[name.text] = (expr, indices)
    return found


def _frontend_sites() -> list[_Site]:
    """Extract every ``/api/...`` call site from ``frontend/src``."""
    sites: list[_Site] = []
    for path in _frontend_files():
        text = path.read_text(encoding="utf-8")
        if "/api/" not in text:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        toks = _tokenize(text)
        assignments = _assignments(toks)
        handled: set[int] = set()

        for k, tok in enumerate(toks):
            if tok.kind != "ident":
                continue
            if tok.text in _HTTP_CALLS:
                call, kind = tok.text, "http"
            elif tok.text == "WebSocket" and k and toks[k - 1].text == "new":
                call, kind = "WebSocket", "ws"
            else:
                continue
            if k + 1 >= len(toks) or toks[k + 1].text != "(":
                continue

            open_idx = k + 1
            close_idx = _call_end(toks, open_idx)
            args = _split_args(toks, open_idx, close_idx)
            handled.update(range(k, close_idx + 1))
            if not args:
                continue

            arg0 = args[0]
            methods: tuple[str, ...]
            if kind == "ws":
                methods = (_WS,)
            else:
                methods = _method_names(args[1] if len(args) > 1 else [])

            groups = _path_groups(arg0)
            if not groups and len(arg0) == 1 and arg0[0].kind == "ident":
                # path held in a variable: resolve it to its assignment
                resolved = assignments.get(arg0[0].text)
                if resolved is not None:
                    expr, indices = resolved
                    groups = _path_groups(expr)
                    handled |= indices
                    arg0 = expr

            paths = [
                normalized
                for normalized in (
                    _normalize_path("".join(t.value for t in group)) for group in groups
                )
                if normalized
            ]
            if not paths:
                continue  # e.g. fetch(path) in the generic request helper
            line = min(group[0].line for group in groups)
            sites.append(
                _Site(
                    file=rel,
                    line=line,
                    call=call,
                    methods=methods,
                    paths=tuple(dict.fromkeys(paths)),
                )
            )

        # Bare paths outside any call (comparisons, ``startsWith``, a returned
        # link) are still requests: the browser issues them as GETs.
        for k, tok in enumerate(toks):
            if tok.kind not in {"str", "template"} or k in handled:
                continue
            norm = _normalize_path(tok.value)
            if norm is None:
                continue
            sites.append(
                _Site(file=rel, line=tok.line, call="url", methods=("GET",), paths=(norm,))
            )
    return sites


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

_PARAM_RE = re.compile(r"^\{([^{}]+)\}$")


def _segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def _match_segments(
    route_segs: list[str], call_segs: list[str], ri: int = 0, ci: int = 0
) -> bool:
    """Whether a route path can serve a call path, segment by segment.

    ``{param}`` matches one call segment; ``{param:path}`` may match several
    (it is registered for nested artifact paths).  A whole-segment ``*`` (a
    frontend template hole) must line up with a route parameter -- a concrete
    id is not evidence for a literal route such as ``/api/schedules/attention``.
    A hole sharing a segment with literal text may expand to nothing (the
    stream URL appends an optional ``${qs}``), so only the literal characters
    around it are asserted.
    """
    if ri == len(route_segs):
        return ci == len(call_segs)
    route_seg = route_segs[ri]
    param = _PARAM_RE.match(route_seg)
    if param:
        name = param.group(1)
        if name.endswith(":path"):
            for take in range(1, len(call_segs) - ci + 1):
                if _match_segments(route_segs, call_segs, ri + 1, ci + take):
                    return True
            return False
        if ci >= len(call_segs):
            return False
        return _match_segments(route_segs, call_segs, ri + 1, ci + 1)
    if ci >= len(call_segs):
        return False
    call_seg = call_segs[ci]
    if call_seg == "*":
        return False
    if "*" in call_seg:
        pattern = "^" + re.escape(call_seg).replace(r"\*", ".*") + "$"
        return bool(re.match(pattern, route_seg)) and _match_segments(
            route_segs, call_segs, ri + 1, ci + 1
        )
    return call_seg == route_seg and _match_segments(
        route_segs, call_segs, ri + 1, ci + 1
    )


def _path_matches(route_path: str, call_path: "tuple[str, ...]") -> bool:
    return _match_segments(_segments(route_path), list(call_path))


def _has_route(routes: list[_Route], method: str, call_path: "tuple[str, ...]") -> bool:
    return any(r.method == method and _path_matches(r.path, call_path) for r in routes)


def _call_display(path: "tuple[str, ...]") -> str:
    return "/" + "/".join(path)


def _near_misses(
    routes: list[_Route], method: str, call_path: "tuple[str, ...]", limit: int = 4
) -> list[str]:
    """Routes in the same API area, to make a typo obvious in the failure text."""
    area = list(call_path[:2])
    return sorted(
        {
            r.path
            for r in routes
            if r.method == method
            and _segments(r.path)[:2] == area
            and not _path_matches(r.path, call_path)
        }
    )[:limit]


def _contract_problems(sites: list[_Site], routes: list[_Route]) -> list[str]:
    """Every frontend call the backend cannot serve, as ``file:line`` lines."""
    problems: list[str] = []
    for site in sites:
        where = f"{site.file}:{site.line}"
        displays = [_call_display(p) for p in site.paths]
        joined = ", ".join(displays)

        if not site.methods:
            # The verb was unreadable; still insist the path exists somewhere.
            missing = [
                d
                for path, d in zip(site.paths, displays)
                if not any(_path_matches(r.path, path) for r in routes)
            ]
            if missing:
                problems.append(
                    f"{where}: {site.call}({', '.join(missing)}) "
                    "-- no backend route for this path (any method)"
                )
            continue

        # ``available[path]`` = the verbs this site could use that the backend
        # serves for that path.  All paths must have at least one (so no click
        # can hit a 404), and every verb must be served by at least one path
        # (so a ternary's other branch is not dead).
        available = {
            path: [m for m in site.methods if _has_route(routes, m, path)]
            for path in site.paths
        }
        missing_paths = [
            d for path, d in zip(site.paths, displays) if not available[path]
        ]
        missing_verbs = [
            m for m in site.methods if not any(m in available[p] for p in site.paths)
        ]
        if not missing_paths and not missing_verbs:
            continue

        tried = "|".join(site.methods)
        reasons: list[str] = []
        if missing_paths:
            reasons.append(f"no {tried} route for {', '.join(missing_paths)}")
        # For a single-path call the missing verb is the same finding; only spell
        # it out separately when a ternary's other branch is what fell over.
        if missing_verbs and (len(site.paths) > 1 or not missing_paths):
            reasons.append(f"method {'/'.join(missing_verbs)} serves none of these paths")
        line = f"{where}: {site.call}({joined}) -- " + "; ".join(reasons)
        hints: list[str] = []
        for path in site.paths:
            if available[path]:
                continue
            for method in site.methods:
                for near in _near_misses(routes, method, path):
                    hint = f"{method} {near}"
                    if hint not in hints:
                        hints.append(hint)
        if hints:
            line += "\n        后端同一区域现有的路由：" + ", ".join(hints[:6])
        problems.append(line)
    return problems


def _report(problems: list[str]) -> str:
    listed = "\n".join(f"  - {p}" for p in problems)
    return (
        f"前端调用了 {len(problems)} 条后端无法承接的 /api 调用"
        "（浏览器里表现为 404 / 点了没反应）：\n" + listed
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_frontend_api_calls_have_backend_routes() -> None:
    """Every ``/api`` call in ``frontend/src`` is served by a backend route."""
    if not FRONTEND_SRC.is_dir():
        pytest.skip("frontend sources are not part of this checkout")
    sites = _frontend_sites()
    routes = _backend_routes()
    assert sites, "extracted no frontend API calls -- the extractor is broken"
    problems = _contract_problems(sites, routes)
    assert not problems, _report(problems)


def test_extractor_recognises_the_shapes_the_frontend_uses() -> None:
    """Guard the extractor itself.

    The contract test above is only as good as the call sites it finds: a
    tokenizer regression would make it pass by finding nothing.  These spot
    checks pin one example of every shape the frontend actually uses.
    """
    if not FRONTEND_SRC.is_dir():
        pytest.skip("frontend sources are not part of this checkout")
    sites = _frontend_sites()
    assert len(sites) >= 60, f"only {len(sites)} API call sites extracted"

    def has(
        file_suffix: str, methods: "tuple[str, ...]", paths: "tuple[str, ...]"
    ) -> bool:
        return any(
            site.file.endswith(file_suffix)
            and paths in site.paths
            and set(methods) <= set(site.methods)
            for site in sites
        )

    # plain literal + implicit GET
    assert has("useAutomation.tsx", ("GET",), ("api", "schedules"))
    # template holes become parameters
    assert has("useMemory.tsx", ("PATCH",), ("api", "memory", "entries", "*"))
    # query strings are stripped
    assert has("useConversations.tsx", ("GET",), ("api", "sessions", "*", "messages"))
    # a fetch() with an explicit verb
    assert has("useAutomation.tsx", ("POST",), ("api", "schedules", "preview"))
    # `a + b` concatenation across lines is one path
    assert has("useAutomation.tsx", ("GET",), ("api", "schedules", "*", "runs", "*", "output"))
    # ternary path and ternary method in one call
    assert has("useAutomation.tsx", ("PUT", "POST"), ("api", "workflows", "*"))
    assert has("useAutomation.tsx", ("PUT", "POST"), ("api", "workflows"))
    # WebSocket route behind a full ws:// URL; the trailing hole is the
    # optional ``?token=...`` query the socket appends
    assert has("useConversations.tsx", ("WS",), ("api", "sessions", "*", "stream*"))
    # path built in a variable, verb read from the call that consumes it
    target = [
        site
        for site in sites
        if site.file.endswith("useAutomation.tsx")
        and ("api", "schedules", "*") in site.paths
        and "PUT" in site.methods
    ]
    assert target, "the `const target = ...; api(target, {method})` shape was not resolved"


def test_backend_route_table_is_non_trivial_and_its_unused_routes_reported() -> None:
    """The table looks like the real API surface, and the unused part is printed.

    The table is produced by ``WebChannel`` -- i.e. by the production
    ``_build_app`` -- so its size and the known routes double as a check that
    the app factory is still where this test thinks it is.
    """
    if not FRONTEND_SRC.is_dir():
        pytest.skip("frontend sources are not part of this checkout")
    routes = _backend_routes()
    pairs = {(r.method, r.path) for r in routes}
    assert ("GET", "/api/health") in pairs
    assert ("POST", "/api/sessions") in pairs
    # The streaming socket is a WebSocketRoute, not a normal Route: a plain
    # method-only table would miss it and the frontend's socket would look
    # unserved.
    assert (_WS, "/api/sessions/{session_id}/stream") in pairs
    assert len(routes) >= 50, f"only {len(routes)} backend routes seen"

    # Reverse direction: reported, never asserted.  Several routes exist for
    # the CLI or other clients, and treating "unused by this page" as a defect
    # would make the test wrong the first time a route is added for one client.
    sites = _frontend_sites()
    called = [
        r for r in routes if any(_path_matches(r.path, p) for s in sites for p in s.paths)
    ]
    unused = sorted(
        {
            (r.method, r.path)
            for r in routes
            if r.path.startswith("/api/") and r not in called
        }
    )
    if unused:
        print("\nbackend /api routes no frontend call site matches (informational):")
        for method, path in unused:
            print(f"  {method:7} {path}")
