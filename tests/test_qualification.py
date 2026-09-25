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
        self.qual = self.service.qualification
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-001", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-002", "远山系统", "vendor2")
        criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-001", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def bid(self, vendor, actor, price=800000):
        return self.service.submit_bid(actor, "vendor", self.tender["id"], vendor["id"], {"报价": price, "质量": 90}, price)

    def test_unsubmitted_pending_and_rejected_vendors_are_blocked(self):
        with self.assertRaises(DomainError) as ctx:
            self.bid(self.vendor1, "vendor1")
        self.assertEqual(403, ctx.exception.status)
        qualification = self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"}
        )
        self.assertEqual("pending", qualification["status"])
        with self.assertRaises(DomainError) as ctx2:
            self.bid(self.vendor1, "vendor1")
        self.assertEqual(403, ctx2.exception.status)
        reviewed = self.qual.review_qualification("proc1", "procurement", qualification["id"], "rejected", "材料不全")
        self.assertEqual("rejected", reviewed["status"])
        with self.assertRaises(DomainError):
            self.bid(self.vendor1, "vendor1")
        resubmitted = self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效", "资质证书": "ISO9001"}
        )
        self.assertEqual("pending", resubmitted["status"])
        self.assertEqual("", resubmitted["review_comment"])
        self.qual.review_qualification("proc1", "procurement", resubmitted["id"], "approved")
        self.assertEqual("sealed", self.bid(self.vendor1, "vendor1")["status"])

    def test_approved_review_is_final_before_opening(self):
        qualification = self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"}
        )
        self.qual.review_qualification("proc1", "procurement", qualification["id"], "approved")
        with self.assertRaises(QualificationError) as ctx:
            self.qual.review_qualification("proc1", "procurement", qualification["id"], "rejected", "改判")
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(QualificationError) as ctx2:
            self.qual.submit_qualification("vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "新"})
        self.assertEqual(409, ctx2.exception.status)
        self.assertEqual("sealed", self.bid(self.vendor1, "vendor1")["status"])

    def test_results_freeze_after_opening_and_approved_vendor_unaffected(self):
        qual1 = self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"}
        )
        self.qual.review_qualification("proc1", "procurement", qual1["id"], "approved")
        qual2 = self.qual.submit_qualification(
            "vendor2", "vendor", self.tender["id"], self.vendor2["id"], {"营业执照": "有效"}
        )
        self.bid(self.vendor1, "vendor1")
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.assertEqual(1, len(opened["bids"]))
        with self.assertRaises(QualificationError) as ctx:
            self.qual.submit_qualification("vendor2", "vendor", self.tender["id"], self.vendor2["id"], {"营业执照": "补充"})
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(QualificationError) as ctx2:
            self.qual.review_qualification("proc1", "procurement", qual2["id"], "approved")
        self.assertEqual(409, ctx2.exception.status)
        bid_id = opened["bids"][0]["id"]
        self.service.evaluate_bid("eval1", "evaluator", bid_id, {"报价": 800000, "质量": 90})
        current = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], current["tender"]["version"])
        self.assertEqual("awarded", award["tender"]["status"])

    def test_review_permissions_and_validation(self):
        qualification = self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"}
        )
        with self.assertRaises(QualificationError) as ctx:
            self.qual.review_qualification("vendor1", "vendor", qualification["id"], "approved")
        self.assertEqual(403, ctx.exception.status)
        with self.assertRaises(QualificationError):
            self.qual.review_qualification("proc1", "procurement", qualification["id"], "maybe")
        with self.assertRaises(QualificationError):
            self.qual.review_qualification("proc1", "procurement", qualification["id"], "rejected")
        with self.assertRaises(QualificationError) as ctx2:
            self.qual.submit_qualification("proc1", "procurement", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"})
        self.assertEqual(403, ctx2.exception.status)
        with self.assertRaises(QualificationError):
            self.qual.submit_qualification("vendor1", "vendor", self.tender["id"], self.vendor1["id"], {})

    def test_state_and_tender_list_qualification_status(self):
        self.qual.submit_qualification(
            "vendor1", "vendor", self.tender["id"], self.vendor1["id"], {"营业执照": "有效"}
        )
        public_state = self.service.state("visitor", "public")
        self.assertEqual(1, len(public_state["qualifications"]))
        self.assertEqual("pending", public_state["qualifications"][0]["status"])
        self.assertNotIn("materials", public_state["qualifications"][0])
        full_state = self.service.state("proc1", "procurement")
        self.assertIn("materials", full_state["qualifications"][0])
        detail = self.service.get_tender("proc1", "procurement", self.tender["id"])
        self.assertEqual(1, len(detail["qualifications"]))
        self.assertEqual(self.vendor1["id"], detail["qualifications"][0]["vendor_id"])


if __name__ == "__main__":
    unittest.main()
