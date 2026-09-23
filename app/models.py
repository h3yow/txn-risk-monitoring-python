"""领域模型：账户、交易、规则版本、判定结论、可疑事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 枚举

class RuleType(str, Enum):
    AMOUNT_LIMIT = "amount_limit"
    VELOCITY = "velocity"
    BLOCKLIST = "blocklist"


class Decision(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    REVIEW = "review"


class EventStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    ESCALATED = "escalated"
    CLOSED = "closed"


# ---------------------------------------------------------------- 异常

class RiskError(Exception):
    """业务异常基类。"""


class DuplicateTransactionError(RiskError):
    """同一笔交易被重复提交（已完成评估）。"""


class ConcurrentSubmissionError(RiskError):
    """同一笔交易正在被并发评估（进行中的冲突）。"""


class RuleValidationError(RiskError):
    """规则集结构或取值非法，整批拒绝。"""


class InvalidTransitionError(RiskError):
    """非法事件状态流转。"""


# ---------------------------------------------------------------- 实体

@dataclass(frozen=True)
class Account:
    account_id: str
    risk_level: str = "normal"
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    account_id: str
    amount: float
    currency: str
    counterparty: str
    submitted_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    rule_type: RuleType
    params: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class RuleVersion:
    version: int
    rules: tuple[Rule, ...]
    created_at: datetime
    note: str = ""


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    rule_type: RuleType
    rule_version: int
    reason: str


@dataclass(frozen=True)
class Assessment:
    txn_id: str
    decision: Decision
    score: int
    rule_version: int
    hits: tuple[RuleHit, ...]
    assessed_at: datetime


@dataclass(frozen=True)
class EventAction:
    action: str
    from_status: EventStatus
    to_status: EventStatus
    operator: str
    note: str
    acted_at: datetime


@dataclass
class SuspiciousEvent:
    event_id: str
    txn_id: str
    status: EventStatus
    assessment_snapshot: Assessment
    transaction_snapshot: Transaction
    history: list[EventAction] = field(default_factory=list)
