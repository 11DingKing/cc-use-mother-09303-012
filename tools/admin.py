#!/usr/bin/env python3
"""协议管理员命令行。

子命令：
  seed       播种演示场景（设备交付已逾期一个月、负责人离任、报告/师资证明待核验）
  serve      启动 HTTP 服务
  recalc     以管理员身份重算指定区间，幂等生成提醒，输出每项义务"为何到期"
  explain    查看一项或全部义务的到期推导链
  overdue    列出逾期案件
  history    查看不可变事件历史（延期/豁免/移交/核验/投递）
  reminders  查看提醒（默认仅活跃，--all 含被取代的历史提醒）
  dispatch   重放全部待投递提醒（服务恢复后安全重复执行）
  transfer / extension / waiver / submit / verify / grant-role  生命周期演练

所有时间取系统 UTC 时钟；场景日期固定，便于对照"逾期一个月"。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from obligation import (  # noqa: E402
    FixedClock,
    ObligationKind,
    ObligationSpec,
    ProofRequirement,
    Prerequisite,
    Repository,
    ResponsibleParty,
    SchedulerService,
    Store,
    SystemClock,
    TimeWindow,
    User,
)
from obligation.errors import DomainError  # noqa: E402
from obligation.planner import PlannerService  # noqa: E402
from obligation.server import build_server  # noqa: E402
from obligation.verification import VerificationService  # noqa: E402

DEFAULT_DB = str(ROOT / "data" / "obligation.db")
AGREEMENT = "A-09303-012"


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------

def bootstrap(db_path: str, clock=None):
    store = Store(db_path)
    clk = clock or SystemClock()
    repo = Repository(store)
    planner = PlannerService(repo, clk)
    scheduler = SchedulerService(repo, clk)
    verifier = VerificationService(repo, clk)
    return store, repo, clk, planner, scheduler, verifier


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def must_user(repo: Repository, user_id: str) -> User:
    user = repo.get_user(user_id)
    if user is None:
        raise SystemExit(f"用户不存在：{user_id}（可先 seed）")
    return user


# --------------------------------------------------------------------------
# 演示场景
# --------------------------------------------------------------------------

def cmd_seed(args) -> None:
    """播种与需求对应的完整场景。

    日期基准：今天 2026-09-29。
    EQ-01 设备交付截止 2026-08-29（逾期约一个月），首任负责人老周已离任；
    RPT-01 报告截止 2026-09-15，已提交证明但尚无独立核验；
    FAC-01 师资义务截止 2026-10-15，以前置"报告结清"为依赖。
    """
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db, clock=FixedClock("2026-07-01T09:00:00Z"))

    admin = User("u-admin", "林管理员", "admin")
    old_owner = User("u-zhou", "周伯（离任负责人）", "party_member", party_id="P-VOC-1")
    new_owner = User("u-wu", "吴欣（新任负责人）", "party_member", party_id="P-VOC-1")
    insp = User("u-insp", "设备核验员钱检", "verifier")          # 独立第三方，无 party_id
    reviewer = User("u-review", "学术核验员孙审", "verifier")
    for user, token in [
        (admin, "tok-admin"), (old_owner, "tok-zhou"),
        (new_owner, "tok-wu"), (insp, "tok-insp"), (reviewer, "tok-review"),
    ]:
        repo.upsert_user(user, token)
    planner.grant_verifier_role(admin, "u-insp", "equipment_inspector")
    planner.grant_verifier_role(admin, "u-review", "academic_reviewer")

    planner.create_agreement(admin, AGREEMENT, "HZXY-2026-012", "校企合作协议（设备交付/报告/师资）")

    old_party = ResponsibleParty("P-VOC-1", "致远职业学院", "u-zhou", "周伯（离任负责人）", "zhou@example.edu")
    new_party = ResponsibleParty("P-VOC-1", "致远职业学院", "u-wu", "吴欣（新任负责人）", "wu@example.edu")

    equipment = ObligationSpec(
        code="EQ-01", kind=ObligationKind.EQUIPMENT_DELIVERY, title="实训设备交付与安装验收",
        responsible=old_party,
        window=TimeWindow(date(2026, 7, 1), date(2026, 8, 29), advance_days=(7, 1)),
        proof=ProofRequirement(
            "delivery_note", "设备签收单与安装验收合格记录",
            verifier_roles=("equipment_inspector",),
            required_attributes=("serial_list", "install_conformity"),
        ),
    )
    report = ObligationSpec(
        code="RPT-01", kind=ObligationKind.REPORT, title="合作中期报告",
        responsible=old_party,
        window=TimeWindow(date(2026, 7, 1), date(2026, 9, 15), advance_days=(7, 3, 1)),
        proof=ProofRequirement(
            "signed_report", "盖章中期报告及数据附件",
            verifier_roles=("academic_reviewer",),
            required_attributes=("official_seal",),
        ),
    )
    faculty = ObligationSpec(
        code="FAC-01", kind=ObligationKind.FACULTY, title="派驻师资名单与资格证明",
        responsible=old_party,
        window=TimeWindow(date(2026, 8, 1), date(2026, 10, 15), advance_days=(14, 7, 1)),
        proof=ProofRequirement(
            "faculty_certificate", "师资资格证书与派驻协议",
            verifier_roles=("academic_reviewer",),
            required_attributes=("qualification_cert",),
        ),
        prerequisites=(Prerequisite("RPT-01", "中期报告结清后方启动师资派驻核验"),),
    )
    v1 = planner.register_plan(admin, AGREEMENT, [equipment, report, faculty], "首版计划登记")

    # 8 月 30 日调度：当时负责人还是老周，到期前/到期提醒都投给了他
    clk.set("2026-08-30T09:00:00Z")
    scheduler.recalculate(admin, AGREEMENT, date(2026, 8, 1), date(2026, 9, 30), dispatch=True)

    # 9 月 20 日责任移交：旧提醒保留（可证明当时发给了谁），新提醒改投新负责人；
    # 移交日之前的档位不向新负责人补发。
    clk.set("2026-09-20T09:00:00Z")
    v2 = planner.transfer_responsibility(
        admin, AGREEMENT, "EQ-01", new_party, reason="原负责人周伯离任，由吴欣接手"
    )
    # 报告/师资一并移交
    for code in ("RPT-01", "FAC-01"):
        planner.transfer_responsibility(
            admin, AGREEMENT, code, new_party, reason="原负责人周伯离任，由吴欣接手"
        )
    scheduler.recalculate(admin, AGREEMENT, date(2026, 9, 1), date(2026, 11, 30), dispatch=True)

    # 今天 9 月 29 日：设备交付已逾期一个月；新一轮逾期升级投递给新任负责人。
    clk.set("2026-09-29T09:00:00Z")
    scheduler.recalculate(admin, AGREEMENT, date(2026, 9, 20), date(2026, 9, 30), dispatch=True)

    # 学院已提交一份中期报告证明，但系统中尚无核验人员确认 → 不能算合格
    sid = verifier.submit_proof(
        new_owner, AGREEMENT, "RPT-01", "signed_report",
        "docs/mid_report.pdf", {"official_seal": "yes"},
    )

    summary = {
        "db": args.db,
        "agreement_id": AGREEMENT,
        "plan_versions": {"v1": v1, "latest": v2},
        "today": "2026-09-29",
        "scenario": {
            "EQ-01": "设备交付截止 2026-08-29，已逾期约一个月；负责人由周伯移交吴欣",
            "RPT-01": f"报告截止 2026-09-15；证明 submission_id={sid} 已提交，等待独立核验",
            "FAC-01": "师资义务截止 2026-10-15；前置 RPT-01 未结清，暂不触发",
        },
        "tokens": {
            "admin": "tok-admin", "old_owner": "tok-zhou", "new_owner": "tok-wu",
            "equipment_verifier": "tok-insp", "academic_verifier": "tok-review",
        },
        "next_steps": [
            "python3 tools/admin.py overdue " + AGREEMENT,
            f"python3 tools/admin.py explain {AGREEMENT} --code EQ-01",
            f"python3 tools/admin.py recalc {AGREEMENT} --start 2026-09-01 --end 2026-10-15",
            f"python3 tools/admin.py verify {AGREEMENT} RPT-01 {sid} --user u-review --conforming",
            f"python3 tools/admin.py history {AGREEMENT} --code EQ-01",
        ],
    }
    print_json(summary)
    store.close()


# --------------------------------------------------------------------------
# 读命令
# --------------------------------------------------------------------------

def cmd_explain(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    if args.code:
        print_json(scheduler.explain(args.agreement, args.code).to_dict())
    else:
        print_json([e.to_dict() for e in scheduler.explain_all(args.agreement)])
    store.close()


def cmd_overdue(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    print_json(scheduler.overdue_cases(args.agreement))
    store.close()


def cmd_history(args) -> None:
    store, repo, *_ = bootstrap(args.db)
    rows = repo.list_events(args.agreement, args.code)
    print_json([
        {"id": r["id"], "occurred_at": r["occurred_at"], "obligation_code": r["obligation_code"],
         "event_type": r["event_type"], "actor_user_id": r["actor_user_id"],
         "payload": json.loads(r["payload_json"])}
        for r in rows
    ])
    store.close()


def cmd_reminders(args) -> None:
    store, repo, *_ = bootstrap(args.db)
    rows = repo.all_reminders(args.agreement, args.code)
    out = []
    for r in rows:
        if not args.all and r["superseded_at"]:
            continue
        delivery = repo.get_delivery(r["id"])
        out.append({
            "id": r["id"], "code": r["code"], "generation": r["generation"],
            "type": r["reminder_type"], "seq_no": r["seq_no"],
            "due_date": r["due_date"], "fire_date": r["fire_date"],
            "owner_user_id": r["owner_user_id"], "recipient": r["recipient"],
            "superseded_at": r["superseded_at"],
            "delivery": dict(delivery) if delivery else None,
        })
    print_json(out)
    store.close()


# --------------------------------------------------------------------------
# 管理写命令
# --------------------------------------------------------------------------

def cmd_recalc(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    result = scheduler.recalculate(
        admin, args.agreement, args.start, args.end, dispatch=not args.no_dispatch
    )
    print_json(result)
    store.close()


def cmd_dispatch(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    if admin.role != "admin":
        raise SystemExit("仅协议管理员可触发投递重放")
    print_json(scheduler.dispatch_pending())
    store.close()


def cmd_tick(args) -> None:
    """定时任务入口：按真实时钟对全部协议幂等补算近 N 天并投递。

    供 cron/调度平台每天调用；重复调用与停机恢复均安全（全链路幂等）。
    """
    import datetime as _dt

    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    today = clk.now().date()
    start = today - _dt.timedelta(days=args.catch_up_days)
    summary = []
    for row in repo.list_agreements():
        result = scheduler.recalculate(admin, row["agreement_id"], start, today, dispatch=True)
        summary.append({
            "agreement_id": row["agreement_id"],
            "created": result["created"], "already_existed": result["already_existed"],
            "dispatched": result["dispatched"],
        })
    print_json({"today": today.isoformat(), "catch_up_days": args.catch_up_days, "agreements": summary})
    store.close()


def cmd_transfer(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    owner = must_user(repo, args.owner)
    party = ResponsibleParty(
        owner.party_id or args.party_id, args.party_name or owner.party_id or args.party_id,
        owner.user_id, owner.name, args.contact,
    )
    version_no = planner.transfer_responsibility(admin, args.agreement, args.code, party, args.reason)
    print_json({"version_no": version_no, "new_owner": owner.user_id})
    store.close()


def cmd_extension(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    new_due = planner.grant_extension(admin, args.agreement, args.code, args.days, args.reason)
    print_json({"effective_due_date": new_due})
    store.close()


def cmd_waiver(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    planner.grant_waiver(admin, args.agreement, args.code, args.reason)
    print_json({"status": "waived"})
    store.close()


def cmd_submit(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    actor = must_user(repo, args.user)
    attributes = json.loads(args.attributes) if args.attributes else {}
    sid = verifier.submit_proof(actor, args.agreement, args.code, args.proof_type, args.file_ref, attributes)
    print_json({"submission_id": sid, "status": "pending_verification"})
    store.close()


def cmd_verify(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    actor = must_user(repo, args.user)
    verifier.verify(
        actor, args.agreement, args.code, args.submission_id,
        bool(args.conforming), args.reject_reason,
    )
    print_json({"submission_id": args.submission_id, "conforming": bool(args.conforming)})
    store.close()


def cmd_grant_role(args) -> None:
    store, repo, clk, planner, scheduler, verifier = bootstrap(args.db)
    admin = must_user(repo, args.user)
    planner.grant_verifier_role(admin, args.target, args.role_code)
    print_json({"user_id": args.target, "granted_role": args.role_code})
    store.close()


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

def cmd_serve(args) -> None:
    from obligation.server import Application

    store = Store(args.db)
    app = Application(store, clock=SystemClock())
    server = build_server(args.host, args.port, app)
    print(f"服务已启动：http://{args.host}:{args.port}（健康检查 GET /health）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合作协议义务提醒：协议管理员命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="播种演示场景")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("recalc", help="重算指定区间（幂等）")
    p.add_argument("agreement")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--no-dispatch", action="store_true", help="只生成提醒/投递单，不立即发日历")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_recalc)

    p = sub.add_parser("explain", help="查看义务到期推导链")
    p.add_argument("agreement")
    p.add_argument("--code")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("overdue", help="列出逾期案件")
    p.add_argument("agreement")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_overdue)

    p = sub.add_parser("history", help="查看不可变事件历史")
    p.add_argument("agreement")
    p.add_argument("--code")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("reminders", help="查看提醒")
    p.add_argument("agreement")
    p.add_argument("--code")
    p.add_argument("--all", action="store_true", help="含已被取代的历史提醒")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_reminders)

    p = sub.add_parser("dispatch", help="重放待投递提醒")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("tick", help="定时任务：按真实时钟补算近 N 天并投递（幂等）")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--catch-up-days", type=int, default=3, help="停机恢复时向前补算天数")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("transfer", help="责任移交")
    p.add_argument("agreement")
    p.add_argument("code")
    p.add_argument("--owner", required=True, help="新任负责人 user_id")
    p.add_argument("--party-id", default="")
    p.add_argument("--party-name", default="")
    p.add_argument("--contact", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_transfer)

    p = sub.add_parser("extension", help="延期")
    p.add_argument("agreement")
    p.add_argument("code")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_extension)

    p = sub.add_parser("waiver", help="豁免")
    p.add_argument("agreement")
    p.add_argument("code")
    p.add_argument("--reason", default="")
    p.add_argument("--user", default="u-admin")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_waiver)

    p = sub.add_parser("submit", help="提交证明")
    p.add_argument("agreement")
    p.add_argument("code")
    p.add_argument("--proof-type", required=True)
    p.add_argument("--file-ref")
    p.add_argument("--attributes", help="JSON 对象")
    p.add_argument("--user", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("verify", help="独立核验证明")
    p.add_argument("agreement")
    p.add_argument("code")
    p.add_argument("submission_id", type=int)
    p.add_argument("--user", required=True, help="核验人员 user_id")
    p.add_argument("--conforming", action="store_true")
    p.add_argument("--reject-reason")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("grant-role", help="授予核验角色")
    p.add_argument("target")
    p.add_argument("--role-code", required=True)
    p.add_argument("--user", default="u-admin")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_grant_role)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    try:
        args.func(args)
    except DomainError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
