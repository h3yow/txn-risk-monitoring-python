"""FastAPI 请求/响应 Pydantic 模型（骨架）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AccountIn(BaseModel):
    account_id: str
    country: str = ""
    risk_level: str = "normal"


class TransactionIn(BaseModel):
    txn_id: str = Field(min_length=1)
    account_id: str
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    country: str
    payee: str
    rule_version: int | None = None


class RuleIn(BaseModel):
    rule_id: str
    rule_type: str
    params: dict
    description: str = ""


class PublishRulesIn(BaseModel):
    rules: list[RuleIn] = Field(min_length=1)
    note: str = ""


class TransitionIn(BaseModel):
    target: str
    operator: str = Field(min_length=1)
    note: str = ""


class ReplayIn(BaseModel):
    rule_version: int
