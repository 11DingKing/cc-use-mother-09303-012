"""计划登记与义务生命周期服务。

不变量：
- 每次登记产生新的 plan_version，旧快照永不修改；
- 延期 / 豁免 / 责任移交只追加事件，并使调度代次 generation +1，
  旧提醒标记 superseded 但不删除，旧投递记录完整保留；
- 所有写操作要求相应角色（登记类仅协议管理员）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .clock import Clock
from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .models import (
    EventType,
    ObligationSpec,
    ObligationStatus,
    ResponsibleParty,
    User,
    iso,
    parse_date,
)
from .repository import Repository

ROLE_ADMIN = "admin"


class PlannerService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    # ---- 协议与用户 -----------------------------------------------------

    def create_agreement(self, actor: User, agreement_id: str, code: str, title: str) -> None:
        self._require_admin(actor)
        if self.repo.get_agreement(agreement_id) is not None:
            raise ConflictError(f"协议已存在：{agreement_id}")
        self.repo.create_agreement(agreement_id, code, title, iso(self.clock.now()))

    def grant_verifier_role(self, actor: User, user_id: str, role_code: str) -> None:
        self._require_admin(actor)
        if self.repo.get_user(user_id) is None:
            raise NotFoundError(f"用户不存在：{user_id}")
        with self.repo.store.begin() as c:
            c.execute(
                "INSERT INTO user_verifier_roles(user_id,role_code,granted_at,granted_by)"
                " VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,role_code) DO UPDATE SET revoked_at=NULL",
                (user_id, role_code, iso(self.clock.now()), actor.user_id),
            )

    # ---- 版本化计划登记 -------------------------------------------------

    def register_plan(
        self,
        actor: User,
        agreement_id: str,
        obligations: list[ObligationSpec],
        change_summary: str,
    ) -> int:
        """登记一个计划版本。

        可全量登记（首版），也可只登记变更义务；未出现在本版中的义务沿用其最新快照。
        新版本号递增，全部快照不可变。
        """
        self._require_admin(actor)
        self.repo.require_agreement(agreement_id)
        if not obligations:
            raise ValidationError("至少登记一项义务")
        codes = [o.code for o in obligations]
        if len(codes) != len(set(codes)):
            raise ValidationError("同一计划版本中义务代码不能重复")

        existing = self.repo.latest_specs(agreement_id)
        merged: dict[str, ObligationSpec] = dict(existing)
        for spec in obligations:
            self._validate_spec(spec, merged)
            # 已登记义务的时间窗不可通过重新登记被静默改写：
            # 截止日变更必须走 grant_extension 并留下旧→新截止日事件。
            if spec.code in existing:
                old_window = existing[spec.code].window
                if spec.window != old_window:
                    raise ValidationError(
                        f"义务 {spec.code} 的时间窗与已登记版本不一致；"
                        "调整截止日请使用延期操作以保留历史"
                    )
            merged[spec.code] = spec

        # 前置条件引用必须最终可解析
        for spec in merged.values():
            for pre in spec.prerequisites:
                if pre.obligation_code not in merged:
                    raise ValidationError(
                        f"义务 {spec.code} 的前置条件 {pre.obligation_code} 未登记"
                    )

        now = iso(self.clock.now())
        version_no = self.repo.latest_plan_version_no(agreement_id) + 1
        with self.repo.store.begin() as c:
            cur = c.execute(
                "INSERT INTO plan_versions(agreement_id,version_no,created_at,created_by,change_summary)"
                " VALUES(?,?,?,?,?)",
                (agreement_id, version_no, now, actor.user_id, change_summary),
            )
            plan_version_id = int(cur.lastrowid)
            for spec in merged.values():
                import json

                c.execute(
                    "INSERT INTO obligation_versions(plan_version_id,agreement_id,code,spec_json)"
                    " VALUES(?,?,?,?)",
                    (plan_version_id, agreement_id, spec.code,
                     json.dumps(spec.to_dict(), ensure_ascii=False)),
                )
            self.repo.insert_event(
                c, now, agreement_id, EventType.PLAN_VERSIONED.value, actor.user_id,
                {"version_no": version_no, "codes": sorted(merged), "summary": change_summary},
            )
            # 建立/更新生效状态：新义务代次为 1，已有义务保留各自状态与代次。
            for code, spec in merged.items():
                row = self.repo.get_effective(agreement_id, code)
                if row is None:
                    c.execute(
                        "INSERT INTO effective_state(agreement_id,code,latest_version_no,generation,"
                        "status,effective_due) VALUES(?,?,?,1,'active',?)",
                        (agreement_id, code, version_no, spec.window.due_date.isoformat()),
                    )
                else:
                    c.execute(
                        "UPDATE effective_state SET latest_version_no=? WHERE agreement_id=? AND code=?",
                        (version_no, agreement_id, code),
                    )
        return version_no

    # ---- 责任移交 -------------------------------------------------------

    def transfer_responsibility(
        self,
        actor: User,
        agreement_id: str,
        code: str,
        new_responsible: ResponsibleParty,
        reason: str,
    ) -> int:
        """责任移交：新版本快照 + 代次递增；旧负责人与旧提醒保留。"""
        self._require_admin(actor)
        spec = self._require_spec(agreement_id, code)
        self._require_active(agreement_id, code)
        old = spec.responsible
        if old.party_id == new_responsible.party_id and old.owner_user_id == new_responsible.owner_user_id:
            raise ValidationError("新责任方与当前责任方相同，无需移交")

        new_spec = ObligationSpec(
            code=spec.code, kind=spec.kind, title=spec.title, responsible=new_responsible,
            window=spec.window, proof=spec.proof, prerequisites=spec.prerequisites,
        )
        version_no = self.register_plan(
            actor, agreement_id, [new_spec],
            change_summary=f"义务 {code} 责任移交：{old.owner_name} → {new_responsible.owner_name}（{reason}）",
        )
        self._bump_generation(
            agreement_id, code, EventType.RESPONSIBILITY_TRANSFERRED, actor,
            {
                "old_party_id": old.party_id, "old_owner_user_id": old.owner_user_id,
                "old_owner_name": old.owner_name, "old_contact": old.contact,
                "new_party_id": new_responsible.party_id,
                "new_owner_user_id": new_responsible.owner_user_id,
                "new_owner_name": new_responsible.owner_name, "new_contact": new_responsible.contact,
                "reason": reason,
            },
        )
        return version_no

    # ---- 延期 -----------------------------------------------------------

    def grant_extension(
        self, actor: User, agreement_id: str, code: str, days: int, reason: str
    ) -> str:
        self._require_admin(actor)
        if days <= 0:
            raise ValidationError("延期天数必须为正整数")
        spec = self._require_spec(agreement_id, code)
        self._require_active(agreement_id, code)
        eff = self.repo.get_effective(agreement_id, code)
        old_due = parse_date(eff["effective_due"])
        new_due = old_due + timedelta(days=days)
        now = iso(self.clock.now())
        payload = {"days": days, "old_due_date": old_due.isoformat(),
                   "new_due_date": new_due.isoformat(), "reason": reason}
        with self.repo.store.begin() as c:
            c.execute(
                "UPDATE effective_state SET effective_due=?, generation=generation+1 "
                "WHERE agreement_id=? AND code=?",
                (new_due.isoformat(), agreement_id, code),
            )
            self._supersede_active(c, agreement_id, code, now)
            self.repo.insert_event(
                c, now, agreement_id, EventType.EXTENSION_GRANTED.value, actor.user_id,
                payload, obligation_code=code,
            )
        return new_due.isoformat()

    # ---- 豁免 -----------------------------------------------------------

    def grant_waiver(self, actor: User, agreement_id: str, code: str, reason: str) -> None:
        self._require_admin(actor)
        self._require_spec(agreement_id, code)
        eff = self.repo.get_effective(agreement_id, code)
        if eff["status"] == ObligationStatus.COMPLETED.value:
            raise ConflictError("义务已结清，不能豁免")
        if eff["status"] == ObligationStatus.WAIVED.value:
            raise ConflictError("义务已处于豁免状态")
        now = iso(self.clock.now())
        with self.repo.store.begin() as c:
            c.execute(
                "UPDATE effective_state SET status='waived', waived_at=? "
                "WHERE agreement_id=? AND code=?",
                (now, agreement_id, code),
            )
            self.repo.insert_event(
                c, now, agreement_id, EventType.WAIVER_GRANTED.value, actor.user_id,
                {"reason": reason}, obligation_code=code,
            )
            self._supersede_active(c, agreement_id, code, now)

    # ---- 内部 -----------------------------------------------------------

    def _bump_generation(
        self, agreement_id: str, code: str, event_type: EventType, actor: User, payload: dict[str, Any]
    ) -> None:
        """代次 +1 并把当代次未取代的提醒标记为 superseded；历史保留。"""
        now = iso(self.clock.now())
        with self.repo.store.begin() as c:
            c.execute(
                "UPDATE effective_state SET generation=generation+1 "
                "WHERE agreement_id=? AND code=?",
                (agreement_id, code),
            )
            self._supersede_active(c, agreement_id, code, now)
            self.repo.insert_event(
                c, now, agreement_id, event_type.value, actor.user_id, payload,
                obligation_code=code,
            )

    @staticmethod
    def _supersede_active(conn, agreement_id: str, code: str, at: str) -> None:
        conn.execute(
            "UPDATE reminders SET superseded_at=? "
            "WHERE agreement_id=? AND code=? AND superseded_at IS NULL",
            (at, agreement_id, code),
        )

    def _require_spec(self, agreement_id: str, code: str) -> ObligationSpec:
        self.repo.require_agreement(agreement_id)
        specs = self.repo.latest_specs(agreement_id)
        if code not in specs:
            raise NotFoundError(f"义务不存在：{code}")
        return specs[code]

    def _require_active(self, agreement_id: str, code: str) -> None:
        eff = self.repo.get_effective(agreement_id, code)
        if eff is None:
            raise NotFoundError(f"义务不存在：{code}")
        status = eff["status"]
        if status == ObligationStatus.COMPLETED.value:
            raise ConflictError("义务已结清，不能变更责任方或时间窗")
        if status == ObligationStatus.WAIVED.value:
            raise ConflictError("义务已豁免，不能变更责任方或时间窗")

    @staticmethod
    def _validate_spec(spec: ObligationSpec, others: dict[str, ObligationSpec]) -> None:
        if not spec.code.strip():
            raise ValidationError("义务代码不能为空")
        if spec.window.due_date < spec.window.start_date:
            raise ValidationError(f"义务 {spec.code} 的截止日早于起始日")
        if not spec.proof.verifier_roles:
            raise ValidationError(f"义务 {spec.code} 必须至少登记一个核验角色")
        if spec.proof.minimum_count < 1:
            raise ValidationError(f"义务 {spec.code} 的合格证明数量至少为 1")
        for pre in spec.prerequisites:
            if pre.obligation_code == spec.code:
                raise ValidationError(f"义务 {spec.code} 不能以自身为前置条件")
        if spec.code in others:
            # 允许新版本覆盖登记；窗口/责任方变更通过显式操作留痕，这里只校验形状
            pass

    @staticmethod
    def _require_admin(actor: User) -> None:
        if actor.role != ROLE_ADMIN:
            raise AuthorizationError("仅协议管理员可执行该登记操作")
