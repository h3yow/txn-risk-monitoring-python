"""HTTP 入口：重复/并发拒绝码可区分；规则非法 422；非法流转 409。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

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

    def blocking(self, txn, rv, pending):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, pending)

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


# -------------------------------------------- 补录与生效时间（固定日期，与当天无关）

V1_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)
V2_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
V1_PERIOD = datetime(2026, 3, 10, tzinfo=timezone.utc)
V2_PERIOD = datetime(2026, 7, 10, tzinfo=timezone.utc)


def _client_with_versioned_rules():
    api_mod.svc = api_mod.RiskMonitoringService()
    client = TestClient(app)
    client.post("/accounts", json={"account_id": "ACC-1"})
    r1 = client.post(
        "/rules/versions",
        json={"note": "v1", "effective_at": V1_AT.isoformat(), "rules": [
            {"rule_id": "AMT-1000", "rule_type": "amount_limit",
             "params": {"limit": 1000, "score": 120}},
        ]},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/rules/versions",
        json={"note": "v2", "effective_at": V2_AT.isoformat(), "rules": [
            {"rule_id": "AMT-5000", "rule_type": "amount_limit",
             "params": {"limit": 5000, "score": 120}},
        ]},
    )
    assert r2.status_code == 201
    return client


def test_http_backfill_uses_effective_version_and_replay_matches():
    client = _client_with_versioned_rules()
    # 1500 发生在 v1 期间 -> reject(v1)
    resp = client.post(
        "/transactions",
        json={"txn_id": "H-1", "account_id": "ACC-1", "amount": 1500,
              "submitted_at": V1_PERIOD.isoformat()},
    )
    assert resp.status_code == 201
    first = resp.json()
    assert first["rule_version"] == 1 and first["decision"] == "reject"

    replay = client.post("/transactions/H-1/replay").json()
    assert (replay["decision"], replay["score"], replay["rule_version"]) == (
        first["decision"], first["score"], first["rule_version"],
    )
    assert [h["rule_id"] for h in replay["hits"]] == ["AMT-1000"]

    # 同金额发生在 v2 期间 -> pass(v2)
    resp2 = client.post(
        "/transactions",
        json={"txn_id": "H-2", "account_id": "ACC-1", "amount": 1500,
              "submitted_at": V2_PERIOD.isoformat()},
    )
    body2 = resp2.json()
    assert body2["rule_version"] == 2 and body2["decision"] == "pass"


def test_http_effective_time_conflict_distinct_from_invalid_ruleset():
    client = _client_with_versioned_rules()
    # 内容合法、生效时间撞车 -> 409 RULE_EFFECTIVE_TIME_CONFLICT
    resp = client.post(
        "/rules/versions",
        json={"effective_at": V2_AT.isoformat(), "rules": [
            {"rule_id": "AMT-9", "rule_type": "amount_limit",
             "params": {"limit": 9, "score": 1}},
        ]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "RULE_EFFECTIVE_TIME_CONFLICT"

    # 内容非法 -> 422 INVALID_RULESET
    resp2 = client.post(
        "/rules/versions",
        json={"effective_at": datetime(2027, 1, 1, tzinfo=timezone.utc).isoformat(),
              "rules": [
            {"rule_id": "BAD", "rule_type": "amount_limit",
             "params": {"limit": -1, "score": 1}},
        ]},
    )
    assert resp2.status_code == 422
    assert resp2.json()["error"]["code"] == "INVALID_RULESET"
    # 两种失败都没产生新版本（版本数量仍为 2，不依赖"当前"生效的是哪个）
    assert len(api_mod.svc.store.rule_versions) == 2


def test_http_backfill_before_earliest_version_rejected():
    client = _client_with_versioned_rules()
    resp = client.post(
        "/transactions",
        json={"txn_id": "OLD", "account_id": "ACC-1", "amount": 10,
              "submitted_at": (V1_AT - timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NO_EFFECTIVE_RULE_VERSION"


def test_http_content_mismatch_code_distinct_from_duplicate():
    client = _client_with_versioned_rules()
    txn = {"txn_id": "DUP", "account_id": "ACC-1", "amount": 100,
           "submitted_at": V1_PERIOD.isoformat()}
    assert client.post("/transactions", json=txn).status_code == 201

    # 同号同内容 -> DUPLICATE_TRANSACTION
    r1 = client.post("/transactions", json=txn)
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "DUPLICATE_TRANSACTION"

    # 同号金额对不上 -> TRANSACTION_CONTENT_MISMATCH
    r2 = client.post("/transactions", json={**txn, "amount": 200})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TRANSACTION_CONTENT_MISMATCH"


def test_http_batch_backfill_success_and_atomic_failure():
    client = _client_with_versioned_rules()
    ok = client.post(
        "/transactions/batch",
        json={"transactions": [
            {"txn_id": "B-1", "account_id": "ACC-1", "amount": 5000,
             "submitted_at": V1_PERIOD.isoformat()},  # reject(v1)+event
            {"txn_id": "B-2", "account_id": "ACC-1", "amount": 10,
             "submitted_at": (V1_PERIOD + timedelta(days=1)).isoformat()},
            {"txn_id": "B-3", "account_id": "ACC-1", "amount": 6000,
             "submitted_at": V2_PERIOD.isoformat()},  # reject(v2)+event
        ]},
    )
    assert ok.status_code == 201
    body = ok.json()
    assert body["count"] == 3
    assert [r["rule_version"] for r in body["results"]] == [1, 1, 2]
    assert [r["event_id"] is not None for r in body["results"]] == [True, False, True]

    # 失败批：含早于最早版本的交易 -> 整批拒绝，B-4/B-5 都不落库
    fail = client.post(
        "/transactions/batch",
        json={"transactions": [
            {"txn_id": "B-4", "account_id": "ACC-1", "amount": 10,
             "submitted_at": V1_PERIOD.isoformat()},
            {"txn_id": "B-5", "account_id": "ACC-1", "amount": 10,
             "submitted_at": (V1_AT - timedelta(days=1)).isoformat()},
        ]},
    )
    assert fail.status_code == 422
    assert fail.json()["error"]["code"] == "NO_EFFECTIVE_RULE_VERSION"

    # 整批失败后再提交 B-4 不应被判重（说明它没落库）
    again = client.post(
        "/transactions",
        json={"txn_id": "B-4", "account_id": "ACC-1", "amount": 10,
              "submitted_at": V1_PERIOD.isoformat()},
    )
    assert again.status_code == 201
