"""Core orchestration exports."""

from .attachments import (
    MessageAttachment,
    attachment_kind_for_mime,
    format_attachment_context,
)
from .output import CliOutputSink, OutputSink

__all__ = [
    "AgentContext",
    "AgentCore",
    "AgentResult",
    "BaseAgent",
    "CliOutputSink",
    "MessageAttachment",
    "OutputSink",
    "RalphTask",
    "RuntimeComponents",
    "RuntimeEvent",
    "RuntimeSessionState",
    "SubAgentProgressEvent",
    "TurnExecution",
    "TurnInput",
    "TurnResult",
    "TurnRunner",
    "attachment_kind_for_mime",
    "format_attachment_context",
]


def __getattr__(name: str):
    if name in {
        "AgentContext",
        "AgentResult",
        "BaseAgent",
        "SubAgentProgressEvent",
    }:
        from . import agent as core_agent

        return getattr(core_agent, name)
    if name == "RalphTask":
        import agent as agent_module

        return agent_module.RalphTask
    if name in {
        "AgentCore",
        "RuntimeComponents",
        "RuntimeEvent",
        "RuntimeSessionState",
        "TurnExecution",
        "TurnInput",
        "TurnResult",
        "TurnRunner",
    }:
        from agent import runtime

        return getattr(runtime, name)
    raise AttributeError(name)
