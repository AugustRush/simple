"""RuleStore — persists and manages learned behavioral rules.

Rules are derived from repeated correction patterns and stored in
``~/.agent/rl/rules.jsonl``.  Each rule has a lifecycle:

  probation  →  active  →  retired

A rule starts in *probation*.  After ``EVAL_THRESHOLD`` applications it is
automatically evaluated: if the post-rule correction rate dropped compared
to the pre-rule baseline, it is promoted to *active*; otherwise it is
*retired*.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Minimum number of applications before a probation rule is evaluated.
EVAL_THRESHOLD = 10
# Correction-rate improvement required to promote a rule (absolute drop).
IMPROVEMENT_DELTA = 0.05

_RL_DIR = Path.home() / ".agent" / "rl"
_RULES_FILE = _RL_DIR / "rules.jsonl"


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

    def __init__(self, rules_file: Optional[Path] = None) -> None:
        self._path = rules_file or _RULES_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)

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
                pass  # skip corrupt lines
        return rules

    def _save(self, rules: list[BehaviorRule]) -> None:
        self._path.write_text(
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

    def get_active_rule_ids(self) -> list[str]:
        """Return IDs of all *active* and *probation* rules."""
        return [r.id for r in self._load() if r.status in ("active", "probation")]

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
                    r.status = (
                        "active" if improvement >= IMPROVEMENT_DELTA else "retired"
                    )
                break
        self._save(rules)

    def get_stats(self) -> dict:
        rules = self._load()
        by_status: dict[str, int] = {}
        for r in rules:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {"total": len(rules), "by_status": by_status}
