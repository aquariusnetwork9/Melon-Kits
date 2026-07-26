"""Ledger tests.

The cooldown is the whole anti-abuse surface, so most of this file is about the ways it can
be sidestepped and whether it holds -- checked against the Discord id and the Minecraft UUID
independently, because either alone is trivial to get around.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import store as store_mod

GUILD = 4242
GUILD_B = 9999
UUID_A = "00000000-1111-2222-3333-4444444444aa"
UUID_B = "00000000-1111-2222-3333-4444444444bb"
DAY = 86400


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="melonkit-store-")
        self.st = store_mod.open_store(os.path.join(self.dir, "t.sqlite3"))
        self.addCleanup(self._teardown)

    def _teardown(self):
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ tickets

    def test_open_ticket_lifecycle(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A, "lost everything")
        self.assertEqual(self.st.open_ticket_count(GUILD, 100), 1)
        self.assertEqual(self.st.open_ticket_for(GUILD, 100)["mc_name"], "Alice")
        self.st.set_ticket_thread(tid, 999)
        self.assertEqual(self.st.ticket_for_thread(999)["id"], tid)

        self.st.record_decision(tid, 500, store_mod.STATUS_APPROVED)
        self.assertEqual(self.st.open_ticket_count(GUILD, 100), 0)
        self.assertEqual(self.st.get_ticket(tid)["status"], store_mod.STATUS_APPROVED)
        self.assertIsNotNone(self.st.get_ticket(tid)["closed_at"])

    def test_decision_records_the_reason(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.record_decision(tid, 500, store_mod.STATUS_DECLINED, "known alt of Bob")
        rows = self.st.decisions_for(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "known alt of Bob")
        self.assertEqual(rows[0]["reviewer_id"], 500)

    def test_only_one_reviewer_can_decide_a_ticket(self):
        """Two reviewers pressing Approve at the same instant.

        The old read-then-write let both pass an `if status == open` check, both record a
        kit, and the applicant's cooldown get burned twice for one request. The status change
        is now a conditional UPDATE, so the second caller gets False.
        """
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.assertTrue(self.st.record_decision(tid, 500, store_mod.STATUS_APPROVED))
        self.assertFalse(self.st.record_decision(tid, 600, store_mod.STATUS_APPROVED))
        self.assertFalse(self.st.record_decision(tid, 600, store_mod.STATUS_DECLINED))
        # Exactly one decision on record, by the winner.
        rows = self.st.decisions_for(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reviewer_id"], 500)

    def test_concurrent_decisions_produce_exactly_one_winner(self):
        import concurrent.futures
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result(timeout=30) for f in [
                pool.submit(self.st.record_decision, tid, 500 + n,
                            store_mod.STATUS_APPROVED) for n in range(8)]]
        self.assertEqual(sum(1 for r in results if r), 1, "more than one reviewer won")
        self.assertEqual(len(self.st.decisions_for(tid)), 1)

    def test_kits_for_ticket_is_not_limited_by_recency(self):
        """Transcripts used to filter kit_history, which is capped and recency-ordered, so a
        heavy repeat applicant's older ticket reported no delivery."""
        target = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        kit = self.st.record_kit(GUILD, target, 100, "Alice", UUID_A)
        # Force it to be genuinely oldest. record_kit stamps time.time(), so 31 rows created
        # in the same second share a created_at and ORDER BY created_at DESC does not
        # actually bury anything -- the fixture has to be explicit or it proves nothing.
        self.st._db.execute("UPDATE kits SET created_at=1 WHERE id=?", (kit,))
        for _ in range(30):
            self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)

        self.assertNotIn(kit, [r["id"] for r in self.st.kit_history(GUILD, 
            discord_user_id=100, limit=10)], "fixture failed to bury the kit")
        found = self.st.kits_for_ticket(target)
        self.assertEqual([r["id"] for r in found], [kit])

    def test_unknown_outcome_is_rejected(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        with self.assertRaises(ValueError):
            self.st.record_decision(tid, 500, "maybe")

    def test_uuid_can_be_backfilled_after_a_late_resolution(self):
        tid = self.st.create_ticket(GUILD, 100, "alice", None)
        self.st.set_ticket_uuid(tid, UUID_A, "Alice")
        row = self.st.get_ticket(tid)
        self.assertEqual((row["mc_uuid"], row["mc_name"]), (UUID_A, "Alice"))

    # ----------------------------------------------------------------- cooldown

    def test_no_history_means_no_cooldown(self):
        self.assertFalse(self.st.cooldown(GUILD, 21, discord_user_id=100)["blocked"])

    def test_cooldown_blocks_on_the_discord_id_alone(self):
        """Same person, new Minecraft account."""
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        cd = self.st.cooldown(GUILD, 21, discord_user_id=100, mc_uuid=UUID_B)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["days_left"], 21)

    def test_cooldown_blocks_on_the_uuid_alone(self):
        """Same Minecraft account, new Discord account."""
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        cd = self.st.cooldown(GUILD, 21, discord_user_id=999, mc_uuid=UUID_A)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["matched"], "mc_uuid")

    def test_cooldown_clears_after_the_window(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        later = int(time.time()) + 21 * DAY + 1
        cd = self.st.cooldown(GUILD, 21, discord_user_id=100, now=later)
        self.assertFalse(cd["blocked"])
        self.assertEqual(cd["days_left"], 0)

    def test_days_left_rounds_up_so_it_never_understates(self):
        """'1 day left' must not actually mean 'come back in 40 minutes'."""
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        almost = int(time.time()) + 21 * DAY - 600      # ten minutes short
        cd = self.st.cooldown(GUILD, 21, discord_user_id=100, now=almost)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["days_left"], 1)

    def test_zero_cooldown_disables_the_check(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertFalse(self.st.cooldown(GUILD, 0, discord_user_id=100)["blocked"])

    def test_cooldown_with_no_identifier_is_not_blocked(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertFalse(self.st.cooldown(GUILD, 21)["blocked"])

    def test_unrelated_accounts_are_unaffected(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertFalse(
            self.st.cooldown(GUILD, 21, discord_user_id=200, mc_uuid=UUID_B)["blocked"])

    # --------------------------------------------------------------- dispatch

    def test_claim_is_atomic_and_the_second_presser_loses(self):
        """Two runners pressing Claim at the same moment is the normal case, not a rare
        one, so the database decides rather than callback ordering."""
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertTrue(self.st.claim_kit(kit, 700))
        self.assertFalse(self.st.claim_kit(kit, 800))
        self.assertEqual(self.st.get_kit(kit)["claimed_by"], 700)

    def test_unclaim_returns_it_to_the_pool(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertTrue(self.st.unclaim_kit(kit, 700))
        self.assertIsNone(self.st.get_kit(kit)["claimed_by"])
        self.assertTrue(self.st.claim_kit(kit, 800))

    def test_only_the_holder_can_unclaim(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertFalse(self.st.unclaim_kit(kit, 800))

    def test_delivery_is_idempotent(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertTrue(self.st.mark_delivered(kit))
        self.assertFalse(self.st.mark_delivered(kit))

    def test_reviewer_can_release_a_claim_held_by_someone_else(self):
        """A runner goes quiet holding a delivery. unclaim_kit deliberately refuses that, so
        there is a separate release_kit for the reviewer override."""
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertFalse(self.st.unclaim_kit(kit, 900))   # not the holder
        self.assertTrue(self.st.release_kit(kit))
        self.assertIsNone(self.st.get_kit(kit)["claimed_by"])
        self.assertTrue(self.st.claim_kit(kit, 900))

    def test_release_will_not_reopen_a_delivered_kit(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.st.mark_delivered(kit)
        self.assertFalse(self.st.release_kit(kit))

    def test_cancelling_a_ticket_frees_the_applicants_slot(self):
        """The lockout this exists to prevent: the panel pre-check counts OPEN tickets, so an
        undecided ticket with no way to close it bars the applicant from ever asking again."""
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.assertEqual(self.st.open_ticket_count(GUILD, 100), 1)
        self.st.record_decision(tid, 500, store_mod.STATUS_CANCELLED, "no reply in a week")
        self.assertEqual(self.st.open_ticket_count(GUILD, 100), 0)
        self.assertEqual(self.st.get_ticket(tid)["status"], store_mod.STATUS_CANCELLED)
        # ...and closing does NOT record a kit, so the cooldown is untouched.
        self.assertFalse(self.st.cooldown(GUILD, 21, discord_user_id=100)["blocked"])
        self.assertIsNotNone(self.st.create_ticket(GUILD, 100, "Alice", UUID_A))

    def test_delivered_kit_cannot_be_unclaimed(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.st.mark_delivered(kit)
        self.assertFalse(self.st.unclaim_kit(kit, 700))

    # ------------------------------------------------------------------- flags

    def test_flag_by_uuid_and_by_name(self):
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A, mc_name="Alice", note="alt of Bob")
        self.assertEqual(len(self.st.flags_for(GUILD, mc_uuid=UUID_A)), 1)
        self.assertEqual(len(self.st.flags_for(GUILD, mc_name="Alice")), 1)

    def test_flag_name_match_is_case_insensitive(self):
        self.st.set_flag(GUILD, "alt", 500, mc_name="Alice")
        self.assertEqual(len(self.st.flags_for(GUILD, mc_name="aLiCe")), 1)

    def test_cleared_flags_disappear(self):
        fid = self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A)
        self.assertTrue(self.st.clear_flag(GUILD, fid))
        self.assertEqual(self.st.flags_for(GUILD, mc_uuid=UUID_A), [])
        self.assertFalse(self.st.clear_flag(GUILD, fid))

    def test_flag_needs_an_identifier(self):
        with self.assertRaises(ValueError):
            self.st.set_flag(GUILD, "alt", 500)

    def test_flags_for_nothing_matches_nothing(self):
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A)
        self.assertEqual(self.st.flags_for(GUILD), [])

    # --------------------------------------------------------------- fan-out

    def test_linked_accounts_finds_other_names_on_one_discord_id(self):
        self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.create_ticket(GUILD, 100, "Alicia", UUID_B)
        names = {r["mc_name"] for r in self.st.linked_accounts(GUILD, discord_user_id=100)}
        self.assertEqual(names, {"Alice", "Alicia"})

    # -------------------------------------------------------- instrumentation

    def test_shown_chats_are_recorded_and_labellable(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.record_shown_chats(tid, [
            {"ts": "2026-07-25T00:00:00Z", "chat": "gg"},
            {"ts": "2026-07-25T00:01:00Z", "chat": "base at [coords]"},
        ])
        rows = self.st.shown_chats(tid)
        self.assertEqual([r["position"] for r in rows], [0, 1])
        self.assertTrue(self.st.flag_chat(tid, 1))
        self.assertEqual(self.st.shown_chats(tid)[1]["flagged"], 1)
        self.assertEqual(self.st.counts()["flagged_chats"], 1)

    def test_flagging_a_line_that_was_never_shown_fails_quietly(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.assertFalse(self.st.flag_chat(tid, 42))

    def test_counts_covers_every_table(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.record_decision(tid, 500, store_mod.STATUS_APPROVED)
        self.st.record_kit(GUILD, tid, 100, "Alice", UUID_A)
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A)
        counts = self.st.counts()
        self.assertEqual((counts["tickets"], counts["decisions"], counts["kits"],
                          counts["flags"]), (1, 1, 1, 1))

    # --------------------------------------------------- cross-guild isolation
    #
    # The whole point of multi-tenancy. Each server's records are its own.

    def test_a_kit_in_one_guild_does_not_start_a_cooldown_in_another(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertTrue(self.st.cooldown(GUILD, 21, discord_user_id=100)["blocked"])
        self.assertFalse(self.st.cooldown(GUILD_B, 21, discord_user_id=100)["blocked"])
        self.assertFalse(self.st.cooldown(GUILD_B, 21, mc_uuid=UUID_A)["blocked"])

    def test_flags_do_not_leak_between_guilds(self):
        self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A, mc_name="Alice")
        self.assertEqual(len(self.st.flags_for(GUILD, mc_uuid=UUID_A)), 1)
        self.assertEqual(self.st.flags_for(GUILD_B, mc_uuid=UUID_A), [])
        self.assertEqual(self.st.flags_for(GUILD_B, mc_name="Alice"), [])

    def test_one_guild_cannot_act_on_anothers_ticket_by_number(self):
        """The cross-tenant hole this design has to avoid.

        Row ids are globally unique, so a lookup by id alone succeeds from anywhere -- which
        would let `/close 5` in one server close a different server's ticket #5. Every
        user-supplied id is looked up with the caller's guild.
        """
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.assertIsNotNone(self.st.get_ticket(tid, GUILD))
        self.assertIsNone(self.st.get_ticket(tid, GUILD_B))
        # Unscoped still works, for internal callers holding a row they already trust.
        self.assertIsNotNone(self.st.get_ticket(tid))

    def test_one_guild_cannot_act_on_anothers_dispatch_by_number(self):
        kit = self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        self.assertIsNotNone(self.st.get_kit(kit, GUILD))
        self.assertIsNone(self.st.get_kit(kit, GUILD_B))

    def test_one_guild_cannot_clear_anothers_flag(self):
        fid = self.st.set_flag(GUILD, "alt", 500, mc_uuid=UUID_A)
        self.assertFalse(self.st.clear_flag(GUILD_B, fid))
        self.assertEqual(len(self.st.flags_for(GUILD, mc_uuid=UUID_A)), 1)
        self.assertTrue(self.st.clear_flag(GUILD, fid))

    def test_open_tickets_and_fan_out_are_scoped(self):
        self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.assertEqual(self.st.open_ticket_count(GUILD, 100), 1)
        self.assertEqual(self.st.open_ticket_count(GUILD_B, 100), 0)
        self.assertIsNone(self.st.open_ticket_for(GUILD_B, 100))
        self.assertEqual(self.st.linked_accounts(GUILD_B, discord_user_id=100), [])

    def test_counts_can_be_scoped_or_global(self):
        a = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.create_ticket(GUILD_B, 100, "Alice", UUID_A)
        self.st.record_kit(GUILD, a, 100, "Alice", UUID_A)
        self.st.record_shown_chats(a, [{"ts": None, "chat": "gg"}])
        self.assertEqual(self.st.counts(GUILD)["tickets"], 1)
        self.assertEqual(self.st.counts(GUILD)["kits"], 1)
        self.assertEqual(self.st.counts(GUILD)["shown_chats"], 1)
        self.assertEqual(self.st.counts(GUILD_B)["kits"], 0)
        self.assertEqual(self.st.counts(GUILD_B)["shown_chats"], 0)
        self.assertEqual(self.st.counts()["tickets"], 2)

    # ------------------------------------------------------------ guild config

    def test_guild_config_round_trips_and_is_isolated(self):
        self.st.set_guild_config(GUILD, {"panel_channel_id": 11, "cooldown_days": 7})
        self.assertEqual(self.st.get_guild_config(GUILD),
                         {"panel_channel_id": 11, "cooldown_days": 7})
        self.assertEqual(self.st.get_guild_config(GUILD_B), {})
        # Upsert, not insert-or-explode.
        self.st.set_guild_config(GUILD, {"panel_channel_id": 22})
        self.assertEqual(self.st.get_guild_config(GUILD)["panel_channel_id"], 22)
        self.assertEqual(self.st.get_guild_config(GUILD)["cooldown_days"], 7)
        self.assertEqual(self.st.configured_guilds(), [GUILD])
        self.assertEqual(self.st.clear_guild_config(GUILD), 2)
        self.assertEqual(self.st.get_guild_config(GUILD), {})

    def test_a_corrupt_config_row_does_not_take_the_guild_down(self):
        self.st.set_guild_config(GUILD, {"panel_channel_id": 11})
        self.st._db.execute(
            "INSERT INTO guild_config(guild_id,key,value,set_at) VALUES(?,?,?,0)",
            (GUILD, "broken", "{not json"))
        got = self.st.get_guild_config(GUILD)
        self.assertEqual(got["panel_channel_id"], 11)
        self.assertNotIn("broken", got)

    # -------------------------------------------------------------- durability

    def test_reopening_the_file_keeps_the_history(self):
        self.st.record_kit(GUILD, None, 100, "Alice", UUID_A)
        path = self.st.path
        self.st.close()
        again = store_mod.open_store(path)
        self.addCleanup(again.close)
        self.assertTrue(again.cooldown(GUILD, 21, discord_user_id=100)["blocked"])

    def test_queue_thread_round_trips(self):
        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)
        self.st.set_queue_thread(tid, 777)
        self.assertEqual(self.st.get_ticket(tid)["queue_thread_id"], 777)
        self.assertEqual(self.st.ticket_for_queue_thread(777)["id"], tid)

    def test_a_v1_ledger_gains_the_new_column_without_being_rebuilt(self):
        """CREATE TABLE IF NOT EXISTS leaves an existing table alone, so without an explicit
        migration a v1 database keeps v1 columns and every later query fails at runtime. The
        ledger is the one thing here that cannot be regenerated, so an upgrade must never
        require rebuilding it."""
        path = os.path.join(self.dir, "v1.sqlite3")
        # Build a v1-shaped tickets table: no queue_thread_id.
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version','1');
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL, mc_name TEXT NOT NULL, mc_uuid TEXT,
                thread_id INTEGER, status TEXT NOT NULL, note TEXT,
                created_at INTEGER NOT NULL, closed_at INTEGER);
            INSERT INTO tickets(discord_user_id, mc_name, status, created_at)
                VALUES (100, 'Alice', 'open', 1750000000);
        """)
        db.commit()
        db.close()

        st = store_mod.open_store(path)
        self.addCleanup(st.close)
        cols = {r[1] for r in st._db.execute("PRAGMA table_info(tickets)")}
        self.assertIn("queue_thread_id", cols)
        self.assertIn("guild_id", cols)
        self.assertEqual(
            st._db.execute("SELECT value FROM meta WHERE key='schema_version'")
            .fetchone()["value"], str(store_mod.SCHEMA_VERSION))

        # The row survived the migration...
        raw = st._db.execute("SELECT * FROM tickets").fetchall()
        self.assertEqual(len(raw), 1)
        self.assertIsNone(raw[0]["guild_id"])

        # ...but it belongs to no guild yet, so every scoped query rightly cannot see it.
        # THIS is the upgrade hazard: on a single-guild deployment it would look exactly
        # like the ledger having been wiped -- cooldowns reset, flags gone.
        self.assertIsNone(st.open_ticket_for(GUILD, 100))
        self.assertEqual(st.open_ticket_count(GUILD, 100), 0)

        # adopt_legacy_rows is what closes that gap, once, at startup.
        self.assertEqual(st.adopt_legacy_rows(GUILD), 1)
        row = st.open_ticket_for(GUILD, 100)
        self.assertIsNotNone(row)
        self.assertEqual(row["mc_name"], "Alice")
        self.assertIsNone(row["queue_thread_id"])

        # ...and it is adopted by that guild only.
        self.assertIsNone(st.open_ticket_for(GUILD_B, 100))

        st.set_queue_thread(int(row["id"]), 999)
        self.assertEqual(st.get_ticket(int(row["id"]))["queue_thread_id"], 999)

    def test_store_is_usable_from_a_worker_thread(self):
        """The bug that broke every button press in production.

        The bot runs blocking work -- the 2b2t requests, and the ledger reads inside
        card.gather -- through loop.run_in_executor, so the store really is touched from a
        thread other than the one that opened it. A single shared sqlite3 connection raises
        ProgrammingError("SQLite objects created in a thread can only be used in that same
        thread") the moment that happens, and the failure surfaced only in the real
        interaction path: a smoke test that called gather directly, on the main thread, was
        perfectly green.
        """
        import concurrent.futures

        tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A)

        def worker():
            # Exactly what card.gather touches.
            cd = self.st.cooldown(GUILD, 21, discord_user_id=100, mc_uuid=UUID_A)
            hist = self.st.kit_history(GUILD, discord_user_id=100, mc_uuid=UUID_A)
            flags = self.st.flags_for(GUILD, mc_uuid=UUID_A, mc_name="Alice")
            linked = self.st.linked_accounts(GUILD, discord_user_id=100, mc_uuid=UUID_A)
            # ...and a write, which is what record_shown_chats does after it.
            self.st.record_shown_chats(tid, [{"ts": None, "chat": "gg"}])
            return cd, hist, flags, linked

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            cd, hist, flags, linked = pool.submit(worker).result(timeout=20)

        self.assertFalse(cd["blocked"])
        self.assertEqual(hist, [])
        self.assertEqual(flags, [])
        self.assertEqual(len(linked), 1)
        # The write from the worker thread is visible from this one.
        self.assertEqual(len(self.st.shown_chats(tid)), 1)

    def test_concurrent_threads_can_both_write(self):
        """Two workers writing at once. WAL plus a per-connection busy timeout is what makes
        this safe; it is the shape of two reviews being built simultaneously."""
        import concurrent.futures

        def worker(n):
            tid = self.st.create_ticket(GUILD, 200 + n, "Player%d" % n, None)
            self.st.record_shown_chats(tid, [{"ts": None, "chat": "line"}])
            return tid

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = [f.result(timeout=30)
                   for f in [pool.submit(worker, n) for n in range(4)]]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(self.st.counts()["tickets"], 4)

    def test_a_future_schema_is_refused_rather_than_mangled(self):
        path = self.st.path
        self.st._db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        self.st.close()
        with self.assertRaises(RuntimeError) as ctx:
            store_mod.open_store(path)
        self.assertIn("newer version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
