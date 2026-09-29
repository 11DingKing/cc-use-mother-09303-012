"""SQLite 存储层。

设计要点：
- plan_versions / obligation_versions 只追加，旧版本永不更新或删除；
- events 为追加型审计日志，延期、豁免、移交、核验全部留痕；
- reminders 与 deliveries 上的唯一约束保证重复调度 / 崩溃恢复不产生多条提醒；
- deliveries 同时承担 outbox 职责：pending 待投递，sent 已投递，崩溃后重放安全。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('admin','party_member','verifier')),
    party_id  TEXT
);

-- 登录令牌（演示用；生产应由鉴权中间件替换）。
CREATE TABLE IF NOT EXISTS auth_tokens (
    token   TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id)
);

-- 核验人员被授权的核验角色代码（如 equipment_inspector、academic_reviewer）。
CREATE TABLE IF NOT EXISTS user_verifier_roles (
    user_id   TEXT NOT NULL REFERENCES users(user_id),
    role_code TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (user_id, role_code)
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id   TEXT NOT NULL REFERENCES agreements(agreement_id),
    version_no     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    UNIQUE (agreement_id, version_no)
);

CREATE TABLE IF NOT EXISTS obligation_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions(id),
    agreement_id    TEXT NOT NULL,
    code            TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    UNIQUE (plan_version_id, code)
);

-- 派生缓存：始终可由 plan_versions + events 重建，仅为查询便利而维护。
-- generation：计划代次。登记=1；每次延期/移交/产生新计划版本使旧提醒作废时 +1。
CREATE TABLE IF NOT EXISTS effective_state (
    agreement_id      TEXT NOT NULL,
    code              TEXT NOT NULL,
    latest_version_no INTEGER NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL CHECK (status IN ('active','waived','completed')),
    effective_due     TEXT NOT NULL,   -- 基础截止日叠加全部延期后的当前截止日
    waived_at         TEXT,
    completed_at      TEXT,
    PRIMARY KEY (agreement_id, code)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at     TEXT NOT NULL,
    agreement_id    TEXT NOT NULL,
    obligation_code TEXT,
    event_type      TEXT NOT NULL,
    actor_user_id   TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL,
    code          TEXT NOT NULL,
    generation    INTEGER NOT NULL DEFAULT 1,
    reminder_type TEXT NOT NULL CHECK (reminder_type IN ('advance','due','overdue')),
    seq_no        INTEGER NOT NULL,            -- 提前提醒档位 / 逾期第几轮
    due_date      TEXT NOT NULL,               -- 生成时所依据的截止日
    fire_date     TEXT NOT NULL,               -- 应触发日（幂等区间定位键）
    owner_user_id TEXT NOT NULL,               -- 生成当时的负责人；移交后旧单仍指向离任人
    recipient     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_at TEXT,                        -- 延期/移交后被新一代次取代；保留不删
    UNIQUE (agreement_id, code, generation, reminder_type, seq_no, due_date)
);

-- 活跃（未被取代）提醒每个档位仅一条；同代次重复调度直接命中此约束。
CREATE UNIQUE INDEX IF NOT EXISTS ux_reminders_active
    ON reminders(agreement_id, code, generation, reminder_type, seq_no)
    WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id     INTEGER NOT NULL REFERENCES reminders(id),
    channel         TEXT NOT NULL DEFAULT 'calendar',
    recipient       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending','sent','failed')),
    idempotency_key TEXT NOT NULL UNIQUE,      -- 对日历网关的去重键
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    sent_at         TEXT,
    created_at      TEXT NOT NULL
);
-- 同一提醒对同一接收人只允许一条有效投递（失败的可重试新单）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_deliveries_active
    ON deliveries(reminder_id, channel, recipient)
    WHERE status <> 'failed';

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id     TEXT NOT NULL,
    code             TEXT NOT NULL,
    proof_type       TEXT NOT NULL,
    file_ref         TEXT,
    attributes_json  TEXT NOT NULL DEFAULT '{}',
    submitted_by     TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    verifier_user_id TEXT,
    verified_at      TEXT,
    conforming       INTEGER,                  -- NULL 未核验；1/0 核验结论
    reject_reason    TEXT
);

CREATE TABLE IF NOT EXISTS recalc_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id TEXT NOT NULL,
    range_start  TEXT NOT NULL,
    range_end    TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    ran_at       TEXT NOT NULL,
    result_json  TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 封装；写操作集中在一把锁内串行化。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ---- 基础工具 -------------------------------------------------------

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchall()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def begin(self) -> "Transaction":
        return Transaction(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Transaction:
    """显式事务（BEGIN IMMEDIATE），配合 Store 锁使用。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str) -> Any:
    return json.loads(value)


def to_date_text(value: date) -> str:
    return value.isoformat()


def now_text(value: datetime) -> str:
    return iso(value)
