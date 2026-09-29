"""HTTP 应用（标准库实现，无第三方依赖）。

身份与角色通过请求头传递：
    X-Actor-Id / X-Actor-Name / X-Actor-Role / X-Actor-Party
POST 请求可带 ``Idempotency-Key``，同键重放返回首次结果，不重复执行。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .clock import Clock
from .errors import DomainError
from .models import Actor
from .scheduler import Scheduler
from .services import RegistrationService
from .store import Store, utc_now_iso
from .verification import VerificationService


class AppContext:
    def __init__(self, store: Store, clock: Clock, transport=None) -> None:
        self.store = store
        self.clock = clock
        self.registration = RegistrationService(store, clock)
        self.verification = VerificationService(store, clock)
        self.scheduler = Scheduler(store, clock, transport=transport)
        # 单 SQLite 连接：请求级串行，避免多线程游标交错
        self.request_lock = __import__("threading").RLock()


def _json_body(handler: "Handler") -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise DomainError("请求体必须是 JSON 对象")
    return value


class Handler(BaseHTTPRequestHandler):
    ctx: AppContext

    # ---- 框架胶水 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[-1] for k, v in parsed.items()}

    def _path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def _actor_required(self) -> Actor:
        return Actor.from_headers(self.headers)

    def _actor_optional(self) -> Actor | None:
        if self.headers.get("X-Actor-Id"):
            return Actor.from_headers(self.headers)
        return None

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            with self.ctx.request_lock:
                path = self._path()
                qs = self._query()
                body = _json_body(self) if method == "POST" else {}

                # 幂等键：同键重放直接返回首次响应
                idem_key = self.headers.get("Idempotency-Key")
                if method == "POST" and idem_key:
                    cached = self._idempotency_lookup(idem_key)
                    if cached is not None:
                        self._send(cached[0], cached[1])
                        return

                status, response = self._route(method, path, qs, body)
                if method == "POST" and idem_key:
                    self._idempotency_store(idem_key, status, response)
            self._send(status, response)
        except DomainError as exc:
            self._send(exc.status, {"error": exc.code, "message": str(exc)})
        except Exception as exc:  # 防御：不让连接挂死
            self._send(500, {"error": "internal_error", "message": repr(exc)})

    def _idempotency_lookup(self, key: str) -> tuple[int, Any] | None:
        row = self.ctx.store.fetchone(
            "SELECT response_status, response_body FROM idempotency_keys WHERE key=?", (key,)
        )
        if row is None:
            return None
        return row["response_status"], json.loads(row["response_body"])

    def _idempotency_store(self, key: str, status: int, response: Any) -> None:
        try:
            with self.ctx.store.tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys(key, actor, created_at,"
                    " response_status, response_body) VALUES (?,?,?,?,?)",
                    (key, self.headers.get("X-Actor-Id", "unknown"), utc_now_iso(),
                     status, json.dumps(response, ensure_ascii=False, sort_keys=True)),
                )
        except sqlite3.Error:
            pass

    def _route(self, method, path, qs, body) -> tuple[int, Any]:
        routes: list[tuple[str, str, Callable]] = [
            ("GET", "^/health$", self._health),
            ("POST", "^/agreements$", self._create_agreement),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/plans$", self._register_plan),
            ("GET", r"^/plans/(?P<plan_id>[^/]+)$", self._get_plan),
            ("GET", r"^/plans/(?P<plan_id>[^/]+)/duties$", self._list_duties),
            ("GET", r"^/plans/(?P<plan_id>[^/]+)/audit$", self._plan_audit),
            ("GET", r"^/duties/(?P<duty_id>[^/]+)$", self._get_duty),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/extend$", self._extend),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/exempt$", self._exempt),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/transfer$", self._transfer),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/amend$", self._amend),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/submissions$", self._submit),
            ("POST", r"^/duties/(?P<duty_id>[^/]+)/confirmations$", self._decide),
            ("GET", r"^/duties/(?P<duty_id>[^/]+)/trail$", self._trail),
            ("GET", r"^/duties/(?P<duty_id>[^/]+)/explain$", self._explain),
            ("POST", "^/scheduler/dispatch$", self._dispatch),
            ("POST", "^/scheduler/recalculate$", self._recalculate),
            ("GET", "^/cases$", self._list_cases),
            ("GET", r"^/cases/(?P<case_id>[^/]+)$", self._get_case),
            ("GET", "^/reminders$", self._list_reminders),
            ("GET", r"^/runs/(?P<run_id>[^/]+)$", self._get_run),
        ]
        for m, pattern, fn in routes:
            if m != method:
                continue
            match = re.match(pattern + "$", path)
            if match:
                return fn(qs, body, **match.groupdict())
        return 404, {"error": "not_found", "message": f"无此路由：{method} {path}"}

    # ---- 处理器 ----

    def _health(self, qs, body) -> tuple[int, Any]:
        return 200, {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    def _create_agreement(self, qs, body) -> tuple[int, Any]:
        actor = self._actor_required()
        return 201, self.ctx.registration.create_agreement(actor, body)

    def _register_plan(self, qs, body, agreement_id) -> tuple[int, Any]:
        actor = self._actor_required()
        return 201, self.ctx.registration.register_plan(actor, agreement_id, body)

    def _get_plan(self, qs, body, plan_id) -> tuple[int, Any]:
        repo_rows = self.ctx.store.fetchall(
            "SELECT pv.* FROM plan_versions pv WHERE pv.plan_id=? ORDER BY pv.version", (plan_id,)
        )
        plan = self.ctx.store.fetchone("SELECT * FROM plans WHERE plan_id=?", (plan_id,))
        if plan is None:
            from .errors import NotFoundError

            raise NotFoundError(f"计划不存在：{plan_id}")
        return 200, {
            "plan_id": plan_id,
            "agreement_id": plan["agreement_id"],
            "current_version": plan["current_version"],
            "created_at": plan["created_at"],
            "versions": [
                {"version": r["version"], "status": r["status"], "created_by": r["created_by"],
                 "created_at": r["created_at"], "activated_at": r["activated_at"],
                 "note": r["note"], "payload": json.loads(r["payload"])}
                for r in repo_rows
            ],
        }

    def _list_duties(self, qs, body, plan_id) -> tuple[int, Any]:
        return 200, {"duties": self.ctx.registration.list_plan_duties(plan_id)}

    def _plan_audit(self, qs, body, plan_id) -> tuple[int, Any]:
        plan = self.ctx.store.fetchone("SELECT plan_id FROM plans WHERE plan_id=?", (plan_id,))
        if plan is None:
            from .errors import NotFoundError

            raise NotFoundError(f"计划不存在：{plan_id}")
        duty_ids = [r["duty_id"] for r in self.ctx.store.fetchall(
            "SELECT duty_id FROM obligations WHERE plan_id=?", (plan_id,))]
        placeholders = ",".join("?" for _ in duty_ids) if duty_ids else "''"
        rows = self.ctx.store.fetchall(
            f"SELECT * FROM audit_log WHERE entity_id=? OR entity_id IN ({placeholders})"
            " ORDER BY seq DESC LIMIT 500",
            (plan_id, *duty_ids),
        )
        return 200, {"audit": [_audit_row(r) for r in rows]}

    def _get_duty(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.registration.get_duty_detail(duty_id)

    def _extend(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.registration.extend_deadline(self._actor_required(), duty_id, body)

    def _exempt(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.registration.grant_exemption(self._actor_required(), duty_id, body)

    def _transfer(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.registration.transfer_ownership(self._actor_required(), duty_id, body)

    def _amend(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.registration.amend(self._actor_required(), duty_id, body)

    def _submit(self, qs, body, duty_id) -> tuple[int, Any]:
        return 201, self.ctx.verification.submit_evidence(self._actor_required(), duty_id, body)

    def _decide(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.verification.decide(self._actor_required(), duty_id, body)

    def _trail(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.verification.trail(duty_id, qs.get("window"))

    def _explain(self, qs, body, duty_id) -> tuple[int, Any]:
        return 200, self.ctx.scheduler.explain(duty_id, qs.get("as_of"))

    def _dispatch(self, qs, body) -> tuple[int, Any]:
        actor = self._actor_optional()
        return 200, self.ctx.scheduler.dispatch(actor, plan_id=body.get("plan_id") or qs.get("plan_id"))

    def _recalculate(self, qs, body) -> tuple[int, Any]:
        actor = self._actor_required()
        range_start = body.get("range_start") or qs.get("range_start")
        range_end = body.get("range_end") or qs.get("range_end")
        if not range_start or not range_end:
            from .errors import ValidationError

            raise ValidationError("recalculate 需要 range_start 与 range_end")
        return 200, self.ctx.scheduler.recalculate(
            actor,
            range_start=range_start,
            range_end=range_end,
            plan_id=body.get("plan_id"),
        )

    def _list_cases(self, qs, body) -> tuple[int, Any]:
        sql = (
            "SELECT c.*, o.code AS duty_code FROM overdue_cases c"
            " JOIN obligations o ON o.duty_id=c.duty_id WHERE 1=1"
        )
        params: list[Any] = []
        if qs.get("status"):
            sql += " AND c.status=?"
            params.append(qs["status"])
        if qs.get("plan_id"):
            sql += " AND c.plan_id=?"
            params.append(qs["plan_id"])
        sql += " ORDER BY c.opened_on DESC, c.case_id"
        rows = self.ctx.store.fetchall(sql, tuple(params))
        return 200, {"cases": [dict(r) for r in rows]}

    def _get_case(self, qs, body, case_id) -> tuple[int, Any]:
        row = self.ctx.store.fetchone("SELECT * FROM overdue_cases WHERE case_id=?", (case_id,))
        if row is None:
            from .errors import NotFoundError

            raise NotFoundError(f"案件不存在：{case_id}")
        events = self.ctx.store.fetchall(
            "SELECT * FROM case_events WHERE case_id=? ORDER BY seq", (case_id,)
        )
        result = dict(row)
        result["events"] = [
            {"seq": e["seq"], "at": e["at"], "event": e["event_type"],
             "duty_version": e["duty_version"], "detail": json.loads(e["detail"])}
            for e in events
        ]
        return 200, result

    def _list_reminders(self, qs, body) -> tuple[int, Any]:
        sql = "SELECT * FROM reminders WHERE 1=1"
        params: list[Any] = []
        if qs.get("duty_id"):
            sql += " AND duty_id=?"
            params.append(qs["duty_id"])
        if qs.get("status"):
            sql += " AND status=?"
            params.append(qs["status"])
        sql += " ORDER BY scheduled_for DESC, reminder_id LIMIT 500"
        rows = self.ctx.store.fetchall(sql, tuple(params))
        return 200, {"reminders": [dict(r) for r in rows]}

    def _get_run(self, qs, body, run_id) -> tuple[int, Any]:
        row = self.ctx.store.fetchone("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if row is None:
            from .errors import NotFoundError

            raise NotFoundError(f"运行不存在：{run_id}")
        result = dict(row)
        if result.get("detail"):
            result["detail"] = json.loads(result["detail"])
        return 200, result


def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def build_server(host: str, port: int, ctx: AppContext) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    server = ThreadingHTTPServer((host, port), handler)
    return server
