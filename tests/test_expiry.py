from datetime import UTC, date, datetime, timedelta, timezone, tzinfo

import pytest

from padeploy.expiry import deadline, describe, is_due

EXPIRY = date(2026, 11, 23)
EST = timezone(timedelta(hours=-5), "EST")


def utc(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A 2026 time in UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def test_deadline_is_midnight_utc_on_the_expiry_date() -> None:
    assert deadline(EXPIRY) == utc(11, 23)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(utc(11, 15, 23, 59), False, id="just-over-a-week"),
        pytest.param(utc(11, 16), True, id="exactly-a-week"),
        pytest.param(utc(11, 22, 12), True, id="the-day-before"),
        pytest.param(utc(11, 23), True, id="at-the-deadline"),
        pytest.param(utc(12, 1), True, id="past"),
    ],
)
def test_is_due(now: datetime, expected: bool) -> None:
    assert is_due(EXPIRY, now) is expected


@pytest.mark.parametrize(
    ("now", "tz", "expected"),
    [
        pytest.param(
            utc(10, 30),
            EST,
            "x.example.com expires in 24 days (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="days",
        ),
        pytest.param(
            utc(11, 20, 20),
            EST,
            "x.example.com expires in 2 days 4 hours (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="days-and-hours",
        ),
        pytest.param(
            utc(11, 22),
            EST,
            "x.example.com expires in 1 day (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="one-day",
        ),
        pytest.param(
            utc(11, 22, 23),
            EST,
            "x.example.com expires in 1 hour (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="one-hour",
        ),
        pytest.param(
            utc(11, 22, 23, 30),
            EST,
            "x.example.com expires in under an hour (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="under-an-hour",
        ),
        pytest.param(
            utc(11, 23),
            EST,
            "x.example.com expired (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="at-the-deadline",
        ),
        pytest.param(
            utc(12, 1),
            EST,
            "x.example.com expired (2026-11-23 00:00 UTC, 2026-11-22 19:00 EST).",
            id="past",
        ),
        pytest.param(
            utc(10, 30),
            UTC,
            "x.example.com expires in 24 days (2026-11-23 00:00 UTC).",
            id="utc-is-not-repeated",
        ),
    ],
)
def test_describe(now: datetime, tz: tzinfo, expected: str) -> None:
    assert describe("x.example.com", EXPIRY, now, tz) == expected
