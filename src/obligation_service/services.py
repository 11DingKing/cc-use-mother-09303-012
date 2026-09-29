"""登记与修订服务：版本化计划、追加式延期/豁免/移交。"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .clock import Clock
from .errors import AuthorizationError, ConflictError, ValidationError
from .models import (
    ROLE_ADMIN,
    Actor,
    ObligationRevision,
)
from .repository import Repository
from .store import Store
from .util import iso_date, parse_date, uid


class RegistrationService:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    # ---- 协议 ----

    def create_agreement(self, actor: Actor, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(actor)
        for name in ("code", "title"):
            if not payload.get(name):
                raise ValidationError(f"协议缺少字段：{name}")
        agreement_id = uid("agr")
        now = self.clock.now().isoformat()
        with self.store.tx() as conn:
            exists = conn.execute("SELECT 1 FROM agreements WHERE code=?", (payload["code"],)).fetchone()
            if exists:
                raise ConflictError(f"协议编号已存在：{payload['code']}")
            conn.execute(
                "INSERT INTO agreements(agreement_id, code, title, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (agreement_id, payload["code"], payload["title"], json.dumps(payload, ensure_ascii=False), now),
            )
            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action="agreement_created",
                entity_type="agreement", entity_id=agreement_id, payload={"code": payload["code"]},
            )
        return {"agreement_id": agreement_id, "code": payload["code"]}

    # ---- 计划（版本化）----

    def register_plan(self, actor: Actor, agreement_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """登记计划 v1：义务、责任方、前置条件、时间窗、证明要求一次成形。"""
        self._require_admin(actor)
        duties = payload.get("duties")
        if not isinstance(duties, list) or not duties:
            raise ValidationError("计划必须包含非空 duties 列表")

        verification_policy = self._parse_verification_policy(payload.get("verification"))

        # 先全部解析，提前暴露字段错误
        parsed: list[dict[str, Any]] = []
        codes: set[str] = set()
        ordered_codes: list[str] = []
        for item in duties:
            code = item.get("code")
            if not code:
                raise ValidationError("义务缺少 code")
            if code in codes:
                raise ValidationError(f"义务编码重复：{code}")
            codes.add(code)
            ordered_codes.append(code)
            revision = ObligationRevision.from_payload(
                item,
                version=1,
                revision_type="register",
                effective_date=self.clock.today(),
                created_by=actor.actor_id,
            )
            parsed.append({"code": code, "revision": revision, "payload": item})

        self._check_prerequisites(parsed)

        plan_id = uid("plan")
        now = self.clock.now().isoformat()
        today = iso_date(self.clock.today())
        duty_ids: list[str] = []
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO plans(plan_id, agreement_id, current_version, created_at) VALUES (?,?,1,?)",
                (plan_id, agreement_id, now),
            )
            conn.execute(
                "INSERT INTO plan_versions(plan_id, version, status, created_by, created_at,"
                " activated_at, note, payload) VALUES (?,1,'active',?,?,?,'初始登记',?)",
                (plan_id, actor.actor_id, now, now, json.dumps(
                    {"verification": verification_policy,
                     "duties": [d["payload"] for d in parsed]},
                    ensure_ascii=False, sort_keys=True)),
            )
            for entry in parsed:
                duty_id = uid("duty")
                duty_ids.append(duty_id)
                rev = entry["revision"]
                conn.execute(
                    "INSERT INTO obligations(duty_id, plan_id, code, current_version, created_at)"
                    " VALUES (?,?,?,1,?)",
                    (duty_id, plan_id, entry["code"], now),
                )
                conn.execute(
                    "INSERT INTO obligation_revisions(duty_id, version, revision_type, effective_date,"
                    " created_by, created_at, reason, predecessor_version, payload)"
                    " VALUES (?,1,'register',?,?,?,NULL,NULL,?)",
                    (duty_id, iso_date(rev.effective_date), actor.actor_id, now,
                     json.dumps(rev.to_payload(), ensure_ascii=False, sort_keys=True)),
                )
            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action="plan_registered",
                entity_type="plan", entity_id=plan_id,
                payload={"agreement_id": agreement_id, "duty_codes": sorted(codes), "version": 1},
            )
        return {
            "plan_id": plan_id,
            "agreement_id": agreement_id,
            "version": 1,
            "duties": [{"duty_id": did, "code": c} for did, c in zip(duty_ids, ordered_codes)],
            "registered_on": today,
        }

    def _check_prerequisites(self, parsed: list[dict[str, Any]]) -> None:
        codes = {d["code"] for d in parsed}
        graph: dict[str, list[str]] = {}
        for entry in parsed:
            deps = [p.duty_code for p in entry["revision"].prerequisites]
            unknown = [d for d in deps if d not in codes]
            if unknown:
                raise ValidationError(f"义务 {entry['code']} 的前置义务不存在：{'、'.join(unknown)}")
            graph[entry["code"]] = deps
        # 环检测
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValidationError(f"前置义务存在环：{node}")
            visiting.add(node)
            for dep in graph.get(node, []):
                visit(dep)
            visiting.discard(node)
            done.add(node)

        for code in graph:
            visit(code)

    # ---- 修订：延期 / 豁免 / 移交 / 一般修订 ----

    def _add_revision(
        self,
        actor: Actor,
        duty_id: str,
        revision_type: str,
        mutate,
        reason: str,
        effective_date: date | None = None,
    ) -> dict[str, Any]:
        if not reason:
            raise ValidationError("修订必须提供 reason（变更依据）")
        eff = effective_date or self.clock.today()
        with self.store.tx() as conn:
            repo = Repository(conn)
            duty = repo.get_duty(duty_id)
            current = repo.latest_revision(duty_id)
            new_version = int(duty["current_version"]) + 1
            payload = mutate(current.to_payload())
            revision = ObligationRevision.from_payload(
                payload,
                version=new_version,
                revision_type=revision_type,  # type: ignore[arg-type]
                effective_date=eff,
                created_by=actor.actor_id,
                reason=reason,
                predecessor_version=current.version,
            )
            now = self.clock.now().isoformat()
            conn.execute(
                "INSERT INTO obligation_revisions(duty_id, version, revision_type, effective_date,"
                " created_by, created_at, reason, predecessor_version, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (duty_id, new_version, revision_type, iso_date(eff), actor.actor_id, now, reason,
                 current.version, json.dumps(revision.to_payload(), ensure_ascii=False, sort_keys=True)),
            )
            conn.execute(
                "UPDATE obligations SET current_version=? WHERE duty_id=?", (new_version, duty_id)
            )
            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action=f"duty_{revision_type}",
                entity_type="duty", entity_id=duty_id,
                payload={
                    "version": new_version,
                    "predecessor_version": current.version,
                    "reason": reason,
                    "effective_date": iso_date(eff),
                    "snapshot": revision.to_payload(),
                },
            )
            return self._revision_summary(duty_id, new_version, revision_type, reason, current.version)

    def extend_deadline(
        self, actor: Actor, duty_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """延期：产生新版本，旧截止日与旧提醒保留可查。"""
        self._require_admin(actor)
        reason = str(payload.get("reason", ""))
        eff = parse_date(payload["effective_date"], "effective_date") if payload.get("effective_date") else None

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            tw = dict(snapshot["time_window"])
            if payload.get("new_first_due"):
                tw["first_due"] = iso_date(parse_date(payload["new_first_due"], "new_first_due"))
            if payload.get("add_days") is not None:
                add = payload["add_days"]
                if not isinstance(add, int) or add <= 0:
                    raise ValidationError("add_days 必须是正整数")
                from datetime import timedelta

                base = parse_date(tw["first_due"])
                tw["first_due"] = iso_date(base + timedelta(days=add))
            if payload.get("grace_days") is not None:
                tw["grace_days"] = payload["grace_days"]
            snapshot["time_window"] = tw
            return snapshot

        return self._add_revision(actor, duty_id, "extend", mutate, reason, eff)

    def grant_exemption(self, actor: Actor, duty_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """豁免：产生新版本；义务被标记豁免，旧记录完整保留。"""
        self._require_admin(actor)
        reason = str(payload.get("reason", ""))
        eff = parse_date(payload["effective_date"], "effective_date") if payload.get("effective_date") else None

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            snapshot["exempt"] = True
            return snapshot

        return self._add_revision(actor, duty_id, "exempt", mutate, reason, eff)

    def transfer_ownership(self, actor: Actor, duty_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """责任移交：新责任方写入新版本；旧版本、旧接收人留痕。"""
        self._require_admin(actor)
        reason = str(payload.get("reason", ""))
        new_owner = payload.get("new_owner_party")
        new_contact = payload.get("new_owner_contact")
        if not new_owner or not new_contact:
            raise ValidationError("移交需要 new_owner_party 与 new_owner_contact")
        eff = parse_date(payload["effective_date"], "effective_date") if payload.get("effective_date") else None

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            snapshot["owner_party"] = str(new_owner)
            snapshot["owner_contact"] = str(new_contact)
            if payload.get("channel"):
                snapshot["channel"] = str(payload["channel"])
            return snapshot

        return self._add_revision(actor, duty_id, "transfer", mutate, reason, eff)

    def amend(self, actor: Actor, duty_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """一般修订（证明要求、前置条件、描述等）。"""
        self._require_admin(actor)
        reason = str(payload.get("reason", ""))
        changes = payload.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("amend 需要非空 changes")
        eff = parse_date(payload["effective_date"], "effective_date") if payload.get("effective_date") else None

        def mutate(snapshot: dict[str, Any]) -> dict[str, Any]:
            for key in ("title", "description", "channel", "evidence", "prerequisites", "time_window"):
                if key in changes:
                    snapshot[key] = changes[key]
            return snapshot

        return self._add_revision(actor, duty_id, "amend", mutate, reason, eff)

    # ---- 读取 ----

    def get_duty_detail(self, duty_id: str) -> dict[str, Any]:
        repo = Repository(self.store.conn)
        duty = repo.get_duty(duty_id)
        rows = repo.list_revisions(duty_id)
        from .repository import row_to_revision

        rev_dicts = []
        for row in rows:
            rev = row_to_revision(row)
            rev_dicts.append(
                {
                    "version": rev.version,
                    "revision_type": rev.revision_type,
                    "effective_date": iso_date(rev.effective_date),
                    "created_by": rev.created_by,
                    "reason": rev.reason,
                    "predecessor_version": rev.predecessor_version,
                    "owner_party": rev.owner_party,
                    "owner_contact": rev.owner_contact,
                    "exempt": rev.exempt,
                    "time_window": rev.time_window.to_dict(),
                    "evidence": rev.evidence.to_dict(),
                    "prerequisites": [p.duty_code for p in rev.prerequisites],
                    "title": rev.title,
                }
            )
        return {
            "duty_id": duty_id,
            "plan_id": duty["plan_id"],
            "code": duty["code"],
            "current_version": duty["current_version"],
            "revisions": rev_dicts,
        }

    def list_plan_duties(self, plan_id: str) -> list[dict[str, Any]]:
        repo = Repository(self.store.conn)
        repo.get_plan(plan_id)
        result = []
        for duty in repo.list_duties(plan_id):
            rev = repo.latest_revision(duty["duty_id"])
            result.append(
                {
                    "duty_id": duty["duty_id"],
                    "code": duty["code"],
                    "current_version": duty["current_version"],
                    "title": rev.title,
                    "owner_party": rev.owner_party,
                    "owner_contact": rev.owner_contact,
                    "exempt": rev.exempt,
                    "first_due": rev.time_window.to_dict()["first_due"],
                }
            )
        return result

    def _revision_summary(
        self, duty_id: str, version: int, revision_type: str, reason: str, predecessor: int
    ) -> dict[str, Any]:
        return {
            "duty_id": duty_id,
            "version": version,
            "revision_type": revision_type,
            "predecessor_version": predecessor,
            "reason": reason,
            "note": "旧版本保留在 obligation_revisions，未被修改或删除",
        }

    @staticmethod
    def _parse_verification_policy(value: dict[str, Any] | None) -> dict[str, Any]:
        """核验角色登记：角色固定为核验人员，可限定授权核验方白名单。"""
        from .models import ROLE_VERIFIER

        if value is None:
            return {"verifier_role": ROLE_VERIFIER, "allowed_verifier_parties": []}
        role = value.get("verifier_role", ROLE_VERIFIER)
        if role != ROLE_VERIFIER:
            raise ValidationError(f"完成确认的核验角色必须是 {ROLE_VERIFIER}，收到：{role}")
        parties = value.get("allowed_verifier_parties", [])
        if not isinstance(parties, list) or not all(isinstance(p, str) and p for p in parties):
            raise ValidationError("allowed_verifier_parties 必须是字符串列表")
        return {"verifier_role": role, "allowed_verifier_parties": parties}

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if actor.role != ROLE_ADMIN:
            raise AuthorizationError(f"仅{ROLE_ADMIN}可执行该操作，当前角色：{actor.role}")
