"""The delivery claim log.

`kits.claimed_by` is current state and is nulled the moment a runner hands back, so it can
only ever answer "who holds this now". The question that matters after the fact is "who has
had this", and a kit that passed through three people and never arrived is exactly the one
worth being able to read about. These tests are mostly about the handoffs, because the
single-claimer case was already covered by the column this replaces.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import store as store_mod

GUILD = 4242
UUID_A = "00000000-1111-2222-3333-4444444444aa"
RUNNER_A = 7001
RUNNER_B = 7002
REVIEWER = 500


class ClaimLogCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="melonkit-claims-")
        self.st = store_mod.open_store(os.path.join(self.dir, "t.sqlite3"))
        self.addCleanup(self._teardown)
        self.tid = self.st.create_ticket(GUILD, 100, "Alice", UUID_A, "lost everything")
        self.kit = self.st.record_kit(GUILD, self.tid, 100, "Alice", UUID_A)

    def _teardown(self):
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _claims(self):
        return self.st.claims_for_kit(self.kit)

    # ------------------------------------------------------------------- basics

    def test_claiming_records_the_claimer(self):
        self.assertTrue(self.st.claim_kit(self.kit, RUNNER_A))
        rows = self._claims()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_id"], RUNNER_A)
        self.assertIsNone(rows[0]["released_at"])
        self.assertIsNone(rows[0]["outcome"])

    def test_losing_the_race_records_nothing(self):
        """The claim and its log row are one transaction, so a loser leaves no trace."""
        self.assertTrue(self.st.claim_kit(self.kit, RUNNER_A))
        self.assertFalse(self.st.claim_kit(self.kit, RUNNER_B))
        rows = self._claims()
        self.assertEqual([r["actor_id"] for r in rows], [RUNNER_A])

    # ---------------------------------------------------------------- handoffs

    def test_hand_back_keeps_the_runner_in_the_log(self):
        """The whole point: `claimed_by` goes back to NULL, the history does not."""
        self.st.claim_kit(self.kit, RUNNER_A)
        self.assertTrue(self.st.unclaim_kit(self.kit, RUNNER_A))

        self.assertIsNone(self.st.get_kit(self.kit)["claimed_by"])
        rows = self._claims()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_id"], RUNNER_A)
        self.assertEqual(rows[0]["outcome"], store_mod.CLAIM_HANDED_BACK)
        self.assertEqual(rows[0]["released_by"], RUNNER_A)
        self.assertIsNotNone(rows[0]["released_at"])

    def test_every_hand_it_passed_through_is_kept(self):
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.unclaim_kit(self.kit, RUNNER_A)
        self.st.claim_kit(self.kit, RUNNER_B)
        self.st.unclaim_kit(self.kit, RUNNER_B)
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.mark_delivered(self.kit)

        rows = self._claims()
        self.assertEqual([r["actor_id"] for r in rows], [RUNNER_A, RUNNER_B, RUNNER_A])
        self.assertEqual([r["outcome"] for r in rows],
                         [store_mod.CLAIM_HANDED_BACK, store_mod.CLAIM_HANDED_BACK,
                          store_mod.CLAIM_DELIVERED])

    def test_release_records_who_took_it_off_them(self):
        """A reviewer prising a stale claim off someone reads differently from a hand-back,
        and that difference is the only signal distinguishing unreliable from unavailable."""
        self.st.claim_kit(self.kit, RUNNER_A)
        self.assertTrue(self.st.release_kit(self.kit, REVIEWER))
        row = self._claims()[0]
        self.assertEqual(row["outcome"], store_mod.CLAIM_RELEASED)
        self.assertEqual(row["actor_id"], RUNNER_A)
        self.assertEqual(row["released_by"], REVIEWER)

    def test_hand_back_by_someone_else_is_refused_and_logs_nothing(self):
        self.st.claim_kit(self.kit, RUNNER_A)
        self.assertFalse(self.st.unclaim_kit(self.kit, RUNNER_B))
        row = self._claims()[0]
        self.assertIsNone(row["released_at"])
        self.assertEqual(self.st.get_kit(self.kit)["claimed_by"], RUNNER_A)

    # --------------------------------------------------------------- delivery

    def test_delivery_closes_the_open_claim(self):
        self.st.claim_kit(self.kit, RUNNER_A)
        self.assertTrue(self.st.mark_delivered(self.kit))
        row = self._claims()[0]
        self.assertEqual(row["outcome"], store_mod.CLAIM_DELIVERED)
        self.assertIsNotNone(row["released_at"])
        self.assertIsNone(row["released_by"])

    def test_second_delivery_is_refused(self):
        self.st.claim_kit(self.kit, RUNNER_A)
        self.assertTrue(self.st.mark_delivered(self.kit))
        self.assertFalse(self.st.mark_delivered(self.kit))
        self.assertEqual(len(self._claims()), 1)

    def test_hand_back_after_delivery_is_refused(self):
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.mark_delivered(self.kit)
        self.assertFalse(self.st.unclaim_kit(self.kit, RUNNER_A))
        self.assertFalse(self.st.release_kit(self.kit, REVIEWER))
        self.assertEqual(self._claims()[0]["outcome"], store_mod.CLAIM_DELIVERED)

    def test_release_of_an_unclaimed_kit_changes_nothing(self):
        self.assertFalse(self.st.release_kit(self.kit, REVIEWER))
        self.assertEqual(self._claims(), [])

    # ------------------------------------------------- the hand-back/deliver race

    def test_a_delivered_kit_cannot_be_reclaimed(self):
        """The bricking sequence, in order.

        A hand-back landing in the same instant as a delivery leaves a kit both delivered and
        unclaimed. Without the `delivered_at IS NULL` guard on claiming, the next person to
        press Claim takes a kit that has already gone out -- and their claim row can never be
        closed, because every path that closes one refuses a delivered kit. They hold it in
        the ledger forever.
        """
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.release_kit(self.kit, REVIEWER)      # the hand-back wins the race
        self.assertTrue(self.st.mark_delivered(self.kit, RUNNER_A))

        self.assertFalse(self.st.claim_kit(self.kit, RUNNER_B))
        self.assertIsNone(self.st.get_kit(self.kit)["claimed_by"])
        self.assertNotIn(RUNNER_B, [r["actor_id"] for r in self._claims()])

    def test_a_delivery_with_no_open_claim_still_names_somebody(self):
        """A delivered kit whose history says nobody ever had it is worse than approximate."""
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.release_kit(self.kit, REVIEWER)
        self.st.mark_delivered(self.kit, RUNNER_B)

        delivered = [r for r in self._claims()
                     if r["outcome"] == store_mod.CLAIM_DELIVERED]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["actor_id"], RUNNER_B)

    def test_no_open_claim_and_no_presser_records_nothing_invented(self):
        self.assertTrue(self.st.mark_delivered(self.kit))
        self.assertEqual(self._claims(), [])

    # ---------------------------------------------------------------- legacy

    def test_a_kit_claimed_before_the_log_existed_still_delivers(self):
        """Upgrades must not strand live work.

        A dispatch claimed by the previous build has no open claim row, and a delivery that
        insisted on closing one would fail for every kit already in flight at upgrade time.
        """
        self.st._db.execute("UPDATE kits SET claimed_by=?, claimed_at=1 WHERE id=?",
                            (RUNNER_A, self.kit))
        self.assertEqual(self._claims(), [])
        self.assertTrue(self.st.mark_delivered(self.kit, RUNNER_A))
        self.assertIsNotNone(self.st.get_kit(self.kit)["delivered_at"])
        # ...and the history says who, rather than the embed naming a runner the attachment
        # claims never existed. Two halves of one permanent record must not disagree.
        rows = self._claims()
        self.assertEqual([r["actor_id"] for r in rows], [RUNNER_A])
        self.assertEqual(rows[0]["outcome"], store_mod.CLAIM_DELIVERED)

    def test_upgrade_backfills_claims_in_flight(self):
        """A dispatch somebody is holding as the upgrade lands keeps its claimer.

        The migration runs on every open, so this simulates the pre-v6 state and re-opens.
        """
        self.st._db.execute("UPDATE kits SET claimed_by=?, claimed_at=1234 WHERE id=?",
                            (RUNNER_A, self.kit))
        self.st._db.execute("DELETE FROM kit_claims")
        path = self.st.path
        self.st.close()
        self.st = store_mod.open_store(path)

        rows = self._claims()
        self.assertEqual([r["actor_id"] for r in rows], [RUNNER_A])
        self.assertEqual(rows[0]["claimed_at"], 1234)
        self.assertIsNone(rows[0]["released_at"])

    def test_backfill_leaves_delivered_kits_alone(self):
        """Their history is closed; inventing a row for one would be a guess, not a record."""
        self.st._db.execute(
            "UPDATE kits SET claimed_by=?, claimed_at=1, delivered_at=2 WHERE id=?",
            (RUNNER_A, self.kit))
        self.st._db.execute("DELETE FROM kit_claims")
        path = self.st.path
        self.st.close()
        self.st = store_mod.open_store(path)
        self.assertEqual(self._claims(), [])

    # --------------------------------------------------------------- by ticket

    def test_claims_for_ticket_spans_every_dispatch(self):
        second = self.st.record_kit(GUILD, self.tid, 100, "Alice", UUID_A)
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.claim_kit(second, RUNNER_B)
        rows = self.st.claims_for_ticket(self.tid)
        self.assertEqual([r["actor_id"] for r in rows], [RUNNER_A, RUNNER_B])

    def test_claims_for_ticket_ignores_other_tickets(self):
        other_t = self.st.create_ticket(GUILD, 200, "Bob", None)
        other_k = self.st.record_kit(GUILD, other_t, 200, "Bob", None)
        self.st.claim_kit(self.kit, RUNNER_A)
        self.st.claim_kit(other_k, RUNNER_B)
        self.assertEqual([r["actor_id"] for r in self.st.claims_for_ticket(self.tid)],
                         [RUNNER_A])


if __name__ == "__main__":
    unittest.main()
