"""业务错误与授权错误。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期的领域规则违反。"""


class AuthorizationError(DomainError):
    """操作者角色或独立性不满足要求。"""


class NotFoundError(DomainError):
    """协议或义务不存在。"""


class ConflictError(DomainError):
    """状态不允许该操作（如重复核验、对已结清义务再豁免）。"""


class ValidationError(DomainError):
    """登记内容不合法。"""
