"""Unit tests for tsutil — SPEC §3 and §12.11.

tsutil is the foundation of the corpus: a bug here silently corrupts every row
ever written, so this is the most heavily tested module in the project. Every
test below corresponds to a defect that was actually found during research or to
an invariant another module relies on.

No network, no real chat text, no real coordinates (SPEC §11.4 / test
conventions in docs/INTERFACES.md).
"""

from __future__ import annotations

import ast
import calendar
import datetime
import os
import random
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tsutil  # noqa: E402
from tsutil import (  # noqa: E402
    TsParseError,
    fmt_bound,
    fmt_ts,
    now_us,
    parse_ts,
    to_dump_csv_ts,
    utc_date,
)

US = 1_000_000


def epoch_us(y, mo, d, h, mi, s, micros=0):
    """Independent expected-value helper: calendar.timegm, no tsutil involved."""
    return calendar.timegm((y, mo, d, h, mi, s, 0, 1, -1)) * US + micros


# A synthetic instant used throughout. 2026-07-25T20:37:13 UTC.
BASE_S = epoch_us(2026, 7, 25, 20, 37, 13)
BASE_STR = "2026-07-25T20:37:13"


class TestFractionalWidths(unittest.TestCase):
    """SPEC §3.1 / §12.11 — Jackson trims trailing zeros, so 0..9 digits occur."""

    def test_all_ten_widths(self):
        digits = "123456789"
        for width in range(0, 10):
            frac = digits[:width]
            s = BASE_STR + ("." + frac if frac else "") + "Z"
            expected_micros = int((frac + "000000000")[:9][:6])
            with self.subTest(width=width, length=len(s)):
                self.assertEqual(parse_ts(s), BASE_S + expected_micros)

    def test_nine_digit_sse_shape_is_truncated_not_rounded(self):
        # The observed SSE shape: 9 fractional digits, 30-char string.
        s = "2026-07-25T20:37:13.404045678Z"
        self.assertEqual(len(s), 30)
        self.assertEqual(parse_ts(s), epoch_us(2026, 7, 25, 20, 37, 13, 404045))

    def test_zero_digit_and_bare_forms_agree(self):
        for s in (BASE_STR + "Z", BASE_STR + ".0Z", BASE_STR + ".000000000Z",
                  BASE_STR, BASE_STR + "+00"):
            with self.subTest(length=len(s)):
                self.assertEqual(parse_ts(s), BASE_S)

    def test_trailing_zero_widths_are_equal_instants(self):
        # ".11" from one query and ".110000" from another are the same instant;
        # this is why the dedupe key is the integer, not the string (SPEC §4.2).
        a = parse_ts(BASE_STR + ".11Z")
        b = parse_ts(BASE_STR + ".110000Z")
        c = parse_ts(BASE_STR + ".110000000Z")
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        self.assertEqual(a, BASE_S + 110000)

    def test_no_fixed_offset_slicing(self):
        # Widths 1..9 must all parse; a fixed-offset slicer would break on most.
        for width in range(1, 10):
            s = BASE_STR + "." + ("9" * width) + "Z"
            with self.subTest(width=width):
                self.assertEqual(parse_ts(s), BASE_S + int(("9" * width + "000000000")[:6]))


