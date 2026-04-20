from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
import json
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
        candidate = scheduled_for.astimezone(UTC) + timedelta(days=7)
        while candidate <= now.astimezone(UTC):
            candidate = candidate + timedelta(days=7)
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

    def to_json(self) -> str:
        return json.dumps(
            {"trigger_type": self.trigger_type, "payload": self.payload},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "TriggerSpec":
        data = json.loads(raw)
        return cls(trigger_type=data["trigger_type"], payload=data["payload"])


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
    next_run_at: Optional[datetime]
    lease_until: Optional[datetime]
    active_run_id: Optional[str]
    last_run_at: Optional[datetime]
    last_success_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


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
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
