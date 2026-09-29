"""可注入时钟。

调度、逾期判定与核验全部只依赖该抽象，生产环境使用 SystemClock，
测试与管理演练使用 FixedClock / SettableClock，保证"可控制的时间"。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实墙钟，统一返回 UTC 感知时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定或可手动推进的时钟，用于确定性测试与逾期演练。"""

    def __init__(self, at: datetime | str) -> None:
        from .models import parse_dt

        self._at = parse_dt(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime | str) -> None:
        from .models import parse_dt

        self._at = parse_dt(at)

    def advance(self, **kwargs) -> datetime:
        self._at = self._at + timedelta(**kwargs)
        return self._at


class SettableClock:
    """服务运行期可经受控接口（如 X-Now）拨弄的时钟。"""

    def __init__(self, at: datetime | None = None) -> None:
        self._at = at or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime | str) -> None:
        from .models import parse_dt

        self._at = parse_dt(at)
