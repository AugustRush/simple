"""The memory contracts, checked against the implementation that exists.

Three things are asserted here, and they are different questions:

1. **Does the implementation satisfy the protocol?**  Every member declared in
   ``agent/memory/contracts.py`` exists on the real class with the same parameter
   names and the same async-ness.  ``isinstance`` alone would only check names,
   and a renamed parameter is exactly how a replacement breaks a caller that
   passes it by keyword.
2. **Is the protocol the whole seam?**  The public members the concrete classes
   have *beyond* the protocol must not be called from outside ``agent/memory/``.
   If one is, the contract is incomplete -- a replacement would satisfy the
   protocol and still break that caller.
3. **Does the behaviour hold?**  :func:`run_store_contract` exercises the
   semantics any store must have, and it is written to be imported by another
   implementation's test: the built-in store passes it today, and the point of
   the exercise is that a second backend passes the same function unchanged
   (that is what "replaceable" means, as opposed to "documented as replaceable").
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: "The contract declares no default for this name", distinguished from a default of
#: ``None`` -- which some contracts use deliberately as a sentinel.
_UNSET = object()

#: Attribute peeks that remain, each a decision rather than a blanket exclusion.
#:
#: The rule the guard enforces is "an out-of-package reader must not depend on a
#: member the contract does not declare".  These four do, and each is *soft*: the
#: caller feature-detects, so an implementation without the member loses a feature
#: rather than breaking.  Recording them keeps the guard's teeth for the hard ones
#: -- and the next round's queue is exactly this list.
_ATTRIBUTE_PEEK_ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent/channels/web.py", "store"): (
        "soft: the web session service takes the store it is given and degrades "
        "without one; contracting it is a separate decision about SessionService"
    ),
    ("agent/commands/builtin.py", "store"): (
        "soft: the store is the manager's collaborator, and the read goes through the "
        "contract's recent_conversation_turns (this used to feature-detect "
        "get_turns_for_session, which nothing implements -- a dead path)"
    ),
    ("agent/core/token_budget.py", "consolidation"): (
        "not a peek to close: three lazy reads of the engine's estimators, each with a "
        "pure-Python fallback whose docstring says the calibrated and raw estimates "
        "coincide when there is no engine -- that fallback is the designed behaviour, "
        "so contracting it would remove the no-engine path rather than narrow anything"
    ),
    ("agent/session_service.py", "dir"): (
        "not the memory store: this receiver is the *session* store, whose name "
        "collides with LTMStore.dir (a name-based scan cannot tell them apart)"
    ),
}

#: Public members of the concrete classes that are deliberately *outside* the
#: contracts, with the reason.  Empty is the goal; an entry here is a decision.
OUTSIDE_THE_CONTRACT: dict[str, str] = {}


def _protocol_members(protocol: Any) -> dict[str, Any]:
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    }


def _protocol_attributes(protocol: Any) -> dict[str, Any]:
    """Public data members: declared properties, plus bare annotations.

    Methods are one half of an interface and data is the other; a store handed to
    ``MemoryPalace`` has to expose ``memory_dir`` or the palace cannot find its
    export directory, and nothing about "the method signatures match" would catch
    that.
    """
    declared = {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and isinstance(member, property)
    }
    for name, hint in getattr(protocol, "__annotations__", {}).items():
        if not name.startswith("_"):
            declared.setdefault(name, hint)
    return declared


def _signature_shape(func: Any) -> list[tuple[str, str, bool]]:
    """Parameter names, kinds and whether each has a default.

    Default *values* are deliberately not compared: a replacement may choose its
    own as long as the parameter exists and is optional in the same way.
    """
    signature = inspect.signature(func)
    return [
        (parameter.name, str(parameter.kind), parameter.default is not inspect.Parameter.empty)
        for parameter in signature.parameters.values()
    ]


def _signature_accepts(declared: list[tuple[str, str, bool]], actual: list[tuple[str, str, bool]]) -> str:
    """Whether *actual* can serve every call the contract allows, or why not.

    Directional on purpose, in both directions that matter:

    * every parameter the contract declares must exist in the implementation, in
      the same position kind -- and may not be *more* required than declared (a
      contract default is a promise that callers may omit it);
    * an implementation may add parameters of its own only if they are optional,
      because a caller written against the contract will not pass them.
    """
    if len(actual) < len(declared):
        return f"declares fewer parameters than the contract: {actual} vs {declared}"
    for index, (name, kind, has_default) in enumerate(declared):
        actual_name, actual_kind, actual_default = actual[index]
        if actual_name != name or actual_kind != kind:
            return f"parameter {index} is {actual_name!r}/{actual_kind}, contract says {name!r}/{kind}"
        if has_default and not actual_default:
            return f"parameter {name!r} is required, but the contract makes it optional"
    for name, kind, has_default in actual[len(declared):]:
        if not has_default and "VAR_" not in kind:
            return f"extra required parameter {name!r} is not in the contract"
    return ""


def _assert_conforms(protocol: Any, implementation: Any, label: str) -> None:
    """Names, parameter shapes and async-ness, compared class to class.

    Read off the *classes* rather than the instance: an instance's bound method
    has already dropped ``self``, and comparing that with the protocol's own
    function would report every member as different.  Class-level access also
    resolves ``staticmethod``/``classmethod`` the way a caller sees them.
    """
    # An instance or a class: some services are awkward to construct in a test, and
    # the class-level check is the same one (every member present, same signature),
    # while ``isinstance`` against a runtime-checkable Protocol on a *class object*
    # would inspect ``type``'s attributes and fail for the wrong reason.
    cls = implementation if isinstance(implementation, type) else type(implementation)
    if not isinstance(implementation, type):
        assert isinstance(implementation, protocol), (
            f"{label} does not satisfy {protocol.__name__}"
        )
    for name, member in _protocol_members(protocol).items():
        assert hasattr(cls, name), f"{label} is missing {name}"
        actual = getattr(cls, name)
        problem = _signature_accepts(
            _signature_shape(member), _signature_shape(actual)
        )
        assert not problem, f"{label}.{name}: {problem}"
        assert inspect.iscoroutinefunction(actual) == inspect.iscoroutinefunction(member), (
            f"{label}.{name} async-ness differs from the contract"
        )
    # Data members may be instance attributes (``self.api_format = ...``), which a
    # class does not carry: read them off the class *and* off its ``__init__``.
    instance_data = _class_instance_data(cls)
    for name, declared in _protocol_attributes(protocol).items():
        present = hasattr(cls, name) or name in instance_data
        assert present, f"{label} is missing the {name} attribute"
        if not hasattr(cls, name):
            continue
        value = getattr(cls, name)
        # ``None`` is only a failure when the *contract* does not also declare it.
        # The rule used to be unconditional, which is wrong for a documented sentinel:
        # ``ModelTransport.thinking_effort = None`` means "the config never said, keep
        # the provider's default", and an implementation inheriting exactly that is
        # conforming, not unset.
        contract_default = getattr(protocol, name, _UNSET)
        if contract_default is _UNSET or contract_default is not None:
            assert value is not None, f"{label}.{name} is None"
        if declared is int or declared == "int":
            assert isinstance(value, int), f"{label}.{name} should be an int"
        if "Path" in str(declared):
            assert isinstance(value, Path), f"{label}.{name} should be a Path"


def _make_store(tmp_path: Path) -> Any:
    from agent import LTMStore

    return LTMStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )


def test_every_backend_satisfies_the_store_contract(backend, tmp_path):
    """Signature-and-attribute conformance, for every implementation.

    Parametrized like the behaviour suite: a second backend that satisfies the
    semantics but renames a parameter would break a caller that passes it by
    keyword, and nothing about the behavioural run would notice.
    """
    from agent.memory.contracts import MemoryStore

    _assert_conforms(MemoryStore, backend(tmp_path), backend.__name__)


def test_the_builtin_services_satisfy_their_contracts(tmp_path):
    from agent import ConsolidationEngine, ContextManager, LocalRetriever, MemoryPalace, StagingBuffer
    from agent.memory.contracts import ContextService, MemoryService

    context_dir = tmp_path / "context"
    store = _make_store(tmp_path)
    manager = ContextManager(
        store=store,
        retriever=LocalRetriever(),
        consolidation=ConsolidationEngine(store=store),
        staging=StagingBuffer(context_dir=context_dir, session_id="session-1"),
    )
    palace = MemoryPalace(base_dir=tmp_path / "memory", context_dir=context_dir)

    _assert_conforms(ContextService, manager, "ContextManager")
    _assert_conforms(MemoryService, palace, "MemoryPalace")


def _concrete_public_members(path: Path, class_name: str) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith("_")
            }
    raise AssertionError(f"{class_name} not found in {path.name}")


@pytest.mark.parametrize(
    ("path", "class_name", "protocol_name"),
    [
        ("agent/memory/store.py", "LTMStore", "MemoryStore"),
        ("agent/memory/context.py", "ContextManager", "ContextService"),
        ("agent/memory/palace.py", "MemoryPalace", "MemoryService"),
    ],
)
def test_nothing_outside_the_package_calls_past_the_contract(
    path: str, class_name: str, protocol_name: str
):
    """A member beyond the protocol that a consumer calls is a hole in it.

    ``ContextManager.store`` is the recorded exception rather than a silent one:
    two core modules reach through the service to the store
    (``core/spawn_planning.py``, ``core/subagent_finalize.py`` call
    ``context_manager.store.add_entry``), which is why ``add_entry`` is part of
    the store contract even though the store is otherwise reached only through
    the manager.
    """
    from agent.memory import contracts

    protocol = getattr(contracts, protocol_name)
    declared = set(_protocol_members(protocol))
    extras = _concrete_public_members(REPO_ROOT / path, class_name) - declared

    offenders: list[str] = []
    for source in sorted((REPO_ROOT / "agent").rglob("*.py")):
        relative = source.relative_to(REPO_ROOT)
        if str(relative).startswith("agent/memory/") or "__pycache__" in str(relative):
            continue
        text = source.read_text(encoding="utf-8")
        for member in sorted(extras):
            if member in OUTSIDE_THE_CONTRACT:
                continue
            if re.search(rf"\.{re.escape(member)}\s*\(", text):
                offenders.append(f"{relative}: {class_name}.{member}")
    assert not offenders, (
        "these members are outside the contract but a consumer calls them, so a "
        "replacement could satisfy the protocol and still break it:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "protocol_name", ["MemoryStore", "ContextService", "MemoryService"]
)
def test_no_member_is_declared_without_a_caller(protocol_name: str):
    """A contract member nobody calls is a guess, not a contract.

    The protocols are derived from what consumers use; adding a member "for
    completeness" is how an interface grows the very surface this exists to
    shrink.  Every declared name must appear in a call site outside the contracts
    module itself.
    """
    from agent.memory import contracts

    protocol = getattr(contracts, protocol_name)
    sources = [
        source
        for source in (REPO_ROOT / "agent").rglob("*.py")
        if "__pycache__" not in str(source) and source.name != "contracts.py"
    ]
    text = "\n".join(source.read_text(encoding="utf-8") for source in sources)

    unused = [
        name
        for name in _protocol_members(protocol)
        if not re.search(rf"\.{re.escape(name)}\s*\(", text)
        and not re.search(rf"def {re.escape(name)}\s*\(", text)
    ]
    assert not unused, f"{protocol_name} declares members nobody calls: {unused}"


def _class_instance_data(cls: type) -> set[str]:
    """Data a class's methods assign to ``self`` -- its instance attributes."""
    import inspect

    try:
        source_path = Path(inspect.getfile(cls))
    except (TypeError, OSError):  # pragma: no cover - builtins and C extensions
        return set()
    try:
        return _concrete_data_members(source_path, cls.__name__)
    except AssertionError:  # pragma: no cover - not defined in that file
        return set()


