"""可注入时钟。

调度、立案、核验全部依赖该抽象，生产环境使用 ``SystemClock``，
测试与区间重算使用 ``FixedClock``，保证结果可重复、可回放。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的当前时刻（UTC）。"""

    def today(self) -> date:
        """返回当前业务日期。"""


class SystemClock:
    """生产时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()


class FixedClock:
    """测试/演练用固定时钟，可按天推进。"""

    def __init__(self, moment: datetime | str) -> None:
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._moment

    def today(self) -> date:
        return self._moment.date()

    def advance(self, *, days: int = 0, seconds: int = 0) -> None:
        from datetime import timedelta

        self._moment += timedelta(days=days, seconds=seconds)
