"""测试夹具：构造合作协议场景。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from obligation_service.clock import FixedClock
from obligation_service.models import Actor, ROLE_ADMIN, ROLE_OWNER, ROLE_VERIFIER
from obligation_service.scheduler import Scheduler
from obligation_service.seed_data import SEED_AGREEMENT, seed_plan_payload
from obligation_service.services import RegistrationService
from obligation_service.store import Store
from obligation_service.verification import VerificationService


def admin() -> Actor:
    return Actor("u-admin", "李管理员", ROLE_ADMIN, "administrative-office")


def old_owner() -> Actor:
    return Actor("u-zhang", "张主任（离任）", ROLE_OWNER, "houqinchu")


def new_owner() -> Actor:
    return Actor("u-wang", "王主任（接任）", ROLE_OWNER, "equipment-dept")


def college_owner(party: str = "college", uid: str = "u-college", name: str = "院校经办人") -> Actor:
    return Actor(uid, name, ROLE_OWNER, party)


def verifier(party: str = "quality-office", uid: str = "u-verifier", name: str = "核验员") -> Actor:
    return Actor(uid, name, ROLE_VERIFIER, party)


def clock(day: str = "2026-09-29") -> FixedClock:
    return FixedClock(datetime.fromisoformat(day + "T09:00:00+00:00"))


def build_world(day: str = "2026-09-29", db: str = ":memory:"):
    clk = clock(day)
    store = Store(db)
    reg = RegistrationService(store, clk)
    ver = VerificationService(store, clk)
    sch = Scheduler(store, clk)
    return {
        "store": store, "clock": clk, "reg": reg, "ver": ver, "sch": sch,
        "admin": admin(), "old_owner": old_owner(), "new_owner": new_owner(),
        "college": college_owner(), "verifier": verifier(),
    }


def sample_plan_payload() -> dict:
    """合作协议三项义务：设备交付 / 年度报告 / 师资培训。"""
    return seed_plan_payload()


def register_sample_world(world: dict) -> dict:
    agreement = world["reg"].create_agreement(world["admin"], dict(SEED_AGREEMENT))
    plan = world["reg"].register_plan(world["admin"], agreement["agreement_id"], sample_plan_payload())
    duties = {d["code"]: d["duty_id"] for d in plan["duties"]}
    return {"agreement_id": agreement["agreement_id"], "plan_id": plan["plan_id"], "duties": duties}
