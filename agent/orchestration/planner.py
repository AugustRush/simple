from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.skills.catalog import SkillCatalog


@dataclass(frozen=True)
class OrchestrationDecision:
    mode: str
    reason: str = ""
    guidance: str = ""
    max_rendezvous_rounds: int = 2


@dataclass(frozen=True)
class OrchestrationPlanner:
    default_mode: str = "direct"
    parallel_keywords: tuple[str, ...] = field(default_factory=tuple)
    pipeline_leading_keywords: tuple[str, ...] = field(default_factory=tuple)
    pipeline_followup_keywords: tuple[str, ...] = field(default_factory=tuple)
    pipeline_keywords: tuple[str, ...] = field(default_factory=tuple)
    rendezvous_keywords: tuple[str, ...] = field(default_factory=tuple)
    max_rendezvous_rounds: int = 2
    policy_skill_id: str = "multi-agent-orchestration"

    @classmethod
    def from_skill_catalog(cls, skill_catalog: SkillCatalog | None) -> "OrchestrationPlanner":
        if skill_catalog is None:
            return cls()
        bundle = skill_catalog.get("multi-agent-orchestration")
        if bundle is None:
            return cls()
        metadata = bundle.metadata or {}
        return cls(
            default_mode=str(metadata.get("default-mode", "direct") or "direct"),
            parallel_keywords=cls._tupled(metadata.get("parallel-keywords")),
            pipeline_leading_keywords=cls._tupled(
                metadata.get("pipeline-leading-keywords")
            ),
            pipeline_followup_keywords=cls._tupled(
                metadata.get("pipeline-followup-keywords")
            ),
            pipeline_keywords=cls._tupled(metadata.get("pipeline-keywords")),
            rendezvous_keywords=cls._tupled(metadata.get("rendezvous-keywords")),
            max_rendezvous_rounds=max(
                1, int(metadata.get("max-rendezvous-rounds", 2) or 2)
            ),
            policy_skill_id=bundle.id,
        )

    @staticmethod
    def _tupled(value: Any) -> tuple[str, ...]:
        if not value:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value if str(item).strip())
        return (str(value),)

    @staticmethod
    def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def decide(
        self,
        user_message: str,
        *,
        tools_enabled: bool,
        has_spawn_agent: bool,
    ) -> OrchestrationDecision:
        if not tools_enabled or not has_spawn_agent:
            return OrchestrationDecision(mode="direct", reason="spawn unavailable")
        lowered = user_message.lower()
        if self.rendezvous_keywords and self._contains_any(lowered, self.rendezvous_keywords):
            return OrchestrationDecision(
                mode="rendezvous",
                reason=f"user message matched rendezvous keywords: "
                       f"{self._matched_keywords(lowered, self.rendezvous_keywords)}",
                max_rendezvous_rounds=self.max_rendezvous_rounds,
            )
        pipeline_hits = self._matched_keywords(
            lowered,
            self.pipeline_keywords + self.pipeline_leading_keywords + self.pipeline_followup_keywords,
        )
        if pipeline_hits:
            return OrchestrationDecision(
                mode="pipeline",
                reason=f"user message matched pipeline keywords: {pipeline_hits}",
            )
        if self.parallel_keywords and self._contains_any(lowered, self.parallel_keywords):
            return OrchestrationDecision(
                mode="parallel",
                reason=f"user message matched parallel keywords: "
                       f"{self._matched_keywords(lowered, self.parallel_keywords)}",
            )
        return OrchestrationDecision(
            mode="explicit",
            reason="no keyword match; runtime derives mode from explicit subtask plan",
            max_rendezvous_rounds=self.max_rendezvous_rounds,
        )

    @staticmethod
    def _matched_keywords(text: str, keywords: tuple[str, ...]) -> str:
        hits = [kw for kw in keywords if kw.lower() in text]
        return ", ".join(hits[:5])
