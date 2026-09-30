"""历史交易补录与规则生效时间语义。

所有时间均使用固定常量（2026 年内的锚点），用例在任何一天跑结果都一样。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Decision,
    DuplicateTransactionError,
    ConcurrentSubmissionError,
    NoEffectiveRuleVersionError,
    RuleEffectiveTimeConflictError,
    RuleValidationError,
    TransactionContentConflictError,
)

# 夹具中 v1 的生效时间为 2026-01-01T00:00:00Z
V1_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
T3 = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 规则生效时间

def test_effective_at_earlier_than_existing_is_rejected_distinct_from_invalid_rules(svc):
    # 内容完全合法，但生效时间早于 v1 -> 生效时间冲突，区别于内容非法
    legal_rules = [
        {"rule_id": "AMT-1", "rule_type": "amount_limit",
         "params": {"limit": 100, "score": 1}},
    ]
    with pytest.raises(RuleEffectiveTimeConflictError):
        svc.publish_rules(legal_rules, effective_at=V1_AT - timedelta(seconds=1))
    # 撞在同一时间点同样整批拒绝
    with pytest.raises(RuleEffectiveTimeConflictError):
        svc.publish_rules(legal_rules, effective_at=V1_AT)
    # 拒绝不产生新版本
    assert svc.current_rule_version() == 1

    # 对照：规则内容非法时是另一种异常
    bad_rules = [
        {"rule_id": "BAD", "rule_type": "amount_limit",
         "params": {"limit": "oops", "score": 1}},
    ]
    with pytest.raises(RuleValidationError):
        svc.publish_rules(bad_rules, effective_at=V1_AT + timedelta(days=10))


def test_effective_times_must_be_strictly_monotonic(svc):
    svc.publish_rules(
        [{"rule_id": "AMT-2", "rule_type": "amount_limit",
          "params": {"limit": 200, "score": 1}}],
        effective_at=T1,
    )
    # 第三个版本生效时间早于第二个版本 -> 拒绝，不挑版本凑合
    with pytest.raises(RuleEffectiveTimeConflictError):
        svc.publish_rules(
            [{"rule_id": "AMT-3", "rule_type": "amount_limit",
              "params": {"limit": 300, "score": 1}}],
            effective_at=T1 - timedelta(hours=1),
        )
    assert svc.current_rule_version() == 2


def test_future_effective_version_is_not_yet_active_for_live_submission(svc):
    # v2 生效时间在"未来"（相对固定的业务时间锚点；发布动作本身不影响）
    future = T3 + timedelta(days=3650)
    svc.publish_rules(
        [{"rule_id": "AMT-5", "rule_type": "amount_limit",
          "params": {"limit": 5, "score": 120}}],
        effective_at=future,
    )
    # 当前没有进入 v2 生效窗口 -> 实时交易仍按 v1（5000 在 v1 limit=1000 下拒绝）
    result = svc.submit_transaction(txn_id="T-LIVE", account_id="ACC-1", amount=5000)
    assert result["assessment"].rule_version == 1


def test_txn_before_earliest_effective_version_is_rejected(svc):
    ancient = V1_AT - timedelta(days=30)
    with pytest.raises(NoEffectiveRuleVersionError):
        svc.backfill_transactions(
            [{"txn_id": "OLD-1", "account_id": "ACC-1",
              "amount": 10, "submitted_at": ancient}]
        )
    # 拒绝后没有任何落库痕迹
    assert svc.get_assessment("OLD-1") is None
    assert svc.store.get_transaction("OLD-1") is None
    assert len(svc.store.events) == 0


# ---------------------------------------------------------------- 补录按发生时刻判定

def test_backfilled_txn_uses_version_effective_at_occurrence_time(svc):
    # v2 在 T1 生效：限额从 1000 提到 5000
    svc.publish_rules(
        [{"rule_id": "AMT-5000", "rule_type": "amount_limit",
          "params": {"limit": 5000, "score": 120}},
         {"rule_id": "BL-1", "rule_type": "blocklist",
          "params": {"counterparties": ["BAD-CORP"], "score": 200}}],
        effective_at=T1,
    )
    # 发生在 T1 之前（v1 窗口）的 1500 -> v1 reject
    results = svc.backfill_transactions([
        {"txn_id": "BF-V1", "account_id": "ACC-1", "amount": 1500,
         "submitted_at": T1 - timedelta(minutes=1)},
        {"txn_id": "BF-V2", "account_id": "ACC-1", "amount": 1500,
         "submitted_at": T1 + timedelta(minutes=1)},
    ])
    a_before, a_after = (r["assessment"] for r in results)
    assert a_before.rule_version == 1
    assert a_before.decision is Decision.REJECT
    assert [h.rule_id for h in a_before.hits] == ["AMT-1000"]
    assert a_after.rule_version == 2
    assert a_after.decision is Decision.PASS
    assert a_after.hits == ()

    # 事件只为 reject 的那笔开立
    assert results[0]["event"] is not None
    assert results[1]["event"] is None


def test_backfill_first_assessment_equals_later_replay(svc):
    """补录首评结论 == 之后单独重放：版本、命中、分值、结论逐项一致。"""
    svc.publish_rules(
        [{"rule_id": "AMT-5000", "rule_type": "amount_limit",
          "params": {"limit": 5000, "score": 120}}],
        effective_at=T2,
    )
    results = svc.backfill_transactions([
        {"txn_id": "BF-R1", "account_id": "ACC-1", "amount": 1500,
         "submitted_at": T1},  # v1 窗口 -> reject
        {"txn_id": "BF-R2", "account_id": "ACC-1", "amount": 10,
         "counterparty": "BAD-CORP", "submitted_at": T2 + timedelta(hours=1)},
    ])
    first = results[0]["assessment"]

    replayed = svc.replay_transaction("BF-R1")
    assert replayed.rule_version == first.rule_version
    assert replayed.decision == first.decision
    assert replayed.score == first.score
    assert [(h.rule_id, h.rule_version) for h in replayed.hits] == \
        [(h.rule_id, h.rule_version) for h in first.hits]


def test_backfill_velocity_matches_replay_within_batch(svc):
    """同批补录在 velocity 窗口内互相可见：首评与落库后逐笔重放一致。

    v1 velocity：5 分钟内 >2 笔命中。补录 3 笔落在同一 5 分钟窗口，
    第 3 笔首评即应命中；落库后单独重放结论不变。
    """
    base = T1
    items = [
        {"txn_id": f"BF-V-{i}", "account_id": "ACC-1", "amount": 10,
         "counterparty": "NORMAL", "submitted_at": base + timedelta(minutes=i)}
        for i in range(3)
    ]
    results = svc.backfill_transactions(items)
    hits_first = [
        (a.decision, a.score, [h.rule_id for h in a.hits])
        for r in results for a in [r["assessment"]]
    ]
    # 前两笔不触发（含自身分别为 1、2 笔），第三笔窗口内 3 笔 > 2 -> 命中 50 分 -> review
    assert hits_first[0][2] == []
    assert hits_first[1][2] == []
    assert hits_first[2] == (Decision.REVIEW, 50, ["VEL-2-5M"])

    # 落库后逐笔默认重放，结论必须与首评逐项相同
    for i in range(3):
        txn_id = f"BF-V-{i}"
        replayed = svc.replay_transaction(txn_id)
        first = svc.get_assessment(txn_id)
        assert (replayed.decision, replayed.score) == (first.decision, first.score)
        assert [h.rule_id for h in replayed.hits] == \
            [h.rule_id for h in first.hits]
        assert replayed.rule_version == first.rule_version


def test_backfill_does_not_mutate_existing_assessments_or_events(svc):
    # 先有一笔已存在的结论与事件
    existing = svc.submit_transaction(
        txn_id="EXIST-1", account_id="ACC-1", amount=5000,
        counterparty="NORMAL",
    )
    old_assessment = existing["assessment"]
    old_event_id = existing["event"].event_id

    # 补录一批发生时间更早的历史交易（含黑名单命中）
    svc.backfill_transactions([
        {"txn_id": "BF-OLD-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T1},
    ])

    assert svc.get_assessment("EXIST-1") == old_assessment
    event = svc.get_event(old_event_id)
    assert event.assessment_snapshot == old_assessment
    assert len(event.history) == 0


def test_live_submission_behavior_unchanged(svc):
    # 实时提交（不给 submitted_at）行为与以往一致：当前版本、重复拒绝
    r1 = svc.submit_transaction(txn_id="LIVE-1", account_id="ACC-1", amount=10)
    assert r1["assessment"].rule_version == 1
    with pytest.raises(DuplicateTransactionError):
        svc.submit_transaction(txn_id="LIVE-1", account_id="ACC-1", amount=10)


# ---------------------------------------------------------------- 批量原子性与拒绝原因

def test_batch_is_atomic_when_one_item_conflicts(svc):
    # 先落一笔 T-SEEN
    svc.backfill_transactions([
        {"txn_id": "T-SEEN", "account_id": "ACC-1", "amount": 100,
         "counterparty": "NORMAL", "submitted_at": T1},
    ])

    items = [
        {"txn_id": "BF-OK-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T1},
        # 与已存在交易号相同、内容不一致 -> 内容冲突
        {"txn_id": "T-SEEN", "account_id": "ACC-1", "amount": 999,
         "counterparty": "NORMAL", "submitted_at": T1},
        # 同批里排后面的本来没问题，但整批必须一起回滚
        {"txn_id": "BF-OK-2", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T2},
    ]
    with pytest.raises(TransactionContentConflictError):
        svc.backfill_transactions(items)

    # 整批零落库：没有半截结论，没有凭空事件
    assert svc.get_assessment("BF-OK-1") is None
    assert svc.get_assessment("BF-OK-2") is None
    assert svc.store.get_transaction("BF-OK-1") is None
    assert len(svc.store.events) == 0
    # 占位全部释放：失败批次中无冲突的交易之后仍可正常补录
    again = svc.backfill_transactions([
        {"txn_id": "BF-OK-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T1},
    ])
    assert again[0]["assessment"].txn_id == "BF-OK-1"


def test_batch_rolls_back_when_one_item_has_no_effective_version(svc):
    with pytest.raises(NoEffectiveRuleVersionError):
        svc.backfill_transactions([
            {"txn_id": "BF-A", "account_id": "ACC-1", "amount": 10,
             "submitted_at": T1},
            {"txn_id": "BF-B", "account_id": "ACC-1", "amount": 10,
             "submitted_at": V1_AT - timedelta(days=1)},
        ])
    assert svc.get_assessment("BF-A") is None
    assert svc.get_assessment("BF-B") is None
    assert not svc.store.in_flight


def test_three_rejection_reasons_are_distinguishable(svc):
    # 已完成的同一笔（相同内容）-> 重复
    svc.backfill_transactions([
        {"txn_id": "DUP-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T1},
    ])
    with pytest.raises(DuplicateTransactionError):
        svc.backfill_transactions([
            {"txn_id": "DUP-1", "account_id": "ACC-1", "amount": 10,
             "submitted_at": T1},
        ])
    # 同号但金额对不上 -> 内容冲突
    with pytest.raises(TransactionContentConflictError):
        svc.backfill_transactions([
            {"txn_id": "DUP-1", "account_id": "ACC-1", "amount": 11,
             "submitted_at": T1},
        ])


def test_intra_batch_same_id_duplicate_and_conflict_are_distinct(svc):
    with pytest.raises(DuplicateTransactionError):
        svc.backfill_transactions([
            {"txn_id": "X-1", "account_id": "ACC-1", "amount": 10,
             "submitted_at": T1},
            {"txn_id": "X-1", "account_id": "ACC-1", "amount": 10,
             "submitted_at": T1},
        ])
    with pytest.raises(TransactionContentConflictError):
        svc.backfill_transactions([
            {"txn_id": "X-2", "account_id": "ACC-1", "amount": 10,
             "submitted_at": T1},
            {"txn_id": "X-2", "account_id": "ACC-1", "amount": 11,
             "submitted_at": T1},
        ])
    # 两种失败都零落库、零占位残留
    assert not svc.store.in_flight
    assert svc.get_assessment("X-1") is None
    assert svc.get_assessment("X-2") is None


def test_backfill_validation_errors_leave_nothing(svc):
    with pytest.raises(RuleValidationError):
        svc.backfill_transactions([
            {"txn_id": "BF-MISS", "account_id": "ACC-1",
             "amount": 10},  # 缺 submitted_at
        ])
    with pytest.raises(RuleValidationError):
        svc.backfill_transactions([
            {"txn_id": "BF-NOACC", "account_id": "ACC-404", "amount": 10,
             "submitted_at": T1},
        ])
    with pytest.raises(RuleValidationError):
        svc.backfill_transactions([])
    assert not svc.store.in_flight


# ---------------------------------------------------------------- 与实时交易并发交叠

def test_backfill_overlapping_with_live_concurrent_submission(svc, monkeypatch):
    """补录批次评估窗口内，实时通道提交同一交易号：并发 vs 内容冲突可区分。"""
    import threading
    import app.engine as engine_mod

    release = threading.Event()
    entered = threading.Event()
    original = engine_mod.AssessmentEngine.assess_historical

    def blocking(self, txn, extra=(), **_kw):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, extra=extra)

    monkeypatch.setattr(engine_mod.AssessmentEngine, "assess_historical", blocking)

    errors: list[Exception] = []

    def batch():
        try:
            svc.backfill_transactions([
                {"txn_id": "RACE-1", "account_id": "ACC-1", "amount": 10,
                 "submitted_at": T1},
            ])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=batch)
    t.start()
    entered.wait(timeout=5)  # 批次已占位、停在评估窗口内

    # 相同内容（含相同发生时刻）的并发提交 -> 并发冲突
    with pytest.raises(ConcurrentSubmissionError):
        svc.submit_transaction(
            txn_id="RACE-1", account_id="ACC-1", amount=10, submitted_at=T1
        )
    # 同号但金额对不上 -> 内容冲突（即使对方在途）
    with pytest.raises(TransactionContentConflictError):
        svc.submit_transaction(
            txn_id="RACE-1", account_id="ACC-1", amount=999, submitted_at=T1
        )

    release.set()
    t.join(timeout=10)
    assert errors == []

    # 批次落库后再以不同内容提交 -> 内容冲突（此时是与已完成结论比对）
    with pytest.raises(TransactionContentConflictError):
        svc.submit_transaction(
            txn_id="RACE-1", account_id="ACC-1", amount=999, submitted_at=T1
        )
    with pytest.raises(DuplicateTransactionError):
        svc.submit_transaction(
            txn_id="RACE-1", account_id="ACC-1", amount=10, submitted_at=T1
        )


def test_same_content_with_different_tz_representation_is_duplicate(svc):
    """+00:00 与等价的偏移量表示视为同一时刻，不算内容冲突。"""
    tz_plus8 = datetime(2026, 6, 1, 18, 0, 0,
                        tzinfo=timezone(timedelta(hours=8)))  # == T1
    svc.backfill_transactions([
        {"txn_id": "TZ-1", "account_id": "ACC-1", "amount": 10,
         "submitted_at": T1},
    ])
    with pytest.raises(DuplicateTransactionError):
        svc.backfill_transactions([
            {"txn_id": "TZ-1", "account_id": "ACC-1", "amount": 10,
             "submitted_at": tz_plus8},
        ])
