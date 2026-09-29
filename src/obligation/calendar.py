"""日历投递网关。

生产环境替换 CalendarGateway 实现（接入企业日历/邮件）。
InMemoryCalendarGateway 为默认实现，自身按幂等键去重，
模拟"日历侧"也不会因服务恢复重放而收到重复邀请。
"""
from __future__ import annotations

from typing import Any, Protocol


class CalendarGateway(Protocol):
    def send(self, idempotency_key: str, recipient: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class InMemoryCalendarGateway:
    """进程内假网关：记录全部已收邀请，拒绝重复幂等键。"""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, Any]] = {}

    def send(self, idempotency_key: str, recipient: str, payload: dict[str, Any]) -> dict[str, Any]:
        if idempotency_key in self._seen:
            # 真实日历网关对同键重放应返回首次结果而非新建邀请
            return {"deduplicated": True, **self._seen[idempotency_key]}
        record = {"recipient": recipient, "payload": payload, "event_ref": f"cal-{idempotency_key[:12]}"}
        self._seen[idempotency_key] = record
        return {"deduplicated": False, **record}

    def all_events(self) -> list[dict[str, Any]]:
        return [{"idempotency_key": k, **v} for k, v in sorted(self._seen.items())]

    def reset(self) -> None:
        self._seen.clear()


class FailingCalendarGateway(InMemoryCalendarGateway):
    """演练用：前 fail_times 次调用抛错，之后恢复，验证恢复重放不重复。"""

    def __init__(self, fail_times: int = 1) -> None:
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    def send(self, idempotency_key: str, recipient: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("日历服务暂时不可用")
        return super().send(idempotency_key, recipient, payload)
