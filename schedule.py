"""Pure-logic schedule checker for Format A schedule files.

Answers one question: given the content of a schedule file and a moment on the
local clock, is running allowed? This module is pure logic — it never touches
windows, the screen, processes, or the clock itself; the caller supplies the
moment.

Format A:
    {
      "mon-fri": ["08:00-17:00"],
      "sat": ["10:00-12:30", "13:30-18:00"],
      "all": ["22:00-02:00"]
    }

Keys are day names (case-insensitive ``"mon"``...``"sun"``), day spans such as
``"mon-fri"`` (including week-wrapping spans such as ``"sat-mon"``), or the
special key ``"all"``. Values are non-empty lists of time ranges written
``"HH:MM-HH:MM"`` or ``"HH:MM:SS-HH:MM:SS"`` (seconds optional, but uniform
within a single range). A range start counts as inside, its end as outside.
Ranges may cross midnight.
"""

import datetime
import json
import os
import re
from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Optional, Tuple, Union

SCHEDULE_FILENAME = "schedule.json"

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_INDEX = {name: index for index, name in enumerate(_DAY_NAMES)}
_ALL_DAYS = frozenset(_DAY_INDEX.values())

_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_MINUTE = 60


class ScheduleError(Exception):
    """Raised when schedule content is broken.

    The message always names the offending key or range so the fault can be
    located in the file.
    """


@dataclass(frozen=True)
class TimeRange:
    """A start and end clock time, in seconds since local midnight.

    ``start`` is inclusive and ``end`` is exclusive. ``end`` may be smaller
    than ``start``, which means the range crosses midnight.
    """

    start: int
    end: int

    @property
    def crosses_midnight(self) -> bool:
        return self.start > self.end

    def covers(self, seconds: int) -> bool:
        """Return True when *seconds* (since midnight) fall inside the range."""
        if self.crosses_midnight:
            return seconds >= self.start or seconds < self.end
        return self.start <= seconds < self.end


@dataclass(frozen=True)
class ScheduleEntry:
    """A set of weekdays plus the time ranges that apply on them."""

    days: FrozenSet[int]
    ranges: Tuple[TimeRange, ...]


class Schedule:
    """A parsed schedule. Ask it whether a given moment is allowed.

    A moment is allowed when any entry whose days match has a range that covers
    the moment. Matching entries and ranges are joined (union); overlaps need
    no conflict handling.
    """

    def __init__(self, entries: Iterable[ScheduleEntry]):
        self.entries: List[ScheduleEntry] = list(entries)

    def is_allowed(self, moment: datetime.datetime) -> bool:
        """Return True when *moment* (local clock) falls in an allowed period."""
        day = moment.weekday()  # Monday == 0 ... Sunday == 6
        previous_day = (day - 1) % 7
        seconds = (
            moment.hour * _SECONDS_PER_HOUR
            + moment.minute * _SECONDS_PER_MINUTE
            + moment.second
        )
        for entry in self.entries:
            for time_range in entry.ranges:
                if time_range.crosses_midnight:
                    # The part before midnight belongs to this day; the part
                    # after midnight belongs to the day the range started on.
                    if seconds >= time_range.start and day in entry.days:
                        return True
                    if seconds < time_range.end and previous_day in entry.days:
                        return True
                elif time_range.covers(seconds) and day in entry.days:
                    return True
        return False


def _parse_day_key(key: str) -> FrozenSet[int]:
    """Expand a day name, day span, or ``"all"`` into weekday indices."""
    normalized = key.strip().lower()

    if normalized == "all":
        return _ALL_DAYS

    if "-" in normalized:
        parts = normalized.split("-")
        if len(parts) != 2:
            raise ScheduleError(f"Invalid day key '{key}'")
        start_name, end_name = parts[0].strip(), parts[1].strip()
        if start_name not in _DAY_INDEX or end_name not in _DAY_INDEX:
            raise ScheduleError(f"Invalid day key '{key}'")
        start, end = _DAY_INDEX[start_name], _DAY_INDEX[end_name]
        days = set()
        cursor = start
        while True:
            days.add(cursor)
            if cursor == end:
                break
            cursor = (cursor + 1) % 7
        return frozenset(days)

    if normalized not in _DAY_INDEX:
        raise ScheduleError(f"Invalid day key '{key}'")
    return frozenset({_DAY_INDEX[normalized]})


