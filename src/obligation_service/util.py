"""通用工具：日期、标识与 JSON。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from typing import Any

from .errors import ValidationError


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def stable_id(*parts: Any) -> str:
    """由业务键生成稳定标识，用于插入即幂等的记录（提醒、案件）。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def parse_date(value: str | date, field: str = "日期") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}格式非法：{value!r}，应为 YYYY-MM-DD") from exc


def iso_date(value: date) -> str:
    return value.isoformat()


def iso_ts(value: datetime) -> str:
    return value.astimezone().astimezone().isoformat() if value.tzinfo is None else value.isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def require_fields(payload: dict, fields: list[str]) -> None:
    missing = [name for name in fields if payload.get(name) in (None, "", [])]
    if missing:
        raise ValidationError("缺少必填字段：" + "、".join(missing))
