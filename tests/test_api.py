"""HTTP 入口：重复/并发拒绝码可区分；规则非法 422；非法流转 409。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

import app.api as api_mod
from app.api import app


def _client_with_seed_rules() -> TestClient:
    # 每个用例使用全新的模块级服务，避免相互污染
    api_mod.svc = api_mod.RiskMonitoringService()
    client = TestClient(app)
    client.post("/accounts", json={"account_id": "ACC-1"})
    resp = client.post(
        "/rules/versions",
        json={"note": "v1", "rules": [
            {"rule_id": "AMT-1000", "rule_type": "amount_limit",
             "params": {"limit": 1000, "score": 120}},
        ]},
    )
    assert resp.status_code == 201
    return client


def test_http_duplicate_vs_concurrent_error_codes_are_distinct():
    client = _client_with_seed_rules()
    txn = {"txn_id": "T-API-1", "account_id": "ACC-1", "amount": 10}

    first = client.post("/transactions", json=txn)
    assert first.status_code == 201

    second = client.post("/transactions", json=txn)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DUPLICATE_TRANSACTION"


def test_http_concurrent_submission_code():
    import app.engine as engine_mod

    client = _client_with_seed_rules()

    release = threading.Event()
    entered = threading.Event()
    original = engine_mod.AssessmentEngine._evaluate

    def blocking(self, txn, rv, include_current):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, include_current)

    engine_mod.AssessmentEngine._evaluate = blocking
    try:
        def holder():
            client.post(
                "/transactions",
                json={"txn_id": "T-API-2", "account_id": "ACC-1", "amount": 10},
            )

        t = threading.Thread(target=holder)
        t.start()
        entered.wait(timeout=5)

        resp = client.post(
            "/transactions",
            json={"txn_id": "T-API-2", "account_id": "ACC-1", "amount": 10},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "CONCURRENT_SUBMISSION"

        release.set()
        t.join(timeout=10)
    finally:
        engine_mod.AssessmentEngine._evaluate = original


def test_http_invalid_ruleset_rejected_entirely():
    client = _client_with_seed_rules()
    resp = client.post(
        "/rules/versions",
        json={"rules": [
            {"rule_id": "BAD", "rule_type": "amount_limit",
             "params": {"limit": "not-a-number", "score": 1}},
        ]},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_RULESET"
    # 未产生新版本
    assert client.get("/health").json()["rule_version"] == 1


def test_http_event_lifecycle_and_invalid_transition():
    client = _client_with_seed_rules()
    resp = client.post(
        "/transactions",
        json={"txn_id": "T-API-3", "account_id": "ACC-1", "amount": 5000},
    )
    event_id = resp.json()["event_id"]

    r1 = client.post(
        f"/events/{event_id}/actions",
        json={"action": "mark_false_positive", "operator": "alice"},
    )
    assert r1.status_code == 200

    r2 = client.post(
        f"/events/{event_id}/actions",
        json={"action": "confirm", "operator": "alice"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "INVALID_TRANSITION"


def test_http_get_event_traceability():
    client = _client_with_seed_rules()
    resp = client.post(
        "/transactions",
        json={"txn_id": "T-API-4", "account_id": "ACC-1", "amount": 5000},
    )
    event_id = resp.json()["event_id"]
    detail = client.get(f"/events/{event_id}").json()
    assert detail["assessment"]["rule_version"] == 1
    assert detail["assessment"]["hits"][0]["rule_id"] == "AMT-1000"
    assert detail["input"]["txn_id"] == "T-API-4"
