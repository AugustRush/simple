"""Retrieval: planning a query, routing it to categories, ranking and fitting what comes back, and assembling the sections the prompt is built from. Moved out of ContextManager (see the remaining-big-files survey work order). The instance state it used -- the store, the retriever, the staging buffer, the route keywords -- arrives as explicit collaborators."""

from __future__ import annotations

from typing import Optional
from agent import shared
from . import text as _render
from .query_terms import _has_multi_intent_marker, _matched_fact_alias_terms
from ._helpers import _FACT_QUERY_PREDICATE_ALIASES, _FACT_QUERY_SUBJECT_ALIASES, _lexical_terms
from .models import LTMEntry, QueryPlan, ResolvedFact

__all__ = [
    "retrieve_context",
    "retrieve_history_context",
    "retrieve_ltm_context",
    "retrieve_resolved_fact_context",
    "rank_ltm_entries",
    "_route_categories",
    "_plan_query",
    "_fit_sections_to_budget",
    "_recent_session_context",
    "_recent_unconsolidated_context",
    "_assistant_identity_context",
    "_should_include_freeform_context",
    "ltm_candidates",
    "_is_episode_recall_query",
    "_targeted_fact_conflict",
    "_resolved_fact_candidates",
]


def _plan_query(
    query: str,
    *,
    route_keywords,
) -> QueryPlan:
    lexical_terms = tuple(_lexical_terms(query))
    if _is_episode_recall_query(query, route_keywords=route_keywords):
        return QueryPlan(
            query_type="event_recall",
            scope="current_session",
            lexical_terms=lexical_terms,
            allow_freeform_fallback=False,
        )

    lowered_query = str(query or "").strip().lower()
    subjects, subject_terms = _matched_fact_alias_terms(
        lowered_query,
        _FACT_QUERY_SUBJECT_ALIASES,
    )
    predicates, predicate_terms = _matched_fact_alias_terms(
        lowered_query,
        _FACT_QUERY_PREDICATE_ALIASES,
    )
    if not subjects and predicates:
        if "你" in lowered_query or "your" in lowered_query or "assistant" in lowered_query:
            subjects = ["assistant"]

    if not subjects and not predicates:
        return QueryPlan(
            query_type="freeform_context",
            scope="global",
            lexical_terms=lexical_terms,
            allow_freeform_fallback=True,
        )

    residual_terms = tuple(
        term
        for term in lexical_terms
        if term not in subject_terms and term not in predicate_terms
    )
    return QueryPlan(
        query_type="mixed" if residual_terms else "fact_lookup",
        scope="global",
        target_subjects=tuple(subjects),
        target_predicates=tuple(predicates),
        lexical_terms=lexical_terms,
        allow_freeform_fallback=True,
    )


def _is_episode_recall_query(
    query: str,
    *,
    route_keywords,
) -> bool:
    q = query.lower()

    explicit_keywords = route_keywords.get("episodes", ())
    if any(keyword in q for keyword in explicit_keywords):
        return True

    zh_phrases = (
        "对话历史",
        "聊天内容",
        "聊天记录",
        "刚才的对话",
        "刚刚的对话",
        "本次会话",
        "这次会话",
    )
    if any(phrase in q for phrase in zh_phrases):
        return True

    zh_recent = ("刚才", "刚刚", "上次", "之前")
    zh_conversation = ("聊", "说", "提", "问", "讨论", "对话", "聊天")
    if any(marker in q for marker in zh_recent) and any(
        marker in q for marker in zh_conversation
    ):
        return True

    en_phrases = (
        "conversation history",
        "chat history",
        "recent conversation",
        "earlier conversation",
        "previous conversation",
        "current conversation",
    )
    if any(phrase in q for phrase in en_phrases):
        return True

    en_recent = ("just", "earlier", "recently", "previously", "last time")
    en_conversation = ("talk", "chat", "discuss", "say", "ask", "conversation")
    if any(marker in q for marker in en_recent) and any(
        marker in q for marker in en_conversation
    ):
        return True
    return False


