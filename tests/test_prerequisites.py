"""前置义务依赖测试。"""
from __future__ import annotations

import unittest

from _fixtures import build_world, college_owner, register_sample_world, verifier


class PrerequisiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = build_world("2026-09-29")
        self.ctx = register_sample_world(self.world)
        self.equip = self.ctx["duties"]["EQUIP-DELIVERY"]
        self.faculty = self.ctx["duties"]["FACULTY-TRAIN"]

    def test_blocker_recorded_until_prerequisite_confirmed(self) -> None:
        w = self.world
        w["sch"].dispatch()
        expl = w["sch"].explain(self.faculty, "2026-09-29")
        win = expl["windows"][0]
        self.assertTrue(any("EQUIP-DELIVERY" in b for b in win["blockers"]), win["blockers"])
        case = w["store"].fetchone(
            "SELECT * FROM overdue_cases WHERE duty_id=?", (self.faculty,))
        import json

        opened_detail = json.loads(w["store"].fetchall(
            "SELECT detail FROM case_events WHERE case_id=? AND event_type='opened'",
            (case["case_id"],))[0]["detail"])
        self.assertTrue(any("EQUIP-DELIVERY" in b for b in opened_detail["blockers"]))

        # 设备交付经独立核验合格后，师资义务的阻塞解除
        w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": [
                {"type": "设备签收单", "ref": "d1", "original": True},
                {"type": "安装验收报告", "ref": "d2", "original": True},
            ]},
        )
        w["ver"].decide(verifier(), self.equip, {"decision": "approved"})
        expl2 = w["sch"].explain(self.faculty, "2026-09-29")
        self.assertEqual(expl2["windows"][0]["blockers"], [])

    def test_exempted_prerequisite_does_not_block(self) -> None:
        w = self.world
        w["reg"].grant_exemption(w["admin"], self.equip, {"reason": "补充条款豁免"})
        w["sch"].dispatch()
        expl = w["sch"].explain(self.faculty, "2026-09-29")
        self.assertEqual(expl["windows"][0]["blockers"], [])


if __name__ == "__main__":
    unittest.main()
