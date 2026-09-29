"""领域模型：角色、义务修订、时间窗、证明要求、前置条件。

核心设计：义务（``Obligation``）的全部可变内容都保存在**追加式修订链**
``ObligationRevision`` 中。登记、延期、豁免、责任移交各产生一个新版本，
旧版本永不修改，任何时点的有效内容都可按日期重放。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from .errors import ValidationError
from .util import parse_date

# ---- 角色（与 domain/contract.json 的 actors 对齐）----
ROLE_ADMIN = "协议管理员"
ROLE_OWNER = "责任院校"
ROLE_VERIFIER = "核验人员"
ALL_ROLES = (ROLE_ADMIN, ROLE_OWNER, ROLE_VERIFIER)
# HTTP 头等传输场景使用的稳定 ASCII 代码
ROLE_CODES = {"admin": ROLE_ADMIN, "owner": ROLE_OWNER, "verifier": ROLE_VERIFIER}

RevisionType = Literal["register", "extend", "exempt", "transfer", "amend"]
Decision = Literal["approved", "rejected"]

RECURRENCE = ("once", "monthly", "quarterly", "yearly", "every_days")
MONTH_STEP = {"monthly": 1, "quarterly": 3, "yearly": 12}


@dataclass(frozen=True)
class Actor:
    """操作人：身份 + 角色 + 所属方（独立性判断需要所属方）。"""

    actor_id: str
    name: str
    role: str
    party: str

    @classmethod
    def from_headers(cls, headers: Any) -> "Actor":
        get = headers.get
        actor_id = get("x-actor-id") or get("X-Actor-Id")
        name = get("x-actor-name") or get("X-Actor-Name") or actor_id
        role = get("x-actor-role") or get("X-Actor-Role")
        party = get("x-actor-party") or get("X-Actor-Party")
        if not actor_id or not role:
            raise ValidationError("缺少操作人身份头：X-Actor-Id / X-Actor-Role")
        role = ROLE_CODES.get(role, role)
        if role not in ALL_ROLES:
            raise ValidationError(f"未知角色：{role}")
        if not party:
            raise ValidationError("缺少操作人所属方：X-Actor-Party")
        return cls(actor_id=actor_id, name=name, role=role, party=party)

    def to_dict(self) -> dict[str, Any]:
        return {"actor_id": self.actor_id, "name": self.name, "role": self.role, "party": self.party}


@dataclass(frozen=True)
class EvidenceRequirement:
    """证明要求：需要哪类合格证明、是否必须提交原件。"""

    expected_types: tuple[str, ...]
    require_original: bool = False
    note: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "EvidenceRequirement":
        if not value:
            return cls(expected_types=())
        types = value.get("expected_types") or value.get("types") or []
        if not isinstance(types, list) or not all(isinstance(t, str) and t for t in types):
            raise ValidationError("证明要求 expected_types 必须是非空字符串列表")
        return cls(
            expected_types=tuple(types),
            require_original=bool(value.get("require_original", False)),
            note=str(value.get("note", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_types": list(self.expected_types),
            "require_original": self.require_original,
            "note": self.note,
        }


@dataclass(frozen=True)
class Prerequisite:
    """前置义务依赖：按义务编码引用，必须已由核验人员确认合格。"""

    duty_code: str
    same_window: bool = True  # 是否要求同一期次（一次性义务恒为同一期）

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Prerequisite":
        code = value.get("duty_code")
        if not code:
            raise ValidationError("前置条件缺少 duty_code")
        return cls(duty_code=code, same_window=bool(value.get("same_window", True)))


@dataclass(frozen=True)
class TimeWindow:
    """义务时间窗。

    - recurrence=once：一次性义务，``first_due`` 即截止日；
    - 周期义务：以 first_due 为首期截止日，按周期/天数滚动；
    - remind_before_days：提前提醒偏移（如 [7, 1]）；
    - grace_days：截止日后宽限天数，超过才立逾期案件。
    """

    first_due: date
    recurrence: str = "once"
    interval_days: int | None = None
    remind_before_days: tuple[int, ...] = (7, 1)
    grace_days: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TimeWindow":
        if not value:
            raise ValidationError("缺少时间窗 time_window")
        due = parse_date(value.get("first_due"), "first_due")
        recurrence = value.get("recurrence", "once")
        if recurrence not in RECURRENCE:
            raise ValidationError(f"不支持的周期类型：{recurrence}")
        interval = value.get("interval_days")
        if recurrence == "every_days":
            if not isinstance(interval, int) or interval <= 0:
                raise ValidationError("every_days 周期要求正整数 interval_days")
        offsets = value.get("remind_before_days", [7, 1])
        if not isinstance(offsets, list) or not all(isinstance(x, int) and x >= 0 for x in offsets):
            raise ValidationError("remind_before_days 必须是非负整数列表")
        grace = value.get("grace_days", 0)
        if not isinstance(grace, int) or grace < 0:
            raise ValidationError("grace_days 必须是非负整数")
        return cls(
            first_due=due,
            recurrence=recurrence,
            interval_days=interval,
            remind_before_days=tuple(offsets),
            grace_days=grace,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_due": self.first_due.isoformat(),
            "recurrence": self.recurrence,
            "interval_days": self.interval_days,
            "remind_before_days": list(self.remind_before_days),
            "grace_days": self.grace_days,
        }

    def due_of(self, ordinal: int) -> date:
        """第 ordinal 期（0 起）的截止日。"""
        if ordinal == 0:
            return self.first_due
        if self.recurrence == "every_days":
            from datetime import timedelta

            return self.first_due + timedelta(days=self.interval_days * ordinal)
        if self.recurrence in MONTH_STEP:
            return add_months(self.first_due, MONTH_STEP[self.recurrence] * ordinal)
        return self.first_due  # once：不存在后续期次


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    # 月末截断（1/31 + 1 月 -> 2/28）
    from calendar import monthrange

    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class ObligationRevision:
    """义务的一个不可变版本。"""

    version: int
    revision_type: RevisionType
    title: str
    description: str
    owner_party: str
    owner_contact: str
    channel: str
    time_window: TimeWindow
    evidence: EvidenceRequirement
    prerequisites: tuple[Prerequisite, ...]
    effective_date: date
    created_by: str
    reason: str = ""
    predecessor_version: int | None = None
    exempt: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "owner_party": self.owner_party,
            "owner_contact": self.owner_contact,
            "channel": self.channel,
            "time_window": self.time_window.to_dict(),
            "evidence": self.evidence.to_dict(),
            "prerequisites": [
                {"duty_code": p.duty_code, "same_window": p.same_window} for p in self.prerequisites
            ],
            "exempt": self.exempt,
        }

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        version: int,
        revision_type: RevisionType,
        effective_date: date,
        created_by: str,
        reason: str = "",
        predecessor_version: int | None = None,
    ) -> "ObligationRevision":
        for name in ("title", "owner_party", "owner_contact", "channel"):
            if not payload.get(name):
                raise ValidationError(f"义务缺少字段：{name}")
        prereqs = tuple(Prerequisite.from_dict(p) for p in payload.get("prerequisites", []))
        codes = [p.duty_code for p in prereqs]
        if len(codes) != len(set(codes)):
            raise ValidationError("前置义务编码不能重复")
        return cls(
            version=version,
            revision_type=revision_type,
            title=str(payload["title"]),
            description=str(payload.get("description", "")),
            owner_party=str(payload["owner_party"]),
            owner_contact=str(payload["owner_contact"]),
            channel=str(payload["channel"]),
            time_window=TimeWindow.from_dict(payload.get("time_window") or {}),
            evidence=EvidenceRequirement.from_dict(payload.get("evidence")),
            prerequisites=prereqs,
            effective_date=effective_date,
            created_by=created_by,
            reason=reason,
            predecessor_version=predecessor_version,
            exempt=bool(payload.get("exempt", False)),
        )


@dataclass
class DutyWindow:
    """义务在一个期次上的确定视图（调度器求值结果的载体）。"""

    duty_id: str
    code: str
    ordinal: int
    window_key: str
    due_date: date
    revision: ObligationRevision
    state: str  # pending | fulfilled | rejected
    blockers: list[str] = field(default_factory=list)

    @property
    def overdue_on(self) -> date:
        tw = self.revision.time_window
        from datetime import timedelta

        return tw.due_of(self.ordinal) + timedelta(days=tw.grace_days)
