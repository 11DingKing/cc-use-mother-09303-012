"""HTTP 服务端端到端测试（真实端口，标准库 urllib）。"""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from obligation import (  # noqa: E402
    FixedClock, InMemoryCalendarGateway, ObligationKind, ObligationSpec,
    ProofRequirement, ResponsibleParty, Store, TimeWindow, User,
)
from obligation.planner import PlannerService  # noqa: E402
from obligation.repository import Repository  # noqa: E402
from obligation.server import Application, build_server  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spec(code, due):
    return ObligationSpec(
        code=code, kind=ObligationKind.EQUIPMENT_DELIVERY, title=code,
        responsible=ResponsibleParty("P1", "学院", "zhou", "老周", "zhou@x"),
        window=TimeWindow(date(2026, 7, 1), due, advance_days=(7,)),
        proof=ProofRequirement("delivery_note", "签收单", verifier_roles=("equipment_inspector",)),
    )


class HttpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock("2026-09-29T00:00:00Z")
        self.calendar = InMemoryCalendarGateway()
        self.app = Application(Store(":memory:"), clock=self.clock, calendar=self.calendar)
        repo = self.app.repo
        admin = User("u-admin", "管理员", "admin")
        zhou = User("u-zhou", "老周", "party_member", party_id="P1")
        insp = User("u-insp", "钱检", "verifier")
        repo.upsert_user(admin, "tok-admin")
        repo.upsert_user(zhou, "tok-zhou")
        repo.upsert_user(insp, "tok-insp")
        planner = PlannerService(repo, self.clock)
        planner.grant_verifier_role(admin, "u-insp", "equipment_inspector")
        planner.create_agreement(admin, "A1", "C1", "协议")
        planner.register_plan(admin, "A1", [spec("EQ-01", date(2026, 8, 29))], "v1")

        self.server = build_server("127.0.0.1", free_port(), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method: str, path: str, token: str | None = None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_is_open(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_auth_required(self) -> None:
        status, _ = self.call("GET", "/agreements/A1/overdue")
        self.assertEqual(status, 403)

    def test_overdue_and_explain_via_api(self) -> None:
        status, cases = self.call("GET", "/agreements/A1/overdue", "tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(cases[0]["code"], "EQ-01")
        self.assertEqual(cases[0]["days_overdue"], 31)

        status, ex = self.call("GET", "/agreements/A1/explain?code=EQ-01", "tok-admin")
        self.assertEqual(status, 200)
        self.assertTrue(ex["overdue"])
        self.assertTrue(ex["reasons"])

    def test_recalc_is_admin_only_and_idempotent(self) -> None:
        status, _ = self.call(
            "POST", "/admin/recalc/A1", "tok-zhou",
            {"range_start": "2026-08-01", "range_end": "2026-09-30"},
        )
        self.assertEqual(status, 403)

        status, r1 = self.call(
            "POST", "/admin/recalc/A1", "tok-admin",
            {"range_start": "2026-08-01", "range_end": "2026-09-30"},
        )
        self.assertEqual(status, 200)
        self.assertGreater(r1["created"], 0)
        self.assertIn("explanation", r1["obligations"][0])

        status, r2 = self.call(
            "POST", "/admin/recalc/A1", "tok-admin",
            {"range_start": "2026-08-01", "range_end": "2026-09-30"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["already_existed"], r1["created"])

    def test_register_plan_via_api(self) -> None:
        payload = {
            "code": "EQ-02", "kind": "equipment_delivery", "title": "第二批设备",
            "responsible": {"party_id": "P1", "owner_user_id": "u-zhou", "contact": "zhou@x"},
            "window": {"start_date": "2026-09-01", "due_date": "2026-12-01", "advance_days": [7]},
            "proof": {"proof_type": "delivery_note", "verifier_roles": ["equipment_inspector"]},
        }
        status, body = self.call("POST", "/agreements/A1/plans", "tok-admin",
                                 {"obligations": [payload], "change_summary": "增补"})
        self.assertEqual(status, 201)
        self.assertEqual(body["version_no"], 2)

    def test_independent_verification_flow_via_api(self) -> None:
        # 先重算产生逾期提醒
        self.call("POST", "/admin/recalc/A1", "tok-admin",
                  {"range_start": "2026-08-01", "range_end": "2026-09-30"})
        # 院校提交证明
        status, sub = self.call(
            "POST", "/agreements/A1/obligations/EQ-01/submissions", "tok-zhou",
            {"proof_type": "delivery_note", "file_ref": "note.pdf"},
        )
        self.assertEqual(status, 201)
        sid = sub["submission_id"]
        # 独立核验员确认合格
        status, _ = self.call(
            "POST", f"/agreements/A1/obligations/EQ-01/submissions/{sid}/verify", "tok-insp",
            {"conforming": True},
        )
        self.assertEqual(status, 200)
        # 结清后逾期案件消失
        status, cases = self.call("GET", "/agreements/A1/overdue", "tok-admin")
        self.assertEqual(cases, [])


if __name__ == "__main__":
    unittest.main()
