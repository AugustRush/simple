"""Detecting a tool loop that has stopped making progress.

Why this module exists
----------------------
``BaseAgent`` carried thirteen methods and three constants for one question: has
this tool loop stopped getting anywhere?  They were pure except for two
thresholds, so the whole state machine could be built and driven without an
agent -- which is what ``tests/test_tool_watchdog.py`` now does.

The state machine, in one paragraph
-----------------------------------
Each turn of the loop produces a batch of tool calls and their results.  Two
signatures summarise the batch: a *pair* signature (which calls, with which
arguments) and a *result* signature (what came back).  Two run-length counters
track how many consecutive rounds repeated each signature, a third tracks rounds
classified as unproductive, and a per-batch occurrence count catches a batch
that keeps returning without ever being the same *pair* twice.  Threshold rules
are then tested in priority order.

Why the rules are ordered the way they are
------------------------------------------
``intent_required`` is checked first because it is the one condition a retry can
never fix: the tool is refusing until the model states an intent, and repeating
the call cannot supply one.  The rest are progressively weaker evidence of a
loop, and the first that trips is the reason reported -- so the reported reason
is the most specific true statement about why the turn is stopping, not the
first counter that happened to overflow.

``ToolLoopOutcome`` moved here with the logic.  It is re-exported from
``agent.core.agent`` so existing importers are unaffected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from agent import shared

__all__ = ["ToolLoopOutcome", "ToolLoopWatchdog"]


@dataclass
class ToolLoopOutcome:
    """What one batch of tool results says about progress."""

    progress_made: bool = False
    artifact_changed: bool = False
    needs_user: bool = False
    retryable: bool = False
    fatal: bool = False
    unproductive: bool = True
    intent_required: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "progress_made": self.progress_made,
            "artifact_changed": self.artifact_changed,
            "needs_user": self.needs_user,
            "retryable": self.retryable,
            "fatal": self.fatal,
            "unproductive": self.unproductive,
            "intent_required": self.intent_required,
        }


class ToolLoopWatchdog:
    """Stuck-detection for one agent's tool loop.

    One instance per agent, not per turn: the thresholds are configuration, the
    per-turn counters live in the state dict the caller owns and resets.
    """

    __slots__ = ("_repeat_threshold", "_unproductive_threshold", "_batch_threshold")

    def __init__(
        self,
        *,
        repeat_threshold: int,
        unproductive_threshold: int,
        batch_threshold: Optional[int] = None,
    ) -> None:
        self._repeat_threshold = int(repeat_threshold)
        self._unproductive_threshold = int(unproductive_threshold)
        # The occurrence count for one batch signature.  Separate from the
        # run-length threshold above, and defaulting to it so behaviour is
        # unchanged -- but separable, because the two are not the same
        # measurement and sharing one number is what makes the occurrence rule
        # unreachable.  See `check`.
        self._batch_threshold = (
            self._unproductive_threshold
            if batch_threshold is None
            else int(batch_threshold)
        )

    # -- per-turn state ------------------------------------------------------

    @staticmethod
    def new_state() -> dict[str, Any]:
        """Per-turn dedup state for tool-loop stuck-detection."""
        return {
            "last_pair_sig": "",
            "last_result_sig": "",
            "same_pair": 0,
            "same_result": 0,
            "unproductive_rounds": 0,
            "batch_counts": {},
            "last_outcome": {},
        }

    # -- signatures ----------------------------------------------------------

    @staticmethod
    def stable_key(value: Any) -> str:
        """A hashable, order-independent key for one JSON-ish value.

        Sorted keys so a provider that reorders an object does not read as a
        different call; truncated so a large payload does not become an
        unbounded signature component.
        """
        try:
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            encoded = str(value)
        return encoded[:512]

    @classmethod
    def tool_use_signature(cls, tool_uses: list[dict]) -> str:
        """Identify a batch of tool calls by name and arguments."""
        parts: list[str] = []
        for tool_use in tool_uses:
            name = str(tool_use.get("name", ""))
            args = tool_use.get("input", tool_use.get("arguments", {}))
            parts.append(f"{name}:{cls.stable_key(args)}")
        return "|".join(parts)

    @staticmethod
    def normalized_tool_result(raw_result: str) -> Any:
        """The parsed result, or the raw text when it is not JSON."""
        text = str(raw_result or "").strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except Exception:
            return text

    @classmethod
    def tool_result_signature(cls, results: list[str]) -> str:
        """Identify a batch of results, with volatile keys dropped.

        ``_signature_keep`` filters the keys that legitimately differ between
        two calls that did the same thing (timings, operation ids), so a loop is
        detected even when every result carries a fresh id.
        """
        parts: list[str] = []
        for raw in results:
            value = cls.normalized_tool_result(raw)
            if isinstance(value, dict):
                kept = {
                    key: cls._signature_value(item)
                    for key, item in value.items()
                    if cls._signature_keep(key)
                }
                parts.append(cls.stable_key(kept))
            else:
                parts.append(cls.stable_key(value))
        return "|".join(parts)

    @staticmethod
    def _signature_keep(key: str) -> bool:
        """Whether a result key is stable enough to compare between rounds."""
        lowered = str(key).lower()
        if lowered in {
            "operation_id",
            "elapsed_ms",
            "duration_ms",
            "started_at",
            "finished_at",
            "timestamp",
            "latency_ms",
            "run_id",
            "trace_id",
            "request_id",
        }:
            return False
        return not lowered.endswith("_ms") and not lowered.endswith("_at")

    @staticmethod
    def _signature_value(value: Any) -> Any:
        """Bound one kept value so a huge string cannot dominate the signature."""
        if isinstance(value, str) and len(value) > 200:
            return value[:200]
        return value

    # -- classification ------------------------------------------------------

    @staticmethod
    def classify_tool_result(raw_result: str) -> ToolLoopOutcome:
        """What one tool result says about whether it got anywhere.

        Two readings: a JSON payload is inspected for the keys the tools
        actually set (``ok``, ``error``, ``blocked``, ``recoverable_by_agent``,
        ``intent_required``, artifact keys), and non-JSON text is matched
        against failure markers.  A result that is neither is taken at face
        value as progress.
        """
        text = str(raw_result or "").strip()
        if not text:
            return ToolLoopOutcome(unproductive=True)
        try:
            payload = json.loads(text)
        except Exception:
            lower = text.lower()
            unproductive = any(
                marker in lower
                for marker in (
                    "timed out",
                    "timeout",
                    "not found",
                    "permission denied",
                    "requires confirmation",
                    "blocked",
                    "error",
                )
            )
            retryable = "timeout" in lower or "timed out" in lower
            return ToolLoopOutcome(
                progress_made=not unproductive,
                retryable=retryable,
                fatal=unproductive and not retryable,
                unproductive=unproductive,
            )
        if not isinstance(payload, dict):
            return ToolLoopOutcome(progress_made=True, unproductive=False)
        recoverable = payload.get("recoverable_by_agent") is True or bool(
            payload.get("recovery_hint")
        )
        intent_required = payload.get("intent_required") is True or (
            "intent" in str(payload.get("error", "")).lower()
        )
        needs_user = (
            payload.get("requires_confirmation") is True
            or intent_required
        )
        artifact_changed = any(
            payload.get(key) not in (None, "", [], {})
            for key in (
                "artifact",
                "artifacts",
                "file",
                "files",
                "path",
                "paths",
                "written_files",
                "created",
                "updated",
            )
        )
        if recoverable:
            return ToolLoopOutcome(
                progress_made=True,
                artifact_changed=artifact_changed,
                retryable=True,
                unproductive=False,
                intent_required=intent_required,
            )
        blocked = payload.get("blocked") is True
        failed = payload.get("ok") is False or (
            bool(payload.get("error")) and payload.get("ok") is not True
        )
        if needs_user or blocked or failed:
            return ToolLoopOutcome(
                artifact_changed=artifact_changed,
                needs_user=needs_user,
                fatal=not recoverable,
                unproductive=True,
                intent_required=intent_required,
            )
        meaningful_values = [
            value for value in payload.values() if value not in (None, "", [], {})
        ]
        progress_made = bool(meaningful_values)
        return ToolLoopOutcome(
            progress_made=progress_made,
            artifact_changed=artifact_changed,
            unproductive=not progress_made,
            intent_required=intent_required,
        )

    @classmethod
    def classify_tool_results(cls, results: list[str]) -> ToolLoopOutcome:
        """Fold a batch's outcomes into one.

        Note the asymmetry: ``fatal`` is ``all(...)`` over the unproductive
        outcomes when the batch is unproductive, and ``any(...)`` otherwise.  A
        batch is fatal only when everything in it failed for good; one success
        makes the batch non-fatal.
        """
        if not results:
            return ToolLoopOutcome(unproductive=True)
        outcomes = [cls.classify_tool_result(result) for result in results]
        unproductive = all(outcome.unproductive for outcome in outcomes)
        return ToolLoopOutcome(
            progress_made=any(outcome.progress_made for outcome in outcomes),
            artifact_changed=any(outcome.artifact_changed for outcome in outcomes),
            needs_user=any(outcome.needs_user for outcome in outcomes),
            retryable=any(outcome.retryable for outcome in outcomes),
            fatal=all(outcome.fatal for outcome in outcomes if outcome.unproductive)
            if unproductive
            else any(outcome.fatal for outcome in outcomes),
            unproductive=unproductive,
            intent_required=all(outcome.intent_required for outcome in outcomes),
        )

    @classmethod
    def result_preview(cls, results: list[str], limit: int = 220) -> str:
        """A short, single-line rendering of a batch, for the stop message."""
        joined = " | ".join(str(result or "").strip() for result in results)
        joined = " ".join(joined.split())
        if len(joined) <= limit:
            return joined
        return joined[: max(0, limit - 1)] + "…"

    # -- the decision --------------------------------------------------------

    def check(
        self,
        state: dict[str, Any],
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """Update *state* and return a stuck-reason string, or ``''``.

        Pure single-pass over the new batch: compute both signatures, update the
        run-length counters and the per-batch occurrence count, then test the
        threshold rules in priority order and report the first that trips.

        Extracted from ``send_message`` so the body of the tool loop reads as
        intent (a function call) instead of mechanism (sixty lines of counters).

        One reachability note, measured rather than assumed: with the occurrence
        threshold equal to the run-length threshold -- which is the default --
        the ``unproductive_rounds`` rule below always fires first, because it
        counts every unproductive round while the occurrence count only
        advances for *this* batch signature.  ``same_batch_unproductive`` is
        therefore unreachable at the default settings.  It is kept rather than
        deleted because ``batch_threshold`` can now separate the two, and
        because removing a rule the caller may be relying on is a behaviour
        change, not a cleanup.
        """
        batch_sig = self.tool_use_signature(tool_uses)
        result_sig = self.tool_result_signature(results)
        pair_sig = (batch_sig, result_sig)

        state["batch_counts"][batch_sig] = state["batch_counts"].get(batch_sig, 0) + 1

        if pair_sig == state["last_pair_sig"]:
            state["same_pair"] += 1
        else:
            state["same_pair"] = 1
            state["last_pair_sig"] = pair_sig
        if result_sig == state["last_result_sig"]:
            state["same_result"] += 1
        else:
            state["same_result"] = 1
            state["last_result_sig"] = result_sig

        outcome = self.classify_tool_results(results)
        state["last_outcome"] = outcome.to_dict()
        unproductive = outcome.unproductive
        intent_required = outcome.intent_required
        if unproductive or state["same_pair"] >= self._repeat_threshold:
            state["unproductive_rounds"] += 1
        else:
            state["unproductive_rounds"] = 0

        # Priority order matters: the most specific true statement wins.
        if intent_required and state["same_result"] >= self._repeat_threshold:
            return "intent_required"
        if state["same_pair"] >= self._repeat_threshold:
            return "same_pair"
        if unproductive and state["same_result"] >= self._repeat_threshold:
            return "same_result"
        if state["unproductive_rounds"] >= self._unproductive_threshold:
            return "unproductive_rounds"
        if (
            unproductive
            and state["batch_counts"][batch_sig] >= self._batch_threshold
        ):
            return "same_batch_unproductive"
        return ""

    # -- the stop message ----------------------------------------------------

    @classmethod
    def build_stuck_response(
        cls,
        *,
        user_message: str,
        reason: str,
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """The message shown when the loop stops itself.

        Chinese when the user wrote Chinese, for the same reason the rest of the
        turn does: the stop is the thing they have to act on.
        """
        tool_names = ", ".join(str(tool_use.get("name", "")) for tool_use in tool_uses)
        result_preview = cls.result_preview(results)
        if shared._looks_like_chinese(user_message):
            return cls._stuck_response_zh(reason, tool_names, result_preview)
        return cls._stuck_response_en(reason, tool_names, result_preview)

    @staticmethod
    def _stuck_response_zh(reason: str, tool_names: str, result_preview: str) -> str:
        if reason == "intent_required":
            return "\n".join(
                [
                    (
                        "我先停一下：这次卡住的不是路径、权限或下载问题，"
                        "而是内部工具安全规则没有被满足。"
                    ),
                    "",
                    (
                        f"当前观察：模型连续尝试调用 `{tool_names or 'unknown'}`，"
                        "但这次调用没有满足该工具要求的 `intent`，所以被安全拦截。"
                        "不同工具要求的不是同一件事：shell 要说明命令做什么、"
                        "为什么必须运行；会留下长期产物的工具要逐字引用用户提出"
                        "这个需求的原话——说明「为什么这样做有用」不算。"
                    ),
                    "",
                    "合理的下一步是重新规划这一步：按要求补齐 `intent`；如果用户"
                    "其实并没有提出过这个需求，就应该把它作为建议说出来问他，"
                    "而不是继续空转。",
                ]
            )
        reason_text = {
            "same_pair": "同一个工具请求连续产生相同结果",
            "same_result": "工具结果重复且没有新信息",
            "unproductive_rounds": "连续多轮工具调用失败或没有有效输出",
            "same_batch_unproductive": "同一批工具持续返回无效结果",
        }.get(reason, reason)
        lines = [
            (
                "我先停一下：我检测到本轮已经连续多次调用工具，"
                "但结果没有继续推进；继续自动尝试大概率只会耗到系统上限。"
            ),
            "",
            (
                f"当前观察：最近一轮工具是 `{tool_names or 'unknown'}`；"
                f"触发原因是 {reason_text}。"
            ),
        ]
        if result_preview:
            lines.append(f"最近结果：{result_preview}")
        lines.extend(
            [
                "",
                "你可以直接回复选择下一步：",
                "1. 提供新的路径、权限、凭据或搜索方向后继续",
                "2. 让我基于已有结果先总结",
                "3. 告诉我该在几个可选方案里选哪个",
                "4. 停止这个任务",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _stuck_response_en(reason: str, tool_names: str, result_preview: str) -> str:
        if reason == "intent_required":
            return "\n".join(
                [
                    (
                        "I am pausing because the loop is blocked by the internal "
                        "tool safety protocol, not by a missing path, permission, "
                        "or credential."
                    ),
                    "",
                    (
                        f"Current signal: the model repeatedly tried to call "
                        f"`{tool_names or 'unknown'}` without satisfying the `intent` "
                        "that tool requires, so the calls were blocked. The tools do not "
                        "all ask for the same thing: `shell` wants to be told what the "
                        "command does and why it must run, while a tool whose result "
                        "outlives the turn wants the user's own words quoted verbatim -- "
                        "explaining why the action would be useful is not a substitute."
                    ),
                    "",
                    (
                        "The right next step is to re-plan this action: supply the "
                        "`intent` it asks for, or -- if the user never actually asked for "
                        "this -- propose it to them instead of retrying."
                    ),
                ]
            )
        reason_text = {
            "same_pair": "the same tool request produced the same result repeatedly",
            "same_result": "the tool results repeated without new information",
            "unproductive_rounds": (
                "several consecutive tool rounds failed or returned no useful output"
            ),
            "same_batch_unproductive": (
                "the same tool batch kept returning unproductive results"
            ),
        }.get(reason, reason)
        lines = [
            (
                "I am pausing here because the tool loop is no longer making "
                "clear progress; continuing automatically would likely just "
                "hit the hard iteration limit."
            ),
            "",
            (
                f"Current signal: latest tool batch was `{tool_names or 'unknown'}`; "
                f"trigger was {reason_text}."
            ),
        ]
        if result_preview:
            lines.append(f"Latest result: {result_preview}")
        lines.extend(
            [
                "",
                "Please choose the next step:",
                "1. Continue with a new path, permission, credential, or search direction",
                "2. Summarize what we have so far",
                "3. Tell me which option to pick",
                "4. Stop this task",
            ]
        )
        return "\n".join(lines)
