"""森林经营方案年历领域核心。

设计要点：

* 多年方案按批准版本管理，换版只追加新版本，旧版本与旧任务原样保留；
* 跨年度延期走审批流，批准后形成新的有效窗口，原计划窗口始终保留为逾期依据；
* 临时禁作期（政策边界、灾害风险、生态限制）以独立记录发布，
  命中任务动作与地块时使其“暂停”，而不是直接修改任务；
* 拆分、回执、责任人交接均为追加记录，重复回执被拒绝；
* 可执行清单、逾期状态、提醒时刻均为状态相对于某个日期/时刻的纯函数，
  服务重启重放事件后结果完全一致，不依赖内存定时器。
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from .store import EventStore

# 动作类型
HARVEST = "harvest"
REPLANT = "replant"
TEND = "tend"
ACTIONS = (HARVEST, REPLANT, TEND)
ACTION_LABELS = {HARVEST: "采收", REPLANT: "补植", TEND: "管护"}

# 限制来源类型
POLICY = "policy"
DISASTER = "disaster"
ECOLOGY = "ecology"
RESTRICTION_KINDS = (POLICY, DISASTER, ECOLOGY)
RESTRICTION_KIND_LABELS = {
    POLICY: "政策边界",
    DISASTER: "灾害风险",
    ECOLOGY: "生态限制",
}

REMIND_DAYS_BEFORE_START = 7
REMIND_HOUR = 9


class DomainError(Exception):
    """业务规则冲突，status 给出建议的 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _d(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None)


def _iso(value: Any) -> Any:
    """把 date/datetime 递归转为 JSON 友好的 ISO 字符串。"""

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _iso(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_iso(v) for v in value]
    return value


