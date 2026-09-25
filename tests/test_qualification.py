import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402
from qualification import QualificationError  # noqa: E402


class QualificationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = ProcurementService(Path(self.tmp.name) / "test.db")
        self.vendor = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        criteria = [{"name": "报价", "weight": 100, "kind": "cost", "max_value": 1000000}]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-Q01", "资格预审项目", (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def submit_materials(self):
        return self.service.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor["id"], [{"name": "营业执照", "note": "副本"}]
        )

    def approve(self, qualification_id):
        return self.service.review_qualification("proc1", "procurement", qualification_id, "approved", "材料齐全")

    def try_bid(self):
        return self.service.submit_bid("vendor1", "vendor", self.tender["id"], self.vendor["id"], {"报价": 500000}, 500000)

    def test_bid_blocked_when_missing_pending_or_rejected(self):
        with self.assertRaises(QualificationError) as ctx:
            self.try_bid()
        self.assertEqual(409, ctx.exception.status)
        record = self.submit_materials()
        with self.assertRaises(QualificationError):
            self.try_bid()
        self.service.review_qualification("proc1", "procurement", record["id"], "rejected", "缺财务报表")
        with self.assertRaises(QualificationError):
            self.try_bid()

    def test_approved_vendor_can_bid_and_state_lists_status(self):
        record = self.submit_materials()
        reviewed = self.approve(record["id"])
        self.assertEqual("approved", reviewed["status"])
        bid = self.try_bid()
        self.assertEqual("sealed", bid["status"])
        state = self.service.state("proc1", "procurement")
        self.assertEqual("approved", state["qualifications"][0]["status"])
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual(record["id"], detail["qualifications"][0]["id"])

    def test_resubmission_resets_to_pending_and_review_can_change_before_opening(self):
        record = self.submit_materials()
        self.service.review_qualification("proc1", "procurement", record["id"], "rejected", "材料不全")
        again = self.service.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor["id"], [{"name": "营业执照"}, {"name": "财务报表"}]
        )
        self.assertEqual("pending", again["status"])
        with self.assertRaises(QualificationError):
            self.try_bid()
        self.approve(again["id"])
        self.assertEqual("sealed", self.try_bid()["status"])

    def test_qualification_frozen_after_opening_and_approved_unaffected(self):
        record = self.submit_materials()
        self.approve(record["id"])
        self.try_bid()
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(1, len(opened["bids"]))
        with self.assertRaises(QualificationError):
            self.submit_materials()
        with self.assertRaises(QualificationError):
            self.service.review_qualification("proc1", "procurement", record["id"], "rejected", "反悔")
        current = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual("approved", current["qualifications"][0]["status"])
        self.service.evaluate_bid("eval1", "evaluator", opened["bids"][0]["id"], {"报价": 500000})
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])

    def test_role_permissions(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_qualification("proc1", "procurement", self.tender["id"], self.vendor["id"], [{"name": "营业执照"}])
        self.assertEqual(403, ctx.exception.status)
        record = self.submit_materials()
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_qualification("vendor1", "vendor", record["id"], "approved", "")
        self.assertEqual(403, ctx2.exception.status)


if __name__ == "__main__":
    unittest.main()
