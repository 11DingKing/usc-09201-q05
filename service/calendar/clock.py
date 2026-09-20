"""时钟抽象。

系统重启后提醒时钟不得漂移：所有提醒的判定只依赖事件中持久化的业务时间，
而不是进程内定时器。应用层通过可注入的 Clock 提供“当前时间”，
测试与年度检查可以使用固定时钟模拟禁作期提前、跨年度延期等情形。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """返回当前业务时间。"""

    def now(self) -> datetime:
        """返回带时区信息的当前时间。"""


class SystemClock:
    """生产环境使用的 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """可拨快的固定时钟，供测试模拟重启与时间流逝。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("固定时钟必须使用带时区信息的时间")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, **delta: float) -> None:
        """按 timedelta 参数（days=、hours= 等）拨快时钟。"""

        from datetime import timedelta

        self._moment = self._moment + timedelta(**delta)

    def set(self, moment: datetime) -> None:
        """直接设置时间。"""

        if moment.tzinfo is None:
            raise ValueError("固定时钟必须使用带时区信息的时间")
        self._moment = moment


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 时间，缺失时区时按 UTC 处理。"""

    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def to_iso(moment: datetime) -> str:
    """序列化为 ISO 8601 字符串。"""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def date_key(moment: datetime) -> str:
    """返回 UTC 日期键，用于年度归档。"""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()
