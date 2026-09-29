"""调度器：时间窗求值、幂等提醒、逾期案件与到期解释。

关键保证：
- 提醒与案件由业务唯一约束保护，重复调度、服务恢复重放不会产生多条；
- 全部日期判断经注入时钟，可在任意历史区间重算；
- 修订（延期/豁免/移交）产生新版本：新版本生成新提醒，旧版本未发送的
  提醒被置为 superseded/suppressed，已发送记录与旧接收人完整留痕；
- 逾期案件追加事件流（立案、延期、移交、重开、结清），绝不覆盖历史。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from .errors import AuthorizationError, ValidationError
from .models import ROLE_ADMIN, Actor, ObligationRevision
from .repository import Repository
from .store import Store
from .util import iso_date, parse_date, uid


class Transport(Protocol):
    """提醒下发通道（日历/邮件/站内信）。"""

    name: str

    def send(self, reminder: dict[str, Any]) -> bool:
        """成功返回 True；失败返回 False，调度器保留 pending 以便重试。""";


class RecordingTransport:
    """默认通道：不落外部系统，仅记录，全部视为成功。"""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, reminder: dict[str, Any]) -> bool:
        self.sent.append(
            {
                "reminder_id": reminder["reminder_id"],
                "recipient": reminder["recipient"],
                "kind": reminder["kind"],
                "scheduled_for": reminder["scheduled_for"],
            }
        )
        return True


@dataclass
class RunSummary:
    run_id: str
    kind: str
    as_of: str
    range_start: str
    range_end: str
    plans_scanned: int
    duties_scanned: int
    reminders_created: int
    reminders_sent: int
    reminders_superseded: int
    cases_opened: int
    cases_updated: int
    cases_resolved: int
    explanations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Scheduler:
    def __init__(self, store: Store, clock, transport: Transport | None = None) -> None:
        self.store = store
        self.clock = clock
        self.transport = transport or RecordingTransport()

    # ---- 对外入口 ----

    def dispatch(self, actor: Actor | None = None, *, plan_id: str | None = None) -> dict[str, Any]:
        """按当前时钟做一次调度（定时器/服务恢复后均可安全重复调用）。"""
        as_of = self.clock.today()
        return self._run(
            actor or _system_actor(),
            as_of=as_of,
            range_start=date.min,
            range_end=as_of,
            plan_id=plan_id,
            kind="dispatch",
            with_explanations=False,
        )

    def recalculate(
        self,
        actor: Actor,
        *,
        range_start: str,
        range_end: str,
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """管理员重算指定闭区间，幂等；返回每项义务的到期解释。"""
        if actor.role != ROLE_ADMIN:
            raise AuthorizationError(f"仅{ROLE_ADMIN}可执行区间重算")
        lo = parse_date(range_start, "range_start")
        hi = parse_date(range_end, "range_end")
        if lo > hi:
            raise ValidationError("range_start 不能晚于 range_end")
        return self._run(
            actor, as_of=hi, range_start=lo, range_end=hi, plan_id=plan_id,
            kind="recalculate", with_explanations=True,
        )

    # ---- 主流程 ----

    def _run(
        self,
        actor: Actor,
        *,
        as_of: date,
        range_start: date,
        range_end: date,
        plan_id: str | None,
        kind: str,
        with_explanations: bool,
    ) -> dict[str, Any]:
        run_id = uid("run")
        now_iso = self.clock.now().isoformat()
        counters = {
            "plans": 0, "duties": 0, "created": 0, "sent": 0, "superseded": 0,
            "opened": 0, "updated": 0, "resolved": 0,
        }
        explanations: list[dict[str, Any]] = []

        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO runs(run_id, kind, range_start, range_end, requested_by,"
                " started_at, status) VALUES (?,?,?,?,?,?, 'running')",
                (run_id, kind,
                 None if range_start == date.min else iso_date(range_start),
                 iso_date(range_end), actor.actor_id, now_iso),
            )
            repo = Repository(conn)
            plan_ids = self._plan_ids(conn, plan_id)

            for pid in plan_ids:
                counters["plans"] += 1
                for duty in repo.list_duties(pid):
                    counters["duties"] += 1
                    rev = repo.effective_revision(duty["duty_id"], as_of)
                    if rev.exempt:
                        counters["superseded"] += self._suppress_duty(
                            conn, duty["duty_id"]
                        )
                        counters["resolved"] += self._resolve_case_if_open(
                            conn, repo, duty["duty_id"], "*", "exempted", as_of, rev.version
                        )
                    else:
                        counters["superseded"] += self._supersede_older_versions(
                            conn, duty["duty_id"], rev.version
                        )
                        max_offset = max(rev.time_window.remind_before_days, default=0)
                        for ordinal, due in self._iter_windows(rev, range_end, max_offset):
                            window_key = f"w{ordinal:04d}"
                            self._schedule_points(
                                conn, repo, pid, duty["duty_id"], rev, window_key, due,
                                as_of, range_start, range_end, run_id, counters,
                            )
                            self._reconcile(
                                conn, repo, pid, duty["duty_id"], rev,
                                window_key, due, as_of, counters,
                            )
                        # 独立对账：义务可能延期到未来日而离开枚举窗口，
                        # 已立案案件仍须追加 deadline_moved/transferred 事件。
                        self._reconcile_open_cases(
                            conn, repo, pid, duty["duty_id"], rev, as_of, counters
                        )

                    if with_explanations:
                        explanations.append(self.explain(duty["duty_id"], as_of))

            sent_now = self._flush_pending(conn, run_id)
            counters["sent"] += sent_now

            conn.execute(
                "UPDATE runs SET finished_at=?, status='done', detail=? WHERE run_id=?",
                (self.clock.now().isoformat(),
                 _dumps({k: v for k, v in counters.items() if k != "plans"} | {"plans": counters["plans"]}),
                 run_id),
            )
            self.store.audit(
                conn, actor=actor.actor_id, actor_role=actor.role, action=f"scheduler_{kind}",
                entity_type="run", entity_id=run_id, run_id=run_id,
                payload={"as_of": iso_date(as_of), "range_start": iso_date(range_start),
                         "range_end": iso_date(range_end), **counters},
            )

        summary = RunSummary(
            run_id=run_id, kind=kind, as_of=iso_date(as_of),
            range_start=iso_date(range_start), range_end=iso_date(range_end),
            plans_scanned=counters["plans"], duties_scanned=counters["duties"],
            reminders_created=counters["created"], reminders_sent=counters["sent"],
            reminders_superseded=counters["superseded"], cases_opened=counters["opened"],
            cases_updated=counters["updated"], cases_resolved=counters["resolved"],
            explanations=explanations,
        )
        return summary.to_dict()

    # ---- 窗口枚举 ----

    def _iter_windows(self, rev: ObligationRevision, hi: date, lookahead: int):
        ordinal = 0
        while True:
            due = rev.time_window.due_of(ordinal)
            # 提醒点最早为 due - max_offset；它晚于区间上界则后续期次更晚
            if due - timedelta(days=lookahead) > hi:
                return
            yield ordinal, due
            if rev.time_window.recurrence == "once":
                return
            ordinal += 1

    # ---- 提醒点 ----

    def _schedule_points(
        self, conn, repo, plan_id, duty_id, rev: ObligationRevision, window_key, due: date,
        as_of, lo, hi, run_id, counters,
    ) -> None:
        tw = rev.time_window
        points: list[tuple[str, date]] = []
        for offset in sorted(set(tw.remind_before_days), reverse=True):
            kind = "due" if offset == 0 else "upcoming"
            points.append((kind, due - timedelta(days=offset)))
        if 0 not in tw.remind_before_days:
            points.append(("due", due))
        overdue_on = due + timedelta(days=tw.grace_days)
        if as_of >= overdue_on:
            points.append(("overdue", overdue_on))

        recipient = rev.owner_contact
        for kind, point_date in points:
            if point_date < lo or point_date > hi or point_date > as_of:
                continue
            scheduled_for = iso_date(point_date)
            existing = repo.find_reminder(duty_id, rev.version, kind, window_key, scheduled_for)
            if existing is not None:
                continue  # 幂等：重复调度/服务恢复不产生第二条
            reminder_id = uid("rmd")
            conn.execute(
                "INSERT INTO reminders(reminder_id, plan_id, duty_id, duty_version, window_key,"
                " kind, scheduled_for, channel, recipient, status, reason, run_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (reminder_id, plan_id, duty_id, rev.version, window_key, kind, scheduled_for,
                 rev.channel, recipient, "pending",
                 f"{rev.title} 于 {scheduled_for} {kind}（责任人：{rev.owner_party}）",
                 run_id, self.clock.now().isoformat()),
            )
            counters["created"] += 1

    def _flush_pending(self, conn, run_id: str) -> int:
        """把所有到期 pending 提醒交给通道；成功置 sent，失败留 pending 重试。"""
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status='pending' ORDER BY scheduled_for, reminder_id"
        ).fetchall()
        sent = 0
        for row in rows:
            reminder = dict(row)
            try:
                ok = self.transport.send(reminder)
            except Exception:
                ok = False
            if ok:
                conn.execute(
                    "UPDATE reminders SET status='sent', sent_at=? WHERE reminder_id=?",
                    (self.clock.now().isoformat(), reminder["reminder_id"]),
                )
                sent += 1
        return sent

    # ---- 版本收敛 ----

    def _supersede_older_versions(self, conn, duty_id: str, current_version: int) -> int:
        cur = conn.execute(
            "UPDATE reminders SET status='superseded' WHERE duty_id=? AND duty_version<?"
            " AND status='pending'",
            (duty_id, current_version),
        )
        return cur.rowcount

    def _suppress_duty(self, conn, duty_id: str) -> int:
        """豁免后只抑制未发送提醒；已发送记录是既成事实，保持 sent 不删改。"""
        cur = conn.execute(
            "UPDATE reminders SET status='suppressed' WHERE duty_id=? AND status='pending'",
            (duty_id,),
        )
        conn.execute(
            "INSERT INTO audit_log(at, actor, actor_role, action, entity_type, entity_id,"
            " run_id, payload) VALUES (?,?,?,?,?,?,?,?)",
            (self.clock.now().isoformat(), "system", ROLE_ADMIN, "reminders_suppressed",
             "duty", duty_id, None, _dumps({"reason": "豁免", "count": cur.rowcount})),
        )
        return cur.rowcount

    # ---- 逾期案件 ----

    def _reconcile(
        self, conn, repo, plan_id, duty_id, rev: ObligationRevision,
        window_key, due: date, as_of: date, counters,
    ) -> None:
        overdue_on = due + timedelta(days=rev.time_window.grace_days)
        state_row = repo.get_state(duty_id, window_key)
        state = state_row["status"] if state_row else "pending"
        case = repo.get_case(duty_id, window_key)

        if state == "fulfilled":
            if case is not None and case["status"] == "open":
                self._resolve(conn, case["case_id"], "fulfilled", as_of, rev.version)
                counters["resolved"] += 1
            return

        if as_of < overdue_on:
            return  # 尚在时间窗/宽限期内

        if case is None:
            case_id = uid("case")
            conn.execute(
                "INSERT INTO overdue_cases(case_id, plan_id, duty_id, window_key, opened_on,"
                " due_date, owner_party, current_duty_version, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'open', ?)",
                (case_id, plan_id, duty_id, window_key, iso_date(as_of), iso_date(due),
                 rev.owner_party, rev.version, self.clock.now().isoformat()),
            )
            code_index = self._build_code_index(conn, [plan_id])
            blockers = self._blockers(repo, code_index, plan_id, rev, window_key)
            self._event(
                conn, case_id, as_of, "opened", rev.version,
                {"due_date": iso_date(due), "overdue_on": iso_date(overdue_on),
                 "owner_party": rev.owner_party, "evidence_required": rev.evidence.to_dict(),
                 "blockers": blockers},
            )
            counters["opened"] += 1
            return

        if case["status"] == "open":
            self._update_open_case(conn, case, duty_id, rev, window_key, due, as_of, counters)

    def _reconcile_open_cases(
        self, conn, repo, plan_id, duty_id, rev: ObligationRevision, as_of: date, counters
    ) -> None:
        """对所有 open 案件按当前生效版本对账（即使新截止日已落在枚举窗口之外）。"""
        rows = conn.execute(
            "SELECT * FROM overdue_cases WHERE duty_id=? AND status='open'", (duty_id,)
        ).fetchall()
        for case in rows:
            window_key = case["window_key"]
            ordinal = int(window_key.lstrip("w"))
            due = rev.time_window.due_of(ordinal)
            self._update_open_case(conn, case, duty_id, rev, window_key, due, as_of, counters)

    def _update_open_case(
        self, conn, case, duty_id, rev: ObligationRevision, window_key, due: date,
        as_of: date, counters,
    ) -> None:
        # 已立案：对修订（延期/移交）做追加式收敛
        changed = False
        detail: dict[str, Any] = {}
        if case["due_date"] != iso_date(due):
            detail["old_due_date"] = case["due_date"]
            detail["new_due_date"] = iso_date(due)
            self._event(conn, case["case_id"], as_of, "deadline_moved", rev.version, detail)
            changed = True
        if case["owner_party"] != rev.owner_party:
            detail = {"old_owner_party": case["owner_party"], "new_owner_party": rev.owner_party}
            self._event(conn, case["case_id"], as_of, "transferred", rev.version, detail)
            changed = True
        if changed:
            conn.execute(
                "UPDATE overdue_cases SET due_date=?, owner_party=?, current_duty_version=?,"
                " updated_at=? WHERE case_id=?",
                (iso_date(due), rev.owner_party, rev.version,
                 self.clock.now().isoformat(), case["case_id"]),
            )
            counters["updated"] += 1

    def _resolve_case_if_open(
        self, conn, repo, duty_id: str, window_key: str, resolution: str,
        as_of: date, version: int,
    ) -> int:
        if window_key == "*":
            rows = conn.execute(
                "SELECT case_id FROM overdue_cases WHERE duty_id=? AND status='open'", (duty_id,)
            ).fetchall()
            count = 0
            for row in rows:
                self._resolve(conn, row["case_id"], resolution, as_of, version)
                count += 1
            return count
        case = repo.get_case(duty_id, window_key)
        if case is not None and case["status"] == "open":
            self._resolve(conn, case["case_id"], resolution, as_of, version)
            return 1
        return 0

    def _resolve(self, conn, case_id: str, resolution: str, as_of: date, version: int) -> None:
        conn.execute(
            "UPDATE overdue_cases SET status='resolved', resolution=?, updated_at=? WHERE case_id=?",
            (resolution, self.clock.now().isoformat(), case_id),
        )
        self._event(conn, case_id, as_of, "resolved", version, {"resolution": resolution})

    def _event(self, conn, case_id, at: date, event_type: str, version: int, detail: dict) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO case_events(case_id, seq, at, event_type, duty_version, detail)"
            " VALUES (?,?,?,?,?,?)",
            (case_id, int(row["next"]), iso_date(at), event_type, version, _dumps(detail)),
        )

    # ---- 前置依赖 ----

    def _build_code_index(self, conn, plan_ids: list[str]) -> dict[str, dict[str, str]]:
        index: dict[str, dict[str, str]] = {}
        for pid in plan_ids:
            for duty in conn.execute("SELECT * FROM obligations WHERE plan_id=?", (pid,)).fetchall():
                index.setdefault(pid, {})[duty["code"]] = duty["duty_id"]
        return index

    def _blockers(self, repo, code_index, plan_id, rev: ObligationRevision, window_key: str) -> list[str]:
        blockers: list[str] = []
        for pre in rev.prerequisites:
            dep_id = code_index.get(plan_id, {}).get(pre.duty_code)
            if dep_id is None:
                blockers.append(f"前置义务 {pre.duty_code} 不存在")
                continue
            # 前置义务已被豁免 -> 不再阻塞
            if repo.latest_revision(dep_id).exempt:
                continue
            if pre.same_window:
                dep_state = repo.get_state(dep_id, window_key)
                status = dep_state["status"] if dep_state else "pending"
                if status != "fulfilled":
                    blockers.append(
                        f"前置义务 {pre.duty_code} 的同一期次 {window_key} 尚未经核验确认（当前：{status}）"
                    )
            else:
                fulfilled = self.store.conn.execute(
                    "SELECT 1 FROM duty_states WHERE duty_id=? AND status='fulfilled' LIMIT 1",
                    (dep_id,),
                ).fetchone()
                if fulfilled is None:
                    blockers.append(f"前置义务 {pre.duty_code} 尚无任何期次经核验确认合格")
        return blockers

    # ---- 到期解释 ----

    def explain(self, duty_id: str, as_of: date | str | None = None) -> dict[str, Any]:
        """解释义务每个期次为何到期/未到期/已结清，含全部版本的提醒留痕。"""
        if as_of is None:
            as_of = self.clock.today()
        elif isinstance(as_of, str):
            as_of = parse_date(as_of, "as_of")

        repo = Repository(self.store.conn)
        duty = repo.get_duty(duty_id)
        rev = repo.effective_revision(duty_id, as_of)
        plan_ids = [duty["plan_id"]]
        code_index = self._build_code_index(self.store.conn, plan_ids)

        windows: list[dict[str, Any]] = []
        if rev.exempt:
            ordinals: list[tuple[int, date]] = []
        else:
            max_offset = max(rev.time_window.remind_before_days, default=0)
            ordinals = list(self._iter_windows(rev, as_of, max_offset))
        # 合并已有期次状态/案件：义务即使延期到未来日，历史期次仍须可解释
        known_keys = {wk for wk, in self.store.conn.execute(
            "SELECT DISTINCT window_key FROM duty_states WHERE duty_id=?", (duty_id,)).fetchall()}
        known_keys |= {wk for wk, in self.store.conn.execute(
            "SELECT DISTINCT window_key FROM overdue_cases WHERE duty_id=?", (duty_id,)).fetchall()}
        present = {o for o, _ in ordinals}
        for wk in known_keys:
            ordinal = int(wk.lstrip("w"))
            if ordinal not in present:
                ordinals.append((ordinal, rev.time_window.due_of(ordinal)))
        ordinals.sort(key=lambda x: x[0])
        for ordinal, due in ordinals:
            window_key = f"w{ordinal:04d}"
            overdue_on = due + timedelta(days=rev.time_window.grace_days)
            state_row = repo.get_state(duty_id, window_key)
            blockers = self._blockers(repo, code_index, duty["plan_id"], rev, window_key)
            reminders = [
                {
                    "reminder_id": r["reminder_id"],
                    "duty_version": r["duty_version"],
                    "kind": r["kind"],
                    "scheduled_for": r["scheduled_for"],
                    "recipient": r["recipient"],
                    "status": r["status"],
                    "sent_at": r["sent_at"],
                }
                for r in self.store.conn.execute(
                    "SELECT * FROM reminders WHERE duty_id=? AND window_key=?"
                    " ORDER BY scheduled_for, duty_version",
                    (duty_id, window_key),
                ).fetchall()
            ]
            case_row = repo.get_case(duty_id, window_key)
            case = None
            if case_row is not None:
                case = {
                    "case_id": case_row["case_id"],
                    "status": case_row["status"],
                    "opened_on": case_row["opened_on"],
                    "due_date": case_row["due_date"],
                    "owner_party": case_row["owner_party"],
                    "resolution": case_row["resolution"],
                    "events": [
                        {"seq": e["seq"], "at": e["at"], "event": e["event_type"],
                         "duty_version": e["duty_version"], "detail": _loads(e["detail"])}
                        for e in repo.list_case_events(case_row["case_id"])
                    ],
                }
            reasons = self._reasons(
                state_row["status"] if state_row else "pending", due, overdue_on,
                as_of, blockers, bool(case_row and case_row["status"] == "open"), rev,
            )
            windows.append({
                "window_key": window_key,
                "ordinal": ordinal,
                "due_date": iso_date(due),
                "overdue_on": iso_date(overdue_on),
                "state": state_row["status"] if state_row else "pending",
                "blockers": blockers,
                "reminders": reminders,
                "overdue_case": case,
                "why": reasons,
            })

        return {
            "duty_id": duty_id,
            "code": duty["code"],
            "plan_id": duty["plan_id"],
            "as_of": iso_date(as_of),
            "effective_version": rev.version,
            "title": rev.title,
            "exempt": rev.exempt,
            "owner_party": rev.owner_party,
            "owner_contact": rev.owner_contact,
            "evidence_required": rev.evidence.to_dict(),
            "prerequisites": [p.duty_code for p in rev.prerequisites],
            "windows": windows,
        }

    def _reasons(self, state, due, overdue_on, as_of, blockers, case_open, rev) -> list[str]:
        reasons: list[str] = []
        if rev.exempt:
            if state == "fulfilled":
                reasons.append(
                    f"该期次已由核验人员确认合格；义务整体后被豁免（新版本登记），历史记录保留"
                )
                return reasons
            return ["该义务已被豁免（新版本登记），不再生成提醒与逾期案件；旧版本记录保留"]
        if state == "fulfilled":
            reasons.append(f"已由核验人员确认合格（截止日 {iso_date(due)}），义务结清")
            return reasons
        if as_of < due:
            reasons.append(f"尚未到期：截止日为 {iso_date(due)}，当前 {iso_date(as_of)}")
            if case_open:
                reasons.append("该期次曾按旧截止日逾期立案，延期后案件保持开启，事件流可查")
        elif as_of <= overdue_on:
            reasons.append(
                f"已到交付期：截止日 {iso_date(due)}，宽限至 {iso_date(overdue_on)}"
            )
        else:
            reasons.append(
                f"已逾期：截止日 {iso_date(due)}，宽限满日 {iso_date(overdue_on)}，"
                f"当前 {iso_date(as_of)}，逾期 {(as_of - overdue_on).days} 天"
            )
            if case_open:
                reasons.append("已生成逾期案件并通知当前责任方")
        if blockers:
            reasons.append("前置条件未满足：" + "；".join(blockers))
        reasons.append(
            f"当前责任方：{rev.owner_party}（{rev.owner_contact}）；"
            f"证明要求：{('、'.join(rev.evidence.expected_types) or '未限定类型')}"
            + ("（须原件）" if rev.evidence.require_original else "")
        )
        return reasons

    # ---- 杂项 ----

    def _plan_ids(self, conn, plan_id: str | None) -> list[str]:
        if plan_id is not None:
            row = conn.execute("SELECT plan_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                _raise_plan(plan_id)
            return [plan_id]
        return [r["plan_id"] for r in conn.execute("SELECT plan_id FROM plans ORDER BY created_at").fetchall()]


def _raise_plan(plan_id: str):
    from .errors import NotFoundError

    raise NotFoundError(f"计划不存在：{plan_id}")


def _system_actor() -> Actor:
    return Actor(actor_id="system-scheduler", name="系统调度器", role=ROLE_ADMIN, party="system")


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> Any:
    import json

    return json.loads(value)
