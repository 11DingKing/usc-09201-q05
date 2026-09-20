"""年历应用服务。

所有写操作只做校验并追加事件，不就地修改计划；读取模型每次从事件存储
重放得到。命令支持 command_key 幂等提交，回执号在领域内唯一，
因此网络重试、重复点击不会产生重复影响。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PrerequisiteError,
    ValidationError,
    VersionError,
)
from .events import EventType
from .model import TERMINAL_STATUSES, CalendarState, Task
from .store import EventStore


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} 必须是 ISO 日期 (YYYY-MM-DD)") from exc


class CalendarService:
    """森林经营方案年历应用服务。"""

    def __init__(self, store: EventStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    # ==================================================================
    # 读取侧：每次从持久化事件重放，保证重启后结果不漂移
    # ==================================================================

    def _state(self) -> CalendarState:
        state = CalendarState()
        for event in self.store.load_all():
            state.apply(event)
        return state

    def board(self, today: str | date | None = None) -> dict[str, Any]:
        day = self._day(today)
        return self._state().executable_board(day)

    def overdue_basis(self, today: str | date | None = None) -> list[dict[str, Any]]:
        day = self._day(today)
        return self._state().overdue_basis(day)

    def history(self) -> dict[str, Any]:
        return self._state().history()

    def task(self, task_id: str) -> dict[str, Any]:
        state = self._state()
        task = state.tasks.get(task_id)
        if task is None:
            raise NotFoundError("任务不存在", details={"task_id": task_id})
        return _task_view(task, state)

    def events(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [
            e.as_dict()
            for e in self.store.load_all()
            if e.seq > after_seq
        ]

    def _day(self, today: str | date | None) -> date:
        if today is None:
            return self.clock.now().date()
        return _parse_date(today, "today")

    def _prior(self, command_key: str | None):
        """重复命令直接回放首次结果，不再走任何校验。"""

        if command_key:
            return self.store.peek_command(command_key)
        return None

    def _commit(self, state: CalendarState, events: list, command_key: str | None):
        stored, first = self.store.append(events, command_key)
        return stored, first

    # ==================================================================
    # 方案与批准版本
    # ==================================================================

    def register_plan(
        self,
        code: str,
        name: str,
        *,
        plan_id: str | None = None,
        actor: str = "system",
        command_key: str | None = None,
    ) -> str:
        if not code or not name:
            raise ValidationError("方案编号与名称不能为空")
        prior = self._prior(command_key)
        if prior is not None:
            return prior[0].payload["plan_id"]
        state = self._state()
        for plan in state.plans.values():
            if plan["code"] == code:
                raise ConflictError("方案编号已存在", details={"code": code})
        plan_id = plan_id or _new_id("plan")
        self._commit(
            state,
            [
                (
                    EventType.PLAN_REGISTERED,
                    self.clock.now(),
                    actor,
                    {"plan_id": plan_id, "code": code, "name": name},
                    None,
                )
            ],
            command_key,
        )
        return plan_id

    def approve_version(
        self,
        plan_id: str,
        version_no: int,
        *,
        remark: str = "",
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> None:
        """批准方案版本。任务只能排到已批准版本上。"""

        state = self._state()
        plan = self._require_plan(state, plan_id)
        if version_no in plan["versions"]:
            raise ConflictError("该版本已批准", details={"version_no": version_no})
        if version_no < 1:
            raise ValidationError("版本号必须从 1 开始")
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.PLAN_VERSION_APPROVED,
                    self.clock.now(),
                    actor,
                    {
                        "plan_id": plan_id,
                        "version_no": version_no,
                        "remark": remark,
                    },
                    None,
                )
            ],
            command_key,
        )

    def supersede_version(
        self,
        plan_id: str,
        new_version_no: int,
        *,
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> None:
        """方案换版：新版本必须已批准；旧版本任务原样保留、不得修改。"""

        state = self._state()
        plan = self._require_plan(state, plan_id)
        old = plan["current_version"]
        if old is None:
            raise ConflictError("方案尚无已批准版本")
        if new_version_no == old:
            raise ConflictError("新版本不能与当前版本相同")
        if new_version_no not in plan["versions"]:
            raise VersionError("新版本必须先批准才能换版")
        if new_version_no < old:
            raise VersionError("不允许回退到更早版本")
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.PLAN_VERSION_SUPERSEDED,
                    self.clock.now(),
                    actor,
                    {
                        "plan_id": plan_id,
                        "old_version_no": old,
                        "new_version_no": new_version_no,
                    },
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 地块、责任主体
    # ==================================================================

    def register_parcel(
        self,
        code: str,
        name: str,
        *,
        parcel_id: str | None = None,
        area: float | None = None,
        actor: str = "system",
        command_key: str | None = None,
    ) -> str:
        if not code:
            raise ValidationError("地块编号不能为空")
        parcel_id = parcel_id or _new_id("parcel")
        prior = self._prior(command_key)
        if prior is not None:
            return prior[0].payload["parcel_id"]
        state = self._state()
        for parcel in state.parcels.values():
            if parcel["code"] == code:
                raise ConflictError("地块编号已存在", details={"code": code})
        payload = {"parcel_id": parcel_id, "code": code, "name": name}
        if area is not None:
            payload["area"] = float(area)
        self._commit(
            state,
            [(EventType.PARCEL_REGISTERED, self.clock.now(), actor, payload, None)],
            command_key,
        )
        return parcel_id

    def register_party(
        self,
        name: str,
        role: str,
        *,
        party_id: str | None = None,
        contact: str = "",
        actor: str = "system",
        command_key: str | None = None,
    ) -> str:
        if not name or not role:
            raise ValidationError("责任主体名称与角色不能为空")
        party_id = party_id or _new_id("party")
        prior = self._prior(command_key)
        if prior is not None:
            return prior[0].payload["party_id"]
        self._commit(
            self._state(),
            [
                (
                    EventType.PARTY_REGISTERED,
                    self.clock.now(),
                    actor,
                    {
                        "party_id": party_id,
                        "name": name,
                        "role": role,
                        "contact": contact,
                    },
                    None,
                )
            ],
            command_key,
        )
        return party_id

    def assign_responsibility(
        self,
        task_id: str,
        party_id: str,
        *,
        reason: str = "",
        actor: str = "system",
        command_key: str | None = None,
    ) -> None:
        """责任人交接：原责任人记录保留，任务挂到新责任主体。"""

        state = self._state()
        task = self._require_open_task(state, task_id)
        if task.plan_version != state.plans[task.plan_id]["current_version"]:
            raise VersionError("任务所属方案版本已换版，旧任务冻结，请在新版本承接")
        if party_id not in state.parties:
            raise NotFoundError("责任主体不存在", details={"party_id": party_id})
        if task.assignee_id == party_id:
            raise ConflictError("任务已由该责任主体负责")
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.RESPONSIBILITY_ASSIGNED,
                    self.clock.now(),
                    actor,
                    {
                        "task_id": task_id,
                        "party_id": party_id,
                        "reason": reason or "handover",
                    },
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 生态限制（临时禁作期，含提前修订）
    # ==================================================================

    def declare_restriction(
        self,
        kind: str,
        reason: str,
        source_ref: str,
        start_date: str | date,
        *,
        end_date: str | date | None = None,
        plan_id: str | None = None,
        parcel_ids: list[str] | None = None,
        task_kinds: list[str] | None = None,
        supersedes: str | None = None,
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> str:
        """登记生态限制/临时禁作期。

        supersedes 用于“禁作期提前”：新通知废止原通知并给出更早的起始日，
        下游影响按事件中两个日期的并集计算，原通知仍保留在历史中。
        """

        if not kind or not reason or not source_ref:
            raise ValidationError("限制类型、原因与政策依据文号不能为空")
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date") if end_date else None
        if end and end < start:
            raise ValidationError("结束日期不能早于起始日期")
        state = self._state()
        if plan_id is not None and plan_id not in state.plans:
            raise NotFoundError("方案不存在", details={"plan_id": plan_id})
        for parcel_id in parcel_ids or []:
            if parcel_id not in state.parcels:
                raise NotFoundError("地块不存在", details={"parcel_id": parcel_id})
        if supersedes is not None:
            prior = state.restrictions.get(supersedes)
            if prior is None:
                raise NotFoundError("被修订的限制不存在", details={"restriction_id": supersedes})
            if prior["status"] != "active":
                raise ConflictError("只能修订仍在生效的限制")
            if start >= prior["start_date"]:
                raise ValidationError("提前修订的新起始日期必须早于原起始日期")
        prior_events = self._prior(command_key)
        if prior_events is not None:
            return prior_events[0].payload["restriction_id"]
        restriction_id = _new_id("restriction")
        self._commit(
            state,
            [
                (
                    EventType.RESTRICTION_DECLARED,
                    self.clock.now(),
                    actor,
                    {
                        "restriction_id": restriction_id,
                        "kind": kind,
                        "reason": reason,
                        "source_ref": source_ref,
                        "plan_id": plan_id,
                        "parcel_ids": list(parcel_ids or []),
                        "task_kinds": list(task_kinds or []),
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat() if end else None,
                        "supersedes": supersedes,
                    },
                    None,
                )
            ],
            command_key,
        )
        return restriction_id

    def revoke_restriction(
        self,
        restriction_id: str,
        *,
        reason: str = "",
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> None:
        """解除限制（政策边界恢复）。已计入历史的顺延不回改。"""

        state = self._state()
        restriction = state.restrictions.get(restriction_id)
        if restriction is None:
            raise NotFoundError("限制不存在", details={"restriction_id": restriction_id})
        if restriction["status"] != "active":
            raise ConflictError("限制已解除")
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.RESTRICTION_REVOKED,
                    self.clock.now(),
                    actor,
                    {"restriction_id": restriction_id, "reason": reason},
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 年度任务
    # ==================================================================

    def schedule_task(
        self,
        plan_id: str,
        plan_year: int,
        code: str,
        kind: str,
        title: str,
        window_start: str | date,
        window_end: str | date,
        *,
        version_no: int | None = None,
        parcel_ids: list[str] | None = None,
        quantity: float = 1.0,
        unit: str = "",
        prerequisites: list[str] | None = None,
        assignee_id: str | None = None,
        source_task_id: str | None = None,
        actor: str = "planner",
        command_key: str | None = None,
    ) -> str:
        """在已批准版本上排定年度任务。

        source_task_id 用于方案换版后的承接：新任务以旧任务为来源，
        旧任务保持不变并在历史中可追溯。
        """

        start = _parse_date(window_start, "window_start")
        end = _parse_date(window_end, "window_end")
        if end < start:
            raise ValidationError("作业窗口结束日期不能早于起始日期")
        if quantity <= 0:
            raise ValidationError("任务量必须大于 0")
        state = self._state()
        plan = self._require_plan(state, plan_id)
        version_no = version_no if version_no is not None else plan["current_version"]
        if version_no is None or version_no not in plan["versions"]:
            raise VersionError("任务只能排到已批准的方案版本上")
        if version_no != plan["current_version"]:
            raise VersionError("只能向当前批准版本排定任务，历史版本不得追加改动")
        prior_events = self._prior(command_key)
        if prior_events is not None:
            return prior_events[0].payload["task_id"]
        if source_task_id is not None:
            source = state.tasks.get(source_task_id)
            if source is None:
                raise NotFoundError("来源任务不存在", details={"task_id": source_task_id})
            if source.plan_id != plan_id:
                raise ValidationError("承接任务必须属于同一方案")
        for parcel_id in parcel_ids or []:
            if parcel_id not in state.parcels:
                raise NotFoundError("地块不存在", details={"parcel_id": parcel_id})
        if assignee_id is not None and assignee_id not in state.parties:
            raise NotFoundError("责任主体不存在", details={"party_id": assignee_id})
        task_id = _new_id("task")
        prereq_ids = list(prerequisites or [])
        self._validate_prerequisites(state, task_id, prereq_ids)
        for existing in state.tasks.values():
            if existing.plan_id == plan_id and existing.code == code:
                raise ConflictError("同方案内任务编号已存在", details={"code": code})
        self._commit(
            state,
            [
                (
                    EventType.TASK_SCHEDULED,
                    self.clock.now(),
                    actor,
                    {
                        "task_id": task_id,
                        "plan_id": plan_id,
                        "plan_version": version_no,
                        "plan_year": plan_year,
                        "code": code,
                        "kind": kind,
                        "title": title,
                        "parcel_ids": list(parcel_ids or []),
                        "quantity": float(quantity),
                        "unit": unit,
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "prerequisites": prereq_ids,
                        "assignee_id": assignee_id,
                        "source_task_id": source_task_id,
                    },
                    None,
                )
            ],
            command_key,
        )
        return task_id

    def split_task(
        self,
        task_id: str,
        splits: list[dict[str, Any]],
        *,
        actor: str = "planner",
        command_key: str | None = None,
    ) -> list[str]:
        """计划拆分：原任务标记为 split 并保留全部原计划信息，生成承接子任务。"""

        if not splits:
            raise ValidationError("至少要有一个拆分明细")
        state = self._state()
        parent = self._require_open_task(state, task_id)
        if not state.is_current_version(parent):
            raise VersionError("任务所属方案版本已换版，旧任务冻结，请在新版本承接")
        if parent.status not in {"scheduled", "in_progress"}:
            raise ConflictError(f"任务当前状态不允许拆分: {parent.status}")
        if parent.pending_deferral:
            raise ConflictError("存在未决延期审批，不能拆分，请先完成审批")
        prior_events = self._prior(command_key)
        if prior_events is not None:
            return [e.payload["task_id"] for e in prior_events]

        total = 0.0
        child_ids = [_new_id("task") for _ in splits]
        child_events = []
        for child_id, item in zip(child_ids, splits):
            qty = float(item.get("quantity", 0))
            if qty <= 0:
                raise ValidationError("拆分子任务量必须大于 0")
            total += qty
            start = _parse_date(item["window_start"], "window_start")
            end = _parse_date(item["window_end"], "window_end")
            if end < start:
                raise ValidationError("子任务作业窗口结束日期不能早于起始日期")
            assignee = item.get("assignee_id", parent.assignee_id)
            if assignee is not None and assignee not in state.parties:
                raise NotFoundError("责任主体不存在", details={"party_id": assignee})
            child_events.append(
                (
                    EventType.TASK_SCHEDULED,
                    self.clock.now(),
                    actor,
                    {
                        "task_id": child_id,
                        "plan_id": parent.plan_id,
                        "plan_version": parent.plan_version,
                        "plan_year": int(item.get("plan_year", parent.plan_year)),
                        "code": str(item["code"]),
                        "kind": item.get("kind", parent.kind),
                        "title": item.get("title", parent.title),
                        "parcel_ids": list(item.get("parcel_ids", parent.parcel_ids)),
                        "quantity": qty,
                        "unit": parent.unit,
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "prerequisites": list(
                            item.get("prerequisites", parent.prerequisites)
                        ),
                        "assignee_id": assignee,
                        "parent_id": parent.task_id,
                    },
                    None,
                )
            )
        if abs(total - parent.remaining) > 1e-6:
            raise ValidationError(
                "拆分子任务量之和必须等于原任务剩余量",
                details={"sum": total, "remaining": parent.remaining},
            )
        codes = [str(item["code"]) for item in splits]
        if len(set(codes)) != len(codes):
            raise ValidationError("拆分子任务编号不能重复")
        plan_codes = {t.code for t in state.tasks.values() if t.plan_id == parent.plan_id}
        duplicated = set(codes) & plan_codes
        if duplicated:
            raise ConflictError("同方案内任务编号已存在", details={"codes": sorted(duplicated)})
        events = list(child_events)
        events.append(
            (
                EventType.TASK_SPLIT,
                self.clock.now(),
                actor,
                {"task_id": task_id, "child_task_ids": child_ids},
                None,
            )
        )
        self._commit(state, events, command_key)
        return child_ids

    # ==================================================================
    # 延期（可跨年度）与审批
    # ==================================================================

    def request_deferral(
        self,
        task_id: str,
        new_window_end: str | date,
        reason: str,
        *,
        actor: str = "planner",
        command_key: str | None = None,
    ) -> str:
        if not reason:
            raise ValidationError("延期必须说明原因")
        new_end = _parse_date(new_window_end, "new_window_end")
        state = self._state()
        task = self._require_open_task(state, task_id)
        if not state.is_current_version(task):
            raise VersionError("任务所属方案版本已换版，旧任务冻结，请在新版本承接")
        if task.pending_deferral:
            raise ConflictError("该任务已有未决延期审批")
        if new_end <= task.effective_window_end:
            raise ValidationError(
                "延期后的窗口末日必须晚于当前有效到期日",
                details={"effective_window_end": task.effective_window_end.isoformat()},
            )
        prior_events = self._prior(command_key)
        if prior_events is not None:
            return prior_events[0].payload["request_id"]
        request_id = _new_id("deferral")
        self._commit(
            state,
            [
                (
                    EventType.TASK_DEFERRAL_REQUESTED,
                    self.clock.now(),
                    actor,
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "new_window_end": new_end.isoformat(),
                        "reason": reason,
                    },
                    None,
                )
            ],
            command_key,
        )
        return request_id

    def decide_deferral(
        self,
        task_id: str,
        request_id: str,
        decision: str,
        *,
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> None:
        """审批延期。批准可跨年度，到期日按批准事件计算，重启不漂移。"""

        if decision not in {"approved", "rejected"}:
            raise ValidationError("审批结论必须是 approved 或 rejected")
        state = self._state()
        task = self._require_open_task(state, task_id)
        if not state.is_current_version(task):
            raise VersionError("任务所属方案版本已换版，旧任务冻结")
        if not task.pending_deferral or task.pending_deferral["request_id"] != request_id:
            raise NotFoundError("未决延期申请不存在", details={"request_id": request_id})
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.TASK_DEFERRAL_DECIDED,
                    self.clock.now(),
                    actor,
                    {
                        "task_id": task_id,
                        "request_id": request_id,
                        "decision": decision,
                    },
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 作业回执（部分完成 / 完工，幂等）
    # ==================================================================

    def report_progress(
        self,
        task_id: str,
        quantity: float,
        receipt_id: str,
        *,
        actor: str = "field-staff",
        command_key: str | None = None,
    ) -> None:
        """上报部分完成量。回执号重复时原样返回，不产生第二次影响。"""

        if quantity <= 0:
            raise ValidationError("完成量必须大于 0")
        if self._prior(command_key) is not None:
            return
        state = self._state()
        task = self._require_open_task(state, task_id)
        self._check_receipt(state, receipt_id, task_id)
        self._check_executable_now(state, task)
        new_total = task.completed_quantity + float(quantity)
        if new_total > task.quantity + 1e-6:
            raise ConflictError(
                "累计完成量不能超过任务量",
                details={"completed": new_total, "quantity": task.quantity},
            )
        if abs(new_total - task.quantity) <= 1e-6:
            raise ConflictError("完成量已达任务量，请直接提交完工回执")
        moment = self.clock.now()
        events = [
            (
                EventType.TASK_PROGRESS_REPORTED,
                moment,
                actor,
                {
                    "task_id": task_id,
                    "receipt_id": receipt_id,
                    "quantity": float(quantity),
                    "completed_quantity": new_total,
                },
                receipt_id,
            )
        ]
        self._commit(state, events, command_key or f"receipt:{receipt_id}")

    def complete_task(
        self,
        task_id: str,
        receipt_id: str,
        *,
        actor: str = "field-staff",
        command_key: str | None = None,
    ) -> None:
        """提交完工回执，任务量全额完成。"""

        if self._prior(command_key) is not None:
            return
        state = self._state()
        task = self._require_open_task(state, task_id)
        self._check_receipt(state, receipt_id, task_id)
        self._check_executable_now(state, task)
        moment = self.clock.now()
        events = [
            (
                EventType.TASK_COMPLETED,
                moment,
                actor,
                {
                    "task_id": task_id,
                    "receipt_id": receipt_id,
                    "completed_quantity": task.quantity,
                },
                receipt_id,
            )
        ]
        self._commit(state, events, command_key or f"receipt:{receipt_id}")

    def cancel_task(
        self,
        task_id: str,
        reason: str,
        *,
        actor: str = "forestry-dept",
        command_key: str | None = None,
    ) -> None:
        if not reason:
            raise ValidationError("取消任务必须说明原因")
        if self._prior(command_key) is not None:
            return
        state = self._state()
        task = self._require_open_task(state, task_id)
        if not state.is_current_version(task):
            raise VersionError("任务所属方案版本已换版，旧任务冻结")
        if task.pending_deferral:
            raise ConflictError("存在未决延期审批，不能取消")
        self._commit(
            state,
            [
                (
                    EventType.TASK_CANCELLED,
                    self.clock.now(),
                    actor,
                    {"task_id": task_id, "reason": reason},
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 提醒扫描：完全依据持久化事件与当日日期，重启可重复
    # ==================================================================

    def scan_reminders(
        self,
        today: str | date | None = None,
        *,
        actor: str = "system",
        command_key: str | None = None,
    ) -> list[str]:
        """扫描应提醒任务（到期/逾期）。每个未结任务只生成一次活动提醒。"""

        day = self._day(today)
        key = command_key or f"scan:{day.isoformat()}"
        prior = self._prior(key)
        if prior is not None:
            return [e.payload["reminder_id"] for e in prior]
        state = self._state()
        board = state.executable_board(day)
        candidate_ids = {
            row["task_id"]: ("due_today" if row["effective_due"] == day.isoformat() else "overdue")
            for row in board["executable"] + board["overdue"]
        }
        raised: list = []
        moment = self.clock.now()
        for task in state.tasks.values():
            if task.status in TERMINAL_STATUSES or task.status == "split":
                continue
            if state.active_reminder_for(task.task_id) is not None:
                continue
            kind = candidate_ids.get(task.task_id)
            if kind is None:
                continue
            due, _ = state.effective_due(task, day)
            reminder_id = _new_id("reminder")
            raised.append(
                (
                    EventType.REMINDER_RAISED,
                    moment,
                    actor,
                    {
                        "reminder_id": reminder_id,
                        "task_id": task.task_id,
                        "kind": kind,
                        "due_at": due.isoformat(),
                    },
                    None,
                )
            )
        if raised:
            stored, _ = self._commit(state, raised, key)
            return [e.payload["reminder_id"] for e in stored]
        return []

    def acknowledge_reminder(
        self,
        reminder_id: str,
        *,
        reason: str = "",
        new_status: str = "acknowledged",
        actor: str = "field-staff",
        command_key: str | None = None,
    ) -> None:
        state = self._state()
        reminder = state.reminders.get(reminder_id)
        if reminder is None:
            raise NotFoundError("提醒不存在", details={"reminder_id": reminder_id})
        if reminder["status"] != "raised":
            raise ConflictError("提醒已处理")
        if new_status not in {"acknowledged", "dismissed"}:
            raise ValidationError("提醒处理状态非法")
        if self._prior(command_key) is not None:
            return
        self._commit(
            state,
            [
                (
                    EventType.REMINDER_ACKNOWLEDGED,
                    self.clock.now(),
                    actor,
                    {
                        "reminder_id": reminder_id,
                        "new_status": new_status,
                        "reason": reason,
                    },
                    None,
                )
            ],
            command_key,
        )

    # ==================================================================
    # 校验辅助
    # ==================================================================

    def _require_plan(self, state: CalendarState, plan_id: str):
        plan = state.plans.get(plan_id)
        if plan is None:
            raise NotFoundError("方案不存在", details={"plan_id": plan_id})
        return plan

    def _require_open_task(self, state: CalendarState, task_id: str) -> Task:
        task = state.tasks.get(task_id)
        if task is None:
            raise NotFoundError("任务不存在", details={"task_id": task_id})
        if task.status in TERMINAL_STATUSES or task.status == "split":
            raise ConflictError(f"任务已终结（{task.status}），其原计划不得修改")
        return task

    def _check_receipt(self, state: CalendarState, receipt_id: str, task_id: str) -> None:
        if not receipt_id:
            raise ValidationError("回执号不能为空")
        owner = state.receipts.get(receipt_id)
        if owner is not None:
            if owner != task_id:
                raise ConflictError("回执号已用于其他任务", details={"receipt_id": receipt_id})
            raise ConflictError("回执已处理，重复提交不产生新影响",
                                details={"receipt_id": receipt_id})

    def _check_executable_now(self, state: CalendarState, task: Task) -> None:
        """作业回执只允许在“可执行”时提交：当前版本、窗口已开始、
        前置满足、当日不在禁作期。"""

        today = self.clock.now().date()
        if not state.is_current_version(task):
            raise VersionError("任务所属版本已换版，请按新版本承接任务执行")
        if today < task.window_start:
            raise ConflictError("尚未进入作业窗口")
        _, toll_days, paused = state.restriction_effect(task, today)
        if paused:
            raise ConflictError(
                "当日处于生态限制禁作期，作业暂停",
                details={"toll_days": toll_days},
            )
        met, unmet = state.prerequisites_met(task)
        if not met:
            raise PrerequisiteError(
                "前置任务未完成", details={"unmet_prerequisites": unmet}
            )

    def _validate_prerequisites(
        self, state: CalendarState, task_id: str, prereq_ids: list[str]
    ) -> None:
        seen = set()
        for prereq_id in prereq_ids:
            if prereq_id == task_id:
                raise ValidationError("任务不能以自身为前置")
            if prereq_id in seen:
                raise ValidationError("前置任务重复")
            seen.add(prereq_id)
            if prereq_id not in state.tasks:
                raise NotFoundError("前置任务不存在", details={"task_id": prereq_id})


def _task_view(task: Task, state: CalendarState) -> dict[str, Any]:
    today = datetime.now().date()
    due, basis = state.effective_due(task, today)
    met, unmet = state.prerequisites_met(task)
    return {
        "task_id": task.task_id,
        "code": task.code,
        "title": task.title,
        "kind": task.kind,
        "plan_id": task.plan_id,
        "plan_version": task.plan_version,
        "version_current": state.is_current_version(task),
        "plan_year": task.plan_year,
        "parcel_ids": task.parcel_ids,
        "quantity": task.quantity,
        "completed_quantity": task.completed_quantity,
        "remaining": task.remaining,
        "unit": task.unit,
        "window_start": task.window_start.isoformat(),
        "window_end": task.window_end.isoformat(),
        "approved_window_end": (
            task.approved_window_end.isoformat() if task.approved_window_end else None
        ),
        "effective_due": due.isoformat(),
        "prerequisites": task.prerequisites,
        "prerequisites_met": met,
        "unmet_prerequisites": unmet,
        "status": task.status,
        "assignee_id": task.assignee_id,
        "assignment_history": task.assignment_history,
        "pending_deferral": task.pending_deferral,
        "deferrals": task.deferrals,
        "receipts": task.receipts,
        "parent_id": task.parent_id,
        "replaced_by": task.replaced_by,
        "overdue_basis": basis if today > due else None,
    }
