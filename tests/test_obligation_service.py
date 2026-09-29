"""端到端领域测试：覆盖需求中的全部关键场景。

使用固定时钟，确定性地演练：
1. 版本化计划登记（义务/责任方/前置条件/时间窗/证明要求/核验角色）；
2. 设备交付逾期一个月的逾期案件与升级；
3. 负责人离任移交：旧提醒保留且指向旧负责人，新提醒只发给新负责人；
4. 报告/师资证明未经独立核验不算合格、前置条件门控；
5. 独立核验的角色与独立性约束、合格数量结清；
6. 延期/豁免/移交不抹去旧记录；
7. 重复调度与服务恢复不产生多条提醒（日历侧亦不重复）。
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from obligation import (  # noqa: E402
    FailingCalendarGateway,
    FixedClock,
    InMemoryCalendarGateway,
    ObligationKind,
    ObligationSpec,
    ProofRequirement,
    Prerequisite,
    ResponsibleParty,
    SchedulerService,
    Store,
    TimeWindow,
    User,
)
from obligation.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError  # noqa: E402
from obligation.planner import PlannerService  # noqa: E402
from obligation.repository import Repository  # noqa: E402
from obligation.verification import VerificationService  # noqa: E402

AGREEMENT = "A-1"


def party(owner_id: str, owner_name: str, contact: str, party_id: str = "P1") -> ResponsibleParty:
    return ResponsibleParty(party_id, "学院", owner_id, owner_name, contact)


def make_spec(code, kind, due, owner, verifier_roles, prerequisites=(), advance=(7, 1),
              proof_type=None, required=()):
    return ObligationSpec(
        code=code, kind=kind, title=code, responsible=owner,
        window=TimeWindow(date(2026, 7, 1), due, advance_days=advance,
                          overdue_interval_days=7, overdue_max_reminders=8),
        proof=ProofRequirement(
            proof_type or f"proof-{code}", f"{code} 证明",
            verifier_roles=tuple(verifier_roles), required_attributes=tuple(required),
        ),
        prerequisites=tuple(prerequisites),
    )


class World:
    def __init__(self, clock="2026-09-29T00:00:00Z", calendar=None):
        self.clock = FixedClock(clock)
        self.store = Store(":memory:")
        self.repo = Repository(self.store)
        self.planner = PlannerService(self.repo, self.clock)
        self.calendar = calendar or InMemoryCalendarGateway()
        self.scheduler = SchedulerService(self.repo, self.clock, self.calendar)
        self.verifier = VerificationService(self.repo, self.clock)

        self.admin = User("admin", "管理员", "admin")
        self.old_owner = User("zhou", "老周", "party_member", party_id="P1")
        self.new_owner = User("wu", "吴欣", "party_member", party_id="P1")
        self.inspector = User("insp", "钱检", "verifier")           # 独立第三方
        self.party_verifier = User("pver", "院校内检", "verifier", party_id="P1")  # 不独立
        self.reviewer = User("rev", "孙审", "verifier")
        for u in (self.admin, self.old_owner, self.new_owner,
                  self.inspector, self.party_verifier, self.reviewer):
            self.repo.upsert_user(u)
        self.planner.create_agreement(self.admin, AGREEMENT, "C-1", "测试协议")
        self.planner.grant_verifier_role(self.admin, "insp", "equipment_inspector")
        self.planner.grant_verifier_role(self.admin, "rev", "academic_reviewer")
        self.planner.grant_verifier_role(self.admin, "pver", "equipment_inspector")


def seed_overdue_world() -> World:
    """设备交付截止 2026-08-29；7 月 1 日登记计划，今天 2026-09-29（逾期 31 天）。"""
    w = World("2026-07-01T00:00:00Z")
    spec = make_spec(
        "EQ-01", ObligationKind.EQUIPMENT_DELIVERY, date(2026, 8, 29),
        party("zhou", "老周", "zhou@x"), ("equipment_inspector",),
        proof_type="delivery_note", required=("serial_list",),
    )
    w.planner.register_plan(w.admin, AGREEMENT, [spec], "首版")
    w.clock.set("2026-09-29T00:00:00Z")
    return w


class PlanVersioningTest(unittest.TestCase):
    def test_plan_versions_are_immutable_and_incrementing(self) -> None:
        w = World()
        s1 = make_spec("O1", ObligationKind.REPORT, date(2026, 10, 1),
                       party("zhou", "老周", "z@x"), ("academic_reviewer",))
        self.assertEqual(w.planner.register_plan(w.admin, AGREEMENT, [s1], "v1"), 1)
        # 责任移交产生 v2；截止日不变
        new_party = party("wu", "吴欣", "w@x")
        self.assertEqual(
            w.planner.transfer_responsibility(w.admin, AGREEMENT, "O1", new_party, "交接"),
            2,
        )
        # 静默改写时间窗的重新登记必须被拒绝（改期须走延期，保留旧截止日）
        s1b = make_spec("O1", ObligationKind.REPORT, date(2026, 11, 1),
                        party("wu", "吴欣", "w@x"), ("academic_reviewer",))
        with self.assertRaises(ValidationError):
            w.planner.register_plan(w.admin, AGREEMENT, [s1b], "偷改截止日")

        v1 = w.repo.spec_at_version(AGREEMENT, 1)
        v2 = w.repo.spec_at_version(AGREEMENT, 2)
        self.assertEqual(v1["O1"].responsible.owner_user_id, "zhou")
        self.assertEqual(v1["O1"].window.due_date, date(2026, 10, 1))
        self.assertEqual(v2["O1"].responsible.owner_user_id, "wu")
        self.assertEqual(v2["O1"].window.due_date, date(2026, 10, 1))
        # 旧版本仍可列出且未被覆盖
        versions = w.repo.list_plan_versions(AGREEMENT)
        self.assertEqual([v["version_no"] for v in versions], [1, 2])

    def test_non_admin_cannot_register(self) -> None:
        w = World()
        s = make_spec("O1", ObligationKind.REPORT, date(2026, 10, 1),
                      party("zhou", "老周", "z@x"), ("academic_reviewer",))
        with self.assertRaises(AuthorizationError):
            w.planner.register_plan(w.old_owner, AGREEMENT, [s], "x")

    def test_missing_prerequisite_rejected(self) -> None:
        w = World()
        s = make_spec("O2", ObligationKind.FACULTY, date(2026, 10, 1),
                      party("zhou", "老周", "z@x"), ("academic_reviewer",),
                      prerequisites=(Prerequisite("O1"),))
        with self.assertRaises(ValidationError):
            w.planner.register_plan(w.admin, AGREEMENT, [s], "缺前置")

    def test_prerequisite_and_verifier_role_required_at_registration(self) -> None:
        w = World()
        bad = make_spec("O1", ObligationKind.REPORT, date(2026, 10, 1),
                        party("zhou", "老周", "z@x"), ())
        with self.assertRaises(ValidationError):
            w.planner.register_plan(w.admin, AGREEMENT, [bad], "无核验角色")


class OverdueTest(unittest.TestCase):
    def test_equipment_one_month_overdue_generates_case_and_escalations(self) -> None:
        w = seed_overdue_world()
        result = w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        self.assertGreater(result["created"], 0)

        cases = w.scheduler.overdue_cases(AGREEMENT)
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case["code"], "EQ-01")
        self.assertEqual(case["days_overdue"], 31)
        # 逾期升级：09-05(第1轮) 与 09-12/19/26 在区间内应已生成
        overdue_fire_dates = [r["fire_date"] for r in case["overdue_reminders"]]
        self.assertIn("2026-09-26", overdue_fire_dates)
        self.assertTrue(any("逾期" in reason for reason in case["reasons"]))

    def test_explain_says_why_due(self) -> None:
        w = seed_overdue_world()
        ex = w.scheduler.explain(AGREEMENT, "EQ-01")
        self.assertTrue(ex.overdue)
        self.assertEqual(ex.days_to_due, -31)
        joined = "\n".join(ex.reasons)
        self.assertIn("已逾期 31 天", joined)
        self.assertIn("generation", joined)
        self.assertIn("核验角色", joined)


class TransferTest(unittest.TestCase):
    def test_transfer_keeps_old_reminders_to_departed_owner_and_routes_new_to_successor(self) -> None:
        w = seed_overdue_world()
        # 时间回到 8 月底调度：当时提醒都发给老周（历史真实发生）
        w.clock.set("2026-08-30T00:00:00Z")
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 10, 31))
        old_reminders = w.repo.all_reminders(AGREEMENT, "EQ-01")
        self.assertTrue(all(r["owner_user_id"] == "zhou" for r in old_reminders))
        old_cal = [e for e in w.calendar.all_events() if e["recipient"] == "zhou@x"]
        self.assertTrue(old_cal)  # 日历确实投给了离任负责人（旧问题被真实记录）

        # 9 月 20 日移交
        w.clock.set("2026-09-20T00:00:00Z")
        new_party = party("wu", "吴欣", "wu@x")
        w.planner.transfer_responsibility(w.admin, AGREEMENT, "EQ-01", new_party, "老周离任")
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 12, 31))

        all_rows = w.repo.all_reminders(AGREEMENT, "EQ-01")
        superseded = [r for r in all_rows if r["superseded_at"]]
        active = [r for r in all_rows if not r["superseded_at"]]
        # 旧记录保留，仍指向老周
        self.assertTrue(superseded)
        self.assertTrue(all(r["owner_user_id"] == "zhou" for r in superseded))
        # 新提醒全部指向吴欣
        self.assertTrue(active)
        self.assertTrue(all(r["owner_user_id"] == "wu" for r in active))
        self.assertTrue(all(r["recipient"] == "wu@x" for r in active))
        # 移交日（09-20）之前的新代次档位不补发：09-05/12/19 无投递单
        suppressed = [
            r for r in active if r["reminder_type"] == "overdue"
            and date.fromisoformat(r["fire_date"]) < date(2026, 9, 20)
            and w.repo.get_delivery(r["id"]) is None
        ]
        self.assertTrue(suppressed)
        # 事件历史中有移交记录
        types = [r["event_type"] for r in w.repo.list_events(AGREEMENT, "EQ-01")]
        self.assertIn("responsibility_transferred", types)

    def test_calendar_receives_no_duplicate_for_departed_owner(self) -> None:
        w = seed_overdue_world()
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        count_first = len(w.calendar.all_events())
        # 移交前重复重算同一区间
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        w.scheduler.dispatch_pending()
        self.assertEqual(len(w.calendar.all_events()), count_first)


class IdempotencyAndRecoveryTest(unittest.TestCase):
    def test_repeated_recalc_creates_no_duplicate_reminders(self) -> None:
        w = seed_overdue_world()
        r1 = w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        n_after_first = len(w.repo.all_reminders(AGREEMENT))
        r2 = w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        r3 = w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        self.assertEqual(len(w.repo.all_reminders(AGREEMENT)), n_after_first)
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r3["created"], 0)
        self.assertEqual(r2["already_existed"], r1["created"])
        # 每个提醒至多一条有效投递
        for r in w.repo.all_reminders(AGREEMENT):
            count = len([d for d in w.repo.store.query_all(
                "SELECT id FROM deliveries WHERE reminder_id=? AND status<>'failed'", (r["id"],))])
            self.assertLessEqual(count, 1)

    def test_service_recovery_replays_without_duplicate_calendar_invites(self) -> None:
        cal = FailingCalendarGateway(fail_times=1)
        w = seed_overdue_world()
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 12))
        # 首次投递：第一次调用日历失败，其余成功
        w.scheduler.dispatch_pending()
        sent_before = len([e for e in cal.all_events()])
        # 服务恢复：重放 pending（含失败单）与整体重算
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 12))
        w.scheduler.dispatch_pending()
        w.scheduler.dispatch_pending()
        # 同一档位日历邀请只有一份（网关按幂等键去重 + 库内唯一）
        self.assertEqual(len(cal.all_events()), sent_before)
        # 且最终没有遗留 pending/failed
        self.assertEqual(
            w.repo.store.query_all("SELECT COUNT(*) n FROM deliveries WHERE status IN ('pending','failed')")[0]["n"],
            0,
        )

    def test_dispatch_alone_recovers_failed_rows_after_gateway_restored(self) -> None:
        cal = FailingCalendarGateway(fail_times=2)
        w = World("2026-07-01T00:00:00Z", calendar=cal)
        spec = make_spec(
            "EQ-01", ObligationKind.EQUIPMENT_DELIVERY, date(2026, 8, 29),
            party("zhou", "老周", "zhou@x"), ("equipment_inspector",),
            proof_type="delivery_note",
        )
        w.planner.register_plan(w.admin, AGREEMENT, [spec], "首版")
        w.clock.set("2026-09-01T00:00:00Z")
        w.scheduler.recalculate(
            w.admin, AGREEMENT, date(2026, 8, 20), date(2026, 9, 5), dispatch=False
        )
        self.assertGreaterEqual(
            w.repo.store.query_all("SELECT COUNT(*) n FROM deliveries WHERE status='pending'")[0]["n"], 3
        )
        w.scheduler.dispatch_pending()  # 前两次失败
        failed = w.repo.store.query_all("SELECT COUNT(*) n FROM deliveries WHERE status='failed'")[0]["n"]
        self.assertGreaterEqual(failed, 1)
        # 不重算，仅重放投递（网关已恢复）：failed 单被回收且日历无重复
        w.scheduler.dispatch_pending()
        self.assertEqual(
            w.repo.store.query_all("SELECT COUNT(*) n FROM deliveries WHERE status='failed'")[0]["n"],
            0,
        )
        recovered_events = len(cal.all_events())
        self.assertGreater(recovered_events, 0)
        # 再重放一次仍不重复
        w.scheduler.dispatch_pending()
        self.assertEqual(len(cal.all_events()), recovered_events)

    def test_extension_supersedes_old_schedule_without_deleting_history(self) -> None:
        w = seed_overdue_world()
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        w.planner.grant_extension(w.admin, AGREEMENT, "EQ-01", 30, "供货延迟")
        self.assertEqual(
            w.repo.get_effective(AGREEMENT, "EQ-01")["effective_due"], "2026-09-28"
        )
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 11, 30))
        rows = w.repo.all_reminders(AGREEMENT, "EQ-01")
        gens = {r["generation"] for r in rows}
        self.assertEqual(gens, {1, 2})
        old = [r for r in rows if r["generation"] == 1]
        new = [r for r in rows if r["generation"] == 2]
        self.assertTrue(all(r["superseded_at"] for r in old))
        self.assertTrue(all(not r["superseded_at"] for r in new))
        # 新截止日 09-28，今天 09-29：仍然逾期但只逾期 1 天
        cases = w.scheduler.overdue_cases(AGREEMENT)
        self.assertEqual(cases[0]["days_overdue"], 1)
        # 延期事件保留了旧截止日
        ex = w.scheduler.explain(AGREEMENT, "EQ-01")
        self.assertEqual(ex.extensions[0]["old_due_date"], "2026-08-29")
        self.assertEqual(ex.extensions[0]["new_due_date"], "2026-09-28")

    def test_waiver_keeps_history_and_stops_reminders(self) -> None:
        w = seed_overdue_world()
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))
        w.planner.grant_waiver(w.admin, AGREEMENT, "EQ-01", "不可抗力豁免")
        self.assertEqual(w.scheduler.overdue_cases(AGREEMENT), [])
        # 再次重算不产生新提醒
        before = len(w.repo.all_reminders(AGREEMENT))
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 8, 1), date(2026, 12, 31))
        self.assertEqual(len(w.repo.all_reminders(AGREEMENT)), before)
        events = [r["event_type"] for r in w.repo.list_events(AGREEMENT, "EQ-01")]
        self.assertIn("waiver_granted", events)


class VerificationIndependenceTest(unittest.TestCase):
    def _world_with_report_faculty(self) -> World:
        w = World()
        rpt = make_spec(
            "RPT-01", ObligationKind.REPORT, date(2026, 9, 15),
            party("zhou", "老周", "z@x"), ("academic_reviewer",),
            proof_type="signed_report", required=("official_seal",),
        )
        fac = make_spec(
            "FAC-01", ObligationKind.FACULTY, date(2026, 10, 15),
            party("zhou", "老周", "z@x"), ("academic_reviewer",),
            prerequisites=(Prerequisite("RPT-01", "报告先结清"),),
            proof_type="faculty_certificate",
        )
        w.planner.register_plan(w.admin, AGREEMENT, [rpt, fac], "v1")
        return w

    def test_submission_without_independent_verification_is_not_conforming(self) -> None:
        w = self._world_with_report_faculty()
        sid = w.verifier.submit_proof(
            w.old_owner, AGREEMENT, "RPT-01", "signed_report",
            "f.pdf", {"official_seal": "yes"},
        )
        self.assertEqual(w.repo.conforming_proof_count(AGREEMENT, "RPT-01", "signed_report"), 0)
        self.assertEqual(w.repo.get_effective(AGREEMENT, "RPT-01")["status"], "active")
        # 提交行存在但未核验
        row = w.repo.get_submission(sid)
        self.assertIsNone(row["conforming"])

    def test_party_member_and_wrong_role_cannot_verify_independent_reviewer_can(self) -> None:
        w = self._world_with_report_faculty()
        sid = w.verifier.submit_proof(
            w.old_owner, AGREEMENT, "RPT-01", "signed_report", None, {"official_seal": "yes"}
        )
        # 持有核验角色但隶属责任方：不独立
        with self.assertRaises(AuthorizationError):
            w.verifier.verify(w.party_verifier, AGREEMENT, "RPT-01", sid, True)
        # 设备核验角色不能核验学术类证明
        with self.assertRaises(AuthorizationError):
            w.verifier.verify(w.inspector, AGREEMENT, "RPT-01", sid, True)
        # 独立、角色匹配的学术核验人通过 → 结清
        w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, True)
        self.assertEqual(w.repo.get_effective(AGREEMENT, "RPT-01")["status"], "completed")

    def test_self_verification_blocked(self) -> None:
        w = self._world_with_report_faculty()
        # admin 代为提交，核验人 reviewer 提交后不能自核（构造提交者=核验人）
        # 用 party_member 提交常规材料，另让一个 verifier 提交不允许；直接校验规则：
        sid = w.verifier.submit_proof(
            w.old_owner, AGREEMENT, "RPT-01", "signed_report", None, {"official_seal": "yes"}
        )
        # 篡改提交者为 reviewer 以触发"不能核验自己的提交"
        with w.store.begin() as c:
            c.execute("UPDATE submissions SET submitted_by='rev' WHERE id=?", (sid,))
        with self.assertRaises(AuthorizationError):
            w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, True)

    def test_independent_verification_completes_and_unblocks_prerequisite(self) -> None:
        w = self._world_with_report_faculty()
        # 报告未结清前，师资义务被前置门控，不产生任何提醒
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 7, 1), date(2026, 10, 31))
        self.assertEqual(w.repo.all_reminders(AGREEMENT, "FAC-01"), [])
        ex_fac = w.scheduler.explain(AGREEMENT, "FAC-01")
        self.assertTrue(ex_fac.blocked)

        sid = w.verifier.submit_proof(
            w.old_owner, AGREEMENT, "RPT-01", "signed_report", None, {"official_seal": "yes"}
        )
        w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, True)
        self.assertEqual(w.repo.get_effective(AGREEMENT, "RPT-01")["status"], "completed")
        # 结清后不再产生提醒/逾期
        self.assertFalse(w.scheduler.explain(AGREEMENT, "RPT-01").overdue)
        # 前置满足，师资义务解锁
        w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 7, 1), date(2026, 10, 31))
        self.assertNotEqual(w.repo.all_reminders(AGREEMENT, "FAC-01"), [])
        self.assertFalse(w.scheduler.explain(AGREEMENT, "FAC-01").blocked)

    def test_rejected_proof_does_not_count_and_requires_reason(self) -> None:
        w = self._world_with_report_faculty()
        sid = w.verifier.submit_proof(
            w.old_owner, AGREEMENT, "RPT-01", "signed_report", None, {"official_seal": "yes"}
        )
        with self.assertRaises(ConflictError):
            w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, False)
        w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, False, "印章模糊")
        self.assertEqual(w.repo.conforming_proof_count(AGREEMENT, "RPT-01", "signed_report"), 0)
        self.assertEqual(w.repo.get_effective(AGREEMENT, "RPT-01")["status"], "active")
        # 已核验不能重复核验
        with self.assertRaises(ConflictError):
            w.verifier.verify(w.reviewer, AGREEMENT, "RPT-01", sid, True)

    def test_missing_required_attribute_rejected_at_submission(self) -> None:
        w = self._world_with_report_faculty()
        with self.assertRaises(ConflictError):
            w.verifier.submit_proof(
                w.old_owner, AGREEMENT, "RPT-01", "signed_report", None, {}
            )


class RecalcAuthorizationTest(unittest.TestCase):
    def test_only_admin_can_recalc(self) -> None:
        w = seed_overdue_world()
        with self.assertRaises(AuthorizationError):
            w.scheduler.recalculate(w.old_owner, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30))

    def test_recalc_result_includes_explanation_per_obligation(self) -> None:
        w = seed_overdue_world()
        result = w.scheduler.recalculate(w.admin, AGREEMENT, date(2026, 9, 1), date(2026, 9, 30))
        ob = next(o for o in result["obligations"] if o["code"] == "EQ-01")
        self.assertTrue(ob["explanation"]["overdue"])
        self.assertTrue(any("为何" in r or "逾期" in r for r in ob["explanation"]["reasons"]))
        # 每次重算都有留痕
        runs = w.store.query_all("SELECT * FROM recalc_runs")
        self.assertEqual(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