def _parse_time_token(token: str, key: str, raw: str) -> Tuple[int, bool]:
    """Return ``(seconds_since_midnight, has_seconds)`` for one clock time."""
    match = _TIME_RE.match(token)
    if not match:
        raise ScheduleError(
            f"Invalid time range '{raw}' for day key '{key}': "
            f"bad time '{token}'"
        )
    hours, minutes = int(match.group(1)), int(match.group(2))
    seconds_text = match.group(3)
    seconds = int(seconds_text) if seconds_text is not None else 0
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ScheduleError(
            f"Invalid time range '{raw}' for day key '{key}': "
            f"time out of range '{token}'"
        )
    return (
        hours * _SECONDS_PER_HOUR + minutes * _SECONDS_PER_MINUTE + seconds,
        seconds_text is not None,
    )


def _parse_range(raw: object, key: str) -> TimeRange:
    """Parse one ``"HH:MM-HH:MM"`` / ``"HH:MM:SS-HH:MM:SS"`` range string."""
    if not isinstance(raw, str):
        raise ScheduleError(
            f"Range for day key '{key}' must be a string, got {raw!r}"
        )

    parts = raw.split("-")
    if len(parts) != 2:
        raise ScheduleError(f"Invalid time range '{raw}' for day key '{key}'")

    start_seconds, start_has_seconds = _parse_time_token(parts[0].strip(), key, raw)
    end_seconds, end_has_seconds = _parse_time_token(parts[1].strip(), key, raw)

    if start_has_seconds != end_has_seconds:
        raise ScheduleError(
            f"Invalid time range '{raw}' for day key '{key}': "
            "start and end must use the same precision"
        )
    if start_seconds == end_seconds:
        raise ScheduleError(
            f"Time range '{raw}' for day key '{key}' has equal start and end"
        )
    return TimeRange(start_seconds, end_seconds)


def parse_schedule(content: str) -> Schedule:
    """Parse Format A schedule text into a :class:`Schedule`.

    Raises :class:`ScheduleError` with a message naming the offending key or
    range on any break.
    """
    if content is None or not content.strip():
        raise ScheduleError("Schedule content is empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ScheduleError(f"Schedule is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise ScheduleError(
            f"Schedule must be a JSON object, got {type(data).__name__}"
        )
    if not data:
        raise ScheduleError("Schedule object is empty: it defines no day keys")

    entries: List[ScheduleEntry] = []
    for key, value in data.items():
        days = _parse_day_key(key)
        if not isinstance(value, list):
            raise ScheduleError(
                f"Day key '{key}' must map to a non-empty list of time ranges"
            )
        if not value:
            raise ScheduleError(f"Day key '{key}' has an empty time range list")
        ranges = tuple(_parse_range(item, key) for item in value)
        entries.append(ScheduleEntry(days=days, ranges=ranges))

    return Schedule(entries)


def load_schedule(path: Union[str, os.PathLike]) -> Optional[Schedule]:
    """Read a schedule file and parse it, or return ``None`` when it is absent.

    Three outcomes, the same ones the monitor loop re-checks every cycle:

    * no file at *path* → return ``None`` (feature off; run as before),
    * a valid file → return the parsed :class:`Schedule`,
    * a broken or empty file → raise :class:`ScheduleError` naming the fault.

    Pure file reading: it never touches windows, the screen, processes, or the
    clock. ``path`` may be a :class:`str` or any path-like object.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as error:
        raise ScheduleError(
            f"Schedule file is not valid UTF-8 text: {error}"
        ) from error
    return parse_schedule(content)
