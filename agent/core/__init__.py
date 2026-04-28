"""Core orchestration exports."""

from .output import CliOutputSink, OutputSink

__all__ = [
    "AgentContext",
    "AgentResult",
    "BaseAgent",
    "CliOutputSink",
    "OutputSink",
    "RalphTask",
    "RuntimeComponents",
    "RuntimeSessionState",
    "SubAgentProgressEvent",
    "TurnInput",
    "TurnResult",
    "TurnRunner",
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
        "RuntimeComponents",
        "RuntimeSessionState",
        "TurnInput",
        "TurnResult",
        "TurnRunner",
    }:
        from agent import runtime

        return getattr(runtime, name)
    raise AttributeError(name)
