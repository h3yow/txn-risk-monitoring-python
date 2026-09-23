"""FastAPI 路由装配。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import DomainError
from app.models import Account, EventStatus, TransactionInput
from app.schemas import (
    AccountIn,
    PublishRulesIn,
    ReplayIn,
    TransactionIn,
    TransitionIn,
)
from app.services import RiskService
from app.storage import Storage

_HTTP_STATUS = {
    "duplicate_submission": 409,
    "concurrent_conflict": 409,
    "payload_mismatch": 422,
    "rule_batch_invalid": 422,
    "illegal_transition": 409,
    "not_found": 404,
}


def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=_HTTP_STATUS.get(exc.code, 400),
        content={
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


def create_app(service: RiskService | None = None) -> FastAPI:
    from app.logging_config import configure_logging

    configure_logging()
    service = service or RiskService(Storage())
    app = FastAPI(title="交易风险监控与可疑事件处置服务", version="1.0.0")
    app.state.service = service
    app.add_exception_handler(DomainError, _domain_error_handler)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "rule_versions": len(service.list_rule_versions())}

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountIn) -> dict:
        account = service.create_account(
            Account(account_id=body.account_id,
                    country=body.country, risk_level=body.risk_level)
        )
        return {"account_id": account.account_id, "country": account.country,
                "risk_level": account.risk_level}

    @app.post("/transactions", status_code=201)
    def submit_transaction(body: TransactionIn) -> dict:
        txn = TransactionInput(
            txn_id=body.txn_id, account_id=body.account_id, amount=body.amount,
            currency=body.currency, country=body.country, payee=body.payee,
        )
        record = service.submit_transaction(txn, rule_version=body.rule_version)
        return _decision_to_dict(record)

    @app.get("/transactions/{txn_id}")
    def get_transaction(txn_id: str) -> dict:
        return _decision_to_dict(service.get_decision(txn_id))

    @app.post("/transactions/{txn_id}/replay")
    def replay_transaction(txn_id: str, body: ReplayIn) -> dict:
        decision, score, hits = service.replay_transaction(txn_id, body.rule_version)
        stored = service.get_decision(txn_id)
        return {
            "txn_id": txn_id,
            "replay": {"rule_version": body.rule_version,
                       "decision": decision.value, "score": score,
                       "hits": [_hit_to_dict(h) for h in hits]},
            "stored": {"rule_version": stored.rule_version,
                       "decision": stored.decision.value, "score": stored.score},
            "consistent": (stored.rule_version == body.rule_version
                           and stored.decision is decision and stored.score == score),
        }

    @app.post("/rules/versions", status_code=201)
    def publish_rules(body: PublishRulesIn) -> dict:
        raw = [r.model_dump() for r in body.rules]
        version = service.publish_rules(raw, note=body.note)
        return {"version": version.version, "rule_count": len(version.rules),
                "published_at": version.published_at.isoformat(), "note": version.note}

    @app.get("/rules/versions")
    def list_rule_versions() -> dict:
        versions = service.list_rule_versions()
        return {"versions": [
            {"version": v.version, "rule_count": len(v.rules),
             "published_at": v.published_at.isoformat(), "note": v.note}
            for v in versions
        ]}

    @app.get("/events/{event_id}")
    def get_event(event_id: str) -> dict:
        return _event_to_dict(service.get_event(event_id))

    @app.post("/events/{event_id}/transition")
    def transition_event(event_id: str, body: TransitionIn) -> dict:
        try:
            target = EventStatus(body.target)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"error": "illegal_transition",
                         "message": f"未知目标状态 '{body.target}'",
                         "details": {"allowed": [s.value for s in EventStatus]}},
            )
        event = service.transition_event(event_id, target,
                                         operator=body.operator, note=body.note)
        return _event_to_dict(event)

    return app


def _hit_to_dict(hit) -> dict:
    return {"rule_id": hit.rule_id, "rule_type": hit.rule_type.value,
            "rule_version": hit.rule_version, "score": hit.score,
            "reason": hit.reason}


def _decision_to_dict(record) -> dict:
    return {
        "txn_id": record.txn_id, "account_id": record.account_id,
        "amount": record.amount, "currency": record.currency,
        "country": record.country, "payee": record.payee,
        "decision": record.decision.value, "score": record.score,
        "rule_version": record.rule_version,
        "evaluated_at": record.evaluated_at.isoformat(),
        "event_id": record.event_id,
        "hits": [_hit_to_dict(h) for h in record.hits],
    }


def _event_to_dict(event) -> dict:
    return {
        "event_id": event.event_id, "txn_id": event.txn_id,
        "status": event.status.value, "decision": event.decision.value,
        "score": event.score, "rule_version": event.rule_version,
        "hit_rule_ids": event.hit_rule_ids,
        "created_at": event.created_at.isoformat(),
        "history": event.history,
    }


app = create_app()
