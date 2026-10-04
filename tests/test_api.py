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

    def blocking(self, txn, rv, include_current, **_kw):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, include_current, **_kw)

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


# ---------------------------------------------------------------- 补录与生效时间

def _client_with_dated_rules() -> TestClient:
    api_mod.svc = api_mod.RiskMonitoringService()
    client = TestClient(app)
    client.post("/accounts", json={"account_id": "ACC-1"})
    resp = client.post(
        "/rules/versions",
        json={"note": "v1", "effective_at": "2026-01-01T00:00:00Z", "rules": [
            {"rule_id": "AMT-1000", "rule_type": "amount_limit",
             "params": {"limit": 1000, "score": 120}},
            {"rule_id": "VEL-2-5M", "rule_type": "velocity",
             "params": {"window_minutes": 5, "max_count": 2, "score": 50}},
        ]},
    )
    assert resp.status_code == 201
    return client


def test_http_backfill_uses_occurrence_time_version_and_matches_replay():
    client = _client_with_dated_rules()
    pub = client.post(
        "/rules/versions",
        json={"effective_at": "2026-06-02T00:00:00Z", "rules": [
            {"rule_id": "AMT-5000", "rule_type": "amount_limit",
             "params": {"limit": 5000, "score": 120}},
        ]},
    )
    assert pub.status_code == 201

    resp = client.post("/transactions/backfill", json={"transactions": [
        {"txn_id": "H-1", "account_id": "ACC-1", "amount": 1500,
         "submitted_at": "2026-06-01T10:00:00Z"},   # v1 窗口 -> reject
        {"txn_id": "H-2", "account_id": "ACC-1", "amount": 1500,
         "submitted_at": "2026-06-03T10:00:00Z"},   # v2 窗口 -> pass
    ]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["count"] == 2
    assert body["results"][0]["rule_version"] == 1
    assert body["results"][0]["decision"] == "reject"
    assert body["results"][0]["event_id"] is not None
    assert body["results"][1]["rule_version"] == 2
    assert body["results"][1]["decision"] == "pass"
    assert body["results"][1]["event_id"] is None

    # 首评与后续重放完全一致
    replay = client.post("/transactions/H-1/replay").json()
    assert replay["rule_version"] == 1
    assert replay["decision"] == "reject"
    assert replay["score"] == body["results"][0]["score"]
    assert [h["rule_id"] for h in replay["hits"]] == \
        [h["rule_id"] for h in body["results"][0]["hits"]]


def test_http_backfill_distinct_conflict_codes():
    client = _client_with_dated_rules()
    item = {"txn_id": "H-DUP", "account_id": "ACC-1", "amount": 10,
            "submitted_at": "2026-06-01T10:00:00Z"}
    assert client.post("/transactions/backfill", json={"transactions": [item]}).status_code == 201

    # 重复
    r = client.post("/transactions/backfill", json={"transactions": [item]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_TRANSACTION"

    # 同号内容不一致
    r = client.post("/transactions/backfill", json={"transactions": [
        {**item, "amount": 11},
    ]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRANSACTION_CONTENT_CONFLICT"


def test_http_backfill_batch_failure_is_all_or_nothing():
    client = _client_with_dated_rules()
    resp = client.post("/transactions/backfill", json={"transactions": [
        {"txn_id": "A-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": "2026-06-01T10:00:00Z"},
        {"txn_id": "A-2", "account_id": "ACC-1", "amount": 10,
         "submitted_at": "2025-12-31T23:59:59Z"},  # 早于最早生效版本
    ]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NO_EFFECTIVE_RULE_VERSION"
    # 整批零落库
    assert client.post("/transactions/A-1/replay").status_code == 422


def test_http_rule_effective_time_conflict_is_distinct_code():
    client = _client_with_dated_rules()
    payload = {"effective_at": "2025-12-31T00:00:00Z", "rules": [
        {"rule_id": "AMT-1", "rule_type": "amount_limit",
         "params": {"limit": 100, "score": 1}},
    ]}
    resp = client.post("/rules/versions", json=payload)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RULE_EFFECTIVE_TIME_CONFLICT"
    # 当前版本仍是 v1
    assert client.get("/health").json()["rule_version"] == 1

    # 撞点也拒绝
    resp = client.post("/rules/versions", json={
        **payload, "effective_at": "2026-01-01T00:00:00Z"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RULE_EFFECTIVE_TIME_CONFLICT"


def test_http_backfill_requires_submitted_at():
    client = _client_with_dated_rules()
    resp = client.post("/transactions/backfill", json={"transactions": [
        {"txn_id": "H-X", "account_id": "ACC-1", "amount": 10},
    ]})
    assert resp.status_code == 422
