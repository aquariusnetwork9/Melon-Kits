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
        tid = self.st.create_ticket(100, "Alice", UUID_A, "lost everything")
        self.assertEqual(self.st.open_ticket_count(100), 1)
        self.assertEqual(self.st.open_ticket_for(100)["mc_name"], "Alice")
        self.st.set_ticket_thread(tid, 999)
        self.assertEqual(self.st.ticket_for_thread(999)["id"], tid)

        self.st.record_decision(tid, 500, store_mod.STATUS_APPROVED)
        self.assertEqual(self.st.open_ticket_count(100), 0)
        self.assertEqual(self.st.get_ticket(tid)["status"], store_mod.STATUS_APPROVED)
        self.assertIsNotNone(self.st.get_ticket(tid)["closed_at"])

    def test_decision_records_the_reason(self):
        tid = self.st.create_ticket(100, "Alice", UUID_A)
        self.st.record_decision(tid, 500, store_mod.STATUS_DECLINED, "known alt of Bob")
        rows = self.st.decisions_for(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "known alt of Bob")
        self.assertEqual(rows[0]["reviewer_id"], 500)

    def test_unknown_outcome_is_rejected(self):
        tid = self.st.create_ticket(100, "Alice", UUID_A)
        with self.assertRaises(ValueError):
            self.st.record_decision(tid, 500, "maybe")

    def test_uuid_can_be_backfilled_after_a_late_resolution(self):
        tid = self.st.create_ticket(100, "alice", None)
        self.st.set_ticket_uuid(tid, UUID_A, "Alice")
        row = self.st.get_ticket(tid)
        self.assertEqual((row["mc_uuid"], row["mc_name"]), (UUID_A, "Alice"))

    # ----------------------------------------------------------------- cooldown

    def test_no_history_means_no_cooldown(self):
        self.assertFalse(self.st.cooldown(21, discord_user_id=100)["blocked"])

    def test_cooldown_blocks_on_the_discord_id_alone(self):
        """Same person, new Minecraft account."""
        self.st.record_kit(None, 100, "Alice", UUID_A)
        cd = self.st.cooldown(21, discord_user_id=100, mc_uuid=UUID_B)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["days_left"], 21)

    def test_cooldown_blocks_on_the_uuid_alone(self):
        """Same Minecraft account, new Discord account."""
        self.st.record_kit(None, 100, "Alice", UUID_A)
        cd = self.st.cooldown(21, discord_user_id=999, mc_uuid=UUID_A)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["matched"], "mc_uuid")

    def test_cooldown_clears_after_the_window(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        later = int(time.time()) + 21 * DAY + 1
        cd = self.st.cooldown(21, discord_user_id=100, now=later)
        self.assertFalse(cd["blocked"])
        self.assertEqual(cd["days_left"], 0)

    def test_days_left_rounds_up_so_it_never_understates(self):
        """'1 day left' must not actually mean 'come back in 40 minutes'."""
        self.st.record_kit(None, 100, "Alice", UUID_A)
        almost = int(time.time()) + 21 * DAY - 600      # ten minutes short
        cd = self.st.cooldown(21, discord_user_id=100, now=almost)
        self.assertTrue(cd["blocked"])
        self.assertEqual(cd["days_left"], 1)

    def test_zero_cooldown_disables_the_check(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        self.assertFalse(self.st.cooldown(0, discord_user_id=100)["blocked"])

    def test_cooldown_with_no_identifier_is_not_blocked(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        self.assertFalse(self.st.cooldown(21)["blocked"])

    def test_unrelated_accounts_are_unaffected(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        self.assertFalse(
            self.st.cooldown(21, discord_user_id=200, mc_uuid=UUID_B)["blocked"])

    # --------------------------------------------------------------- dispatch

    def test_claim_is_atomic_and_the_second_presser_loses(self):
        """Two runners pressing Claim at the same moment is the normal case, not a rare
        one, so the database decides rather than callback ordering."""
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.assertTrue(self.st.claim_kit(kit, 700))
        self.assertFalse(self.st.claim_kit(kit, 800))
        self.assertEqual(self.st.get_kit(kit)["claimed_by"], 700)

    def test_unclaim_returns_it_to_the_pool(self):
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertTrue(self.st.unclaim_kit(kit, 700))
        self.assertIsNone(self.st.get_kit(kit)["claimed_by"])
        self.assertTrue(self.st.claim_kit(kit, 800))

    def test_only_the_holder_can_unclaim(self):
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertFalse(self.st.unclaim_kit(kit, 800))

    def test_delivery_is_idempotent(self):
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.assertTrue(self.st.mark_delivered(kit))
        self.assertFalse(self.st.mark_delivered(kit))

    def test_reviewer_can_release_a_claim_held_by_someone_else(self):
        """A runner goes quiet holding a delivery. unclaim_kit deliberately refuses that, so
        there is a separate release_kit for the reviewer override."""
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.assertFalse(self.st.unclaim_kit(kit, 900))   # not the holder
        self.assertTrue(self.st.release_kit(kit))
        self.assertIsNone(self.st.get_kit(kit)["claimed_by"])
        self.assertTrue(self.st.claim_kit(kit, 900))

    def test_release_will_not_reopen_a_delivered_kit(self):
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.st.mark_delivered(kit)
        self.assertFalse(self.st.release_kit(kit))

    def test_cancelling_a_ticket_frees_the_applicants_slot(self):
        """The lockout this exists to prevent: the panel pre-check counts OPEN tickets, so an
        undecided ticket with no way to close it bars the applicant from ever asking again."""
        tid = self.st.create_ticket(100, "Alice", UUID_A)
        self.assertEqual(self.st.open_ticket_count(100), 1)
        self.st.record_decision(tid, 500, store_mod.STATUS_CANCELLED, "no reply in a week")
        self.assertEqual(self.st.open_ticket_count(100), 0)
        self.assertEqual(self.st.get_ticket(tid)["status"], store_mod.STATUS_CANCELLED)
        # ...and closing does NOT record a kit, so the cooldown is untouched.
        self.assertFalse(self.st.cooldown(21, discord_user_id=100)["blocked"])
        self.assertIsNotNone(self.st.create_ticket(100, "Alice", UUID_A))

    def test_delivered_kit_cannot_be_unclaimed(self):
        kit = self.st.record_kit(None, 100, "Alice", UUID_A)
        self.st.claim_kit(kit, 700)
        self.st.mark_delivered(kit)
        self.assertFalse(self.st.unclaim_kit(kit, 700))

    # ------------------------------------------------------------------- flags

    def test_flag_by_uuid_and_by_name(self):
        self.st.set_flag("alt", 500, mc_uuid=UUID_A, mc_name="Alice", note="alt of Bob")
        self.assertEqual(len(self.st.flags_for(mc_uuid=UUID_A)), 1)
        self.assertEqual(len(self.st.flags_for(mc_name="Alice")), 1)

    def test_flag_name_match_is_case_insensitive(self):
        self.st.set_flag("alt", 500, mc_name="Alice")
        self.assertEqual(len(self.st.flags_for(mc_name="aLiCe")), 1)

    def test_cleared_flags_disappear(self):
        fid = self.st.set_flag("alt", 500, mc_uuid=UUID_A)
        self.assertTrue(self.st.clear_flag(fid))
        self.assertEqual(self.st.flags_for(mc_uuid=UUID_A), [])
        self.assertFalse(self.st.clear_flag(fid))

    def test_flag_needs_an_identifier(self):
        with self.assertRaises(ValueError):
            self.st.set_flag("alt", 500)

    def test_flags_for_nothing_matches_nothing(self):
        self.st.set_flag("alt", 500, mc_uuid=UUID_A)
        self.assertEqual(self.st.flags_for(), [])

    # --------------------------------------------------------------- fan-out

    def test_linked_accounts_finds_other_names_on_one_discord_id(self):
        self.st.create_ticket(100, "Alice", UUID_A)
        self.st.create_ticket(100, "Alicia", UUID_B)
        names = {r["mc_name"] for r in self.st.linked_accounts(discord_user_id=100)}
        self.assertEqual(names, {"Alice", "Alicia"})

    # -------------------------------------------------------- instrumentation

    def test_shown_chats_are_recorded_and_labellable(self):
        tid = self.st.create_ticket(100, "Alice", UUID_A)
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
        tid = self.st.create_ticket(100, "Alice", UUID_A)
        self.assertFalse(self.st.flag_chat(tid, 42))

    def test_counts_covers_every_table(self):
        tid = self.st.create_ticket(100, "Alice", UUID_A)
        self.st.record_decision(tid, 500, store_mod.STATUS_APPROVED)
        self.st.record_kit(tid, 100, "Alice", UUID_A)
        self.st.set_flag("alt", 500, mc_uuid=UUID_A)
        counts = self.st.counts()
        self.assertEqual((counts["tickets"], counts["decisions"], counts["kits"],
                          counts["flags"]), (1, 1, 1, 1))

    # -------------------------------------------------------------- durability

    def test_reopening_the_file_keeps_the_history(self):
        self.st.record_kit(None, 100, "Alice", UUID_A)
        path = self.st.path
        self.st.close()
        again = store_mod.open_store(path)
        self.addCleanup(again.close)
        self.assertTrue(again.cooldown(21, discord_user_id=100)["blocked"])

    def test_queue_thread_round_trips(self):
        tid = self.st.create_ticket(100, "Alice", UUID_A)
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
        # The pre-existing row survived, and the version was moved forward.
        row = st.open_ticket_for(100)
        self.assertEqual(row["mc_name"], "Alice")
        self.assertIsNone(row["queue_thread_id"])
        self.assertEqual(
            st._db.execute("SELECT value FROM meta WHERE key='schema_version'")
            .fetchone()["value"], str(store_mod.SCHEMA_VERSION))
        # ...and the new column is usable straight away.
        st.set_queue_thread(int(row["id"]), 999)
        self.assertEqual(st.get_ticket(int(row["id"]))["queue_thread_id"], 999)

    def test_a_future_schema_is_refused_rather_than_mangled(self):
        path = self.st.path
        self.st._db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        self.st.close()
        with self.assertRaises(RuntimeError) as ctx:
            store_mod.open_store(path)
        self.assertIn("newer version", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
