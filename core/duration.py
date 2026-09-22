from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
MONTH = 30 * DAY
YEAR = 365 * DAY

_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": MINUTE,
    "min": MINUTE,
    "mins": MINUTE,
    "minute": MINUTE,
    "minutes": MINUTE,
    "h": HOUR,
    "hr": HOUR,
    "hrs": HOUR,
    "hour": HOUR,
    "hours": HOUR,
    "d": DAY,
    "day": DAY,
    "days": DAY,
    "w": WEEK,
    "week": WEEK,
    "weeks": WEEK,
    "mo": MONTH,
    "month": MONTH,
    "months": MONTH,
    "y": YEAR,
    "yr": YEAR,
    "year": YEAR,
    "years": YEAR,
}

_TOKEN = re.compile(r"(\d+)\s*([a-zA-Z]+)")

PERMANENT_WORDS = {"perm", "permanent", "forever", "infinite", "inf", "0"}

MAX_TIMEOUT_SECONDS = 28 * DAY


def is_permanent(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip().casefold() in PERMANENT_WORDS


def parse(value: str | None) -> int | None:
    if is_permanent(value):
        return None

    assert value is not None
    total = 0
    for amount, unit in _TOKEN.findall(value):
        seconds = _UNITS.get(unit.casefold())
        if seconds is None:
            continue
        total += int(amount) * seconds

    if total <= 0:
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped) * MINUTE
        return None
    return total


def humanise(seconds: int | None) -> str:
    if seconds is None:
        return "Permanent"

    parts: list[tuple[int, str]] = [
        (YEAR, "year"),
        (MONTH, "month"),
        (WEEK, "week"),
        (DAY, "day"),
        (HOUR, "hour"),
        (MINUTE, "minute"),
        (1, "second"),
    ]

    remaining = int(seconds)
    chunks: list[str] = []
    for size, name in parts:
        if remaining >= size:
            count, remaining = divmod(remaining, size)
            chunks.append(f"{count} {name}{'s' if count != 1 else ''}")
        if len(chunks) == 2:
            break
    return " ".join(chunks) if chunks else "0 seconds"


def span_label(seconds: int | None) -> str:
    if seconds is None:
        return "Permanent"

    total = int(seconds)
    if total <= 0:
        return "Permanent"

    for size, name in ((YEAR, "Year"), (MONTH, "Month"), (WEEK, "Week"), (DAY, "Day"), (HOUR, "Hour"), (MINUTE, "Minute")):
        if total % size == 0:
            return f"{total // size} {name}"
    return humanise(total)


def expiry(seconds: int | None) -> datetime | None:
    if seconds is None:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def timestamp(when: datetime | float | None, style: str = "F") -> str:
    if when is None:
        return "Never"
    if isinstance(when, datetime):
        value = int(when.timestamp())
    else:
        value = int(when)
    return f"<t:{value}:{style}>"


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()
