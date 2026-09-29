"""独立核验、证明符合性与留痕测试。"""
from __future__ import annotations

import unittest

from _fixtures import build_world, college_owner, register_sample_world, verifier
from obligation_service.errors import AuthorizationError, ConflictError, ValidationError
from obligation_service.models import ROLE_OWNER, ROLE_VERIFIER, Actor


class VerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = build_world("2026-09-29")
        self.ctx = register_sample_world(self.world)
        self.equip = self.ctx["duties"]["EQUIP-DELIVERY"]
        self.faculty = self.ctx["duties"]["FACULTY-TRAIN"]

    def _equip_evidence(self) -> list[dict]:
        return [
            {"type": "设备签收单", "ref": "doc://signoff-001", "original": True},
            {"type": "安装验收报告", "ref": "doc://install-001", "original": True},
        ]

    def test_only_current_owner_may_submit(self) -> None:
        w = self.world
        other = Actor("u-x", "外单位", ROLE_OWNER, "third-party")
        with self.assertRaises(AuthorizationError):
            w["ver"].submit_evidence(other, self.equip, {"evidence": self._equip_evidence()})

    def test_missing_required_evidence_type_is_flagged(self) -> None:
        w = self.world
        resp = w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": [{"type": "设备签收单", "ref": "d1", "original": True}]},
        )
        self.assertFalse(resp["conforms"])
        self.assertTrue(any("安装验收报告" in p for p in resp["conformance_problems"]))

    def test_original_required(self) -> None:
        w = self.world
        resp = w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": [
                {"type": "设备签收单", "ref": "d1", "original": False},
                {"type": "安装验收报告", "ref": "d2", "original": False},
            ]},
        )
        self.assertTrue(any("原件" in p for p in resp["conformance_problems"]))

    def test_verifier_role_required(self) -> None:
        w = self.world
        with self.assertRaises(AuthorizationError):
            w["ver"].decide(college_owner("quality-office"), self.equip, {"decision": "approved"})

    def test_verifier_must_be_independent_party(self) -> None:
        w = self.world
        # 责任方所属的人充当核验人员 -> 拒绝
        biased = Actor("u-bad", "后勤处内部稽核", ROLE_VERIFIER, "houqinchu")
        with self.assertRaises(AuthorizationError):
            w["ver"].decide(biased, self.equip, {"decision": "approved"})

    def test_full_flow_submit_independent_confirm_resolves_case(self) -> None:
        w = self.world
        # 已逾期 -> 立案
        w["sch"].dispatch()
        case_before = w["store"].fetchone(
            "SELECT status FROM overdue_cases WHERE duty_id=?", (self.equip,))
        self.assertEqual(case_before["status"], "open")

        # 责任方提交
        sub = w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": self._equip_evidence()},
        )
        self.assertTrue(sub["conforms"])

        # 无证明不能确认
        with self.assertRaises(ValidationError):
            w["ver"].decide(verifier(), self.faculty, {"decision": "approved"})

        # 独立核验人员确认
        decision = w["ver"].decide(verifier(), self.equip, {"decision": "approved",
                                                            "comment": "材料齐全，验收合格"})
        self.assertEqual(decision["state"], "fulfilled")
        self.assertEqual(decision["case_effect"], "resolved")

        # 案件结清并留核验事件
        case = w["store"].fetchone(
            "SELECT * FROM overdue_cases WHERE duty_id=?", (self.equip,))
        self.assertEqual(case["status"], "resolved")
        self.assertEqual(case["resolution"], "fulfilled")
        events = [r["event_type"] for r in w["store"].fetchall(
            "SELECT event_type FROM case_events WHERE case_id=? ORDER BY seq", (case["case_id"],))]
        self.assertEqual(events, ["opened", "resolved"])

        # 重复确认被拒绝
        with self.assertRaises(ConflictError):
            w["ver"].decide(verifier(), self.equip, {"decision": "approved"})

        # 调度器重放不会重新立案
        w["sch"].dispatch()
        self.assertEqual(
            w["store"].fetchone(
                "SELECT COUNT(*) AS c FROM overdue_cases WHERE duty_id=?", (self.equip,))["c"],
            1,
        )

    def test_rejection_keeps_case_open_and_reopens(self) -> None:
        w = self.world
        w["sch"].dispatch()
        w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": self._equip_evidence()},
        )
        decision = w["ver"].decide(verifier(), self.equip,
                                  {"decision": "rejected", "comment": "验收报告缺签字"})
        self.assertEqual(decision["state"], "rejected")
        case = w["store"].fetchone(
            "SELECT * FROM overdue_cases WHERE duty_id=?", (self.equip,))
        self.assertEqual(case["status"], "open")

        # 补正后通过
        w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": self._equip_evidence(), "note": "已补签字"},
        )
        decision2 = w["ver"].decide(verifier("audit-office", "u-v2", "复审员"), self.equip,
                                   {"decision": "approved"})
        self.assertEqual(decision2["state"], "fulfilled")

        # 追加留痕：两条确认记录都在
        trail = w["ver"].trail(self.equip, "w0000")
        self.assertEqual([c["decision"] for c in trail["confirmations"]],
                         ["rejected", "approved"])
        self.assertEqual(len(trail["submissions"]), 2)

    def test_verifier_party_must_be_registered(self) -> None:
        w = self.world
        # 计划登记了 quality-office/audit-office；其他核验方被拒
        outsider = Actor("u-out", "外组核验", ROLE_VERIFIER, "other-inspectorate")
        with self.assertRaises(AuthorizationError):
            w["ver"].decide(outsider, self.equip, {"decision": "approved"})

    def test_submitter_party_cannot_verify(self) -> None:
        w = self.world
        w["ver"].submit_evidence(
            college_owner("houqinchu", "u-zhang", "张主任"), self.equip,
            {"evidence": self._equip_evidence()},
        )
        # 核验人员恰好属于提交方（挂靠后勤处）—— 独立性失败
        mole = Actor("u-mole", "伪装核验", ROLE_VERIFIER, "houqinchu")
        with self.assertRaises(AuthorizationError):
            w["ver"].decide(mole, self.equip, {"decision": "approved"})


if __name__ == "__main__":
    unittest.main()
