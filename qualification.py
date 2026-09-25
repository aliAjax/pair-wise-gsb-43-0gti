"""资格预审独立模块。

开标前（项目处于 published 状态）：供应商提交或补交资格材料，采购方逐份审核，
审核结论可以更改；只有审核通过的供应商才允许投标。开标后（项目离开 published
状态）资格结果冻结：材料不能补交，审核不能更改，已通过的供应商保持通过。

模块不依赖采购服务本体，所有方法通过调用方传入的数据库连接参与其事务。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
DECISIONS = {STATUS_APPROVED, STATUS_REJECTED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS qualifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id INTEGER NOT NULL REFERENCES tenders(id),
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    materials TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    review_comment TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(tender_id,vendor_id)
);
CREATE INDEX IF NOT EXISTS idx_qualifications_tender ON qualifications(tender_id,status);
"""


class QualificationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["materials"] = json.loads(item["materials"])
    return item


class QualificationService:
    """资格预审规则与存取，供采购服务组合调用。"""

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise QualificationError("采购项目不存在", 404)
        return row

    def _require_before_opening(self, conn: sqlite3.Connection, tender_id: int, action: str) -> sqlite3.Row:
        tender = self._tender(conn, tender_id)
        if tender["status"] == "draft":
            raise QualificationError("项目尚未发布，不能%s" % action, 409)
        if tender["status"] != "published":
            raise QualificationError("开标后资格结果已冻结，不能%s" % action, 409)
        return tender

    def submit_materials(self, conn: sqlite3.Connection, tender_id: int, vendor_id: int,
                         actor: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(materials, list) or not materials:
            raise QualificationError("资格材料必须是非空列表")
        normalized = []
        for item in materials:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise QualificationError("资格材料格式无效")
            normalized.append({"name": str(item["name"]).strip(), "note": str(item.get("note", "")).strip()})
        self._require_before_opening(conn, tender_id, "提交资格材料")
        if not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
            raise QualificationError("供应商不存在", 404)
        now = _utcnow()
        text = json.dumps(normalized, ensure_ascii=False)
        existing = conn.execute(
            "SELECT * FROM qualifications WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)
        ).fetchone()
        if existing:
            # 补交材料后回到待审核，需采购方重新审核
            conn.execute(
                """UPDATE qualifications SET materials=?,status='pending',review_comment='',reviewed_by=NULL,
                   reviewed_at=NULL,submitted_by=?,submitted_at=?,version=version+1 WHERE id=?""",
                (text, actor, now, existing["id"]),
            )
            qualification_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO qualifications(tender_id,vendor_id,materials,submitted_by,submitted_at) VALUES(?,?,?,?,?)",
                (tender_id, vendor_id, text, actor, now),
            )
            qualification_id = cur.lastrowid
        return _row_to_dict(conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone())

    def review(self, conn: sqlite3.Connection, qualification_id: int, actor: str,
               decision: str, comment: str = "") -> dict[str, Any]:
        row = conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone()
        if not row:
            raise QualificationError("资格预审记录不存在", 404)
        self._require_before_opening(conn, row["tender_id"], "审核资格材料")
        if decision not in DECISIONS:
            raise QualificationError("审核决定必须是 approved 或 rejected")
        conn.execute(
            "UPDATE qualifications SET status=?,review_comment=?,reviewed_by=?,reviewed_at=?,version=version+1 WHERE id=?",
            (decision, (comment or "").strip(), actor, _utcnow(), qualification_id),
        )
        return _row_to_dict(conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone())

    def assert_can_bid(self, conn: sqlite3.Connection, tender_id: int, vendor_id: int) -> None:
        row = conn.execute(
            "SELECT status FROM qualifications WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)
        ).fetchone()
        if not row:
            raise QualificationError("供应商尚未提交资格预审材料，不能投标", 409)
        if row["status"] == STATUS_PENDING:
            raise QualificationError("资格预审材料尚未审核，不能投标", 409)
        if row["status"] != STATUS_APPROVED:
            raise QualificationError("资格预审未通过，不能投标", 409)

    def list_for_tender(self, conn: sqlite3.Connection, tender_id: int) -> list[dict[str, Any]]:
        return [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM qualifications WHERE tender_id=? ORDER BY id", (tender_id,)
        ).fetchall()]

    def list_recent(self, conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
        return [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM qualifications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
