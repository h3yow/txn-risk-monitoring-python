"""重复提交 / 并发提交去重测试。"""
from __future__ import annotations

import threading

import pytest

from app.errors import (
    ConcurrentConflictError,
    DuplicateSubmissionError,
    PayloadMismatchError,
)
from tests.conftest import make_txn

V1_RULES = [
    {"rule_id": "r-amount", "rule_type": "amount_threshold",
     "params": {"threshold": 1000, "score": 60}},
]


def _publish(service):
    service.publish_rules(V1_RULES, note="v1")


def test_duplicate_submission_rejected_with_distinct_code(service_with_account, caplog):
    service = service_with_account
    _publish(service)
    txn = make_txn()
    first = service.submit_transaction(txn)
    assert first.decision.value == "review"

    with pytest.raises(DuplicateSubmissionError) as exc:
        service.submit_transaction(make_txn())
    assert exc.value.code == "duplicate_submission"
    assert exc.value.details["txn_id"] == "T1"

    # 重复提交不得重复计分/告警：结论与事件均只有一份
    stored = service.get_decision("T1")
    assert stored.score == 60
    assert len(stored.hits) == 1
    # 日志打印交易标识、规则版本与判定依据
    log_text = caplog.text
    assert "txn_id=T1" in log_text
    assert "rule_version=v1" in log_text
    assert "r-amount" in log_text
    assert "txn_duplicate_submission" in log_text


def test_duplicate_with_mismatched_payload_is_distinct_from_plain_duplicate(
    service_with_account,
):
    service = service_with_account
    _publish(service)
    service.submit_transaction(make_txn(amount=2000.0))

    # 同 txn_id 但金额不同 -> 载荷不一致，原因码必须可区分
    with pytest.raises(PayloadMismatchError) as exc:
        service.submit_transaction(make_txn(amount=9999.0))
    assert exc.value.code == "payload_mismatch"
    assert exc.value.details["existing"]["amount"] == 2000.0

    # 被拒后原结论不变
    assert service.get_decision("T1").score == 60


def test_concurrent_submissions_exactly_one_wins(service_with_account, monkeypatch):
    service = service_with_account
    _publish(service)

    # 确定性门控：赢家预留成功后，等所有线程都完成 reserve，再继续评估，
    # 保证落败者全部观察到 in-flight -> concurrent_conflict
    thread_count = 16
    reserve_barrier = threading.Barrier(thread_count)
    call_count = {"n": 0}
    orig_reserve = service.storage.reserve_txn

    def gated_reserve(txn):
        result = orig_reserve(txn)
        # 门控仅对第一波并发请求生效；事后的重复提交不得在此等待
        call_count["n"] += 1
        if call_count["n"] <= thread_count:
            reserve_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(service.storage, "reserve_txn", gated_reserve)
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()  # 最大化争抢
        try:
            record = service.submit_transaction(make_txn("TC"))
            with lock:
                outcomes.append(f"ok:{record.event_id}")
        except ConcurrentConflictError as e:
            with lock:
                outcomes.append(f"conflict:{e.code}")
        except DuplicateSubmissionError as e:
            with lock:
                outcomes.append(f"dup:{e.code}")

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [o for o in outcomes if o.startswith("ok:")]
    conflicts = [o for o in outcomes if o.startswith("conflict:")]
    dups = [o for o in outcomes if o.startswith("dup:")]

    assert len(oks) == 1, f"应有且仅有一个成功，实际: {outcomes}"
    assert len(conflicts) + len(dups) == thread_count - 1
    # 并发冲突的拒绝原因码与事后重复提交可区分
    assert all(o == "conflict:concurrent_conflict" for o in conflicts)

    # 争用结束后再提交 -> 一定是 duplicate_submission，且结果一致
    with pytest.raises(DuplicateSubmissionError) as exc:
        service.submit_transaction(make_txn("TC"))
    assert exc.value.code == "duplicate_submission"

    record = service.get_decision("TC")
    assert record.score == 60
    assert len(record.hits) == 1
    assert record.event_id is not None
    assert service.get_event(record.event_id).txn_id == "TC"


def test_concurrent_distinct_transactions_are_independent(service_with_account):
    service = service_with_account
    _publish(service)
    results: dict[str, str] = {}
    results_lock = threading.Lock()
    ids = [f"TI-{i}" for i in range(10)]
    barrier = threading.Barrier(len(ids))

    def worker(txn_id: str) -> None:
        barrier.wait()
        rec = service.submit_transaction(make_txn(txn_id))
        with results_lock:
            results[txn_id] = rec.decision.value

    threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == sorted(ids)
    assert all(v == "review" for v in results.values())
