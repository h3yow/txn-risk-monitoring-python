"""规则版本化、整批拒绝加载与历史重放测试。"""
from __future__ import annotations

import pytest

from app.errors import RuleBatchInvalidError
from tests.conftest import make_txn


def test_invalid_rule_batch_is_entirely_rejected(service_with_account, caplog):
    service = service_with_account
    good = [{"rule_id": "r1", "rule_type": "amount_threshold",
             "params": {"threshold": 1000, "score": 60}}]
    service.publish_rules(good, note="v1-good")

    bad_batch = [
        {"rule_id": "ok-rule", "rule_type": "blacklist",
         "params": {"payees": ["X"], "score": 100}},
        {"rule_id": "bad-rule", "rule_type": "amount_threshold",
         "params": {"threshold": -5, "score": 60}},
        {"rule_id": "", "rule_type": "not_exist_type",
         "params": {"score": "high"}},
    ]
    with pytest.raises(RuleBatchInvalidError) as exc:
        service.publish_rules(bad_batch, note="should-not-exist")

    errors = exc.value.details["errors"]
    # 必须逐条指出问题，且整批拒绝
    assert any("threshold" in e for e in errors)
    assert any("rule_type" in e for e in errors)
    assert len(service.list_rule_versions()) == 1  # 未部分生效
    assert service.list_rule_versions()[0].note == "v1-good"


def test_duplicate_rule_id_in_batch_rejects_entire_batch(service_with_account):
    service = service_with_account
    batch = [
        {"rule_id": "dup", "rule_type": "amount_threshold",
         "params": {"threshold": 100, "score": 10}},
        {"rule_id": "dup", "rule_type": "amount_threshold",
         "params": {"threshold": 20, "score": 20}},
    ]
    with pytest.raises(RuleBatchInvalidError):
        service.publish_rules(batch)
    assert service.list_rule_versions() == []


def test_rule_upgrade_does_not_change_historical_decisions(service_with_account, caplog):
    service = service_with_account
    service.publish_rules(
        [{"rule_id": "r-amount", "rule_type": "amount_threshold",
          "params": {"threshold": 1000, "score": 60}}],
        note="v1 阈值1000",
    )
    txn = make_txn("T1", amount=5000)
    v1_record = service.submit_transaction(txn)
    assert v1_record.rule_version == 1
    assert v1_record.decision.value == "review"
    event_id_v1 = v1_record.event_id

    # 升级：阈值上调到 10000，且停用旧规则的效果 -> 同一交易在 v2 下应为 allow
    service.publish_rules(
        [{"rule_id": "r-amount-v2", "rule_type": "amount_threshold",
          "params": {"threshold": 10000, "score": 60}}],
        note="v2 阈值10000",
    )

    # 历史结论不允许被重提改变
    stored = service.get_decision("T1")
    assert (stored.rule_version, stored.decision.value, stored.score) == (1, "review", 60)
    assert stored.event_id == event_id_v1

    # 按当时版本重放：v1 复现 review/60，v2 复现 allow/0
    d1, s1, h1 = service.replay_transaction("T1", 1)
    d2, s2, h2 = service.replay_transaction("T1", 2)
    assert (d1.value, s1, [x.rule_id for x in h1]) == ("review", 60, ["r-amount"])
    assert (d2.value, s2, h2) == ("allow", 0, [])

    # 重放后既有结论与事件仍不变
    again = service.get_decision("T1")
    assert (again.rule_version, again.decision.value, again.score) == (1, "review", 60)
    event = service.get_event(event_id_v1)
    assert event.rule_version == 1
    assert event.hit_rule_ids == ["r-amount"]

    # 单测日志含交易标识、规则版本、判定依据
    text = caplog.text
    assert "txn_replayed txn_id=T1" in text
    assert "replay_rule_version=v1" in text
    assert "replay_rule_version=v2" in text
    assert "stored=v1/review/60" in text
    assert "r-amount(+60)" in text


def test_new_transactions_after_upgrade_use_new_version(service_with_account):
    service = service_with_account
    service.publish_rules(
        [{"rule_id": "a", "rule_type": "amount_threshold",
          "params": {"threshold": 1000, "score": 60}}], note="v1")
    service.publish_rules(
        [{"rule_id": "b", "rule_type": "blacklist",
          "params": {"payees": ["BADGUY"], "score": 100}}], note="v2")

    rec_old = service.submit_transaction(make_txn("OLD", amount=5000))
    assert (rec_old.rule_version, rec_old.decision.value) == (2, "allow")

    rec_bad = service.submit_transaction(
        make_txn("BAD", amount=10, payee="BADGUY"))
    assert (rec_bad.rule_version, rec_bad.decision.value, rec_bad.score) == (2, "block", 100)


def test_replay_unknown_version_raises(service_with_account):
    service = service_with_account
    service.publish_rules(
        [{"rule_id": "a", "rule_type": "amount_threshold",
          "params": {"threshold": 1, "score": 10}}], note="v1")
    service.submit_transaction(make_txn("T1"))
    with pytest.raises(Exception) as exc:
        service.replay_transaction("T1", 99)
    assert exc.value.code == "not_found"


def test_pin_specific_rule_version_at_submission(service_with_account):
    service = service_with_account
    service.publish_rules(
        [{"rule_id": "a", "rule_type": "amount_threshold",
          "params": {"threshold": 10000, "score": 60}}], note="v1 宽松")
    service.publish_rules(
        [{"rule_id": "a", "rule_type": "amount_threshold",
          "params": {"threshold": 1000, "score": 60}}], note="v2 严格")
    # 显式指定按 v1 评估
    rec = service.submit_transaction(make_txn("PIN", amount=5000), rule_version=1)
    assert rec.rule_version == 1 and rec.decision.value == "allow"