def _route_categories(
    query: str,
    *,
    route_keywords,
) -> list[str]:
    q = query.lower()
    routes: list[str] = []
    if _is_episode_recall_query(query, route_keywords=route_keywords):
        routes.append("episodes")
    for category, keywords in route_keywords.items():
        if category == "episodes":
            continue
        if any(keyword in q for keyword in keywords):
            if category == "projects":
                routes.extend(["projects", "tasks"])
            else:
                routes.append(category)
    seen = []
    for cat in routes:
        if cat not in seen:
            seen.append(cat)
    return seen


def _targeted_fact_conflict(
    plan: QueryPlan,
    *,
    store,
) -> bool:
    if not plan.target_subjects or not plan.target_predicates:
        return False
    for subject in plan.target_subjects:
        for predicate in plan.target_predicates:
            if store.has_conflicted_fact(subject, predicate, plan.scope):
                return True
    return False


def _should_include_freeform_context(
    *,
    query: str,
    plan: QueryPlan,
    has_fact_hits: bool,
    store,
) -> bool:
    if plan.query_type == "event_recall":
        return False
    if _targeted_fact_conflict(plan, store=store):
        return False
    if plan.query_type == "fact_lookup":
        return not has_fact_hits
    if plan.query_type == "mixed":
        if not has_fact_hits:
            return True
        return _has_multi_intent_marker(query)
    return True


def _resolved_fact_candidates(
    query: str,
    *,
    plan: Optional[QueryPlan] = None,
    top_k: int = shared.RETRIEVAL_TOP_K,
    store,
    route_keywords,
) -> list[ResolvedFact]:
    plan = plan or _plan_query(query, route_keywords=route_keywords)
    if plan.query_type not in {"fact_lookup", "mixed"}:
        return []
    facts = store.read_resolved_facts()
    if not facts:
        return []

    candidates: list[ResolvedFact] = []
    for fact in facts:
        if plan.target_subjects and fact.subject not in plan.target_subjects:
            continue
        if plan.target_predicates and fact.predicate not in plan.target_predicates:
            continue
        candidates.append(fact)
    if not candidates:
        return []

    query_terms = set(plan.lexical_terms)

    def score(fact: ResolvedFact) -> float:
        value_terms = set(_lexical_terms(_render._format_fact_value(fact.value)))
        total = float(fact.confidence or 0.0)
        if fact.subject in plan.target_subjects:
            total += 3.0
        if fact.predicate in plan.target_predicates:
            total += 3.0
        total += 0.25 * len(query_terms & value_terms)
        return total

    ranked = sorted(candidates, key=score, reverse=True)
    return [fact for fact in ranked[:top_k] if score(fact) > 0]


def retrieve_resolved_fact_context(
    query: str,
    *,
    top_k: int = shared.RETRIEVAL_TOP_K,
    plan: Optional[QueryPlan] = None,
    title: str = "## Resolved Facts",
    store,
    route_keywords,
) -> str:
    facts = _resolved_fact_candidates(query, plan=plan, top_k=top_k, store=store, route_keywords=route_keywords)
    if not facts:
        return ""
    lines = [title]
    for fact in facts:
        lines.append(
            f"- {fact.subject}.{fact.predicate} = {_render._format_fact_value(fact.value)}"
        )
    return "\n".join(lines)


def ltm_candidates(
    query: str,
    top_k: int = shared.RETRIEVAL_TOP_K,
    *,
    store,
    route_keywords,
    staging,
    project_scope,
) -> list[LTMEntry]:
    """Stage 1: the candidate set the re-ranker is allowed to choose from.

        Named and exposed because it is a hard ceiling on retrieval quality —
        an entry that stage 1 misses cannot be recovered by any amount of
        stage-2 ranking. Measuring the two stages separately is what tells you
        which one to work on; without the split, a recall failure and a
        ranking failure look identical from the outside.
        """
    scopes = ["global", f"session:{staging.session_id}"]
    if project_scope:
        scopes.insert(1, project_scope)
    candidates = store.search_entries(
        query,
        categories=None,
        limit=top_k * 6,
        scopes=scopes,
    )
    if not candidates and _is_episode_recall_query(query, route_keywords=route_keywords):
        candidates = store.read_entries("episodes", scopes=scopes)[: top_k * 3]
    return candidates


