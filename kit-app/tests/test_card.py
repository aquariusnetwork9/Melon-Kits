"""Reviewer-card tests, against a fake API client. No network.

Two properties get the most attention here because they are the ones that would quietly
change a decision if they broke:

- **Redaction happens inside `gather`**, so no display path can be the one that forgot.
- **A failed section and an empty section render differently.** "Could not retrieve deaths"
  and "no deaths" mean opposite things, and rendering the first as the second would decline
  people for having a clean record.

All coordinates are synthetic and inside 20k of the origin.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import card as card_mod
import config
import screening
import store as store_mod
import vc as vc_mod

UUID_A = "00000000-1111-2222-3333-4444444444aa"
HOUR = 3600


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago_ts(**kw):
    return iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**kw))


class FakeClient(object):
    """Stands in for vc.Client. `fail` names sections that should raise."""

    def __init__(self, stats=None, deaths=None, chats=None, fail=()):
        self._stats = stats
        self._deaths = deaths or []
        self._chats = chats or []
        self._fail = set(fail)

    def stats(self, uuid=None, name=None):
        if "stats" in self._fail:
            raise vc_mod.VcUnavailable("rate limited")
        return self._stats

    def deaths(self, uuid=None, name=None, limit=3):
        if "deaths" in self._fail:
            raise vc_mod.VcUnavailable("rate limited")
        return {"deaths": self._deaths[:limit], "total": len(self._deaths)}

    def chats(self, uuid=None, name=None, limit=100):
        if "chats" in self._fail:
            raise vc_mod.VcUnavailable("rate limited")
        return {"chats": self._chats[:limit], "total": len(self._chats)}


class CardCase(unittest.TestCase):
    def setUp(self):
        # The failure-path tests deliberately make sections fail, which logs at WARNING.
        # Silence it so a green run has clean output and a real warning stands out.
        import logging
        logging.getLogger("melonkit.card").setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp(prefix="melonkit-card-")
        self.st = store_mod.open_store(os.path.join(self.dir, "t.sqlite3"))
        self.cfg = config.load_config(None, env={})
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, client, lex=None, user=100, name="Alice", uuid=UUID_A):
        return card_mod.gather(name, uuid, user, self.cfg, client, self.st,
                               lex or screening.Lexicon({}))

    def text_of(self, card, section_name):
        for sec in card_mod.sections(card):
            if sec["name"] == section_name:
                return sec["value"]
        return ""

    # ------------------------------------------------------------------ redaction

    def test_gather_redacts_coordinates_out_of_chat(self):
        c = FakeClient(chats=[{"time": ago_ts(hours=1), "chat": "my base is 1234 64 -5678"}])
        card = self.build(c)
        self.assertEqual(card["coords_redacted"], 1)
        line = card["chat_lines"][0]["chat"]
        self.assertIn("[coords]", line)
        self.assertNotIn("1234", line)
        self.assertNotIn("5678", line)

    def test_chat_dump_carries_only_redacted_text(self):
        c = FakeClient(chats=[{"time": ago_ts(hours=1), "chat": "base 1234, -5678"}])
        dump = card_mod.chat_dump(self.build(c))
        self.assertNotIn("1234", dump)
        self.assertIn("[coords]", dump)

    # -------------------------------------------------------------------- signals

    def test_a_recent_death_is_the_verified_claim(self):
        c = FakeClient(
            stats={"firstSeen": ago_ts(days=400), "lastSeen": ago_ts(minutes=5),
                   "playtimeSeconds": 90000},
            deaths=[{"time": ago_ts(minutes=11), "deathMessage": "Alice was slain by Bob",
                     "killerPlayerName": "Bob"}])
        card = self.build(c)
        self.assertTrue(card["recently_died"])
        body = self.text_of(card, "Recent deaths")
        self.assertIn("verified", body)
        self.assertIn("Bob", body)

    def test_an_old_death_does_not_support_the_claim(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=400), "playtimeSeconds": 90000},
                       deaths=[{"time": ago_ts(days=30), "deathMessage": "died"}])
        card = self.build(c)
        self.assertFalse(card["recently_died"])
        self.assertNotIn("verified", self.text_of(card, "Recent deaths"))

    def test_new_account_is_flagged_as_new(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=2), "playtimeSeconds": 600})
        card = self.build(c)
        self.assertTrue(card["is_new"])
        self.assertIn("first seen", card_mod.headline(card))

    def test_low_playtime_on_an_old_account_renders_as_ambiguous_not_as_a_lean(self):
        """This shape describes an alt AND a returning lapsed player. The card must say so
        rather than imply either."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=900), "lastSeen": ago_ts(days=800),
                              "playtimeSeconds": 400})
        card = self.build(c)
        self.assertTrue(card["low_playtime_old_account"])
        body = self.text_of(card, "On 2b2t")
        self.assertIn("not evidence either way", body)

    def test_untracked_player_says_so_plainly(self):
        card = self.build(FakeClient(stats=None))
        self.assertFalse(card["tracked"])
        self.assertIn("Not tracked", card_mod.headline(card))

    def test_no_card_field_contains_a_score(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=2), "playtimeSeconds": 60})
        card = self.build(c)
        for forbidden in ("score", "confidence", "probability", "risk", "verdict"):
            self.assertNotIn(forbidden, card)

    # ---------------------------------------------------- missing vs empty

    def test_a_failed_death_lookup_is_not_reported_as_no_deaths(self):
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}, fail=("deaths",)))
        self.assertFalse(card["deaths_ok"])
        body = self.text_of(card, "Recent deaths")
        self.assertIn("not the same as", body)
        self.assertTrue(card["errors"])

    def test_an_empty_death_list_reads_as_none_recorded(self):
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}, deaths=[]))
        self.assertTrue(card["deaths_ok"])
        self.assertIn("none recorded", self.text_of(card, "Recent deaths"))

    def test_a_failed_chat_lookup_is_not_reported_as_said_nothing(self):
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}, fail=("chats",)))
        self.assertFalse(card["chats_ok"])
        self.assertIn("not the same as", self.text_of(card, "Recent public chat"))

    def test_every_section_failing_still_produces_a_card(self):
        card = self.build(FakeClient(fail=("stats", "deaths", "chats")))
        self.assertEqual(len(card["errors"]), 3)
        self.assertTrue(card_mod.sections(card))
        self.assertIn("Incomplete", [s["name"] for s in card_mod.sections(card)])

    # -------------------------------------------------------------------- ledger

    def test_cooldown_dominates_the_headline(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        c = FakeClient(stats={"firstSeen": ago_ts(days=1)},
                       deaths=[{"time": ago_ts(minutes=5), "deathMessage": "died"}])
        card = self.build(c)
        self.assertTrue(card["cooldown"]["blocked"])
        self.assertIn("COOLDOWN", card_mod.headline(card))

    def test_reviewer_flags_appear_as_their_own_section(self):
        self.st.set_flag("alt", 500, mc_uuid=UUID_A, note="alt of Bob")
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}))
        self.assertIn("Reviewer flags", [s["name"] for s in card_mod.sections(card)])
        self.assertIn("alt of Bob", self.text_of(card, "Reviewer flags"))

    def test_shared_discord_account_is_shown_with_its_caveat(self):
        self.st.create_ticket(100, "Alice", UUID_A)
        self.st.create_ticket(100, "Alicia", "00000000-1111-2222-3333-4444444444bb")
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}))
        body = self.text_of(card, "Kit ledger")
        self.assertIn("Alicia", body)
        self.assertIn("also a rename", body)

    # ----------------------------------------------------------------- screening

    def test_screening_counts_appear_with_the_no_verdict_caveat(self):
        lex = screening.Lexicon({"test": {"terms": ["badword"]}})
        c = FakeClient(chats=[{"time": ago_ts(hours=1), "chat": "b4dw0rd here"},
                              {"time": ago_ts(hours=2), "chat": "gg"}])
        card = self.build(c, lex=lex)
        self.assertEqual(card["screening"]["lines_flagged"], 1)
        body = self.text_of(card, "Recent public chat")
        self.assertIn("test=1", body)
        self.assertIn("not a verdict", body)

    def test_no_lexicon_says_read_the_lines(self):
        c = FakeClient(chats=[{"time": ago_ts(hours=1), "chat": "anything"}])
        card = self.build(c)
        self.assertIsNone(card["screening"])
        self.assertIn("read the lines", self.text_of(card, "Recent public chat"))

    # --------------------------------------------------------------------- time

    def test_relative_time_formatting(self):
        now = datetime.datetime(2026, 7, 25, 12, 0, tzinfo=datetime.timezone.utc)
        cases = [
            (datetime.timedelta(seconds=30), "30 seconds ago"),
            (datetime.timedelta(minutes=11), "11 minutes ago"),
            (datetime.timedelta(hours=5), "5 hours ago"),
            (datetime.timedelta(days=3), "3 days ago"),
        ]
        for delta, want in cases:
            self.assertEqual(card_mod.ago(now - delta, now), want)
        self.assertEqual(card_mod.ago(None), "unknown")

    def test_parses_both_timestamp_shapes_the_api_emits(self):
        """Python 3.9's fromisoformat cannot read a trailing Z, and the API emits both
        fractional and whole-second forms."""
        self.assertIsNotNone(card_mod.parse_ts("2016-06-19T03:28:47Z"))
        self.assertIsNotNone(card_mod.parse_ts("2022-09-12T23:10:24.084Z"))
        self.assertIsNone(card_mod.parse_ts("not a date"))
        self.assertIsNone(card_mod.parse_ts(None))

    def test_duration_formatting(self):
        self.assertEqual(card_mod.duration(0), "none recorded")
        self.assertEqual(card_mod.duration(None), "none recorded")
        self.assertIn("minutes", card_mod.duration(600))
        self.assertIn("hours", card_mod.duration(15001))
        self.assertIn("days", card_mod.duration(400000))


if __name__ == "__main__":
    unittest.main()
