from __future__ import annotations

from dataclasses import dataclass, field
import calendar
from datetime import datetime, time as dt_time, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
_WEEKDAY_MAP = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_time_of_day(value: str) -> dt_time:
    hour_text, minute_text = str(value).split(":", 1)
    return dt_time(hour=int(hour_text), minute=int(minute_text))


def _advance_until_future(
    candidate: datetime, step: timedelta, now: datetime
) -> datetime:
    while candidate <= now:
        candidate = candidate + step
    return candidate


@dataclass
class OnceTrigger:
    at: datetime
    timezone_name: str = "UTC"

    def next_after(self, now: datetime) -> Optional[datetime]:
        candidate = self.at.astimezone(UTC)
        return candidate if candidate > now.astimezone(UTC) else None

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        return None


@dataclass
class IntervalTrigger:
    every: int
    unit: str
    anchor_at: datetime
    timezone_name: str = "UTC"

    def _step(self) -> timedelta:
        unit = self.unit.lower()
        if unit in {"minute", "minutes"}:
            return timedelta(minutes=self.every)
        if unit in {"hour", "hours"}:
            return timedelta(hours=self.every)
        if unit in {"day", "days"}:
            return timedelta(days=self.every)
        if unit in {"week", "weeks"}:
            return timedelta(weeks=self.every)
        raise ValueError(f"Unsupported interval unit: {self.unit}")

    def next_after(self, now: datetime) -> Optional[datetime]:
        candidate = self.anchor_at.astimezone(UTC)
        return _advance_until_future(candidate, self._step(), now.astimezone(UTC))

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        candidate = scheduled_for.astimezone(UTC) + self._step()
        return _advance_until_future(candidate, self._step(), now.astimezone(UTC))


