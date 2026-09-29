"""领域值对象与枚举。

所有对象只描述"是什么"，不触碰数据库；规则在 services 中实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: datetime | str) -> datetime:
    """把输入规范成 UTC 感知时间。

    接受带 Z 后缀的 ISO 串；朴素时间一律按 UTC 处理，避免二义性。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip())


def iso(dt: datetime) -> str:
    return parse_dt(dt).isoformat().replace("+00:00", "Z")


class ObligationKind(str, Enum):
    EQUIPMENT_DELIVERY = "equipment_delivery"  # 设备交付
    REPORT = "report"                          # 报告提交
    FACULTY = "faculty"                        # 师资义务

    @property
    def label(self) -> str:
        return {
            "equipment_delivery": "设备交付",
            "report": "报告",
            "faculty": "师资",
        }[self.value]


class ReminderType(str, Enum):
    ADVANCE = "advance"   # 到期前提醒
    DUE = "due"           # 到期当日提醒
    OVERDUE = "overdue"   # 逾期提醒


class ObligationStatus(str, Enum):
    ACTIVE = "active"                 # 生效中
    WAIVED = "waived"                 # 被豁免（记录保留，不再履行）
    COMPLETED = "completed"           # 已由独立核验确认结清


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class EventType(str, Enum):
    PLAN_VERSIONED = "plan_versioned"
    RESPONSIBILITY_TRANSFERRED = "responsibility_transferred"
    EXTENSION_GRANTED = "extension_granted"
    WAIVER_GRANTED = "waiver_granted"
    SUBMISSION_RECORDED = "submission_recorded"
    VERIFICATION_APPROVED = "verification_approved"
    VERIFICATION_REJECTED = "verification_rejected"
    REMINDER_DISPATCHED = "reminder_dispatched"
    REMINDER_FAILED = "reminder_failed"
    RECALCULATED = "recalculated"


@dataclass(frozen=True)
class TimeWindow:
    """义务时间窗：起始日、基础截止日、提前提醒天数、逾期升级天数。"""

    start_date: date
    due_date: date
    advance_days: tuple[int, ...] = (7, 1)
    overdue_interval_days: int = 7
    overdue_max_reminders: int = 12  # 防止逾期案件无限升级

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "advance_days": list(self.advance_days),
            "overdue_interval_days": self.overdue_interval_days,
            "overdue_max_reminders": self.overdue_max_reminders,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TimeWindow":
        return cls(
            start_date=parse_date(value["start_date"]),
            due_date=parse_date(value["due_date"]),
            advance_days=tuple(value.get("advance_days", [7, 1])),
            overdue_interval_days=value.get("overdue_interval_days", 7),
            overdue_max_reminders=value.get("overdue_max_reminders", 12),
        )


@dataclass(frozen=True)
class ProofRequirement:
    """证明要求：要提交什么、谁可以核验、需要什么属性。"""

    proof_type: str                       # delivery_note / signed_report / faculty_certificate ...
    description: str
    verifier_roles: tuple[str, ...]       # 允许核验的角色代码
    required_attributes: tuple[str, ...] = ()  # 证明必须带有的字段，如 ["conformity_check"]
    minimum_count: int = 1                # 至少需要几份合格证明

    def to_dict(self) -> dict[str, Any]:
        return {
            "proof_type": self.proof_type,
            "description": self.description,
            "verifier_roles": list(self.verifier_roles),
            "required_attributes": list(self.required_attributes),
            "minimum_count": self.minimum_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProofRequirement":
        return cls(
            proof_type=value["proof_type"],
            description=value.get("description", ""),
            verifier_roles=tuple(value.get("verifier_roles", ())),
            required_attributes=tuple(value.get("required_attributes", ())),
            minimum_count=value.get("minimum_count", 1),
        )


@dataclass(frozen=True)
class ResponsibleParty:
    """责任方及其负责人（提醒接收人）。

    责任移交会产生新的 ResponsibleParty 版本；旧版本与旧接收人保留在历史中。
    """

    party_id: str
    party_name: str
    owner_user_id: str
    owner_name: str
    contact: str = ""  # 日历投递地址（邮箱 / 日历账号）

    def to_dict(self) -> dict[str, Any]:
        return {
            "party_id": self.party_id,
            "party_name": self.party_name,
            "owner_user_id": self.owner_user_id,
            "owner_name": self.owner_name,
            "contact": self.contact,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResponsibleParty":
        return cls(
            party_id=value["party_id"],
            party_name=value["party_name"],
            owner_user_id=value["owner_user_id"],
            owner_name=value.get("owner_name", ""),
            contact=value.get("contact", ""),
        )


@dataclass(frozen=True)
class Prerequisite:
    """前置条件：本义务开始/到期前必须已结清的另一项义务。"""

    obligation_code: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"obligation_code": self.obligation_code, "description": self.description}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Prerequisite":
        return cls(
            obligation_code=value["obligation_code"],
            description=value.get("description", ""),
        )


@dataclass(frozen=True)
class ObligationSpec:
    """一项义务的完整登记内容（计划版本快照的一部分）。"""

    code: str
    kind: ObligationKind
    title: str
    responsible: ResponsibleParty
    window: TimeWindow
    proof: ProofRequirement
    prerequisites: tuple[Prerequisite, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "kind": self.kind.value,
            "title": self.title,
            "responsible": self.responsible.to_dict(),
            "window": self.window.to_dict(),
            "proof": self.proof.to_dict(),
            "prerequisites": [p.to_dict() for p in self.prerequisites],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObligationSpec":
        return cls(
            code=value["code"],
            kind=ObligationKind(value["kind"]),
            title=value["title"],
            responsible=ResponsibleParty.from_dict(value["responsible"]),
            window=TimeWindow.from_dict(value["window"]),
            proof=ProofRequirement.from_dict(value["proof"]),
            prerequisites=tuple(Prerequisite.from_dict(p) for p in value.get("prerequisites", ())),
        )


@dataclass(frozen=True)
class User:
    """系统用户：协议管理员、责任院校联系人、核验人员。"""

    user_id: str
    name: str
    role: str           # admin / party_member / verifier
    party_id: str | None = None  # 隶属责任方；独立核验判定用