class ForestCalendar:
    """可独立部署的森林经营方案年历。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.lock = threading.RLock()
        self.plans: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.restrictions: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.delivered: set[str] = set()
        self._replay()

    # ------------------------------------------------------------------ 重建

    def _reset(self) -> None:
        self.plans.clear()
        self.tasks.clear()
        self.restrictions.clear()
        self.approvals.clear()
        self.delivered.clear()

    def _replay(self) -> None:
        self._reset()
        for event in self.store.events():
            self._apply(event)

    def _append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.store.append(event_type, _iso(payload))
        self._apply(event)
        return event

    def _apply(self, event: dict[str, Any]) -> None:
        etype = event["type"]
        p = event["data"]
        getattr(self, f"_apply_{etype}")(p)

    # -------------------------------------------------------------- 方案版本

    def create_plan(
        self,
        name: str,
        enterprise: str,
        plots: list[dict[str, str]] | None = None,
    ) -> str:
        """建立多年经营方案，并登记适用地块。"""

        with self.lock:
            plan_id = _new_id("plan")
            plot_records = {}
            for plot in plots or []:
                plot_id = _new_id("plot")
                plot_records[plot_id] = {
                    "plot_id": plot_id,
                    "code": plot["code"],
                    "name": plot.get("name", plot["code"]),
                }
            self._append(
                "plan_created",
                {
                    "plan_id": plan_id,
                    "name": name,
                    "enterprise": enterprise,
                    "plots": list(plot_records.values()),
                },
            )
            return plan_id

    def _apply_plan_created(self, p: dict[str, Any]) -> None:
        self.plans[p["plan_id"]] = {
            "plan_id": p["plan_id"],
            "name": p["name"],
            "enterprise": p["enterprise"],
            "plots": {x["plot_id"]: dict(x) for x in p["plots"]},
            "versions": [],
        }

    def add_version(
        self,
        plan_id: str,
        version_no: str,
        title: str,
        approved_at: str | date,
        approver: str,
        note: str = "",
    ) -> str:
        """登记一个已批准的方案版本；更早的版本自动转为 superseded。"""

        with self.lock:
            plan = self._plan(plan_id)
            if any(v["version_no"] == version_no for v in plan["versions"]):
                raise DomainError("version_exists", f"版本号 {version_no} 已存在", 409)
            version_id = _new_id("ver")
            self._append(
                "version_added",
                {
                    "version_id": version_id,
                    "plan_id": plan_id,
                    "version_no": version_no,
                    "title": title,
                    "approved_at": _d(approved_at),
                    "approver": approver,
                    "note": note,
                },
            )
            return version_id

    def _apply_version_added(self, p: dict[str, Any]) -> None:
        plan = self.plans[p["plan_id"]]
        for old in plan["versions"]:
            if old["status"] == "active":
                old["status"] = "superseded"
                old["superseded_by"] = p["version_id"]
        plan["versions"].append(
            {
                "version_id": p["version_id"],
                "version_no": p["version_no"],
                "title": p["title"],
                "approved_at": _d(p["approved_at"]),
                "approver": p["approver"],
                "note": p["note"],
                "status": "active",
                "superseded_by": None,
            }
        )

    # ------------------------------------------------------------------ 任务

    def create_task(
        self,
        plan_id: str,
        version_no: str,
        year: int,
        action: str,
        title: str,
        plot_ids: list[str],
        qty: float,
        unit: str,
        planned_start: str | date,
        planned_end: str | date,
        responsible_org: str,
        responsible_person: str,
        prerequisite_ids: list[str] | None = None,
        created_by: str = "system",
        created_at: str | date | None = None,
    ) -> str:
        """在已批准版本下建立年度任务，可声明前置任务。"""

        with self.lock:
            plan = self._plan(plan_id)
            version = self._version(plan_id, version_no)
            if action not in ACTIONS:
                raise DomainError("bad_action", f"未知动作类型 {action}")
            if not plot_ids or any(pid not in plan["plots"] for pid in plot_ids):
                raise DomainError("bad_plot", "地块不存在或未指定")
            start, end = _d(planned_start), _d(planned_end)
            if start is None or end is None or end < start:
                raise DomainError("bad_window", "计划窗口起止日期不合法")
            if qty <= 0:
                raise DomainError("bad_qty", "任务量必须为正")
            prereqs = prerequisite_ids or []
            for tid in prereqs:
                prereq = self.tasks.get(tid)
                if prereq is None or prereq["plan_id"] != plan_id:
                    raise DomainError("bad_prerequisite", f"前置任务 {tid} 不存在")
            task_id = _new_id("task")
            self._append(
                "task_created",
                {
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "version_id": version["version_id"],
                    "version_no": version_no,
                    "year": year,
                    "action": action,
                    "title": title,
                    "plot_ids": list(plot_ids),
                    "qty": float(qty),
                    "unit": unit,
                    "planned_start": start,
                    "planned_end": end,
                    "prerequisite_ids": list(prereqs),
                    "responsible_org": responsible_org,
                    "responsible_person": responsible_person,
                    "created_by": created_by,
                    "created_at": _d(created_at) or start,
                },
            )
            return task_id

    def _apply_task_created(self, p: dict[str, Any]) -> None:
        self.tasks[p["task_id"]] = {
            "task_id": p["task_id"],
            "plan_id": p["plan_id"],
            "version_id": p["version_id"],
            "version_no": p["version_no"],
            "year": p["year"],
            "action": p["action"],
            "title": p["title"],
            "plot_ids": list(p["plot_ids"]),
            "qty": p["qty"],
            "unit": p["unit"],
            "planned_start": _d(p["planned_start"]),
            "planned_end": _d(p["planned_end"]),
            "prerequisite_ids": list(p["prerequisite_ids"]),
            "responsibles": [
                {
                    "org": p["responsible_org"],
                    "person": p["responsible_person"],
                    "at": _d(p["created_at"]),
                    "note": "初始责任人",
                }
            ],
            "receipts": [],
            "deferrals": [],
            "split_at": None,
            "parent_id": None,
            "children": [],
        }

    # ---------------------------------------------------------- 生态/政策限制

    def issue_restriction(
        self,
        plan_id: str,
        code: str,
        title: str,
        kind: str,
        actions: list[str],
        start: str | date,
        end: str | date,
        source: str,
        plot_ids: list[str] | None = None,
        issued_at: str | date | None = None,
    ) -> str:
        """发布禁作/限制窗口；空 plot_ids 表示适用于方案全部地块。"""

        with self.lock:
            self._plan(plan_id)
            if kind not in RESTRICTION_KINDS:
                raise DomainError("bad_kind", f"未知限制类型 {kind}")
            if any(a not in ACTIONS for a in actions) or not actions:
                raise DomainError("bad_action", "限制动作不合法")
            start_d, end_d = _d(start), _d(end)
            if start_d is None or end_d is None or end_d < start_d:
                raise DomainError("bad_window", "限制窗口起止日期不合法")
            restriction_id = _new_id("rest")
            self._append(
                "restriction_issued",
                {
                    "restriction_id": restriction_id,
                    "plan_id": plan_id,
                    "code": code,
                    "title": title,
                    "kind": kind,
                    "actions": list(actions),
                    "plot_ids": list(plot_ids or []),
                    "start": start_d,
                    "end": end_d,
                    "source": source,
                    "issued_at": _d(issued_at) or date.today(),
                    "lifted_at": None,
                },
            )
            return restriction_id

    def _apply_restriction_issued(self, p: dict[str, Any]) -> None:
        self.restrictions[p["restriction_id"]] = {
            "restriction_id": p["restriction_id"],
            "plan_id": p["plan_id"],
            "code": p["code"],
            "title": p["title"],
            "kind": p["kind"],
            "actions": list(p["actions"]),
            "plot_ids": list(p["plot_ids"]),
            "start": _d(p["start"]),
            "end": _d(p["end"]),
            "source": p["source"],
            "issued_at": _d(p["issued_at"]),
            "lifted_at": None,
        }

    def lift_restriction(
        self, restriction_id: str, lifted_at: str | date, by: str, note: str = ""
    ) -> None:
        """提前解除一条限制（追加记录，不删除原记录）。"""

        with self.lock:
            restriction = self._restriction(restriction_id)
            if restriction["lifted_at"] is not None:
                raise DomainError("already_lifted", "该限制已解除", 409)
            self._append(
                "restriction_lifted",
                {
                    "restriction_id": restriction_id,
                    "lifted_at": _d(lifted_at),
                    "by": by,
                    "note": note,
                },
            )

    def _apply_restriction_lifted(self, p: dict[str, Any]) -> None:
        self.restrictions[p["restriction_id"]]["lifted_at"] = _d(p["lifted_at"])

    # ------------------------------------------------------------------ 延期

    def request_deferral(
        self,
        task_id: str,
        new_start: str | date,
        new_end: str | date,
        reason: str,
        requested_by: str,
        requested_at: str | date | None = None,
    ) -> str:
        """发起延期申请（可跨年度），批准前不改变有效窗口。"""

        with self.lock:
            task = self._task(task_id)
            if task["split_at"] is not None:
                raise DomainError("task_split", "任务已拆分，延期应作用于子任务")
            if any(
                a["status"] == "pending"
                for a in self.approvals.values()
                if a["task_id"] == task_id
            ):
                raise DomainError("approval_pending", "该任务已有待决审批", 409)
            start_d, end_d = _d(new_start), _d(new_end)
            _, current_end = self._effective_window(task)
            if start_d is None or end_d is None or end_d <= current_end:
                raise DomainError("bad_deferral", "延期后的完成日期必须晚于当前有效完成日期")
            approval_id = _new_id("appr")
            self._append(
                "deferral_requested",
                {
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "new_start": start_d,
                    "new_end": end_d,
                    "reason": reason,
                    "requested_by": requested_by,
                    "requested_at": _d(requested_at) or date.today(),
                    "status": "pending",
                },
            )
            return approval_id

    def _apply_deferral_requested(self, p: dict[str, Any]) -> None:
        self.approvals[p["approval_id"]] = {
            "approval_id": p["approval_id"],
            "kind": "deferral",
            "task_id": p["task_id"],
            "new_start": _d(p["new_start"]),
            "new_end": _d(p["new_end"]),
            "reason": p["reason"],
            "requested_by": p["requested_by"],
            "requested_at": _d(p["requested_at"]),
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "note": "",
        }

    def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
        decided_at: str | date | None = None,
        note: str = "",
    ) -> None:
        """批准或驳回待决申请；批准延期时才改写任务有效窗口。"""

        with self.lock:
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise DomainError("approval_not_found", "审批不存在", 404)
            if approval["status"] != "pending":
                raise DomainError("approval_decided", "该审批已有结论", 409)
            if decision not in ("approved", "rejected"):
                raise DomainError("bad_decision", "审批结论只能是 approved/rejected")
            at = _d(decided_at) or date.today()
            self._append(
                "approval_decided",
                {
                    "approval_id": approval_id,
                    "decision": decision,
                    "decided_by": decided_by,
                    "decided_at": at,
                    "note": note,
                },
            )

    def _apply_approval_decided(self, p: dict[str, Any]) -> None:
        approval = self.approvals[p["approval_id"]]
        approval["status"] = p["decision"]
        approval["decided_by"] = p["decided_by"]
        approval["decided_at"] = _d(p["decided_at"])
        approval["note"] = p["note"]
        if p["decision"] == "approved" and approval["kind"] == "deferral":
            task = self.tasks[approval["task_id"]]
            task["deferrals"].append(
                {
                    "approval_id": approval["approval_id"],
                    "new_start": approval["new_start"],
                    "new_end": approval["new_end"],
                    "reason": approval["reason"],
                    "decided_by": approval["decided_by"],
                    "decided_at": approval["decided_at"],
                }
            )

    # ------------------------------------------------------------------ 拆分

    def split_task(
        self,
        task_id: str,
        splits: list[dict[str, Any]],
        requested_by: str,
        split_at: str | date,
    ) -> list[str]:
        """把任务拆成若干子任务；数量必须等于尚未完成的余量。

        原任务保留，已记录回执仍挂在原任务上，子任务承担剩余工作量。
        """

        with self.lock:
            task = self._task(task_id)
            if task["split_at"] is not None:
                raise DomainError("task_split", "任务已拆分，不能重复拆分", 409)
            at = _d(split_at)
            remaining_before = task["qty"] - sum(
                r["qty"] for r in task["receipts"] if r["at"] <= at
            )
            eff_start, eff_end = self._effective_window(task)
            if not splits:
                raise DomainError("bad_split", "至少需要一个拆分子任务")
            total = 0.0
            child_payloads = []
            for item in splits:
                qty = float(item["qty"])
                if qty <= 0:
                    raise DomainError("bad_qty", "子任务量必须为正")
                total += qty
                plot_ids = item.get("plot_ids") or task["plot_ids"]
                plan = self._plan(task["plan_id"])
                if any(pid not in plan["plots"] for pid in plot_ids):
                    raise DomainError("bad_plot", "子任务地块不存在")
                child_payloads.append(
                    {
                        "child_id": _new_id("task"),
                        "qty": qty,
                        "plot_ids": list(plot_ids),
                        "title": item.get("title", task["title"]),
                        "org": item.get("responsible_org", task["responsibles"][-1]["org"]),
                        "person": item.get(
                            "responsible_person", task["responsibles"][-1]["person"]
                        ),
                        "planned_start": _d(item.get("planned_start"))
                        or eff_start,
                        "planned_end": _d(item.get("planned_end")) or eff_end,
                    }
                )
            if abs(total - remaining_before) > 1e-9:
                raise DomainError(
                    "bad_split",
                    f"子任务量合计 {total:g} 与拆分时余量 {remaining_before:g} 不一致",
                )
            self._append(
                "task_split",
                {
                    "task_id": task_id,
                    "split_at": at,
                    "requested_by": requested_by,
                    "children": child_payloads,
                },
            )
            return [c["child_id"] for c in child_payloads]

    def _apply_task_split(self, p: dict[str, Any]) -> None:
        parent = self.tasks[p["task_id"]]
        parent["split_at"] = _d(p["split_at"])
        for c in p["children"]:
            parent["children"].append(c["child_id"])
            self.tasks[c["child_id"]] = {
                "task_id": c["child_id"],
                "plan_id": parent["plan_id"],
                "version_id": parent["version_id"],
                "version_no": parent["version_no"],
                "year": parent["year"],
                "action": parent["action"],
                "title": c["title"],
                "plot_ids": list(c["plot_ids"]),
                "qty": c["qty"],
                "unit": parent["unit"],
                "planned_start": _d(c["planned_start"]),
                "planned_end": _d(c["planned_end"]),
                # 前置门控已在父任务作业前生效，余量延续不再重复门控
                "prerequisite_ids": [],
                "responsibles": [
                    {"org": c["org"], "person": c["person"], "at": _d(p["split_at"]),
                     "note": "拆分自原任务"}
                ],
                "receipts": [],
                "deferrals": [],
                "split_at": None,
                "parent_id": parent["task_id"],
                "children": [],
            }

    # ------------------------------------------------------------------ 回执

    def record_receipt(
        self,
        task_id: str,
        receipt_id: str,
        qty: float,
        at: str | date,
        by: str,
        note: str = "",
    ) -> None:
        """记录完成回执。回执编号去重，超量回执被拒绝，原任务不被改写。"""

        with self.lock:
            task = self._task(task_id)
            if task["split_at"] is not None:
                raise DomainError("task_split", "任务已拆分，回执应记录到子任务")
            if any(r["receipt_id"] == receipt_id for r in task["receipts"]):
                raise DomainError("duplicate_receipt", f"回执 {receipt_id} 已存在", 409)
            if qty <= 0:
                raise DomainError("bad_qty", "回执数量必须为正")
            done = self._done_qty(task)
            if done + qty > task["qty"] + 1e-9:
                raise DomainError(
                    "over_qty",
                    f"回执后完成量 {done + qty:g} 超过任务量 {task['qty']:g}",
                )
            self._append(
                "receipt_recorded",
                {
                    "task_id": task_id,
                    "receipt_id": receipt_id,
                    "qty": float(qty),
                    "at": _d(at),
                    "by": by,
                    "note": note,
                },
            )

    def _apply_receipt_recorded(self, p: dict[str, Any]) -> None:
        self.tasks[p["task_id"]]["receipts"].append(
            {
                "receipt_id": p["receipt_id"],
                "qty": p["qty"],
                "at": _d(p["at"]),
                "by": p["by"],
                "note": p["note"],
            }
        )

    # ------------------------------------------------------------------ 交接

    def handover(
        self,
        task_id: str,
        to_org: str,
        to_person: str,
        by: str,
        at: str | date,
        note: str = "",
    ) -> None:
        """交接责任人；历史责任人与待决审批都保留。"""

        with self.lock:
            task = self._task(task_id)
            current = task["responsibles"][-1]
            if current["person"] == to_person and current["org"] == to_org:
                raise DomainError("same_responsible", "新责任人与当前责任人相同")
            self._append(
                "responsibility_transferred",
                {
                    "task_id": task_id,
                    "from_org": current["org"],
                    "from_person": current["person"],
                    "to_org": to_org,
                    "to_person": to_person,
                    "by": by,
                    "at": _d(at),
                    "note": note,
                },
            )

    def _apply_responsibility_transferred(self, p: dict[str, Any]) -> None:
        self.tasks[p["task_id"]]["responsibles"].append(
            {
                "org": p["to_org"],
                "person": p["to_person"],
                "at": _d(p["at"]),
                "note": p["note"],
            }
        )

    # ------------------------------------------------------------ 提醒发送留痕

    def mark_reminder_delivered(self, key: str, at: str | datetime | None = None) -> None:
        with self.lock:
            if key in self.delivered:
                raise DomainError("already_delivered", "该提醒已发送", 409)
            self._append(
                "reminder_delivered",
                {"key": key, "at": _dt(at) if at else datetime.now()},
            )

    def _apply_reminder_delivered(self, p: dict[str, Any]) -> None:
        self.delivered.add(p["key"])

    # ============================================================== 读模型

    def plan_detail(self, plan_id: str) -> dict[str, Any]:
        with self.lock:
            plan = self._plan(plan_id)
            return {
                "plan_id": plan["plan_id"],
                "name": plan["name"],
                "enterprise": plan["enterprise"],
                "plots": list(plan["plots"].values()),
                "active_version": next(
                    (v["version_no"] for v in plan["versions"] if v["status"] == "active"),
                    None,
                ),
                "versions": list(plan["versions"]),
            }

    def task_detail(self, task_id: str) -> dict[str, Any]:
        with self.lock:
            task = self._task(task_id)
            start, end = self._effective_window(task)
            return {
                "task": self._task_payload(task),
                "original_window": {
                    "start": task["planned_start"],
                    "end": task["planned_end"],
                },
                "effective_window": {"start": start, "end": end},
                "completed_qty": self._done_qty(task),
                "current_responsible": task["responsibles"][-1],
                "deferrals": list(task["deferrals"]),
                "receipts": list(task["receipts"]),
                "responsible_history": list(task["responsibles"]),
                "children": list(task["children"]),
            }

    def plan_history(self, plan_id: str) -> dict[str, Any]:
        """方案历史：版本沿革及每个版本下任务的原始计划，均不可变。"""

        with self.lock:
            plan = self._plan(plan_id)
            versions = []
            for v in plan["versions"]:
                tasks = []
                for t in self._sorted_tasks(plan_id):
                    if t["version_id"] != v["version_id"]:
                        continue
                    tasks.append(
                        {
                            "task_id": t["task_id"],
                            "title": t["title"],
                            "action": t["action"],
                            "action_label": ACTION_LABELS[t["action"]],
                            "year": t["year"],
                            "original_planned_start": t["planned_start"],
                            "original_planned_end": t["planned_end"],
                            "qty": t["qty"],
                            "unit": t["unit"],
                            "parent_id": t["parent_id"],
                        }
                    )
                versions.append(
                    {
                        "version_id": v["version_id"],
                        "version_no": v["version_no"],
                        "title": v["title"],
                        "approved_at": v["approved_at"],
                        "approver": v["approver"],
                        "status": v["status"],
                        "superseded_by": v["superseded_by"],
                        "tasks": tasks,
                    }
                )
            return {"plan_id": plan_id, "name": plan["name"], "versions": versions}

    def pending_approvals(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            result = []
            for a in self.approvals.values():
                if a["status"] != "pending":
                    continue
                if plan_id and self.tasks[a["task_id"]]["plan_id"] != plan_id:
                    continue
                task = self.tasks[a["task_id"]]
                result.append(
                    {
                        "approval_id": a["approval_id"],
                        "kind": a["kind"],
                        "task_id": a["task_id"],
                        "task_title": task["title"],
                        "responsible": task["responsibles"][-1]["person"],
                        "new_start": a["new_start"],
                        "new_end": a["new_end"],
                        "reason": a["reason"],
                        "requested_by": a["requested_by"],
                        "requested_at": a["requested_at"],
                    }
                )
            return result

    def checklist(self, plan_id: str, as_of: str | date) -> dict[str, Any]:
        """以 as_of 时点推演可执行清单与逾期依据。"""

        with self.lock:
            plan = self._plan(plan_id)
            day = _d(as_of)
            version = self._version_at(plan_id, day)
            if version is None:
                raise DomainError("no_version", f"{day} 之前尚无已批准版本")
            active_restrictions = [
                r
                for r in self.restrictions.values()
                if r["plan_id"] == plan_id
                and r["issued_at"] <= day
                and (r["lifted_at"] is None or day < r["lifted_at"])
            ]
            rows = []
            for task in self._sorted_tasks(plan_id):
                # 拆分在该时点生效后，父任务退场、子任务上场
                if task["parent_id"] is None and task["split_at"] is not None and task["split_at"] <= day:
                    continue
                if task["parent_id"] is not None and self.tasks[task["parent_id"]]["split_at"] > day:
                    continue
                row = self._build_row(task, day, version, active_restrictions)
                if row is None:
                    continue
                rows.append(row)
            return {
                "plan_id": plan_id,
                "as_of": day,
                "version_no": version["version_no"],
                "rows": rows,
                "executable_task_ids": [r["task_id"] for r in rows if r["executable"]],
            }

    def reminders(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        """提醒时钟。fire_at 完全由已批准窗口派生，重启后逐字节一致。"""

        with self.lock:
            moment = _dt(now) if now else datetime.now()
            result = []
            for task in self.tasks.values():
                if task["split_at"] is not None:
                    continue
                if self._done_qty(task) >= task["qty"]:
                    continue
                start, end = self._effective_window(task)
                specs = [
                    ("start", datetime.combine(start, time(REMIND_HOUR, 0))
                     - timedelta(days=REMIND_DAYS_BEFORE_START)),
                    ("due", datetime.combine(end, time(REMIND_HOUR, 0))),
                ]
                for kind, fire_at in specs:
                    key = f"{task['task_id']}:{kind}:{start.isoformat()}:{end.isoformat()}"
                    if key in self.delivered:
                        status = "delivered"
                    elif fire_at <= moment:
                        status = "due"
                    else:
                        status = "scheduled"
                    result.append(
                        {
                            "key": key,
                            "task_id": task["task_id"],
                            "task_title": task["title"],
                            "kind": kind,
                            "fire_at": fire_at,
                            "status": status,
                            "responsible": task["responsibles"][-1]["person"],
                        }
                    )
            result.sort(key=lambda r: (r["fire_at"], r["task_id"], r["kind"]))
            return result

    # ============================================================== 推演辅助

    def _build_row(
        self,
        task: dict[str, Any],
        day: date,
        current_version: dict[str, Any],
        restrictions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        deferrals = [d for d in task["deferrals"] if d["decided_at"] <= day]
        start, end = self._effective_window(task, deferrals)
        done = self._done_qty(task, day)
        complete = done + 1e-9 >= task["qty"]

        # 旧版本任务：只有经批准结转（延期跨过换版日）且未完成的才保留在清单
        kind = "current"
        if task["version_id"] != current_version["version_id"]:
            switch_at = current_version["approved_at"]
            crosses = any(d["new_end"] >= switch_at for d in deferrals)
            if complete or not crosses:
                return None
            kind = "carry_over"
        elif start.year != task["year"] or end.year != task["year"]:
            kind = "carry_over"

        blocking = [
            self._restriction_payload(r)
            for r in restrictions
            if task["action"] in r["actions"]
            and (not r["plot_ids"] or set(task["plot_ids"]) & set(r["plot_ids"]))
            and r["start"] <= day <= r["end"]
        ]
        window_restrictions = [
            self._restriction_payload(r)
            for r in restrictions
            if task["action"] in r["actions"]
            and (not r["plot_ids"] or set(task["plot_ids"]) & set(r["plot_ids"]))
            and r["start"] <= end and r["end"] >= start
        ]

        prereqs_met = all(self._is_complete_by(self.tasks[t], day) for t in task["prerequisite_ids"])

        if complete:
            state = "completed"
        elif blocking:
            state = "overdue_suspended" if day > end else "suspended"
        elif not prereqs_met:
            state = "waiting_prerequisite"
        elif day < start:
            state = "not_started"
        elif day <= end:
            state = "executable"
        else:
            state = "overdue"

        receipts = [r for r in task["receipts"] if r["at"] <= day]
        basis = None
        if day > end and not complete:
            basis = {
                "original_due": task["planned_end"],
                "effective_due": end,
                "deferrals": [
                    {
                        "approval_id": d["approval_id"],
                        "new_start": d["new_start"],
                        "new_end": d["new_end"],
                        "decided_at": d["decided_at"],
                    }
                    for d in deferrals
                ],
                "restrictions": window_restrictions,
                "currently_blocking": blocking,
                "completed_qty": done,
                "remaining": max(task["qty"] - done, 0.0),
                "last_receipt": (
                    {
                        "receipt_id": receipts[-1]["receipt_id"],
                        "at": receipts[-1]["at"],
                        "qty": receipts[-1]["qty"],
                    }
                    if receipts
                    else None
                ),
            }

        responsible_at_day = [r for r in task["responsibles"] if r["at"] <= day]
        responsible = (
            max(responsible_at_day, key=lambda r: r["at"])
            if responsible_at_day
            else task["responsibles"][0]
        )
        return {
            "task_id": task["task_id"],
            "title": task["title"],
            "action": task["action"],
            "action_label": ACTION_LABELS[task["action"]],
            "year": task["year"],
            "kind": kind,
            "plan_version_no": task["version_no"],
            "plot_ids": task["plot_ids"],
            "responsible": {"org": responsible["org"], "person": responsible["person"]},
            "original_window": {"start": task["planned_start"], "end": task["planned_end"]},
            "effective_window": {"start": start, "end": end},
            "qty": task["qty"],
            "unit": task["unit"],
            "completed_qty": done,
            "remaining": max(task["qty"] - done, 0.0),
            "prerequisites_met": prereqs_met,
            "state": state,
            "executable": state == "executable",
            "blocking_restrictions": blocking,
            "overdue_basis": basis,
        }

    def _effective_window(
        self, task: dict[str, Any], deferrals: list[dict[str, Any]] | None = None
    ) -> tuple[date, date]:
        start, end = task["planned_start"], task["planned_end"]
        for d in deferrals if deferrals is not None else task["deferrals"]:
            start, end = d["new_start"], d["new_end"]
        return start, end

    def _done_qty(self, task: dict[str, Any], as_of: date | None = None) -> float:
        own = sum(
            r["qty"]
            for r in task["receipts"]
            if as_of is None or r["at"] <= as_of
        )
        children = sum(
            self._done_qty(self.tasks[c], as_of) for c in task["children"]
        )
        return own + children

    def _is_complete_by(self, task: dict[str, Any], day: date) -> bool:
        if task["split_at"] is not None and task["split_at"] <= day:
            return all(
                self._is_complete_by(self.tasks[c], day) for c in task["children"]
            )
        return self._done_qty(task, day) + 1e-9 >= task["qty"]

    def _version_at(self, plan_id: str, day: date) -> dict[str, Any] | None:
        plan = self._plan(plan_id)
        candidates = [v for v in plan["versions"] if v["approved_at"] <= day]
        if not candidates:
            return None
        return max(candidates, key=lambda v: (v["approved_at"], v["version_id"]))

    def _sorted_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return sorted(
            (t for t in self.tasks.values() if t["plan_id"] == plan_id),
            key=lambda t: t["task_id"],
        )

    def _restriction_payload(self, r: dict[str, Any]) -> dict[str, Any]:
        return {
            "restriction_id": r["restriction_id"],
            "code": r["code"],
            "title": r["title"],
            "kind": r["kind"],
            "kind_label": RESTRICTION_KIND_LABELS[r["kind"]],
            "actions": r["actions"],
            "start": r["start"],
            "end": r["end"],
            "source": r["source"],
        }

    def _task_payload(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v
            for k, v in task.items()
            if k not in ("responsibles", "receipts", "deferrals", "children")
        }

    def _plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DomainError("plan_not_found", f"方案 {plan_id} 不存在", 404)
        return plan

    def _version(self, plan_id: str, version_no: str) -> dict[str, Any]:
        plan = self._plan(plan_id)
        for v in plan["versions"]:
            if v["version_no"] == version_no:
                return v
        raise DomainError("version_not_found", f"版本 {version_no} 不存在", 404)

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            raise DomainError("task_not_found", f"任务 {task_id} 不存在", 404)
        return task

    def _restriction(self, restriction_id: str) -> dict[str, Any]:
        r = self.restrictions.get(restriction_id)
        if r is None:
            raise DomainError("restriction_not_found", "限制记录不存在", 404)
        return r
