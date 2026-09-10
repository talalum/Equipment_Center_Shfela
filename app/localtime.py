"""
The local timezone — Israel by default, from TZ_NAME.

Every date is stored in UTC (see app/db.py), and this module is the single place
that converts to and from the time the user actually reads and types.

It is not only for display: the mail fetch window is computed from the local
date too, because "from the 10th" means the 10th here, and in the small hours
that is still the 9th in UTC.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import config

log = logging.getLogger(__name__)

#: The value format of an <input type="datetime-local">.
INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def local_timezone(name: str):
    """
    The timezone used for display.

    Windows has no timezone database in the operating system, so ZoneInfo fails
    there unless the tzdata package is installed. In that case we fall back to
    the machine's own clock — better to show a correct time from the system than
    to bring the server down over a date display.
    """
    try:
        return ZoneInfo(name)
    except Exception:
        fallback = datetime.now().astimezone().tzinfo
        log.warning(
            'Timezone "%s" not found (common on Windows without the tzdata package) — '
            "showing the machine clock instead. To install: pip install tzdata",
            name,
        )
        return fallback


LOCAL_TZ = local_timezone(config.TZ_NAME)


def as_utc(value: datetime) -> datetime:
    """A date read back from the database has no timezone, and is UTC by definition."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def to_local(value: datetime) -> datetime:
    return as_utc(value).astimezone(LOCAL_TZ)


def format_dt(value: datetime | None) -> str:
    """For the screen: 10/09/2026 14:32, in local time."""
    if value is None:
        return ""
    return to_local(value).strftime("%d/%m/%Y %H:%M")


def to_input_value(value: datetime | None) -> str:
    """A stored UTC moment as the local value of a datetime-local field."""
    if value is None:
        return ""
    return to_local(value).strftime(INPUT_FORMAT)


def parse_input_value(raw: str) -> datetime | None:
    """
    The reverse: what was typed in the browser is local time, and is stored in
    UTC.

    Returns None for anything unparseable rather than guessing, so a mangled
    form field cannot quietly become a wrong date. A date with no time is
    accepted as local midnight, for a browser that renders the field as a plain
    text box.
    """
    text = (raw or "").strip().replace(" ", "T")
    if not text:
        return None
    for fmt in (INPUT_FORMAT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return None
