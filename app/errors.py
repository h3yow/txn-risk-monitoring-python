"""领域异常定义（骨架）。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务拒绝的基类。"""

    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DuplicateSubmissionError(DomainError):
    code = "duplicate_submission"


class ConcurrentConflictError(DomainError):
    code = "concurrent_conflict"


class PayloadMismatchError(DomainError):
    code = "payload_mismatch"


class RuleBatchInvalidError(DomainError):
    code = "rule_batch_invalid"


class IllegalTransitionError(DomainError):
    code = "illegal_transition"


class NotFoundError(DomainError):
    code = "not_found"
