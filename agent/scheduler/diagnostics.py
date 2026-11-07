"""What a failed run's words actually mean to a person.

The evidence
------------
A run failed with ``OpenDesign: connection lost mid-reply (unexpected EOF)`` and
another with ``scheduled task timed out after 1800 seconds``. Both sat on the
board for two days, in a Chinese interface, under a red dot. Neither sentence says
anything a person can act on -- one is a provider's internal phrasing, the other
is this codebase's own, and both are *diagnostics* dressed as explanation.

So: classify the reason once, here, and let every reader use the classification.

Why one place
-------------
Three surfaces show a failure (the board's attention card, the run detail, the
message a finished run leaves in the conversation). Three copies of "if it says
connection lost then say 网络..." would disagree the first time one was edited.

Why a classification and not a translation
------------------------------------------
The raw text stays, verbatim, where a person can open it -- a support thread needs
it. What the classification adds is the part that was missing: whether this is
something that fixes itself, and what a person could do about it. That second part
is also what decides whether an automatic retry is worth spending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = ["FailureReason", "describe_failure"]


@dataclass(frozen=True)
class FailureReason:
    """One failure, in the terms a person uses."""

    #: Two to six characters, safe to put in a badge or a list row.
    label: str
    #: One sentence: what it means, and what could be done. May be empty.
    hint: str
    #: Whether trying again could plausibly end differently. This is the only
    #: input to the automatic grace retry, so a wrong ``True`` costs a run and a
    #: wrong ``False`` costs the retry that would have worked.
    transient: bool
    #: The first line of the original, so a reader can still see the source.
    raw: str

    @property
    def summary(self) -> str:
        """One line for a card, a notification or a report."""
        return f"{self.label}：{self.hint}" if self.hint else self.label


#: Most specific first, and one ordering is load-bearing: "额度用尽" is checked
#: before "被限流" because a quota window is usually reported as a 429. Retrying a
#: spent account lands in the same closed window, so calling it a throttle would
#: buy a retry that cannot work. Every pattern is matched case-insensitively
#: against the whole text.
_RULES: tuple[tuple[re.Pattern[str], str, str, bool], ...] = (
    (
        re.compile(r"connection lost|unexpected eof|connection reset|broken pipe|"
                   r"econnreset|server disconnected|remote end closed", re.I),
        "连接中断",
        "模型服务这次没有把回复发完，多半是暂时的；会先自动重试一次。",
        True,
    ),
    (
        re.compile(r"timed out|timeout|超时", re.I),
        "超时",
        "这次跑得太久被时间上限掐断了。可以把超时调长，或者把任务拆小一点。",
        True,
    ),
    (
        re.compile(r"account.{0,20}(exhaust|quota)|quota.{0,20}(exceed|exhaust)|"
                   r"insufficient_quota|余额|额度.*(用完|不足)", re.I),
        "额度用尽",
        "这个服务商的额度不够了，重试也过不去；需要充值或换服务商。",
        False,
    ),
    (
        re.compile(r"rate.?limit|too many requests|\b429\b|overloaded", re.I),
        "被限流",
        "服务方要求慢一点，等一会儿再来。",
        True,
    ),
    (
        re.compile(r"\b50[234]\b|bad gateway|service unavailable|gateway timeout", re.I),
        "服务端错误",
        "模型服务那边出的问题，不是你或任务本身的错。",
        True,
    ),
    (
        re.compile(r"unauthorized|forbidden|invalid api key|authentication|"
                   r"\b401\b|\b403\b|凭证|未授权", re.I),
        "鉴权失败",
        "凭证或权限有问题，需要你检查一下 API Key 和账号权限。",
        False,
    ),
    (
        re.compile(r"\b400\b|bad request|invalid request|validation", re.I),
        "请求被拒绝",
        "请求本身被服务方拒绝了，通常要改任务描述或参数。",
        False,
    ),
)


def describe_failure(text: object, *, raw_limit: int = 200) -> Optional[FailureReason]:
    """Classify one error string, or return ``None`` when there is nothing to say.

    An unclassifiable error is *not* treated as transient: retrying a cause nobody
    understands spends a whole run to learn the same thing again. The label then
    is the original's first line, which is at least honest about being unknown.
    """
    whole = str(text or "").strip()
    if not whole:
        return None
    first_line = whole.splitlines()[0].strip()
    raw = first_line[:raw_limit]
    for pattern, label, hint, transient in _RULES:
        if pattern.search(whole):
            return FailureReason(label=label, hint=hint, transient=transient, raw=raw)
    return FailureReason(label=raw, hint="", transient=False, raw=raw)
