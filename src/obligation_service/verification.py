"""提交与核验服务。

- 责任院校提交合格证明（可多次提交，全部追加保留）；
- 完成必须由**独立核验人员**裁决：角色必须是核验人员，且所属方
  不同于责任方、不同于提交方；
- 核验通过按期次写 fulfilled，逾期案件即时结清；通过后又被驳回的，
  案件以 reopened 事件重新立案，旧确认记录保留。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from .clock import Clock
from .errors import AuthorizationError, ConflictError, ValidationError
from .models import ROLE_OWNER, ROLE_VERIFIER, Actor, ObligationRevision
from .repository import Repository
from .store import Store
from .util import iso_date, uid


def _current_ordinal(rev: ObligationRevision, as_of: date) -> int:
    """求 as_of 所在/最近到期的期次序号。"""
    if rev.time_window.recurrence == "once":
        return 0
    ordinal = 0
    while True:
        nxt = ordinal + 1
        if rev.time_window.due_of(nxt) > as_of:
            return ordinal
        ordinal = nxt


def _normalize_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ValidationError("必须提交至少一份证明材料 evidence")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("证明材料必须是对象列表")
        etype = item.get("type")
        ref = item.get("ref")
        if not etype or not ref:
            raise ValidationError("每份证明必须包含 type 与 ref（材料定位信息）")
        normalized.append(
            {"type": str(etype), "ref": str(ref), "original": bool(item.get("original", False))}
        )
    return normalized


def _check_conformance(evidence: list[dict[str, Any]], rev: ObligationRevision) -> list[str]:
    """对照证明要求检查符合性，返回缺失说明（空列表表示合格）。"""
    problems: list[str] = []
    required = set(rev.evidence.expected_types)
    submitted = {e["type"] for e in evidence}
    missing = required - submitted
    if missing:
        problems.append("缺少证明类型：" + "、".join(sorted(missing)))
    if rev.evidence.require_original and not any(e["original"] for e in evidence):
        problems.append("证明要求包含原件，但未提交原件")
    return problems


class VerificationService:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def _check_verifier_authorized(self, conn, plan_id: str, actor: Actor) -> None:
        """核验人员必须在计划登记的核验方白名单内（白名单为空表示不作限定）。"""
        row = conn.execute(
            "SELECT payload FROM plan_versions WHERE plan_id=?"
            " ORDER BY version DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        policy = json.loads(row["payload"]).get("verification", {}) if row else {}
        allowed = policy.get("allowed_verifier_parties", [])
        if allowed and actor.party not in allowed:
            raise AuthorizationError(
                f"核验方 {actor.party} 不在计划登记的授权核验方名单内：{'、'.join(allowed)}"
            )

    def _resolve_window(self, repo: Repository, duty_id: str, payload: dict[str, Any],
                        rev: ObligationRevision) -> tuple[int, str, date]:
        as_of = self.clock.today()
        if payload.get("ordinal") is not None:
            ordinal = payload["ordinal"]
            if not isinstance(ordinal, int) or ordinal < 0:
                raise ValidationError("ordinal 必须是非负整数")
        else:
            ordinal = _current_ordinal(rev, as_of)
        due = rev.time_window.due_of(ordinal)
        if rev.time_window.recurrence == "once" and ordinal != 0:
            raise ValidationError("一次性义务只有 w0000 一个期次")
        return ordinal, f"w{ordinal:04d}", due

    def submit_evidence(self, actor: Actor, duty_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """责任方提交证明；系统对照登记的证明要求给出符合性结论。"""
        if actor.role != ROLE_OWNER:
            raise AuthorizationError(f"仅{ROLE_OWNER}可提交证明，当前角色：{actor.role}")
        evidence = _normalize_evidence(payload.get("evidence") or [])

        with self.store.tx() as conn:
            repo = Repository(conn)
            duty = repo.get_duty(duty_id)
            rev = repo.latest_revision(duty_id)
            if rev.exempt:
                raise ConflictError("该义务已豁免，无需提交证明")
            if actor.party != rev.owner_party:
                raise AuthorizationError(
                    f"只有当前责任方 {rev.owner_party} 可提交，当前所属方：{actor.party}"
                )
            ordinal, window_key, due = self._resolve_window(repo, duty_id, payload, rev)
            problems = _check_conformance(evidence, rev)
            submission_id = uid("sub")
            now = self.clock.now().isoformat()
            conn.execute(
                "INSERT INTO submissions(submission_id, duty_id, window_key, duty_version,"
                " submitted_by, submitter_party, evidence_refs, note, submitted_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (submission_id, duty_id, window_key, rev.version, actor.actor_id, actor.party,
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                 str(payload.get("note", "")), now),
            )
            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action="evidence_submitted",
                entity_type="duty", entity_id=duty_id,
                payload={"submission_id": submission_id, "window_key": window_key,
                         "duty_version": rev.version, "evidence": evidence,
                         "conformance_problems": problems},
            )
            return {
                "submission_id": submission_id,
                "duty_id": duty_id,
                "window_key": window_key,
                "duty_version": rev.version,
                "due_date": iso_date(due),
                "conforms": not problems,
                "conformance_problems": problems,
                "note": "证明尚需核验人员独立确认后才视为完成",
            }

    def decide(self, actor: Actor, duty_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """核验人员裁决。decision=approved|rejected。"""
        if actor.role != ROLE_VERIFIER:
            raise AuthorizationError(f"完成确认必须由{ROLE_VERIFIER}作出，当前角色：{actor.role}")
        decision = payload.get("decision")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 必须是 approved 或 rejected")

        with self.store.tx() as conn:
            repo = Repository(conn)
            duty = repo.get_duty(duty_id)
            rev = repo.latest_revision(duty_id)
            self._check_verifier_authorized(conn, duty["plan_id"], actor)
            ordinal, window_key, due = self._resolve_window(repo, duty_id, payload, rev)
            submissions = repo.list_submissions(duty_id, window_key)

            # 独立性：核验方既不能是责任方，也不能是提交方
            submitter_parties = {s["submitter_party"] for s in submissions}
            if actor.party == rev.owner_party or actor.party in submitter_parties:
                raise AuthorizationError(
                    "核验不独立：核验人员所属方不能是责任方或证明提交方"
                )

            state_row = repo.get_state(duty_id, window_key)
            if decision == "approved":
                if not submissions:
                    raise ValidationError("尚无责任方提交的证明材料，无法确认合格")
                problems = _check_conformance(
                    _all_evidence(submissions), rev
                )
                if problems:
                    raise ValidationError("证明不满足登记的证明要求：" + "；".join(problems))
                if state_row is not None and state_row["status"] == "fulfilled":
                    raise ConflictError(f"期次 {window_key} 已确认合格；如需推翻请使用 rejected 裁决")

            confirmation_id = uid("cfm")
            evidence_refs = [
                {"submission_id": s["submission_id"], "evidence_refs": json.loads(s["evidence_refs"])}
                for s in submissions
            ]
            now = self.clock.now().isoformat()
            conn.execute(
                "INSERT INTO confirmations(confirmation_id, duty_id, window_key, duty_version,"
                " decision, verifier_role, verifier_party, evidence_refs, comment, decided_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (confirmation_id, duty_id, window_key, rev.version, decision,
                 actor.role, actor.party,
                 json.dumps(evidence_refs, ensure_ascii=False, sort_keys=True),
                 str(payload.get("comment", "")), now),
            )
            new_status = "fulfilled" if decision == "approved" else "rejected"
            conn.execute(
                "INSERT INTO duty_states(duty_id, window_key, status, confirmation_id, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(duty_id, window_key) DO UPDATE SET"
                " status=excluded.status, confirmation_id=excluded.confirmation_id,"
                " updated_at=excluded.updated_at",
                (duty_id, window_key, new_status, confirmation_id, now),
            )

            case = repo.get_case(duty_id, window_key)
            case_effect = "none"
            if decision == "approved":
                if case is not None and case["status"] == "open":
                    conn.execute(
                        "UPDATE overdue_cases SET status='resolved', resolution='fulfilled',"
                        " updated_at=? WHERE case_id=?",
                        (now, case["case_id"]),
                    )
                    seq = repo.next_case_seq(conn, case["case_id"])
                    conn.execute(
                        "INSERT INTO case_events(case_id, seq, at, event_type, duty_version, detail)"
                        " VALUES (?,?,?,?,?,?)",
                        (case["case_id"], seq, iso_date(self.clock.today()), "resolved", rev.version,
                         json.dumps({"resolution": "fulfilled", "confirmation_id": confirmation_id,
                                     "verifier_party": actor.party}, ensure_ascii=False, sort_keys=True)),
                    )
                    case_effect = "resolved"
            else:
                if case is None:
                    # 驳回但尚未立案（窗口内驳回）：不主动立案；到期后调度器立案
                    case_effect = "none"
                elif case["status"] == "resolved":
                    conn.execute(
                        "UPDATE overdue_cases SET status='open', resolution=NULL, updated_at=?"
                        " WHERE case_id=?",
                        (now, case["case_id"]),
                    )
                    seq = repo.next_case_seq(conn, case["case_id"])
                    conn.execute(
                        "INSERT INTO case_events(case_id, seq, at, event_type, duty_version, detail)"
                        " VALUES (?,?,?,?,?,?)",
                        (case["case_id"], seq, iso_date(self.clock.today()), "reopened", rev.version,
                         json.dumps({"confirmation_id": confirmation_id, "verifier_party": actor.party,
                                     "comment": str(payload.get("comment", ""))},
                                    ensure_ascii=False, sort_keys=True)),
                    )
                    case_effect = "reopened"

            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action=f"verification_{decision}",
                entity_type="duty", entity_id=duty_id,
                payload={"confirmation_id": confirmation_id, "window_key": window_key,
                         "duty_version": rev.version, "verifier_party": actor.party,
                         "case_effect": case_effect},
            )
            return {
                "confirmation_id": confirmation_id,
                "duty_id": duty_id,
                "window_key": window_key,
                "duty_version": rev.version,
                "decision": decision,
                "verifier_party": actor.party,
                "state": new_status,
                "case_effect": case_effect,
            }

    def trail(self, duty_id: str, window_key: str | None = None) -> dict[str, Any]:
        """查看提交与核验留痕（追加链，永不擦除）。"""
        repo = Repository(self.store.conn)
        duty = repo.get_duty(duty_id)
        rows_subs = self.store.conn.execute(
            "SELECT * FROM submissions WHERE duty_id=? ORDER BY submitted_at", (duty_id,)
        ).fetchall()
        rows_cfm = self.store.conn.execute(
            "SELECT * FROM confirmations WHERE duty_id=? ORDER BY decided_at", (duty_id,)
        ).fetchall()
        states = self.store.conn.execute(
            "SELECT * FROM duty_states WHERE duty_id=? ORDER BY window_key", (duty_id,)
        ).fetchall()
        return {
            "duty_id": duty_id,
            "code": duty["code"],
            "submissions": [
                {
                    "submission_id": s["submission_id"],
                    "window_key": s["window_key"],
                    "duty_version": s["duty_version"],
                    "submitted_by": s["submitted_by"],
                    "submitter_party": s["submitter_party"],
                    "evidence": json.loads(s["evidence_refs"]),
                    "note": s["note"],
                    "submitted_at": s["submitted_at"],
                }
                for s in rows_subs if window_key is None or s["window_key"] == window_key
            ],
            "confirmations": [
                {
                    "confirmation_id": c["confirmation_id"],
                    "window_key": c["window_key"],
                    "duty_version": c["duty_version"],
                    "decision": c["decision"],
                    "verifier_role": c["verifier_role"],
                    "verifier_party": c["verifier_party"],
                    "comment": c["comment"],
                    "decided_at": c["decided_at"],
                }
                for c in rows_cfm if window_key is None or c["window_key"] == window_key
            ],
            "window_states": [
                {"window_key": st["window_key"], "status": st["status"],
                 "confirmation_id": st["confirmation_id"], "updated_at": st["updated_at"]}
                for st in states
            ],
        }


def _all_evidence(submissions) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for s in submissions:
        items.extend(json.loads(s["evidence_refs"]))
    return items
