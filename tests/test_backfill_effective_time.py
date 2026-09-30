"""补录历史交易与规则生效时间语义。

所有用例都使用固定时间常量（2026 年内），不依赖跑用例当天的日期，
因此在任意一天执行，期望值都一致。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ConcurrentSubmissionError,
    Decision,
    DuplicateTransactionError,
    NoEffectiveRuleVersionError,
    RuleEffectiveTimeConflictError,
    RuleValidationError,
    TransactionContentMismatchError,
)

# 固定的规则时间线：v1 从久远过去生效（保证实时交易在任意一天都有版本可用），
# v2 固定 2026-06-01；交易分别发生在 2026-03（v1 期间）与 2026-07（v2 期间）
V1_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)
V2_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)
V1_PERIOD = datetime(2026, 3, 10, tzinfo=timezone.utc)
V2_PERIOD = datetime(2026, 7, 10, tzinfo=timezone.utc)

V1_AMOUNT_RULE = {
    "rule_id": "AMT-1000",
    "rule_type": "amount_limit",
    "params": {"limit": 1000, "score": 120},
}
V2_AMOUNT_RULE = {
    "rule_id": "AMT-5000",
    "rule_type": "amount_limit",
    "params": {"limit": 5000, "score": 120},
}
VELOCITY_RULE = {
    "rule_id": "VEL-2-5M",
    "rule_type": "velocity",
    "params": {"window_minutes": 5, "max_count": 2, "score": 50},
}


@pytest.fixture
def timeline_svc():
    """v1: 限额 1000；v2(2026-06-01 生效): 限额 5000。"""
    from app.service import RiskMonitoringService

    s = RiskMonitoringService()
    s.create_account("ACC-1")
    s.create_account("ACC-2")
    s.publish_rules([V1_AMOUNT_RULE], note="v1", effective_at=V1_AT)
    s.publish_rules([V2_AMOUNT_RULE], note="v2", effective_at=V2_AT)
    return s


def _assessment_tuple(a):
    return (
        a.decision,
        a.score,
        a.rule_version,
        tuple((h.rule_id, h.rule_type, h.rule_version, h.reason) for h in a.hits),
    )


# -------------------------------------------- 1. 补录按发生时刻判定，首评==重放


def test_backfilled_txn_uses_rules_effective_at_occurrence_time(timeline_svc):
    # 1500 在 v1（limit=1000）下 reject；同金额若发生在 v2 期间则 pass
    r_old = timeline_svc.submit_transaction(
        txn_id="T-OLD", account_id="ACC-1", amount=1500, submitted_at=V1_PERIOD
    )
    r_new = timeline_svc.submit_transaction(
        txn_id="T-NEW", account_id="ACC-1", amount=1500, submitted_at=V2_PERIOD
    )
    assert r_old["assessment"].decision is Decision.REJECT
    assert r_old["assessment"].rule_version == 1
    assert r_new["assessment"].decision is Decision.PASS
    assert r_new["assessment"].rule_version == 2


def test_first_assessment_equals_later_replay_for_backfilled_txn(timeline_svc):
    result = timeline_svc.submit_transaction(
        txn_id="T-BF-1", account_id="ACC-1", amount=5000, submitted_at=V1_PERIOD
    )
    first = result["assessment"]

    # 之后单独重放：规则版本、命中规则、分值、决策完全一致
    replayed = timeline_svc.replay_transaction("T-BF-1")
    assert _assessment_tuple(replayed) == _assessment_tuple(first)
    assert replayed.rule_version == 1
    assert [h.rule_id for h in replayed.hits] == ["AMT-1000"]
    assert (replayed.decision, replayed.score) == (Decision.REJECT, 120)


def test_backfill_does_not_change_existing_conclusions(timeline_svc):
    # 先有一笔 v2 期间的实时（按发生时刻）交易
    existing = timeline_svc.submit_transaction(
        txn_id="T-EXIST", account_id="ACC-1", amount=6000, submitted_at=V2_PERIOD
    )["assessment"]
    # 再补录一批更早的 v1 期间交易
    timeline_svc.submit_transactions_batch([
        {"txn_id": "T-BF-A", "account_id": "ACC-1", "amount": 2000,
         "submitted_at": V1_PERIOD + timedelta(days=1)},
        {"txn_id": "T-BF-B", "account_id": "ACC-1", "amount": 2000,
         "submitted_at": V1_PERIOD + timedelta(days=2)},
    ])
    # 既有结论与事件不被补录改动
    assert timeline_svc.get_assessment("T-EXIST") == existing
    assert timeline_svc.get_assessment("T-EXIST").rule_version == 2


def test_normal_realtime_submission_behavior_unchanged():
    """不带发生时间即实时交易：按当前生效版本判定；幂等重试仍是重复提交。

    使用生效时间固定在久远过去的服务，保证与跑用例当天的日期无关。
    """
    from app.service import RiskMonitoringService

    s = RiskMonitoringService()
    s.create_account("ACC-1")
    s.publish_rules([V1_AMOUNT_RULE], note="v1", effective_at=V1_AT)

    result = s.submit_transaction(txn_id="T-LIVE", account_id="ACC-1", amount=1500)
    assert result["assessment"].rule_version == 1
    assert result["assessment"].decision is Decision.REJECT

    # 幂等重试（不带发生时间，两次当前时间不同）仍是重复提交而非内容不一致
    with pytest.raises(DuplicateTransactionError):
        s.submit_transaction(txn_id="T-LIVE", account_id="ACC-1", amount=1500)


# -------------------------------------------- 2. 规则生效时间语义


def test_effective_time_equal_or_earlier_is_rejected_as_time_conflict(timeline_svc):
    # 与已有版本同一时刻
    with pytest.raises(RuleEffectiveTimeConflictError):
        timeline_svc.publish_rules(
            [V2_AMOUNT_RULE], effective_at=V2_AT
        )
    # 比当前已生效版本更早
    with pytest.raises(RuleEffectiveTimeConflictError):
        timeline_svc.publish_rules(
            [V2_AMOUNT_RULE], effective_at=V2_AT - timedelta(seconds=1)
        )
    # 时间冲突整批拒绝：不产生新版本，当前仍为 v2
    assert len(timeline_svc.store.rule_versions) == 2


def test_time_conflict_is_distinguishable_from_invalid_content(timeline_svc):
    # 内容非法 -> RuleValidationError
    with pytest.raises(RuleValidationError) as cexc:
        timeline_svc.publish_rules(
            [{"rule_id": "BAD", "rule_type": "amount_limit",
              "params": {"limit": -1, "score": 1}}],
            effective_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
    assert not isinstance(cexc.value, RuleEffectiveTimeConflictError)

    # 内容合法、时间非法 -> RuleEffectiveTimeConflictError，而不是内容校验错
    with pytest.raises(RuleEffectiveTimeConflictError) as texc:
        timeline_svc.publish_rules([V2_AMOUNT_RULE], effective_at=V1_AT)
    assert not isinstance(texc.value, RuleValidationError)

    # 两种失败都不产生新版本
    assert len(timeline_svc.store.rule_versions) == 2


def test_backfill_before_earliest_effective_version_is_rejected(timeline_svc):
    with pytest.raises(NoEffectiveRuleVersionError):
        timeline_svc.submit_transaction(
            txn_id="T-ANCIENT",
            account_id="ACC-1",
            amount=10,
            submitted_at=V1_AT - timedelta(seconds=1),
        )
    # 不落库、不产生结论/事件
    assert timeline_svc.get_assessment("T-ANCIENT") is None
    assert timeline_svc.store.get_transaction("T-ANCIENT") is None


def test_backfill_exactly_at_earliest_effective_time_uses_v1(timeline_svc):
    # 边界：发生时刻恰好等于最早版本生效时间 -> v1 生效（effective_at <= at）
    result = timeline_svc.submit_transaction(
        txn_id="T-AT-EDGE", account_id="ACC-1", amount=5000, submitted_at=V1_AT
    )
    assert result["assessment"].rule_version == 1


# -------------------------------------------- 3. 整批补录、并发与拒绝原因


def test_batch_assessment_matches_per_txn_replay_including_velocity():
    """整批首次结论 == 之后逐笔单独重放（velocity 跨批内交易计数）。"""
    from app.service import RiskMonitoringService

    s = RiskMonitoringService()
    s.create_account("ACC-1")
    s.publish_rules([VELOCITY_RULE], note="v1", effective_at=V1_AT)

    base = V1_PERIOD
    batch = [
        {"txn_id": f"VEL-{i}", "account_id": "ACC-1", "amount": 1,
         "submitted_at": base + timedelta(minutes=i)}
        for i in range(4)
    ]
    results = s.submit_transactions_batch(batch)
    first = {
        r["assessment"].txn_id: _assessment_tuple(r["assessment"]) for r in results
    }

    # 逐笔单独重放：版本/分值/决策/命中完全一致
    for i in range(4):
        txn_id = f"VEL-{i}"
        replayed = s.replay_transaction(txn_id)
        assert _assessment_tuple(replayed) == first[txn_id]

    # max_count=2：前两笔 pass，后两笔 review 并各建一个事件
    assert {tid: first[tid][0] for tid in first} == {
        "VEL-0": Decision.PASS,
        "VEL-1": Decision.PASS,
        "VEL-2": Decision.REVIEW,
        "VEL-3": Decision.REVIEW,
    }


def test_batch_is_atomic_when_any_item_is_invalid(timeline_svc):
    txn_before = len(timeline_svc.store.transactions)
    evt_before = len(timeline_svc.store.events)

    # 第二笔早于最早生效版本 -> 整批拒绝
    with pytest.raises(NoEffectiveRuleVersionError):
        timeline_svc.submit_transactions_batch([
            {"txn_id": "B-OK", "account_id": "ACC-1", "amount": 5000,
             "submitted_at": V1_PERIOD},
            {"txn_id": "B-BAD", "account_id": "ACC-1", "amount": 10,
             "submitted_at": V1_AT - timedelta(days=1)},
        ])

    # 干净地一笔不落：无半截结论、无凭空事件
    assert len(timeline_svc.store.transactions) == txn_before
    assert len(timeline_svc.store.events) == evt_before
    assert timeline_svc.get_assessment("B-OK") is None


def test_batch_duplicate_id_inside_batch_is_rejected_entirely(timeline_svc):
    txn_before = len(timeline_svc.store.transactions)
    with pytest.raises(DuplicateTransactionError):
        timeline_svc.submit_transactions_batch([
            {"txn_id": "SAME", "account_id": "ACC-1", "amount": 10,
             "submitted_at": V1_PERIOD},
            {"txn_id": "SAME", "account_id": "ACC-1", "amount": 10,
             "submitted_at": V1_PERIOD + timedelta(days=1)},
        ])
    assert len(timeline_svc.store.transactions) == txn_before


def test_batch_conflict_reasons_are_distinguishable(timeline_svc):
    # 先成功一笔
    timeline_svc.submit_transaction(
        txn_id="KNOWN", account_id="ACC-1", amount=100, submitted_at=V1_PERIOD
    )

    # 同号同内容 -> 重复
    with pytest.raises(DuplicateTransactionError):
        timeline_svc.submit_transactions_batch([
            {"txn_id": "KNOWN", "account_id": "ACC-1", "amount": 100,
             "submitted_at": V1_PERIOD},
        ])

    # 同号关键内容不一致 -> 内容不匹配（可与重复区分）
    with pytest.raises(TransactionContentMismatchError):
        timeline_svc.submit_transactions_batch([
            {"txn_id": "KNOWN", "account_id": "ACC-1", "amount": 999,
             "submitted_at": V1_PERIOD},
        ])


def test_single_submit_mismatch_vs_duplicate_vs_concurrent_are_distinct(
    timeline_svc, monkeypatch
):
    import app.engine as engine_mod

    timeline_svc.submit_transaction(
        txn_id="RACE", account_id="ACC-1", amount=100, submitted_at=V1_PERIOD
    )
    # 已完成：同内容=重复，异内容=内容不匹配
    with pytest.raises(DuplicateTransactionError):
        timeline_svc.submit_transaction(
            txn_id="RACE", account_id="ACC-1", amount=100, submitted_at=V1_PERIOD
        )
    with pytest.raises(TransactionContentMismatchError):
        timeline_svc.submit_transaction(
            txn_id="RACE", account_id="ACC-1", amount=200, submitted_at=V1_PERIOD
        )

    # 进行中：并发提交，与前两种原因区分
    release = threading.Event()
    entered = threading.Event()
    original = engine_mod.AssessmentEngine._evaluate

    def blocking(self, txn, rv, pending):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, pending)

    monkeypatch.setattr(engine_mod.AssessmentEngine, "_evaluate", blocking)

    def holder():
        timeline_svc.submit_transaction(
            txn_id="FLIGHT", account_id="ACC-1", amount=100,
            submitted_at=V1_PERIOD + timedelta(days=5),
        )

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(timeout=5)
    try:
        with pytest.raises(ConcurrentSubmissionError):
            timeline_svc.submit_transaction(
                txn_id="FLIGHT", account_id="ACC-1", amount=100,
                submitted_at=V1_PERIOD + timedelta(days=5),
            )
    finally:
        release.set()
        t.join(timeout=10)


def test_batch_overlapping_with_in_flight_single_is_rejected_as_concurrent(
    timeline_svc, monkeypatch
):
    """补录批次与正在处理的单笔实时交易交叠：整批按并发拒绝，且一笔不落。"""
    import app.engine as engine_mod

    release = threading.Event()
    entered = threading.Event()
    original = engine_mod.AssessmentEngine._evaluate

    def blocking(self, txn, rv, pending):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, pending)

    monkeypatch.setattr(engine_mod.AssessmentEngine, "_evaluate", blocking)

    def holder():
        timeline_svc.submit_transaction(
            txn_id="LIVE-FLIGHT", account_id="ACC-1", amount=100,
            submitted_at=V1_PERIOD + timedelta(days=9),
        )

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(timeout=5)
    txn_before = len(timeline_svc.store.transactions)
    try:
        with pytest.raises(ConcurrentSubmissionError):
            timeline_svc.submit_transactions_batch([
                {"txn_id": "LIVE-FLIGHT", "account_id": "ACC-1", "amount": 100,
                 "submitted_at": V1_PERIOD},
                {"txn_id": "OTHER", "account_id": "ACC-1", "amount": 10,
                 "submitted_at": V1_PERIOD + timedelta(days=1)},
            ])
        # 整批一笔不落
        assert len(timeline_svc.store.transactions) == txn_before
    finally:
        release.set()
        t.join(timeout=10)


def test_batch_success_persists_all_and_creates_events_only_for_suspicious(
    timeline_svc,
):
    results = timeline_svc.submit_transactions_batch([
        {"txn_id": "P-1", "account_id": "ACC-1", "amount": 5000,
         "submitted_at": V1_PERIOD},  # v1 reject -> event
        {"txn_id": "P-2", "account_id": "ACC-1", "amount": 10,
         "submitted_at": V1_PERIOD + timedelta(days=1)},  # pass -> no event
        {"txn_id": "P-3", "account_id": "ACC-1", "amount": 6000,
         "submitted_at": V2_PERIOD},  # v2 reject -> event
    ])
    assert [r["assessment"].rule_version for r in results] == [1, 1, 2]
    assert [r["event"] is not None for r in results] == [True, False, True]
    assert len(timeline_svc.store.transactions) == 3
    assert len(timeline_svc.store.events) == 2


def test_batch_empty_is_rejected(timeline_svc):
    with pytest.raises(RuleValidationError):
        timeline_svc.submit_transactions_batch([])
