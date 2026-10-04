"""重复提交与并发提交：结论一致、拒绝原因可区分、无重复命中/计分/告警。"""

from __future__ import annotations

import threading

import pytest

from app.models import (
    ConcurrentSubmissionError,
    DuplicateTransactionError,
)
from app.service import RiskMonitoringService


def _submit(svc: RiskMonitoringService, txn_id: str = "T-DUP-1"):
    return svc.submit_transaction(
        txn_id=txn_id,
        account_id="ACC-1",
        amount=100,
        counterparty="NORMAL-CORP",
    )


def test_sequential_duplicate_is_rejected_with_distinct_reason(svc):
    result = _submit(svc)
    first = result["assessment"]
    assert first.score == 0

    with pytest.raises(DuplicateTransactionError) as exc:
        _submit(svc)
    assert "已完成评估" in str(exc.value)

    # 只有一条结论、零事件、零重复命中
    assert svc.get_assessment("T-DUP-1") is first
    assert svc.events.find_by_txn("T-DUP-1") is None
    assert len(svc.store.assessments) == 1


def test_concurrent_submissions_exactly_one_wins(svc, monkeypatch):
    """同一交易并发提交：恰好一个成功，其余全部按"并发"拒绝。

    纯 Python 临界窗口很短，测试把门控点放在 claim 之后的 _evaluate：
    赢家占位后停在评估窗口内，3 个输家此时进入 -> 全部命中 in_flight 占位。
    """
    import app.engine as engine_mod

    release = threading.Event()
    entered = threading.Event()
    original_evaluate = engine_mod.AssessmentEngine._evaluate

    def blocking_evaluate(self, txn, rv, include_current, **_kw):
        entered.set()
        release.wait(timeout=5)
        return original_evaluate(self, txn, rv, include_current, **_kw)

    monkeypatch.setattr(
        engine_mod.AssessmentEngine, "_evaluate", blocking_evaluate
    )

    concurrent_errors: list[ConcurrentSubmissionError] = []

    def winner():
        _submit(svc)

    def loser():
        try:
            _submit(svc)
        except ConcurrentSubmissionError as e:
            concurrent_errors.append(e)

    t_winner = threading.Thread(target=winner)
    t_winner.start()
    entered.wait(timeout=5)  # 赢家已完成占位，停在评估窗口内

    losers = [threading.Thread(target=loser) for _ in range(3)]
    for t in losers:
        t.start()
    for t in losers:
        t.join(timeout=10)

    # 赢家尚未提交结论，3 个输家必须全部是"并发"而非"重复"
    assert len(concurrent_errors) == 3
    assert all("正在评估中" in str(e) for e in concurrent_errors)
    assert svc.get_assessment("T-DUP-1") is None

    release.set()
    t_winner.join(timeout=10)

    # 赢家落库唯一结论，无重复计分、无重复告警
    assert len(svc.store.assessments) == 1
    assert len(svc.store.events) == 0


def test_concurrent_then_sequential_reasons_are_distinguishable(svc, monkeypatch):
    """并发冲突后再提交：先得到 CONCURRENT 原因，再得到 DUPLICATE 原因。"""
    import app.engine as engine_mod

    release = threading.Event()
    entered = threading.Event()
    original_evaluate = engine_mod.AssessmentEngine._evaluate

    def blocking_evaluate(self, txn, rv, include_current, **_kw):
        entered.set()
        release.wait(timeout=5)
        return original_evaluate(self, txn, rv, include_current, **_kw)

    monkeypatch.setattr(
        engine_mod.AssessmentEngine, "_evaluate", blocking_evaluate
    )

    def holder():
        _submit(svc, "T-RACE-9")

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(timeout=5)

    # 第一个占位仍在进行中 -> 并发提交
    with pytest.raises(ConcurrentSubmissionError) as cexc:
        _submit(svc, "T-RACE-9")
    assert "正在评估中" in str(cexc.value)

    release.set()
    t.join(timeout=10)

    # 评估完成后再提交 -> 重复提交
    with pytest.raises(DuplicateTransactionError) as dexc:
        _submit(svc, "T-RACE-9")
    assert "已完成评估" in str(dexc.value)

    assert len(svc.store.assessments) == 1
