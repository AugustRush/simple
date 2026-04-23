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
        del user_message
        if not tools_enabled or not has_spawn_agent:
            return OrchestrationDecision(mode="direct", reason="spawn unavailable")
        return OrchestrationDecision(
            mode="explicit",
            reason="runtime derives mode from explicit subtask plan",
            max_rendezvous_rounds=self.max_rendezvous_rounds,
        )
