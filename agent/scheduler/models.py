from __future__ import annotations

from dataclasses import dataclass, field
import calendar
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agent.verification import (
    VERDICT_NONE,
    VerificationResult,
    command_rejection_reason,
    decode_verification,
    encode_verification,
)


UTC = timezone.utc

#: How much of a subscription's list of names has to arrive before it runs.
#: ``ANY`` is what a single name has always meant.  ``ALL`` is a join, and it
#: exists so a step can wait for several upstreams without the caller having to
#: invent a fake combined signal name nobody emits.
SIGNAL_MODE_ANY = "any"
SIGNAL_MODE_ALL = "all"
SIGNAL_MODES: tuple[str, ...] = (SIGNAL_MODE_ANY, SIGNAL_MODE_ALL)

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


#: What to pass as a timezone when the caller named no zone.  It is not a zone
#: itself -- it is the instruction to look one up -- so it must never reach
#: ``ZoneInfo`` unresolved.
LOCAL_TIMEZONE = "local"


def local_timezone_name() -> str:
    """The IANA name of this machine's own zone.

    A wall-clock time that names no zone means the zone the person is in, so
    this is the answer whenever a trigger's timezone is left unspecified.

    ``datetime.now().astimezone().tzinfo`` is not usable for this: on macOS it
    is a fixed offset named "CST", and ``ZoneInfo`` cannot be built from it --
    which is the trap this function exists to avoid, because a zone that
    silently becomes UTC turns "08:00" into 16:00 in Shanghai.  Both macOS and
    Linux point ``/etc/localtime`` at a real entry under ``zoneinfo/``, so the
    tail of that symlink is the name.  ``TZ`` wins when set, since an explicit
    environment is a statement of intent.

    Falls back to ``"UTC"`` only when no name can be resolved at all, which
    means no ``zoneinfo`` database is reachable -- in that case UTC is the one
    zone ``ZoneInfo`` can always build.
    """
    candidates = []
    from_env = os.environ.get("TZ", "").strip()
    if from_env:
        candidates.append(from_env)
    try:
        link = os.path.realpath("/etc/localtime")
    except OSError:
        link = ""
    _, marker, tail = link.partition("zoneinfo/")
    if marker and tail:
        candidates.append(tail)

    for candidate in candidates:
        try:
            ZoneInfo(candidate)
        except Exception:
            continue
        return candidate
    return "UTC"


def resolve_timezone_name(timezone_name: str | None) -> str:
    """Turn "no zone given" into a zone.

    ``""`` and ``LOCAL_TIMEZONE`` both mean the machine's own zone.  Anything
    else is taken at its word, so an explicit ``"UTC"`` stays UTC -- someone
    who asked for UTC meant UTC, and quietly moving their clock would be the
    same bug in the other direction.
    """
    wanted = str(timezone_name or "").strip()
    if not wanted or wanted.lower() == LOCAL_TIMEZONE:
        return local_timezone_name()
    return wanted


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
    timezone_name: str = field(default_factory=local_timezone_name)

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
    timezone_name: str = field(default_factory=local_timezone_name)

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
    timezone_name: str = field(default_factory=local_timezone_name)

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
    timezone_name: str = field(default_factory=local_timezone_name)

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
    timezone_name: str = field(default_factory=local_timezone_name)

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
    timezone_name: str = field(default_factory=local_timezone_name)

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
class SignalTrigger:
    """A task that runs when a named signal is emitted, not when a clock says.

    Both ``next_after`` and ``advance_from`` return ``None``, and that is the
    whole mechanism: a task with no next occurrence has ``next_run_at IS NULL``,
    so the claim query -- which requires ``next_run_at IS NOT NULL`` -- can
    never pick it up.  Nothing else has to know that signals exist in order to
    stay out of their way.

    It follows that ``count_missed`` reports zero for these tasks, which is
    correct rather than convenient: there is no calendar to fall behind.  A
    signal that arrives while nothing is running is a different problem, and it
    is handled where it belongs -- durably, by the emission record.

    ``names`` is everything this task waits for, and ``mode`` says how much of
    it is enough:

    * ``any`` -- one emission of any listed name starts the run.  This is what
      a single name means, and what a subscription has always meant.
    * ``all`` -- every name must have been emitted *since this task last ran*
      before it runs once.  That is how a step depending on several upstreams
      is expressed, and the "since it last ran" part is what keeps a fast
      upstream from making the join fire twice for one round of work.
    """

    names: list[str]
    mode: str = SIGNAL_MODE_ANY

    @property
    def name(self) -> str:
        """The one name this waits for, for the single-signal case.

        Kept as a read-only view rather than a field so the two cannot drift:
        a caller asking for "the name" of a subscription that waits for three
        gets the first, and should be reading ``names`` instead.
        """
        return self.names[0] if self.names else ""

    def needs_all(self) -> bool:
        return self.mode == SIGNAL_MODE_ALL

    def next_after(self, now: datetime) -> Optional[datetime]:
        return None

    def advance_from(self, scheduled_for: datetime, now: datetime) -> Optional[datetime]:
        return None


