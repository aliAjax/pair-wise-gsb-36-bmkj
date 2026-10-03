"""Cross-region water-right allocation and transfer service (standard library only).

Settlement-date ledger model
----------------------------
``accounts.quota`` is the immutable permit amount. Transfers never mutate it.
Every balance is derived for a settlement date ``D``::

    settled_quota(A, D) = quota(A)
        + approved transfers into  A with effective_date <= D
        - approved transfers out of A with effective_date <= D
    used(A, D)      = sum(usage of A with occurred_at <= D)
    available(A, D) = settled_quota(A, D) - used(A, D)

A transfer therefore changes both sides only on its effective date; before
that date the water still belongs to the transferor. Pending transfers are not
settled water, but they are treated as worst-case reservations when deciding
whether a (new) transfer can ever settle.

Within one settlement date transfers clear before usage. Inserting an entry is
validated by re-walking the affected ledger; if a later-committed entry (an
approval racing a meter reading, a reschedule, ...) would overflow an existing
entry, the newcomer receives a 409 conflict and no existing record is rewritten.
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

# Ordering inside one settlement date: transfers clear before usage.
ORDER_TRANSFER = 0
ORDER_USAGE = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


def resolve_as_of(value: str | None) -> str:
    """Settlement date used by the read-side views."""
    if value is None or str(value).strip() == "":
        return today_iso()
    return parse_date(str(value).strip(), "结算日期").isoformat()


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details


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
                    approved_at TEXT,
                    cancelled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_transfers_effective
                    ON transfers(effective_date, status);
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
                CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(occurred_at);
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
            # Migration for databases created before cancellable transfers.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(transfers)")}
            if "cancelled_at" not in columns:
                conn.execute("ALTER TABLE transfers ADD COLUMN cancelled_at TEXT")

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ------------------------------------------------------------------ reads

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _transfer_row(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if not row:
            raise DomainError("转让记录不存在", 404)
        return row

    def _snapshot(self, conn: sqlite3.Connection, account_id: int, d_iso: str) -> dict[str, Any]:
        """Derived settlement-date balance; never reads a mutated quota."""
        account = self._account_row(conn, account_id)
        sums = conn.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(amount),0) FROM transfers
                   WHERE to_account_id=? AND status='approved' AND effective_date<=?) AS transfers_in,
                (SELECT COALESCE(SUM(amount),0) FROM transfers
                   WHERE from_account_id=? AND status='approved' AND effective_date<=?) AS transfers_out,
                (SELECT COALESCE(SUM(amount),0) FROM usage_records
                   WHERE account_id=? AND occurred_at<=?) AS used,
                (SELECT COALESCE(SUM(amount),0) FROM transfers
                   WHERE from_account_id=? AND status='pending' AND effective_date<=?) AS pending_out
            """,
            (account_id, d_iso, account_id, d_iso, account_id, d_iso, account_id, d_iso),
        ).fetchone()
        transfers_in = float(sums["transfers_in"])
        transfers_out = float(sums["transfers_out"])
        used = float(sums["used"])
        pending_out = float(sums["pending_out"])
        settled_quota = float(account["quota"]) + transfers_in - transfers_out
        available = settled_quota - used
        return {
            "quota": float(account["quota"]),
            "transfers_in": transfers_in,
            "transfers_out": transfers_out,
            "settled_quota": settled_quota,
            "used": used,
            "available": available,
            "pending_outgoing": pending_out,
            "projected_available": available - pending_out,
        }

    def _ledger_conflicts(self, conn: sqlite3.Connection, account_id: int, *,
                          projected: bool = False,
                          transfer_candidate: dict[str, Any] | None = None,
                          usage_candidate: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Re-walk one account's ledger and return every settlement violation.

        ``transfer_candidate`` replaces the row with the same id (or is a
        synthetic outgoing transfer when ``id`` is None). ``projected`` also
        treats pending outgoing transfers as reservations. Events on the same
        date are walked transfer-first.
        """
        account = self._account_row(conn, account_id)
        base_quota = float(account["quota"])
        candidate = transfer_candidate or {}
        candidate_id = candidate.get("id")
        events: list[tuple[str, int, str, Any]] = []
        transfers = conn.execute(
            "SELECT * FROM transfers WHERE (from_account_id=? OR to_account_id=?) AND status IN ('approved','pending')",
            (account_id, account_id),
        ).fetchall()
        for t in transfers:
            if candidate_id is not None and int(t["id"]) == candidate_id:
                status = candidate.get("status", t["status"])
                effective = candidate["effective_date"]
            else:
                status, effective = t["status"], t["effective_date"]
            if status == "approved":
                delta = float(t["amount"]) if int(t["to_account_id"]) == account_id else -float(t["amount"])
                events.append((effective, ORDER_TRANSFER, "quota", delta))
            elif status == "pending" and projected and int(t["from_account_id"]) == account_id:
                events.append((effective, ORDER_TRANSFER, "reserve", float(t["amount"])))
        if candidate_id is None and candidate:
            amount = float(candidate["amount"])
            if candidate.get("status") == "approved":
                delta = amount if candidate.get("side") == "to" else -amount
                events.append((candidate["effective_date"], ORDER_TRANSFER, "quota", delta))
            elif candidate.get("status") == "pending" and projected and candidate.get("side", "from") == "from":
                events.append((candidate["effective_date"], ORDER_TRANSFER, "reserve", amount))

        for u in conn.execute(
            "SELECT * FROM usage_records WHERE account_id=? ORDER BY occurred_at,id", (account_id,)
        ).fetchall():
            events.append((u["occurred_at"], ORDER_USAGE, "usage", u))
        if usage_candidate is not None:
            events.append((usage_candidate["effective_date"], ORDER_USAGE, "usage",
                           {"id": None, "meter_event_id": usage_candidate.get("meter_event_id"),
                            "amount": float(usage_candidate["amount"])}))
        events.sort(key=lambda e: (e[0], e[1]))

        season_cache: dict[int, sqlite3.Row | None] = {}

        def season_rule(month: int) -> sqlite3.Row | None:
            if month not in season_cache:
                season_cache[month] = conn.execute(
                    "SELECT max_fraction FROM season_rules WHERE region=? AND month=?",
                    (account["region"], month),
                ).fetchone()
            return season_cache[month]

        conflicts: list[dict[str, Any]] = []
        settled_quota = base_quota
        reserved = 0.0
        used_total = 0.0
        month_used: dict[str, float] = {}
        for effective, _order, kind, payload in events:
            if kind == "quota":
                settled_quota += float(payload)
            elif kind == "reserve":
                reserved += float(payload)
                # A pending outgoing transfer must remain coverable even when
                # no meter reading exists yet (otherwise the same water could
                # be promised twice).
                free_for_reservation = settled_quota - used_total
                if reserved > free_for_reservation + EPS:
                    conflicts.append({
                        "type": "reservation",
                        "usage_id": None,
                        "meter_event_id": None,
                        "settlement_date": effective,
                        "reserved": reserved,
                        "available": max(0.0, free_for_reservation),
                    })
            else:
                u = payload
                amount = float(u["amount"])
                available_now = settled_quota - reserved - used_total
                if amount > available_now + EPS:
                    conflicts.append({
                        "type": "availability",
                        "usage_id": u["id"],
                        "meter_event_id": u["meter_event_id"],
                        "settlement_date": effective,
                        "amount": amount,
                        "available": max(0.0, available_now),
                    })
                used_total += amount
                month_key = effective[:7]
                month_used[month_key] = month_used.get(month_key, 0.0) + amount
                rule = season_rule(int(effective[5:7]))
                if rule:
                    cap = settled_quota * float(rule["max_fraction"])
                    if month_used[month_key] > cap + EPS:
                        conflicts.append({
                            "type": "season_cap",
                            "usage_id": u["id"],
                            "meter_event_id": u["meter_event_id"],
                            "settlement_date": effective,
                            "month_total": month_used[month_key],
                            "season_cap": cap,
                        })
        return conflicts

    def _first_conflict_error(self, conflicts: list[dict[str, Any]], prefix: str) -> DomainError:
        first = conflicts[0]
        if first["type"] == "season_cap":
            message = f"{prefix}：{first['settlement_date']} 当月累计取水超过季节上限"
        elif first["type"] == "reservation":
            message = f"{prefix}：{first['settlement_date']} 待审批转让预占额度超过可承诺水量"
        else:
            message = f"{prefix}：{first['settlement_date']} 结算可用额度不足（同日转让先结清）"
        return DomainError(message, 409, {"conflicts": conflicts})

    # ------------------------------------------------------------- accounts

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
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone()
            result = dict(row)
            result.update({"as_of": today_iso(), **self._snapshot(conn, cur.lastrowid, today_iso())})
            return result

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        d_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            snapshot = self._snapshot(conn, account_id, d_iso)
        return {"account_id": account_id, "as_of": d_iso, **snapshot}

    def list_accounts(self, as_of: str | None = None) -> list[dict[str, Any]]:
        d_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["used"] = None  # legacy counter is not authoritative under the ledger model
                item.update({"as_of": d_iso, **self._snapshot(conn, int(row["id"]), d_iso)})
                result.append(item)
        return result

    # ---------------------------------------------------------------- rules

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

    # ------------------------------------------------------------- transfers

    def _valid_on(self, row: sqlite3.Row, d_iso: str, label: str) -> None:
        if not (row["valid_from"] <= d_iso <= row["valid_to"]):
            raise DomainError(f"{label}在结算日 {d_iso} 不在许可有效期内", 409)

    def _impact_check(self, conn: sqlite3.Connection, source: sqlite3.Row, target: sqlite3.Row,
                      amount: float, d_iso: str, exclude_transfer_id: int | None) -> None:
        impact = conn.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
            (source["region"], target["region"]),
        ).fetchone()
        if not impact:
            return
        # Retention is a hard constraint on the actual post-transfer balance;
        # pending reservations of unrelated transfers are not settled water.
        params: list[Any] = [source["id"], d_iso]
        exclude_sql = ""
        if exclude_transfer_id is not None:
            exclude_sql = " AND id<>?"
            params.append(exclude_transfer_id)
        settled_out = float(conn.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM transfers WHERE from_account_id=? AND status='approved' AND effective_date<=?{exclude_sql}",
            params,
        ).fetchone()[0])
        settled_in = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transfers WHERE to_account_id=? AND status='approved' AND effective_date<=?",
            (source["id"], d_iso),
        ).fetchone()[0])
        used = float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM usage_records WHERE account_id=? AND occurred_at<=?",
            (source["id"], d_iso),
        ).fetchone()[0])
        remaining = float(source["quota"]) + settled_in - settled_out - used - amount
        minimum = float(source["quota"]) * float(impact["min_source_fraction"])
        if remaining + EPS < minimum:
            raise DomainError("转让会违反下游第三方最小留存约束", 409)

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
        d_iso = effective.isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            self._valid_on(source, d_iso, "转出账户")
            self._valid_on(target, d_iso, "转入账户")
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            # Pending reservations are worst-case commitments, so a new pending
            # transfer must still leave a feasible ledger at its settlement date.
            conflicts = self._ledger_conflicts(
                conn, source_id, projected=True,
                transfer_candidate={"id": None, "status": "pending", "side": "from",
                                    "amount": amount, "effective_date": d_iso},
            )
            if conflicts:
                raise self._first_conflict_error(conflicts, "发起转让会使既有取水结算失败")
            self._impact_check(conn, source, target, amount, d_iso, None)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, d_iso, actor, utcnow()),
            )
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": d_iso})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._enrich_transfer(conn, row, today_iso())

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = self._transfer_row(conn, transfer_id)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            d_iso = transfer["effective_date"]
            self._valid_on(source, d_iso, "转出账户")
            self._valid_on(target, d_iso, "转入账户")
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            amount = float(transfer["amount"])
            candidate = {"id": transfer_id, "status": "approved", "effective_date": d_iso}
            for account_id in (int(source["id"]), int(target["id"])):
                conflicts = self._ledger_conflicts(conn, account_id, transfer_candidate=candidate)
                if conflicts:
                    raise self._first_conflict_error(conflicts, "审批与已登记取水按结算顺序冲突，原账目未改动")
            self._impact_check(conn, source, target, amount, d_iso, transfer_id)
            conn.execute(
                "UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                (actor, utcnow(), transfer_id),
            )
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id,
                        {"amount": amount, "effective_date": d_iso})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            return self._enrich_transfer(conn, row, today_iso())

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(conn, transfer_id)
            if row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute(
                "UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?",
                (actor, utcnow(), transfer_id),
            )
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def cancel_transfer(self, transfer_id: int, actor: str, role: str = "editor",
                        as_of: str | None = None) -> dict[str, Any]:
        """Cancel a pending or approved-not-yet-effective transfer.

        Idempotent: cancelling an already-cancelled transfer returns it as-is
        and never writes a second audit entry.
        """
        if role not in {"editor", "reviewer"}:
            raise DomainError("只有水权编辑或审核人员可以取消转让", 403)
        today = resolve_as_of(as_of)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(conn, transfer_id)
            if row["status"] == "cancelled":
                return self._enrich_transfer(conn, row, today)
            if row["status"] == "rejected":
                raise DomainError("已退回的转让不能取消", 409)
            if row["status"] == "approved" and row["effective_date"] <= today:
                raise DomainError("转让已到生效日，不能取消；取消只适用于未生效转让", 409)
            conn.execute("UPDATE transfers SET status='cancelled',cancelled_at=? WHERE id=?", (utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.cancelled", "transfer", transfer_id,
                        {"former_status": row["status"], "effective_date": row["effective_date"]})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            result = self._enrich_transfer(conn, row, today)
            result["recomputed"] = self._recomputed_sides(conn, row, today)
            return result

    def reschedule_transfer(self, transfer_id: int, actor: str, payload: dict[str, Any],
                            role: str = "editor", as_of: str | None = None) -> dict[str, Any]:
        """Move a not-yet-effective transfer to a new settlement date.

        Both ledgers are recomputed under the candidate date; on any conflict
        the transfer keeps its old effective date and nothing else is touched,
        so the same request can safely be retried.
        """
        if role not in {"editor", "reviewer"}:
            raise DomainError("只有水权编辑或审核人员可以调整生效日期", 403)
        new_effective = parse_date(str((payload or {}).get("effective_date", "")), "新生效日期")
        new_iso = new_effective.isoformat()
        today = resolve_as_of(as_of)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._transfer_row(conn, transfer_id)
            if row["status"] in {"cancelled", "rejected"}:
                raise DomainError("已终结的转让不能调整生效日期", 409)
            if row["status"] == "approved":
                if row["effective_date"] <= today:
                    raise DomainError("转让已到生效日，不能调整生效日期", 409)
                if new_iso < today:
                    raise DomainError("已批准转让的新生效日期不能早于当前结算日", 409)
            if new_iso == row["effective_date"]:
                # Idempotent retry: nothing to recompute.
                return self._enrich_transfer(conn, row, today)
            source = self._account_row(conn, row["from_account_id"])
            target = self._account_row(conn, row["to_account_id"])
            self._valid_on(source, new_iso, "转出账户")
            self._valid_on(target, new_iso, "转入账户")
            candidate = {"id": transfer_id, "status": row["status"], "effective_date": new_iso}
            amount = float(row["amount"])
            if row["status"] == "approved":
                for account_id in (int(source["id"]), int(target["id"])):
                    conflicts = self._ledger_conflicts(conn, account_id, transfer_candidate=candidate)
                    if conflicts:
                        raise self._first_conflict_error(
                            conflicts, "调整生效日期后重算取水失败，原账目未改动，可修改日期后重试")
                self._impact_check(conn, source, target, amount, new_iso, transfer_id)
            else:
                conflicts = self._ledger_conflicts(conn, int(source["id"]), projected=True,
                                                   transfer_candidate=candidate)
                if conflicts:
                    raise self._first_conflict_error(
                        conflicts, "调整生效日期后重算取水失败，原账目未改动，可修改日期后重试")
                self._impact_check(conn, source, target, amount, new_iso, transfer_id)
            old_iso = row["effective_date"]
            conn.execute("UPDATE transfers SET effective_date=? WHERE id=?", (new_iso, transfer_id))
            self._audit(conn, actor, "transfer.rescheduled", "transfer", transfer_id,
                        {"old_date": old_iso, "new_date": new_iso, "status": row["status"]})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            result = self._enrich_transfer(conn, row, today)
            result["recomputed"] = self._recomputed_sides(conn, row, new_iso)
            return result

    def _recomputed_sides(self, conn: sqlite3.Connection, row: sqlite3.Row, d_iso: str) -> dict[str, Any]:
        return {
            "as_of": d_iso,
            "source": self._snapshot(conn, int(row["from_account_id"]), d_iso),
            "target": self._snapshot(conn, int(row["to_account_id"]), d_iso),
        }

    def _enrich_transfer(self, conn: sqlite3.Connection, row: sqlite3.Row, d_iso: str) -> dict[str, Any]:
        item = dict(row)
        names = conn.execute(
            "SELECT a.name AS from_name, b.name AS to_name FROM accounts a, accounts b WHERE a.id=? AND b.id=?",
            (row["from_account_id"], row["to_account_id"]),
        ).fetchone()
        item["from_name"] = names["from_name"]
        item["to_name"] = names["to_name"]
        status = row["status"]
        if status == "pending":
            state = "pending_approval"
        elif status == "approved":
            state = "settled" if row["effective_date"] <= d_iso else "approved_pending_effect"
        else:
            state = status
        item["as_of"] = d_iso
        item["settlement_state"] = state
        item["settled"] = state == "settled"
        return item

    def list_transfers(self, as_of: str | None = None) -> list[dict[str, Any]]:
        d_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, a.name AS from_name, b.name AS to_name
                FROM transfers t
                JOIN accounts a ON a.id=t.from_account_id
                JOIN accounts b ON b.id=t.to_account_id
                ORDER BY t.id DESC
                """,
            ).fetchall()
            return [self._enrich_transfer(conn, row, d_iso) for row in rows]

    def transfer_detail(self, transfer_id: int, as_of: str | None = None) -> dict[str, Any]:
        d_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            row = self._transfer_row(conn, transfer_id)
            result = self._enrich_transfer(conn, row, d_iso)
            result["source"] = self._snapshot(conn, int(row["from_account_id"]), d_iso)
            result["target"] = self._snapshot(conn, int(row["to_account_id"]), d_iso)
        return result

    # ----------------------------------------------------------------- usage

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
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        d_iso = occurred.isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            self._valid_on(account, d_iso, "取水账户")
            # Same-day approved transfers are already part of the walk; they
            # settle first, so this reading is refused instead of overflowing.
            conflicts = self._ledger_conflicts(
                conn, account_id,
                usage_candidate={"amount": amount, "effective_date": d_iso, "meter_event_id": meter_event_id},
            )
            if conflicts:
                raise self._first_conflict_error(conflicts, "取水登记失败")
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, d_iso, actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": d_iso, "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
            result = dict(row)
            result["snapshot"] = self._snapshot(conn, account_id, d_iso)
            return result

    # --------------------------------------------------------------- drought

    def simulate_drought(self, total_supply: float, reduction: float = 0.0,
                         as_of: str | None = None, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        d_iso = resolve_as_of(as_of)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE valid_from<=? AND valid_to>=? ORDER BY priority,name",
                (d_iso, d_iso),
            ).fetchall()
            entries = []
            for row in rows:
                snapshot = self._snapshot(conn, int(row["id"]), d_iso)
                entries.append({"row": row, "remaining": max(0.0, snapshot["settled_quota"] - snapshot["used"]),
                                "snapshot": snapshot})
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining_supply = supply
        priorities = sorted({int(e["row"]["priority"]) for e in entries})
        for priority in priorities:
            group = [e for e in entries if int(e["row"]["priority"]) == priority]
            requested = sum(e["remaining"] for e in group)
            if requested <= 0:
                for e in group:
                    allocation[int(e["row"]["id"])] = 0.0
                    deficit[int(e["row"]["id"])] = 0.0
                continue
            take = min(remaining_supply, requested)
            for e in group:
                share = take * e["remaining"] / requested
                allocation[int(e["row"]["id"])] = share
                deficit[int(e["row"]["id"])] = e["remaining"] - share
            remaining_supply -= take
            if remaining_supply <= EPS:
                for lower in entries:
                    if int(lower["row"]["priority"]) > priority:
                        allocation[int(lower["row"]["id"])] = 0.0
                        deficit[int(lower["row"]["id"])] = lower["remaining"]
                break
        return {"as_of": d_iso, "total_supply": total_supply, "reduction": reduction,
                "effective_supply": supply, "unallocated": max(0.0, remaining_supply),
                "allocations": [
                    {"account_id": int(e["row"]["id"]), "name": e["row"]["name"],
                     "priority": e["row"]["priority"], "settled_quota": e["snapshot"]["settled_quota"],
                     "used": e["snapshot"]["used"], "remaining": e["remaining"],
                     "allocation": allocation.get(int(e["row"]["id"]), 0.0),
                     "deficit": deficit.get(int(e["row"]["id"]), 0.0)}
                    for e in entries
                ]}

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/2.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, exc: DomainError) -> None:
        payload: dict[str, Any] = {"error": str(exc)}
        if exc.details:
            payload["details"] = exc.details
        self._send(payload, exc.status)

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
        as_of = q.get("as_of", [None])[0]
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parts == ["api", "accounts"]:
                return self._send({"as_of": resolve_as_of(as_of), "accounts": self.db.list_accounts(as_of)})
            if parts == ["api", "transfers"]:
                return self._send({"as_of": resolve_as_of(as_of), "transfers": self.db.list_transfers(as_of)})
            if len(parts) == 3 and parts[:2] == ["api", "transfers"]:
                return self._send(self.db.transfer_detail(int(parts[2]), as_of))
            if len(parts) == 4 and parts[:2] == ["api", "accounts"] and parts[3] == "available":
                return self._send(self.db.available(int(parts[2]), as_of))
            if parts == ["api", "audit"]:
                return self._send({"audit": self.db.audit()})
            if parts == ["api", "drought", "simulate"]:
                return self._send(self.db.simulate_drought(
                    float(q.get("supply", ["0"])[0]),
                    float(q.get("reduction", ["0"])[0]),
                    q.get("as_of", [None])[0],
                ))
            raise DomainError("接口不存在", 404)
        except DomainError as exc:
            self._send_error(exc)
        except ValueError:
            self._send_error(DomainError("URL 中的编号或日期不合法"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        as_of = q.get("as_of", [None])[0]
        parts = [p for p in parsed.path.split("/") if p]
        try:
            actor, role = self._auth()
            body = self._body()
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
                return self._send(self.db.cancel_transfer(int(parts[2]), actor, role, as_of))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reschedule":
                return self._send(self.db.reschedule_transfer(int(parts[2]), actor, body, role, as_of))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            raise DomainError("接口不存在", 404)
        except DomainError as exc:
            self._send_error(exc)
        except (ValueError, TypeError) as exc:
            self._send_error(DomainError(f"请求参数不合法：{exc}"))

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
