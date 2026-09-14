"""The consent oracle for a turn that has no human behind it.

``UnattendedOutputSink`` is what a scheduled run talks to when a tool asks
"may I?".  It is not a permission system and it is not an approval bypass:
the envelope is enforced by configuration (see ``profiles``), and this sink
answers from a rule that needs no configuration at all -- *there is nobody
here, so nothing that needs a person is granted*.

Two consequences worth stating out loud, because they are the whole point:

1. **The grant side is deliberately empty.**  It is tempting to let a
   ``workspace_write`` run approve the requests that show up, which would make
   the sink a second gate whose verdict can contradict the first: a shell
   ``pip install`` (asked at *every* permission level, by design) or a plugin
   install (arbitrary code in the live agent process) would become
   auto-approved even under ``read_only``.  One gate, in the config, is what
   keeps "what may this run do?" answerable.  If a future profile needs to
   grant a category, it belongs in ``profiles`` as a config override, not
   here as an ``if``.

2. **Refusals are recorded instead of silent.**  Without a sink at all, a
   gate returned ``False`` and the run carried on with no trace of what it
   asked for.  A person reading the run afterwards could not tell a task that
   never needed approval from one whose every attempt was refused.  The audit
   makes that visible in the run's own output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.core.output import OutputSink

from .profiles import PermissionProfile


#: Bounded so that a long run cannot grow the audit without limit.  A run that
#: hits more refusals than this is not going to be diagnosed from the tail.
MAX_RECORDED_DECISIONS = 50
MAX_RECORDED_STATUS = 20


@dataclass
class ConsentDecision:
    """One "may I?" that reached the sink, and what it was told."""

    tool: str
    command: str
    risk_level: str
    reason: str

    def as_row(self) -> str:
        command = self.command.replace("|", "\\|").replace("\n", " ").strip()
        if len(command) > 160:
            command = command[:157] + "..."
        reason = self.reason.replace("|", "\\|").replace("\n", " ").strip()
        return f"| `{self.tool}` | {self.risk_level or '-'} | `{command}` | {reason or '-'} |"


@dataclass
class UnattendedAudit:
    """Everything a person needs to know about a run's envelope, afterwards."""

    task_id: str = ""
    run_id: str = ""
    profile: Optional[PermissionProfile] = None
    notes: list[str] = field(default_factory=list)
    decisions: list[ConsentDecision] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dropped_decisions: int = 0

    def note(self, text: str) -> None:
        if text:
            self.notes.append(text)

    def note_error(self, text: str) -> None:
        message = str(text or "").strip()
        if not message:
            return
        self.errors.append(message)
        del self.errors[:-MAX_RECORDED_STATUS]

    def note_consent_request(
        self, *, tool: str, command: str, risk_level: str, reason: str
    ) -> None:
        decision = ConsentDecision(
            tool=str(tool or "unknown"),
            command=str(command or ""),
            risk_level=str(risk_level or ""),
            reason=str(reason or ""),
        )
        if len(self.decisions) >= MAX_RECORDED_DECISIONS:
            self.dropped_decisions += 1
            return
        self.decisions.append(decision)

    @property
    def denied_count(self) -> int:
        return len(self.decisions) + self.dropped_decisions

    def has_findings(self) -> bool:
        return bool(self.decisions or self.dropped_decisions or self.errors or self.notes)

    def report(self) -> str:
        """Markdown section for the run's own output, or "" when silent.

        Returns "" for the ordinary case -- a run that never needed approval
        and whose envelope was applied cleanly -- so that everyday output does
        not grow a boilerplate appendix nobody reads.
        """
        if not self.has_findings():
            return ""
        lines: list[str] = ["## 无人值守运行说明", ""]
        if self.profile is not None:
            lines.append(
                f"- 权限档位：`{self.profile.key}`（{self.profile.label}）"
            )
        for note in self.notes:
            lines.append(f"- {note}")
        if self.decisions or self.dropped_decisions:
            lines += [
                "",
                "以下动作需要人工确认，本次运行无人可问，已自动拒绝：",
                "",
                "| 工具 | 风险 | 请求 | 说明 |",
                "| --- | --- | --- | --- |",
            ]
            lines += [decision.as_row() for decision in self.decisions]
            if self.dropped_decisions:
                lines.append(
                    f"| … | | | 另有 {self.dropped_decisions} 条同类型请求未逐条记录 |"
                )
            lines += [
                "",
                "拒绝是终态：同一动作在本次运行内重复请求也会被同样拒绝。",
            ]
        if self.errors:
            lines += ["", "运行期间的错误：", ""]
            lines += [f"- {error}" for error in self.errors]
        lines.append("")
        return "\n".join(lines)

    def write_report(self, path: Path) -> Optional[Path]:
        """Persist the section next to the run's artifacts.

        Returned rather than raised on failure: an audit that cannot be
        written must not turn a successful run into a failed one.
        """
        report = self.report()
        if not report:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding="utf-8")
        except OSError:
            return None
        return path


class UnattendedOutputSink(OutputSink):
    """Non-interactive sink that answers "may I?" with a recorded refusal."""

    #: No live UI is attached to a scheduled run; streams would be discarded.
    streaming = False

    def __init__(self, *, audit: Optional[UnattendedAudit] = None) -> None:
        self.audit = audit if audit is not None else UnattendedAudit()

    @property
    def interactive_confirmation(self) -> bool:
        # False is load-bearing, not a default.  Callers such as
        # ``memory_clear`` refuse outright unless a *person* can be asked, and
        # that is the correct answer here: irreversible destruction is not in
        # anybody's unattended envelope.
        return False

    async def on_tool_confirmation(
        self,
        name: str,
        *,
        command: str,
        risk_level: str,
        reason: str,
        confirmation_token: str,
        scope: Any,
    ) -> bool:
        self.audit.note_consent_request(
            tool=name,
            command=command,
            risk_level=risk_level,
            reason=reason,
        )
        return False

    def on_status(self, text: str, *, level: str = "info") -> None:
        if level in {"error", "warning"}:
            self.audit.note_error(str(text))

    def on_error(self, error: str) -> None:
        self.audit.note_error(str(error))


__all__ = [
    "ConsentDecision",
    "UnattendedAudit",
    "UnattendedOutputSink",
]