@dataclass
class TriggerSpec:
    trigger_type: str
    payload: dict[str, Any]

    @classmethod
    def once(
        cls, at: str | datetime, timezone_name: str = LOCAL_TIMEZONE
    ) -> "TriggerSpec":
        return cls(
            "once",
            {
                "at": parse_datetime(at).isoformat(),
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def interval(
        cls,
        every: int,
        unit: str,
        anchor_at: str | datetime,
        timezone_name: str = LOCAL_TIMEZONE,
    ) -> "TriggerSpec":
        return cls(
            "interval",
            {
                "every": int(every),
                "unit": unit,
                "anchor_at": parse_datetime(anchor_at).isoformat(),
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def daily(
        cls, time_of_day: str, timezone_name: str = LOCAL_TIMEZONE
    ) -> "TriggerSpec":
        return cls(
            "daily",
            {
                "time_of_day": time_of_day,
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def weekly(
        cls,
        day_of_week: str,
        time_of_day: str,
        timezone_name: str = LOCAL_TIMEZONE,
    ) -> "TriggerSpec":
        return cls(
            "weekly",
            {
                "day_of_week": day_of_week,
                "time_of_day": time_of_day,
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def weekdays(
        cls, time_of_day: str, timezone_name: str = LOCAL_TIMEZONE
    ) -> "TriggerSpec":
        return cls(
            "weekdays",
            {
                "time_of_day": time_of_day,
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def monthly(
        cls,
        day_of_month: int,
        time_of_day: str,
        timezone_name: str = LOCAL_TIMEZONE,
    ) -> "TriggerSpec":
        return cls(
            "monthly",
            {
                "day_of_month": int(day_of_month),
                "time_of_day": time_of_day,
                "timezone_name": resolve_timezone_name(timezone_name),
            },
        )

    @classmethod
    def signal(cls, name: str) -> "TriggerSpec":
        """A subscription to a named signal.

        The name is the entire contract between whoever emits and whoever
        subscribes, so it is stored verbatim and matched exactly.  A near-miss
        is not a near-match: see the store's signal listing, which exists so
        the name can be picked from what is actually emitted rather than
        typed.
        """
        return cls("signal", {"name": str(name).strip()})

    @classmethod
    def signal_all(cls, names: list[str]) -> "TriggerSpec":
        """A subscription that waits for every one of *names*.

        Stored under a separate key from the single-name form on purpose.  The
        two shapes are read by the same accessor, but keeping ``name`` intact
        means every existing row, tool call and API response keeps meaning what
        it meant, and a database written by an older build stays readable.
        """
        cleaned = [str(item).strip() for item in names if str(item).strip()]
        deduped = list(dict.fromkeys(cleaned))
        if not deduped:
            raise ValueError("signal_all needs at least one signal name")
        return cls("signal", {"names": deduped, "mode": SIGNAL_MODE_ALL})

    def instantiate(self):
        if self.trigger_type == "once":
            return OnceTrigger(
                at=parse_datetime(self.payload["at"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "interval":
            return IntervalTrigger(
                every=int(self.payload["every"]),
                unit=str(self.payload["unit"]),
                anchor_at=parse_datetime(self.payload["anchor_at"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "daily":
            return DailyTrigger(
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "weekly":
            return WeeklyTrigger(
                day_of_week=str(self.payload["day_of_week"]),
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "weekdays":
            return WeekdaysTrigger(
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "monthly":
            return MonthlyTrigger(
                day_of_month=int(self.payload["day_of_month"]),
                time_of_day=str(self.payload["time_of_day"]),
                timezone_name=resolve_timezone_name(
                    self.payload.get("timezone_name")
                ),
            )
        if self.trigger_type == "signal":
            return SignalTrigger(
                names=signal_names(self),
                mode=signal_mode(self),
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


#: What became of one emission.  ``coalesced``, ``refused``, ``waiting`` and
#: ``unmatched`` are not failures to be hidden: they are the four ways a signal
#: can arrive without producing a run, and each is stored with a reason so that
#: "the automation did not run" is never something a person has to infer from a
#: silence.  ``unmatched`` in particular is what a typo looks like -- a signal
#: nobody subscribes to, which without this state would be indistinguishable
#: from a signal that worked.  ``waiting`` is what a join looks like one
#: upstream short: accepted and recorded, with a run still to come, which is
#: neither of the other two and must not be reported as either.
SIGNAL_STATES: tuple[str, ...] = (
    "pending",
    "delivered",
    "coalesced",
    "waiting",
    "refused",
    "unmatched",
)

#: How deep a cascade may go before delivery is refused.
#:
#: A declarable graph can be checked for cycles when it is built.  Signals
#: cannot: task A emitting a signal that task B subscribes to, while B emits
#: one A subscribes to, is not a mistake anybody makes on purpose -- it is a
#: loop that assembles itself out of two edits made weeks apart.  So instead of
#: trying to prove a property of the graph up front, every emission carries how
#: far it is from the thing that started the cascade, and delivery stops at a
#: ceiling.  That bounds the runaway whether it is a ring, a diamond that
#: doubles at every level, or something nobody drew at all.
DEFAULT_SIGNAL_MAX_DEPTH = 10

#: The prefix that marks a signal as "a run of a particular task finished".
#: Names outside this shape are free-form: only whoever emits them can say
#: whether they are spelled right.
TASK_SIGNAL_PREFIX = "task:"


def task_signal_name(task_id: str, status: str) -> str:
    """The signal a finished run emits: ``task:<id>:<status>``.

    Keyed by id rather than by name.  A task can be renamed, and a
    subscription that quietly stopped matching after a rename would be exactly
    the kind of failure that looks like success -- the trigger would simply
    never fire again.  The readable name travels in the emission payload
    instead, so the interface can label the signal without the subscription
    depending on a value that is allowed to change.
    """
    return f"{TASK_SIGNAL_PREFIX}{task_id}:{status}"


def signal_names(trigger: "TriggerSpec") -> list[str]:
    """Every signal name a subscription waits for, in the order given.

    One accessor for both stored shapes, because two readers disagreeing about
    what a subscription means is how a task ends up waiting forever.  The
    single-name form written by older builds (``{"name": ...}``) and the
    list form (``{"names": [...]}``) are the same thing to every caller, and a
    subscription to several names is read as a set of one-name waits.
    """
    payload = getattr(trigger, "payload", None) or {}
    listed = payload.get("names")
    if isinstance(listed, (list, tuple)):
        names = [str(item).strip() for item in listed]
        return [item for item in names if item]
    single = str(payload.get("name", "") or "").strip()
    return [single] if single else []


def signal_mode(trigger: "TriggerSpec") -> str:
    """Whether one name is enough (``any``) or all of them are needed (``all``).

    A trigger with no mode recorded is ``any``: that is what every subscription
    written before joins existed meant, so reading it that way keeps old rows
    behaving exactly as they did.
    """
    payload = getattr(trigger, "payload", None) or {}
    mode = str(payload.get("mode", "") or "").strip().lower()
    return mode if mode in SIGNAL_MODES else SIGNAL_MODE_ANY


def subscribes_to(trigger: "TriggerSpec", name: str) -> bool:
    """Whether this subscription wants to hear about *name*."""
    if getattr(trigger, "trigger_type", "") != "signal":
        return False
    return str(name or "").strip() in signal_names(trigger)


#: The statuses a run can finish in.  A subscription to anything else is a
#: typo that can be caught when it is written, rather than discovered as a
#: task that mysteriously never runs.
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "skipped",
    #: The work was not judged: an acceptance check was declared and could not
    #: be evaluated.  See :data:`RUN_UNVERIFIED_STATUS` for why this is not
    #: ``failed``.
    "unverified",
)


def parse_task_signal(name: str) -> Optional[tuple[str, str]]:
    """Split ``task:<id>:<status>`` into its two parts, or ``None``.

    ``None`` means "not a task signal", which is not an error: it is how a
    free-form name like ``report.ready`` is recognized as something only its
    emitter can vouch for.
    """
    text = str(name or "").strip()
    if not text.startswith(TASK_SIGNAL_PREFIX):
        return None
    task_id, separator, status = text[len(TASK_SIGNAL_PREFIX) :].rpartition(":")
    if not separator or not task_id or not status:
        return None
    return task_id, status


@dataclass
class SignalEmission:
    """One signal, recorded before anything is done about it.

    The record exists so that an emission cannot be lost between "somebody
    emitted" and "a task ran": delivery is a separate step that reads these
    rows, so a process that stops mid-flight resumes and finishes the job
    instead of dropping half of it.  It is the same reason the run record
    exists at all -- a scheduled system that cannot say what it did not do is
    not one you can leave alone.
    """

    id: str
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: What produced it: ``task``, ``agent`` or ``manual``.  Kept for the
    #: audit trail rather than for behaviour -- the name is the contract.
    source: str = "manual"
    #: Distance from the emission that started this cascade.  Zero for
    #: something a person or a clock started.
    depth: int = 0
    #: The root emission of the cascade, so a chain can be read end to end.
    origin_id: str = ""
    state: str = "pending"
    #: Why it did not become its own run, when it did not.
    reason: str = ""
    delivered_run_id: str = ""
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


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


#: Bounds on a declared acceptance criterion.  A criterion is a sentence, not a
#: document: it is injected into a run's system prompt on every execution, so
#: an unbounded one is paid for again on every run, forever.
MAX_ACCEPTANCE_CRITERIA = 20
MAX_ACCEPTANCE_CRITERION_CHARS = 500
MAX_VERIFY_COMMAND_CHARS = 2000

#: Bounds on a retry policy, in one place so that every way of writing one
#: agrees.  The API checked these itself and nothing else did, which left a
#: policy written straight into the store unbounded: a step that never gives up
#: is a chain that waits forever for it.
MAX_RETRY_ATTEMPTS = 5
MAX_RETRY_BACKOFF_SECONDS = 86400


@dataclass
class Acceptance:
    """What has to be true of this task's work for the run to count as success.

    Two halves answering the same question from opposite directions, and
    neither is redundant:

    ``criteria`` is *what the agent is aiming at*.  A scheduled run has nobody
    to ask, so a prompt with no stated target is judged only by whether the
    model replied -- which it always does.  These go into the run's system
    prompt, where they are the difference between work and output.

    ``verify_command`` is *what the record can be trusted to say*.  An agent's
    opinion of its own work is not evidence, so the machine-checkable half is a
    command whose exit code decides.

    An empty ``Acceptance`` is the honest default, and it means "nothing was
    declared": the run is not judged, which is a different statement from
    having been judged and passing.
    """

    criteria: list[str] = field(default_factory=list)
    verify_command: str = ""

    def is_declared(self) -> bool:
        return bool(self.criteria) or bool(self.verify_command.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria": list(self.criteria),
            "verify_command": self.verify_command,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Acceptance":
        if not isinstance(data, dict):
            return cls()
        raw_criteria = data.get("criteria")
        criteria = (
            [str(item).strip() for item in raw_criteria if str(item).strip()]
            if isinstance(raw_criteria, list)
            else []
        )
        return cls(
            criteria=criteria,
            verify_command=str(data.get("verify_command") or "").strip(),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: Any) -> "Acceptance":
        """Read back a stored criterion.

        An unparseable column reads as "nothing declared" rather than raising:
        the value is attached to a task that already exists, and a corrupt blob
        must not make that task unloadable.  "We have no criterion" is the
        honest reading of a criterion nobody can parse.
        """
        if not raw:
            return cls()
        if isinstance(raw, dict):
            return cls.from_dict(raw)
        try:
            return cls.from_dict(json.loads(str(raw)))
        except (TypeError, ValueError):
            return cls()


def acceptance_payload(acceptance: Optional["Acceptance"]) -> dict[str, Any]:
    """What a task or step is judged by, always as an object.

    Always present and always an object -- possibly with an empty ``criteria``
    and an empty ``verify_command`` -- so a reader can take the fields without
    checking whether the key exists.  "Nothing was declared" is a value here,
    not an absence, and that difference is why this is not the ``None`` the
    callers happen to be holding.

    One definition for the interface and for the agent's own tools: both read
    a task's criterion, and a criterion that grows a field has to reach every
    reader of it.
    """
    to_dict = getattr(acceptance, "to_dict", None)
    data = dict(to_dict()) if callable(to_dict) else {}
    criteria = data.get("criteria")
    return {
        "criteria": [str(item) for item in criteria] if isinstance(criteria, list) else [],
        "verify_command": str(data.get("verify_command") or ""),
    }


def validate_acceptance(
    acceptance: Optional["Acceptance"],
    *,
    workspace_root: str,
    output_dir: str,
    label: str = "任务",
) -> None:
    """Refuse an acceptance criterion that could never be evaluated.

    Called where a task is *written*, not where it runs.  The alternative --
    finding out at 3am that the check was never going to be allowed to run --
    produces a run with no verdict at all, which is strictly worse than
    refusing the definition while somebody is still looking at it.

    This is a gate on the common refusals (an inline interpreter, a high-risk
    command, shell operators), none of which depend on where the command runs.
    The verifier checks again before running, because the answer can change
    between writing a task and its next execution, and *that* check is the
    authoritative one.
    """
    if acceptance is None:
        return
    if len(acceptance.criteria) > MAX_ACCEPTANCE_CRITERIA:
        raise ValueError(
            f"{label}的验收条件最多 {MAX_ACCEPTANCE_CRITERIA} 条，"
            f"当前 {len(acceptance.criteria)} 条"
        )
    for item in acceptance.criteria:
        if len(str(item)) > MAX_ACCEPTANCE_CRITERION_CHARS:
            raise ValueError(
                f"{label}有一条验收条件超过 {MAX_ACCEPTANCE_CRITERION_CHARS} 个字符"
            )
    command = str(acceptance.verify_command or "").strip()
    if not command:
        return
    if len(command) > MAX_VERIFY_COMMAND_CHARS:
        raise ValueError(f"{label}的验收命令超过 {MAX_VERIFY_COMMAND_CHARS} 个字符")
    reason = command_rejection_reason(
        command,
        workspace_root=workspace_root,
        output_dir=output_dir,
    )
    if reason is not None:
        raise ValueError(f"{label}的{reason}")


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
    #: What this task's work has to be true for the run to count as a success.
    #: Empty means nothing was declared, which is not the same as having been
    #: judged and passing -- see :class:`Acceptance`.
    acceptance: Acceptance = field(default_factory=Acceptance)
    #: Set only when the task is one step of a workflow.  Part of the task's
    #: *identity* rather than of its behaviour: an identical definition created
    #: on its own is a different thing from a step, and deduplication that
    #: confused the two would quietly fold one into the other.
    workflow_id: str = ""
    step_key: str = ""
    #: The words that asked for this task, quoted from whoever asked: the
    #: user's own sentence in a conversation, or the prompt of the run that
    #: created it.  Kept on the row for the same reason the acceptance
    #: criterion is -- "why is this here" has to be answerable from the thing
    #: itself, months later, by someone who was not in the room.
    #:
    #: Empty means *no sentence was recorded*, which is not the same as "nobody
    #: asked": rows that predate this column never recorded one, a chain's steps
    #: before the chain existed have none, and a task made by filling in the
    #: interface form was asked for by a click rather than by a sentence.
    #: Inventing a quote for any of those would fabricate evidence, so the
    #: column stays empty and the reader is left with the truth.
    #:
    #: A workflow's steps carry the sentence that asked for the *chain*: no
    #: person asked for one step by name, and a step that read "asked for by
    #: nobody" would be indistinguishable from a task that appeared unasked.
    request_quote: str = ""


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
    acceptance: Acceptance = field(default_factory=Acceptance)
    #: Which workflow this task is a step of, and which step.  Empty for a
    #: standalone task -- most tasks are, and a task does not have to belong to
    #: anything to be scheduled.  Kept on the task rather than looked up from
    #: the graph so an unattended run can tell, from its own row, that it has
    #: consequences downstream when it finishes.
    workflow_id: str = ""
    step_key: str = ""
    #: See :attr:`NewScheduledTask.request_quote`: the words that asked for
    #: this task, carried through to the stored row so the answer to "why does
    #: this exist" does not depend on anyone remembering.
    request_quote: str = ""


#: Terminal statuses that mean "a person should look at this".
#:
#: ``cancelled`` is deliberately absent: the user asked for that one, so
#: surfacing it as something needing attention would train people to ignore
#: the signal.  ``interrupted`` *is* present — a lost lease, a restart or a
#: timeout is exactly the kind of silent outcome nobody is watching for.
#:
#: ``skipped`` is absent for a different reason: it is never the first thing
#: that went wrong.  Something further up failed, and *that* run is already
#: asking for attention.  Listing every step a failure blocked as its own
#: thing to look at would turn one problem into a list of a dozen, and the
#: list is where the one that matters stops being read.
#:
#: ``unverified`` *is* present, and it is the case this list exists for.  A run
#: whose acceptance check could not be evaluated is the one outcome nobody can
#: find by reading a success and nobody is alerted to by reading a failure: the
#: task declared what it needed to be true, and the system could not say whether
#: it was.  Silence here would be the only way to lose that entirely.
ATTENTION_STATUSES: tuple[str, ...] = ("failed", "interrupted", "unverified")


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
    #: What the task's acceptance check said about the work.  ``""`` means no
    #: criterion was declared, so nothing was judged -- which is not the same
    #: as having been judged and passing.
    verdict: str = VERDICT_NONE
    #: The verification result behind the verdict, kept whole so the run detail
    #: can show the exit code and the tail of what the check said.  ``None``
    #: when no criterion was declared.
    verification: Optional[VerificationResult] = None
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
        # Carried on the run so the acceptance check that judges this execution
        # is the one that was declared when it was scheduled.  Reading it from
        # the task at completion time would let an edit made mid-run change
        # what an already-running execution is judged against.
        "acceptance": task.acceptance.to_dict(),
        "delivery_mode": task.delivery_mode,
        "delivery_target": {
            "target_type": task.delivery_target.target_type,
            "payload": task.delivery_target.payload,
        },
    }


#: The kinds of work a step can name.  Kept in step with what the runtime can
#: actually execute -- a workflow that stores a kind nothing runs would fail at
#: the point of use, in the middle of the night, one step into a chain.
STEP_KINDS: tuple[str, ...] = ("agent_prompt", "message", "system_job")

#: The statuses a run occupies while it is still going to happen, or is
#: happening.
#:
#: ``queued`` belongs here and that is the whole point of naming the pair: a
#: run woken by a signal is written down as ``queued`` first and only claimed
#: on a later scheduler tick, so for that window the task has no
#: ``active_run_id`` at all.  Anything asking "is this task busy?" from
#: ``active_run_id`` alone therefore answers no during exactly the window
#: somebody watching a workflow is looking at.
#:
#: The complement of :data:`TERMINAL_RUN_STATUSES` would not do instead: a
#: status invented later would land in it without anybody deciding that it
#: means "in flight".
RUN_IN_FLIGHT_STATUSES: tuple[str, ...] = ("queued", "running")


#: The one terminal status that means "the step's work happened".
#:
#: ``cancelled`` and ``interrupted`` are terminal but are *not* success, and
#: neither is ``failed``.  Naming the single succeeding value rather than
#: listing the failing ones is deliberate: a new terminal status invented later
#: then defaults to "did not succeed", which is the safe way for a downstream
#: step to be wrong.
RUN_SUCCESS_STATUS = "succeeded"

#: The status of a step that was never started because something above it
#: failed.
#:
#: It is its own status rather than ``cancelled`` because the two say different
#: things.  Cancelled means somebody decided not to run it; skipped means the
#: decision was made by a failure three steps up, and the person reading the
#: history needs to be able to tell those apart -- one of them is asking to be
#: fixed, and it is not the one they asked for.
RUN_SKIPPED_STATUS = "skipped"


#: The status of a run whose work was never judged.
#:
#: A task may declare an acceptance check -- a command whose exit code says
#: whether the work met the bar.  When that check cannot be *evaluated* (it was
#: refused, it timed out, it failed to start), the run has not been shown to
#: succeed, and it has not been shown to fail either.  ``failed`` would assert
#: something nobody observed: it would skip every downstream step, raise an
#: alert, and read in the history as "the work was wrong" when the truth is
#: "we never looked".
#:
#: Its own status for the same reason ``skipped`` has one: two different facts
#: that a reader has to be able to tell apart, and collapsing them loses the
#: one that needs a person.
RUN_UNVERIFIED_STATUS = "unverified"


#: The statuses worth trying again when a retry policy allows it.
#:
#: ``unverified`` is here because its cause is usually environmental -- a check
#: that timed out may well pass next time.  ``failed`` is here because that is
#: what it has always meant.  Nothing else is: a cancelled run was cancelled on
#: purpose, and an interrupted one is being resumed by the recovery path.
#:
#: This is a list of *what a retry could help with*, not of what is wrong; the
#: opt-in lives in ``retry_policy.max_attempts``, which defaults to 1, so no
#: task retries unless somebody asked for it.
RETRYABLE_RUN_STATUSES: tuple[str, ...] = ("failed", RUN_UNVERIFIED_STATUS)


#: The no-retry default, spelled out once.  A fresh copy per call because it is
#: handed to a step that may then be edited.
def _default_retry_policy() -> dict[str, Any]:
    return {"max_attempts": 1, "backoff_seconds": 30}


def _step_retry_policy(raw: Any) -> dict[str, Any]:
    """A step's retry policy, read from a stored graph.

    Anything unreadable becomes the no-retry default rather than raising.  A
    graph written before this field existed has no key at all, and that is the
    common case rather than an error -- those steps have never retried, so
    reading them as "do not retry" is what keeps an old workflow behaving the
    way it already behaved.

    The two numbers are clamped to the same bounds the API enforces, because a
    graph can also be written by an agent calling the store directly.  An
    unbounded ``max_attempts`` is a step that never gives up, and the chain
    below it waits forever for a step that is still trying.
    """
    data = raw if isinstance(raw, dict) else {}
    try:
        attempts = int(data.get("max_attempts", 1) or 1)
    except (TypeError, ValueError):
        attempts = 1
    try:
        backoff = int(data.get("backoff_seconds", 30) or 0)
    except (TypeError, ValueError):
        backoff = 30
    return {
        "max_attempts": min(MAX_RETRY_ATTEMPTS, max(1, attempts)),
        "backoff_seconds": min(MAX_RETRY_BACKOFF_SECONDS, max(0, backoff)),
    }


@dataclass
class WorkflowStep:
    """One step of a workflow: a task, plus what has to finish before it runs.

    A step is a task definition *without a trigger* whenever something else has
    to finish first, because in that case the upstreams are the trigger.  A step
    with no upstreams is an entry step and keeps a trigger of its own -- it is
    an ordinary scheduled task, which is why a workflow can start from a clock,
    from an external signal, or from anything else the scheduler already
    understands.

    Storing ``trigger`` as optional rather than inventing a "workflow trigger"
    type keeps that asymmetry visible in the data: whoever reads a step can see
    whether it starts things or follows them, instead of having to work it out
    from the graph.
    """

    key: str
    name: str
    kind: str
    payload: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    #: Entry steps only.  Rejected on a step that has upstreams, because "run
    #: at 9am" and "run after A" are two different answers to when this runs,
    #: and quietly preferring one of them would make the other a lie.
    trigger: Optional[TriggerSpec] = None
    workspace_root: str = ""
    permission_profile: str = "inherit"
    context_policy: str = "stateless"
    model_override: Optional[str] = None
    timeout_seconds: int = 1800
    selected_skills: list[str] = field(default_factory=list)
    #: What this step's work has to be true for it to count as a success.
    #: Load-bearing for the graph rather than merely informative: a dependent
    #: step runs only when its upstreams *succeeded*, so this is what decides
    #: whether the chain continues or stops here.
    acceptance: Acceptance = field(default_factory=Acceptance)
    delivery_mode: str = "standalone"
    delivery_target: Optional[DeliveryTarget] = None
    #: How many times this step's work is retried before the run is called
    #: failed, and how long to wait between attempts.  Carried on the step for
    #: the same reason ``acceptance`` is: a failed step stops every step below
    #: it, so "try twice before giving up" is a statement about the chain, not
    #: about one task.  Without it every step of every workflow is built with
    #: ``max_attempts=1`` and a flake in the middle of a chain takes the rest of
    #: the chain down with it -- while the identical task created on its own can
    #: be told to retry.
    #:
    #: ``overlap_policy`` and ``missed_run_policy`` are deliberately *not* here.
    #: Nothing reads them (see the note above :class:`NewScheduledTask`), so a
    #: step that carried them would offer a choice the runtime does not honour.
    retry_policy: dict[str, Any] = field(default_factory=_default_retry_policy)

    def is_entry(self) -> bool:
        return not self.depends_on

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "payload": self.payload,
            "depends_on": list(self.depends_on),
            "trigger": self.trigger.to_json() if self.trigger is not None else None,
            "workspace_root": self.workspace_root,
            "permission_profile": self.permission_profile,
            "context_policy": self.context_policy,
            "model_override": self.model_override,
            "timeout_seconds": int(self.timeout_seconds),
            "selected_skills": list(self.selected_skills),
            "acceptance": self.acceptance.to_dict(),
            "retry_policy": dict(self.retry_policy),
            "delivery_mode": self.delivery_mode,
            "delivery_target": (
                self.delivery_target.to_json()
                if self.delivery_target is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        raw_trigger = data.get("trigger")
        raw_target = data.get("delivery_target")
        return cls(
            key=str(data.get("key", "")).strip(),
            name=str(data.get("name", "")).strip(),
            kind=str(data.get("kind", "")).strip(),
            payload=dict(data.get("payload") or {}),
            depends_on=[
                str(item).strip()
                for item in (data.get("depends_on") or [])
                if str(item).strip()
            ],
            trigger=(
                TriggerSpec.from_json(raw_trigger)
                if isinstance(raw_trigger, str) and raw_trigger.strip()
                else None
            ),
            workspace_root=str(data.get("workspace_root", "") or ""),
            permission_profile=str(
                data.get("permission_profile", "inherit") or "inherit"
            ),
            context_policy=str(data.get("context_policy", "stateless") or "stateless"),
            model_override=data.get("model_override") or None,
            timeout_seconds=int(data.get("timeout_seconds", 1800) or 1800),
            selected_skills=[
                str(item)
                for item in (data.get("selected_skills") or [])
                if str(item).strip()
            ],
            acceptance=Acceptance.from_dict(data.get("acceptance")),
            # A graph stored before this field existed has no such key, and
            # reads back as the no-retry default -- which is what those steps
            # have always done, so an old workflow behaves tomorrow the way it
            # behaved yesterday.
            retry_policy=_step_retry_policy(data.get("retry_policy")),
            delivery_mode=str(data.get("delivery_mode", "standalone") or "standalone"),
            delivery_target=(
                DeliveryTarget.from_json(raw_target)
                if isinstance(raw_target, str) and raw_target.strip()
                else None
            ),
        )


@dataclass
class Workflow:
    """A named graph of steps, where the edges are "this finished, so run that".

    The edges are stored rather than inferred from signal names on purpose.
    Two steps connected by a join of task signals are indistinguishable, from
    the signals alone, from two unrelated tasks that happen to subscribe to
    each other -- and the difference is exactly what a skip has to know: when a
    step fails, the steps below it must not run, and only the graph can say
    which those are.
    """

    name: str
    steps: list[WorkflowStep]
    id: str = ""
    description: str = ""
    enabled: bool = True
    #: The words that asked for this chain, quoted from whoever asked for it.
    #: Not part of :meth:`to_graph`: the graph is what the chain *is*, and this
    #: is what put it there -- the same relationship ``created_at`` has to it.
    #: Kept when the graph is edited, because rewriting a step does not change
    #: who asked for the chain in the first place.
    request_quote: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def step(self, key: str) -> Optional[WorkflowStep]:
        wanted = str(key or "").strip()
        for item in self.steps:
            if item.key == wanted:
                return item
        return None

    def to_graph(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "enabled": bool(self.enabled),
            "steps": [item.to_dict() for item in self.steps],
        }

    @classmethod
    def from_graph(
        cls,
        raw: str,
        *,
        workflow_id: str = "",
        request_quote: str = "",
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> "Workflow":
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("workflow graph must be a JSON object")
        steps = [
            WorkflowStep.from_dict(item)
            for item in (data.get("steps") or [])
            if isinstance(item, dict)
        ]
        return cls(
            id=workflow_id,
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            enabled=bool(data.get("enabled", True)),
            steps=steps,
            request_quote=str(request_quote or ""),
            created_at=created_at,
            updated_at=updated_at,
        )


def workflow_step_order(steps: list[WorkflowStep]) -> list[str]:
    """The step keys in an order where every step follows its upstreams.

    Raises ``ValueError`` on anything that makes such an order impossible:
    a step naming an upstream that does not exist, a step that names itself,
    or a cycle.  The message names the steps involved, because "the graph has a
    cycle" is not actionable -- which steps, and in what order, is.

    Materialization needs this order and not just the check: a downstream
    step's trigger is built from its upstreams' task ids, so those tasks have
    to exist first.
    """
    by_key: dict[str, WorkflowStep] = {}
    for step in steps:
        key = str(step.key or "").strip()
        if not key:
            raise ValueError("每个步骤都需要一个 key")
        if key in by_key:
            raise ValueError(f"步骤 key 重复：{key}")
        by_key[key] = step
    if not by_key:
        raise ValueError("workflow 至少要有一个步骤")

    for step in steps:
        key = str(step.key).strip()
        edges = [str(item).strip() for item in step.depends_on]
        duplicates = [item for item in dict.fromkeys(edges) if edges.count(item) > 1]
        if duplicates:
            raise ValueError(f"步骤 {key} 重复声明了上游：{'、'.join(duplicates)}")
        if key in edges:
            raise ValueError(f"步骤 {key} 不能依赖自己")
        missing = [item for item in edges if item not in by_key]
        if missing:
            raise ValueError(f"步骤 {key} 依赖了不存在的上游：{'、'.join(missing)}")

    order: list[str] = []
    visiting: list[str] = []
    placed: set[str] = set()

    def visit(key: str) -> None:
        if key in placed:
            return
        if key in visiting:
            # Cut the cycle open at the repeat, so the message can show the
            # ring itself rather than the path that happened to reach it.
            ring = visiting[visiting.index(key) :] + [key]
            raise ValueError("workflow 存在循环依赖：" + " → ".join(ring))
        visiting.append(key)
        for upstream in by_key[key].depends_on:
            visit(str(upstream).strip())
        visiting.pop()
        placed.add(key)
        order.append(key)

    for key in list(by_key):
        visit(key)
    return order


def validate_workflow_graph(
    steps: list[WorkflowStep],
    *,
    acceptance_workspaces: Optional[dict[str, str]] = None,
    output_dir: str = "",
) -> list[str]:
    """Check a graph is buildable, and return its step order.

    Everything a workflow can be wrong about is wrong *before* it runs: a step
    with no work in it, a step whose trigger contradicts its edges, a cycle.
    Catching them here means the failure lands where somebody can still read
    it, rather than as a task that quietly never fires.

    ``acceptance_workspaces`` maps a step key to the folder that step will run
    in, and is what makes the acceptance criterion checkable at this point.
    It is optional because the caller that knows the folders is the store, and
    because the command safety gate is root-sensitive: checking a criterion
    against a folder the step will not use would refuse a perfectly good
    command.  Omitting it skips that one check and does everything else.
    """
    order = workflow_step_order(steps)
    by_key = {str(step.key).strip(): step for step in steps}
    for key in order:
        step = by_key[key]
        if not str(step.name or "").strip():
            raise ValueError(f"步骤 {key} 缺少名称")
        if step.kind not in STEP_KINDS:
            raise ValueError(
                f"步骤 {key} 的执行类型不支持：{step.kind!r}；"
                "可选：" + "、".join(STEP_KINDS)
            )
        payload = step.payload or {}
        if step.kind == "message" and not str(payload.get("message_text", "")).strip():
            raise ValueError(f"步骤 {key} 是消息任务，但缺少 message_text")
        if (
            step.kind == "agent_prompt"
            and not str(payload.get("prompt", "")).strip()
        ):
            raise ValueError(f"步骤 {key} 是 Agent 任务，但缺少 prompt")
        if step.kind == "system_job" and not str(payload.get("job_name", "")).strip():
            raise ValueError(f"步骤 {key} 是系统任务，但缺少 job_name")
        if step.is_entry():
            if step.trigger is None:
                raise ValueError(
                    f"步骤 {key} 没有任何上游，必须自带触发方式（时间或信号）"
                )
        elif step.trigger is not None:
            raise ValueError(
                f"步骤 {key} 有上游，它的触发方式由上游决定；"
                "不能另外再给它一个时间或信号触发，否则「什么时候运行」会有两个答案"
            )
        if int(step.timeout_seconds) <= 0:
            raise ValueError(f"步骤 {key} 的超时时间必须为正数")
        if acceptance_workspaces is not None:
            validate_acceptance(
                step.acceptance,
                workspace_root=str(acceptance_workspaces.get(key, "") or ""),
                output_dir=output_dir,
                label=f"步骤「{key}」",
            )
    return order


def step_trigger_spec(
    step: WorkflowStep, upstream_task_ids: list[str]
) -> TriggerSpec:
    """The trigger a step gets once its upstreams are real tasks.

    A dependent step waits for **all** of its upstreams to have succeeded, and
    it waits on their success signals specifically -- not on "they finished".
    A step that ran because its upstream failed would usually produce something
    wrong rather than nothing, and wrong output that looks like a result is
    worse than a step that did not run.
    """
    if step.is_entry():
        if step.trigger is None:
            raise ValueError(f"步骤 {step.key} 没有触发方式")
        return step.trigger
    if len(upstream_task_ids) != len(step.depends_on):
        raise ValueError(
            f"步骤 {step.key} 需要 {len(step.depends_on)} 个上游任务 id，"
            f"只拿到 {len(upstream_task_ids)} 个"
        )
    return TriggerSpec.signal_all(
        [
            task_signal_name(str(task_id), RUN_SUCCESS_STATUS)
            for task_id in upstream_task_ids
        ]
    )


def workflow_downstream_steps(steps: list[WorkflowStep], key: str) -> list[str]:
    """Every step that would be blocked by *key* failing, transitively.

    Used when a step fails: the steps that depend on it, and the steps that
    depend on those, are all waiting for a success that is no longer coming.
    Returning the whole set at once -- rather than just the direct children --
    is what keeps a chain from stopping halfway and leaving the rest looking
    like tasks nobody has gotten around to running.
    """
    blocked: set[str] = set()
    frontier = [str(key).strip()]
    while frontier:
        current = frontier.pop()
        for step in steps:
            step_key = str(step.key).strip()
            if step_key in blocked:
                continue
            if current in [str(item).strip() for item in step.depends_on]:
                blocked.add(step_key)
                frontier.append(step_key)
    return [str(step.key).strip() for step in steps if str(step.key).strip() in blocked]


@dataclass
class ClaimedTask:
    task: ScheduledTask
    run: TaskRun


@dataclass
class ExecutionResult:
    summary: str
    text_output: str
    output_path: str = ""
    #: What the run said about its own work, via ``report_outcome``.  Empty
    #: when it said nothing, which is the ordinary case.
    #:
    #: A self-report can only ever *lower* the verdict -- see
    #: :func:`agent.verification.combine_verdicts`.  An agent saying "I could
    #: not do this" is worth acting on; an agent saying "I did this" is not
    #: evidence, and must not be able to stand in for an acceptance check that
    #: could not run.
    self_report_verdict: str = VERDICT_NONE
    self_report_reason: str = ""


@dataclass
class DeliveryResult:
    status: str
    output_path: str = ""
    error: str = ""
