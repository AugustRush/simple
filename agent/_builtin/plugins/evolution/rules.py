"""RuleStore — persists and manages learned behavioral rules.

Rules are derived from repeated correction patterns and stored in
``~/.agent/rl/rules.jsonl``.  Each rule has a lifecycle:

  probation  →  active  →  retired

A rule starts in *probation*.  After ``EVAL_THRESHOLD`` applications it is
automatically evaluated: if the post-rule correction rate dropped compared
to the pre-rule baseline, it is promoted to *active*; otherwise it is
*retired*.

Verdicts are written to ``~/.agent/rl/rule-decisions.jsonl``, append-only, at
the moment ``status`` flips.  The counters on the rule survive the flip, so the
rates can be recomputed afterwards -- but the *decision* cannot.  Which
threshold was applied and which side of it the rule landed on are known only at
that instant, and a rule that was tried and retired has to stay legible: the
proposer consults this history (``retired_rule_texts``) so it does not spend
another round re-deriving a rule that already failed.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent import shared
from typing import Iterable, Optional

from agent.lexical import keyword_terms

# Minimum number of applications before a probation rule is evaluated.
EVAL_THRESHOLD = 10
# Correction-rate improvement required to promote a rule (absolute drop).
IMPROVEMENT_DELTA = 0.05

def _rules_file() -> Path:
    """Resolve the rules path at call time, never at import time.

    ``shared.RL_DIR`` is rewritten by ``--name`` (multi-instance isolation).
    Binding it into a module constant captures whichever home happened to be
    active when this module was first imported, which for a plugin is before
    the CLI has parsed ``--name`` — so every named instance silently shared
    the default instance's rules.
    """
    from agent import shared

    return shared.RL_DIR / "rules.jsonl"


def _decisions_file() -> Path:
    """Resolve the decision-log path at call time, for the same reason as above."""
    from agent import shared

    return shared.RL_DIR / "rule-decisions.jsonl"

_RULE_STOPWORDS = {
    "always",
    "before",
    "after",
    "when",
    "then",
    "with",
    "from",
    "that",
    "this",
    "into",
    "your",
    "have",
    "been",
    "will",
    "must",
    "should",
    "总是",
    "应该",
    "需要",
    "之前",
    "之后",
    "如果",
    "这个",
    "那个",
    "时候",
    "请先",
}

_TOKEN_ALIASES = {
    "changes": "diff",
    "change": "diff",
    "diff": "diff",
    "display": "show",
    "displaying": "show",
    "file": "file",
    "files": "file",
    "modified": "modify",
    "modifies": "modify",
    "modify": "modify",
    "modifying": "modify",
    "show": "show",
    "showing": "show",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "error": "error",
    "errors": "error",
    "报错": "error",
    "错误": "error",
    "差异": "diff",
    "变更": "diff",
    "展示": "show",
    "显示": "show",
    "文件": "file",
    "测试": "test",
    "修改": "modify",
    "改动": "modify",
}

#: How much keyword overlap with an already-retired rule makes a new proposal
#: *the same* proposal.  Deliberately strict: missing a restatement costs one
#: wasted extraction round, while a false match means a genuinely new rule can
#: never be learned at all.
REPEAT_SIMILARITY = 0.8

_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalized_rule(text: str) -> str:
    """Case-folded, punctuation-free form used to compare two rules."""
    return " ".join(_NON_WORD_RE.sub(" ", str(text or "").casefold()).split())


def _rule_terms(text: str) -> set[str]:
    """Keyword terms of a rule, under the store's own stopword/alias table."""
    return keyword_terms(
        text,
        stopwords=_RULE_STOPWORDS,
        aliases=_TOKEN_ALIASES,
        latin_min_len=3,
    )


def is_repeat_of_retired(
    rule_text: str,
    retired_texts: Iterable[str],
    *,
    threshold: float = REPEAT_SIMILARITY,
) -> bool:
    """Whether *rule_text* restates a rule that was already tried and retired.

    Two rules count as the same when they normalise to the same string, or when
    their keyword terms overlap by at least *threshold* (Jaccard).  Terms rather
    than characters, because the same rule gets re-derived in different words --
    "show a diff before modifying files" and "always display the changes before
    editing a file" are one rule written twice, and a character-level distance
    would call them different.
    """
    candidate = _normalized_rule(rule_text)
    if not candidate:
        return False
    candidate_terms = _rule_terms(candidate)
    for retired in retired_texts:
        previous = _normalized_rule(retired)
        if not previous:
            continue
        if candidate == previous:
            return True
        if not candidate_terms:
            continue
        previous_terms = _rule_terms(previous)
        union = candidate_terms | previous_terms
        if union and len(candidate_terms & previous_terms) / len(union) >= threshold:
            return True
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class BehaviorRule:
    id: str
    rule: str
    source_failures: list[str]
    created_at: str
    applications: int = 0
    corrections_after: int = 0  # correction events while rule was active
    pre_correction_rate: float = 0.0  # estimated rate before rule
    status: str = "probation"  # "probation" | "active" | "retired"

    @property
    def post_correction_rate(self) -> float:
        if self.applications == 0:
            return 0.0
        return self.corrections_after / self.applications


