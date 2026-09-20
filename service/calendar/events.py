"""追加式领域事件。

系统采用事件溯源：任何状态变化都表现为一条不可修改、不可删除的事件。
旧计划不会被“改掉”——修订（拆分、延期、换版、禁作期提前等）都产生新事件，
读取模型在回放时据此计算当前状态，并保留完整历史计划与逾期依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .clock import to_iso


class EventType(str, Enum):
    # 方案与版本
    PLAN_REGISTERED = "plan_registered"
    PLAN_VERSION_APPROVED = "plan_version_approved"
    PLAN_VERSION_SUPERSEDED = "plan_version_superseded"
    # 地块、责任主体、生态限制
    PARCEL_REGISTERED = "parcel_registered"
    PARTY_REGISTERED = "party_registered"
    RESPONSIBILITY_ASSIGNED = "responsibility_assigned"   # 责任人交接留痕
    RESTRICTION_DECLARED = "restriction_declared"
    RESTRICTION_REVOKED = "restriction_revoked"
    # 任务
    TASK_SCHEDULED = "task_scheduled"
    TASK_SPLIT = "task_split"
    TASK_DEFERRAL_REQUESTED = "task_deferral_requested"
    TASK_DEFERRAL_DECIDED = "task_deferral_decided"
    TASK_PROGRESS_REPORTED = "task_progress_reported"
    TASK_COMPLETED = "task_completed"
    TASK_CANCELLED = "task_cancelled"
    # 回执（幂等）
    RECEIPT_ISSUED = "receipt_issued"
    # 提醒
    REMINDER_RAISED = "reminder_raised"
    REMINDER_ACKNOWLEDGED = "reminder_acknowledged"


@dataclass(frozen=True)
class Event:
    """一条不可变事件。"""

    seq: int
    event_type: str
    occurred_at: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
        }


def make_event(
    seq: int,
    event_type: EventType,
    moment,
    actor: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> Event:
    """构造事件。"""

    return Event(
        seq=seq,
        event_type=event_type.value,
        occurred_at=to_iso(moment),
        actor=actor,
        payload=payload,
        idempotency_key=idempotency_key,
    )
