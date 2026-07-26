"""Timestamp parsing and rendering for the 2b2t chat corpus collector.

Implements SPEC §3 in full. This module is the foundation of the corpus: every
`ts` / `ts_us` pair on disk, every dedupe key, and every backfill cursor bound is
produced here, so the rules below are deliberately literal about the two traps
that were found during research (SPEC §3.1, §3.3, §12.11):

* The SSE feed emits **nanosecond** precision, but Jackson trims trailing zeros,
  so 0 through 9 fractional digits all occur on the same endpoint. Nothing here
  ever slices at a fixed offset and `datetime.fromisoformat` is never used.
* Fractions are right-padded to 9 digits and then **floor-truncated** to 6.
  Rounding up would push a value into the next microsecond and corrupt the
  inclusive backfill cursor.
* `fmt_bound()` emits **no zone suffix at all**: `/chats/window` binds a Java
  `LocalDateTime` and silently discards any offset, so a `Z` would query the
  wrong instant with no error.

All internal time is `int` microseconds since the Unix epoch (`*_us`). Never a
float, never a naive `datetime`, never `time.mktime`, never
`datetime.timestamp()` — the epoch conversion goes through `calendar.timegm`
and the inverse goes through `datetime.date.toordinal` arithmetic, both of which
are pure calendar math with no local-timezone surface.

Privacy (SPEC §11.4): `TsParseError` messages are built from metadata only
(a reason code and the input length). The offending string is never interpolated
into an exception, because a `time` field could in principle carry arbitrary
text and 2b2t chat routinely contains coordinates.
"""

from __future__ import annotations

import calendar
import datetime
import re
import time
from typing import Optional, Tuple

__all__ = [
    "TsParseError",
    "parse_ts",
    "fmt_ts",
    "fmt_bound",
    "now_us",
    "utc_date",
    "to_dump_csv_ts",
]

US_PER_S = 1_000_000

# SPEC §3.1. Two deliberate deviations from the spec's literal pattern:
#   * the zone may also be a bare '+HH' / '-HH', because the bulk dump writes
#     '+00'; offsets are therefore '+HH', '+HHMM' or '+HH:MM';
#   * the terminator is `\Z`, not `$`. In Python `$` also matches immediately
#     before a trailing newline, so `$` would silently accept
#     "…T20:37:13Z\n" — a trailing-whitespace bug that must be a parse error,
#     not a quietly-accepted value.
# A missing zone means UTC.
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.(\d{1,9}))?"
    r"(Z|z|[+-]\d{2}(?::?\d{2})?)?\Z"
)

# datetime.date(1970, 1, 1).toordinal(); the exact epoch anchor calendar.timegm
# uses internally, which makes _civil_from_us() a provable inverse of timegm.
_EPOCH_ORD = datetime.date(1970, 1, 1).toordinal()


class TsParseError(ValueError):
    """Raised when a source timestamp cannot be parsed. SPEC §3.1.

    Callers drop the record and increment ``parse_errors``. The message contains
    metadata only — never the offending value (SPEC §11.4).
    """


def _fail(reason: str, length: int) -> "TsParseError":
    """Build a content-free TsParseError. SPEC §11.4."""
    return TsParseError("timestamp parse failed: %s (len=%d)" % (reason, length))


