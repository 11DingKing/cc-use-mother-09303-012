"""调度引擎：用可注入时钟生成提醒与逾期案件，并给出"为何到期"的解释。

幂等保证：
- reminders 上有 (agreement, code, generation, type, seq) 的活跃部分唯一索引，
  以及含 due_date 的表级唯一约束；重复调度、服务恢复后重算都走
  INSERT ... ON CONFLICT DO NOTHING，不会产生第二条；
- 每条提醒至多一条有效 delivery（部分唯一索引），delivery 携带确定性
  idempotency key，日历网关侧同样去重，崩溃恢复重放安全。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .calendar import InMemoryCalendarGateway
from .clock import Clock
from .errors import AuthorizationError, DomainError
from .models import (
    EventType,
    ObligationSpec,
    ObligationStatus,
    ReminderType,
    User,
    iso,
)
from .repository import Repository

ROLE_ADMIN = "admin"


@dataclass
class PlannedReminder:
    reminder_type: ReminderType
    seq_no: int
    fire_date: date
    due_date: date
    reason: str

    def key(self) -> tuple[str, int, str, int, str]:
        return ("", 0, self.reminder_type.value, self.seq_no, self.due_date.isoformat())


@dataclass
class ObligationExplanation:
    """一项义务"为何（不）到期"的完整推导链。"""

    agreement_id: str
    code: str
    title: str
    kind: str
    status: str
    responsible_party: str
    responsible_owner: str
    recipient: str
    base_due_date: str
    extensions: list[dict[str, Any]] = field(default_factory=list)
    effective_due_date: str = ""
    prerequisite_state: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    days_to_due: int | None = None      # 负=已逾期天数
    proof_required: dict[str, Any] = field(default_factory=dict)
    conforming_proof_count: int = 0
    schedule: list[dict[str, Any]] = field(default_factory=list)
    overdue: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agreement_id": self.agreement_id,
            "code": self.code,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "responsible_party": self.responsible_party,
            "responsible_owner": self.responsible_owner,
            "recipient": self.recipient,
            "base_due_date": self.base_due_date,
            "extensions": self.extensions,
            "effective_due_date": self.effective_due_date,
            "prerequisite_state": self.prerequisite_state,
            "blocked": self.blocked,
            "days_to_due": self.days_to_due,
            "proof_required": self.proof_required,
            "conforming_proof_count": self.conforming_proof_count,
            "schedule": self.schedule,
            "overdue": self.overdue,
            "reasons": self.reasons,
        }


class SchedulerService:
    def __init__(self, repo: Repository, clock: Clock, calendar: "CalendarGateway | None" = None) -> None:
        self.repo = repo
        self.clock = clock
        self.calendar = calendar or InMemoryCalendarGateway()

    # ---- 解释 -----------------------------------------------------------

    def explain(self, agreement_id: str, code: str, today: date | None = None) -> ObligationExplanation:
        self.repo.require_agreement(agreement_id)
        specs = self.repo.latest_specs(agreement_id)
        if code not in specs:
            from .errors import NotFoundError

            raise NotFoundError(f"义务不存在：{code}")
        today = today or self.clock.now().date()
        return self._explain_one(agreement_id, specs[code], specs, today)

    def explain_all(self, agreement_id: str, today: date | None = None) -> list[ObligationExplanation]:
        self.repo.require_agreement(agreement_id)
        specs = self.repo.latest_specs(agreement_id)
        today = today or self.clock.now().date()
        return [self._explain_one(agreement_id, s, specs, today) for s in specs.values()]

    def _explain_one(
        self, agreement_id: str, spec: ObligationSpec, all_specs: dict[str, ObligationSpec], today: date
    ) -> ObligationExplanation:
        eff = self.repo.get_effective(agreement_id, spec.code)
        effective_due = date.fromisoformat(eff["effective_due"])
        generation = int(eff["generation"])
        status = eff["status"]

        extension_events = [
            self._event_payload(r)
            for r in self.repo.list_events(agreement_id, spec.code)
            if r["event_type"] == EventType.EXTENSION_GRANTED.value
        ]

        # 前置条件核验
        prereq_state: list[dict[str, Any]] = []
        blocked = False
        for pre in spec.prerequisites:
            pre_eff = self.repo.get_effective(agreement_id, pre.obligation_code)
            if pre_eff is None:
                pre_status = "missing"
            else:
                pre_status = pre_eff["status"]
            satisfied = pre_status in (ObligationStatus.COMPLETED.value, ObligationStatus.WAIVED.value)
            if not satisfied:
                blocked = True
            prereq_state.append({
                "obligation_code": pre.obligation_code,
                "description": pre.description,
                "status": pre_status,
                "satisfied": satisfied,
            })

        days_to_due = (effective_due - today).days
        overdue = (
            status == ObligationStatus.ACTIVE.value
            and not blocked
            and today > effective_due
        )

        reasons: list[str] = []
        reasons.append(f"当前计划代次 generation={generation}（每次延期/移交递增，旧提醒保留可查）")
        reasons.append(f"基础截止日 {spec.window.due_date.isoformat()}，叠加 {len(extension_events)} 次延期")
        reasons.append(f"当前生效截止日 {effective_due.isoformat()}，相对今天 {today.isoformat()} 为 {days_to_due:+d} 天")
        if status == ObligationStatus.COMPLETED.value:
            reasons.append("义务已经独立核验结清，不再产生提醒或逾期案件")
        elif status == ObligationStatus.WAIVED.value:
            reasons.append("义务已被豁免（历史与证明记录保留），不再产生提醒")
        elif blocked:
            unsat = [p["obligation_code"] for p in prereq_state if not p["satisfied"]]
            reasons.append(f"前置条件未满足：{', '.join(unsat)}，本义务暂不触发提醒/逾期")
        elif overdue:
            reasons.append(f"已逾期 {-days_to_due} 天，生成逾期案件并按 {spec.window.overdue_interval_days} 天间隔升级")
        else:
            reasons.append("义务生效且前置满足，按时间窗生成到期前/到期提醒")
        reasons.append(
            f"证明要求：{spec.proof.proof_type}（{spec.proof.description}），"
            f"需 {spec.proof.minimum_count} 份合格证明，核验角色：{', '.join(spec.proof.verifier_roles)}"
        )

        conforming = self.repo.conforming_proof_count(
            agreement_id, spec.code, spec.proof.proof_type
        )
        reasons.append(f"当前合格证明数：{conforming}/{spec.proof.minimum_count}（未经独立核验的提交不计入）")

        schedule: list[dict[str, Any]] = []
        if status == ObligationStatus.ACTIVE.value and not blocked:
            for planned in self._planned_reminders(spec, effective_due):
                schedule.append({
                    "type": planned.reminder_type.value,
                    "seq_no": planned.seq_no,
                    "fire_date": planned.fire_date.isoformat(),
                    "due_date": planned.due_date.isoformat(),
                    "reason": planned.reason,
                })

        return ObligationExplanation(
            agreement_id=agreement_id,
            code=spec.code,
            title=spec.title,
            kind=spec.kind.value,
            status=status,
            responsible_party=spec.responsible.party_name,
            responsible_owner=spec.responsible.owner_name,
            recipient=spec.responsible.contact or spec.responsible.owner_user_id,
            base_due_date=spec.window.due_date.isoformat(),
            extensions=extension_events,
            effective_due_date=effective_due.isoformat(),
            prerequisite_state=prereq_state,
            blocked=blocked,
            days_to_due=days_to_due,
            proof_required=spec.proof.to_dict(),
            conforming_proof_count=conforming,
            schedule=schedule,
            overdue=overdue,
            reasons=reasons,
        )

    # ---- 提醒规划（纯函数） ---------------------------------------------

    def _generation_valid_from(self, agreement_id: str, code: str, generation: int) -> date:
        """代次生效日：gen1 取首版计划登记日；其后取第 (gen-1) 次移交/延期事件日。

        代次生效日之前的提醒档位不应被补发（例如移交前的到期前提醒、
        逾期后才批准的延期所对应的旧档位）。
        """
        if generation <= 1:
            row = self.repo.store.query_one(
                "SELECT MIN(pv.created_at) AS t FROM plan_versions pv "
                "JOIN obligation_versions ov ON ov.plan_version_id=pv.id "
                "WHERE ov.agreement_id=? AND ov.code=?",
                (agreement_id, code),
            )
            text = row["t"] if row and row["t"] else self.clock.now().isoformat()
            return date.fromisoformat(text[:10])
        bumps = self.repo.store.query_all(
            "SELECT occurred_at FROM events WHERE agreement_id=? AND obligation_code=? "
            "AND event_type IN (?, ?) ORDER BY id",
            (agreement_id, code,
             EventType.RESPONSIBILITY_TRANSFERRED.value, EventType.EXTENSION_GRANTED.value),
        )
        return date.fromisoformat(bumps[generation - 2]["occurred_at"][:10])

    def _planned_reminders(self, spec: ObligationSpec, effective_due: date) -> list[PlannedReminder]:
        out: list[PlannedReminder] = []
        for idx, days in enumerate(sorted(spec.window.advance_days, reverse=True), start=1):
            out.append(PlannedReminder(
                ReminderType.ADVANCE, idx, effective_due - timedelta(days=days), effective_due,
                f"到期前 {days} 天提醒（时间窗 advance_days 档位 {idx}）",
            ))
        out.append(PlannedReminder(
            ReminderType.DUE, 1, effective_due, effective_due,
            "到期当日提醒（时间窗 due_date）",
        ))
        for seq in range(1, spec.window.overdue_max_reminders + 1):
            fire = effective_due + timedelta(days=spec.window.overdue_interval_days * seq)
            out.append(PlannedReminder(
                ReminderType.OVERDUE, seq, fire, effective_due,
                f"逾期第 {seq} 轮升级（截止日 {effective_due.isoformat()} 后 "
                f"{spec.window.overdue_interval_days * seq} 天）",
            ))
        return out

    # ---- 区间重算（管理命令核心） ---------------------------------------

    def recalculate(
        self,
        actor: User,
        agreement_id: str,
        range_start: date | str,
        range_end: date | str,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """重算 [start, end] 内各义务应触发的提醒，幂等落库并可选立即投递。

        重复执行同一区间：已存在的档位命中唯一约束被跳过，不会产生多条。
        """
        if actor.role != ROLE_ADMIN:
            raise AuthorizationError("仅协议管理员可执行区间重算")
        if isinstance(range_start, str):
            range_start = date.fromisoformat(range_start)
        if isinstance(range_end, str):
            range_end = date.fromisoformat(range_end)
        if range_end < range_start:
            raise DomainError("区间结束日不能早于开始日")

        self.repo.require_agreement(agreement_id)
        specs = self.repo.latest_specs(agreement_id)
        today = self.clock.now().date()
        now = iso(self.clock.now())

        per_obligation: list[dict[str, Any]] = []
        created_total = 0
        existed_total = 0

        for code in sorted(specs):
            spec = specs[code]
            explanation = self._explain_one(agreement_id, spec, specs, today)
            actions: list[dict[str, Any]] = []
            created = existed = 0
            created_ids: list[int] = []

            if explanation.status != ObligationStatus.ACTIVE.value or explanation.blocked:
                per_obligation.append({
                    "code": code,
                    "created": 0, "existed": 0, "dispatched": 0,
                    "actions": actions,
                    "explanation": explanation.to_dict(),
                })
                continue

            eff = self.repo.get_effective(agreement_id, code)
            generation = int(eff["generation"])
            effective_due = date.fromisoformat(eff["effective_due"])
            valid_from = self._generation_valid_from(agreement_id, code, generation)

            for planned in self._planned_reminders(spec, effective_due):
                if not (range_start <= planned.fire_date <= range_end):
                    continue
                reminder_id, was_created = self._insert_reminder_idempotent(
                    agreement_id, spec, generation, planned, now
                )
                if was_created:
                    created += 1
                    created_ids.append(reminder_id)
                    actions.append({"action": "created", **self._action_fields(generation, planned)})
                else:
                    existed += 1
                    actions.append({"action": "already_exists", **self._action_fields(generation, planned)})

                # 投递（outbox）只在以下条件同时满足时建单：
                # 1) fire_date <= 今天（不提前数周发出）；
                # 2) fire_date >= 当代次生效日（不向新负责人补发移交/延期前的档位）。
                # 计划行始终保留，未投递档位在解释中可区分。
                if valid_from <= planned.fire_date <= today:
                    delivery = self.repo.get_delivery(reminder_id)
                    if delivery is None:
                        self._insert_delivery(agreement_id, code, generation, spec, reminder_id, planned, now)
                elif planned.fire_date < valid_from:
                    actions.append({
                        "action": "delivery_suppressed_prior_to_generation",
                        "generation_valid_from": valid_from.isoformat(),
                        **self._action_fields(generation, planned),
                    })

            created_total += created
            existed_total += existed
            per_obligation.append({
                "code": code,
                "created": created,
                "existed": existed,
                "dispatched": 0,  # 统一投递后回填
                "_created_reminder_ids": created_ids,
                "actions": actions,
                "explanation": explanation.to_dict(),
            })

        dispatched_total = 0
        if dispatch:
            dispatched_total = self.dispatch_pending()["sent"]
            for item in per_obligation:
                ids = item.pop("_created_reminder_ids", [])
                item["dispatched"] = sum(
                    1 for rid in ids
                    if (d := self.repo.get_delivery(rid)) is not None and d["status"] == "sent"
                )
        else:
            for item in per_obligation:
                item.pop("_created_reminder_ids", None)

        result = {
            "agreement_id": agreement_id,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "evaluated_at": now,
            "created": created_total,
            "already_existed": existed_total,
            "dispatched": dispatched_total,
            "obligations": per_obligation,
        }
        self.repo.record_recalc(
            agreement_id, range_start.isoformat(), range_end.isoformat(),
            actor.user_id, now, result,
        )
        return result

    @staticmethod
    def _action_fields(generation: int, planned: PlannedReminder) -> dict[str, Any]:
        return {
            "generation": generation,
            "type": planned.reminder_type.value,
            "seq_no": planned.seq_no,
            "fire_date": planned.fire_date.isoformat(),
            "due_date": planned.due_date.isoformat(),
            "reason": planned.reason,
        }

    def _insert_reminder_idempotent(
        self, agreement_id: str, spec: ObligationSpec, generation: int,
        planned: PlannedReminder, now: str,
    ) -> tuple[int, bool]:
        """返回 (reminder_id, 是否新建)。已存在（含被取代的同键历史）时取旧行。"""
        with self.repo.store.begin() as c:
            cur = c.execute(
                "INSERT INTO reminders(agreement_id,code,generation,reminder_type,seq_no,due_date,"
                "fire_date,owner_user_id,recipient,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (agreement_id, spec.code, generation, planned.reminder_type.value, planned.seq_no,
                 planned.due_date.isoformat(), planned.fire_date.isoformat(),
                 spec.responsible.owner_user_id, spec.responsible.contact or spec.responsible.owner_user_id, now),
            )
            if cur.rowcount == 1:
                return int(cur.lastrowid), True
        row = self.repo.store.query_one(
            "SELECT id FROM reminders WHERE agreement_id=? AND code=? AND generation=? "
            "AND reminder_type=? AND seq_no=? AND due_date=?",
            (agreement_id, spec.code, generation, planned.reminder_type.value,
             planned.seq_no, planned.due_date.isoformat()),
        )
        return int(row["id"]), False

    def _insert_delivery(
        self, agreement_id: str, code: str, generation: int, spec: ObligationSpec,
        reminder_id: int, planned: PlannedReminder, now: str,
    ) -> int:
        idem = self.idempotency_key(
            agreement_id, code, generation, planned.reminder_type.value,
            planned.seq_no, planned.due_date, spec.responsible.owner_user_id,
        )
        with self.repo.store.begin() as c:
            cur = c.execute(
                "INSERT INTO deliveries(reminder_id,channel,recipient,status,idempotency_key,created_at)"
                " VALUES(?,'calendar',?,'pending',?,?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "status='pending', last_error=NULL",
                (reminder_id, spec.responsible.contact or spec.responsible.owner_user_id, idem, now),
            )
            row = c.execute("SELECT id FROM deliveries WHERE idempotency_key=?", (idem,)).fetchone()
            return int(row["id"])

    @staticmethod
    def idempotency_key(
        agreement_id: str, code: str, generation: int, rtype: str, seq: int,
        due_date: date | str, owner_user_id: str,
    ) -> str:
        raw = "|".join(
            str(v) for v in (agreement_id, code, generation, rtype, seq, due_date, owner_user_id)
        )
        return "rem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    # ---- 投递（outbox 模式） --------------------------------------------

    def dispatch_pending(self, actor: User | None = None) -> dict[str, int]:
        """把所有 pending 投递送往日历网关。

        可在恢复后任意重复调用：网关按 idempotency key 去重，
        成功置 sent、失败置 failed 并保留错误，不产生第二条有效单。
        """
        sent = failed = 0
        for row in self.repo.pending_deliveries(include_failed=True):
            try:
                self.calendar.send(
                    idempotency_key=row["idempotency_key"],
                    recipient=row["recipient"],
                    payload={"reminder_id": row["reminder_id"], "channel": row["channel"]},
                )
            except Exception as exc:  # 网关失败不致命，等待下次重放
                with self.repo.store.begin() as c:
                    c.execute(
                        "UPDATE deliveries SET status='failed', attempts=attempts+1, last_error=? WHERE id=?",
                        (str(exc), row["id"]),
                    )
                failed += 1
                continue
            with self.repo.store.begin() as c:
                c.execute(
                    "UPDATE deliveries SET status='sent', attempts=attempts+1, sent_at=? WHERE id=?",
                    (iso(self.clock.now()), row["id"]),
                )
                reminder = c.execute(
                    "SELECT * FROM reminders WHERE id=?", (row["reminder_id"],)
                ).fetchone()
                if reminder is not None:
                    self.repo.insert_event(
                        c, iso(self.clock.now()), reminder["agreement_id"],
                        EventType.REMINDER_DISPATCHED.value, "system",
                        {"reminder_id": row["reminder_id"], "recipient": row["recipient"],
                         "idempotency_key": row["idempotency_key"],
                         "generation": reminder["generation"], "type": reminder["reminder_type"],
                         "seq_no": reminder["seq_no"]},
                        obligation_code=reminder["code"],
                    )
            sent += 1
        return {"sent": sent, "failed": failed}

    # ---- 逾期案件查询 ---------------------------------------------------

    def overdue_cases(self, agreement_id: str, today: date | None = None) -> list[dict[str, Any]]:
        explanations = self.explain_all(agreement_id, today)
        today = today or self.clock.now().date()
        cases: list[dict[str, Any]] = []
        for ex in explanations:
            if not ex.overdue:
                continue
            active_reminders = self.repo.active_reminders(agreement_id, ex.code)
            valid_from = self._generation_valid_from(
                agreement_id, ex.code,
                int(self.repo.get_effective(agreement_id, ex.code)["generation"]),
            )
            overdue_reminders = [
                {
                    "seq_no": r["seq_no"], "fire_date": r["fire_date"],
                    "owner_user_id": r["owner_user_id"], "recipient": r["recipient"],
                    "generation": r["generation"],
                    "delivery_status": (
                        self._delivery_status(r["id"])
                        if date.fromisoformat(r["fire_date"]) >= valid_from
                        else "suppressed_before_generation"
                    ),
                }
                for r in active_reminders
                if r["reminder_type"] == ReminderType.OVERDUE.value
                and date.fromisoformat(r["fire_date"]) <= today
            ]
            cases.append({
                "agreement_id": agreement_id,
                "code": ex.code,
                "title": ex.title,
                "kind": ex.kind,
                "effective_due_date": ex.effective_due_date,
                "days_overdue": -ex.days_to_due,
                "responsible_owner": ex.responsible_owner,
                "recipient": ex.recipient,
                "overdue_reminders": overdue_reminders,
                "reasons": ex.reasons,
            })
        cases.sort(key=lambda c: (-c["days_overdue"], c["code"]))
        return cases

    def _delivery_status(self, reminder_id: int) -> str:
        row = self.repo.get_delivery(reminder_id)
        return row["status"] if row else "not_created"

    @staticmethod
    def _event_payload(row) -> dict[str, Any]:
        import json

        return {"occurred_at": row["occurred_at"], "actor": row["actor_user_id"],
                **json.loads(row["payload_json"])}
