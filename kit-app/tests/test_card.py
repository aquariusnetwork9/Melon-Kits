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

GUILD = 4242
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

    def chats_window(self, uuid=None, name=None, start=None, end=None, max_pages=5):
        """Mirrors the real page-walk, cap included, so the partial-coverage path is reachable.

        `pageSize` is 100 on the live API and 150 is a 400, so the cap here is pages x 100 -- a
        fake that returned everything would make `chat_complete` untestable, which is the one
        field a reviewer's "their chat is clean" conclusion rests on.
        """
        if "chats" in self._fail:
            raise vc_mod.VcUnavailable("rate limited")
        room = max(1, int(max_pages)) * 100
        rows = self._chats[:room]
        return {"chats": rows, "total": len(self._chats),
                "complete": len(rows) >= len(self._chats),
                "pages_read": max(1, -(-len(rows) // 100))}


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
        return card_mod.gather(GUILD, name, uuid, user, self.cfg, client, self.st,
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
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        c = FakeClient(stats={"firstSeen": ago_ts(days=1)},
                       deaths=[{"time": ago_ts(minutes=5), "deathMessage": "died"}])
        card = self.build(c)
        self.assertTrue(card["cooldown"]["blocked"])
        self.assertIn("COOLDOWN", card_mod.headline(card))

    def test_reviewer_flags_appear_as_their_own_section(self):
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A, note="alt of Bob")
        card = self.build(FakeClient(stats={"firstSeen": ago_ts(days=5)}))
        self.assertIn("Reviewer flags", [s["name"] for s in card_mod.sections(card)])
        self.assertIn("alt of Bob", self.text_of(card, "Reviewer flags"))

    def test_shared_discord_account_is_shown_with_its_caveat(self):
        self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.create_ticket(GUILD, 100, "Alicia", "00000000-1111-2222-3333-4444444444bb")
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

    # ---------------------------------------------------------- recommendation
    #
    # Chat decides here, slurs included -- that is what reading chat history is for. What is
    # asserted instead is where the line now sits, because that is the load-bearing part:
    #   1. profanity still never moves the call (ambient register, and no target)
    #   2. chat denies on VOLUME, not on one hit, so the heading stays worth reading
    #   3. it counts flagged lines, never hits, so one sentence cannot look like a pattern

    def lex_with(self, **cats):
        return screening.Lexicon(
            {name: {"terms": terms} for name, terms in cats.items()})

    def chats_saying(self, *texts):
        return [{"time": ago_ts(days=1 + i), "chat": t} for i, t in enumerate(texts)]

    def clean(self, n):
        """`n` unremarkable lines, to pad a rate denominator."""
        return ["hello there friend"] * n

    def test_profanity_never_moves_the_call(self):
        """Measured against the 2025 dump, the tuned lexicon still flags 3.7% of ALL 2b2t chat.
        A recommendation that moved on profanity would be recommending against the median
        player, so the counts stay visible and stay out of the arithmetic."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying(*(["badword"] * 40)))
        card = self.build(c, lex=self.lex_with(profanity=["badword"]))
        rec = card_mod.recommend(card)
        # The hits are counted...
        self.assertEqual(card["screening"]["per_category"]["profanity"], 40)
        # ...and changed nothing.
        self.assertEqual(rec["call"], card_mod.CALL_APPROVE)
        self.assertFalse([r for r in rec["rules"] if "profanity" in r["rule"]])

    def test_a_few_slurs_ask_for_a_look(self):
        """Below the deny threshold a slur is still worth surfacing -- just not worth a Blocked
        heading, which is the difference between a useful recommendation and one reviewers
        learn to skip."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying(*(["badword"] * 2 + self.clean(200))))
        card = self.build(c, lex=self.lex_with(slur=["badword"]))
        rec = card_mod.recommend(card)
        said = [r for r in rec["rules"] if r["rule"] == "chat: slur"]
        self.assertTrue(said, "a slur hit did not even appear in the trace")
        self.assertEqual(said[0]["says"], card_mod.CALL_LOOK)
        self.assertNotEqual(rec["call"], card_mod.CALL_DENY)

    def test_enough_slurs_deny(self):
        """The change the whole rewrite is for: chat alone can now say deny."""
        n = card_mod.CHAT_DENY["slur"]["lines"]
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying(*(["badword"] * n)))
        card = self.build(c, lex=self.lex_with(slur=["badword"]))
        rec = card_mod.recommend(card)
        self.assertEqual(rec["call"], card_mod.CALL_DENY)
        said = [r for r in rec["rules"] if r["rule"] == "chat: slur"]
        # A deny has to show its own arithmetic, or a reviewer cannot argue with it.
        self.assertIn(str(n), said[0]["because"])

    def test_a_high_rate_denies_below_the_line_count(self):
        """Someone whose every other line is a slur, but who has said little, still denies --
        that is what the rate arm is for."""
        rate = card_mod.CHAT_DENY["slur"]["rate"]
        read = card_mod.CHAT_DENY_MIN_READ + 10
        bad = int(read * rate) + 1
        self.assertLess(bad, card_mod.CHAT_DENY["slur"]["lines"], "the count arm would fire")
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying(*(["badword"] * bad + self.clean(read - bad))))
        card = self.build(c, lex=self.lex_with(slur=["badword"]))
        self.assertEqual(card_mod.recommend(card)["call"], card_mod.CALL_DENY)

    def test_a_rate_off_a_handful_of_lines_does_not_deny(self):
        """2 flagged lines out of 3 is 67% and means nothing. Below the floor, only the
        absolute count may deny."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying("badword", "badword", "hello there friend"))
        card = self.build(c, lex=self.lex_with(slur=["badword"]))
        self.assertLess(len(card["chat_lines"]), card_mod.CHAT_DENY_MIN_READ)
        self.assertNotEqual(card_mod.recommend(card)["call"], card_mod.CALL_DENY)

    def test_one_line_matching_many_terms_is_one_line(self):
        """Thresholds are on flagged lines, not hits: "dox your ass" matches two terms, and a
        single sentence must not be able to look like a pattern."""
        card = self.build(
            FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying("aaaaaa bbbbbb cccccc")),
            lex=self.lex_with(slur=["aaaaaa", "bbbbbb", "cccccc"]))
        # Three hits, one line -- and one line is under every threshold there is.
        self.assertEqual(card["screening"]["per_category"]["slur"], 3)
        self.assertEqual(len(card["screening"]["category_lines"]["slur"]), 1)
        self.assertNotEqual(card_mod.recommend(card)["call"], card_mod.CALL_DENY)

    def test_off_game_denies_sooner_than_slur(self):
        """It is rarer and it is the only category aimed at a person, so it sits lower -- but
        not at 1, because a keyword list cannot tell a threat from a discussion of one."""
        self.assertLess(card_mod.CHAT_DENY["off_game"]["lines"],
                        card_mod.CHAT_DENY["slur"]["lines"])
        self.assertGreater(card_mod.CHAT_DENY["off_game"]["lines"], 1)

    def test_one_off_game_line_asks_for_a_look(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying("hello", "im gonna swat you", "bye"))
        card = self.build(c, lex=self.lex_with(off_game=["swat you"]))
        rec = card_mod.recommend(card)
        self.assertEqual(rec["call"], card_mod.CALL_LOOK)
        hit = [r for r in rec["rules"] if r["rule"] == "chat: off_game"]
        self.assertTrue(hit)
        # It has to name WHICH line, or it is just a number next to a word.
        self.assertIn("1", hit[0]["because"])

    def test_the_trace_fits_a_discord_field_without_losing_the_caveat(self):
        """Twelve rules on a heavy talker came to ~1630 characters against Discord's 1024 field
        cap, and a tail-truncation ate the "this is a recommendation, not the decision" line --
        on Blocked cards, the one place it has to survive. Adding `slur` is what made a trace
        that long reachable."""
        self.st.set_flag(GUILD, "deny", 500, mc_uuid=UUID_A, note="ban evading, third this month")
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A, note="alt")
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       chats=self.chats_saying(*(["badword swatting you"] * 60
                                                 + self.clean(60))))
        card = self.build(c, lex=self.lex_with(slur=["badword"], off_game=["swatting you"]))
        rec = card_mod.recommend(card)
        text = card_mod.rules_text(rec)
        self.assertEqual(rec["call"], card_mod.CALL_DENY)
        self.assertLessEqual(len(text), card_mod.RULES_TEXT_BUDGET)
        self.assertIn("recommendation, not the decision", text)
        # Every deny rule survives -- those are the reasons for the heading.
        self.assertEqual(text.count("✗"), sum(1 for r in rec["rules"] if r["says"] == "deny"))
        # And what was dropped is stated, not just missing.
        if len(rec["rules"]) > text.count("**"):
            self.assertIn("not shown", text)

    def test_a_short_trace_is_left_alone(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}])
        text = card_mod.rules_text(card_mod.recommend(self.build(c)))
        self.assertNotIn("not shown", text)
        self.assertLessEqual(len(text), card_mod.RULES_TEXT_BUDGET)

    def test_every_deny_threshold_names_a_category_that_can_fire(self):
        """A CHAT_DENY entry for a category the loop never consults would be dead config that
        looks live."""
        for category in card_mod.CHAT_DENY:
            self.assertIn(category, card_mod._DECIDING_CATEGORIES, category)

    def test_discussing_doxxing_is_not_a_hit(self):
        """The bare noun is out of the shipped lexicon on purpose: with chat able to deny, a
        term that fires on talking ABOUT the act would deny people for talking about it. The
        top five accounts by score in the 2025 dump were an ad fleet whose copy contained it."""
        lex = screening.Lexicon.load(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "lexicon.example.json"))
        for innocent in ("doxxing is bad", "he got doxxed", "stop doxing people"):
            self.assertFalse(screening.scan(innocent, lex), innocent)
        for real in ("im gonna dox you", "i will doxx you", "doxxed you already"):
            self.assertTrue(screening.scan(real, lex), real)

    def test_only_facts_can_deny(self):
        """A reviewer's own do-not-serve flag, and the cooldown. Nothing inferred."""
        self.st.set_flag(GUILD, "deny", 500, mc_uuid=UUID_A, note="known alt of someone")
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}])
        rec = card_mod.recommend(self.build(c))
        self.assertEqual(rec["call"], card_mod.CALL_DENY)
        self.assertTrue([r for r in rec["rules"] if r["rule"] == "do-not-serve flag"])

    def test_a_cooldown_denies_and_says_which_one(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.record_kit(GUILD, tid, 100, "Alice", UUID_A, kind=store_mod.KIND_RESCUE)
        c = FakeClient(stats={"firstSeen": ago_ts(days=800)})
        rec = card_mod.recommend(card_mod.gather(
            GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st, screening.Lexicon({}),
            request_type=store_mod.KIND_RESCUE))
        self.assertEqual(rec["call"], card_mod.CALL_DENY)
        because = [r["because"] for r in rec["rules"] if r["rule"] == "cooldown"][0]
        self.assertIn("rescue kit", because)

    def test_a_deny_outranks_an_approve(self):
        """The call is the most cautious thing any rule said, so one blocker is enough."""
        self.st.set_flag(GUILD, "deny", 500, mc_uuid=UUID_A)
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}])
        rec = card_mod.recommend(self.build(c))
        says = {r["says"] for r in rec["rules"]}
        self.assertIn(card_mod.CALL_APPROVE, says)      # the death still fired
        self.assertEqual(rec["call"], card_mod.CALL_DENY)

    def test_a_failed_lookup_can_never_read_as_clean(self):
        """"No recent death" and "the death lookup failed" are opposite claims, and the same
        goes for chat. Neither may produce a confident approve."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800)}, fail=("deaths", "chats"))
        rec = card_mod.recommend(self.build(c))
        self.assertEqual(rec["call"], card_mod.CALL_LOOK)
        rules = {r["rule"] for r in rec["rules"]}
        self.assertIn("deaths unavailable", rules)
        self.assertIn("chat unavailable", rules)

    def test_a_sampled_year_says_so_rather_than_implying_a_clean_one(self):
        """94% of players fit inside the page cap. For the rest, "nothing found" covers only
        what was read, and the difference has to reach the reviewer."""
        many = self.chats_saying(*(["just chatting"] * 900))
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}], chats=many)
        card = self.build(c)
        self.assertFalse(card["chat_complete"])
        rec = card_mod.recommend(card)
        self.assertEqual(rec["call"], card_mod.CALL_LOOK)
        self.assertTrue([r for r in rec["rules"] if r["rule"] == "chat only sampled"])
        self.assertIn("not examined", rec["basis"])

    def test_a_short_year_is_reported_as_complete(self):
        """The median player says twelve lines in a year, so this is the normal case."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}],
                       chats=self.chats_saying(*(["hello"] * 12)))
        card = self.build(c)
        self.assertTrue(card["chat_complete"])
        self.assertEqual(card["chat_window_days"], 365)
        self.assertIn("read all 12", card_mod.chat_coverage_phrase(card))
        self.assertEqual(card_mod.recommend(card)["call"], card_mod.CALL_APPROVE)

    def test_the_trace_is_not_a_score(self):
        """A score is one number standing in for a judgement nobody can audit. Every rule here
        names the evidence it read, so a reviewer can disagree with a specific claim."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}])
        rec = card_mod.recommend(self.build(c))
        self.assertEqual(set(rec), {"call", "rules", "basis"})
        for r in rec["rules"]:
            self.assertEqual(set(r), {"rule", "says", "because"})
            for value in r.values():
                self.assertIsInstance(value, str)
                self.assertNotIsInstance(value, (int, float))
            # Every rule explains itself; none is a bare label.
            self.assertGreater(len(r["because"]), 20)

    def test_funding_never_gets_an_approve_from_history_alone(self):
        """There is no equivalent of the death line for a build. Nothing available can verify a
        project is real, so an established account earns "nothing against them" and the
        judgement stays with the reviewer."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000})
        card = card_mod.gather(GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st,
                               screening.Lexicon({}),
                               request_type=store_mod.KIND_FUNDING,
                               details={"project": "hub", "needs": "obsidian"})
        rec = card_mod.recommend(card)
        rules = {r["rule"] for r in rec["rules"]}
        self.assertIn("established account", rules)
        self.assertNotIn("recent death", rules)
        self.assertNotIn("new to 2b2t", rules)

    def test_the_rule_trace_leads_the_card_and_repeats_the_caveat(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "died"}])
        card = self.build(c)
        first = card_mod.sections(card)[0]
        self.assertEqual(first["name"], "Nothing against them")
        # The caveat travels with the recommendation, because that is where it gets read.
        self.assertIn("profanity", first["value"].lower())
        # It must say which way round it is now: profanity out, slurs in.
        self.assertIn("slurs", first["value"].lower())

    # ------------------------------------------------------------ meeting point

    def test_coords_are_read_out_of_every_shape_people_type(self):
        for text in ("1200 64 -840", "1200, 64, -840", "x=1200 y=64 z=-840",
                     "1200/64/-840", "  1200   64   -840  "):
            self.assertEqual(card_mod.parse_coords(text), (1200, 64, -840), text)

    def test_a_landmark_alongside_the_numbers_still_parses(self):
        self.assertEqual(card_mod.parse_coords("1200 64 -840 by the big cobble tower"),
                         (1200, 64, -840))

    def test_a_number_glued_to_letters_is_not_a_coordinate(self):
        """'2b2t' must not contribute a 2. Without the word-boundary guard this reads as
        (2, 2, 1200) and sends a runner to entirely the wrong place."""
        self.assertEqual(card_mod.parse_coords("2b2t spawn 1200 64 -840"),
                         (1200, 64, -840))

    def test_a_decimal_coordinate_truncates_rather_than_confusing_the_parse(self):
        self.assertEqual(card_mod.parse_coords("1200.5 64.9 -840.1"), (1200, 64, -840))

    def test_input_that_is_not_three_numbers_is_refused(self):
        for text in ("somewhere near the hub", "~ ~ ~", "1200 64", "", None):
            self.assertIsNone(card_mod.parse_coords(text), text)

    def test_out_of_range_values_are_refused(self):
        self.assertIsNone(card_mod.parse_coords("99999999999 64 0"))
        # A transposed pair -- "x y z" typed as "x z y" -- shows up as an impossible height.
        self.assertIsNone(card_mod.parse_coords("1200 -840 64"))

    def test_formatting_says_so_when_there_is_nothing_yet(self):
        self.assertEqual(card_mod.format_coords(None), "not given yet")
        self.assertIn("nether", card_mod.format_coords((0, 64, 0), "nether"))

    # -------------------------------------------------------------- the chat pager
    #
    # The pager holds no page state anywhere: the page a reviewer is looking at is re-derived
    # from the ledger rows on every button press. That only works if the packing is a pure,
    # deterministic function of the rows, which is what most of this section checks.

    def rows(self, *chats):
        """Ledger-shaped rows. A leading '!' marks the line as flagged."""
        out = []
        for i, chat in enumerate(chats):
            flagged = chat.startswith("!")
            out.append({"position": i, "chat_ts": "2026-07-24T22:%02d:00" % (i % 60),
                        "chat": chat[1:] if flagged else chat,
                        "flagged": 1 if flagged else 0})
        return out

    def test_the_same_rows_always_page_the_same_way(self):
        """The pager re-derives page N on every press instead of remembering it, so identical
        input has to give identical pages or Next would land somewhere else than Previous."""
        rows = self.rows(*["line %d" % i for i in range(50)])
        self.assertEqual(card_mod.chat_pages(rows), card_mod.chat_pages(rows))

    def test_no_page_can_exceed_the_embed_description_limit(self):
        rows = self.rows(*["x" * 300 for _ in range(40)])
        for page in card_mod.chat_pages(rows):
            self.assertLessEqual(len(card_mod.chat_page_body(page)), 4096)

    def test_a_line_longer_than_a_whole_page_is_split_not_dropped(self):
        """A single 9k-character chat line cannot fit any page, and the packing loop would spin
        forever if it tried to flush an empty page to make room for it."""
        pages = card_mod.chat_pages(self.rows("y" * 9000))
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(card_mod.chat_page_body(page)), 4096)
        recovered = "".join(page_line for page in pages for page_line in page["lines"])
        self.assertEqual(recovered.count("y"), 9000)

    def test_positions_come_from_the_ledger_not_the_page_offset(self):
        """/flagline matches on the stored position. Re-numbering per page would offset every
        label by the page offset and mislabel the only screening data that exists."""
        rows = self.rows(*["line %d" % i for i in range(45)])
        pages = card_mod.chat_pages(rows)
        self.assertGreater(len(pages), 1)
        last = pages[-1]
        self.assertGreater(last["first"], 0)
        # Asserted on the number, not on a column width: the point is that a page which starts
        # at position 30 prints 30 and not 0.
        self.assertEqual(last["lines"][0].split()[0], str(last["first"]))

    def test_markdown_and_mentions_cannot_escape_the_page(self):
        page = card_mod.chat_pages(self.rows(
            "__Notch__ *took* it @everyone <@1234>", "```rm -rf```"))[0]
        body = card_mod.chat_page_body(page)
        # An even number of fences means nothing closed the block early and spilled the rest
        # of the page into live markdown.
        self.assertEqual(body.count("```") % 2, 0)
        self.assertTrue(body.startswith("```"))
        self.assertTrue(body.endswith("```"))

    def test_a_newline_in_chat_stays_one_rendered_line(self):
        """One stored row must render as exactly one line, or the position numbers stop lining
        up with what a reviewer counts on screen."""
        page = card_mod.chat_pages(self.rows("first\nsecond\r\nthird"))[0]
        self.assertEqual(len(page["lines"]), 1)

    def test_flagged_lines_are_marked_and_listed(self):
        pages = card_mod.chat_pages(self.rows("fine", "!not fine", "fine again"))
        self.assertEqual(pages[0]["flagged"], [1])
        marked = [l for l in pages[0]["lines"] if l.startswith("!")]
        self.assertEqual(len(marked), 1, pages[0]["lines"])
        self.assertIn("not fine", marked[0])

    # ------------------------------------------------- how a page is laid out
    #
    # Discord gives a code block inside an embed description no horizontal scrollbar, so these
    # are about the two things that made the list hard to read: a wasted left margin, and a long
    # line breaking at column 0 underneath the next entry's number.

    def test_a_long_line_wraps_with_a_hanging_indent(self):
        """Uniform-width tokens, so the wrap lands on a token boundary and the same word starts
        both lines -- which makes the column comparison below mean what it says."""
        page = card_mod.chat_pages(self.rows("abcdefg " * 20))[0]
        self.assertGreater(len(page["lines"]), 1, "did not wrap at all")
        first, second = page["lines"][0], page["lines"][1]
        # The continuation starts its text in the same column as the first line's.
        self.assertEqual(first.index("abcdefg"), second.index("abcdefg"), (first, second))
        # It still says which position it belongs to, because a wrap can straddle a page...
        self.assertEqual(first.split()[0], second.split()[0])
        # ...and it does not repeat the timestamp, which would read as a second message.
        self.assertIn("07-24", first)
        self.assertNotIn("07-24", second)

    def test_no_rendered_line_is_wider_than_the_wrap(self):
        rows = self.rows(*["word " * 40 for _ in range(5)])
        for page in card_mod.chat_pages(rows):
            for line in page["lines"]:
                self.assertLessEqual(len(line), card_mod.CHAT_PAGE_WRAP, line)

    def test_wrapping_drops_no_character_of_what_was_said(self):
        """A break keeps its space on the line before it. Eating one is still editing the
        evidence, which is the whole reason `neutralise` exists."""
        said = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
                "november oscar papa quebec romeo sierra tango uniform victor whiskey")
        lines = card_mod.chat_pages(self.rows(said))[0]["lines"]
        self.assertGreater(len(lines), 1, "did not wrap, so this proves nothing")
        # Every line shares one prefix width -- that IS the hanging indent -- so the text is
        # recoverable by slicing it off, and what comes back must be what went in.
        plen = lines[0].index("alpha")
        self.assertEqual("".join(l[plen:] for l in lines), said)

    def test_the_left_margin_is_not_wasted_on_a_short_log(self):
        """A 20-line log used to print every entry behind up to five columns of blank space: the
        position field was fixed at four wide and right-aligned, whatever the numbers were."""
        page = card_mod.chat_pages(self.rows(*["hi"] * 20))[0]
        for line in page["lines"]:
            # One column, and only ever the unflagged rows' empty flag marker.
            self.assertLessEqual(len(line) - len(line.lstrip()), 1, repr(line))

    def test_the_number_column_does_not_move_between_pages(self):
        """Width comes from the whole log, not the page: derived per page, position 9 and
        position 10 would sit in different columns and the text would jump sideways on Next."""
        rows = self.rows(*["line %d" % i for i in range(45)])
        pages = card_mod.chat_pages(rows)
        self.assertGreater(len(pages), 1)
        stamp_at = [l.index("07-24") for p in pages for l in p["lines"] if "07-24" in l]
        self.assertEqual(len(set(stamp_at)), 1, "the stamp column moves between pages")

    def test_an_empty_log_never_claims_the_applicant_was_silent(self):
        """`chat_lines` is empty both when the player said nothing and when the api.2b2t.vc
        call failed, and the ledger cannot tell them apart -- so asserting silence would be
        the worse of the two wrong answers."""
        pages = card_mod.chat_pages([])
        self.assertEqual(len(pages), 1)
        body = card_mod.chat_page_body(pages[0])
        self.assertIn("does not record", body)
        self.assertIn("Recent public chat", body)

    def test_the_footer_carries_the_page_and_the_flagline_hint(self):
        rows = self.rows(*["line %d" % i for i in range(45)])
        pages = card_mod.chat_pages(rows)
        footer = card_mod.chat_page_footer(pages, 0, len(rows), 42)
        self.assertIn("Page 1/%d" % len(pages), footer)
        self.assertIn("/flagline 42", footer)

    def test_the_downloadable_file_is_rebuilt_from_the_ledger(self):
        text = card_mod.chat_file(self.rows("gg", "!not gg"), 42, "Alice")
        self.assertIn("ticket #42", text)
        self.assertIn("gg", text)
        self.assertIn("!", text)

    # ------------------------------------------------------- the two request kinds

    def test_a_funding_card_leads_with_the_ask_not_with_a_death(self):
        """A death an hour ago is the entire case for a rescue kit and says nothing about
        whether to fund a build. Leading a funding card with it would put the wrong fact in the
        one position a reviewer always reads."""
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "Alice died"}])
        card = card_mod.gather(GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st,
                               screening.Lexicon({}),
                               request_type=store_mod.KIND_FUNDING,
                               details={"project": "nether hub", "needs": "4 stacks obsidian",
                                        "scale": "a weekend"})
        names = [s["name"] for s in card_mod.sections(card)]
        # The ask comes before any history. Relative order, not index 0 -- the rule trace sits
        # above everything as a table of contents for the card.
        self.assertLess(names.index("The ask"), names.index("Recent deaths"))
        self.assertLess(names.index("The ask"), names.index("On 2b2t"))
        self.assertIn("nether hub", self.text_of(card, "The ask"))
        self.assertNotIn("died", card_mod.headline(card))
        # And the rescue-only "claim verified" line must not appear on a funding card.
        self.assertNotIn("verified", self.text_of(card, "Recent deaths"))

    def test_a_rescue_card_still_leads_with_the_death(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=800), "playtimeSeconds": 400000},
                       deaths=[{"time": ago_ts(hours=1), "deathMessage": "Alice died"}])
        card = self.build(c)
        names = [s["name"] for s in card_mod.sections(card)]
        self.assertNotIn("The ask", names)
        self.assertLess(names.index("Recent deaths"), names.index("On 2b2t"))
        self.assertIn("died", card_mod.headline(card))
        self.assertIn("verified", self.text_of(card, "Recent deaths"))

    def test_a_missing_funding_answer_says_so_rather_than_rendering_blank(self):
        c = FakeClient(stats={"firstSeen": ago_ts(days=800)})
        card = card_mod.gather(GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st,
                               screening.Lexicon({}),
                               request_type=store_mod.KIND_FUNDING,
                               details={"project": "hub"})
        self.assertIn("not given", self.text_of(card, "The ask"))

    def test_a_ticketless_lookup_reports_any_grant_not_just_rescue(self):
        """`/lookup` vets a name with no ticket in front of the reviewer, so there is no one
        cooldown to report. Scoping it to rescue by default would answer "cooldown clear" for
        somebody whose only grant was project funding three days ago -- which is precisely the
        question the lookup exists to answer."""
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A,
                                    request_type=store_mod.KIND_FUNDING)
        self.st.record_kit(GUILD, tid, 100, "Alice", UUID_A, kind=store_mod.KIND_FUNDING)
        c = FakeClient(stats={"firstSeen": ago_ts(days=800)})

        lookup = self.build(c)                       # no request_type -- what /lookup does
        self.assertIsNone(lookup["request_type"])
        self.assertTrue(lookup["cooldown"]["blocked"])

        # A rescue *ticket* still gets the rescue clock, which the funding grant leaves alone.
        rescue = card_mod.gather(GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st,
                                 screening.Lexicon({}),
                                 request_type=store_mod.KIND_RESCUE)
        self.assertFalse(rescue["cooldown"]["blocked"])

    def test_a_card_built_before_request_types_reads_as_rescue(self):
        self.assertEqual(card_mod.kind_of({}), store_mod.KIND_RESCUE)
        self.assertEqual(card_mod.kind_of({"request_type": None}), store_mod.KIND_RESCUE)
        self.assertEqual(card_mod.kind_of({"request_type": "nonsense"}),
                         store_mod.KIND_RESCUE)

    def test_the_funding_cooldown_is_the_one_a_funding_card_reports(self):
        """Separate clocks: a rescue kit on record must not show as a block on a funding card."""
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.record_kit(GUILD, tid, 100, "Alice", UUID_A, kind=store_mod.KIND_RESCUE)
        c = FakeClient(stats={"firstSeen": ago_ts(days=800)})
        rescue = self.build(c)
        funding = card_mod.gather(GUILD, "Alice", UUID_A, 100, self.cfg, c, self.st,
                                  screening.Lexicon({}),
                                  request_type=store_mod.KIND_FUNDING)
        self.assertTrue(rescue["cooldown"]["blocked"])
        self.assertFalse(funding["cooldown"]["blocked"])
        # The history is not kind-scoped: a reviewer wants to see everything either way.
        self.assertEqual(len(funding["kit_history"]), 1)

    def test_duration_formatting(self):
        self.assertEqual(card_mod.duration(0), "none recorded")
        self.assertEqual(card_mod.duration(None), "none recorded")
        # Sub-minute spans must not floor to "0 minutes": a ticket closed in seconds is
        # exactly the kind the archive should show honestly.
        self.assertEqual(card_mod.duration(20), "20 seconds")
        self.assertEqual(card_mod.duration(59), "59 seconds")
        self.assertEqual(card_mod.duration(60), "1 minutes")
        self.assertIn("minutes", card_mod.duration(600))
        self.assertIn("hours", card_mod.duration(15001))
        self.assertIn("days", card_mod.duration(400000))


if __name__ == "__main__":
    unittest.main()
