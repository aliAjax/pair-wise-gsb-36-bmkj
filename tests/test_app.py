import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class WaterRightsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.source = self.accounts["北区水库"]   # quota 1000, priority 1, upstream
        self.target = self.accounts["河口灌区"]   # quota 500,  priority 2, downstream
        # Seed already books 100 usage on 2026-03-01 for the source.

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers --------------------------------------------------------

    def avail(self, account_id, as_of):
        return self.db.available(account_id, as_of)

    def make_future_accounts(self):
        """Accounts valid out to 2030 so transfers stay before their effective day."""
        src = self.db.create_account("alice", {
            "name": "未来上游", "region": "upstream", "holder": "甲", "priority": 1,
            "valid_from": "2030-01-01", "valid_to": "2030-12-31", "quota": 1000}, "editor")
        dst = self.db.create_account("alice", {
            "name": "未来下游", "region": "downstream", "holder": "乙", "priority": 2,
            "valid_from": "2030-01-01", "valid_to": "2030-12-31", "quota": 500}, "editor")
        return src["id"], dst["id"]

    # -- settlement-date availability ----------------------------------

    def test_pending_and_future_transfer_keeps_water_with_transferor(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 400, "effective_date": "2026-06-01"}, "editor")
        # Pending: nothing is reserved against the source at any settlement date.
        self.assertEqual(self.avail(self.source, "2026-05-31")["available"], 900)
        self.assertEqual(self.avail(self.source, "2026-05-31")["future_outgoing"], 0)
        approved = self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")
        # Approved but not yet effective: water still belongs to the source.
        self.assertEqual(self.avail(self.source, "2026-05-31")["available"], 900)
        self.assertEqual(self.avail(self.target, "2026-05-31")["available"], 500)
        self.assertEqual(self.avail(self.source, "2026-05-31")["future_outgoing"], 400)
        self.assertEqual(self.avail(self.target, "2026-05-31")["future_incoming"], 400)
        # Effective date: the transfer enters both ledgers (transfers settle first).
        self.assertEqual(self.avail(self.source, "2026-06-01")["available"], 500)
        self.assertEqual(self.avail(self.target, "2026-06-01")["available"], 900)
        self.assertEqual(self.avail(self.source, "2026-06-01")["settled_outgoing"], 400)

    def test_usage_before_effective_date_belongs_to_transferor(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 200, "effective_date": "2026-09-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # The source may run its balance down right up to the effective date.
        usage = self.db.record_usage("meter-01", {
            "account_id": self.source, "amount": 800,
            "meter_event_id": "UP-AUG-1", "occurred_at": "2026-08-31"}, "meter")
        self.assertEqual(usage["amount"], 800)
        self.assertEqual(self.avail(self.source, "2026-08-31")["available"], 100)
        # On the effective day the 200 still moves even though the source is
        # now overdrawn; the target's ledger gains the water as scheduled.
        self.assertEqual(self.avail(self.source, "2026-09-01")["available"], 0)
        self.assertEqual(self.avail(self.target, "2026-09-01")["available"], 700)

    # -- same-day settlement ordering ----------------------------------

    def test_same_day_transfer_settles_before_usage(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 200, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # Source: settled quota 800, 100 already used -> 450 fits.
        self.db.record_usage("m", {
            "account_id": self.source, "amount": 450,
            "meter_event_id": "S-1", "occurred_at": "2026-06-01"}, "meter")
        # Target: settled quota 700, 600 fits, 101 more would not.
        self.db.record_usage("m", {
            "account_id": self.target, "amount": 600,
            "meter_event_id": "T-1", "occurred_at": "2026-06-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "可用额度不足"):
            self.db.record_usage("m", {
                "account_id": self.target, "amount": 101,
                "meter_event_id": "T-2", "occurred_at": "2026-06-01"}, "meter")

    def test_effective_day_usage_failure_keeps_original_ledger(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 500, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # Source settled quota on 6/1 is 500 with 100 already used -> only 400 left.
        with self.assertRaisesRegex(DomainError, "先结清同日转让"):
            self.db.record_usage("m", {
                "account_id": self.source, "amount": 450,
                "meter_event_id": "S-BAD", "occurred_at": "2026-06-01"}, "meter")
        # Nothing was written: availability and the usage table are unchanged.
        self.assertEqual(self.avail(self.source, "2026-06-01")["available"], 400)
        with self.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) c FROM usage_records").fetchone()["c"]
        self.assertEqual(count, 1)  # only the seeded March record remains
        # The failed meter event can be resubmitted with a feasible amount.
        ok = self.db.record_usage("m", {
            "account_id": self.source, "amount": 400,
            "meter_event_id": "S-BAD", "occurred_at": "2026-06-01"}, "meter")
        self.assertEqual(ok["amount"], 400)
        self.assertEqual(self.avail(self.source, "2026-06-01")["available"], 0)

    def test_approval_and_meter_on_same_day_one_loses_ledger_untouched(self):
        # Whichever request is processed first wins; the lagging transaction
        # receives 409 and the committed ledger is never rewritten.
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 250, "effective_date": "2026-10-15"}, "editor")
        outcomes = {"approved": False, "usage": False, "errors": []}
        barrier = threading.Barrier(2)

        def approve():
            try:
                barrier.wait()
                self.db.approve_transfer(transfer["id"], "bob", "reviewer")
                outcomes["approved"] = True
            except DomainError as exc:
                outcomes["errors"].append(str(exc))

        def meter():
            try:
                barrier.wait()
                self.db.record_usage("m", {
                    "account_id": self.source, "amount": 700,
                    "meter_event_id": "S-RACE", "occurred_at": "2026-10-15"}, "meter")
                outcomes["usage"] = True
            except DomainError as exc:
                outcomes["errors"].append(str(exc))

        t1, t2 = threading.Thread(target=approve), threading.Thread(target=meter)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(1, sum([outcomes["approved"], outcomes["usage"]]))
        self.assertEqual(1, len(outcomes["errors"]))
        with self.db.connect() as conn:
            used = conn.execute(
                "SELECT COALESCE(SUM(amount),0) t FROM usage_records WHERE occurred_at='2026-10-15'"
            ).fetchone()["t"]
            status = conn.execute("SELECT status FROM transfers WHERE id=?", (transfer["id"],)).fetchone()["status"]
        if outcomes["approved"]:
            self.assertEqual(status, "approved")
            self.assertEqual(used, 0)
        else:
            self.assertEqual(status, "pending")
            self.assertEqual(used, 700)

    # -- cancellation and rescheduling ---------------------------------

    def test_cancel_pending_transfer_changes_nothing_and_is_idempotent(self):
        src, dst = self.make_future_accounts()
        transfer = self.db.create_transfer("alice", {
            "from_account_id": src, "to_account_id": dst,
            "amount": 300, "effective_date": "2030-06-01"}, "editor")
        result = self.db.cancel_transfer(transfer["id"], "alice", "editor")
        self.assertEqual(result["transfer"]["status"], "cancelled")
        self.assertTrue(result["recompute"]["ok"])
        self.assertEqual(self.avail(src, "2030-06-01")["available"], 1000)
        self.assertEqual(self.avail(dst, "2030-06-01")["available"], 500)
        with self.assertRaisesRegex(DomainError, "已终结"):
            self.db.cancel_transfer(transfer["id"], "alice", "editor")

    def test_cancel_effective_transfer_replays_target_and_preserves_entries(self):
        src, dst = self.make_future_accounts()
        transfer = self.db.create_transfer("alice", {
            "from_account_id": src, "to_account_id": dst,
            "amount": 300, "effective_date": "2030-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # 700 only fits while the incoming 300 is settled on the target.
        self.db.record_usage("m", {
            "account_id": dst, "amount": 700,
            "meter_event_id": "F-1", "occurred_at": "2030-06-02"}, "meter")
        result = self.db.cancel_transfer(transfer["id"], "alice", "editor")
        self.assertEqual(result["transfer"]["status"], "cancelled")
        self.assertFalse(result["recompute"]["ok"])
        target_report = next(a for a in result["recompute"]["accounts"] if a["account_id"] == dst)
        conflict = target_report["conflicts"][0]
        self.assertEqual(conflict["meter_event_id"], "F-1")
        self.assertAlmostEqual(conflict["shortfall"], 200)
        # Original usage entry is preserved; replay can be retried safely.
        with self.db.connect() as conn:
            rows = conn.execute("SELECT COUNT(*) c FROM usage_records WHERE meter_event_id='F-1'").fetchone()["c"]
        self.assertEqual(rows, 1)
        retry = self.db.recompute("alice", dst, "2030-06-01")
        self.assertFalse(retry["ok"])
        self.assertEqual(len(retry["accounts"][0]["conflicts"]), 1)
        with self.db.connect() as conn:
            rows = conn.execute("SELECT COUNT(*) c FROM usage_records WHERE meter_event_id='F-1'").fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_reschedule_recalculates_window_and_repeat_never_double_deducts(self):
        src, dst = self.make_future_accounts()
        transfer = self.db.create_transfer("alice", {
            "from_account_id": src, "to_account_id": dst,
            "amount": 300, "effective_date": "2030-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        self.db.record_usage("m", {
            "account_id": dst, "amount": 700,
            "meter_event_id": "F-1", "occurred_at": "2030-06-02"}, "meter")
        # Move the transfer away: the June usage loses its backing water.
        moved = self.db.reschedule_transfer(transfer["id"], "alice",
                                            {"effective_date": "2030-09-01"}, "editor")
        self.assertFalse(moved["recompute"]["ok"])
        self.assertEqual(moved["transfer"]["effective_date"], "2030-09-01")
        # Same-date resubmission changes nothing, so it never deducts water
        # twice; the June conflict is still discoverable via a recompute retry.
        again = self.db.reschedule_transfer(transfer["id"], "alice",
                                            {"effective_date": "2030-09-01"}, "editor")
        retry = self.db.recompute("alice", dst, "2030-06-01")
        self.assertEqual(len(retry["accounts"][0]["conflicts"]), 1)
        with self.db.connect() as conn:
            total = conn.execute("SELECT COALESCE(SUM(amount),0) t FROM usage_records WHERE account_id=?", (dst,)).fetchone()["t"]
        self.assertEqual(total, 700)
        # Move it back and the replayed ledger is consistent again.
        restored = self.db.reschedule_transfer(transfer["id"], "alice",
                                               {"effective_date": "2030-06-01"}, "editor")
        self.assertTrue(restored["recompute"]["ok"])
        self.assertEqual(self.avail(dst, "2030-06-02")["available"], 100)

    # -- views share one settlement date --------------------------------

    def test_accounts_transfer_detail_and_drought_share_as_of(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 300, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        before = self.db.list_accounts("2026-05-31")
        self.assertEqual(before["as_of"], "2026-05-31")
        by_name = {a["name"]: a for a in before["accounts"]}
        self.assertEqual(by_name["北区水库"]["available"], 900)
        self.assertEqual(by_name["河口灌区"]["available"], 500)
        after = self.db.list_accounts("2026-06-01")
        by_name = {a["name"]: a for a in after["accounts"]}
        self.assertEqual(by_name["北区水库"]["available"], 600)
        self.assertEqual(by_name["河口灌区"]["available"], 800)

        detail = self.db.get_transfer(transfer["id"], "2026-05-31")
        self.assertFalse(detail["settled"])
        self.assertEqual(detail["source"]["available"], 900)
        detail_due = self.db.get_transfer(transfer["id"], "2026-06-01")
        self.assertTrue(detail_due["settled"])
        self.assertEqual(detail_due["target"]["available"], 800)

        drought_before = self.db.simulate_drought(1500, 0.0, "2026-05-31")
        requests = {x["name"]: x["requested"] for x in drought_before["allocations"]}
        self.assertEqual(requests["北区水库"], 900)
        self.assertEqual(requests["河口灌区"], 500)
        drought_after = self.db.simulate_drought(1500, 0.0, "2026-06-01")
        requests = {x["name"]: x["requested"] for x in drought_after["allocations"]}
        self.assertEqual(requests["北区水库"], 600)
        self.assertEqual(requests["河口灌区"], 800)
        total = sum(x["allocation"] for x in drought_after["allocations"])
        self.assertAlmostEqual(total + drought_after["unallocated"], 1500)

    # -- preserved domain rules ----------------------------------------

    def test_duplicate_meter_event_rejected(self):
        self.db.record_usage("meter-01", {
            "account_id": self.source, "amount": 10,
            "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能重复计水"):
            self.db.record_usage("meter-01", {
                "account_id": self.source, "amount": 10,
                "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")

    def test_minimum_retention_and_self_approval_conflicts(self):
        with self.assertRaisesRegex(DomainError, "最小留存"):
            self.db.create_transfer("alice", {
                "from_account_id": self.source, "to_account_id": self.target,
                "amount": 501, "effective_date": "2026-06-01"}, "editor")
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 100, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            self.db.approve_transfer(transfer["id"], "alice", "reviewer")

    def test_double_selling_blocked_at_approval(self):
        first = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 500, "effective_date": "2026-06-01"}, "editor")
        second = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 500, "effective_date": "2026-06-01"}, "editor")
        self.db.approve_transfer(first["id"], "bob", "reviewer")
        with self.assertRaisesRegex(DomainError, "可用额度不足"):
            self.db.approve_transfer(second["id"], "carol", "reviewer")

    def test_season_cap_uses_settled_quota(self):
        transfer = self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 300, "effective_date": "2026-07-01"}, "editor")
        self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        # July cap = 35% of the 700 settled quota = 245.
        self.db.record_usage("m", {
            "account_id": self.source, "amount": 245,
            "meter_event_id": "JUL-1", "occurred_at": "2026-07-10"}, "meter")
        with self.assertRaisesRegex(DomainError, "季节配额"):
            self.db.record_usage("m", {
                "account_id": self.source, "amount": 1,
                "meter_event_id": "JUL-2", "occurred_at": "2026-07-11"}, "meter")


if __name__ == "__main__":
    unittest.main()
