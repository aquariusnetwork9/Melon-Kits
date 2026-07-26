"""Screening tests.

The terms used here are innocuous stand-ins ('badword', 'kitten', 'grape') because the
mechanism is what is under test, not any particular lexicon -- and a repository is a bad
place for a slur list. The evasion spellings are the real subject: if normalisation does not
collapse them onto one form, a lexicon is worth almost nothing.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import screening


def lex(**cats):
    return screening.Lexicon({k: dict(v) for k, v in cats.items()})


class NormaliseCase(unittest.TestCase):
    def test_case_and_separators_fold_away(self):
        self.assertEqual(screening.normalise("B A D-W_O.R.D")[0], "badword")

    def test_digit_and_symbol_substitutions_fold_to_letters(self):
        for spelling in ("b4dw0rd", "b@dw0rd", "badw0rd", "b4dword", "8adw0rd"):
            self.assertEqual(screening.normalise(spelling)[0], "badword",
                             "%r did not fold" % (spelling,))

    def test_repeated_characters_collapse(self):
        self.assertEqual(screening.normalise("baaaadwooord")[0], "badword")

    def test_accents_and_combining_marks_fold(self):
        self.assertEqual(screening.normalise("bádwörd")[0], "badword")

    def test_index_map_recovers_the_original_spelling(self):
        """The mining path: report the spelling that actually appeared, not the term that
        matched. Guessing evasion spellings does not work; harvesting them does."""
        text = "you are a B-4-A-D W0RD ok"
        norm, index = screening.normalise(text)
        self.assertEqual(len(norm), len(index))
        start = norm.find("badword")
        self.assertGreaterEqual(start, 0)
        original = text[index[start]:index[start + len("badword") - 1] + 1]
        self.assertEqual(original, "B-4-A-D W0RD")

    def test_empty_and_symbol_only_input(self):
        self.assertEqual(screening.normalise("")[0], "")
        self.assertEqual(screening.normalise("??? ...")[0], "")

    def test_substitution_symbols_can_synthesise_letters_and_that_is_intended(self):
        """'!!!' normalises to 'i', which looks wrong and is not.

        '!' folds to 'i' because that is how people actually write around a filter, and
        dropping the substitution to make punctuation inert would cost far more matches than
        it saves. The consequence is that punctuation alone can produce a letter, which is
        harmless: it only matters for pathological single-letter terms, and a one-letter
        entry in a lexicon is already meaningless.
        """
        self.assertEqual(screening.normalise("!!!")[0], "i")
        self.assertEqual(screening.normalise("@$$")[0], "as")   # collapsed 'ass'

    def test_terms_and_text_normalise_the_same_way_so_doubled_letters_still_match(self):
        """Collapsing runs shortens the lexicon too -- 'kill' is stored as 'kil' -- so both
        sides agree. This is why the Lexicon normalises its own terms on load rather than
        matching raw strings."""
        l = lex(a={"terms": ["kill"]})
        self.assertEqual(l.categories["a"]["terms"], ["kil"])
        for spelling in ("kill", "kiiiill", "k1ll", "KILL", "kíll"):
            self.assertEqual(len(screening.scan(spelling, l)), 1,
                             "%r did not match" % (spelling,))
        # ...but NOT spelled out with separators. `kill` folds to `kil`, three characters,
        # and separator-spanning is only accepted at six or more -- below that the letters
        # fall adjacent across ordinary word boundaries far too often to be a signal. This
        # is a real and deliberate loss of coverage, bought for not flagging a quarter of
        # all chat. Long terms keep it; see the evasion test below.
        self.assertEqual(screening.scan("k i l l", l), [])


class ScanCase(unittest.TestCase):
    def test_finds_an_evasion_spelling_of_a_plain_term(self):
        hits = screening.scan("what a b4dw0rd thing", lex(x={"terms": ["badword"]}))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].category, "x")
        self.assertEqual(hits[0].spelling, "b4dw0rd")

    def test_no_lexicon_means_no_hits(self):
        self.assertEqual(screening.scan("b4dw0rd", screening.Lexicon({})), [])

    def test_multi_word_term_matches_across_a_space(self):
        hits = screening.scan("i will s w a t you", lex(t={"terms": ["swat you"]}))
        self.assertEqual(len(hits), 1)

    def test_a_short_term_cannot_fire_inside_a_longer_word(self):
        """The Scunthorpe family, now structurally impossible rather than filtered.

        A term under 6 characters only ever matches a whole token, so it cannot appear
        inside an innocent word at all -- no exception entry required.
        """
        l = lex(a={"terms": ["grape"]})
        self.assertEqual(screening.scan("i ate a grapefruit", l), [])
        self.assertEqual(len(screening.scan("i ate a grape", l)), 1)

    def test_the_collapse_collisions_that_flagged_a_quarter_of_all_chat(self):
        """Measured against the 2025 bulk dump, plain substring matching flagged 25.9% of
        every line. Normalisation collapses doubles, so `ass` -> `as`, `hell` -> `hel`,
        `coon` -> `con`, and a substring search fired those inside ordinary words. `as`
        alone hit 1.18 million lines."""
        for term, innocent in (("ass", "that was a big ask"),
                               ("hell", "hello there, need help?"),
                               ("coon", "i lost control of my connection"),
                               ("piss", "nice pistol"),
                               ("abo", "what about it")):
            l = lex(a={"terms": [term]})
            self.assertEqual(screening.scan(innocent, l), [],
                             "%r wrongly fired on %r" % (term, innocent))

    def test_numbers_do_not_fold_into_words(self):
        """The biggest false-positive source found in the 2025 bulk dump.

        The fold maps 6->g, 0->o, 4->a, 8->b, so on a Minecraft server -- where people talk
        in distances and coordinates all day -- `60k` normalises to `gok` and `480` to `abo`.
        Those produced tens of thousands of false hits against 18 genuine ones. A match whose
        original text is more digits than letters is a number, because leet substitution
        writes *letters as digits* and still leaves a minority of digits.
        """
        for term, number in (("gook", "60k"), ("gook", "600k"), ("gook", "90k")):
            l = lex(a={"terms": [term]})
            self.assertEqual(screening.scan("base is %s out" % number, l), [],
                             "%r wrongly fired on %r" % (term, number))
        # ...while genuine leet spelling, which is mostly letters, still matches.
        l = lex(a={"terms": ["gook"]})
        self.assertEqual(len(screening.scan("g00k", l)), 1)
        l2 = lex(a={"terms": ["badword"]})
        self.assertEqual(len(screening.scan("b4dw0rd", l2)), 1)

    def test_a_long_term_still_matches_inside_a_compound(self):
        l = lex(a={"terms": ["badword"]})
        self.assertEqual(len(screening.scan("badwordery", l)), 1)

    def test_a_short_term_does_not_match_letters_that_merely_fall_adjacent(self):
        """The last big false-positive source in the bulk dump.

        A three-letter term finds its letters lying consecutively across a word boundary
        constantly: `gok` matched `gg/ok`-shaped spans 22,245 times, `abo` matched `a bo`
        15,439, `hel` matched `he l` 31,273. Spelling a word out with separators is only a
        recognisable evasion when the word is long enough for that to be deliberate.
        """
        for term, innocent in (("gook", "gg/ok mate"), ("abo", "a bomb"),
                               ("hell", "he left"), ("piss", "a pistol")):
            l = lex(a={"terms": [term]})
            self.assertEqual(screening.scan(innocent, l), [],
                             "%r wrongly fired on %r" % (term, innocent))

    def test_separator_evasion_spanning_tokens_is_still_caught(self):
        """The whole reason whole-line matching was attractive. A match is accepted across
        tokens only when the original span really does cross a separator, which no innocent
        single word can do."""
        l = lex(a={"terms": ["badword"]})
        for spelling in ("b a d w o r d", "b.a.d.w.o.r.d", "b-a-d w0rd", "BAD WORD"):
            self.assertEqual(len(screening.scan("you %s here" % spelling, l)), 1,
                             "%r was not caught" % spelling)

    def test_the_infix_threshold_applies_to_the_NORMALISED_length(self):
        """Easy to trip over: `kitten` collapses to `kiten`, five characters, so it is
        treated as a short term and will not fire inside `kittenish`. The threshold is about
        the folded form, not what you typed."""
        self.assertEqual(screening.normalise("kitten")[0], "kiten")
        l = lex(a={"terms": ["kitten"]})
        self.assertEqual(screening.scan("that is kittenish", l), [])
        self.assertEqual(len(screening.scan("a kitten", l)), 1)

    def test_exception_suppresses_an_infix_match(self):
        l = lex(a={"terms": ["badword"]})
        self.assertEqual(len(screening.scan("that is badwordery", l)), 1)
        with_exc = lex(a={"terms": ["badword"], "exceptions": ["badwordery"]})
        self.assertEqual(screening.scan("that is badwordery", with_exc), [])

    def test_exception_does_not_hide_a_real_hit_elsewhere_in_the_line(self):
        l = lex(a={"terms": ["badword"], "exceptions": ["badwordery"]})
        self.assertEqual(len(screening.scan("badwordery but also badword", l)), 1)

    def test_repeated_term_counts_each_occurrence(self):
        hits = screening.scan("kitten kitten kitten", lex(a={"terms": ["kitten"]}))
        self.assertEqual(len(hits), 3)


class SummariseCase(unittest.TestCase):
    def test_counts_lines_not_hits(self):
        """One person saying one word forty times is a different thing from forty people
        saying it once, and lines_flagged is the number that answers the reviewer's
        question."""
        lines = ["badword badword badword", "clean", "clean too"]
        out = screening.summarise(lines, lex(a={"terms": ["badword"]}))
        self.assertEqual(out["lines_scanned"], 3)
        self.assertEqual(out["lines_flagged"], 1)
        self.assertEqual(out["per_category"]["a"], 3)

    def test_flagged_indexes_point_at_the_right_lines(self):
        lines = ["clean", "b4dw0rd", "clean", "BADWORD"]
        out = screening.summarise(lines, lex(a={"terms": ["badword"]}))
        self.assertEqual(out["flagged_line_indexes"], [1, 3])

    def test_spellings_are_ranked_by_frequency(self):
        lines = ["b4dw0rd", "b4dw0rd", "badword"]
        out = screening.summarise(lines, lex(a={"terms": ["badword"]}))
        self.assertEqual(out["spellings"][0], ("b4dw0rd", 2))

    def test_empty_lexicon_yields_no_categories(self):
        out = screening.summarise(["anything"], screening.Lexicon({}))
        self.assertEqual(out["per_category"], {})
        self.assertEqual(out["lines_flagged"], 0)

    def test_there_is_no_score_anywhere_in_the_output(self):
        """A weight or a score would immediately be read as the answer to a question counts
        cannot answer. Its absence is a design constraint, so assert it."""
        out = screening.summarise(["badword"], lex(a={"terms": ["badword"]}))
        for forbidden in ("score", "weight", "severity", "verdict", "confidence"):
            self.assertNotIn(forbidden, out)


class LexiconCase(unittest.TestCase):
    def test_loads_from_disk_and_normalises_terms(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"categories": {"a": {"note": "n", "terms": ["B4D-W0RD"]}}}, fh)
        fh.close()
        try:
            loaded = screening.Lexicon.load(fh.name)
            self.assertEqual(loaded.categories["a"]["terms"], ["badword"])
            self.assertEqual(loaded.term_count, 1)
            self.assertTrue(loaded)
        finally:
            os.unlink(fh.name)

    def test_no_path_gives_an_empty_lexicon(self):
        self.assertFalse(screening.Lexicon.load(None))

    def test_malformed_lexicon_is_rejected(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"nope": {}}, fh)
        fh.close()
        try:
            with self.assertRaises(ValueError):
                screening.Lexicon.load(fh.name)
        finally:
            os.unlink(fh.name)

    def test_blank_terms_are_dropped(self):
        self.assertEqual(lex(a={"terms": ["", "   ", "kitten"]}).term_count, 1)


if __name__ == "__main__":
    unittest.main()
