"""证明提交与独立核验服务。

独立性规则（不可由责任方自我确认）：
- 核验人必须系统角色为 verifier；
- 必须持有该义务证明要求中登记的核验角色之一；
- 核验人不能隶属义务的责任方（party_id 不同，且非责任院校成员）；
- 提交证明的人不能核验自己的提交；
- 合格证明数达到 minimum_count 才结清；结清只追加事件，不改写历史。
"""
from __future__ import annotations

from typing import Any

from .clock import Clock
from .errors import AuthorizationError, ConflictError, NotFoundError
from .models import EventType, ObligationStatus, User, iso
from .repository import Repository


class VerificationService:
    def __init__(self, repo: Repository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock

    def submit_proof(
        self,
        actor: User,
        agreement_id: str,
        code: str,
        proof_type: str,
        file_ref: str | None,
        attributes: dict[str, Any] | None = None,
    ) -> int:
        """责任院校提交证明。提交本身不代表合格，须经独立核验。"""
        spec = self._require_active_spec(agreement_id, code)
        if actor.role not in ("party_member", "admin"):
            raise AuthorizationError("仅责任院校可提交证明")
        if actor.role == "party_member" and actor.party_id != spec.responsible.party_id:
            raise AuthorizationError("不能为非本方责任的义务提交证明")
        if proof_type != spec.proof.proof_type:
            raise ConflictError(
                f"证明类型不符：登记要求 {spec.proof.proof_type}，收到 {proof_type}"
            )
        missing = [a for a in spec.proof.required_attributes if a not in (attributes or {})]
        if missing:
            raise ConflictError("证明缺少必需属性：" + "、".join(missing))

        now = iso(self.clock.now())
        submission_id = self.repo.insert_submission(
            agreement_id, code, proof_type, file_ref, attributes or {}, actor.user_id, now
        )
        with self.repo.store.begin() as c:
            self.repo.insert_event(
                c, now, agreement_id, EventType.SUBMISSION_RECORDED.value, actor.user_id,
                {"submission_id": submission_id, "proof_type": proof_type,
                 "file_ref": file_ref, "attributes": attributes or {}},
                obligation_code=code,
            )
        return submission_id

    def verify(
        self, actor: User, agreement_id: str, code: str, submission_id: int,
        conforming: bool, reject_reason: str | None = None,
    ) -> None:
        """独立核验人员给出合格/不合格结论。"""
        spec = self._require_active_spec(agreement_id, code)
        self._assert_independent_verifier(actor, spec)

        row = self.repo.get_submission(submission_id)
        if row is None or row["agreement_id"] != agreement_id or row["code"] != code:
            raise NotFoundError(f"证明不存在：{submission_id}")
        if row["conforming"] is not None:
            raise ConflictError("该证明已经核验，不能重复核验（如需更正请提交新证明后重新核验）")
        if row["submitted_by"] == actor.user_id:
            raise AuthorizationError("不能核验自己提交的证明")
        if not conforming and not reject_reason:
            raise ConflictError("不合格结论必须填写驳回理由")

        now = iso(self.clock.now())
        with self.repo.store.begin() as c:
            c.execute(
                "UPDATE submissions SET verifier_user_id=?, verified_at=?, conforming=?, reject_reason=? WHERE id=?",
                (actor.user_id, now, 1 if conforming else 0, reject_reason, submission_id),
            )
            event_type = (
                EventType.VERIFICATION_APPROVED.value if conforming
                else EventType.VERIFICATION_REJECTED.value
            )
            self.repo.insert_event(
                c, now, agreement_id, event_type, actor.user_id,
                {"submission_id": submission_id, "verifier_roles": sorted(
                    self.repo.verifier_roles(actor.user_id))},
                obligation_code=code,
            )

        if conforming:
            self._maybe_complete(agreement_id, spec, actor)

    def _assert_independent_verifier(self, actor: User, spec) -> None:
        if actor.role != "verifier":
            raise AuthorizationError("仅核验人员可执行核验")
        roles = self.repo.verifier_roles(actor.user_id)
        allowed = set(spec.proof.verifier_roles)
        if not (roles & allowed):
            raise AuthorizationError(
                f"核验人缺少该义务登记的核验角色：需要 {sorted(allowed)}，实际 {sorted(roles)}"
            )
        if actor.party_id is not None and actor.party_id == spec.responsible.party_id:
            raise AuthorizationError("核验人隶属于责任方，不满足独立核验要求")

    def _maybe_complete(self, agreement_id: str, spec, actor: User) -> None:
        """合格证明达到登记数量后结清；结清不可逆。"""
        count = self.repo.conforming_proof_count(agreement_id, spec.code, spec.proof.proof_type)
        if count < spec.proof.minimum_count:
            return
        eff = self.repo.get_effective(agreement_id, spec.code)
        if eff["status"] != ObligationStatus.ACTIVE.value:
            return
        now = iso(self.clock.now())
        with self.repo.store.begin() as c:
            c.execute(
                "UPDATE effective_state SET status='completed', completed_at=? "
                "WHERE agreement_id=? AND code=?",
                (now, agreement_id, spec.code),
            )
            # 结清后未触发的提醒作废；已投递的历史保留
            c.execute(
                "UPDATE reminders SET superseded_at=? "
                "WHERE agreement_id=? AND code=? AND superseded_at IS NULL",
                (now, agreement_id, spec.code),
            )
            self.repo.insert_event(
                c, now, agreement_id, EventType.VERIFICATION_APPROVED.value, actor.user_id,
                {"conforming_proof_count": count, "minimum_count": spec.proof.minimum_count,
                 "result": "completed"},
                obligation_code=spec.code,
            )

    def _require_active_spec(self, agreement_id: str, code: str):
        self.repo.require_agreement(agreement_id)
        specs = self.repo.latest_specs(agreement_id)
        if code not in specs:
            raise NotFoundError(f"义务不存在：{code}")
        eff = self.repo.get_effective(agreement_id, code)
        if eff["status"] == ObligationStatus.WAIVED.value:
            raise ConflictError("义务已豁免，不再接收证明")
        if eff["status"] == ObligationStatus.COMPLETED.value:
            raise ConflictError("义务已结清")
        return specs[code]