class RuleStore:
    """Persistent store for learned behavioral rules."""

    def __init__(
        self,
        rules_file: Optional[Path] = None,
        decisions_file: Optional[Path] = None,
    ) -> None:
        self._path = rules_file or _rules_file()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if decisions_file is not None:
            self._decisions_path = decisions_file
        elif rules_file is not None:
            # A store pointed at an explicit file keeps its audit trail beside
            # it.  Falling back to the shared path here would let a test -- or a
            # named instance -- write verdicts into the default home.
            self._decisions_path = rules_file.parent / "rule-decisions.jsonl"
        else:
            self._decisions_path = _decisions_file()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> list[BehaviorRule]:
        if not self._path.exists():
            return []
        rules: list[BehaviorRule] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rules.append(BehaviorRule(**data))
            except Exception:
                # Skipping is right — one bad line must not cost every rule —
                # but it has to be audible.  A silent skip makes a learned rule
                # vanish with no way to tell that from never having learned it.
                logging.getLogger("agent").warning(
                    "dropping unreadable rule line in %s", self._path, exc_info=True
                )
        return rules

    def _save(self, rules: list[BehaviorRule]) -> None:
        # Durable primitive: this rewrites the whole rule set in place, so a
        # truncating write can drop every rule at once.
        shared._atomic_write_text(
            self._path,
            "\n".join(json.dumps(asdict(r), ensure_ascii=False) for r in rules) + "\n",
            encoding="utf-8",
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def add_rule(
        self,
        rule_text: str,
        source_failures: list[str],
        pre_correction_rate: float = 0.0,
    ) -> BehaviorRule:
        """Persist a new rule with *probation* status and return it."""
        rule = BehaviorRule(
            id=_new_id(),
            rule=rule_text,
            source_failures=source_failures,
            created_at=_now(),
            pre_correction_rate=pre_correction_rate,
        )
        rules = self._load()
        rules.append(rule)
        self._save(rules)
        return rule

    def get_active_rules(self) -> list[str]:
        """Return rule texts for all *active* and *probation* rules."""
        return [r.rule for r in self._load() if r.status in ("active", "probation")]

    def get_prompt_rules(self) -> list[str]:
        """Return verified rules that are safe to inject into the system prompt."""
        return [r.rule for r in self._load() if r.status == "active"]

    def get_active_rule_ids(self) -> list[str]:
        """Return IDs of all *active* and *probation* rules."""
        return [r.id for r in self._load() if r.status in ("active", "probation")]

    @staticmethod
    def _keywords(text: str) -> set[str]:
        return _rule_terms(text)

    def retired_rule_texts(self) -> list[str]:
        """Rules that were tried and rejected, for the proposer to steer around.

        Retired rules are kept rather than deleted precisely so this list can
        exist: a rule that lost its evaluation is evidence about what does not
        work here, and the only way to use that evidence is to still have it.
        """
        return [r.rule for r in self._load() if r.status == "retired"]

    def is_repeat_of_retired(
        self, rule_text: str, *, threshold: float = REPEAT_SIMILARITY
    ) -> bool:
        """Whether *rule_text* restates a rule this store already retired."""
        return is_repeat_of_retired(
            rule_text, self.retired_rule_texts(), threshold=threshold
        )

    def get_relevant_rule_ids(self, context_text: str) -> list[str]:
        """Return active/probation rule IDs relevant to the current turn context."""
        context_terms = self._keywords(context_text)
        relevant: list[str] = []
        for rule in self._load():
            if rule.status not in ("active", "probation"):
                continue
            rule_terms = self._keywords(rule.rule)
            if not rule_terms or len(rule_terms) <= 2 or rule_terms & context_terms:
                relevant.append(rule.id)
        return relevant

    def record_application(self, rule_id: str, was_corrected: bool) -> None:
        """Increment application counter; optionally record a correction event.

        P0-6 fix: single load → mutate → evaluate → single save.
        """
        rules = self._load()
        for r in rules:
            if r.id == rule_id:
                r.applications += 1
                if was_corrected:
                    r.corrections_after += 1
                # Inline evaluation (no double read-write).
                if r.status == "probation" and r.applications >= EVAL_THRESHOLD:
                    improvement = r.pre_correction_rate - r.post_correction_rate
                    verdict = (
                        "active" if improvement >= IMPROVEMENT_DELTA else "retired"
                    )
                    self._record_decision(r, verdict, improvement)
                    r.status = verdict
                break
        self._save(rules)

    def _record_decision(
        self, rule: BehaviorRule, verdict: str, improvement: float
    ) -> None:
        """Append the verdict and the evidence that produced it.

        Called *before* ``status`` is flipped, so the entry can name both ends
        of the transition.  A failure to write must not cost the rule update --
        the counters on the rule are the primary record and this log is the
        explanation -- so it is reported and swallowed rather than raised.
        """
        entry = {
            "at": _now(),
            "rule_id": rule.id,
            "rule": rule.rule,
            "from": rule.status,
            "to": verdict,
            "applications": rule.applications,
            "corrections_after": rule.corrections_after,
            "pre_correction_rate": round(rule.pre_correction_rate, 4),
            "post_correction_rate": round(rule.post_correction_rate, 4),
            "improvement": round(improvement, 4),
            "required_improvement": IMPROVEMENT_DELTA,
            "source_failures": list(rule.source_failures),
        }
        try:
            self._decisions_path.parent.mkdir(parents=True, exist_ok=True)
            with self._decisions_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logging.getLogger("agent").warning(
                "could not append rule decision for %s to %s",
                rule.id,
                self._decisions_path,
                exc_info=True,
            )

    def get_stats(self) -> dict:
        rules = self._load()
        by_status: dict[str, int] = {}
        for r in rules:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {"total": len(rules), "by_status": by_status}
