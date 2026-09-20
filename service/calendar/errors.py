"""领域错误类型。"""

from __future__ import annotations


class CalendarError(Exception):
    """年历领域错误基类。"""

    code = "calendar_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(CalendarError):
    """引用的对象不存在。"""

    code = "not_found"


class ConflictError(CalendarError):
    """操作与当前状态冲突，例如重复回执或状态不允许的流转。"""

    code = "conflict"


class ValidationError(CalendarError):
    """输入不满足领域约束。"""

    code = "validation"


class VersionError(CalendarError):
    """试图修改已批准/已归档版本，或基于过期版本操作。"""

    code = "version_locked"


class PrerequisiteError(CalendarError):
    """前置任务未满足。"""

    code = "prerequisite_not_met"