def rank_ltm_entries(
    query: str | list[str],
    top_k: int = shared.RETRIEVAL_TOP_K,
    *,
    store,
    route_keywords,
    staging,
    retriever,
    project_scope,
) -> list[LTMEntry]:
    """Return the top-K entries for *query*, highest-scoring first.

        *query* may be several phrasings.  This is the single highest-value
        thing the retrieval layer can offer, because the dominant failure is
        not ranking — it is that one unmodified user message is a bad query
        against a lexical index.  Measured on the eval set, 7 of 7 stage-1
        misses were recoverable by re-asking in the memory's own wording or
        language ("我在哪家公司做什么工作" finds nothing; "August 大疆 iOS"
        finds it).  The information was always reachable; the query was the
        problem.

        Candidates are unioned across phrasings and each entry keeps its best
        score, rather than concatenating the phrasings into one string —
        concatenation dilutes IDF across terms that belong to different
        formulations and ends up ranking worse than any single one of them.

        Two-stage retrieval:
          1. :meth:`ltm_candidates` fetches a broad candidate set via FTS5.
          2. LocalRetriever re-ranks candidates by relevance, using document
             frequencies measured over the **whole store**.
          3. Routed categories receive a small score bonus rather than hard
             filtering.
        """
    queries = [query] if isinstance(query, str) else list(query)
    queries = [str(q).strip() for q in queries if str(q).strip()]
    if not queries:
        return []

    scopes = ["global", f"session:{staging.session_id}"]
    if project_scope:
        scopes.insert(1, project_scope)
    best: dict[str, float] = {}
    entries: dict[str, LTMEntry] = {}
    routed: set[str] = set()

    for one in queries:
        candidates = ltm_candidates(one, top_k, store=store, route_keywords=route_keywords, staging=staging, project_scope=project_scope)
        if not candidates:
            continue
        routed.update(_route_categories(one, route_keywords=route_keywords))
        corpus = store.corpus_stats(
            retriever.tokenize(one), scopes=scopes
        )
        for entry, score in retriever.score(one, candidates, corpus):
            if score > best.get(entry.id, 0.0):
                best[entry.id] = score
                entries[entry.id] = entry

    if not best:
        return []
    scored = [
        (entries[entry_id], score * (1.15 if entries[entry_id].category in routed else 1.0))
        for entry_id, score in best.items()
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [entry for entry, score in scored[:top_k] if score > 0]


def retrieve_ltm_context(
    query: str | list[str],
    top_k: int = shared.RETRIEVAL_TOP_K,
    token_budget: Optional[int] = None,
    *,
    store,
    route_keywords,
    staging,
    retriever,
    consolidation,
    project_scope,
) -> str:
    """Format the top-K relevant LTM entries as an injectable string.

        Ranking lives in :meth:`rank_ltm_entries`; this method only decides
        how much of the result fits in *token_budget*.
        """
    top = rank_ltm_entries(query, top_k, store=store, route_keywords=route_keywords, staging=staging, retriever=retriever, project_scope=project_scope)
    if not top:
        return ""
    header = "## Retrieved Context (from long-term memory)"
    lines = [header]
    if token_budget is None or token_budget <= 0:
        for e in top:
            anchor = f"{e.category}/{e.entity}" if e.entity else e.category
            lines.append(f"- [{anchor}] {e.content}")
        return "\n".join(lines)
    # Fit at entry granularity.  Dropping the whole section because one
    # verbose memory overflowed would discard the other four for free, so
    # spend the budget on the highest-scoring entries that fit and say how
    # many were left out.
    estimate = consolidation.estimate_tokens
    used = estimate([{"role": "system", "content": header}])
    omitted = 0
    for e in top:
        anchor = f"{e.category}/{e.entity}" if e.entity else e.category
        line = f"- [{anchor}] {e.content}"
        cost = estimate([{"role": "system", "content": line}])
        if used + cost > token_budget:
            omitted += 1
            continue
        lines.append(line)
        used += cost
    if omitted:
        lines.append(f"- [{omitted} lower-ranked memor(ies) omitted: over budget]")
    return "\n".join(lines) if len(lines) > 1 else ""


def _recent_session_context(
    limit: int = shared.RECENT_SESSION_TURNS,
    *,
    exclude_message_id: str = "",
    store,
    staging,
) -> str:
    """Return the most recent staged turns for explicit current-session recall."""
    staged = staging.read_last(limit * 4)
    if not staged:
        # Staging may be empty after a restart (sleep archived turns to
        # conversation_turns).  Fall back to the durable store so the
        # agent still sees recent conversation history.
        if staging.session_id == "cli":
            turns = store.recent_conversation_turns(
                channel="cli",
                exclude_message_id=exclude_message_id,
                limit=limit,
            )
        else:
            turns = store.recent_conversation_turns(
                session_id=staging.session_id,
                exclude_message_id=exclude_message_id,
                limit=limit,
            )
        if not turns:
            return ""
        lines = ["## Previous Session (restored from history)"]
        for turn in turns:
            content = turn.content.strip()
            if content:
                lines.append(f"- {turn.role.upper()}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""
    lines = ["## Current Session (not yet consolidated)"]
    for msg in staged[-limit:]:
        role = str(msg.get("role", "unknown")).upper()
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"- {role}: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def retrieve_history_context(
    query: str,
    limit: int = shared.RECENT_SESSION_TURNS,
    *,
    exclude_message_id: str = "",
    store,
    route_keywords,
    staging,
) -> str:
    """Return durable event-history evidence when the query asks for it."""
    if not _is_episode_recall_query(query, route_keywords=route_keywords):
        return ""
    if staging.session_id == "cli":
        turns = store.recent_conversation_turns(
            channel="cli",
            exclude_message_id=exclude_message_id,
            limit=limit,
        )
    else:
        turns = store.search_conversation_turns(
            query,
            session_id=staging.session_id,
            exclude_message_id=exclude_message_id,
            limit=limit,
        )
        if not turns:
            turns = store.recent_conversation_turns(
                session_id=staging.session_id,
                exclude_message_id=exclude_message_id,
                limit=limit,
            )
    if not turns:
        return ""
    lines = ["## Conversation History"]
    for turn in turns:
        content = turn.content.strip()
        if content:
            lines.append(f"- {turn.role.upper()}: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _recent_unconsolidated_context(
    current_messages: Optional[list[dict]] = None,
    limit: int = shared.RECENT_SESSION_TURNS,
    *,
    staging,
) -> str:
    staged = staging.read_last(limit * 4)
    if not staged:
        return ""
    if current_messages is None:
        return ""
    # Every string the model has already been shown, kept whole: what the
    # staged text is compared *against* is a delimited run inside one of
    # these, so they must not be pre-split (see `_shows_text`).
    visible_contents: list[str] = []
    for msg in current_messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            visible_contents.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            # Tolerated on purpose, unlike the protocol paths: this only
            # collects text to compare against, so an unreadable block costs
            # at worst one redundant re-injection -- never a lost tool
            # result.  It is not a second tolerance to maintain: the
            # protocol reads all share `_content_blocks`, which raises.
            if not isinstance(block, dict):
                continue
            visible_contents.append(
                str(block.get("text", "") or block.get("content", ""))
            )
    missing = []
    for msg in staged:
        staged_text = str(msg.get("content", "")).strip()
        if not staged_text:
            continue
        if any(
            _render._shows_text(content, staged_text)
            for content in visible_contents
        ):
            continue
        missing.append(msg)
    if not missing:
        return ""
    lines = ["## Current Session (not yet consolidated)"]
    for msg in missing[-limit:]:
        role = str(msg.get("role", "unknown")).upper()
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"- {role}: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _assistant_identity_context(
    limit: int = 3,
    *,
    store,
) -> str:
    name_conflict = store.has_conflicted_fact("assistant", "name")
    role_conflict = store.has_conflicted_fact("assistant", "role")
    facts = [
        fact
        for fact in store.read_resolved_facts(subject="assistant")
        if fact.predicate in {"name", "role", "identity_note"}
        and not (name_conflict and fact.predicate in {"name", "identity_note"})
        and not (role_conflict and fact.predicate == "role")
    ]
    if facts:
        lines = ["## Assistant Identity"]
        for fact in facts[: max(1, limit)]:
            lines.append(
                f"- {fact.subject}.{fact.predicate} = {_render._format_fact_value(fact.value)}"
            )
        return "\n".join(lines)
    if name_conflict or role_conflict:
        return ""
    entries = store.read_entries_for_entity("identity", "assistant")
    if not entries:
        return ""
    lines = ["## Assistant Identity"]
    for entry in entries[: max(1, limit)]:
        content = entry.content.strip()
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def retrieve_context(
    query: str | list[str],
    top_k: int = shared.RETRIEVAL_TOP_K,
    *,
    exclude_message_id: str = "",
    store,
    route_keywords,
    staging,
    retriever,
    consolidation,
    project_scope,
) -> str:
    """Return explicit context lookup results across active session and LTM.

        *query* may be several phrasings; see :meth:`rank_ltm_entries` for why
        that is the highest-value knob in this layer.  The session/fact
        lookups use the first phrasing (they match structured subjects rather
        than free text, so extra wordings add nothing), while the free-text
        LTM search unions all of them.
        """
    queries = [query] if isinstance(query, str) else list(query)
    queries = [str(q).strip() for q in queries if str(q).strip()]
    if not queries:
        return ""
    query = queries[0]
    plan = _plan_query(query, route_keywords=route_keywords)
    sections = []
    history = retrieve_history_context(
        query,
        exclude_message_id=exclude_message_id, store=store, route_keywords=route_keywords, staging=staging
    )
    if history:
        sections.append(history)
    recent = (
        ""
        if history
        else _recent_session_context(exclude_message_id=exclude_message_id, store=store, staging=staging)
    )
    if recent:
        sections.append(recent)
    facts = retrieve_resolved_fact_context(query, top_k=top_k, plan=plan, store=store, route_keywords=route_keywords)
    if facts:
        sections.append(facts)
    include_freeform = _should_include_freeform_context(
        query=query,
        plan=plan,
        has_fact_hits=bool(facts), store=store
    )
    ltm = (
        retrieve_ltm_context(queries, top_k=top_k, store=store, route_keywords=route_keywords, staging=staging, retriever=retriever, consolidation=consolidation, project_scope=project_scope)
        if include_freeform
        else ""
    )
    if ltm:
        sections.append(ltm)
    return "\n\n".join(sections)


def _fit_sections_to_budget(
    sections: list[str],
    token_budget: Optional[int],
    *,
    consolidation,
) -> str:
    """Join sections, dropping whole ones that exceed the budget.

        Sections arrive in priority order (identity, working state, recent
        turns, facts, then free-form LTM), so shedding from the tail drops the
        least important material first.  Dropping a whole section beats
        truncating mid-sentence: half a remembered fact still reads as a
        complete one, which is worse than its absence.  A notice records what
        was shed so the omission is visible rather than silent.
        """
    if token_budget is None or token_budget <= 0 or not sections:
        return "\n\n".join(sections)
    estimate = consolidation.estimate_tokens
    kept: list[str] = []
    dropped = 0
    used = 0
    for section in sections:
        cost = estimate([{"role": "system", "content": section}])
        if used + cost > token_budget:
            dropped += 1
            continue
        kept.append(section)
        used += cost
    if dropped:
        kept.append(
            f"[{dropped} lower-priority context section(s) omitted to fit the "
            f"{token_budget}-token retrieval budget]"
        )
    return "\n\n".join(kept)
