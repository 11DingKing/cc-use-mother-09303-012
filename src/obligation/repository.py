"""数据仓储：在 Store 之上提供领域读取与重建。"""
from __future__ import annotations

import sqlite3
from typing import Any

from .models import (
    ObligationKind,
    ObligationSpec,
    User,
)
from .storage import Store, loads


class Repository:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---- 用户 / 令牌 ----------------------------------------------------

    def upsert_user(self, user: User, token: str | None = None) -> None:
        with self.store.begin() as c:
            c.execute(
                "INSERT INTO users(user_id,name,role,party_id) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, role=excluded.role, party_id=excluded.party_id",
                (user.user_id, user.name, user.role, user.party_id),
            )
            if token:
                c.execute(
                    "INSERT INTO auth_tokens(token,user_id) VALUES(?,?) "
                    "ON CONFLICT(token) DO UPDATE SET user_id=excluded.user_id",
                    (token, user.user_id),
                )

    def get_user(self, user_id: str) -> User | None:
        row = self.store.query_one("SELECT * FROM users WHERE user_id=?", (user_id,))
        return self._user(row) if row else None

    def user_by_token(self, token: str) -> User | None:
        row = self.store.query_one(
            "SELECT u.* FROM users u JOIN auth_tokens t ON t.user_id=u.user_id WHERE t.token=?",
            (token,),
        )
        return self._user(row) if row else None

    def add_verifier_role(self, user_id: str, role_code: str) -> None:
        with self.store.begin() as c:
            c.execute(
                "INSERT OR IGNORE INTO user_verifier_roles(user_id,role_code) VALUES(?,?)",
                (user_id, role_code),
            )

    def verifier_roles(self, user_id: str) -> set[str]:
        rows = self.store.query_all(
            "SELECT role_code FROM user_verifier_roles WHERE user_id=?", (user_id,)
        )
        return {r["role_code"] for r in rows}

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            user_id=row["user_id"], name=row["name"], role=row["role"], party_id=row["party_id"]
        )

    # ---- 协议 -----------------------------------------------------------

    def create_agreement(self, agreement_id: str, code: str, title: str, created_at: str) -> None:
        with self.store.begin() as c:
            c.execute(
                "INSERT INTO agreements(agreement_id,code,title,created_at) VALUES(?,?,?,?)",
                (agreement_id, code, title, created_at),
            )

    def get_agreement(self, agreement_id: str) -> sqlite3.Row | None:
        return self.store.query_one(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        )

    def require_agreement(self, agreement_id: str) -> sqlite3.Row:
        row = self.get_agreement(agreement_id)
        if row is None:
            from .errors import NotFoundError

            raise NotFoundError(f"协议不存在：{agreement_id}")
        return row

    def list_agreements(self) -> list[sqlite3.Row]:
        return self.store.query_all("SELECT * FROM agreements ORDER BY agreement_id")

    # ---- 计划版本 -------------------------------------------------------

    def latest_plan_version_no(self, agreement_id: str) -> int:
        row = self.store.query_one(
            "SELECT COALESCE(MAX(version_no),0) AS v FROM plan_versions WHERE agreement_id=?",
            (agreement_id,),
        )
        return int(row["v"])

    def insert_plan_version(
        self, agreement_id: str, version_no: int, created_at: str, created_by: str, summary: str
    ) -> int:
        with self.store.begin() as c:
            cur = c.execute(
                "INSERT INTO plan_versions(agreement_id,version_no,created_at,created_by,change_summary)"
                " VALUES(?,?,?,?,?)",
                (agreement_id, version_no, created_at, created_by, summary),
            )
            return int(cur.lastrowid)

    def insert_obligation_version(self, plan_version_id: int, agreement_id: str, spec: ObligationSpec) -> None:
        self.store.execute(
            "INSERT INTO obligation_versions(plan_version_id,agreement_id,code,spec_json) VALUES(?,?,?,?)",
            (plan_version_id, agreement_id, spec.code, __import__("json").dumps(spec.to_dict(), ensure_ascii=False)),
        )

    def latest_specs(self, agreement_id: str) -> dict[str, ObligationSpec]:
        """取每个义务代码在最新计划版本中的快照。"""
        rows = self.store.query_all(
            """
            SELECT ov.spec_json AS spec_json
            FROM obligation_versions ov
            JOIN (
                SELECT code, MAX(plan_version_id) AS max_pv
                FROM obligation_versions
                WHERE agreement_id=?
                GROUP BY code
            ) latest ON latest.max_pv = ov.plan_version_id AND latest.code = ov.code
            WHERE ov.agreement_id=?
            """,
            (agreement_id, agreement_id),
        )
        import json

        return {
            spec.code: spec
            for spec in (ObligationSpec.from_dict(json.loads(r["spec_json"])) for r in rows)
        }

    def spec_at_version(self, agreement_id: str, version_no: int) -> dict[str, ObligationSpec]:
        rows = self.store.query_all(
            """
            SELECT ov.spec_json AS spec_json
            FROM obligation_versions ov
            JOIN plan_versions pv ON pv.id=ov.plan_version_id
            WHERE ov.agreement_id=? AND pv.version_no=?
            """,
            (agreement_id, version_no),
        )
        import json

        return {
            spec["code"]: ObligationSpec.from_dict(spec)
            for spec in (json.loads(r["spec_json"]) for r in rows)
        }

    def list_plan_versions(self, agreement_id: str) -> list[sqlite3.Row]:
        return self.store.query_all(
            "SELECT * FROM plan_versions WHERE agreement_id=? ORDER BY version_no",
            (agreement_id,),
        )

    # ---- 生效状态 -------------------------------------------------------

    def get_effective(self, agreement_id: str, code: str) -> sqlite3.Row | None:
        return self.store.query_one(
            "SELECT * FROM effective_state WHERE agreement_id=? AND code=?",
            (agreement_id, code),
        )

    def all_effective(self, agreement_id: str) -> list[sqlite3.Row]:
        return self.store.query_all(
            "SELECT * FROM effective_state WHERE agreement_id=? ORDER BY code",
            (agreement_id,),
        )

    def upsert_effective(
        self,
        conn: sqlite3.Connection,
        agreement_id: str,
        code: str,
        version_no: int,
        status: str,
        effective_due: str,
        generation: int,
        waived_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO effective_state(agreement_id,code,latest_version_no,status,effective_due,
                                        generation,waived_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(agreement_id,code) DO UPDATE SET
                latest_version_no=excluded.latest_version_no,
                status=excluded.status,
                effective_due=excluded.effective_due,
                generation=excluded.generation,
                waived_at=COALESCE(excluded.waived_at, effective_state.waived_at),
                completed_at=COALESCE(excluded.completed_at, effective_state.completed_at)
            """,
            (agreement_id, code, version_no, status, effective_due, generation, waived_at, completed_at),
        )

    # ---- 事件 -----------------------------------------------------------

    def insert_event(
        self,
        conn: sqlite3.Connection,
        occurred_at: str,
        agreement_id: str,
        event_type: str,
        actor_user_id: str,
        payload: dict[str, Any],
        obligation_code: str | None = None,
    ) -> None:
        import json

        conn.execute(
            "INSERT INTO events(occurred_at,agreement_id,obligation_code,event_type,actor_user_id,payload_json)"
            " VALUES(?,?,?,?,?,?)",
            (occurred_at, agreement_id, obligation_code, event_type, actor_user_id,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def list_events(self, agreement_id: str, code: str | None = None) -> list[sqlite3.Row]:
        if code:
            return self.store.query_all(
                "SELECT * FROM events WHERE agreement_id=? AND obligation_code=? ORDER BY id",
                (agreement_id, code),
            )
        return self.store.query_all(
            "SELECT * FROM events WHERE agreement_id=? ORDER BY id", (agreement_id,)
        )

    # ---- 提醒 / 投递 ----------------------------------------------------

    def active_reminders(self, agreement_id: str, code: str) -> list[sqlite3.Row]:
        return self.store.query_all(
            "SELECT * FROM reminders WHERE agreement_id=? AND code=? AND superseded_at IS NULL ORDER BY fire_date,seq_no",
            (agreement_id, code),
        )

    def all_reminders(self, agreement_id: str, code: str | None = None) -> list[sqlite3.Row]:
        if code:
            return self.store.query_all(
                "SELECT * FROM reminders WHERE agreement_id=? AND code=? ORDER BY generation,fire_date,seq_no",
                (agreement_id, code),
            )
        return self.store.query_all(
            "SELECT * FROM reminders WHERE agreement_id=? ORDER BY code,generation,fire_date,seq_no",
            (agreement_id,),
        )

    def get_delivery(self, reminder_id: int) -> sqlite3.Row | None:
        return self.store.query_one(
            "SELECT * FROM deliveries WHERE reminder_id=? AND status<>'failed' ORDER BY id LIMIT 1",
            (reminder_id,),
        )

    def pending_deliveries(self, include_failed: bool = False) -> list[sqlite3.Row]:
        if include_failed:
            return self.store.query_all(
                "SELECT * FROM deliveries WHERE status IN ('pending','failed') ORDER BY id"
            )
        return self.store.query_all(
            "SELECT * FROM deliveries WHERE status='pending' ORDER BY id"
        )

    # ---- 证明 -----------------------------------------------------------

    def insert_submission(
        self, agreement_id: str, code: str, proof_type: str, file_ref: str | None,
        attributes: dict[str, Any], submitted_by: str, submitted_at: str,
    ) -> int:
        import json

        with self.store.begin() as c:
            cur = c.execute(
                "INSERT INTO submissions(agreement_id,code,proof_type,file_ref,attributes_json,"
                "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?)",
                (agreement_id, code, proof_type, file_ref,
                 json.dumps(attributes, ensure_ascii=False), submitted_by, submitted_at),
            )
            return int(cur.lastrowid)

    def get_submission(self, submission_id: int) -> sqlite3.Row | None:
        return self.store.query_one("SELECT * FROM submissions WHERE id=?", (submission_id,))

    def conforming_proof_count(self, agreement_id: str, code: str, proof_type: str) -> int:
        row = self.store.query_one(
            "SELECT COUNT(*) AS n FROM submissions WHERE agreement_id=? AND code=? AND proof_type=? AND conforming=1",
            (agreement_id, code, proof_type),
        )
        return int(row["n"])

    def list_submissions(self, agreement_id: str, code: str | None = None) -> list[sqlite3.Row]:
        if code:
            return self.store.query_all(
                "SELECT * FROM submissions WHERE agreement_id=? AND code=? ORDER BY id",
                (agreement_id, code),
            )
        return self.store.query_all(
            "SELECT * FROM submissions WHERE agreement_id=? ORDER BY id", (agreement_id,)
        )

    # ---- 重算 -----------------------------------------------------------

    def record_recalc(
        self, agreement_id: str, start: str, end: str, actor: str, ran_at: str, result: dict[str, Any]
    ) -> int:
        import json

        with self.store.begin() as c:
            cur = c.execute(
                "INSERT INTO recalc_runs(agreement_id,range_start,range_end,actor_user_id,ran_at,result_json)"
                " VALUES(?,?,?,?,?,?)",
                (agreement_id, start, end, actor, ran_at,
                 json.dumps(result, ensure_ascii=False, sort_keys=True)),
            )
            return int(cur.lastrowid)
