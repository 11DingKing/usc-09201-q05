"""领域状态与读取模型。

状态完全由追加事件重放得到，重放是确定性的：同一批事件在任意时刻、
任意进程上得到完全一致的可执行清单、逾期依据与历史计划，因此重启不会
造成提醒时钟或未决审批漂移。

三个读取模型对应年度检查的三项核对：
* executable_board —— 可执行清单（含应暂停、待前置、待审批、待换版承接）；
* overdue_basis    —— 逾期依据（原始窗口、延期审批、禁作期抵扣、交接、回执）；
* history          —— 不可变历史计划（版本链、血缘、事件序号）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .clock import parse_iso
from .events import EventType

# 任务终态
TERMINAL_STATUSES = {"completed", "cancelled"}


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _ds(value: date) -> str:
    return value.isoformat()


@dataclass
class Task:
    """年度任务的重放状态。"""

    task_id: str
    plan_id: str
    plan_version: int
    plan_year: int
    code: str
    kind: str
    title: str
    parcel_ids: list[str]
    quantity: float
    completed_quantity: float
    unit: str
    window_start: date
    window_end: date
    prerequisites: list[str]
    parent_id: str | None = None
    source_task_id: str | None = None
    status: str = "scheduled"
    assignee_id: str | None = None
    assignment_history: list[dict[str, Any]] = field(default_factory=list)
    deferrals: list[dict[str, Any]] = field(default_factory=list)
    # 最新一次延期申请：{request_id, new_window_end, reason, status, ...}
    pending_deferral: dict[str, Any] | None = None
    approved_window_end: date | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    replaced_by: list[str] = field(default_factory=list)
    cancel_reason: str | None = None
    created_seq: int = 0
    last_event_seq: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.completed_quantity)

    @property
    def effective_window_end(self) -> date:
        return self.approved_window_end or self.window_end


class CalendarState:
    """事件重放后的全部领域状态。"""

    def __init__(self) -> None:
        self.seq = 0
        self.plans: dict[str, dict[str, Any]] = {}
        self.parcels: dict[str, dict[str, Any]] = {}
        self.parties: dict[str, dict[str, Any]] = {}
        self.restrictions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, Task] = {}
        self.receipts: dict[str, str] = {}  # receipt_id -> task_id
        self.reminders: dict[str, dict[str, Any]] = {}
        self._task_seq = 0
        self._restriction_seq = 0
        self._reminder_seq = 0

    # ---- 重放 -------------------------------------------------------

    def apply(self, event) -> None:
        self.seq = event.seq
        p = event.payload
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event, p)

    def _on_plan_registered(self, e, p: dict) -> None:
        self.plans[p["plan_id"]] = {
            "plan_id": p["plan_id"],
            "code": p["code"],
            "name": p["name"],
            "versions": {},
            "current_version": None,
            "created_seq": e.seq,
        }

    def _on_plan_version_approved(self, e, p: dict) -> None:
        plan = self.plans[p["plan_id"]]
        plan["versions"][p["version_no"]] = {
            "version_no": p["version_no"],
            "approved_at": e.occurred_at,
            "approved_seq": e.seq,
            "superseded_at": None,
            "remark": p.get("remark", ""),
        }
        # 首次批准即生效；后续版本仅登记为“已批准”，换版以 supersede 为准。
        if plan["current_version"] is None:
            plan["current_version"] = p["version_no"]

    def _on_plan_version_superseded(self, e, p: dict) -> None:
        plan = self.plans[p["plan_id"]]
        plan["versions"][p["old_version_no"]]["superseded_at"] = e.occurred_at
        plan["versions"][p["old_version_no"]]["superseded_by"] = p["new_version_no"]
        plan["current_version"] = p["new_version_no"]

    def _on_parcel_registered(self, e, p: dict) -> None:
        self.parcels[p["parcel_id"]] = dict(p)

    def _on_party_registered(self, e, p: dict) -> None:
        self.parties[p["party_id"]] = dict(p)

    def _on_responsibility_assigned(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        record = {
            "party_id": p["party_id"],
            "from_seq": e.seq,
            "from_at": e.occurred_at,
            "reason": p.get("reason", ""),
            "actor": e.actor,
        }
        task.assignment_history.append(record)
        task.assignee_id = p["party_id"]
        task.last_event_seq = e.seq

    def _on_restriction_declared(self, e, p: dict) -> None:
        self.restrictions[p["restriction_id"]] = {
            "restriction_id": p["restriction_id"],
            "kind": p["kind"],
            "reason": p["reason"],
            "source_ref": p["source_ref"],
            "plan_id": p.get("plan_id"),
            "parcel_ids": p.get("parcel_ids", []),
            "task_kinds": p.get("task_kinds", []),
            "start_date": _d(p["start_date"]),
            "end_date": _d(p["end_date"]) if p.get("end_date") else None,
            "status": "active",
            "supersedes": p.get("supersedes"),
            "declared_seq": e.seq,
            "declared_at": e.occurred_at,
            "revoked_at": None,
            "revoke_reason": None,
        }
        if p.get("supersedes"):
            prior = self.restrictions.get(p["supersedes"])
            if prior and prior["status"] == "active":
                prior["status"] = "revoked"
                prior["revoked_at"] = e.occurred_at
                prior["revoke_reason"] = "superseded_by_earlier_notice"

    def _on_restriction_revoked(self, e, p: dict) -> None:
        restriction = self.restrictions[p["restriction_id"]]
        restriction["status"] = "revoked"
        restriction["revoked_at"] = e.occurred_at
        restriction["revoke_reason"] = p.get("reason", "")

    def _on_task_scheduled(self, e, p: dict) -> None:
        task = Task(
            task_id=p["task_id"],
            plan_id=p["plan_id"],
            plan_version=p["plan_version"],
            plan_year=p["plan_year"],
            code=p["code"],
            kind=p["kind"],
            title=p["title"],
            parcel_ids=list(p.get("parcel_ids", [])),
            quantity=float(p["quantity"]),
            completed_quantity=float(p.get("completed_quantity", 0.0)),
            unit=p.get("unit", ""),
            window_start=_d(p["window_start"]),
            window_end=_d(p["window_end"]),
            prerequisites=list(p.get("prerequisites", [])),
            parent_id=p.get("parent_id"),
            source_task_id=p.get("source_task_id"),
            created_seq=e.seq,
            last_event_seq=e.seq,
        )
        if p.get("assignee_id"):
            task.assignee_id = p["assignee_id"]
            task.assignment_history.append(
                {
                    "party_id": p["assignee_id"],
                    "from_seq": e.seq,
                    "from_at": e.occurred_at,
                    "reason": "initial",
                    "actor": e.actor,
                }
            )
        self.tasks[task.task_id] = task

    def _on_task_split(self, e, p: dict) -> None:
        parent = self.tasks[p["task_id"]]
        parent.status = "split"
        parent.replaced_by = list(p["child_task_ids"])
        parent.last_event_seq = e.seq

    def _on_task_deferral_requested(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        request = {
            "request_id": p["request_id"],
            "new_window_end": p["new_window_end"],
            "reason": p["reason"],
            "status": "pending",
            "requested_at": e.occurred_at,
            "requested_seq": e.seq,
            "requested_by": e.actor,
            "decided_at": None,
            "decided_by": None,
        }
        task.pending_deferral = request
        task.deferrals.append(request)
        task.last_event_seq = e.seq

    def _on_task_deferral_decided(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        request = next(
            d for d in reversed(task.deferrals) if d["request_id"] == p["request_id"]
        )
        request["status"] = p["decision"]  # approved / rejected
        request["decided_at"] = e.occurred_at
        request["decided_by"] = e.actor
        request["decision_seq"] = e.seq
        if task.pending_deferral and task.pending_deferral["request_id"] == p["request_id"]:
            task.pending_deferral = None
        if p["decision"] == "approved":
            task.approved_window_end = _d(request["new_window_end"])
        task.last_event_seq = e.seq

    def _on_task_progress_reported(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        task.completed_quantity = float(p["completed_quantity"])
        if task.status == "scheduled":
            task.status = "in_progress"
        task.receipts.append(
            {
                "receipt_id": p["receipt_id"],
                "quantity": float(p["quantity"]),
                "at": e.occurred_at,
                "seq": e.seq,
                "kind": "progress",
            }
        )
        self.receipts[p["receipt_id"]] = task.task_id
        task.last_event_seq = e.seq

    def _on_task_completed(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        task.completed_quantity = float(p["completed_quantity"])
        task.status = "completed"
        task.receipts.append(
            {
                "receipt_id": p["receipt_id"],
                "quantity": float(p["completed_quantity"]),
                "at": e.occurred_at,
                "seq": e.seq,
                "kind": "completion",
            }
        )
        self.receipts[p["receipt_id"]] = task.task_id
        task.last_event_seq = e.seq

    def _on_task_cancelled(self, e, p: dict) -> None:
        task = self.tasks[p["task_id"]]
        task.status = "cancelled"
        task.cancel_reason = p.get("reason", "")
        task.last_event_seq = e.seq

    def _on_receipt_issued(self, e, p: dict) -> None:
        # 回执与进度/完工事件同事务写入；登记索引即可。
        self.receipts.setdefault(p["receipt_id"], p["task_id"])

    def _on_reminder_raised(self, e, p: dict) -> None:
        self.reminders[p["reminder_id"]] = {
            "reminder_id": p["reminder_id"],
            "task_id": p["task_id"],
            "kind": p["kind"],
            "due_at": p["due_at"],
            "status": "raised",
            "raised_at": e.occurred_at,
            "raised_seq": e.seq,
            "ack_at": None,
            "ack_reason": None,
        }

    def _on_reminder_acknowledged(self, e, p: dict) -> None:
        reminder = self.reminders[p["reminder_id"]]
        reminder["status"] = p.get("new_status", "acknowledged")
        reminder["ack_at"] = e.occurred_at
        reminder["ack_reason"] = p.get("reason", "")

    # ---- 派生查询 ---------------------------------------------------

    def plan(self, plan_id: str) -> dict[str, Any]:
        return self.plans[plan_id]

    def is_current_version(self, task: Task) -> bool:
        return self.plans[task.plan_id]["current_version"] == task.plan_version

    def prerequisites_met(self, task: Task) -> tuple[bool, list[str]]:
        """前置是否完成；被拆分的前置由其全部承接子任务完成视为满足。"""

        unmet: list[str] = []
        for prereq_id in task.prerequisites:
            prereq = self.tasks.get(prereq_id)
            if prereq is None:
                unmet.append(prereq_id)
                continue
            if prereq.status == "completed":
                continue
            if prereq.status == "split":
                children = [self.tasks[c] for c in prereq.replaced_by]
                if children and all(c.status == "completed" for c in children):
                    continue
            unmet.append(prereq_id)
        return not unmet, unmet

    def matching_restrictions(self, task: Task) -> list[dict[str, Any]]:
        """返回与任务匹配的生态限制（不限状态，调用方按日期/状态判断）。"""

        result = []
        for restriction in self.restrictions.values():
            if restriction["plan_id"] and restriction["plan_id"] != task.plan_id:
                continue
            if restriction["task_kinds"] and task.kind not in restriction["task_kinds"]:
                continue
            if restriction["parcel_ids"] and not (
                set(restriction["parcel_ids"]) & set(task.parcel_ids)
            ):
                continue
            result.append(restriction)
        return result

    @staticmethod
    def _restriction_end(restriction: dict[str, Any], today: date) -> date:
        """限制的实际失效日。

        生效中的开放式限制按当日截断；被解除/被提前通知修订的，
        以解除日为终点——已实际阻断的天数保留在历史顺延中，不回改。
        """

        if restriction["end_date"] is not None:
            if restriction["status"] == "active":
                return restriction["end_date"]
            return min(restriction["end_date"], parse_iso(restriction["revoked_at"]).date())
        if restriction["status"] == "active":
            return today
        return parse_iso(restriction["revoked_at"]).date()

    def restriction_effect(
        self, task: Task, today: date
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """计算限制对任务的影响。

        返回 (当日生效限制明细, 顺延天数, 当日是否处于禁作期)。
        顺延天数 = 合法作业区间 [window_start, 批准后窗口末日] 与
        限制覆盖区间（截至当日，未来日期不计）交集的并集天数；
        禁作期持续期间任务不会被判逾期，解除后顺延天数固定保留。
        """

        base_end = task.effective_window_end
        interval_start = task.window_start
        blocked: list[tuple[date, date]] = []
        active_today: list[dict[str, Any]] = []
        for restriction in self.matching_restrictions(task):
            r_start = restriction["start_date"]
            r_end = self._restriction_end(restriction, today)
            if (
                restriction["status"] == "active"
                and restriction["end_date"] is not None
                and r_start <= today <= r_end
            ):
                active_today.append(restriction)
            elif (
                restriction["status"] == "active"
                and restriction["end_date"] is None
                and r_start <= today
            ):
                active_today.append(restriction)
            overlap_start = max(r_start, interval_start)
            overlap_end = min(r_end, base_end, today)
            if overlap_start <= overlap_end:
                blocked.append((overlap_start, overlap_end))
        toll_days = _union_days(blocked)
        paused = bool(active_today)
        return active_today, toll_days, paused

    def effective_due(self, task: Task, today: date) -> tuple[date, dict[str, Any]]:
        """有效到期日 = 窗口末日 + 已批准延期 + 禁作期顺延。"""

        active, toll_days, paused = self.restriction_effect(task, today)
        deferral_days = 0
        if task.approved_window_end and task.approved_window_end > task.window_end:
            deferral_days = (task.approved_window_end - task.window_end).days
        due = task.window_end + timedelta(days=deferral_days + toll_days)
        basis = {
            "window_end": _ds(task.window_end),
            "deferral_days": deferral_days,
            "toll_days": toll_days,
            "paused": paused,
            "active_restrictions": [r["restriction_id"] for r in active],
        }
        return due, basis

    # ---- 读取模型一：执行看板 --------------------------------------

    def executable_board(self, today: date) -> dict[str, Any]:
        board: dict[str, Any] = {
            "as_of": _ds(today),
            "executable": [],
            "paused": [],
            "waiting_prerequisites": [],
            "pending_deferral_decisions": [],
            "pending_rebaseline": [],
            "overdue": [],
        }
        for task in sorted(self.tasks.values(), key=lambda t: (t.window_end, t.code)):
            if task.status in TERMINAL_STATUSES or task.status == "split":
                continue
            due, basis = self.effective_due(task, today)
            prereq_met, unmet = self.prerequisites_met(task)
            current = self.is_current_version(task)
            common = {
                "task_id": task.task_id,
                "code": task.code,
                "kind": task.kind,
                "title": task.title,
                "plan_id": task.plan_id,
                "plan_version": task.plan_version,
                "plan_year": task.plan_year,
                "parcel_ids": task.parcel_ids,
                "remaining_quantity": _num(task.remaining),
                "unit": task.unit,
                "window_start": _ds(task.window_start),
                "window_end": _ds(task.window_end),
                "effective_due": _ds(due),
                "assignee_id": task.assignee_id,
                "status": task.status,
            }
            if task.pending_deferral:
                board["pending_deferral_decisions"].append(
                    {
                        **common,
                        "request_id": task.pending_deferral["request_id"],
                        "new_window_end": task.pending_deferral["new_window_end"],
                        "reason": task.pending_deferral["reason"],
                        "requested_at": task.pending_deferral["requested_at"],
                    }
                )
                continue
            if not current:
                board["pending_rebaseline"].append(
                    {**common, "current_version": self.plans[task.plan_id]["current_version"]}
                )
                continue
            if today < task.window_start:
                # 未到作业窗口：前置未满足仍需提示，否则暂不上看板
                if not prereq_met:
                    board["waiting_prerequisites"].append(
                        {**common, "unmet_prerequisites": unmet}
                    )
                continue
            if basis["paused"]:
                board["paused"].append(
                    {
                        **common,
                        "restriction_ids": basis["active_restrictions"],
                        "restrictions": [
                            self._restriction_view(rid) for rid in basis["active_restrictions"]
                        ],
                        "toll_days": basis["toll_days"],
                    }
                )
                continue
            if not prereq_met:
                board["waiting_prerequisites"].append(
                    {**common, "unmet_prerequisites": unmet}
                )
                continue
            if today > due:
                board["overdue"].append(
                    {**common, "days_overdue": (today - due).days, "basis": basis}
                )
                continue
            if today < task.window_start:
                # 未到作业窗口，不进入可执行清单
                continue
            board["executable"].append(common)
        return board

    def _restriction_view(self, restriction_id: str) -> dict[str, Any]:
        r = self.restrictions[restriction_id]
        return {
            "restriction_id": r["restriction_id"],
            "kind": r["kind"],
            "reason": r["reason"],
            "source_ref": r["source_ref"],
            "start_date": _ds(r["start_date"]),
            "end_date": _ds(r["end_date"]) if r["end_date"] else None,
        }

    # ---- 读取模型二：逾期依据 --------------------------------------

    def overdue_basis(self, today: date) -> list[dict[str, Any]]:
        rows = []
        for task in sorted(self.tasks.values(), key=lambda t: (t.window_end, t.code)):
            if task.status in TERMINAL_STATUSES or task.status == "split":
                continue
            due, basis = self.effective_due(task, today)
            if today <= due or basis["paused"]:
                continue
            if task.pending_deferral:
                continue
            prereq_met, unmet = self.prerequisites_met(task)
            if not prereq_met:
                continue
            rows.append(
                {
                    "task_id": task.task_id,
                    "code": task.code,
                    "title": task.title,
                    "plan_id": task.plan_id,
                    "plan_version": task.plan_version,
                    "version_current": self.is_current_version(task),
                    "plan_year": task.plan_year,
                    "today": _ds(today),
                    "original_window": {
                        "start": _ds(task.window_start),
                        "end": _ds(task.window_end),
                    },
                    "effective_due": _ds(due),
                    "days_overdue": (today - due).days,
                    "deferrals": [
                        {
                            "request_id": d["request_id"],
                            "new_window_end": d["new_window_end"],
                            "reason": d["reason"],
                            "status": d["status"],
                            "requested_at": d["requested_at"],
                            "decided_at": d["decided_at"],
                            "decided_by": d["decided_by"],
                            "decision_seq": d.get("decision_seq"),
                        }
                        for d in task.deferrals
                    ],
                    "restriction_toll": {
                        "toll_days": basis["toll_days"],
                        "details": [
                            {
                                "restriction_id": r["restriction_id"],
                                "kind": r["kind"],
                                "reason": r["reason"],
                                "source_ref": r["source_ref"],
                                "window": {
                                    "start": _ds(r["start_date"]),
                                    "end": _ds(r["end_date"]) if r["end_date"] else None,
                                },
                                "declared_seq": r["declared_seq"],
                            }
                            for r in self.matching_restrictions(task)
                        ],
                    },
                    "progress": {
                        "quantity": _num(task.completed_quantity),
                        "total": _num(task.quantity),
                        "remaining": _num(task.remaining),
                    },
                    "assignee": task.assignee_id,
                    "assignee_handover_trail": [
                        {
                            "party_id": h["party_id"],
                            "from_at": h["from_at"],
                            "reason": h["reason"],
                            "seq": h["from_seq"],
                        }
                        for h in task.assignment_history
                    ],
                    "receipts": task.receipts,
                    "lineage": self._lineage(task),
                    "evidence": {
                        "scheduled_seq": task.created_seq,
                        "last_event_seq": task.last_event_seq,
                        "replayed_through_seq": self.seq,
                    },
                }
            )
        return rows

    def _lineage(self, task: Task) -> dict[str, Any]:
        parents = []
        cur = task
        while cur.parent_id:
            cur = self.tasks[cur.parent_id]
            parents.append(cur.task_id)
        children = sorted(
            t.task_id for t in self.tasks.values() if t.parent_id == task.task_id
        )
        return {"parents": parents, "children": children,
                "source_task_id": task.source_task_id}

    # ---- 读取模型三：历史计划 --------------------------------------

    def history(self) -> dict[str, Any]:
        result: dict[str, Any] = {"plans": []}
        for plan in sorted(self.plans.values(), key=lambda p: p["code"]):
            versions = []
            for version_no in sorted(plan["versions"]):
                v = plan["versions"][version_no]
                tasks = [
                    self._task_history(task)
                    for task in self.tasks.values()
                    if task.plan_id == plan["plan_id"] and task.plan_version == version_no
                ]
                tasks.sort(key=lambda t: (t["plan_year"], t["code"], t["task_id"]))
                versions.append(
                    {
                        "version_no": version_no,
                        "approved_at": v["approved_at"],
                        "approved_seq": v["approved_seq"],
                        "superseded_at": v.get("superseded_at"),
                        "superseded_by": v.get("superseded_by"),
                        "remark": v.get("remark", ""),
                        "tasks": tasks,
                    }
                )
            result["plans"].append(
                {
                    "plan_id": plan["plan_id"],
                    "code": plan["code"],
                    "name": plan["name"],
                    "current_version": plan["current_version"],
                    "created_seq": plan["created_seq"],
                    "versions": versions,
                }
            )
        result["replayed_through_seq"] = self.seq
        return result

    def _task_history(self, task: Task) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "code": task.code,
            "kind": task.kind,
            "title": task.title,
            "plan_year": task.plan_year,
            "parcel_ids": task.parcel_ids,
            "quantity": _num(task.quantity),
            "completed_quantity": _num(task.completed_quantity),
            "unit": task.unit,
            "window": {"start": _ds(task.window_start), "end": _ds(task.window_end)},
            "approved_window_end": (
                _ds(task.approved_window_end) if task.approved_window_end else None
            ),
            "prerequisites": task.prerequisites,
            "status": task.status,
            "cancel_reason": task.cancel_reason,
            "assignee_id": task.assignee_id,
            "parent_id": task.parent_id,
            "source_task_id": task.source_task_id,
            "children": task.replaced_by or [
                t.task_id
                for t in self.tasks.values()
                if t.parent_id == task.task_id
            ],
            "deferrals": task.deferrals,
            "assignment_history": task.assignment_history,
            "receipts": task.receipts,
            "created_seq": task.created_seq,
            "last_event_seq": task.last_event_seq,
        }

    # ---- 提醒 -------------------------------------------------------

    def active_reminder_for(self, task_id: str) -> dict[str, Any] | None:
        for reminder in self.reminders.values():
            if reminder["task_id"] == task_id and reminder["status"] == "raised":
                return reminder
        return None


def _union_days(intervals: list[tuple[date, date]]) -> int:
    """多个闭区间日期并集的天数。"""

    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end + timedelta(days=1):
            cur_end = max(cur_end, end)
        else:
            total += (cur_end - cur_start).days + 1
            cur_start, cur_end = start, end
    total += (cur_end - cur_start).days + 1
    return total


def _num(value: float) -> float | int:
    if float(value).is_integer():
        return int(value)
    return round(value, 4)