class TestFloorNotRound(unittest.TestCase):
    """SPEC §3.1 — rounding up corrupts the inclusive backfill cursor."""

    def test_seventh_digit_never_rounds_up(self):
        cases = [
            (".1234569", 123456),
            (".1234565", 123456),
            (".9999999", 999999),
            (".0000009", 0),
            (".0000005", 0),
            (".1234564999", None),   # 10 digits -> malformed, checked separately
        ]
        for frac, expected in cases:
            if expected is None:
                continue
            s = BASE_STR + frac + "Z"
            with self.subTest(frac_len=len(frac) - 1):
                self.assertEqual(parse_ts(s), BASE_S + expected)

    def test_floor_at_every_position_past_six(self):
        # .999999X for X in 0..9 must all floor to .999999, never tick the second.
        for x in range(10):
            s = "%s.999999%dZ" % (BASE_STR, x)
            with self.subTest(x=x):
                self.assertEqual(parse_ts(s), BASE_S + 999999)
        # And the 8th/9th digits likewise.
        self.assertEqual(parse_ts(BASE_STR + ".999999999Z"), BASE_S + 999999)

    def test_floor_never_advances_the_second(self):
        # A rounding implementation would produce BASE_S + 1_000_000 here.
        self.assertLess(parse_ts(BASE_STR + ".9999995Z"), BASE_S + US)

    def test_boundary_cursor_survives_the_known_872508_case(self):
        # SPEC §3.3: a real row at .872508 vanishes if a bound is rounded to .873.
        us = parse_ts(BASE_STR + ".872508123Z")
        self.assertEqual(us, BASE_S + 872508)
        self.assertEqual(fmt_bound(us), BASE_STR + ".872508")


class TestZoneHandling(unittest.TestCase):
    """SPEC §3.1 — missing zone means UTC; dump uses '+00', REST/SSE use 'Z'."""

    def test_z_lower_z_and_absent_are_all_utc(self):
        for suffix in ("Z", "z", "", "+00", "+0000", "+00:00", "-00:00", "-0000", "-00"):
            with self.subTest(suffix=suffix or "<absent>"):
                self.assertEqual(parse_ts(BASE_STR + ".404045" + suffix),
                                 BASE_S + 404045)

    def test_negative_half_hour_offset(self):
        # -05:30 means the UTC instant is 5h30m LATER than the wall clock shown.
        expected = BASE_S + 404045 + (5 * 3600 + 30 * 60) * US
        for suffix in ("-05:30", "-0530"):
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_ts(BASE_STR + ".404045" + suffix), expected)

    def test_positive_offsets_all_three_shapes(self):
        expected = BASE_S - 5 * 3600 * US
        for suffix in ("+05", "+0500", "+05:00"):
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_ts(BASE_STR + suffix), expected)

    def test_positive_offset_with_minutes(self):
        expected = BASE_S - (5 * 3600 + 45 * 60) * US
        for suffix in ("+0545", "+05:45"):
            with self.subTest(suffix=suffix):
                self.assertEqual(parse_ts(BASE_STR + suffix), expected)

    def test_offset_shapes_are_equivalent_across_sources(self):
        # bulk dump form vs REST/SSE form for the same instant.
        dump = "2026-07-25 20:37:13.404045+00"
        sse = "2026-07-25T20:37:13.404045678Z"
        self.assertEqual(parse_ts(dump), parse_ts(sse))


class TestSeparator(unittest.TestCase):
    """SPEC §3.1 — the bulk dump uses a space where SSE/REST use 'T'."""

    def test_space_separator(self):
        self.assertEqual(parse_ts("2026-07-25 20:37:13.404045+00"),
                         BASE_S + 404045)

    def test_space_and_t_agree_at_every_width(self):
        for width in range(0, 10):
            frac = "404045678"[:width]
            tail = ("." + frac if frac else "")
            with self.subTest(width=width):
                self.assertEqual(parse_ts("2026-07-25T20:37:13" + tail + "Z"),
                                 parse_ts("2026-07-25 20:37:13" + tail + "+00"))


