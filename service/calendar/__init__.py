"""森林经营方案年历领域包。

事件溯源的年历核心：多年目标拆为有前置关系的年度任务，关联责任主体、
适用地块、生态限制与批准版本；修订只追加事件，保证历史计划、逾期依据
与重启后的提醒/审批状态一致。
"""

from __future__ import annotations

from .clock import FixedClock, SystemClock
from .errors import (
    CalendarError,
    ConflictError,
    NotFoundError,
    PrerequisiteError,
    ValidationError,
    VersionError,
)
from .service import CalendarService
from .store import EventStore

__all__ = [
    "CalendarService",
    "EventStore",
    "SystemClock",
    "FixedClock",
    "CalendarError",
    "ConflictError",
    "NotFoundError",
    "PrerequisiteError",
    "ValidationError",
    "VersionError",
]
