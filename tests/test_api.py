"""HTTP 入口集成测试：不同入口一致结论 + 可区分拒绝原因。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import create_app
from app.services import RiskService
from app.storage import Storage

RULES = [
    {"rule_id": "r-amount", "rule_type": "amount_threshold",
     "params": {"threshold": 1000, "score": 60}},
]
TXN = {"txn_id": "T1", "account_id": "A1", "amount": 2000,
       "currency": "CNY", "country": "CN", "payee": "P1"}


def _client() -> TestClient:
    service = RiskService(Storage())
    service.create_account  # 账户可选，不影响规则评估
    return TestClient(create_app(service))


def test_submit_then_get_consistent_and_duplicate_rejected():
    c = _client()
    c.post("/rules/versions", json={"rules": RULES, "note": "v1"})

    r1 = c.post("/transactions", json=TXN)
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["decision"] == "review"
    assert body1["rule_version"] == 1

    # 查询入口返回与受理入口完全一致的结论
    r_get = c.get("/transactions/T1")
    assert r_get.status_code == 200
    assert r_get.json() == body1

    # 重复提交：409 + 明确原因码
    r2 = c.post("/transactions", json=TXN)
    assert r2.status_code == 409
    assert r2.json()["error"] == "duplicate_submission"

    # 载荷不一致：不同原因码
    tampered = {**TXN, "amount": 1234.0}
    r3 = c.post("/transactions", json=tampered)
    assert r3.status_code == 422
    assert r3.json()["error"] == "payload_mismatch"


def test_concurrent_http_submissions_distinct_conflict_reason(monkeypatch):
    import threading

    service = RiskService(Storage())

    # 确定性撑开并发窗口：赢家在原子预留成功后、开始评估前，
    # 必须等待其余所有请求都完成各自的 reserve_txn，
    # 从而保证落败者全部观察到 in-flight -> concurrent_conflict（而非 duplicate）
    n = 12
    reserve_barrier = threading.Barrier(n)
    call_count = {"n": 0}
    orig_reserve = service.storage.reserve_txn

    def gated_reserve(txn):
        result = orig_reserve(txn)
        call_count["n"] += 1
        if call_count["n"] <= n:
            reserve_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(service.storage, "reserve_txn", gated_reserve)

    c = TestClient(create_app(service))
    c.post("/rules/versions", json={"rules": RULES, "note": "v1"})
    statuses: list[tuple[int, str]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def fire() -> None:
        barrier.wait()
        r = c.post("/transactions", json=TXN)
        with lock:
            statuses.append((r.status_code, r.json().get("error", "")))

    ts = [threading.Thread(target=fire) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    created = [s for s in statuses if s[0] == 201]
    conflicts = [s for s in statuses if s[1] == "concurrent_conflict"]
    assert len(created) == 1
    assert len(conflicts) == n - 1


def test_replay_endpoint_consistency_flag():
    c = _client()
    c.post("/rules/versions", json={"rules": RULES, "note": "v1"})
    c.post("/transactions", json=TXN)
    c.post("/rules/versions", json={
        "rules": [{"rule_id": "r2", "rule_type": "amount_threshold",
                   "params": {"threshold": 99999, "score": 60}}], "note": "v2"})

    same = c.post("/transactions/T1/replay", json={"rule_version": 1}).json()
    assert same["consistent"] is True
    assert same["replay"]["decision"] == "review"

    changed = c.post("/transactions/T1/replay", json={"rule_version": 2}).json()
    assert changed["consistent"] is False
    assert changed["replay"]["decision"] == "allow"
    # 既有结论仍为 v1
    assert changed["stored"] == {"rule_version": 1, "decision": "review", "score": 60}


def test_event_transition_endpoints():
    c = _client()
    c.post("/rules/versions", json={"rules": RULES, "note": "v1"})
    eid = c.post("/transactions", json=TXN).json()["event_id"]

    r = c.post(f"/events/{eid}/transition",
               json={"target": "confirmed", "operator": "alice", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "confirmed"

    r = c.post(f"/events/{eid}/transition",
               json={"target": "false_positive", "operator": "alice"})
    assert r.status_code == 409 and r.json()["error"] == "illegal_transition"

    r = c.post(f"/events/{eid}/transition",
               json={"target": "not_a_status", "operator": "alice"})
    assert r.status_code == 422


def test_invalid_rule_batch_http_rejected_whole():
    c = _client()
    r = c.post("/rules/versions", json={"rules": [
        {"rule_id": "good", "rule_type": "blacklist",
         "params": {"payees": ["X"], "score": 100}},
        {"rule_id": "bad", "rule_type": "amount_threshold",
         "params": {"threshold": "oops", "score": 60}},
    ]})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "rule_batch_invalid"
    assert any("threshold" in e for e in body["details"]["errors"])
    assert c.get("/rules/versions").json()["versions"] == []
