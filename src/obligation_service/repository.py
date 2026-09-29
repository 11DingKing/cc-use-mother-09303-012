"""仓储：协议、计划、义务版本链与核验状态的读写。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from .errors import NotFoundError
from .models import ObligationRevision
from .util import iso_date, parse_date


def row_to_revision(row: sqlite3.Row) -> ObligationRevision:
    payload = json.loads(row["payload"])
    return ObligationRevision.from_payload(
        payload,
        version=row["version"],
        revision_type=row["revision_type"],
        effective_date=parse_date(row["effective_date"]),
        created_by=row["created_by"],
        reason=row["reason"] or "",
        predecessor_version=row["predecessor_version"],
    )


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- 协议 / 计划 ----

    def get_agreement(self, agreement_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"协议不存在：{agreement_id}")
        return row

    def get_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"计划不存在：{plan_id}")
        return row

    def list_plan_versions(self, plan_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM plan_versions WHERE plan_id=? ORDER BY version", (plan_id,)
        ).fetchall()

    # ---- 义务 ----

    def list_duties(self, plan_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM obligations WHERE plan_id=? ORDER BY code", (plan_id,)
        ).fetchall()

    def get_duty(self, duty_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM obligations WHERE duty_id=?", (duty_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"义务不存在：{duty_id}")
        return row

    def get_duty_by_code(self, plan_id: str, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM obligations WHERE plan_id=? AND code=?", (plan_id, code)
        ).fetchone()

    def list_revisions(self, duty_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM obligation_revisions WHERE duty_id=? ORDER BY version", (duty_id,)
        ).fetchall()

    def latest_revision(self, duty_id: str) -> ObligationRevision:
        row = self.conn.execute(
            "SELECT * FROM obligation_revisions WHERE duty_id=? ORDER BY version DESC LIMIT 1",
            (duty_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"义务没有任何版本：{duty_id}")
        return row_to_revision(row)

    def effective_revision(self, duty_id: str, on_date: date) -> ObligationRevision:
        """返回 on_date 当日生效的最新修订版本（旧版本永不被抹除）。"""
        row = self.conn.execute(
            "SELECT * FROM obligation_revisions WHERE duty_id=? AND effective_date<=?"
            " ORDER BY version DESC LIMIT 1",
            (duty_id, iso_date(on_date)),
        ).fetchone()
        if row is None:
            # 生效日晚于 on_date：回退到最早版本，保证历史区间可求值
            row = self.conn.execute(
                "SELECT * FROM obligation_revisions WHERE duty_id=? ORDER BY version ASC LIMIT 1",
                (duty_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"义务没有任何版本：{duty_id}")
        return row_to_revision(row)

    # ---- 期次状态 / 提交 / 确认 ----

    def get_state(self, duty_id: str, window_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM duty_states WHERE duty_id=? AND window_key=?", (duty_id, window_key)
        ).fetchone()

    def is_fulfilled(self, duty_id: str, window_key: str) -> bool:
        row = self.get_state(duty_id, window_key)
        return row is not None and row["status"] == "fulfilled"

    def list_confirmations(self, duty_id: str, window_key: str) ->list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM confirmations WHERE duty_id=? AND window_key=? ORDER BY decided_at",
            (duty_id, window_key),
        ).fetchall()

    def latest_confirmation(self, duty_id: str, window_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM confirmations WHERE duty_id=? AND window_key=?"
            " ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (duty_id, window_key),
        ).fetchone()

    def list_submissions(self, duty_id: str, window_key: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM submissions WHERE duty_id=? AND window_key=? ORDER BY submitted_at",
            (duty_id, window_key),
        ).fetchall()

    # ---- 提醒 / 案件 ----

    def find_reminder(
        self, duty_id: str, version: int, kind: str, window_key: str, scheduled_for: str
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE duty_id=? AND duty_version=? AND kind=?"
            " AND window_key=? AND scheduled_for=?",
            (duty_id, version, kind, window_key, scheduled_for),
        ).fetchone()

    def get_case(self, duty_id: str, window_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM overdue_cases WHERE duty_id=? AND window_key=?", (duty_id, window_key)
        ).fetchone()

    def next_case_seq(self, conn: sqlite3.Connection, case_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()
        return int(row["next"])

    # ---- 审计读取 ----

    def list_audit(self, entity_type: str, entity_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM audit_log WHERE entity_type=? AND entity_id=? ORDER BY seq",
            (entity_type, entity_id),
        ).fetchall()

    def list_case_events(self, case_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM case_events WHERE case_id=? ORDER BY seq", (case_id,)
        ).fetchall()
