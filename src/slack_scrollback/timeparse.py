"""Time-window parsing for ``--since`` / ``--until``.

Accepts the forms a person actually types — ``7d``, ``24h``, ``today``,
``2026-01-31`` — and resolves them against the local timezone, matching how
Slack itself presents timestamps. A bare date means the whole day: as a lower
bound it starts at midnight, as an upper bound it runs to the last instant of
that day, so ``--until 2026-01-31`` includes the 31st rather than silently
excluding it.
"""

from __future__ import annotations

import datetime as dt
import re

from .errors import UsageError

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_DATE_FORMATS = ("%Y-%m-%d",)
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)

SYNTAX_HELP = (
    "use a duration like 7d, 24h or 30m; a date like 2026-01-31; "
    "a datetime like 2026-01-31T14:00; or one of: today, yesterday"
)


def _local_now(now: dt.datetime | None) -> dt.datetime:
    return now if now is not None else dt.datetime.now().astimezone()


def _localize(naive: dt.datetime, reference: dt.datetime | None) -> dt.datetime:
    """Attach the local timezone *as it stood on that date*.

    ``datetime.now().astimezone()`` yields a fixed offset — today's. Reusing it
    for an arbitrary date puts every timestamp on the other side of a daylight
    saving boundary an hour out. A naive datetime's own ``astimezone()`` consults
    the platform's rules for that specific moment, which is what a bare date
    means to the person who typed it.

    An explicit ``reference`` (the tests' injected clock) keeps its tzinfo, so
    parsing stays deterministic under a fixed offset.
    """
    if reference is not None and reference.tzinfo is not None:
        return naive.replace(tzinfo=reference.tzinfo)
    return naive.astimezone()


def _end_of_day(day: dt.date, reference: dt.datetime | None) -> dt.datetime:
    return _localize(dt.datetime.combine(day, dt.time(23, 59, 59, 999999)), reference)


def _start_of_day(day: dt.date, reference: dt.datetime | None) -> dt.datetime:
    return _localize(dt.datetime.combine(day, dt.time(0, 0, 0, 0)), reference)


def parse_time(text: str, *, flag: str, upper_bound: bool = False, now: dt.datetime | None = None) -> float:
    """Resolve one ``--since``/``--until`` value to epoch seconds.

    ``upper_bound`` extends bare dates to the end of the day so an ``--until``
    date behaves inclusively.
    """
    raw = text.strip()
    if not raw:
        raise UsageError(f"{flag} is empty — {SYNTAX_HELP}")

    reference = _local_now(now)

    lowered = raw.lower()
    if lowered == "today":
        day = reference.date()
        moment = _end_of_day(day, now) if upper_bound else _start_of_day(day, now)
        return moment.timestamp()
    if lowered == "yesterday":
        day = reference.date() - dt.timedelta(days=1)
        moment = _end_of_day(day, now) if upper_bound else _start_of_day(day, now)
        return moment.timestamp()

    duration = _DURATION_RE.match(lowered)
    if duration:
        # A duration is an elapsed span, so it counts back from the instant `now`
        # denotes; the local offset never enters into it.
        amount = int(duration.group(1))
        seconds = amount * _UNIT_SECONDS[duration.group(2).lower()]
        return (reference - dt.timedelta(seconds=seconds)).timestamp()

    for fmt in _DATETIME_FORMATS:
        try:
            naive = dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return _localize(naive, now).timestamp()

    for fmt in _DATE_FORMATS:
        try:
            day = dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        moment = _end_of_day(day, now) if upper_bound else _start_of_day(day, now)
        return moment.timestamp()

    raise UsageError(f"cannot understand {flag} {raw!r} — {SYNTAX_HELP}")


def to_slack_ts(epoch: float) -> str:
    """Render epoch seconds as a Slack timestamp bound.

    Slack compares ``oldest``/``latest`` against the full ``seconds.microseconds``
    string; truncating to whole seconds moves the boundary by up to a second.
    """
    return f"{epoch:.6f}"
