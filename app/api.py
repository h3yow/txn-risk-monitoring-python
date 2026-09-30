"""FastAPI HTTP 入口。

异常响应通过 ``error.code`` 区分拒绝原因：
- ``DUPLICATE_TRANSACTION``：交易已评估过的重复提交 / 批内同号（HTTP 409）
- ``TRANSACTION_CONTENT_MISMATCH``：同交易号但关键内容对不上（HTTP 409）
- ``CONCURRENT_SUBMISSION``：同一交易进行中的并发提交（HTTP 409）
- ``INVALID_TRANSITION``：非法事件状态流转（HTTP 409）
- ``RULE_EFFECTIVE_TIME_CONFLICT``：规则内容合法但生效时间不合法（HTTP 409）
- ``NO_EFFECTIVE_RULE_VERSION``：交易发生时刻早于最早生效版本（HTTP 422）
- ``INVALID_RULESET``：规则集/批次内容非法，整批拒绝（HTTP 422）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.models import (
    ConcurrentSubmissionError,
    DuplicateTransactionError,
    InvalidTransitionError,
    NoEffectiveRuleVersionError,
    RuleEffectiveTimeConflictError,
    RuleValidationError,
    TransactionContentMismatchError,
)
from app.service import RiskMonitoringService

app = FastAPI(title="txn-risk-monitoring", version="0.1.0")
svc = RiskMonitoringService()


# ---------------------------------------------------------------- 请求/响应模型

class AccountIn(BaseModel):
    account_id: str
    risk_level: str = "normal"


class RulesIn(BaseModel):
    rules: list[dict[str, Any]]
    note: str = ""
    # 生效时间；缺省为发布当下。必须严格晚于已有版本的生效时间。
    effective_at: datetime | None = None


class TransactionIn(BaseModel):
    txn_id: str
    account_id: str
    amount: float = Field(gt=0)
    currency: str = "CNY"
    counterparty: str = ""
    # 缺省为实时交易；补录时传交易实际发生时刻，按该时刻生效版本判定
    submitted_at: datetime | None = None


class TransactionsBatchIn(BaseModel):
    transactions: list[TransactionIn]


class ReplayIn(BaseModel):
    version: int | None = None


class EventActionIn(BaseModel):
    action: str
    operator: str
    note: str = ""


# ---------------------------------------------------------------- 异常处理

def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(DuplicateTransactionError)
def _dup_handler(_request, exc: DuplicateTransactionError) -> JSONResponse:
    return _error("DUPLICATE_TRANSACTION", str(exc), 409)


@app.exception_handler(TransactionContentMismatchError)
def _mismatch_handler(_request, exc: TransactionContentMismatchError) -> JSONResponse:
    return _error("TRANSACTION_CONTENT_MISMATCH", str(exc), 409)


@app.exception_handler(ConcurrentSubmissionError)
def _concurrent_handler(_request, exc: ConcurrentSubmissionError) -> JSONResponse:
    return _error("CONCURRENT_SUBMISSION", str(exc), 409)


@app.exception_handler(InvalidTransitionError)
def _transition_handler(_request, exc: InvalidTransitionError) -> JSONResponse:
    return _error("INVALID_TRANSITION", str(exc), 409)


@app.exception_handler(RuleEffectiveTimeConflictError)
def _effective_time_handler(
    _request, exc: RuleEffectiveTimeConflictError
) -> JSONResponse:
    # 与规则内容非法（422 INVALID_RULESET）区分：内容合法但时间线不合法
    return _error("RULE_EFFECTIVE_TIME_CONFLICT", str(exc), 409)


@app.exception_handler(NoEffectiveRuleVersionError)
def _no_version_handler(_request, exc: NoEffectiveRuleVersionError) -> JSONResponse:
    return _error("NO_EFFECTIVE_RULE_VERSION", str(exc), 422)


@app.exception_handler(RuleValidationError)
def _rules_handler(_request, exc: RuleValidationError) -> JSONResponse:
    return _error("INVALID_RULESET", str(exc), 422)


# ---------------------------------------------------------------- 路由

@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "txn-risk-monitoring"}


@app.get("/health")
def health() -> dict[str, Any]:
    return {"rule_version": svc.current_rule_version()}


@app.post("/accounts", status_code=201)
def create_account(body: AccountIn) -> dict[str, str]:
    svc.create_account(body.account_id, body.risk_level)
    return {"account_id": body.account_id}


@app.post("/rules/versions", status_code=201)
def publish_rules(body: RulesIn) -> dict[str, Any]:
    version = svc.publish_rules(
        body.rules, note=body.note, effective_at=body.effective_at
    )
    return {"version": version}


def _assessment_payload(a, event=None) -> dict[str, Any]:
    return {
        "txn_id": a.txn_id,
        "decision": a.decision.value,
        "score": a.score,
        "rule_version": a.rule_version,
        "hits": [
            {
                "rule_id": h.rule_id,
                "rule_type": h.rule_type.value,
                "rule_version": h.rule_version,
                "reason": h.reason,
            }
            for h in a.hits
        ],
        "event_id": event.event_id if event else None,
    }


@app.post("/transactions", status_code=201)
def submit_transaction(body: TransactionIn) -> dict[str, Any]:
    result = svc.submit_transaction(
        txn_id=body.txn_id,
        account_id=body.account_id,
        amount=body.amount,
        currency=body.currency,
        counterparty=body.counterparty,
        submitted_at=body.submitted_at,
    )
    return _assessment_payload(result["assessment"], result["event"])


@app.post("/transactions/batch", status_code=201)
def submit_transactions_batch(body: TransactionsBatchIn) -> dict[str, Any]:
    """整批补录：整批原子，任一笔失败则全批拒绝（错误码与单笔一致）。"""
    payload = [t.model_dump() for t in body.transactions]
    results = svc.submit_transactions_batch(payload)
    return {
        "count": len(results),
        "results": [
            _assessment_payload(r["assessment"], r["event"]) for r in results
        ],
    }


@app.post("/transactions/{txn_id}/replay")
def replay_transaction(txn_id: str, body: ReplayIn | None = None) -> dict[str, Any]:
    version = body.version if body else None
    a = svc.replay_transaction(txn_id, version=version)
    return {
        "txn_id": a.txn_id,
        "decision": a.decision.value,
        "score": a.score,
        "rule_version": a.rule_version,
        "hits": [
            {
                "rule_id": h.rule_id,
                "rule_type": h.rule_type.value,
                "rule_version": h.rule_version,
                "reason": h.reason,
            }
            for h in a.hits
        ],
    }


@app.post("/events/{event_id}/actions", status_code=200)
def act_on_event(event_id: str, body: EventActionIn) -> dict[str, Any]:
    event = svc.handle_event(event_id, body.action, body.operator, body.note)
    return {
        "event_id": event.event_id,
        "txn_id": event.txn_id,
        "status": event.status.value,
        "history": [
            {
                "action": h.action,
                "operator": h.operator,
                "note": h.note,
                "acted_at": h.acted_at.isoformat(),
            }
            for h in event.history
        ],
    }


@app.get("/events/{event_id}")
def get_event(event_id: str) -> dict[str, Any]:
    event = svc.get_event(event_id)
    if event is None:
        return _error("EVENT_NOT_FOUND", f"事件 {event_id} 不存在", 404)
    a = event.assessment_snapshot
    t = event.transaction_snapshot
    return {
        "event_id": event.event_id,
        "txn_id": event.txn_id,
        "status": event.status.value,
        "input": {
            "txn_id": t.txn_id,
            "account_id": t.account_id,
            "amount": t.amount,
            "currency": t.currency,
            "counterparty": t.counterparty,
            "submitted_at": t.submitted_at.isoformat(),
        },
        "assessment": {
            "decision": a.decision.value,
            "score": a.score,
            "rule_version": a.rule_version,
            "hits": [
                {
                    "rule_id": h.rule_id,
                    "rule_type": h.rule_type.value,
                    "rule_version": h.rule_version,
                    "reason": h.reason,
                }
                for h in a.hits
            ],
        },
        "history": [
            {
                "action": h.action,
                "operator": h.operator,
                "note": h.note,
                "acted_at": h.acted_at.isoformat(),
            }
            for h in event.history
        ],
    }
