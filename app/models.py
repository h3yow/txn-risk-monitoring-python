"""领域模型与枚举定义（骨架）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuleType(str, Enum):
    AMOUNT_THRESHOLD = "amount_threshold"
    BLACKLIST = "blacklist"
    HIGH_RISK_COUNTRY = "high_risk_country"


class RuleStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Decision(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    BLOCK = "block"


class EventStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    ESCALATED = "escalated"
    CLOSED = "closed"


class RejectReason(str, Enum):
    DUPLICATE_SUBMISSION = "duplicate_submission"
    CONCURRENT_CONFLICT = "concurrent_conflict"
    PAYLOAD_MISMATCH = "payload_mismatch"
    RULE_BATCH_INVALID = "rule_batch_invalid"
    ILLEGAL_TRANSITION = "illegal_transition"
    NOT_FOUND = "not_found"
    RULE_VERSION_NOT_FOUND = "rule_version_not_found"
    EVENT_NOT_FOUND = "event_not_found"


@dataclass
class Account:
    account_id: str
    country: str = ""
    risk_level: str = "normal"
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Rule:
    rule_id: str
    rule_type: RuleType
    params: dict[str, Any]
    status: RuleStatus = RuleStatus.ACTIVE
    description: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RuleSetVersion:
    version: int
    rules: list[Rule]
    published_at: datetime = field(default_factory=utcnow)
    note: str = ""


@dataclass
class RuleHit:
    rule_id: str
    rule_type: RuleType
    rule_version: int
    score: int
    reason: str


@dataclass
class TransactionInput:
    txn_id: str
    account_id: str
    amount: float
    currency: str
    country: str
    payee: str
    submitted_at: datetime = field(default_factory=utcnow)


@dataclass
class DecisionRecord:
    txn_id: str
    account_id: str
    amount: float
    currency: str
    country: str
    payee: str
    decision: Decision
    score: int
    hits: list[RuleHit]
    rule_version: int
    evaluated_at: datetime
    event_id: str | None = None


@dataclass
class Event:
    event_id: str
    txn_id: str
    status: EventStatus
    rule_version: int
    hit_rule_ids: list[str]
    score: int
    decision: Decision
    created_at: datetime = field(default_factory=utcnow)
    history: list[dict[str, Any]] = field(default_factory=list)
