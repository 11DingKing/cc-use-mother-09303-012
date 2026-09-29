"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期的业务错误基类。"""

    code = "domain_error"
    status = 400

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        if status is not None:
            self.status = status


class ValidationError(DomainError):
    """输入不满足领域约束。"""

    code = "validation_error"
    status = 422


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    code = "not_found"
    status = 404


class AuthorizationError(DomainError):
    """操作者角色或独立性不满足要求。"""

    code = "forbidden"
    status = 403


class ConflictError(DomainError):
    """操作与当前状态冲突（重复登记、重复裁决等）。"""

    code = "conflict"
    status = 409
