"""Parsing the time windows people actually type."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from slack_scrollback.errors import UsageError
from slack_scrollback.timeparse import parse_time, to_slack_ts

TZ = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 7, 15, 14, 30, 0, tzinfo=TZ)


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=TZ).timestamp()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", NOW - dt.timedelta(seconds=30)),
        ("30m", NOW - dt.timedelta(minutes=30)),
        ("24h", NOW - dt.timedelta(hours=24)),
        ("7d", NOW - dt.timedelta(days=7)),
        ("2w", NOW - dt.timedelta(weeks=2)),
    ],
)
def test_durations_count_back_from_now(text: str, expected: dt.datetime) -> None:
    assert parse_time(text, flag="--since", now=NOW) == expected.timestamp()


def test_durations_ignore_case_and_inner_space() -> None:
    assert parse_time("7D", flag="--since", now=NOW) == parse_time("7 d", flag="--since", now=NOW)


def test_today_starts_at_midnight() -> None:
    assert parse_time("today", flag="--since", now=NOW) == at(2026, 7, 15, 0, 0, 0)


def test_today_as_an_upper_bound_runs_to_the_end_of_the_day() -> None:
    assert parse_time("today", flag="--until", upper_bound=True, now=NOW) == at(2026, 7, 15, 23, 59, 59) + 0.999999


def test_yesterday_is_the_whole_previous_day() -> None:
    assert parse_time("yesterday", flag="--since", now=NOW) == at(2026, 7, 14, 0, 0, 0)
    assert parse_time("yesterday", flag="--until", upper_bound=True, now=NOW) == at(2026, 7, 14, 23, 59, 59) + 0.999999


def test_a_bare_date_as_a_lower_bound_starts_the_day() -> None:
    assert parse_time("2026-01-31", flag="--since", now=NOW) == at(2026, 1, 31, 0, 0, 0)


def test_a_bare_date_as_an_upper_bound_includes_that_whole_day() -> None:
    """--until 2026-01-31 must include the 31st, not stop at its first instant."""
    parsed = parse_time("2026-01-31", flag="--until", upper_bound=True, now=NOW)
    assert parsed > at(2026, 1, 31, 23, 59, 0)
    assert parsed < at(2026, 2, 1, 0, 0, 0)


@pytest.mark.parametrize(
    "text",
    ["2026-01-31T14:00", "2026-01-31T14:00:00", "2026-01-31 14:00", "2026-01-31 14:00:00"],
)
def test_datetimes_are_accepted_in_the_obvious_shapes(text: str) -> None:
    assert parse_time(text, flag="--since", now=NOW) == at(2026, 1, 31, 14, 0, 0)


def test_an_explicit_time_is_not_widened_by_upper_bound() -> None:
    assert parse_time("2026-01-31T14:00", flag="--until", upper_bound=True, now=NOW) == at(2026, 1, 31, 14, 0, 0)


@pytest.mark.parametrize("text", ["", "   ", "soon", "last tuesday", "7", "d7", "7x", "2026-13-45", "31/01/2026"])
def test_unparseable_values_explain_the_accepted_syntax(text: str) -> None:
    with pytest.raises(UsageError) as caught:
        parse_time(text, flag="--since", now=NOW)
    message = str(caught.value)
    assert "--since" in message
    assert "7d" in message and "today" in message


def test_the_error_names_the_flag_that_was_wrong() -> None:
    with pytest.raises(UsageError) as caught:
        parse_time("nonsense", flag="--until", now=NOW)
    assert "--until" in str(caught.value)


def test_slack_timestamps_keep_six_decimal_places() -> None:
    assert to_slack_ts(1700000000.0) == "1700000000.000000"
    assert to_slack_ts(1700000000.5) == "1700000000.500000"


def test_a_bare_date_uses_the_offset_in_force_on_that_date(local_zone: Any) -> None:
    """A summer date parsed in winter must still mean local midnight in summer.

    Taking today's UTC offset and reusing it for an arbitrary date puts every
    timestamp on the far side of a daylight saving boundary an hour out, quietly
    shifting which messages fall inside a window.
    """
    local_zone("Europe/Zurich")
    # Zurich is UTC+1 in January and UTC+2 in July; both are midnight *locally*.
    assert (
        parse_time("2026-01-15", flag="--since")
        == dt.datetime(2026, 1, 15, tzinfo=dt.timezone(dt.timedelta(hours=1))).timestamp()
    )
    assert (
        parse_time("2026-07-15", flag="--since")
        == dt.datetime(2026, 7, 15, tzinfo=dt.timezone(dt.timedelta(hours=2))).timestamp()
    )


def test_a_datetime_uses_the_offset_in_force_on_that_date(local_zone: Any) -> None:
    local_zone("Europe/Zurich")
    winter = parse_time("2026-01-15T12:00", flag="--since")
    summer = parse_time("2026-07-15T12:00", flag="--since")
    assert dt.datetime.fromtimestamp(winter, dt.UTC).hour == 11  # noon at UTC+1
    assert dt.datetime.fromtimestamp(summer, dt.UTC).hour == 10  # noon at UTC+2


def test_parsing_without_an_explicit_now_uses_the_local_clock() -> None:
    before = dt.datetime.now().timestamp()
    parsed = parse_time("0s", flag="--since")
    after = dt.datetime.now().timestamp()
    assert before <= parsed <= after


# -- durations (`--recheck`) --------------------------------------------------


class TestParseDuration:
    def test_resolves_each_unit_to_seconds(self) -> None:
        from slack_scrollback.timeparse import parse_duration

        assert parse_duration("7d", flag="--recheck") == 7 * 86400.0
        assert parse_duration("24h", flag="--recheck") == 86400.0
        assert parse_duration("30m", flag="--recheck") == 1800.0
        assert parse_duration("45s", flag="--recheck") == 45.0
        assert parse_duration("2w", flag="--recheck") == 2 * 604800.0

    def test_tolerates_case_and_whitespace(self) -> None:
        from slack_scrollback.timeparse import parse_duration

        assert parse_duration(" 7D ", flag="--recheck") == 7 * 86400.0

    @pytest.mark.parametrize("bad", ["", "7", "d", "2026-01-31", "today", "7 days", "-3d"])
    def test_rejects_everything_that_is_not_a_bare_duration(self, bad: str) -> None:
        """A date is a point in time; `--recheck` asks for a span. Accepting one
        would silently mean something other than what was typed."""
        from slack_scrollback.timeparse import parse_duration

        with pytest.raises(UsageError) as excinfo:
            parse_duration(bad, flag="--recheck")
        assert "--recheck" in str(excinfo.value)