class TestRoundTrip(unittest.TestCase):
    """SPEC §3.1 + §3.2 — parse_ts(fmt_ts(us)) is the identity."""

    SPREAD = [
        0,                                        # the epoch itself
        1,
        999999,
        US,
        epoch_us(2013, 11, 18, 0, 0, 0),          # earliest chat in the DB
        epoch_us(2013, 11, 18, 12, 34, 56),       # whole-second legacy era
        epoch_us(2023, 6, 30, 23, 59, 59, 123000),  # ms era
        epoch_us(2026, 7, 25, 20, 37, 13, 404045),  # us era
        epoch_us(2024, 2, 29, 12, 0, 0, 1),       # leap day
        epoch_us(2000, 2, 29, 0, 0, 0),           # century leap day
        epoch_us(1999, 12, 31, 23, 59, 59, 999999),
        epoch_us(2100, 1, 1, 0, 0, 0),            # non-leap century
        epoch_us(9999, 12, 31, 23, 59, 59, 999999),  # far future
        -1,                                       # pre-epoch, floor semantics
        -500000,
        epoch_us(1969, 12, 31, 23, 59, 59, 500000),
    ]

    def test_round_trip_identity(self):
        for us in self.SPREAD:
            with self.subTest(us=us):
                self.assertEqual(parse_ts(fmt_ts(us)), us)

    def test_round_trip_identity_via_bound(self):
        # fmt_bound output is zone-less, which parse_ts must read as UTC.
        for us in self.SPREAD:
            with self.subTest(us=us):
                self.assertEqual(parse_ts(fmt_bound(us)), us)

    def test_round_trip_identity_random(self):
        rnd = random.Random(20260725)
        lo = epoch_us(2013, 1, 1, 0, 0, 0)
        hi = epoch_us(2035, 1, 1, 0, 0, 0)
        for _ in range(3000):
            us = rnd.randrange(lo, hi)
            self.assertEqual(parse_ts(fmt_ts(us)), us)

    def test_round_trip_every_microsecond_of_a_second(self):
        base = epoch_us(2026, 7, 25, 20, 37, 13)
        for micros in list(range(0, 1000)) + list(range(999000, 1000000)):
            us = base + micros
            self.assertEqual(parse_ts(fmt_ts(us)), us)

    def test_civil_rendering_matches_datetime_independently(self):
        # Cross-check the ordinal arithmetic against an aware-UTC datetime.
        epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        rnd = random.Random(4242)
        for _ in range(2000):
            us = rnd.randrange(epoch_us(1971, 1, 1, 0, 0, 0),
                               epoch_us(2200, 1, 1, 0, 0, 0))
            dt = epoch + datetime.timedelta(microseconds=us)
            expected = "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ" % (
                dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                dt.microsecond)
            self.assertEqual(fmt_ts(us), expected)


class TestFmtTs(unittest.TestCase):
    """SPEC §3.2 — fixed width is load-bearing."""

    def test_always_27_chars_ending_z(self):
        for us in TestRoundTrip.SPREAD:
            s = fmt_ts(us)
            with self.subTest(us=us):
                self.assertEqual(len(s), 27, s)
                self.assertTrue(s.endswith("Z"))
                self.assertEqual(s[10], "T")
                self.assertEqual(s[19], ".")
                self.assertEqual(len(s) - s.index(".") - 2, 6)  # 6 frac digits
                self.assertTrue(s[20:26].isdigit())

    def test_known_value(self):
        self.assertEqual(fmt_ts(epoch_us(2026, 7, 25, 20, 37, 13, 404045)),
                         "2026-07-25T20:37:13.404045Z")

    def test_epoch(self):
        self.assertEqual(fmt_ts(0), "1970-01-01T00:00:00.000000Z")

    def test_fraction_is_zero_padded_not_truncated(self):
        self.assertEqual(fmt_ts(1), "1970-01-01T00:00:00.000001Z")
        self.assertEqual(fmt_ts(100000), "1970-01-01T00:00:00.100000Z")

    def test_ascii_only(self):
        for us in TestRoundTrip.SPREAD:
            fmt_ts(us).encode("ascii")  # must not raise