def _concrete_data_members(path: Path, class_name: str) -> set[str]:
    """Public *data* a class exposes: properties, class annotations, ``self.x = …``.

    The method scan cannot see any of this, which is how ``.staging``/``.store``
    stayed invisible while six files depended on them.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        names: set[str] = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any("property" in ast.unparse(d) for d in item.decorator_list):
                    names.add(item.name)
                for sub in ast.walk(item):
                    if isinstance(sub, ast.Assign):
                        targets = sub.targets
                    elif isinstance(sub, ast.AnnAssign):
                        targets = [sub.target]
                    else:
                        continue
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            names.add(target.attr)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                names.add(item.target.id)
        return {name for name in names if not name.startswith("_")}
    raise AssertionError(f"{class_name} not found in {path.name}")


@pytest.mark.parametrize(
    ("path", "class_name", "protocol_name"),
    [
        ("agent/memory/store.py", "LTMStore", "MemoryStore"),
        ("agent/memory/context.py", "ContextManager", "ContextService"),
        ("agent/memory/palace.py", "MemoryPalace", "MemoryService"),
    ],
)
def test_no_reader_reaches_past_the_contract_through_attributes(
    path: str, class_name: str, protocol_name: str
):
    """The half of the seam the method scan could not see.

    It was green while six files read ``.staging`` and ``.store`` -- attributes no
    contract declared -- so a replacement could satisfy every method and still raise
    ``AttributeError`` on the first turn, or silently lose orphan-staging recovery
    (that one used ``getattr`` and merely returned 0).  A protocol a second
    implementation has to satisfy is closed under attribute access too.

    Reads whose receiver is ``self`` are not counted: outside this package ``self``
    belongs to some other class, and its own attributes are its own business.  The
    scan is otherwise name-based, which is the same limitation the method scan has --
    distinctive member names make it sufficient, and a false positive is fixed by
    renaming the other object's attribute or by declaring the member here.
    """
    import ast

    from agent.memory import contracts

    protocol = getattr(contracts, protocol_name)
    declared = set(_protocol_attributes(protocol)) | set(_protocol_members(protocol))
    members = _concrete_data_members(REPO_ROOT / path, class_name) - declared

    offenders: list[str] = []
    for source in sorted((REPO_ROOT / "agent").rglob("*.py")):
        relative = source.relative_to(REPO_ROOT)
        if str(relative).startswith("agent/memory/") or "__pycache__" in str(relative):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                if node.attr in members and ast.unparse(node.value) != "self":
                    if (str(relative), node.attr) not in _ATTRIBUTE_PEEK_ALLOWLIST:
                        offenders.append(
                            f"{relative}:{node.lineno} {ast.unparse(node.value)}.{node.attr}"
                        )
                continue
            # ``getattr(x, "staging", None)`` is the same dependency with the name in
            # a string, and it is how three call sites hid from the first version of
            # this scan (``spawn_session``, ``store``) -- so it is checked too.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in members
                and (str(relative), node.args[1].value) not in _ATTRIBUTE_PEEK_ALLOWLIST
            ):
                offenders.append(
                    f"{relative}:{node.lineno} getattr({ast.unparse(node.args[0])}, "
                    f"{node.args[1].value!r})"
                )
    assert not offenders, (
        f"these read a {class_name} member the {protocol_name} contract does not "
        "declare, so a replacement is not obliged to have it:\n  "
        + "\n  ".join(offenders)
    )


# ── the behaviour every context service must have ────────────────────────────


def run_context_contract(service: Any, *, store: Any) -> None:
    """Assert the semantics a :class:`ContextService` must have.

    Written as **relationships between the members**, not as a fixed sequence,
    because the second implementation exists to have a different *policy*: it never
    consolidates and never compacts.  So the suite asks "if you say there is work,
    can it be done and does the count follow" rather than "consolidate now".

    Importable, like :func:`run_store_contract`: another implementation's test calls
    it with its own service.
    """
    import asyncio

    from agent.core.transport import ModelEndpoint
    from agent.memory.models import LTMEntry

    endpoint = ModelEndpoint(client=None, api_format="openai")
    session_id = service.session_id
    assert session_id, "a service journals under a session id"

    # Safely callable at any time, on any implementation.
    service.mark_activity()
    service.on_memory_cleared()

    # Recording is idempotent per message id, and readable back through the store.
    assert service.begin_turn(user_content="hello", message_id="m-1") is True
    assert service.begin_turn(user_content="hello", message_id="m-1") is False
    assert (
        service.record_turn(
            user_content="hello",
            assistant_content="hi",
            message_id="m-1",
            assistant_message_id="m-1:run:1",
        )
        is True
    )
    assert (
        service.record_turn(
            user_content="hello",
            assistant_content="hi",
            message_id="m-1",
            assistant_message_id="m-1:run:1",
        )
        is False
    )
    turns = store.recent_conversation_turns(session_id=session_id)
    assert [(turn.role, turn.content) for turn in turns][:2] == [
        ("user", "hello"),
        ("assistant", "hi"),
    ]

    # What is remembered is reachable through retrieval.
    service.remember(
        LTMEntry(
            id="ctx-1",
            content="Prefers concise answers about widgets",
            importance=0.9,
            category="identity",
            entity="user",
            memory_type="preference",
            source_session=session_id,
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
    )
    assert "widgets" in service.retrieve_context("widgets")
    assert isinstance(service.retrieve_implicit_context("widgets"), str)

    # Staging is a policy, but its answers must be consistent with the contract.
    service.stage_turn("user", "a staged turn")
    assert isinstance(service.has_enough_staged(), bool)

    # Consolidation: enqueue → pending → consumed → empty.
    service.enqueue_consolidation("contract")
    assert service.pending_jobs() >= 1, "an enqueued job is pending"
    consumed = asyncio.run(
        service.process_one_job(endpoint, "contract-model", extractor=lambda *_: [])
    )
    assert consumed is True
    assert service.pending_jobs() == 0
    assert (
        asyncio.run(
            service.process_one_job(endpoint, "contract-model", extractor=lambda *_: [])
        )
        is False
    )
    assert isinstance(service.should_enqueue_consolidation(), bool)
    assert isinstance(service.should_session_end_sleep(), bool)

    # Compaction: trimming to budget is a promise; summarizing is not.
    messages = [{"role": "user", "content": "x" * 200} for _ in range(10)]

    def estimate(items: list[dict]) -> int:
        return sum(len(str(item.get("content", ""))) for item in items)

    budget = 300
    fitted = service.fit_to_budget(
        [dict(m) for m in messages], input_token_budget=budget, estimate_tokens=estimate
    )
    assert estimate(fitted) <= budget, "fit_to_budget must fit the budget it is given"
    compacted = service.compact_messages(
        [dict(m) for m in messages], input_token_budget=budget
    )
    assert isinstance(compacted, list)
    assert all("role" in item for item in compacted)
    assert isinstance(service.should_compact_messages(messages, budget), bool)

    # Sleep is the one member that needs a model.  A stub endpoint may make it fail;
    # what the contract requires is the shape.
    kept = asyncio.run(
        service.sleep([dict(m) for m in messages], endpoint, "contract-model")
    )
    assert isinstance(kept, list) and kept

    # Session fan-out, recovery and teardown.
    spawned = service.spawn_session("contract-child")
    assert spawned.session_id == "contract-child"
    assert spawned is not service
    assert isinstance(service.recover_orphan_staging(), int)
    assert isinstance(service.has_answer(session_id=session_id, message_id="m-1"), bool)
    service.close()
    service.close()  # idempotent


# ── the behaviour every store must have ──────────────────────────────────────


def run_store_contract(store: Any, *, session_id: str = "contract-session") -> None:
    """Assert the semantics a :class:`MemoryStore` must have.

    Importable on purpose: another backend's test calls this with its own store.
    Implementation detail is kept out -- nothing here asserts a table, a file
    name, a ranking score or an id format, only what a caller observes.
    """
    from agent.memory.models import FactAssertion, LTMEntry

    def entry(identifier: str, content: str, category: str = "identity") -> Any:
        return LTMEntry(
            id=identifier,
            content=content,
            importance=0.8,
            category=category,
            entity="user",
            memory_type="preference",
            source_session=session_id,
            created_at="2026-10-11",
            updated_at="2026-10-11",
        )

    # ── entries ─────────────────────────────────────────────────────────────
    store.add_entry(entry("c-1", "Prefers concise responses"))
    store.add_entries([entry("c-2", "Prefers tea over coffee")])

    stored = {item.id for item in store.read_entries("identity")}
    assert {"c-1", "c-2"} <= stored
    assert {"c-1", "c-2"} <= {item.id for item in store.all_entries()}
    assert "c-1" in {item.id for item in store.read_entries_for_entity("identity", "user")}
    assert "identity" in {category.name for category in store.list_categories()}
    assert store.dynamic_category_count() >= 0
    assert store.normalize_category_name("Identity") == store.normalize_category_name(
        " identity "
    )
    assert "c-1" in {item.id for item in store.search_entries("concise")}
    stats = store.corpus_stats(["concise"])
    assert stats is not None

    # ── manual notes and identity ───────────────────────────────────────────
    store.upsert_manual_note("notes", "user", "line one")
    note = store.read_manual_note("notes", "user")
    assert note is not None and "line one" in note.content
    store.upsert_manual_note("notes", "user", "line two", append=True)
    appended = store.read_manual_note("notes", "user")
    assert appended is not None
    assert "line one" in appended.content and "line two" in appended.content

    identity = store.set_identity(subject="assistant", name="Ada", role="helper")
    assert identity["name"] == "Ada", "set_identity reports what it applied"
    # ...and it is readable back: what is set has to be recallable, which is the
    # half a caller depends on.
    recalled = {
        (fact.predicate, str(fact.value))
        for fact in store.read_resolved_facts(subject="assistant")
    }
    assert ("name", "Ada") in recalled

    # ── facts ───────────────────────────────────────────────────────────────
    assertion = store.add_fact_assertion(
        FactAssertion(
            id="fact-1",
            subject="user",
            predicate="likes",
            value="tea",
            source_session=session_id,
        )
    )
    assert assertion.id == "fact-1"
    resolved = store.read_resolved_facts(subject="user")
    assert "tea" in {fact.value for fact in resolved}
    assert store.has_conflicted_fact("user", "likes", "global") is False

    # ── conversation history ────────────────────────────────────────────────
    written = store.write_conversation_exchange(
        session_id=session_id,
        user_content="what do I like?",
        assistant_content="tea, apparently",
        channel="test",
        message_id="msg-1",
        assistant_message_id="msg-1:run:abc:completion:1",
    )
    assert written.user_created is True and written.assistant_created is True
    turns = store.recent_conversation_turns(session_id=session_id)
    assert [(turn.role, turn.content) for turn in turns] == [
        ("user", "what do I like?"),
        ("assistant", "tea, apparently"),
    ]
    assert store.message_has_answer(session_id=session_id, message_id="msg-1") is True
    assert store.message_has_answer(session_id=session_id, message_id="msg-never") is False
    found = store.search_conversation_turns("tea", session_id=session_id)
    assert found and any("tea" in turn.content for turn in found)

    # A second *execution* of the same message is a second answer, and it must be
    # a row of its own: folding it into the first is how a delivered answer
    # disappeared from the record.
    second = store.write_conversation_exchange(
        session_id=session_id,
        user_content="what do I like?",
        assistant_content="tea (second run)",
        channel="test",
        message_id="msg-1",
        assistant_message_id="msg-1:run:def:completion:1",
    )
    assert second.user_created is False and second.assistant_created is True
    answers = [
        turn.content
        for turn in store.recent_conversation_turns(session_id=session_id)
        if turn.role == "assistant"
    ]
    assert "tea, apparently" in answers and "tea (second run)" in answers

    # ...while a retry of the *same* execution is a no-op.
    retry = store.write_conversation_exchange(
        session_id=session_id,
        user_content="what do I like?",
        assistant_content="tea (second run)",
        channel="test",
        message_id="msg-1",
        assistant_message_id="msg-1:run:def:completion:1",
    )
    assert retry.assistant_created is False
    assert len(
        [t for t in store.recent_conversation_turns(session_id=session_id) if t.role == "assistant"]
    ) == 2

    window, events, has_more = store.transcript_window(session_id=session_id, limit=10)
    assert {turn.content for turn in window} >= {"what do I like?", "tea, apparently"}
    assert isinstance(events, list) and has_more is False
    # A window is whole turns: asking for one turn must not return half of it.
    one_turn, _, _ = store.transcript_window(session_id=session_id, limit=1)
    assert {turn.role for turn in one_turn} == {"user", "assistant"}

    # ── working state, checkpoints, usage ───────────────────────────────────
    saved = store.save_session_working_state(
        session_id, {"status": "in_progress", "active_goal": "ship it"}
    )
    assert saved.session_id == session_id
    loaded = store.load_session_working_state(session_id)
    assert loaded is not None and loaded.state["active_goal"] == "ship it"
    assert store.load_session_working_state("no-such-session") is None

    store.save_provider_checkpoint(session_id, [{"role": "user", "content": "hi"}])
    checkpoint = store.load_provider_checkpoint(session_id)
    assert checkpoint is not None and checkpoint["messages"][0]["content"] == "hi"

    assert isinstance(store.usage_summary(session_id), dict)

    # ── runtime events ──────────────────────────────────────────────────────
    event = store.append_agent_event(
        session_id=session_id,
        event_type="turn_started",
        payload={"text_preview": "hi"},
        turn_id="msg-1",
    )
    assert event.event_type == "turn_started"
    recent = store.recent_agent_events(session_id=session_id)
    assert [item.event_type for item in recent] == ["turn_started"]
    assert recent[0].payload["text_preview"] == "hi"

    # ── maintenance and lifecycle ───────────────────────────────────────────
    assert isinstance(store.maintenance_snapshot(), str)
    store.apply_retention()  # must be safe to call at any time
    cleared = store.clear_persistent_memory()
    assert isinstance(cleared, dict)
    assert store.all_entries() == []

    store.close()
    store.close()  # idempotent: a caller that closes twice must not crash


def _make_sqlite_fts_store(tmp_path: Path) -> Any:
    from agent.memory.backends.sqlite_fts import SqliteFtsStore

    return SqliteFtsStore(
        context_dir=tmp_path / "context",
        memory_dir=tmp_path / "memory",
    )


#: Every implementation of ``MemoryStore``.  Adding one here is the whole cost of
#: claiming a second backend: the same suite runs against it unchanged.
BACKENDS: dict[str, Callable[[Path], Any]] = {
    "builtin": _make_store,
    "sqlite-fts": _make_sqlite_fts_store,
}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: pytest.FixtureRequest) -> Callable[[Path], Any]:
    return BACKENDS[request.param]


def test_every_backend_meets_the_behaviour_contract(backend, tmp_path):
    run_store_contract(backend(tmp_path))


#: Every implementation of ``ContextService``: the built-in manager, and the
#: record-only one whose *policy* differs (no consolidation, no compaction).
def _make_context_manager(tmp_path: Path) -> Any:
    from agent import ConsolidationEngine, ContextManager, LocalRetriever, StagingBuffer

    store = _make_store(tmp_path)
    return (
        ContextManager(
            store=store,
            retriever=LocalRetriever(),
            consolidation=ConsolidationEngine(store=store),
            staging=StagingBuffer(context_dir=tmp_path / "context", session_id="builtin"),
        ),
        store,
    )


def _make_record_only_context(tmp_path: Path) -> Any:
    from agent.memory.backends.record_only import RecordOnlyContextService

    store = _make_store(tmp_path)
    return RecordOnlyContextService(store, "record-only"), store


CONTEXT_BACKENDS: dict[str, Callable[[Path], Any]] = {
    "builtin": _make_context_manager,
    "record-only": _make_record_only_context,
}


@pytest.fixture(params=sorted(CONTEXT_BACKENDS))
def context_backend(request: pytest.FixtureRequest) -> Callable[[Path], Any]:
    return CONTEXT_BACKENDS[request.param]


def test_every_context_service_meets_the_behaviour_contract(context_backend, tmp_path):
    service, store = context_backend(tmp_path)
    run_context_contract(service, store=store)


def test_every_context_service_satisfies_the_protocol(context_backend, tmp_path):
    """Signature-and-attribute conformance, for every implementation."""
    from agent.memory.contracts import ContextService

    service, _store = context_backend(tmp_path)
    _assert_conforms(ContextService, service, context_backend.__name__)


def test_a_replacement_store_is_the_one_the_context_manager_gets(tmp_path, monkeypatch):
    """The seam is *used*, not merely declared.

    ``session_plugin`` builds the context manager from ``ctx.get("ltm_store")``, so
    whatever the container holds under that name is what the manager reads and
    writes.  If the plugin built its own store, replacing ``ltm_store`` would
    change nothing -- which is the failure this asserts against.
    """
    from agent import shared
    from agent.kernel.plugin import PluginHost
    from agent.kernel.service import ServiceRegistry
    from agent.memory.session_plugin import SessionPlugin

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()

    class _Replacement:
        """Stands in for another implementation: same surface, different object."""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, item: str) -> Any:
            return getattr(self.inner, item)

    replacement = _Replacement(_make_store(tmp_path))
    mapping: dict[str, Any] = {
        "cfg": {},
        "workspace_root": tmp_path,
        "ltm_store": replacement,
    }
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(SessionPlugin())

    manager = mapping["context_manager"]
    assert manager.store is replacement, "the manager took the replaced store"
    assert manager.store.inner.all_entries() == []
