"""规则版本化、非法规则整批拒绝、规则升级后的历史重放。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Decision, RuleValidationError

T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def test_invalid_ruleset_is_rejected_entirely(svc):
    bad_batches = [
        [],  # 空批次
        [{"rule_id": "X", "rule_type": "unknown_type", "params": {"score": 1}}],
        [{"rule_id": "X", "rule_type": "amount_limit", "params": {"limit": -5, "score": 1}}],
        [{"rule_id": "X", "rule_type": "velocity", "params": {"window_minutes": 0, "max_count": 2, "score": 1}}],
        [{"rule_id": "X", "rule_type": "blocklist", "params": {"counterparties": [], "score": 1}}],
        [{"rule_id": "X", "rule_type": "amount_limit", "params": {"limit": 100, "score": -1}}],
        [{"rule_id": "X", "rule_type": "amount_limit", "params": {"limit": 100}, "enabled": "yes"}],
        # 批次内 rule_id 重复：即使两条各自合法，也必须整批拒绝
        [
            {"rule_id": "DUP", "rule_type": "amount_limit", "params": {"limit": 100, "score": 1}},
            {"rule_id": "DUP", "rule_type": "blocklist", "params": {"counterparties": ["A"], "score": 1}},
        ],
        "not-a-list",
    ]
    for batch in bad_batches:
        with pytest.raises(RuleValidationError):
            svc.publish_rules(batch)  # type: ignore[arg-type]

    # 全部非法批次都没有产生新版本，当前仍是 v1
    assert svc.current_rule_version() == 1


def test_v2_upgrade_does_not_change_historical_conclusion(svc):
    # 金额 1500：在 v1（limit=1000）下被拒绝，命中 AMT-1000
    result = svc.submit_transaction(
        txn_id="T-HIST-1",
        account_id="ACC-1",
        amount=1500,
        submitted_at=T0,
    )
    v1_assessment = result["assessment"]
    assert v1_assessment.decision is Decision.REJECT
    assert v1_assessment.rule_version == 1
    assert [h.rule_id for h in v1_assessment.hits] == ["AMT-1000"]
    event_v1 = result["event"]
    assert event_v1 is not None

    # 发布 v2：提高限额到 5000（旧规则升级/停用），新增黑名单
    v2 = svc.publish_rules(
        [
            {"rule_id": "AMT-5000", "rule_type": "amount_limit",
             "params": {"limit": 5000, "score": 120}},
            {"rule_id": "VEL-2-5M", "rule_type": "velocity", "enabled": False,
             "params": {"window_minutes": 5, "max_count": 2, "score": 50}},
            {"rule_id": "BL-1", "rule_type": "blocklist",
             "params": {"counterparties": ["BAD-CORP", "SANCTIONED-X"], "score": 200}},
            {"rule_id": "BL-2", "rule_type": "blocklist",
             "params": {"counterparties": ["NEW-BAD"], "score": 200}},
        ],
        note="限额上调、velocity 停用",
        created_at=T0 + timedelta(days=1),
    )
    assert v2 == 2

    # 历史落库结论原封不动：版本号、命中规则、决策都不变
    stored = svc.get_assessment("T-HIST-1")
    assert stored == v1_assessment
    assert stored.rule_version == 1
    assert [h.rule_id for h in stored.hits] == ["AMT-1000"]

    # 默认重放：按交易提交时刻生效的版本（v1）复现 -> 与历史一致
    replayed = svc.replay_transaction("T-HIST-1")
    assert replayed.rule_version == 1
    assert replayed.decision is Decision.REJECT
    assert replayed.score == stored.score
    assert [h.rule_id for h in replayed.hits] == ["AMT-1000"]

    # 事件快照也固化在 v1，可追溯触发输入与命中规则版本
    event = svc.get_event(event_v1.event_id)
    assert event.assessment_snapshot.rule_version == 1
    assert event.transaction_snapshot.amount == 1500


def test_replay_with_explicit_new_version_shows_difference_but_keeps_history(svc):
    svc.submit_transaction(
        txn_id="T-HIST-2",
        account_id="ACC-1",
        amount=1500,
        submitted_at=T0,
    )
    svc.publish_rules(
        [
            {"rule_id": "AMT-5000", "rule_type": "amount_limit",
             "params": {"limit": 5000, "score": 120}},
            {"rule_id": "BL-1", "rule_type": "blocklist",
             "params": {"counterparties": ["BAD-CORP"], "score": 200}},
        ],
        created_at=T0 + timedelta(days=1),
    )

    # 显式用 v2 复算：1500 < 5000 且不命中黑名单 -> pass（展示差异）
    replayed_v2 = svc.replay_transaction("T-HIST-2", version=2)
    assert replayed_v2.rule_version == 2
    assert replayed_v2.decision is Decision.PASS
    assert replayed_v2.hits == ()

    # 但历史结论依旧是 v1 的 reject，未被复算覆盖
    stored = svc.get_assessment("T-HIST-2")
    assert stored.rule_version == 1
    assert stored.decision is Decision.REJECT


def test_replay_nonexistent_version_or_txn_is_rejected(svc):
    svc.submit_transaction(
        txn_id="T-HIST-3",
        account_id="ACC-1",
        amount=10,
        submitted_at=T0,
    )
    with pytest.raises(RuleValidationError):
        svc.replay_transaction("T-HIST-3", version=99)
    with pytest.raises(RuleValidationError):
        svc.replay_transaction("NOT-EXIST")


def test_new_txn_after_upgrade_uses_v2(svc):
    svc.publish_rules(
        [
            {"rule_id": "AMT-5000", "rule_type": "amount_limit",
             "params": {"limit": 5000, "score": 120}},
        ],
        created_at=T0 + timedelta(days=1),
    )
    result = svc.submit_transaction(
        txn_id="T-NEW-1",
        account_id="ACC-1",
        amount=1500,
        submitted_at=T0 + timedelta(days=2),
    )
    assert result["assessment"].rule_version == 2
    assert result["assessment"].decision is Decision.PASS
