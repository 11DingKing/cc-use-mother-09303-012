"""HTTP API 端到端测试。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

from _fixtures import sample_plan_payload
from obligation_service.app import AppContext, Handler
from obligation_service.clock import FixedClock
from obligation_service.store import Store

ADMIN_HEADERS = {
    "X-Actor-Id": "u-admin", "X-Actor-Name": "Li",
    "X-Actor-Role": "admin", "X-Actor-Party": "administrative-office",
}
OWNER_HEADERS = {
    "X-Actor-Id": "u-zhang", "X-Actor-Name": "Zhang",
    "X-Actor-Role": "owner", "X-Actor-Party": "houqinchu",
}
VERIFIER_HEADERS = {
    "X-Actor-Id": "u-verifier", "X-Actor-Name": "Verifier",
    "X-Actor-Role": "verifier", "X-Actor-Party": "quality-office",
}


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime.fromisoformat("2026-09-29T09:00:00+00:00"))
        self.store = Store(":memory:")
        ctx = AppContext(self.store, self.clock)
        handler = type("H", (Handler,), {"ctx": ctx})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()

    def _req(self, method: str, path: str, payload=None, headers=None):
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self._req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_end_to_end_domain_scenario(self) -> None:
        # 1. 协议 + 计划
        status, agreement = self._req("POST", "/agreements",
                                      {"code": "XY-2026-007", "title": "校企合作协议"},
                                      ADMIN_HEADERS)
        self.assertEqual(status, 201)
        status, plan = self._req(
            "POST", f"/agreements/{agreement['agreement_id']}/plans",
            sample_plan_payload(), ADMIN_HEADERS,
        )
        self.assertEqual(status, 201)
        duties = {d["code"]: d["duty_id"] for d in plan["duties"]}
        equip = duties["EQUIP-DELIVERY"]

        # 2. 调度：设备交付（截止 8/31）逾期
        status, run = self._req("POST", "/scheduler/dispatch", {}, ADMIN_HEADERS)
        self.assertEqual(status, 200)
        self.assertGreater(run["cases_opened"], 0)

        # 3. 重复调度（无幂等键也安全）——服务恢复语义
        status, run2 = self._req("POST", "/scheduler/dispatch", {}, ADMIN_HEADERS)
        self.assertEqual(run2["reminders_created"], 0)
        self.assertEqual(run2["cases_opened"], 0)

        # 4. 提醒最初发给离任负责人
        status, reminders = self._req("GET", f"/reminders?duty_id={equip}")
        self.assertTrue(all(r["recipient"] == "张主任（离任）" for r in reminders["reminders"]))

        # 5. 非管理员不能移交
        status, err = self._req("POST", f"/duties/{equip}/transfer",
                                {"new_owner_party": "equipment-dept",
                                 "new_owner_contact": "王主任", "reason": "x"}, OWNER_HEADERS)
        self.assertEqual(status, 403)

        # 6. 管理员移交 + 延期
        status, tr = self._req("POST", f"/duties/{equip}/transfer",
                               {"new_owner_party": "equipment-dept",
                                "new_owner_contact": "王主任（接任）",
                                "reason": "原负责人离任"}, ADMIN_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(tr["version"], 2)
        _, ext = self._req("POST", f"/duties/{equip}/extend",
                           {"new_first_due": "2026-10-20", "reason": "清单变更"}, ADMIN_HEADERS)
        self.assertEqual(ext["version"], 3)

        # 7. 再调度：新提醒给接任人；旧提醒留痕
        self._req("POST", "/scheduler/dispatch", {}, ADMIN_HEADERS)
        _, reminders2 = self._req("GET", f"/reminders?duty_id={equip}")
        v1 = [r for r in reminders2["reminders"] if r["duty_version"] == 1]
        v3 = [r for r in reminders2["reminders"] if r["duty_version"] == 3]
        self.assertTrue(v1 and all(r["recipient"] == "张主任（离任）" for r in v1))
        self.assertTrue(all(r["recipient"] == "王主任（接任）" for r in v3))

        # 8. 义务版本链
        status, detail = self._req("GET", f"/duties/{equip}")
        self.assertEqual([r["revision_type"] for r in detail["revisions"]],
                         ["register", "transfer", "extend"])

        # 9. 提交证明：非当前责任方被拒
        status, err = self._req("POST", f"/duties/{equip}/submissions",
                                {"evidence": [{"type": "t", "ref": "r"}]}, OWNER_HEADERS)
        self.assertEqual(status, 403)
        new_owner_headers = {**OWNER_HEADERS,
                             "X-Actor-Id": "u-wang", "X-Actor-Party": "equipment-dept",
                             "X-Actor-Name": "Wang"}
        # 新责任方提交合格证明（延期到 10/20 后仍可提前提交）
        status, sub = self._req("POST", f"/duties/{equip}/submissions", {
            "evidence": [
                {"type": "设备签收单", "ref": "doc://s1", "original": True},
                {"type": "安装验收报告", "ref": "doc://a1", "original": True},
            ]
        }, new_owner_headers)
        self.assertEqual(status, 201)
        self.assertTrue(sub["conforms"])

        # 10. 责任方不能自批；独立核验人员确认
        status, err = self._req("POST", f"/duties/{equip}/confirmations",
                                {"decision": "approved"}, new_owner_headers)
        self.assertEqual(status, 403)
        status, decision = self._req("POST", f"/duties/{equip}/confirmations",
                                     {"decision": "approved", "comment": "合格"},
                                     VERIFIER_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(decision["state"], "fulfilled")

        # 11. 管理员重算区间 + 解释
        status, recalc = self._req("POST", "/scheduler/recalculate", {
            "range_start": "2026-08-01", "range_end": "2026-09-29",
        }, ADMIN_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(recalc["reminders_created"], 0)  # 幂等
        equip_expl = next(e for e in recalc["explanations"] if e["code"] == "EQUIP-DELIVERY")
        self.assertEqual(equip_expl["windows"][0]["state"], "fulfilled")
        self.assertTrue(any("已由核验人员确认" in s for s in equip_expl["windows"][0]["why"]))

        # 12. explain 接口可单独查看
        status, expl = self._req("GET", f"/duties/{equip}/explain?as_of=2026-09-29")
        self.assertEqual(status, 200)
        self.assertTrue(expl["windows"][0]["why"])

    def test_idempotency_key_replays_first_response(self) -> None:
        headers = {**ADMIN_HEADERS, "Idempotency-Key": "key-001"}
        s1, b1 = self._req("POST", "/agreements", {"code": "DUP-1", "title": "t"}, headers)
        s2, b2 = self._req("POST", "/agreements", {"code": "DUP-1", "title": "t"}, headers)
        self.assertEqual((s1, b1), (s2, b2))
        count = self.store.fetchone("SELECT COUNT(*) AS c FROM agreements WHERE code='DUP-1'")["c"]
        self.assertEqual(count, 1)

    def test_recalculate_requires_admin_role(self) -> None:
        status, _ = self._req("POST", "/scheduler/recalculate",
                              {"range_start": "2026-08-01", "range_end": "2026-09-29"},
                              VERIFIER_HEADERS)
        self.assertEqual(status, 403)

    def test_unknown_route_404(self) -> None:
        status, body = self._req("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
