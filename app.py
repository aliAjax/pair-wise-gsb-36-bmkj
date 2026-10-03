"""Cross-region water-right allocation and transfer service (standard library only).

Settlement model
----------------
Accounts hold an immutable licensed ``quota``. Transfers never overwrite that
column: an approved transfer enters both ledgers on its ``effective_date`` and
every balance is derived from the entries settled up to a settlement date::

    settled_quota(d) = quota + approved incoming (effective <= d)
                             - approved outgoing (effective <= d)
    settled_used(d)  = usage records with occurred_at <= d
    available(d)     = settled_quota(d) - settled_used(d)

Before the effective date the water still belongs to the transfer-out party,
so pending and future-effective transfers do not reduce availability. On the
effective date transfers settle before same-day usage. Cancelling a
not-yet-effective transfer or moving its effective date replays the affected
usage records; original entries are never rewritten, and replays are pure
validations that can be retried safely.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"
EPS = 1e-9


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


def resolve_as_of(as_of: str | None) -> str:
    """Normalise a settlement-date parameter (default: today)."""
    if as_of is None or str(as_of).strip() == "":
        return date.today().isoformat()
    return parse_date(str(as_of).strip(), "结算日期").isoformat()


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ------------------------------------------------------------------
    # Settlement-date ledger
    # ------------------------------------------------------------------

    def _transfer_sum(self, conn: sqlite3.Connection, column: str, account_id: int,
                      as_of_iso: str, future: bool = False) -> float:
        op = ">" if future else "<="
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) total FROM transfers "
            f"WHERE {column}=? AND status='approved' AND effective_date{op}?",
            (account_id, as_of_iso),
        ).fetchone()
        return float(row["total"])

    def _settled_quota(self, conn: sqlite3.Connection, account_id: int, as_of_iso: str,
                       exclude_transfer_id: int | None = None) -> float:
        """Licensed quota after approved transfers effective on/before the date."""
        account = conn.execute("SELECT quota FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account:
            raise DomainError("水权账户不存在", 404)
        exclude_sql = "AND id<>?" if exclude_transfer_id is not None else ""
        params: list[Any] = [account_id, as_of_iso]
        if exclude_transfer_id is not None:
            params.append(exclude_transfer_id)
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE to_account_id=? "
            f"AND status='approved' AND effective_date<=? {exclude_sql}",
            params,
        ).fetchone()
        incoming = float(row["total"])
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? "
            f"AND status='approved' AND effective_date<=? {exclude_sql}",
            params,
        ).fetchone()
        outgoing = float(row["total"])
        return float(account["quota"]) + incoming - outgoing

    def _settled_used(self, conn: sqlite3.Connection, account_id: int, as_of_iso: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND occurred_at<=?",
            (account_id, as_of_iso),
        ).fetchone()
        return float(row["total"])

    def _settlement(self, conn: sqlite3.Connection, account_id: int, as_of_iso: str,
                    exclude_transfer_id: int | None = None) -> dict[str, float]:
        quota = self._settled_quota(conn, account_id, as_of_iso, exclude_transfer_id)
        used = self._settled_used(conn, account_id, as_of_iso)
        incoming = self._transfer_sum(conn, "to_account_id", account_id, as_of_iso)
        outgoing = self._transfer_sum(conn, "from_account_id", account_id, as_of_iso)
        future_in = self._transfer_sum(conn, "to_account_id", account_id, as_of_iso, future=True)
        future_out = self._transfer_sum(conn, "from_account_id", account_id, as_of_iso, future=True)
        return {
            "base_quota": float(conn.execute("SELECT quota FROM accounts WHERE id=?", (account_id,)).fetchone()["quota"]),
            "quota": quota,
            "used": used,
            "available": max(0.0, quota - used),
            "settled_incoming": incoming,
            "settled_outgoing": outgoing,
            "future_incoming": future_in,
            "future_outgoing": future_out,
        }

    def _assert_feasible(self, conn: sqlite3.Connection, source: sqlite3.Row, target: sqlite3.Row,
                         amount: float, effective_iso: str, exclude_transfer_id: int | None) -> None:
        """Validate a transfer against the ledger as it will stand at its effective date.

        Same-day transfers settle first, so usage already booked on the
        effective date is counted and the late party receives the conflict.
        """
        quota_d = self._settled_quota(conn, source["id"], effective_iso, exclude_transfer_id)
        used_d = self._settled_used(conn, source["id"], effective_iso)
        available_d = quota_d - used_d
        if amount > available_d + EPS:
            raise DomainError("按生效日结算后转出方可用额度不足", 409)
        impact = conn.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
            (source["region"], target["region"]),
        ).fetchone()
        if impact:
            minimum = quota_d * float(impact["min_source_fraction"])
            if available_d - amount + EPS < minimum:
                raise DomainError("转让会违反下游第三方最小留存约束", 409)

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    # ------------------------------------------------------------------
    # Setup endpoints
    # ------------------------------------------------------------------

    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, region, holder, priority, valid_from.isoformat(), valid_to.isoformat(), quota, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", cur.lastrowid, {"name": name, "quota": quota})
            return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
                (region.strip(), int(month), float(max_fraction), note),
            )
            self._audit(conn, actor, "season_rule.saved", "region", None, {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(source_region,target_region) DO UPDATE SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
                (source_region, target_region, float(min_source_fraction), note),
            )
            self._audit(conn, actor, "impact_rule.saved", "region", None, {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region, "min_source_fraction": min_source_fraction, "note": note}

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        as_of_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            self._account_row(conn, account_id)
            result = {"account_id": account_id, "as_of": as_of_iso}
            result.update(self._settlement(conn, account_id, as_of_iso))
        return result

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        effective_iso = effective.isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            if not (source["valid_from"] <= effective_iso <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective_iso <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            # Advisory check against the projected settlement at the effective
            # date. Pending transfers do not reserve water; approval is the
            # authoritative gate, so this never blocks the source from using
            # the water before the effective date.
            self._assert_feasible(conn, source, target, amount, effective_iso, None)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, effective_iso, actor, utcnow()),
            )
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": effective_iso})
            row = dict(conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone())
        return self._with_settlement_view(row, resolve_as_of(None))

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            effective_iso = transfer["effective_date"]
            if not (source["valid_from"] <= effective_iso <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective_iso <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            # Authoritative settlement-date check inside the write lock:
            # same-day usage committed by a competing meter transaction is
            # visible here, so whichever request settles second gets 409 and
            # the first transaction's ledger stays untouched.
            self._assert_feasible(conn, source, target, amount, effective_iso, transfer_id)
            conn.execute(
                "UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                (actor, utcnow(), transfer_id),
            )
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id,
                        {"amount": amount, "effective_date": effective_iso})
            row = dict(conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone())
        return self._with_settlement_view(row, resolve_as_of(None))

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute("UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def _get_editable_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if not row:
            raise DomainError("转让记录不存在", 404)
        if row["status"] in ("rejected", "cancelled"):
            raise DomainError("转让已终结，不能再修改", 409)
        if row["status"] == "approved" and row["effective_date"] <= date.today().isoformat():
            raise DomainError("转让已经生效，不能再修改", 409)
        return row

    def cancel_transfer(self, transfer_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        """Cancel a pending or approved-not-yet-effective transfer, then replay."""
        if role != "editor":
            raise DomainError("只有水权编辑人员可以取消转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_editable_transfer(conn, transfer_id)
            old_status = row["status"]
            effective_iso = row["effective_date"]
            conn.execute("UPDATE transfers SET status='cancelled' WHERE id=?", (transfer_id,))
            self._audit(conn, actor, "transfer.cancelled", "transfer", transfer_id,
                        {"former_status": old_status, "effective_date": effective_iso})
            # Settlement only changes for entries on/after the effective date.
            replay = self._replay_many(
                conn, [int(row["from_account_id"]), int(row["to_account_id"])], effective_iso)
            saved = dict(conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone())
        return {"transfer": self._with_settlement_view(saved, resolve_as_of(None)), "recompute": replay}

    def reschedule_transfer(self, transfer_id: int, actor: str, payload: dict[str, Any],
                            role: str = "editor") -> dict[str, Any]:
        """Move an effective date, then replay the affected usage window."""
        if role != "editor":
            raise DomainError("只有水权编辑人员可以调整生效日期", 403)
        new_date = parse_date(str(payload.get("effective_date", "")), "生效日期")
        new_iso = new_date.isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._get_editable_transfer(conn, transfer_id)
            old_iso = row["effective_date"]
            source = self._account_row(conn, int(row["from_account_id"]))
            target = self._account_row(conn, int(row["to_account_id"]))
            if not (source["valid_from"] <= new_iso <= source["valid_to"]):
                raise DomainError("转出账户在新生效日无效", 409)
            if not (target["valid_from"] <= new_iso <= target["valid_to"]):
                raise DomainError("转入账户在新生效日无效", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            # Feasibility at the new settlement date (this very transfer is
            # excluded so a repeat submission with the same date is a harmless
            # no-op and never deducts water twice).
            self._assert_feasible(conn, source, target, float(row["amount"]), new_iso, transfer_id)
            conn.execute("UPDATE transfers SET effective_date=? WHERE id=?", (new_iso, transfer_id))
            self._audit(conn, actor, "transfer.rescheduled", "transfer", transfer_id,
                        {"old_date": old_iso, "new_date": new_iso, "status": row["status"]})
            # Only the band between the two dates is settled differently.
            since = min(old_iso, new_iso)
            replay = self._replay_many(
                conn, [int(row["from_account_id"]), int(row["to_account_id"])], since)
            saved = dict(conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone())
        return {"transfer": self._with_settlement_view(saved, resolve_as_of(None)), "recompute": replay}

    # ------------------------------------------------------------------
    # Metered usage
    # ------------------------------------------------------------------

    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        occurred_iso = occurred.isoformat()
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            if not (account["valid_from"] <= occurred_iso <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            # Same-day approved transfers settle first, so they are already
            # part of the quota at this date. On failure the transaction rolls
            # back: no usage row, no cached total — the prior ledger is intact.
            quota_d = self._settled_quota(conn, account_id, occurred_iso)
            used_d = self._settled_used(conn, account_id, occurred_iso)
            if amount > quota_d - used_d + EPS:
                raise DomainError("先结清同日转让后可用额度不足，取水登记失败", 409)
            season = conn.execute(
                "SELECT max_fraction FROM season_rules WHERE region=? AND month=?",
                (account["region"], occurred.month),
            ).fetchone()
            if season:
                month_total = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) total FROM usage_records "
                    "WHERE account_id=? AND substr(occurred_at,1,7)=? AND occurred_at<=?",
                    (account_id, occurred.strftime("%Y-%m"), occurred_iso),
                ).fetchone()["total"]
                cap = quota_d * float(season["max_fraction"])
                if float(month_total) + amount > cap + EPS:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, occurred_iso, actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred_iso, "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    # ------------------------------------------------------------------
    # Recomputation (pure replay, safe to retry)
    # ------------------------------------------------------------------

    def _replay_account(self, conn: sqlite3.Connection, account_id: int, since_iso: str) -> dict[str, Any]:
        account = self._account_row(conn, account_id)
        rows = conn.execute(
            "SELECT * FROM usage_records WHERE account_id=? AND occurred_at>=? ORDER BY occurred_at,id",
            (account_id, since_iso),
        ).fetchall()
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            d = row["occurred_at"]
            quota_d = self._settled_quota(conn, account_id, d)
            # Cumulative usage through this record (same-day entries ordered by id).
            used_through = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM usage_records "
                "WHERE account_id=? AND (occurred_at<? OR (occurred_at=? AND id<=?))",
                (account_id, d, d, row["id"]),
            ).fetchone()["total"]
            if float(used_through) > quota_d + EPS:
                conflicts.append({
                    "usage_id": int(row["id"]),
                    "meter_event_id": row["meter_event_id"],
                    "occurred_at": d,
                    "amount": float(row["amount"]),
                    "reason": "可用额度不足",
                    "settled_quota": quota_d,
                    "settled_used": float(used_through),
                    "shortfall": float(used_through) - quota_d,
                })
                continue
            season = conn.execute(
                "SELECT max_fraction FROM season_rules WHERE region=? AND month=?",
                (account["region"], int(d[5:7])),
            ).fetchone()
            if season:
                month_cum = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) total FROM usage_records "
                    "WHERE account_id=? AND substr(occurred_at,1,7)=? "
                    "AND (occurred_at<? OR (occurred_at=? AND id<=?))",
                    (account_id, d[:7], d, d, row["id"]),
                ).fetchone()["total"]
                cap = quota_d * float(season["max_fraction"])
                if float(month_cum) > cap + EPS:
                    conflicts.append({
                        "usage_id": int(row["id"]),
                        "meter_event_id": row["meter_event_id"],
                        "occurred_at": d,
                        "amount": float(row["amount"]),
                        "reason": "季节配额超限",
                        "settled_quota": quota_d,
                        "month_total": float(month_cum),
                        "season_cap": cap,
                        "shortfall": float(month_cum) - cap,
                    })
        return {"account_id": account_id, "name": account["name"], "checked": len(rows),
                "ok": not conflicts, "conflicts": conflicts}

    def _replay_many(self, conn: sqlite3.Connection, account_ids: list[int], since_iso: str) -> dict[str, Any]:
        seen: set[int] = set()
        reports = []
        for account_id in account_ids:
            if account_id in seen:
                continue
            seen.add(account_id)
            reports.append(self._replay_account(conn, account_id, since_iso))
        return {"since": since_iso, "accounts": reports, "ok": all(r["ok"] for r in reports)}

    def recompute(self, actor: str, account_id: int | None = None, since: str | None = None,
                  role: str = "viewer") -> dict[str, Any]:
        since_iso = "0001-01-01" if since in (None, "") else parse_date(str(since), "起始日期").isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if account_id is None:
                ids = [int(r["id"]) for r in conn.execute("SELECT id FROM accounts ORDER BY id").fetchall()]
            else:
                ids = [int(self._account_row(conn, int(account_id))["id"])]
            report = self._replay_many(conn, ids, since_iso)
            report["recomputed_at"] = date.today().isoformat()
            self._audit(conn, actor, "settlement.recomputed", "account", account_id,
                        {"since": since_iso, "ok": report["ok"]})
        return report

    # ------------------------------------------------------------------
    # Date-aware views
    # ------------------------------------------------------------------

    def _with_settlement_view(self, transfer: dict[str, Any], as_of_iso: str) -> dict[str, Any]:
        with self.connect() as conn:
            item = dict(transfer)
            item["as_of"] = as_of_iso
            item["settled"] = item["status"] == "approved" and item["effective_date"] <= as_of_iso
            item["source"] = self._settlement(conn, int(item["from_account_id"]), as_of_iso)
            item["target"] = self._settlement(conn, int(item["to_account_id"]), as_of_iso)
        return item

    def simulate_drought(self, total_supply: float, reduction: float = 0.0,
                         as_of: str | None = None, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        as_of_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY priority,name").fetchall()
            settled = []
            for row in rows:
                quota_d = self._settled_quota(conn, int(row["id"]), as_of_iso)
                used_d = self._settled_used(conn, int(row["id"]), as_of_iso)
                settled.append({
                    "row": row,
                    "quota": quota_d,
                    "used": used_d,
                    "requested": max(0.0, quota_d - used_d),
                    "valid": row["valid_from"] <= as_of_iso <= row["valid_to"],
                })
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [s for s in settled if int(s["row"]["priority"]) == priority]
            if not group:
                continue
            # During shortage, more critical rights receive their remaining
            # settled allocation first; only then does water flow downstream.
            requested = sum(s["requested"] for s in group)
            if requested <= 0:
                continue
            take = min(remaining, requested)
            for item in group:
                share = take * item["requested"] / requested
                allocation[int(item["row"]["id"])] = share
                deficit[int(item["row"]["id"])] = item["requested"] - share
            remaining -= take
            if remaining <= EPS:
                for lower in settled:
                    if int(lower["row"]["priority"]) > priority:
                        allocation[int(lower["row"]["id"])] = 0.0
                        deficit[int(lower["row"]["id"])] = lower["requested"]
                break
        return {"as_of": as_of_iso, "total_supply": total_supply, "reduction": reduction,
                "effective_supply": supply, "unallocated": remaining, "allocations": [
                    {"account_id": int(s["row"]["id"]), "name": s["row"]["name"], "priority": s["row"]["priority"],
                     "valid": s["valid"], "quota": s["quota"], "used": s["used"], "requested": s["requested"],
                     "allocation": allocation.get(int(s["row"]["id"]), 0.0),
                     "deficit": deficit.get(int(s["row"]["id"]), s["requested"])}
                    for s in settled
                ]}

    def list_accounts(self, as_of: str | None = None) -> dict[str, Any]:
        as_of_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            accounts = []
            for row in rows:
                item = dict(row)
                item["as_of"] = as_of_iso
                item["valid"] = row["valid_from"] <= as_of_iso <= row["valid_to"]
                item.update(self._settlement(conn, int(row["id"]), as_of_iso))
                accounts.append(item)
        return {"as_of": as_of_iso, "accounts": accounts}

    def list_transfers(self, as_of: str | None = None) -> dict[str, Any]:
        as_of_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()
            transfers = []
            for row in rows:
                item = dict(row)
                item["as_of"] = as_of_iso
                item["settled"] = item["status"] == "approved" and item["effective_date"] <= as_of_iso
                item["source"] = self._settlement(conn, int(row["from_account_id"]), as_of_iso)
                item["target"] = self._settlement(conn, int(row["to_account_id"]), as_of_iso)
                transfers.append(item)
        return {"as_of": as_of_iso, "transfers": transfers}

    def get_transfer(self, transfer_id: int, as_of: str | None = None) -> dict[str, Any]:
        as_of_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row:
                raise DomainError("转让记录不存在", 404)
            item = dict(row)
            item["as_of"] = as_of_iso
            item["settled"] = item["status"] == "approved" and item["effective_date"] <= as_of_iso
            item["source"] = self._settlement(conn, int(row["from_account_id"]), as_of_iso)
            item["target"] = self._settlement(conn, int(row["to_account_id"]), as_of_iso)
        return item

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts()["accounts"]:
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()["accounts"]}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send(self.db.list_accounts(q.get("as_of", [""])[0]))
            if parsed.path == "/api/transfers":
                return self._send(self.db.list_transfers(q.get("as_of", [""])[0]))
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id, q.get("as_of", [""])[0]))
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "transfers"]:
                return self._send(self.db.get_transfer(int(parts[2]), q.get("as_of", [""])[0]))
            if parsed.path == "/api/drought/simulate":
                return self._send(self.db.simulate_drought(
                    float(q.get("supply", ["0"])[0]),
                    float(q.get("reduction", ["0"])[0]),
                    q.get("as_of", [""])[0]))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(actor, str(body.get("region", "")), int(body.get("month", 0)), body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(actor, str(body.get("source_region", "")), str(body.get("target_region", "")), body.get("min_source_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "cancel":
                return self._send(self.db.cancel_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reschedule":
                return self._send(self.db.reschedule_transfer(int(parts[2]), actor, body, role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            if parts == ["api", "recompute"]:
                return self._send(self.db.recompute(
                    actor, body.get("account_id"), body.get("since"), role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