class TestFmtBound(unittest.TestCase):
    """SPEC §3.3 — /chats/window binds LocalDateTime and discards any offset."""

    def test_no_zone_suffix_ever(self):
        for us in TestRoundTrip.SPREAD:
            s = fmt_bound(us)
            with self.subTest(us=us):
                self.assertNotIn("Z", s)
                self.assertNotIn("z", s)
                self.assertNotIn("+", s)
                self.assertEqual(len(s), 26, s)
                # The only '-' characters are the two date separators.
                self.assertEqual(s.count("-"), 2)

    def test_exactly_six_fractional_digits(self):
        s = fmt_bound(epoch_us(2026, 7, 25, 20, 37, 13, 872508))
        self.assertEqual(s, "2026-07-25T20:37:13.872508")
        self.assertEqual(len(s.split(".")[1]), 6)
        # Never 3 digits: '.873' would drop a real row at .872508.
        self.assertNotEqual(s, "2026-07-25T20:37:13.873")

    def test_bound_is_fmt_ts_without_the_z(self):
        for us in TestRoundTrip.SPREAD:
            with self.subTest(us=us):
                self.assertEqual(fmt_bound(us) + "Z", fmt_ts(us))

    def test_bound_round_trips_as_utc(self):
        us = epoch_us(2026, 7, 25, 20, 37, 13, 872508)
        self.assertEqual(parse_ts(fmt_bound(us)), us)


class TestLexicographicOrder(unittest.TestCase):
    """SPEC §3.2 — byte-lexicographic order MUST equal numeric order."""

    def _assert_order_preserved(self, values):
        vals = sorted(set(values))
        encoded = [fmt_ts(v).encode("ascii") for v in vals]
        self.assertEqual(encoded, sorted(encoded))
        # And strictly increasing, i.e. the mapping is injective.
        for a, b in zip(encoded, encoded[1:]):
            self.assertLess(a, b)

    def test_order_over_spread(self):
        self._assert_order_preserved(TestRoundTrip.SPREAD)

    def test_order_random(self):
        rnd = random.Random(1337)
        lo = epoch_us(2013, 1, 1, 0, 0, 0)
        hi = epoch_us(2099, 1, 1, 0, 0, 0)
        self._assert_order_preserved([rnd.randrange(lo, hi) for _ in range(5000)])

    def test_order_across_microsecond_and_second_boundaries(self):
        base = epoch_us(2026, 12, 31, 23, 59, 59)
        vals = [base + m for m in range(999990, 1000010)]
        self._assert_order_preserved(vals)

    def test_order_across_year_and_century_boundaries(self):
        vals = []
        for y in (1999, 2000, 2009, 2010, 2099, 2100):
            vals.append(epoch_us(y, 12, 31, 23, 59, 59, 999999))
            vals.append(epoch_us(y, 1, 1, 0, 0, 0))
        self._assert_order_preserved(vals)

    def test_order_matches_sort_of_parsed_strings(self):
        # The property another module depends on: sorting the emitted lines by
        # their ts bytes yields chronological order.
        rnd = random.Random(99)
        lo = epoch_us(2020, 1, 1, 0, 0, 0)
        us_list = [lo + rnd.randrange(0, 5 * 365 * 86400 * US) for _ in range(2000)]
        by_string = [parse_ts(s) for s in sorted(fmt_ts(u) for u in us_list)]
        self.assertEqual(by_string, sorted(us_list))

    def test_canonical_form_is_canonical_across_source_shapes(self):
        # Order-preservation is only useful because the same instant always
        # renders to the same bytes, whatever shape it arrived in.
        variants = [
            "2026-07-25T20:37:13.404045678Z",     # SSE, 9 digits
            "2026-07-25T20:37:13.404045z",        # lowercase zone
            "2026-07-25 20:37:13.404045+00",      # bulk dump
            "2026-07-25T20:37:13.404045",         # zone absent
            "2026-07-25T15:37:13.404045-05:00",   # same instant, other offset
            "2026-07-26T02:07:13.404045+0530",    # same instant, +HHMM
        ]
        rendered = {fmt_ts(parse_ts(v)) for v in variants}
        self.assertEqual(rendered, {"2026-07-25T20:37:13.404045Z"})

    def test_equal_instants_never_sort_apart(self):
        a = parse_ts("2026-07-25T20:37:13.11Z")
        b = parse_ts("2026-07-25T20:37:13.110000000Z")
        self.assertEqual(fmt_ts(a).encode("ascii"), fmt_ts(b).encode("ascii"))


