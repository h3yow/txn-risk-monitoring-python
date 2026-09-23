"""可疑事件状态流转：非法流转拒绝，每次处置可追溯输入、命中规则及版本。"""

from __future__ import annotations

import pytest

from app.models import EventStatus, InvalidTransitionError


def _open_event(svc, txn_id="T-EVT-1", amount=1500):
    result = svc.submit_transaction(
        txn_id=txn_id,
        account_id="ACC-1",
        amount=amount,  # v1 下命中 AMT-1000（120 分）-> reject -> 自动建事件
        counterparty="NORMAL-CORP",
    )
    return result["event"]


def test_legal_lifecycle_records_full_history(svc):
    event = _open_event(svc)
    assert event.status is EventStatus.OPEN

    svc.handle_event(event.event_id, "escalate", "alice", "金额异常需主管复核")
    svc.handle_event(event.event_id, "confirm", "bob", "确认为欺诈")
    svc.handle_event(event.event_id, "close", "bob", "已冻结账户")

    final = svc.get_event(event.event_id)
    assert final.status is EventStatus.CLOSED
    assert [h.action for h in final.history] == [
        "open->escalated",
        "escalated->confirmed",
        "confirmed->closed",
    ]
    assert [h.operator for h in final.history] == ["alice", "bob", "bob"]


def test_illegal_transitions_are_rejected(svc):
    event = _open_event(svc)

    # 误报 -> 关闭合法；关闭是终态，再次确认非法
    svc.handle_event(event.event_id, "mark_false_positive", "alice")
    with pytest.raises(InvalidTransitionError) as exc:
        svc.handle_event(event.event_id, "confirm", "alice")
    assert "false_positive" in str(exc.value) and "confirmed" in str(exc.value)

    svc.handle_event(event.event_id, "close", "alice")
    with pytest.raises(InvalidTransitionError):
        svc.handle_event(event.event_id, "escalate", "alice")

    # 非法流转不得留下审计痕迹
    final = svc.get_event(event.event_id)
    assert final.status is EventStatus.CLOSED
    assert [h.to_status for h in final.history] == [
        EventStatus.FALSE_POSITIVE,
        EventStatus.CLOSED,
    ]


def test_closed_is_terminal_even_for_close_itself(svc):
    event = _open_event(svc, txn_id="T-EVT-2")
    svc.handle_event(event.event_id, "close", "alice")
    with pytest.raises(InvalidTransitionError):
        svc.handle_event(event.event_id, "close", "alice")


def test_unknown_action_and_missing_event_are_rejected(svc):
    event = _open_event(svc, txn_id="T-EVT-3")
    with pytest.raises(InvalidTransitionError):
        svc.handle_event(event.event_id, "delete", "alice")
    with pytest.raises(InvalidTransitionError):
        svc.handle_event("EVT-999999", "close", "alice")


def test_pass_transaction_creates_no_event(svc):
    result = svc.submit_transaction(
        txn_id="T-EVT-PASS",
        account_id="ACC-1",
        amount=10,
        counterparty="NORMAL-CORP",
    )
    assert result["event"] is None


def test_event_snapshot_is_traceable_to_input_rules_and_version(svc):
    event = _open_event(svc, txn_id="T-EVT-TRACE", amount=5000)
    svc.handle_event(event.event_id, "confirm", "alice", note="确认")

    loaded = svc.get_event(event.event_id)
    # 触发时输入
    t = loaded.transaction_snapshot
    assert (t.txn_id, t.amount, t.counterparty) == ("T-EVT-TRACE", 5000.0, "NORMAL-CORP")
    # 命中规则及其版本
    a = loaded.assessment_snapshot
    assert a.rule_version == 1
    hit = a.hits[0]
    assert (hit.rule_id, hit.rule_version) == ("AMT-1000", 1)
    assert "5000" in hit.reason
    # 处置痕迹
    assert loaded.history[0].action == "open->confirmed"
