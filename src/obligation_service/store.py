"""SQLite 存储与架构。

所有写操作走 ``BEGIN IMMEDIATE`` 短事务；提醒、逾期案件、执行记录
均带业务唯一约束，从数据库层面保证重复调度不产生重复数据。
历史类表（版本、修订、裁决、事件、审计日志）只追加、不更新不删除。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,              -- draft | active | superseded
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    note TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
);

CREATE TABLE IF NOT EXISTS obligations (
    duty_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    code TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, code)
);

CREATE TABLE IF NOT EXISTS obligation_revisions (
    duty_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    revision_type TEXT NOT NULL,       -- register | extend | exempt | transfer | amend
    effective_date TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT,
    predecessor_version INTEGER,
    payload TEXT NOT NULL,
    PRIMARY KEY (duty_id, version)
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    duty_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    duty_version INTEGER NOT NULL,
    submitted_by TEXT NOT NULL,
    submitter_party TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    note TEXT,
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    duty_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    duty_version INTEGER NOT NULL,
    decision TEXT NOT NULL,            -- approved | rejected
    verifier_role TEXT NOT NULL,
    verifier_party TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    comment TEXT,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duty_states (
    duty_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | fulfilled | rejected
    confirmation_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (duty_id, window_key)
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    duty_id TEXT NOT NULL,
    duty_version INTEGER NOT NULL,
    window_key TEXT NOT NULL,
    kind TEXT NOT NULL,                -- upcoming | due | overdue
    scheduled_for TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | sent | suppressed | superseded
    reason TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE (duty_id, duty_version, kind, window_key, scheduled_for)
);

CREATE TABLE IF NOT EXISTS overdue_cases (
    case_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    duty_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    opened_on TEXT NOT NULL,
    due_date TEXT NOT NULL,
    owner_party TEXT NOT NULL,
    current_duty_version INTEGER NOT NULL,
    status TEXT NOT NULL,              -- open | resolved
    resolution TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (duty_id, window_key)
);

CREATE TABLE IF NOT EXISTS case_events (
    case_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    event_type TEXT NOT NULL,          -- opened | deadline_moved | transferred | reopened | resolved
    duty_version INTEGER NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY (case_id, seq)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                -- dispatch | recalculate
    range_start TEXT,
    range_end TEXT,
    requested_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,              -- running | done | failed
    detail TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_role TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    run_id TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_sched ON reminders(scheduled_for, status);
CREATE INDEX IF NOT EXISTS idx_reminders_duty ON reminders(duty_id, window_key);
CREATE INDEX IF NOT EXISTS idx_cases_status ON overdue_cases(status);
CREATE INDEX IF NOT EXISTS idx_revisions_duty ON obligation_revisions(duty_id, version);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id, seq);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """封装 SQLite 连接与基础访问。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # 进程内互斥：HTTP 多线程共享连接时，保证事务不嵌套、游标不交错。
        # 跨进程/多实例则由 BEGIN IMMEDIATE + busy_timeout + UNIQUE 约束保证。
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化写事务：进程锁 + ``BEGIN IMMEDIATE`` 保证调度互斥。"""
        conn = self._conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ---- 通用小工具 ----

    def audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        actor_role: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(at, actor, actor_role, action, entity_type, entity_id, run_id, payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                utc_now_iso(),
                actor,
                actor_role,
                action,
                entity_type,
                entity_id,
                run_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        self._conn.close()
