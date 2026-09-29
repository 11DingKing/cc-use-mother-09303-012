#!/usr/bin/env python3
"""义务提醒服务管理命令。

用法示例：
  python -m obligation_service.cli serve --db data/app.db --port 8080
  python -m obligation_service.cli dispatch --db data/app.db [--as-of 2026-09-29]
  python -m obligation_service.cli recalculate --db data/app.db \
      --from 2026-08-01 --to 2026-09-29 [--plan plan_xxx]
  python -m obligation_service.cli explain --db data/app.db --duty duty_xxx [--as-of 2026-09-29]
  python -m obligation_service.cli cases --db data/app.db [--status open]
  python -m obligation_service.cli reminders --db data/app.db [--duty duty_xxx]
  python -m obligation_service.cli trail --db data/app.db --duty duty_xxx

``--as-of`` 把时钟固定在指定日期（可注入时钟），用于复盘与演练。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from .clock import FixedClock, SystemClock
from .errors import DomainError
from .models import Actor, ROLE_ADMIN
from .scheduler import Scheduler
from .services import RegistrationService
from .store import Store
from .verification import VerificationService


def _clock(args: argparse.Namespace):
    if getattr(args, "as_of", None):
        return FixedClock(datetime.fromisoformat(args.as_of + "T09:00:00+00:00"))
    return SystemClock()


def _admin() -> Actor:
    return Actor(actor_id="cli-admin", name="命令行管理员", role=ROLE_ADMIN, party="administrative-office")


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_serve(args: argparse.Namespace) -> int:
    from .app import AppContext, build_server

    store = Store(args.db)
    ctx = AppContext(store, SystemClock())
    server = build_server(args.host, args.port, ctx)
    print(f"服务监听 http://{args.host}:{args.port}（数据库 {args.db}）", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        scheduler = Scheduler(store, _clock(args))
        result = scheduler.dispatch(plan_id=args.plan)
        _print(result)
        return 0
    finally:
        store.close()


def cmd_recalculate(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        scheduler = Scheduler(store, _clock(args))
        result = scheduler.recalculate(
            _admin(), range_start=args.range_from, range_end=args.range_to, plan_id=args.plan
        )
        _print(result)
        return 0
    finally:
        store.close()


def cmd_explain(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        scheduler = Scheduler(store, _clock(args))
        _print(scheduler.explain(args.duty, args.as_of))
        return 0
    finally:
        store.close()


def cmd_cases(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        sql = (
            "SELECT c.*, o.code AS duty_code FROM overdue_cases c"
            " JOIN obligations o ON o.duty_id=c.duty_id"
        )
        params: list = []
        if args.status:
            sql += " WHERE c.status=?"
            params.append(args.status)
        sql += " ORDER BY c.opened_on"
        rows = [dict(r) for r in store.fetchall(sql, tuple(params))]
        _print({"cases": rows, "count": len(rows)})
        return 0
    finally:
        store.close()


def cmd_reminders(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        sql = "SELECT * FROM reminders"
        params: list = []
        if args.duty:
            sql += " WHERE duty_id=?"
            params.append(args.duty)
        sql += " ORDER BY scheduled_for, duty_id"
        rows = [dict(r) for r in store.fetchall(sql, tuple(params))]
        _print({"reminders": rows, "count": len(rows)})
        return 0
    finally:
        store.close()


def cmd_trail(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        service = VerificationService(store, SystemClock())
        _print(service.trail(args.duty))
        return 0
    finally:
        store.close()


def cmd_seed(args: argparse.Namespace) -> int:
    """写入演示场景：设备交付 / 师资培训 / 年度报告三项义务。"""
    from .seed_data import SEED_AGREEMENT, seed_plan_payload

    store = Store(args.db)
    try:
        clk = _clock(args)
        reg = RegistrationService(store, clk)
        existing = store.fetchone("SELECT 1 FROM agreements WHERE code=?", (SEED_AGREEMENT["code"],))
        if existing and not args.force:
            print("演示数据已存在；使用 --force 可重新登记（不影响已有数据）", file=sys.stderr)
            return 1
        agreement = reg.create_agreement(_admin(), SEED_AGREEMENT)
        plan = reg.register_plan(_admin(), agreement["agreement_id"], seed_plan_payload())
        _print({
            "agreement_id": agreement["agreement_id"],
            "plan_id": plan["plan_id"],
            "duties": plan["duties"],
            "registered_on": plan["registered_on"],
            "next_step": "python -m obligation_service.cli dispatch --db " + args.db
            + (f" --as-of {args.as_of}" if args.as_of else ""),
        })
        return 0
    finally:
        store.close()


def cmd_duty(args: argparse.Namespace) -> int:
    store = Store(args.db)
    try:
        service = RegistrationService(store, SystemClock())
        _print(service.get_duty_detail(args.duty))
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obligation-cli", description="合作协议义务提醒管理命令")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db(p):
        p.add_argument("--db", default="data/obligations.db", help="SQLite 数据库路径")

    def add_asof(p):
        p.add_argument("--as-of", dest="as_of", help="固定时钟日期 YYYY-MM-DD（复盘/演练）")

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    add_db(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("seed", help="登记演示协议与三项义务")
    add_db(p); add_asof(p)
    p.add_argument("--force", action="store_true", help="已存在同名协议时仍继续")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("dispatch", help="按当前时钟执行一次调度")
    add_db(p); add_asof(p)
    p.add_argument("--plan", help="仅限指定计划")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("recalculate", help="重算指定区间并给出到期解释（仅管理员）")
    add_db(p); add_asof(p)
    p.add_argument("--from", dest="range_from", required=True, help="区间起 YYYY-MM-DD")
    p.add_argument("--to", dest="range_to", required=True, help="区间止 YYYY-MM-DD")
    p.add_argument("--plan", help="仅限指定计划")
    p.set_defaults(func=cmd_recalculate)

    p = sub.add_parser("explain", help="查看每项义务为何到期")
    add_db(p); add_asof(p)
    p.add_argument("--duty", required=True)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("cases", help="列出逾期案件")
    add_db(p)
    p.add_argument("--status", choices=["open", "resolved"])
    p.set_defaults(func=cmd_cases)

    p = sub.add_parser("reminders", help="列出提醒")
    add_db(p)
    p.add_argument("--duty")
    p.set_defaults(func=cmd_reminders)

    p = sub.add_parser("trail", help="查看提交与核验留痕")
    add_db(p)
    p.add_argument("--duty", required=True)
    p.set_defaults(func=cmd_trail)

    p = sub.add_parser("duty", help="查看义务版本链")
    add_db(p)
    p.add_argument("--duty", required=True)
    p.set_defaults(func=cmd_duty)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DomainError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
