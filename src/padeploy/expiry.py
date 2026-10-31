"""When a free web app stops, and how long is left."""

from datetime import UTC, date, datetime, timedelta, tzinfo

WARNING_DAYS = 7


def deadline(expiry: date) -> datetime:
    """When the web app stops: 00:00 UTC on ``expiry`` (assumed; the API gives only the date)."""
    return datetime(expiry.year, expiry.month, expiry.day, tzinfo=UTC)


def is_due(expiry: date, now: datetime) -> bool:
    """Whether the web app stops within ``WARNING_DAYS`` of ``now``, or already has."""
    return deadline(expiry) - now <= timedelta(days=WARNING_DAYS)


def describe(domain: str, expiry: date, now: datetime, tz: tzinfo | None = None) -> str:
    """When ``domain`` stops, with the deadline in UTC and in ``tz`` (default: local time)."""
    stop = deadline(expiry)
    when = f"{stop:%Y-%m-%d %H:%M UTC}"
    if (local := stop.astimezone(tz)).utcoffset() != timedelta(0):
        when += f", {local:%Y-%m-%d %H:%M %Z}"
    if (left := stop - now) <= timedelta(0):
        return f"{domain} expired ({when})."
    return f"{domain} expires in {_duration(left)} ({when})."


def _duration(left: timedelta) -> str:
    """``left`` (positive) as whole days and hours, e.g. ``3 days 4 hours``."""
    units = ((left.days, "day"), (left.seconds // 3600, "hour"))
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in units if n]
    return " ".join(parts) or "under an hour"
