"""Coordinate-redaction tests.

Every coordinate in this file is synthetic and inside 20k of the origin. Real 2b2t
coordinates must never appear in a repository, a test fixture or a transcript.

The bias under test is deliberate: over-redaction is acceptable, under-redaction is not. So
the false-positive cases below assert what is *preserved* for readability, while the
coordinate cases assert redaction happens -- and where the two conflict, redaction wins.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import redact


class RedactCase(unittest.TestCase):
    def assertRedacted(self, text, msg=None):
        out, n = redact.redact(text)
        self.assertGreater(n, 0, msg or "should have been redacted: %r" % (text,))
        self.assertIn(redact.PLACEHOLDER, out)
        return out

    def assertKept(self, text):
        out, n = redact.redact(text)
        self.assertEqual(n, 0, "should NOT have been redacted: %r -> %r" % (text, out))
        self.assertEqual(out, text)

    # ------------------------------------------------------------ must redact

    def test_bare_triple(self):
        self.assertRedacted("base at 1234 64 -5678")

    def test_triple_with_small_y(self):
        """A y-level is 0..320 and often single digit, so requiring all three components to
        be large would miss the single most common coordinate shape there is."""
        self.assertRedacted("1234 7 -5678")

    def test_comma_separated_triple(self):
        self.assertRedacted("go to 1234, 64, -5678 now")

    def test_slash_separated(self):
        self.assertRedacted("1234/64/-5678")

    def test_bare_pair(self):
        self.assertRedacted("1234, -5678")

    def test_labelled_axes(self):
        out = self.assertRedacted("x: 1234 y: 64 z: -5678")
        self.assertNotIn("1234", out)
        self.assertNotIn("5678", out)

    def test_labelled_axes_without_colons(self):
        self.assertRedacted("x1234 z-5678")

    def test_k_suffix_counts_as_large_on_its_own(self):
        """'2k' is 2000 whichever way you read it, and nobody writes a stack size that way."""
        self.assertRedacted("coords 12k 3k")

    def test_decimal_k_suffix(self):
        self.assertRedacted("1.2k -3.4k")

    def test_three_digit_values_are_large_enough(self):
        self.assertRedacted("meet at 500 -700")

    def test_count_reports_every_substitution(self):
        _, n = redact.redact("1234 64 -5678 and also 4321, -8765")
        self.assertEqual(n, 2)

    # ------------------------------------------------------- must NOT redact

    def test_small_numbers_are_not_coordinates(self):
        self.assertKept("im 1 2 3")

    def test_version_strings_survive(self):
        """'.' is deliberately not a separator, so 1.21.4 parses as 1.21 and 4 -- neither
        of which is large."""
        self.assertKept("running 1.21.4 client")

    def test_clock_times_survive(self):
        self.assertKept("at 15:30 today")

    def test_a_lone_large_number_is_not_a_location(self):
        self.assertKept("i have 2000 diamonds")

    def test_numbers_separated_by_words_survive(self):
        self.assertKept("scored 1500 to 900")

    def test_a_single_labelled_axis_is_ambiguous_and_kept(self):
        self.assertKept("x: 1234 alone")

    def test_plain_chat_untouched(self):
        self.assertKept("gg wp that was close")

    def test_empty_input(self):
        self.assertEqual(redact.redact(""), ("", 0))

    # ------------------------------------------------------------- batch + aux

    def test_redact_lines_totals_across_the_batch(self):
        lines = ["1234 64 -5678", "gg", "x1234 z-5678"]
        out, total = redact.redact_lines(lines)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1], "gg")
        self.assertEqual(total, 3)          # 1 triple + 2 labelled axes

    def test_find_coords_reports_the_spans(self):
        found = redact.find_coords("here 1234 64 -5678 and 4321, -8765")
        self.assertEqual(len(found), 2)

    def test_shape_carries_structure_but_no_content(self):
        s = redact.shape("base at 1234 64 -5678")
        self.assertNotRegex(s, r"[1-9]")
        self.assertNotRegex(s, r"[b-z]")
        self.assertEqual(s, "aaaa aa 0000 00 -0000")

    def test_shape_truncates(self):
        self.assertTrue(redact.shape("a" * 200).endswith("..."))
        self.assertLessEqual(len(redact.shape("a" * 200)), 83)


if __name__ == "__main__":
    unittest.main()
