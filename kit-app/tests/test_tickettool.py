"""Reading Ticket Tool transcripts.

The parsing is fuzzy by necessity -- the Minecraft name is prose somebody typed, not a field --
so what these tests pin down is the *shape* of the fuzziness: which phrasings are recovered,
which are correctly ignored, and above all that a wrong name cannot quietly become somebody's
kit history. The Mojang check is the real filter and lives in the importer; this is everything
before it.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import redact
import tickettool

OWNER = 697802598459375666
HELPER = 693174944825212948


def msg(content, user_id=OWNER, bot=False, username="someone"):
    return {"content": content, "user_id": str(user_id), "bot": bot, "username": username}


def names(entries, owner=OWNER):
    return tickettool.candidate_names(entries, owner, redactor=redact.redact)


class DecodeCase(unittest.TestCase):
    def _html(self, payload):
        blob = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        return '<Base-Transcript><script>let messages = "%s";</script></Base-Transcript>' % blob

    def test_decodes_the_embedded_payload(self):
        """The conversation is a base64 JSON blob in a script tag, not the visible HTML."""
        html = self._html([{"content": "hello", "user_id": "1"}])
        self.assertEqual(tickettool.decode_transcript(html)[0]["content"], "hello")

    def test_a_file_that_is_not_a_transcript_yields_nothing(self):
        self.assertEqual(tickettool.decode_transcript("<html>nope</html>"), [])

    def test_a_corrupt_payload_yields_nothing_rather_than_raising(self):
        """One unreadable file out of 1,400 must not stop the sweep."""
        self.assertEqual(
            tickettool.decode_transcript('<script>let messages = "!!!not base64!!!";</script>'),
            [])

    def test_owner_and_ticket_name_come_out_of_the_embed_fields(self):
        owner, ticket = tickettool.owner_from_fields([
            ("Ticket Owner", "<@697802598459375666>"),
            ("Ticket Name", "closed-1454"),
            ("Panel Name", "Spawn Support Tickets"),
        ])
        self.assertEqual(owner, OWNER)
        self.assertEqual(ticket, "closed-1454")

    def test_missing_fields_are_not_an_error(self):
        self.assertEqual(tickettool.owner_from_fields([("Panel Name", "x")]), (None, None))


class ExtractionCase(unittest.TestCase):
    # Phrasings taken from real tickets, not invented.
    def test_recovers_the_phrasings_people_actually_use(self):
        for text, want in [
            ("Hi! My Minecraft username is ComicSquid74273. I joined today", "ComicSquid74273"),
            ("Hello my in game name is zZ350Zz7384 and I have priority", "zZ350Zz7384"),
            ("ign: Notch_2 please help", "Notch_2"),
            ("IGT Player_One", "Player_One"),
            ("my mc name is Steve123", "Steve123"),
            ("username is Alex_the_2nd", "Alex_the_2nd"),
        ]:
            self.assertEqual(names([msg(text)]), [want], text)

    def test_ordinary_conversation_yields_nothing(self):
        for text in ("hello can you help me please",
                     "thanks so much for the help!",
                     "I am waiting in queue right now"):
            self.assertEqual(names([msg(text)]), [], text)

    def test_coordinates_never_become_a_name(self):
        """Redaction runs BEFORE matching, so a coordinate run cannot be read as an account."""
        entries = [msg("my base is at 1000 2000 500, my ign is RealName99")]
        self.assertEqual(names(entries), ["RealName99"])

    def test_the_owners_own_words_outrank_everyone_elses(self):
        """A helper naming a third party must never outrank the applicant naming themselves.

        This is the one that matters: the ledger is what later refuses somebody a kit, so
        binding the wrong account to a Discord id has a real victim.
        """
        entries = [
            msg("ign SomeoneElse", user_id=HELPER),
            msg("hi, my minecraft username is TheApplicant"),
        ]
        self.assertEqual(names(entries)[0], "TheApplicant")

    def test_bot_messages_are_ignored(self):
        """Ticket Tool's own opening embed asks for the username; it must not answer itself."""
        entries = [msg("please tell us your minecraft username Something", bot=True)]
        self.assertEqual(names(entries), [])

    def test_duplicates_collapse_case_insensitively(self):
        entries = [msg("ign Steve123"), msg("my minecraft name is steve123")]
        self.assertEqual(names(entries), ["Steve123"])

    def test_stopwords_are_dropped(self):
        self.assertEqual(names([msg("my minecraft username is please")]), [])

    def test_names_too_long_to_be_real_are_not_matched(self):
        """17+ characters cannot be a Minecraft account, so it is prose."""
        self.assertNotIn("averyveryverylongname", names([msg("my ign is averyveryverylongname")]))

    def test_participants_lists_humans_only(self):
        got = tickettool.participants([
            msg("hi", username="applicant"),
            msg("hello", user_id=HELPER, username="helper"),
            msg("beep", user_id=999, bot=True, username="Ticket Tool"),
        ])
        self.assertEqual(set(got), {str(OWNER), str(HELPER)})


if __name__ == "__main__":
    unittest.main()
