"""调度、版本修订与幂等性测试。"""
from __future__ import annotations

import unittest

from _fixtures import (
    build_world,
    new_owner,
    register_sample_world,
)
from obligation_service.errors import AuthorizationError, ConflictError, ValidationError


class SchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = build_world("2026-09-29")
        self.ctx = register_sample_world(self.world)
        self.duties = self.ctx["duties"]

    # ---- 幂等：重复调度 / 服务恢复 ----

    def test_dispatch_is_idempotent_across_repeated_runs(self) -> None:
        w = self.world
        first = w["sch"].dispatch()
        second = w["sch"].dispatch()
        third = w["sch"].dispatch()
        # 三次调度，只在首次创建提醒
        self.assertGreater(first["reminders_created"], 0)
        self.assertEqual(second["reminders_created"], 0)
        self.assertEqual(third["reminders_created"], 0)

        equip = self.duties["EQUIP-DELIVERY"]
        rows = w["store"].fetchall(
            "SELECT kind, scheduled_for, status, recipient FROM reminders"
            " WHERE duty_id=? ORDER BY scheduled_for",
            (equip,),
        )
        # 截止日 2026-08-31：提前 7/1/0 天 + 逾期点（2026-08-31 当天，grace=0）
        self.assertEqual(
            [(r["kind"], r["scheduled_for"], r["status"]) for r in rows],
            [
                ("upcoming", "2026-08-24", "sent"),
                ("upcoming", "2026-08-30", "sent"),
                ("due", "2026-08-31", "sent"),
                ("overdue", "2026-08-31", "sent"),
            ],
        )

    def test_service_restart_does_not_duplicate(self) -> None:
        """模拟服务崩溃恢复：新 Scheduler 实例 + 同库重放。"""
        w = self.world
        w["sch"].dispatch()
        from obligation_service.scheduler import Scheduler

        recovered = Scheduler(w["store"], w["clock"])
        result = recovered.dispatch()
        self.assertEqual(result["reminders_created"], 0)
        count = w["store"].fetchone("SELECT COUNT(*) AS c FROM reminders")["c"]
        again = recovered.dispatch()
        self.assertEqual(again["reminders_created"], 0)
        self.assertEqual(w["store"].fetchone("SELECT COUNT(*) AS c FROM reminders")["c"], count)

    # ---- 逾期案件 ----

    def test_overdue_case_opened_after_deadline(self) -> None:
        w = self.world
        w["sch"].dispatch()
        equip = self.duties["EQUIP-DELIVERY"]
        case = w["store"].fetchone(
            "SELECT * FROM overdue_cases WHERE duty_id=?", (equip,)
        )
        self.assertIsNotNone(case)
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["due_date"], "2026-08-31")
        events = w["store"].fetchall(
            "SELECT event_type FROM case_events WHERE case_id=? ORDER BY seq", (case["case_id"],)
        )
        self.assertEqual([e["event_type"] for e in events], ["opened"])

    def test_no_case_inside_grace_period(self) -> None:
        """师资义务截止 9/15，宽限 3 天；9/17 不应立案。"""
        world = build_world("2026-09-17")
        ctx = register_sample_world(world)
        world["sch"].dispatch()
        faculty = ctx["duties"]["FACULTY-TRAIN"]
        self.assertIsNone(world["store"].fetchone(
            "SELECT 1 FROM overdue_cases WHERE duty_id=?", (faculty,)))

    # ---- 责任移交：不抹旧记录，新提醒给新责任人 ----

    def test_transfer_keeps_history_and_redirects_reminders(self) -> None:
        w = self.world
        sch, reg = w["sch"], w["reg"]
        equip = self.duties["EQUIP-DELIVERY"]
        sch.dispatch()
        before = w["store"].fetchall(
            "SELECT recipient, duty_version, status FROM reminders WHERE duty_id=?", (equip,)
        )
        self.assertTrue(all(r["recipient"] == "张主任（离任）" for r in before))

        # 9/29 移交，同时延期到 10/20（设备交付已逾期一个月的处置）
        reg.transfer_ownership(w["admin"], equip, {
            "new_owner_party": "equipment-dept",
            "new_owner_contact": "王主任（接任）",
            "reason": "原负责人离任，责任移交装备保障部(equipment-dept)",
        })
        reg.extend_deadline(w["admin"], equip, {
            "new_first_due": "2026-10-20",
            "reason": "设备清单变更，双方同意延期",
        })
        result = sch.dispatch()
        # 新截止日 10/20，9/29 当天还没有新提醒点
        self.assertEqual(result["reminders_created"], 0)

        # 旧版本 v1 提醒已发送，全部保留且接收人仍是离任负责人
        old_rows = w["store"].fetchall(
            "SELECT duty_version, recipient, status FROM reminders WHERE duty_id=?"
            " ORDER BY duty_version, scheduled_for",
            (equip,),
        )
        v1 = [r for r in old_rows if r["duty_version"] == 1]
        self.assertTrue(v1, "旧版本提醒必须保留")
        self.assertTrue(all(r["recipient"] == "张主任（离任）" for r in v1))

        # 版本链完整：register -> transfer(v2) -> extend(v3)
        detail = reg.get_duty_detail(equip)
        self.assertEqual([r["version"] for r in detail["revisions"]], [1, 2, 3])
        self.assertEqual(
            [r["revision_type"] for r in detail["revisions"]],
            ["register", "transfer", "extend"],
        )
        owners = [r["owner_party"] for r in detail["revisions"]]
        self.assertEqual(owners, ["houqinchu", "equipment-dept", "equipment-dept"])

        # 案件追加 deadline_moved / transferred 事件，opened 仍在且不消失
        case = w["store"].fetchone("SELECT * FROM overdue_cases WHERE duty_id=?", (equip,))
        events = [r["event_type"] for r in w["store"].fetchall(
            "SELECT event_type FROM case_events WHERE case_id=? ORDER BY seq", (case["case_id"],))]
        self.assertEqual(events, ["opened", "deadline_moved", "transferred"])
        self.assertEqual(case["status"], "open")
        self.assertEqual(case["owner_party"], "equipment-dept")
        self.assertEqual(case["due_date"], "2026-10-20")

        # 时间推进到 10/15：v3 提醒出现，且只发给接任负责人
        w["clock"].advance(days=16)
        sch.dispatch()
        new_rows = w["store"].fetchall(
            "SELECT recipient, status FROM reminders WHERE duty_id=? AND duty_version=3",
            (equip,),
        )
        self.assertTrue(new_rows, "进入新窗口后应按新版本生成提醒")
        self.assertTrue(all(r["recipient"] == "王主任（接任）" for r in new_rows))
        # 旧记录仍然存在
        self.assertTrue(w["store"].fetchall(
            "SELECT 1 FROM reminders WHERE duty_id=? AND duty_version=1", (equip,)))

    def test_transfer_requires_admin(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.world["reg"].transfer_ownership(
                new_owner(), self.duties["EQUIP-DELIVERY"],
                {"new_owner_party": "x", "new_owner_contact": "y", "reason": "无权"},
            )

    # ---- 豁免 ----

    def test_exemption_suppresses_and_keeps_history(self) -> None:
        w = self.world
        equip = self.duties["EQUIP-DELIVERY"]
        w["sch"].dispatch()
        self.assertIsNotNone(w["store"].fetchone(
            "SELECT 1 FROM overdue_cases WHERE duty_id=?", (equip,)))

        w["reg"].grant_exemption(w["admin"], equip, {
            "reason": "设备改由捐赠方直接维护，协议补充条款豁免交付义务",
        })
        result = w["sch"].dispatch()
        # 豁免后不再产生新提醒（旧提醒已发送的，作为既成事实保留）
        self.assertEqual(result["reminders_created"], 0)
        remaining = w["store"].fetchone(
            "SELECT COUNT(*) AS c FROM reminders WHERE duty_id=?", (equip,))["c"]
        self.assertGreater(remaining, 0, "历史提醒不得删除")
        case = w["store"].fetchone("SELECT * FROM overdue_cases WHERE duty_id=?", (equip,))
        self.assertEqual(case["status"], "resolved")
        self.assertEqual(case["resolution"], "exempted")
        events = [r["event_type"] for r in w["store"].fetchall(
            "SELECT event_type FROM case_events WHERE case_id=? ORDER BY seq", (case["case_id"],))]
        self.assertEqual(events, ["opened", "resolved"])
        # 豁免后不能再提交证明
        from obligation_service.models import Actor, ROLE_OWNER

        owner = Actor("u1", "装备", ROLE_OWNER, "equipment-dept")
        with self.assertRaises(ConflictError):
            w["ver"].submit_evidence(owner, equip, {"evidence": [{"type": "x", "ref": "y"}]})

    # ---- 前置条件校验 ----

    def test_unknown_prerequisite_rejected(self) -> None:
        world = build_world()
        with self.assertRaises(ValidationError):
            world["reg"].create_agreement(world["admin"], {"code": "A1", "title": "t"})
            agreement_id = world["store"].fetchone(
                "SELECT agreement_id FROM agreements WHERE code='A1'"
            )["agreement_id"]
            world["reg"].register_plan(world["admin"], agreement_id, {
                "duties": [
                    {"code": "A", "title": "a", "owner_party": "p", "owner_contact": "c",
                     "channel": "mail", "time_window": {"first_due": "2026-10-01"},
                     "evidence": {}, "prerequisites": [{"duty_code": "GHOST"}]},
                ]
            })

    def test_cyclic_prerequisite_rejected(self) -> None:
        world = build_world()
        agreement_id = world["reg"].create_agreement(
            world["admin"], {"code": "A2", "title": "t"})["agreement_id"]
        duty = lambda code, dep: {
            "code": code, "title": code, "owner_party": "p", "owner_contact": "c",
            "channel": "mail", "time_window": {"first_due": "2026-10-01"},
            "evidence": {}, "prerequisites": [{"duty_code": dep}] if dep else [],
        }
        with self.assertRaises(ValidationError):
            world["reg"].register_plan(world["admin"], agreement_id, {
                "duties": [duty("A", "B"), duty("B", "A")]
            })

    # ---- 周期义务窗口 ----

    def test_recurring_quarterly_windows(self) -> None:
        w = self.world
        w["sch"].dispatch()
        faculty = self.duties["FACULTY-TRAIN"]
        # 首期 9/15：提前 7 天（9/8）、提前 1 天（9/14）、到期 9/15；
        # 宽限 3 天 => 逾期点 9/18（as_of 9/29 已过）
        rows = w["store"].fetchall(
            "SELECT kind, scheduled_for FROM reminders WHERE duty_id=? AND duty_version=1"
            " ORDER BY scheduled_for",
            (faculty,),
        )
        kinds = [(r["kind"], r["scheduled_for"]) for r in rows]
        self.assertEqual(kinds, [
            ("upcoming", "2026-09-08"),
            ("upcoming", "2026-09-14"),
            ("due", "2026-09-15"),
            ("overdue", "2026-09-18"),
        ])
        # 下一期 12/15 的提醒尚未生成（未进入窗口）
        expl = w["sch"].explain(faculty, "2026-09-29")
        self.assertEqual([win["window_key"] for win in expl["windows"]], ["w0000"])

    # ---- 区间重算：幂等 + 解释 ----

    def test_recalculate_range_is_idempotent_and_explains(self) -> None:
        w = self.world
        equip = self.duties["EQUIP-DELIVERY"]
        r1 = w["sch"].recalculate(
            w["admin"], range_start="2026-08-01", range_end="2026-09-29"
        )
        created = r1["reminders_created"]
        self.assertGreater(created, 0)
        r2 = w["sch"].recalculate(
            w["admin"], range_start="2026-08-01", range_end="2026-09-29"
        )
        self.assertEqual(r2["reminders_created"], 0)
        self.assertEqual(len(r2["explanations"]), 3)
        expl = next(e for e in r2["explanations"] if e["code"] == "EQUIP-DELIVERY")
        win = expl["windows"][0]
        self.assertTrue(any("已逾期" in s for s in win["why"]))
        self.assertEqual(win["state"], "pending")
        self.assertIsNotNone(win["overdue_case"])

    def test_recalculate_requires_admin(self) -> None:
        from _fixtures import college_owner

        with self.assertRaises(AuthorizationError):
            self.world["sch"].recalculate(
                college_owner(), range_start="2026-08-01", range_end="2026-09-29"
            )


if __name__ == "__main__":
    unittest.main()
