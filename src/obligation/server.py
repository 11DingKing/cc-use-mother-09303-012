"""HTTP 服务端（标准库 http.server，零外部依赖）。

鉴权：Authorization: Bearer <token>，令牌在登记用户时签发。
授权：每个端点按用户 role 与核验角色独立判定。
时间：服务启动时注入 Clock；生产用 SystemClock，测试可用固定时钟启动。
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .calendar import CalendarGateway
from .clock import Clock, SystemClock
from .errors import DomainError
from .models import (
    ObligationKind,
    ObligationSpec,
    ProofRequirement,
    Prerequisite,
    ResponsibleParty,
    TimeWindow,
)
from .planner import PlannerService
from .repository import Repository
from .scheduler import SchedulerService
from .storage import Store
from .verification import VerificationService

UUID_RE = re.compile(r"^[A-Za-z0-9_\-\.]+$")


class Application:
    def __init__(self, store: Store, clock: Clock | None = None,
                 calendar: CalendarGateway | None = None) -> None:
        self.clock = clock or SystemClock()
        self.repo = Repository(store)
        self.planner = PlannerService(self.repo, self.clock)
        self.scheduler = SchedulerService(self.repo, self.clock, calendar)
        self.verification = VerificationService(self.repo, self.clock)

    # ---- 请求体 → 领域对象 ----------------------------------------------

    @staticmethod
    def parse_spec(body: dict[str, Any]) -> ObligationSpec:
        r = body["responsible"]
        w = body["window"]
        p = body["proof"]
        return ObligationSpec(
            code=body["code"],
            kind=ObligationKind(body["kind"]),
            title=body["title"],
            responsible=ResponsibleParty(
                party_id=r["party_id"], party_name=r.get("party_name", r["party_id"]),
                owner_user_id=r["owner_user_id"], owner_name=r.get("owner_name", ""),
                contact=r.get("contact", ""),
            ),
            window=TimeWindow(
                start_date=date.fromisoformat(w["start_date"]),
                due_date=date.fromisoformat(w["due_date"]),
                advance_days=tuple(w.get("advance_days", [7, 1])),
                overdue_interval_days=w.get("overdue_interval_days", 7),
                overdue_max_reminders=w.get("overdue_max_reminders", 12),
            ),
            proof=ProofRequirement(
                proof_type=p["proof_type"], description=p.get("description", ""),
                verifier_roles=tuple(p["verifier_roles"]),
                required_attributes=tuple(p.get("required_attributes", ())),
                minimum_count=p.get("minimum_count", 1),
            ),
            prerequisites=tuple(
                Prerequisite(pr["obligation_code"], pr.get("description", ""))
                for pr in body.get("prerequisites", ())
            ),
        )

    @staticmethod
    def parse_party(body: dict[str, Any]) -> ResponsibleParty:
        return ResponsibleParty(
            party_id=body["party_id"], party_name=body.get("party_name", body["party_id"]),
            owner_user_id=body["owner_user_id"], owner_name=body.get("owner_name", ""),
            contact=body.get("contact", ""),
        )


def create_app(db_path: str = ":memory:", clock: Clock | None = None,
               calendar: CalendarGateway | None = None) -> Application:
    return Application(Store(db_path), clock=clock, calendar=calendar)


# ---- HTTP 层 -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    app: Application  # 由 server 工厂注入到类

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默；错误仍在响应体
        return

    # -- 工具 --

    def _send_json(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _user(self):
        import base64

        from .models import User

        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            user = self.app.repo.user_by_token(auth[7:].strip())
            if user is not None:
                return user
        # 演示回退：Authorization: Basic <user_id> 方便命令行演练
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
                uid = decoded.split(":", 1)[0]
            except Exception:
                uid = ""
            user = self.app.repo.get_user(uid)
            if user is not None:
                return user
        from .errors import AuthorizationError

        raise AuthorizationError("缺少或无效的身份凭证")

    def _error_status(self, exc: Exception) -> int:
        from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError

        if isinstance(exc, AuthorizationError):
            return 403
        if isinstance(exc, NotFoundError):
            return 404
        if isinstance(exc, (ConflictError, ValidationError)):
            return 409
        return 400

    # -- 路由 --

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            user = self._user() if path != "/health" else None
            handler, kwargs = self._match(method, path)
            handler(user, kwargs, self._read_body() if method == "POST" else {})
        except Exception as exc:  # 领域错误可预期；其余归 500
            if isinstance(exc, DomainError):
                self._send_json(self._error_status(exc), {"error": type(exc).__name__, "message": str(exc)})
            else:
                self._send_json(500, {"error": "InternalError", "message": repr(exc)})

    def _match(self, method: str, path: str) -> tuple[Callable, dict[str, str]]:
        rules: list[tuple[str, str, Callable]] = [
            ("GET", r"^/health$", self.h_health),
            ("POST", r"^/agreements$", self.h_create_agreement),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/plans$", self.h_register_plan),
            ("GET", r"^/agreements/(?P<agreement_id>[^/]+)/explain$", self.h_explain),
            ("GET", r"^/agreements/(?P<agreement_id>[^/]+)/overdue$", self.h_overdue),
            ("GET", r"^/agreements/(?P<agreement_id>[^/]+)/events$", self.h_events),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/obligations/(?P<code>[^/]+)/transfer$", self.h_transfer),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/obligations/(?P<code>[^/]+)/extension$", self.h_extension),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/obligations/(?P<code>[^/]+)/waiver$", self.h_waiver),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/obligations/(?P<code>[^/]+)/submissions$", self.h_submit),
            ("POST", r"^/agreements/(?P<agreement_id>[^/]+)/obligations/(?P<code>[^/]+)/submissions/(?P<submission_id>\d+)/verify$", self.h_verify),
            ("POST", r"^/admin/recalc/(?P<agreement_id>[^/]+)$", self.h_recalc),
            ("POST", r"^/admin/dispatch$", self.h_dispatch),
        ]
        for m, pattern, fn in rules:
            match = re.match(pattern, path)
            if m == method and match:
                return fn, match.groupdict()
        from .errors import NotFoundError

        raise NotFoundError(f"无此端点：{method} {path}")

    # -- 端点 --

    def h_health(self, user, kwargs, body) -> None:
        self._send_json(200, {"status": "ok", "now": self.app.clock.now().isoformat()})

    def h_create_agreement(self, user, kwargs, body) -> None:
        self.app.planner.create_agreement(
            user, body["agreement_id"], body["code"], body["title"]
        )
        self._send_json(201, {"agreement_id": body["agreement_id"], "status": "created"})

    def h_register_plan(self, user, kwargs, body) -> None:
        specs = [self.app.parse_spec(item) for item in body["obligations"]]
        version_no = self.app.planner.register_plan(
            user, kwargs["agreement_id"], specs, body.get("change_summary", "")
        )
        self._send_json(201, {"version_no": version_no, "obligation_count": len(specs)})

    def h_explain(self, user, kwargs, body) -> None:
        from urllib.parse import parse_qs

        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        if code:
            result = self.app.scheduler.explain(kwargs["agreement_id"], code).to_dict()
        else:
            result = [e.to_dict() for e in self.app.scheduler.explain_all(kwargs["agreement_id"])]
        self._send_json(200, result)

    def h_overdue(self, user, kwargs, body) -> None:
        self._send_json(200, self.app.scheduler.overdue_cases(kwargs["agreement_id"]))

    def h_events(self, user, kwargs, body) -> None:
        import json as _json

        from urllib.parse import parse_qs

        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        rows = self.app.repo.list_events(kwargs["agreement_id"], code)
        self._send_json(200, [
            {"id": r["id"], "occurred_at": r["occurred_at"], "obligation_code": r["obligation_code"],
             "event_type": r["event_type"], "actor_user_id": r["actor_user_id"],
             "payload": _json.loads(r["payload_json"])}
            for r in rows
        ])

    def h_transfer(self, user, kwargs, body) -> None:
        version_no = self.app.planner.transfer_responsibility(
            user, kwargs["agreement_id"], kwargs["code"],
            self.app.parse_party(body), body.get("reason", ""),
        )
        self._send_json(200, {"version_no": version_no, "status": "transferred"})

    def h_extension(self, user, kwargs, body) -> None:
        new_due = self.app.planner.grant_extension(
            user, kwargs["agreement_id"], kwargs["code"],
            int(body["days"]), body.get("reason", ""),
        )
        self._send_json(200, {"effective_due_date": new_due})

    def h_waiver(self, user, kwargs, body) -> None:
        self.app.planner.grant_waiver(
            user, kwargs["agreement_id"], kwargs["code"], body.get("reason", "")
        )
        self._send_json(200, {"status": "waived"})

    def h_submit(self, user, kwargs, body) -> None:
        sid = self.app.verification.submit_proof(
            user, kwargs["agreement_id"], kwargs["code"],
            body["proof_type"], body.get("file_ref"), body.get("attributes"),
        )
        self._send_json(201, {"submission_id": sid, "status": "pending_verification"})

    def h_verify(self, user, kwargs, body) -> None:
        self.app.verification.verify(
            user, kwargs["agreement_id"], kwargs["code"],
            int(kwargs["submission_id"]), bool(body["conforming"]),
            body.get("reject_reason"),
        )
        self._send_json(200, {"submission_id": int(kwargs["submission_id"]), "verification": body["conforming"]})

    def h_recalc(self, user, kwargs, body) -> None:
        result = self.app.scheduler.recalculate(
            user, kwargs["agreement_id"], body["range_start"], body["range_end"],
            dispatch=bool(body.get("dispatch", True)),
        )
        self._send_json(200, result)

    def h_dispatch(self, user, kwargs, body) -> None:
        if user.role != "admin":
            from .errors import AuthorizationError

            raise AuthorizationError("仅协议管理员可触发投递重放")
        self._send_json(200, self.app.scheduler.dispatch_pending())


def build_server(host: str, port: int, app: Application) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    return server