class TestCalendarBoundaries(unittest.TestCase):
    """SPEC §3.1 — leap years and end-of-year rollovers."""

    def test_leap_day_2024(self):
        us = parse_ts("2024-02-29T00:00:00.000000Z")
        self.assertEqual(us, epoch_us(2024, 2, 29, 0, 0, 0))
        self.assertEqual(fmt_ts(us), "2024-02-29T00:00:00.000000Z")

    def test_leap_day_2000_is_valid_1900_is_not(self):
        self.assertEqual(parse_ts("2000-02-29T00:00:00Z"),
                         epoch_us(2000, 2, 29, 0, 0, 0))
        with self.assertRaises(TsParseError):
            parse_ts("1900-02-29T00:00:00Z")

    def test_non_leap_february_29_rejected(self):
        for year in (2023, 2025, 2100, 2200):
            with self.subTest(year=year):
                with self.assertRaises(TsParseError):
                    parse_ts("%04d-02-29T00:00:00Z" % year)

    def test_feb_28_and_mar_1_across_leap_boundary(self):
        prev = parse_ts("2024-02-28T23:59:59.999999Z")
        leap = parse_ts("2024-02-29T00:00:00.000000Z")
        nxt = parse_ts("2024-03-01T00:00:00.000000Z")
        self.assertEqual(leap - prev, 1)
        self.assertEqual(nxt - leap, 86400 * US)

    def test_end_of_year_rollover(self):
        last = parse_ts("2023-12-31T23:59:59.999999Z")
        first = parse_ts("2024-01-01T00:00:00.000000Z")
        self.assertEqual(first - last, 1)
        self.assertEqual(fmt_ts(last + 1), "2024-01-01T00:00:00.000000Z")

    def test_end_of_leap_year_has_366_days(self):
        jan1_2024 = parse_ts("2024-01-01T00:00:00Z")
        jan1_2025 = parse_ts("2025-01-01T00:00:00Z")
        self.assertEqual((jan1_2025 - jan1_2024) // (86400 * US), 366)
        jan1_2026 = parse_ts("2026-01-01T00:00:00Z")
        self.assertEqual((jan1_2026 - jan1_2025) // (86400 * US), 365)

    def test_month_lengths(self):
        expected = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        for month, days in enumerate(expected, start=1):
            last = "2023-%02d-%02dT00:00:00Z" % (month, days)
            self.assertIsInstance(parse_ts(last), int)
            with self.subTest(month=month):
                with self.assertRaises(TsParseError):
                    parse_ts("2023-%02d-%02dT00:00:00Z" % (month, days + 1))

    def test_offset_crossing_a_day_boundary(self):
        # 2026-01-01T01:00:00+05:00 is 2025-12-31T20:00:00Z.
        us = parse_ts("2026-01-01T01:00:00+05:00")
        self.assertEqual(us, epoch_us(2025, 12, 31, 20, 0, 0))
        self.assertEqual(utc_date(us), "2025-12-31")


class TestMalformedInput(unittest.TestCase):
    """SPEC §3.1 — malformed input raises, it never returns garbage."""

    BAD = [
        "",
        "not a timestamp",
        "hello",
        "2026-07-25",
        "2026-07-25T20:37",
        "2026-07-25T20:37:",
        "20260725T203713Z",
        "2026-7-25T20:37:13Z",
        "2026-07-5T20:37:13Z",
        "26-07-25T20:37:13Z",
        "2026-07-25T20:37:13.Z",
        "2026-07-25T20:37:13.",
        "2026-07-25T20:37:13.1234567890Z",   # 10 fractional digits
        "2026-07-25T20:37:13,404045Z",       # comma decimal separator
        "2026-07-25X20:37:13Z",
        "2026-07-25T20:37:13ZZ",
        "2026-07-25T20:37:13 Z",
        " 2026-07-25T20:37:13Z",
        "2026-07-25T20:37:13Z ",
        "\n2026-07-25T20:37:13Z",
        "2026-07-25T20:37:13Z\n",
        "2026-07-25T20:37:13+0",
        "2026-07-25T20:37:13+00:0",
        "2026-07-25T20:37:13+000",
        "2026-07-25T20:37:13+00:00:00",
        "2026-07-25T20:37:13+24:00",
        "2026-07-25T20:37:13-24:00",
        "2026-07-25T20:37:13+00:60",
        "2026-07-25T24:00:00Z",
        "2026-07-25T20:60:00Z",
        "2026-07-25T20:37:60Z",              # leap second: rejected, not clamped
        "2026-13-01T00:00:00Z",
        "2026-00-01T00:00:00Z",
        "2026-07-00T00:00:00Z",
        "2026-07-32T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "1785012433404045",                  # a raw ts_us, not a ts
        "2026-07-25T20:37:13.404045+00 extra",
    ]

    def test_all_bad_inputs_raise_tsparseerror(self):
        for index, s in enumerate(self.BAD):
            # Identify the case by index/length only, never by value (SPEC §11.4).
            with self.subTest(index=index, length=len(s)):
                with self.assertRaises(TsParseError):
                    parse_ts(s)

    def test_trailing_newline_is_a_parse_error_not_a_silent_accept(self):
        # Python's `$` also matches before a final newline; the parser uses `\Z`.
        good = "2026-07-25T20:37:13.404045Z"
        self.assertIsInstance(parse_ts(good), int)
        for bad in (good + "\n", good + "\r\n", good + "\r", good + "\t"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(TsParseError):
                    parse_ts(bad)

    def test_tsparseerror_is_a_valueerror(self):
        # Callers catch ValueError in places; the contract must hold.
        self.assertTrue(issubclass(TsParseError, ValueError))
        with self.assertRaises(ValueError):
            parse_ts("nope")

    def test_non_string_inputs_raise(self):
        for bad in (None, 0, 1785012433404045, 1.5, b"2026-07-25T20:37:13Z",
                    [], {}, object()):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(TsParseError):
                    parse_ts(bad)

    def test_error_message_never_contains_the_input(self):
        # SPEC §11.4 hard rule: no record body, no field value, in any message.
        secrets = ["hello", "gg", "2026-07-25T20:37:13BOGUS", "xyzzy-marker"]
        for s in secrets:
            with self.subTest(kind=len(s)):
                try:
                    parse_ts(s)
                except TsParseError as exc:
                    text = str(exc) + repr(exc.args)
                    self.assertNotIn(s, text)
                    for token in ("hello", "gg", "xyzzy", "BOGUS"):
                        self.assertNotIn(token, text)
                else:
                    self.fail("expected TsParseError")

    def test_error_message_carries_only_metadata(self):
        try:
            parse_ts("xyzzy-marker")
        except TsParseError as exc:
            msg = str(exc)
            self.assertIn("len=12", msg)
            self.assertNotIn("xyzzy", msg)
        else:
            self.fail("expected TsParseError")


class TestToDumpCsvTs(unittest.TestCase):
    """SPEC §3.4 — dump-compatible CSV timestamp rendering."""

    def test_six_significant_digits(self):
        self.assertEqual(to_dump_csv_ts(epoch_us(2026, 7, 25, 20, 37, 13, 404045)),
                         "2026-07-25 20:37:13.404045+00")

    def test_trailing_zeros_stripped(self):
        cases = [
            (500000, "2026-07-25 20:37:13.5+00"),
            (110000, "2026-07-25 20:37:13.11+00"),
            (120000, "2026-07-25 20:37:13.12+00"),
            (123000, "2026-07-25 20:37:13.123+00"),
            (123400, "2026-07-25 20:37:13.1234+00"),
            (123450, "2026-07-25 20:37:13.12345+00"),
            (123456, "2026-07-25 20:37:13.123456+00"),
            (1, "2026-07-25 20:37:13.000001+00"),
            (10, "2026-07-25 20:37:13.00001+00"),
            (100, "2026-07-25 20:37:13.0001+00"),
        ]
        for micros, expected in cases:
            with self.subTest(micros=micros):
                self.assertEqual(
                    to_dump_csv_ts(epoch_us(2026, 7, 25, 20, 37, 13, micros)),
                    expected)

    def test_zero_fraction_drops_the_decimal_point(self):
        self.assertEqual(to_dump_csv_ts(epoch_us(2026, 7, 25, 20, 37, 13, 0)),
                         "2026-07-25 20:37:13+00")
        self.assertEqual(to_dump_csv_ts(0), "1970-01-01 00:00:00+00")
        self.assertNotIn(".", to_dump_csv_ts(epoch_us(2013, 11, 18, 0, 0, 0)))

    def test_space_separator_and_plus_00_suffix(self):
        for us in TestRoundTrip.SPREAD:
            s = to_dump_csv_ts(us)
            with self.subTest(us=us):
                self.assertNotIn("T", s)
                self.assertEqual(s[10], " ")
                self.assertTrue(s.endswith("+00"))
                self.assertNotIn("Z", s)

    def test_lossless_round_trip(self):
        for us in TestRoundTrip.SPREAD:
            with self.subTest(us=us):
                self.assertEqual(parse_ts(to_dump_csv_ts(us)), us)

    def test_derivable_from_canonical_by_the_documented_edit(self):
        # SPEC §3.4 describes the conversion as a string edit of canonical ts;
        # the two implementations must agree for every microsecond value shape.
        for micros in (0, 1, 10, 100, 1000, 10000, 100000, 110000, 404045, 999999):
            us = epoch_us(2026, 7, 25, 20, 37, 13, micros)
            canonical = fmt_ts(us)
            body, frac = canonical[:-1].split(".")
            frac = frac.rstrip("0")
            expected = body.replace("T", " ") + (("." + frac) if frac else "") + "+00"
            with self.subTest(micros=micros):
                self.assertEqual(to_dump_csv_ts(us), expected)


class TestUtcDate(unittest.TestCase):
    """SPEC §5.1 — the file-partition key."""

    def test_known_dates(self):
        self.assertEqual(utc_date(0), "1970-01-01")
        self.assertEqual(utc_date(epoch_us(2026, 7, 25, 20, 37, 13, 404045)),
                         "2026-07-25")

    def test_day_boundaries_are_exact(self):
        day_start = epoch_us(2026, 7, 25, 0, 0, 0)
        self.assertEqual(utc_date(day_start), "2026-07-25")
        self.assertEqual(utc_date(day_start - 1), "2026-07-24")
        self.assertEqual(utc_date(day_start + 86400 * US - 1), "2026-07-25")
        self.assertEqual(utc_date(day_start + 86400 * US), "2026-07-26")

    def test_is_prefix_of_fmt_ts(self):
        rnd = random.Random(7)
        lo = epoch_us(2013, 1, 1, 0, 0, 0)
        hi = epoch_us(2040, 1, 1, 0, 0, 0)
        for _ in range(500):
            us = rnd.randrange(lo, hi)
            self.assertTrue(fmt_ts(us).startswith(utc_date(us)))
            self.assertEqual(len(utc_date(us)), 10)


class TestNowUs(unittest.TestCase):
    """SPEC §3 — integer microseconds, UTC, no float path."""

    def test_type_and_plausible_range(self):
        us = now_us()
        self.assertIsInstance(us, int)
        self.assertNotIsInstance(us, bool)
        # Between 2020-01-01 and 2100-01-01: catches ms/ns unit mistakes.
        self.assertGreater(us, epoch_us(2020, 1, 1, 0, 0, 0))
        self.assertLess(us, epoch_us(2100, 1, 1, 0, 0, 0))

    def test_agrees_with_time_time_within_a_second(self):
        before = time.time()
        us = now_us()
        after = time.time()
        self.assertGreaterEqual(us, int((before - 1.0) * US))
        self.assertLessEqual(us, int((after + 1.0) * US))

    def test_non_decreasing(self):
        samples = [now_us() for _ in range(50)]
        self.assertEqual(samples, sorted(samples))

    def test_renders_to_canonical_shape(self):
        s = fmt_ts(now_us())
        self.assertEqual(len(s), 27)
        self.assertEqual(parse_ts(s), parse_ts(s))


class TestNoLocalTimezoneContamination(unittest.TestCase):
    """SPEC §3.1 — never time.mktime, never naive datetime.timestamp()."""

    def test_parse_is_tz_independent(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is POSIX-only (Windows dev box)")
        samples = ["2026-07-25T20:37:13.404045Z",
                   "2026-07-25 20:37:13.404045+00",
                   "2026-07-25T20:37:13.404045",
                   "2013-11-18T12:34:56Z",
                   "2024-02-29T00:00:00"]
        old_tz = os.environ.get("TZ")
        try:
            baseline = None
            for tz in ("UTC", "America/New_York", "Asia/Kolkata",
                       "Pacific/Kiritimati", "Europe/Berlin"):
                os.environ["TZ"] = tz
                time.tzset()
                got = [parse_ts(s) for s in samples]
                rendered = [fmt_ts(u) for u in got]
                if baseline is None:
                    baseline = (got, rendered)
                else:
                    self.assertEqual((got, rendered), baseline, tz)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_naive_datetime_timestamp_disagrees_under_a_shifted_tz(self):
        # Sanity check that the test above would actually catch the bug: a naive
        # datetime.timestamp() is local-dependent while parse_ts is not.
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is POSIX-only (Windows dev box)")
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            naive = datetime.datetime(2026, 7, 25, 20, 37, 13)
            self.assertNotEqual(int(naive.timestamp()) * US,
                                parse_ts("2026-07-25T20:37:13Z"))
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


class TestTypesAndPurity(unittest.TestCase):
    """Platform / contract invariants from docs/INTERFACES.md."""

    def test_parse_returns_plain_int(self):
        us = parse_ts("2026-07-25T20:37:13.404045Z")
        self.assertIsInstance(us, int)
        self.assertNotIsInstance(us, bool)
        self.assertNotIsInstance(us, float)

    def test_fmt_functions_return_str(self):
        for fn in (fmt_ts, fmt_bound, utc_date, to_dump_csv_ts):
            with self.subTest(fn=fn.__name__):
                self.assertIsInstance(fn(BASE_S), str)

    def test_fmt_rejects_non_int(self):
        for bad in (1.5, "0", None, True):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(TypeError):
                    fmt_ts(bad)

    def test_public_surface_matches_the_interface_contract(self):
        for name in ("TsParseError", "parse_ts", "fmt_ts", "fmt_bound",
                     "now_us", "utc_date", "to_dump_csv_ts"):
            self.assertTrue(hasattr(tsutil, name), name)

    def test_banned_time_calls_are_absent_from_the_source(self):
        # SPEC §3.1 bans these outright: each one would pass a naive unit test on
        # a UTC dev box and silently corrupt the corpus on a box with a local
        # timezone (or, for fromisoformat, on an unexpected fractional width).
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tsutil.py")
        with open(src_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
        for banned in ("fromisoformat", "mktime", "timestamp", "localtime",
                       "utcnow", "now", "strptime"):
            self.assertNotIn(banned, called)
        # And the sanctioned epoch conversion IS used.
        self.assertIn("timegm", called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
