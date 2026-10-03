import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from app import Database, DomainError, seed_demo


class SettlementLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.source = self.accounts["北区水库"]   # quota 1000, used 100 in March
        self.target = self.accounts["河口灌区"]   # quota 500

    def tearDown(self):
        self.tmp.cleanup()

    def _fresh_pair(self, quota_a=1000, quota_b=0):
        """Accounts in a neutral region (no impact rule) for concurrency tests."""
        a = self.db.create_account("alice", {"name": f"甲-{quota_a}-{quota_b}", "region": "neutral-a",
                                             "holder": "h", "priority": 1, "valid_from": "2026-01-01",
                                             "valid_to": "2026-12-31", "quota": quota_a}, "editor")
        b = self.db.create_account("alice", {"name": f"乙-{quota_a}-{quota_b}", "region": "neutral-b",
                                             "holder": "h", "priority": 2, "valid_from": "2026-01-01",
                                             "valid_to": "2026-12-31", "quota": quota_b}, "editor")
        return a["id"], b["id"]

    def test_pending_transfer_does_not_move_water_and_effective_date_does(self):
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": 400, "effective_date": "2026-06-01"}, "editor")
        # Before approval and before the effective date the water stays with
        # the transferor; the pending transfer is only shown as a reservation.
        before = self.db.available(self.source, "2026-05-31")
        self.assertAlmostEqual(before["available"], 900)
        self.assertAlmostEqual(before["pending_outgoing"], 0.0)
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # Approved but not yet effective: still the transferor's water.
        may = self.db.available(self.source, "2026-05-31")
        self.assertAlmostEqual(may["available"], 900)
        target_may = self.db.available(self.target, "2026-05-31")
        self.assertAlmostEqual(target_may["available"], 500)
        # Effective date: both sides move, same-day transfers settle first.
        june = self.db.available(self.source, "2026-06-01")
        self.assertAlmostEqual(june["settled_quota"], 600)
        self.assertAlmostEqual(june["available"], 500)
        target_june = self.db.available(self.target, "2026-06-01")
        self.assertAlmostEqual(target_june["settled_quota"], 900)
        self.assertAlmostEqual(target_june["available"], 900)

    def test_water_before_effective_date_still_belongs_to_transferor(self):
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": 300, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # 800 units can still be drawn in May; on May 31 the transfer has not settled.
        usage = self.db.record_usage(
            "meter-01", {"account_id": self.source, "amount": 800,
                         "meter_event_id": "PRE-DATE", "occurred_at": "2026-05-31"}, "meter")
        self.assertEqual(usage["amount"], 800)
        # After the effective date the transferred water is gone.
        with self.assertRaisesRegex(DomainError, "可用额度不足"):
            self.db.record_usage(
                "meter-01", {"account_id": self.source, "amount": 50,
                             "meter_event_id": "POST-DATE", "occurred_at": "2026-06-02"}, "meter")

    def test_usage_on_effective_date_clears_same_day_transfer_first(self):
        a, b = self._fresh_pair(quota_a=100)
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 60,
                      "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # 60 leaves first: the 60-unit usage would overflow the remaining 40.
        with self.assertRaises(DomainError) as caught:
            self.db.record_usage(
                "m", {"account_id": a, "amount": 60, "meter_event_id": "D-1",
                      "occurred_at": "2026-06-01"}, "meter")
        self.assertEqual(caught.exception.status, 409)
        conflicts = caught.exception.details["conflicts"]
        self.assertEqual(conflicts[0]["settlement_date"], "2026-06-01")
        # Original ledger untouched: the refused reading was never written.
        with self.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) c FROM usage_records WHERE meter_event_id='D-1'").fetchone()["c"]
        self.assertEqual(count, 0)
        ok = self.db.record_usage(
            "m", {"account_id": a, "amount": 40, "meter_event_id": "D-2",
                  "occurred_at": "2026-06-01"}, "meter")
        self.assertEqual(ok["amount"], 40)
        self.assertAlmostEqual(self.db.available(a, "2026-06-01")["available"], 0)

    def test_approval_racing_usage_exactly_one_wins_and_ledger_is_consistent(self):
        a, b = self._fresh_pair(quota_a=1000)
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 600,
                      "effective_date": "2026-09-01"}, "editor")
        barrier = threading.Barrier(2)
        outcomes = {}

        def approve():
            barrier.wait()
            try:
                self.db.approve_transfer(transfer["id"], "bob", "reviewer")
                outcomes["approve"] = "ok"
            except DomainError as exc:
                outcomes["approve"] = exc.status

        def draw():
            barrier.wait()
            try:
                self.db.record_usage(
                    "m", {"account_id": a, "amount": 600, "meter_event_id": "RACE-1",
                          "occurred_at": "2026-09-01"}, "meter")
                outcomes["usage"] = "ok"
            except DomainError as exc:
                outcomes["usage"] = exc.status

        t1, t2 = threading.Thread(target=approve), threading.Thread(target=draw)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertIn("ok", outcomes.values())
        self.assertIn(409, outcomes.values())
        # Whichever lost, the final availability is identical and no water
        # was double spent.
        self.assertAlmostEqual(self.db.available(a, "2026-09-01")["available"], 400)

    def test_cancel_not_yet_effective_is_recomputed_and_idempotent(self):
        a, b = self._fresh_pair(quota_a=1000, quota_b=500)
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 300,
                      "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        audit_before = len([x for x in self.db.audit() if x["action"] == "transfer.cancelled"])
        result = self.db.cancel_transfer(transfer["id"], "carol", "editor", as_of="2026-05-20")
        self.assertEqual(result["status"], "cancelled")
        # Recomputed sides return to their permit quotas.
        self.assertAlmostEqual(result["recomputed"]["source"]["available"], 1000)
        self.assertAlmostEqual(result["recomputed"]["target"]["available"], 500)
        # Idempotent: second cancel changes nothing and writes no new audit row.
        again = self.db.cancel_transfer(transfer["id"], "carol", "editor", as_of="2026-05-20")
        self.assertEqual(again["status"], "cancelled")
        audit_after = len([x for x in self.db.audit() if x["action"] == "transfer.cancelled"])
        self.assertEqual(audit_before + 1, audit_after)
        # A settled transfer cannot be cancelled.
        settled = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 10,
                      "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(settled["id"], "bob", "reviewer")
        with self.assertRaisesRegex(DomainError, "已到生效日"):
            self.db.cancel_transfer(settled["id"], "carol", "editor", as_of="2026-06-02")
        # A pending transfer can be cancelled at any time because it never settled.
        pending = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 10,
                      "effective_date": "2026-06-01"}, "editor")
        self.db.cancel_transfer(pending["id"], "carol", "editor")
        self.assertEqual(self.db.transfer_detail(pending["id"])["status"], "cancelled")

    def test_reschedule_recomputes_conflicts_and_can_be_retried(self):
        a, b = self._fresh_pair(quota_a=1000)
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 300,
                      "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # 850 drawn before the original effective date.
        self.db.record_usage(
            "m", {"account_id": a, "amount": 850, "meter_event_id": "EARLY",
                  "occurred_at": "2026-05-15"}, "meter")
        # Moving the transfer ahead of that usage breaks settlement -> rejected,
        # original transfer and usage stay exactly as they were.
        with self.assertRaisesRegex(DomainError, "重算取水失败"):
            self.db.reschedule_transfer(
                transfer["id"], "carol", {"effective_date": "2026-05-10"},
                "editor", as_of="2026-05-10")
        detail = self.db.transfer_detail(transfer["id"], "2026-05-20")
        self.assertEqual(detail["effective_date"], "2026-06-01")
        # Retrying with a feasible date succeeds; repeated submit does not
        # deduct water twice.
        moved = self.db.reschedule_transfer(
            transfer["id"], "carol", {"effective_date": "2026-06-15"},
            "editor", as_of="2026-05-20")
        self.assertEqual(moved["effective_date"], "2026-06-15")
        same = self.db.reschedule_transfer(
            transfer["id"], "carol", {"effective_date": "2026-06-15"},
            "editor", as_of="2026-05-20")
        self.assertEqual(same["effective_date"], "2026-06-15")
        snap = self.db.available(a, "2026-06-15")
        self.assertAlmostEqual(snap["settled_quota"], 700)
        self.assertAlmostEqual(snap["used"], 850)

    def test_accounts_transfers_and_drought_share_one_settlement_date(self):
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": 300, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        for as_of in ("2026-05-31", "2026-06-01"):
            accounts = {a["id"]: a for a in self.db.list_accounts(as_of)}
            detail = self.db.transfer_detail(transfer["id"], as_of)
            drought = self.db.simulate_drought(5000, 0, as_of)
            drought_map = {x["account_id"]: x for x in drought["allocations"]}
            for account_id, side in ((self.source, "source"), (self.target, "target")):
                self.assertEqual(accounts[account_id]["as_of"], as_of)
                self.assertEqual(detail[side]["available"], accounts[account_id]["available"])
                self.assertEqual(detail[side]["settled_quota"], accounts[account_id]["settled_quota"])
                self.assertEqual(drought_map[account_id]["settled_quota"],
                                 accounts[account_id]["settled_quota"])
                self.assertEqual(drought_map[account_id]["used"], accounts[account_id]["used"])
            self.assertEqual(drought["as_of"], as_of)
        detail_before = self.db.transfer_detail(transfer["id"], "2026-05-31")
        self.assertEqual(detail_before["settlement_state"], "approved_pending_effect")
        self.assertFalse(detail_before["settled"])
        detail_after = self.db.transfer_detail(transfer["id"], "2026-06-01")
        self.assertEqual(detail_after["settlement_state"], "settled")
        self.assertTrue(detail_after["settled"])

    def test_season_cap_uses_settled_quota_after_transfer(self):
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": 400, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # July cap is 35% of the settled 600 quota = 210.
        with self.assertRaisesRegex(DomainError, "季节上限"):
            self.db.record_usage(
                "m", {"account_id": self.source, "amount": 250, "meter_event_id": "JULY-BIG",
                      "occurred_at": "2026-07-10"}, "meter")
        self.db.record_usage(
            "m", {"account_id": self.source, "amount": 200, "meter_event_id": "JULY-OK",
                  "occurred_at": "2026-07-11"}, "meter")

    def test_duplicate_meter_event_rejected_without_double_charge(self):
        self.db.record_usage(
            "meter-01", {"account_id": self.source, "amount": 10, "meter_event_id": "M-1",
                         "occurred_at": "2026-08-01"}, "meter")
        used_once = self.db.available(self.source, "2026-08-01")["used"]
        with self.assertRaisesRegex(DomainError, "不能重复计水"):
            self.db.record_usage(
                "meter-01", {"account_id": self.source, "amount": 10, "meter_event_id": "M-1",
                             "occurred_at": "2026-08-01"}, "meter")
        self.assertAlmostEqual(self.db.available(self.source, "2026-08-01")["used"], used_once)

    def test_third_party_minimum_retention_and_self_approval(self):
        with self.assertRaisesRegex(DomainError, "最小留存"):
            self.db.create_transfer(
                "alice", {"from_account_id": self.source, "to_account_id": self.target,
                          "amount": 501, "effective_date": "2026-06-01"}, "editor")
        transfer = self.db.create_transfer(
            "alice", {"from_account_id": self.source, "to_account_id": self.target,
                      "amount": 100, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            self.db.approve_transfer(transfer["id"], "alice", "reviewer")

    def test_pending_transfer_feasibility_blocks_double_selling(self):
        # Two 800-unit pending transfers (900 free) cannot both promise the
        # same water; the second is refused even before approval. Neutral
        # regions are used so the downstream retention rule is out of scope.
        a, b = self._fresh_pair(quota_a=1000, quota_b=500)
        c = self.db.create_account("alice", {"name": "丙账户", "region": "neutral-c",
                                             "holder": "h", "priority": 2, "valid_from": "2026-01-01",
                                             "valid_to": "2026-12-31", "quota": 100}, "editor")
        self.db.create_transfer(
            "alice", {"from_account_id": a, "to_account_id": b, "amount": 800,
                      "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "既有取水结算失败"):
            self.db.create_transfer(
                "alice", {"from_account_id": a, "to_account_id": c["id"], "amount": 800,
                          "effective_date": "2026-06-01"}, "editor")
        # The first one is unaffected and still shows as a reservation.
        self.assertAlmostEqual(self.db.available(a, "2026-06-01")["pending_outgoing"], 800)

    def test_today_default_as_of(self):
        self.assertEqual(self.db.available(self.source)["as_of"], date.today().isoformat())
        self.assertEqual(self.db.list_accounts()[0]["as_of"], date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
