import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class WaterRightsFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_transfer_approval_usage_and_drought(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 400, "effective_date": "2026-06-01"}, "editor")
        self.assertEqual(self.db.available(source)["reserved_outgoing"], 400)
        approved = self.db.approve_transfer(transfer["id"], "bob", "reviewer")
        self.assertEqual(approved["status"], "approved")
        usage = self.db.record_usage("meter-01", {"account_id": source, "amount": 150, "meter_event_id": "UP-JUL-1", "occurred_at": "2026-07-10"}, "meter")
        self.assertEqual(usage["amount"], 150)
        simulation = self.db.simulate_drought(1000, 0.3)
        self.assertAlmostEqual(sum(x["allocation"] for x in simulation["allocations"]) + simulation["unallocated"], 700)

    def test_duplicate_meter_event_and_pending_reservation(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 500, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaises(DomainError):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 1, "effective_date": "2026-06-01"}, "editor")
        self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "不能重复计水"):
            self.db.record_usage("meter-01", {"account_id": source, "amount": 10, "meter_event_id": "M-1", "occurred_at": "2026-08-01"}, "meter")

    def test_third_party_and_self_approval_conflicts(self):
        source, target = self.accounts["北区水库"], self.accounts["河口灌区"]
        with self.assertRaisesRegex(DomainError, "最小留存"):
            self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 501, "effective_date": "2026-06-01"}, "editor")
        transfer = self.db.create_transfer("alice", {"from_account_id": source, "to_account_id": target, "amount": 100, "effective_date": "2026-06-01"}, "editor")
        with self.assertRaisesRegex(DomainError, "不能批准自己"):
            self.db.approve_transfer(transfer["id"], "alice", "reviewer")


if __name__ == "__main__":
    unittest.main()
