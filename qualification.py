"""供应商资格预审独立模块：开标前提交材料、采购方逐份审核、开标后结果锁定。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


class QualificationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise QualificationError("缺少操作人")
    return actor


def _require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise QualificationError("角色无权执行：%s" % action, 403)


class QualificationService:
    """资格预审：材料提交与补交、逐份审核、开标冻结和投标资格闸门。"""

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS qualifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    materials TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    review_comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE INDEX IF NOT EXISTS idx_qual_tender ON qualifications(tender_id,status);
                """
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), _utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise QualificationError("采购项目不存在", 404)
        return row

    def submit_qualification(self, actor: str, role: str, tender_id: int, vendor_id: int,
                             materials: dict[str, Any]) -> dict[str, Any]:
        actor = _clean_actor(actor)
        _require_role(role, {"vendor"}, "提交资格材料")
        if not isinstance(materials, dict) or not materials:
            raise QualificationError("资格材料不能为空")
        if any(not str(key).strip() for key in materials):
            raise QualificationError("资格材料条目名称不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise QualificationError("开标后资格材料不能再补交", 409)
            if not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
                raise QualificationError("供应商不存在", 404)
            materials_text = json.dumps(materials, ensure_ascii=False, sort_keys=True)
            existing = conn.execute(
                "SELECT * FROM qualifications WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)
            ).fetchone()
            if existing:
                if existing["status"] == "approved":
                    raise QualificationError("资格审核已通过，不能更换材料", 409)
                conn.execute(
                    """UPDATE qualifications SET materials=?,status='pending',review_comment='',
                       reviewed_by=NULL,reviewed_at=NULL,version=version+1,submitted_by=?,submitted_at=? WHERE id=?""",
                    (materials_text, actor, _utcnow(), existing["id"]),
                )
                qualification_id, action = existing["id"], "qualification.resubmitted"
            else:
                cur = conn.execute(
                    """INSERT INTO qualifications(tender_id,vendor_id,materials,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?)""",
                    (tender_id, vendor_id, materials_text, actor, _utcnow()),
                )
                qualification_id, action = cur.lastrowid, "qualification.submitted"
            self._audit(conn, tender_id, actor, action,
                        {"qualification_id": qualification_id, "vendor_id": vendor_id})
            return dict(conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone())

    def review_qualification(self, actor: str, role: str, qualification_id: int, decision: str,
                             comment: str = "") -> dict[str, Any]:
        actor = _clean_actor(actor)
        _require_role(role, {"procurement", "supervisor"}, "审核资格材料")
        if decision not in {"approved", "rejected"}:
            raise QualificationError("审核结论只支持 approved 或 rejected")
        if decision == "rejected" and not comment.strip():
            raise QualificationError("审核不通过必须填写意见")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone()
            if not row:
                raise QualificationError("资格预审记录不存在", 404)
            tender = self._tender(conn, row["tender_id"])
            if tender["status"] != "published":
                raise QualificationError("开标后资格审核结果不能更改", 409)
            if row["status"] != "pending":
                raise QualificationError("该资格材料已审核，不能重复审核", 409)
            conn.execute(
                """UPDATE qualifications SET status=?,review_comment=?,reviewed_by=?,reviewed_at=?,version=version+1
                   WHERE id=?""",
                (decision, comment.strip(), actor, _utcnow(), qualification_id),
            )
            self._audit(conn, row["tender_id"], actor, "qualification.reviewed",
                        {"qualification_id": qualification_id, "vendor_id": row["vendor_id"], "decision": decision})
            return dict(conn.execute("SELECT * FROM qualifications WHERE id=?", (qualification_id,)).fetchone())

    def status_of(self, conn: sqlite3.Connection, tender_id: int, vendor_id: int) -> str | None:
        row = conn.execute(
            "SELECT status FROM qualifications WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)
        ).fetchone()
        return row["status"] if row else None

    def is_qualified(self, conn: sqlite3.Connection, tender_id: int, vendor_id: int) -> bool:
        return self.status_of(conn, tender_id, vendor_id) == "approved"

    def list_qualifications(self, conn: sqlite3.Connection, tender_id: int | None = None,
                            with_materials: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM qualifications"
        params: list[Any] = []
        if tender_id is not None:
            sql += " WHERE tender_id=?"
            params.append(tender_id)
        sql += " ORDER BY id DESC"
        rows = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            if not with_materials:
                item.pop("materials", None)
            rows.append(item)
        return rows