def _days_in_month(year: int, month: int) -> int:
    """Length of a Gregorian month, proleptic. Helper for SPEC §3.1 validation."""
    if month == 2:
        leap = (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    return 30


def parse_ts(s: str) -> int:
    """Parse any source timestamp into microseconds since the epoch. SPEC §3.1.

    Accepts the union of every shape the four sources produce: `T` or space as
    the date/time separator, 0 through 9 fractional digits, and a zone of `Z`,
    `z`, `+HH`, `+HHMM`, `+HH:MM` (either sign) or nothing at all. A missing
    zone means UTC.

    The fraction is right-padded to 9 digits and then FLOOR-truncated to 6.
    Rounding is never performed: rounding up would move the value into the next
    microsecond and corrupt the inclusive `/chats/window` cursor (SPEC §7.3).

    Raises TsParseError on any malformed input, including calendar-invalid dates
    such as 2023-02-29, rather than returning a plausible-looking wrong value.
    Never uses `datetime.fromisoformat`; the epoch conversion is
    `calendar.timegm`, never `time.mktime` and never a naive
    `datetime.timestamp()`.
    """
    if not isinstance(s, str):
        # Never echo the value; the type name is metadata. SPEC §11.4.
        raise TsParseError(
            "timestamp parse failed: not a str (type=%s)" % type(s).__name__)

    m = _TS_RE.match(s)
    if m is None:
        raise _fail("no match", len(s))

    year = int(m.group(1))
    month = int(m.group(2))
    day = int(m.group(3))
    hour = int(m.group(4))
    minute = int(m.group(5))
    second = int(m.group(6))

    if year < 1:
        # `calendar.timegm` raises a bare ValueError("year 0 is out of range") for
        # year 0000, and `datetime.date.fromordinal` cannot render it either. The
        # documented contract of this function is TsParseError, and callers catch
        # exactly that (record.py, writer._ingest_date, three tools), so a bare
        # ValueError escaping here would kill a producer thread.
        raise _fail("year out of range", len(s))
    if not 1 <= month <= 12:
        raise _fail("month out of range", len(s))
    if not 1 <= day <= _days_in_month(year, month):
        raise _fail("day out of range for month", len(s))
    if hour > 23:
        raise _fail("hour out of range", len(s))
    if minute > 59:
        raise _fail("minute out of range", len(s))
    if second > 59:
        raise _fail("second out of range", len(s))

    # Fraction: right-pad to 9, then FLOOR-truncate to 6. SPEC §3.1.
    padded9 = ((m.group(7) or "") + "000000000")[:9]
    micros = int(padded9[:6])

    offset_s = _offset_seconds(m.group(8), len(s))

    # calendar.timegm is pure UTC calendar arithmetic on the parsed parts. It is
    # still wrapped: any value it dislikes must surface as TsParseError, never as a
    # bare ValueError (see the year check above).
    try:
        epoch_s = calendar.timegm((year, month, day, hour, minute, second, 0, 1, -1))
    except (ValueError, OverflowError):
        raise _fail("not a representable instant", len(s)) from None
    return (epoch_s - offset_s) * US_PER_S + micros


def _offset_seconds(zone: Optional[str], length: int) -> int:
    """Zone suffix -> seconds east of UTC. SPEC §3.1. None/'Z'/'z' -> 0."""
    if zone is None or zone in ("Z", "z"):
        return 0
    sign = -1 if zone[0] == "-" else 1
    body = zone[1:].replace(":", "")
    hours = int(body[0:2])
    minutes = int(body[2:4]) if len(body) >= 4 else 0
    if hours > 23:
        raise _fail("zone hour out of range", length)
    if minutes > 59:
        raise _fail("zone minute out of range", length)
    return sign * (hours * 3600 + minutes * 60)


def _civil_from_us(us: int) -> Tuple[int, int, int, int, int, int, int]:
    """us -> (year, month, day, hour, minute, second, microsecond). SPEC §3.2.

    Exact inverse of the `calendar.timegm` path in `parse_ts`. Uses floor
    division throughout, so pre-epoch (negative) values render correctly, and
    ordinal arithmetic rather than `time.gmtime` — the platform `gmtime` rejects
    values past year 3000 on Windows, and the collector must behave identically
    on the Windows dev box and the Linux deploy box.
    """
    if not isinstance(us, int) or isinstance(us, bool):
        raise TypeError("us must be an int (microseconds since epoch), got %s"
                        % type(us).__name__)
    secs, micros = divmod(us, US_PER_S)          # floor: micros is always >= 0
    days, rem = divmod(secs, 86400)              # floor: rem is always >= 0
    hour, rem = divmod(rem, 3600)
    minute, second = divmod(rem, 60)
    d = datetime.date.fromordinal(_EPOCH_ORD + days)
    return d.year, d.month, d.day, hour, minute, second, micros


def _fmt_core(us: int) -> str:
    """'YYYY-MM-DDTHH:MM:SS.ffffff' — fixed width, no zone. SPEC §3.2."""
    year, month, day, hour, minute, second, micros = _civil_from_us(us)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%06d" % (
        year, month, day, hour, minute, second, micros)


def fmt_ts(us: int) -> str:
    """us -> 'YYYY-MM-DDTHH:MM:SS.ffffffZ'. SPEC §3.2.

    Always exactly 27 characters: 6 fractional digits, always `Z`. The fixed
    width is load-bearing — byte-lexicographic order of this output equals
    chronological order of the underlying `ts_us`, which the dump's
    variable-width `+00` form does not guarantee.
    """
    return _fmt_core(us) + "Z"


def fmt_bound(us: int) -> str:
    """us -> 'YYYY-MM-DDTHH:MM:SS.ffffff' with NO zone suffix. SPEC §3.3.

    `/chats/window` binds its `startDate` / `endDate` to a Java
    `LocalDateTime`, which parses and then DISCARDS any offset — a trailing `Z`
    produces a wrong-instant query with no error at all. All 6 fractional
    digits are always emitted: a real row at `.872508` provably vanishes when
    the bound is truncated to `.873`.
    """
    return _fmt_core(us)


def now_us() -> int:
    """Collector wall clock in microseconds since the epoch, UTC. SPEC §3.

    `time.time_ns()` floored to microseconds — an integer path with no float
    rounding, unlike `int(time.time() * 1e6)`.
    """
    return time.time_ns() // 1000


def utc_date(us: int) -> str:
    """us -> 'YYYY-MM-DD' (UTC). SPEC §5.1 partition key / §3.2 rendering."""
    year, month, day = _civil_from_us(us)[:3]
    return "%04d-%02d-%02d" % (year, month, day)


def to_dump_csv_ts(us: int) -> str:
    """us -> the bulk-dump `chats.csv` timestamp form. SPEC §3.4.

    Byte-lossless conversion of the canonical `ts`: `T` becomes a space, `Z`
    becomes `+00`, and trailing zeros are stripped from the fraction — if the
    fraction becomes empty the decimal point is dropped too. For
    `tools/export_csv.py` only; never used on the collector's hot path.
    """
    year, month, day, hour, minute, second, micros = _civil_from_us(us)
    frac = ("%06d" % micros).rstrip("0")
    tail = "." + frac if frac else ""
    return "%04d-%02d-%02d %02d:%02d:%02d%s+00" % (
        year, month, day, hour, minute, second, tail)
