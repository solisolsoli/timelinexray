"""Strict parsers for export cells and command options: counts, ids, times, durations.

Nothing here guesses. Numbers are read with the separators of a declared locale, post ids
stay strings, and a timestamp without an offset is placed in a declared time zone with
daylight-saving gaps and folds reported instead of resolved silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from ..errors import InvalidInput


@dataclass(frozen=True)
class NumberLocale:
    name: str
    group: str
    decimal: str


NUMBER_LOCALES = {
    "en": NumberLocale("en", group=",", decimal="."),
    "tr": NumberLocale("tr", group=".", decimal=","),
}

_DIGITS = re.compile(r"[0-9]+")


class CellError(ValueError):
    """A cell value that cannot be read under the declared rules."""


def parse_count(raw: str, locale: str) -> int:
    """A non-negative whole count, written with the declared locale's group separator.

    ``"1234"`` is read the same under every locale. ``"1.234"`` is 1234 under ``tr`` and is
    rejected under ``en`` (where it would be a fraction). Blank cells are handled by the
    caller; negative, fractional, abbreviated (``"1,5K"``) and signed values are rejected.
    """
    loc = NUMBER_LOCALES[locale]
    text = raw.strip()
    if _DIGITS.fullmatch(text):
        return int(text)
    group = re.escape(loc.group)
    if re.fullmatch(rf"[0-9]{{1,3}}(?:{group}[0-9]{{3}})+", text):
        return int(text.replace(loc.group, ""))
    raise CellError(
        f"{raw!r} is not a whole non-negative count in the {locale!r} number format "
        f"(group separator {loc.group!r}, decimal separator {loc.decimal!r})"
    )


_POST_ID = re.compile(r"[0-9]{1,20}")
_SCIENTIFIC = re.compile(r"[0-9](?:[.,][0-9]+)?[eE][+-]?[0-9]+")


def parse_post_id(raw: str) -> str:
    """The post id as its exact digit string; never converted to a number."""
    text = raw.strip()
    if _POST_ID.fullmatch(text):
        return text
    if _SCIENTIFIC.fullmatch(text):
        raise CellError(
            f"post id {raw!r} is in scientific notation: a spreadsheet has rounded it and the "
            "original id cannot be recovered; export the CSV again without opening it in a "
            "spreadsheet"
        )
    raise CellError(f"post id {raw!r} is not a plain digit string")


#: Names that need no time zone database: UTC has no daylight-saving rules.
_UTC_NAMES = frozenset({"UTC", "Etc/UTC"})


def load_zone(name: str) -> tzinfo:
    """An IANA time zone, or InvalidInput.

    ``UTC`` and ``Etc/UTC`` are the fixed :data:`datetime.timezone.utc`, so they work on a
    machine with no time zone database (a slim container without the system ``tzdata`` and
    without the ``tzdata`` package). Any other name needs the database, and says so when it
    is missing instead of calling the name unknown."""
    if name in _UTC_NAMES:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        if not available_timezones():
            raise InvalidInput(
                f"cannot load time zone {name!r}: no IANA time zone database found "
                "(install the tzdata package: pip install tzdata, or your system's tzdata)"
            ) from exc
        raise InvalidInput(f"unknown IANA time zone {name!r}") from exc


# created_at statuses
PARSED = "parsed"
BLANK = "blank"
MISSING_COLUMN = "missing-column"
UNPARSED = "unparsed"
NONEXISTENT = "nonexistent-local-time"
AMBIGUOUS = "ambiguous-local-time"

_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_DIRECTIVES = ("%H", "%I", "%M", "%S", "%X", "%T", "%R", "%c")


@dataclass(frozen=True)
class ParsedTime:
    """A publication time: the raw text always, and UTC only when it is unambiguous."""

    raw: str | None
    status: str
    precision: str | None = None  # "datetime" or "date"
    utc: datetime | None = None
    local_date: date | None = None  # for date-only values, the calendar date as exported
    offset_source: str | None = None  # "explicit" or "declared-zone"
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "status": self.status,
            "precision": self.precision,
            "utc": format_utc(self.utc) if self.utc else None,
            "local_date": self.local_date.isoformat() if self.local_date else None,
            "offset_source": self.offset_source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParsedTime":
        return cls(
            raw=data.get("raw"),
            status=data["status"],
            precision=data.get("precision"),
            utc=parse_utc(data["utc"]) if data.get("utc") else None,
            local_date=date.fromisoformat(data["local_date"]) if data.get("local_date") else None,
            offset_source=data.get("offset_source"),
            note=data.get("note"),
        )


def parse_created_at(raw: str, zone: tzinfo, date_format: str | None = None) -> ParsedTime:
    """Parse a publication time. ISO 8601 by default, or the declared strptime format."""
    text = raw.strip()
    if not text:
        return ParsedTime(raw, BLANK)
    try:
        if date_format:
            value = datetime.strptime(text, date_format)
            precision = "datetime" if any(d in date_format for d in _TIME_DIRECTIVES) else "date"
        elif _DATE_ONLY.fullmatch(text):
            value, precision = datetime.fromisoformat(text), "date"
        else:
            value, precision = datetime.fromisoformat(text), "datetime"
    except ValueError as exc:
        how = f"format {date_format!r}" if date_format else "ISO 8601"
        return ParsedTime(raw, UNPARSED, note=f"not readable as {how}: {exc}")
    if precision == "date":
        return ParsedTime(raw, PARSED, "date", local_date=value.date(),
                          note="date only: the publication instant is unknown within the day")
    if value.tzinfo is not None:
        return ParsedTime(raw, PARSED, "datetime", value.astimezone(timezone.utc),
                          offset_source="explicit")
    return localize(raw, value, zone)


def localize(raw: str, naive: datetime, zone: tzinfo) -> ParsedTime:
    """Place a naive local time in ``zone``; report DST gaps and folds explicitly."""
    first = naive.replace(tzinfo=zone, fold=0)
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        return ParsedTime(raw, NONEXISTENT, "datetime", offset_source="declared-zone",
                          note=f"{naive.isoformat()} does not exist in {zone} "
                               "(daylight-saving gap)")
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return ParsedTime(raw, AMBIGUOUS, "datetime", offset_source="declared-zone",
                          note=f"{naive.isoformat()} occurs twice in {zone} "
                               "(daylight-saving fold); not resolved silently")
    return ParsedTime(raw, PARSED, "datetime", first.astimezone(timezone.utc),
                      offset_source="declared-zone")


def parse_aware(text: str, what: str) -> datetime:
    """An ISO 8601 instant that carries an offset or ``Z``; returned in UTC."""
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise InvalidInput(f"{what}: {text!r} is not an ISO 8601 date-time") from exc
    if value.tzinfo is None:
        raise InvalidInput(
            f"{what}: {text!r} has no UTC offset; write it like 2026-09-30T18:00:00+09:00 "
            "or 2026-09-30T09:00:00Z"
        )
    return value.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_DURATION = re.compile(r"([0-9]+)\s*([mhd])")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str, what: str, *, allow_zero: bool = False) -> timedelta:
    """``90m``, ``24h`` or ``7d``."""
    match = _DURATION.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise InvalidInput(f"{what}: {text!r} is not a duration like 90m, 24h or 7d")
    seconds = int(match.group(1)) * _UNITS[match.group(2)]
    if seconds == 0 and not allow_zero:
        raise InvalidInput(f"{what}: must be greater than zero")
    return timedelta(seconds=seconds)


def hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600
