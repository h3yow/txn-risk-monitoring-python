"""领域模型：账户、交易、规则版本、判定结论、可疑事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """把无时区信息的时间按 UTC 处理；有时区信息的原样返回。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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


class TransactionContentMismatchError(RiskError):
    """同一交易号再次提交，但关键内容与首次提交对不上。"""


class RuleValidationError(RiskError):
    """规则集结构或取值非法，整批拒绝。"""


class RuleEffectiveTimeConflictError(RiskError):
    """新版本生效时间不晚于已有版本（或与已有版本同一时刻），整批拒绝。

    与规则内容非法（:class:`RuleValidationError`）区分：本异常表示
    规则内容本身合法，但时间线不合法，发布将会让历史结论说不清。
    """


class NoEffectiveRuleVersionError(RiskError):
    """交易发生时刻早于系统中最早生效的规则版本，拒绝硬套最新规则。"""


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
    effective_at: datetime
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