@dataclass
class DailyTrigger:
    time_of_day: str
    timezone_name: str = "UTC"

    def next_after(self, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        local_now = now.astimezone(tz)
        target_time = _parse_time_of_day(self.time_of_day)
        candidate_local = local_now.replace(
            hour=target_time.hour,
            minute=target_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate_local <= local_now:
            candidate_local = candidate_local + timedelta(days=1)
        return candidate_local.astimezone(UTC)

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        candidate = (scheduled_for.astimezone(tz) + timedelta(days=1)).astimezone(UTC)
        while candidate <= now.astimezone(UTC):
            candidate = (candidate.astimezone(tz) + timedelta(days=1)).astimezone(UTC)
        return candidate


@dataclass
class WeeklyTrigger:
    day_of_week: str
    time_of_day: str
    timezone_name: str = "UTC"

    def next_after(self, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        local_now = now.astimezone(tz)
        target_weekday = _WEEKDAY_MAP[str(self.day_of_week).strip().lower()]
        target_time = _parse_time_of_day(self.time_of_day)
        candidate_local = local_now.replace(
            hour=target_time.hour,
            minute=target_time.minute,
            second=0,
            microsecond=0,
        )
        delta_days = (target_weekday - local_now.weekday()) % 7
        candidate_local = candidate_local + timedelta(days=delta_days)
        if candidate_local <= local_now:
            candidate_local = candidate_local + timedelta(days=7)
        return candidate_local.astimezone(UTC)

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        candidate = (scheduled_for.astimezone(tz) + timedelta(days=7)).astimezone(UTC)
        while candidate <= now.astimezone(UTC):
            candidate = (candidate.astimezone(tz) + timedelta(days=7)).astimezone(UTC)
        return candidate


@dataclass
class WeekdaysTrigger:
    time_of_day: str
    timezone_name: str = "UTC"

    def next_after(self, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        local_now = now.astimezone(tz)
        target_time = _parse_time_of_day(self.time_of_day)
        candidate = local_now.replace(
            hour=target_time.hour,
            minute=target_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        candidate = self.next_after(scheduled_for)
        while candidate is not None and candidate <= now.astimezone(UTC):
            candidate = self.next_after(candidate)
        return candidate


@dataclass
class MonthlyTrigger:
    day_of_month: int
    time_of_day: str
    timezone_name: str = "UTC"

    def _candidate(self, year: int, month: int, tz: ZoneInfo) -> Optional[datetime]:
        day = int(self.day_of_month)
        if day < 1 or day > 31:
            raise ValueError("day_of_month must be between 1 and 31")
        if day > calendar.monthrange(year, month)[1]:
            return None
        target_time = _parse_time_of_day(self.time_of_day)
        return datetime(
            year, month, day, target_time.hour, target_time.minute, tzinfo=tz
        )

    @staticmethod
    def _next_month(year: int, month: int) -> tuple[int, int]:
        return (year + 1, 1) if month == 12 else (year, month + 1)

    def next_after(self, now: datetime) -> Optional[datetime]:
        tz = ZoneInfo(self.timezone_name)
        local_now = now.astimezone(tz)
        year, month = local_now.year, local_now.month
        for _ in range(24):
            candidate = self._candidate(year, month, tz)
            if candidate is not None and candidate > local_now:
                return candidate.astimezone(UTC)
            year, month = self._next_month(year, month)
        raise ValueError("unable to calculate monthly occurrence")

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        candidate = self.next_after(scheduled_for)
        while candidate is not None and candidate <= now.astimezone(UTC):
            candidate = self.next_after(candidate)
        return candidate


@dataclass
class TriggerSpec:
    trigger_type: str
    payload: dict[str, Any]

    @classmethod
    def once(cls, at: str | datetime, timezone_name: str) -> "TriggerSpec":
        return cls(
            "once",
            {"at": parse_datetime(at).isoformat(), "timezone_name": timezone_name},
        )

    @classmethod
    def interval(
        cls,
        every: int,
        unit: str,
        anchor_at: str | datetime,
        timezone_name: str,
    ) -> "TriggerSpec":
        return cls(
            "interval",
            {
                "every": int(every),
                "unit": unit,
                "anchor_at": parse_datetime(anchor_at).isoformat(),
                "timezone_name": timezone_name,
            },
        )

    @classmethod
    def daily(cls, time_of_day: str, timezone_name: str) -> "TriggerSpec":
        return cls(
            "daily",
            {"time_of_day": time_of_day, "timezone_name": timezone_name},
        )

    @classmethod
    def weekly(
        cls, day_of_week: str, time_of_day: str, timezone_name: str
    ) -> "TriggerSpec":
        return cls(
            "weekly",
            {
                "day_of_week": day_of_week,
                "time_of_day": time_of_day,
                "timezone_name": timezone_name,
            },
        )

    @classmethod
    def weekdays(cls, time_of_day: str, timezone_name: str) -> "TriggerSpec":
        return cls(
            "weekdays",
            {"time_of_day": time_of_day, "timezone_name": timezone_name},
        )

    @classmethod
    def monthly(
        cls, day_of_month: int, time_of_day: str, timezone_name: str
    ) -> "TriggerSpec":
        return cls(
            "monthly",
            {
                "day_of_month": int(day_of_month),
                "time_of_day": time_of_day,
                "timezone_name": timezone_name,
            },
        )

    def instantiate(self):
        if self.trigger_type == "once":
            return OnceTrigger(
                at=parse_datetime(self.payload["at"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        if self.trigger_type == "interval":
            return IntervalTrigger(
                every=int(self.payload["every"]),
                unit=str(self.payload["unit"]),
                anchor_at=parse_datetime(self.payload["anchor_at"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        if self.trigger_type == "daily":
            return DailyTrigger(
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        if self.trigger_type == "weekly":
            return WeeklyTrigger(
                day_of_week=str(self.payload["day_of_week"]),
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        if self.trigger_type == "weekdays":
            return WeekdaysTrigger(
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        if self.trigger_type == "monthly":
            return MonthlyTrigger(
                day_of_month=int(self.payload["day_of_month"]),
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=self.payload.get("timezone_name", "UTC"),
            )
        raise ValueError(f"Unknown trigger type: {self.trigger_type}")

    def initial_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        if self.trigger_type == "once":
            return parse_datetime(self.payload["at"])
        if self.trigger_type == "interval":
            return parse_datetime(self.payload["anchor_at"])
        return self.instantiate().next_after(now or datetime.now(UTC))

    def advance_after_claim(
        self, scheduled_for: datetime, now: datetime
    ) -> Optional[datetime]:
        return self.instantiate().advance_from(scheduled_for, now)

    def count_missed(
        self, scheduled_for: datetime, next_run_at: Optional[datetime]
    ) -> int:
        """How many occurrences fell between ``scheduled_for`` and the next one.

        Advancing the cursor jumps straight to the next occurrence in the
        future.  When the process was not running, that jump walks over every
        occurrence it should have fired in the meantime, and they vanish
        without a trace: the task looks like it ran on schedule, and the person
        who scheduled a daily report finds out days later that none of them
        ran.  This counts what the jump walked over so the run that resumes the
        schedule can say so.

        The count is derived from the trigger's own ``next_after`` rather than
        from a second copy of its calendar arithmetic.  A parallel
        implementation would be free to disagree with the schedule it claims
        to be describing -- and "the record disagrees with the plan" is exactly
        the kind of silence this exists to end.

        ``next_run_at is None`` means the trigger has no further occurrence
        (a ``once`` task that has just fired, late).  Running late is not a
        miss: the occurrence still happened, so the count is zero.
        """
        if next_run_at is None:
            return 0
        trigger = self.instantiate()
        count = 0
        cursor = scheduled_for
        while count < self.MISSED_COUNT_LIMIT:
            following = trigger.next_after(cursor)
            if following is None or following >= next_run_at:
                return count
            count += 1
            cursor = following
        return count

    #: Where the walk above stops.  The count is taken inside the claim
    #: transaction, so a minutely task left unclaimed for a year must not turn
    #: into half a million steps while the write lock is held -- and nothing a
    #: person would do about it changes between "many" and "exactly 525600".
    #: Hitting the limit means "at least this many".
    MISSED_COUNT_LIMIT = 1000

    def to_json(self) -> str:
        return json.dumps(
            {"trigger_type": self.trigger_type, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "TriggerSpec":
        data = json.loads(raw)
        return cls(trigger_type=data["trigger_type"], payload=data["payload"])


def describe_missed_occurrences(missed_count: int) -> str:
    """The line that says scheduled work did not happen.

    Spelled out rather than left as a number in a column: the count is only
    meaningful next to what it counts, and the run it is attached to looks
    entirely successful otherwise.  Empty string when nothing was missed, so
    callers can concatenate without checking.
    """
    missed = int(missed_count or 0)
    if missed <= 0:
        return ""
    if missed >= TriggerSpec.MISSED_COUNT_LIMIT:
        return (
            f"⚠️ 本次运行前至少 {missed} 次计划未能执行（进程未运行，计数已达上限）。"
        )
    return f"⚠️ 本次运行前有 {missed} 次计划未能执行（进程未运行）。"


@dataclass
class DeliveryTarget:
    target_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def standalone(cls) -> "DeliveryTarget":
        return cls("standalone", {})

    @classmethod
    def channel(
        cls, *, target_type: str, chat_id: str, chat_type: str = "p2p"
    ) -> "DeliveryTarget":
        return cls(target_type, {"chat_id": chat_id, "chat_type": chat_type})

    def to_json(self) -> str:
        return json.dumps(
            {"target_type": self.target_type, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "DeliveryTarget":
        data = json.loads(raw)
        return cls(target_type=data["target_type"], payload=data["payload"])


# ``overlap_policy`` and ``missed_run_policy`` are **identity only**.  Their
# values are part of the signature that ``find_matching_task`` and
# ``disable_duplicate_enabled_tasks`` compare to decide whether two
# definitions describe the same task.  No scheduling decision reads them: the
# claim path always requires ``active_run_id IS NULL`` (i.e. forbid_overlap)
# and always advances the cursor past ``now`` (i.e. coalesce), which is what
# the defaults below spell out.  They are therefore not exposed as API inputs
# or outputs -- a configurable field that changes nothing is worse than no
# field, because the UI implies a promise the runtime does not keep.  Change a
# default here and you change dedup identity, so treat that as a migration.
@dataclass
class NewScheduledTask:
    name: str
    kind: str
    trigger: TriggerSpec
    payload: dict[str, Any]
    delivery_mode: str
    delivery_target: DeliveryTarget
    model_override: Optional[str] = None
    enabled: bool = True
    overlap_policy: str = "forbid_overlap"
    missed_run_policy: str = "coalesce"
    workspace_root: str = field(default_factory=lambda: str(Path.cwd().resolve()))
    context_policy: str = "stateless"
    timeout_seconds: int = 1800
    retry_policy: dict[str, Any] = field(
        default_factory=lambda: {"max_attempts": 1, "backoff_seconds": 30}
    )
    selected_skills: list[str] = field(default_factory=list)
    permission_profile: str = "inherit"


@dataclass
class ScheduledTask:
    id: str
    name: str
    kind: str
    enabled: bool
    trigger: TriggerSpec
    payload: dict[str, Any]
    delivery_mode: str
    delivery_target: DeliveryTarget
    model_override: Optional[str]
    overlap_policy: str
    missed_run_policy: str
    workspace_root: str
    context_policy: str
    timeout_seconds: int
    retry_policy: dict[str, Any]
    selected_skills: list[str]
    permission_profile: str
    next_run_at: Optional[datetime]
    lease_until: Optional[datetime]
    active_run_id: Optional[str]
    last_run_at: Optional[datetime]
    last_success_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


#: Terminal statuses that mean "a person should look at this".
#:
#: ``cancelled`` is deliberately absent: the user asked for that one, so
#: surfacing it as something needing attention would train people to ignore
#: the signal.  ``interrupted`` *is* present — a lost lease, a restart or a
#: timeout is exactly the kind of silent outcome nobody is watching for.
ATTENTION_STATUSES: tuple[str, ...] = ("failed", "interrupted")


def run_needs_attention(run: "TaskRun") -> bool:
    """Whether a run is still something a person has to be told about.

    Two ways in, and the second is why this is a function instead of an
    ``in`` test at the call site: a run that *succeeded* still needs a person
    if it arrived saying that earlier occurrences were skipped while nothing
    was running.  Nothing else in the system ever mentions that gap.

    This is the Python half of a rule that also has to exist as SQL --
    ``SchedulerStore._attention_clause`` counts the same runs in one query.
    Two representations cannot be merged, so they are kept beside each other
    and a test walks a run through both.
    """
    if getattr(run, "acknowledged_at", None) is not None:
        return False
    if int(getattr(run, "missed_count", 0) or 0) > 0:
        return True
    return str(getattr(run, "status", "") or "") in ATTENTION_STATUSES


@dataclass
class TaskRun:
    id: str
    task_id: str
    scheduled_for: datetime
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    summary: str = ""
    error: str = ""
    output_path: str = ""
    delivery_status: str = ""
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    trigger_source: str = "schedule"
    attempt: int = 1
    cancel_requested_at: Optional[datetime] = None
    retry_of_run_id: str = ""
    #: How many occurrences of this task were skipped on the way to this run
    #: because nothing was running to fire them.  Surviving a restart is the
    #: point: the number is the only record that the work did *not* happen,
    #: and it lives on the run that resumed the schedule, which is where
    #: somebody looking at the history will find it.
    missed_count: int = 0
    #: When a person has seen this run's failure.  ``None`` on a run in
    #: ``ATTENTION_STATUSES`` means the run finished while nobody was looking
    #: and nothing has told them yet.
    acknowledged_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def execution_snapshot(task: ScheduledTask) -> dict[str, Any]:
    """Return the immutable, non-secret configuration used by one run."""
    return {
        "task_id": task.id,
        "task_updated_at": task.updated_at.isoformat(),
        "kind": task.kind,
        "payload": task.payload,
        "workspace_root": task.workspace_root,
        "context_policy": task.context_policy,
        "model_override": task.model_override,
        "timeout_seconds": task.timeout_seconds,
        "retry_policy": task.retry_policy,
        "selected_skills": task.selected_skills,
        "permission_profile": task.permission_profile,
        "delivery_mode": task.delivery_mode,
        "delivery_target": {
            "target_type": task.delivery_target.target_type,
            "payload": task.delivery_target.payload,
        },
    }


@dataclass
class ClaimedTask:
    task: ScheduledTask
    run: TaskRun


@dataclass
class ExecutionResult:
    summary: str
    text_output: str
    output_path: str = ""


@dataclass
class DeliveryResult:
    status: str
    output_path: str = ""
    error: str = ""
