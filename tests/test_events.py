"""可疑事件状态流转与处置追溯测试。"""
from __future__ import annotations

import pytest

from app.errors import IllegalTransitionError, NotFoundError
from app.models import EventStatus
from tests.conftest import make_txn


def _setup_event(service):
    service.publish_rules([
        {"rule_id": "r-amount", "rule_type": "amount_threshold",
         "params": {"threshold": 1000, "score": 60}},
        {"rule_id": "r-country", "rule_type": "high_risk_country",
         "params": {"countries": ["XX"], "score": 60}},
    ], note="v1")
    record = service.submit_transaction(make_txn("T1", country="XX"))
    return record


def test_full_lifecycle_open_confirmed_escalated_closed(service_with_account):
    service = service_with_account
    record = _setup_event(service)
    eid = record.event_id
    assert (record.decision.value, record.score) == ("block", 120)

    ev = service.transition_event(eid, EventStatus.CONFIRMED, operator="alice", note="人工确认")
    assert ev.status is EventStatus.CONFIRMED
    ev = service.transition_event(eid, EventStatus.ESCALATED, operator="bob", note="升级")
    assert ev.status is EventStatus.ESCALATED
    ev = service.transition_event(eid, EventStatus.CLOSED, operator="bob", note="处置完成")
    assert ev.status is EventStatus.CLOSED

    actions = [(h["from"], h["to"], h["operator"]) for h in ev.history[1:]]
    assert actions == [
        ("open", "confirmed", "alice"),
        ("confirmed", "escalated", "bob"),
        ("escalated", "closed", "bob"),
    ]


def test_illegal_transitions_are_rejected(service_with_account, caplog):
    service = service_with_account
    record = _setup_event(service)
    eid = record.event_id

    # escalated 不能直接 false_positive
    service.transition_event(eid, EventStatus.ESCALATED, operator="alice")
    with pytest.raises(IllegalTransitionError) as exc:
        service.transition_event(eid, EventStatus.FALSE_POSITIVE, operator="alice")
    assert exc.value.details == {
        "event_id": eid, "from": "escalated", "to": "false_positive",
        "allowed": ["closed", "confirmed"],
    }

    # 关闭后再处置 -> 拒绝
    service.transition_event(eid, EventStatus.CLOSED, operator="alice")
    with pytest.raises(IllegalTransitionError):
        service.transition_event(eid, EventStatus.ESCALATED, operator="alice")

    # 重复流转到同一状态 -> 拒绝
    rec2 = service.submit_transaction(make_txn("T2"))
    with pytest.raises(IllegalTransitionError):
        service.transition_event(rec2.event_id, EventStatus.OPEN, operator="alice")

    assert service.get_event(eid).status is EventStatus.CLOSED
    assert "event_transition" in caplog.text


def test_false_positive_is_terminal(service_with_account):
    service = service_with_account
    record = _setup_event(service)
    eid = record.event_id
    ev = service.transition_event(eid, EventStatus.FALSE_POSITIVE, operator="carol", note="误报")
    assert ev.status is EventStatus.FALSE_POSITIVE
    with pytest.raises(IllegalTransitionError):
        service.transition_event(eid, EventStatus.CLOSED, operator="carol")


def test_event_is_traceable_to_input_rules_and_version(service_with_account, caplog):
    """每次处置都能追溯到触发输入、命中规则及版本。"""
    service = service_with_account
    record = _setup_event(service)
    eid = record.event_id

    event = service.get_event(eid)
    # 命中规则与版本
    assert event.rule_version == 1
    assert set(event.hit_rule_ids) == {"r-amount", "r-country"}
    assert event.score == 120
    # 回溯到触发时的输入与结论
    decision = service.get_decision(event.txn_id)
    assert decision.txn_id == "T1"
    assert decision.amount == 2000
    assert decision.country == "XX"
    assert {h.rule_id for h in decision.hits} == {"r-amount", "r-country"}
    assert all(h.rule_version == 1 for h in decision.hits)

    service.transition_event(eid, EventStatus.CONFIRMED, operator="alice", note="证据确凿")
    log = caplog.text
    assert f"event_id={eid}" in log
    assert "txn_id=T1" in log
    assert "rule_version=1" in log
    assert "open->confirmed" in log


def test_event_not_found(service_with_account):
    service = service_with_account
    with pytest.raises(NotFoundError):
        service.transition_event("EVT-NOPE", EventStatus.CLOSED, operator="x")
