"""可疑事件状态流转与处置追溯。

事件创建时即固化：
- 触发时的交易输入（``transaction_snapshot``）；
- 命中规则、规则版本与判定依据（``assessment_snapshot``）。

状态流转受状态机约束，非法流转抛 :class:`InvalidTransitionError`；
每次合法流转都追加一条不可变的 :class:`EventAction` 审计记录。
"""

from __future__ import annotations

import logging

from app.models import (
    Assessment,
    EventAction,
    EventStatus,
    InvalidTransitionError,
    SuspiciousEvent,
    Transaction,
    utcnow,
)
from app.store import RiskStore

logger = logging.getLogger("risk.events")

# 合法流转表：REVIEW 事件与 REJECT 事件共用一套宽松终态约束
ALLOWED_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.OPEN: {
        EventStatus.CONFIRMED,
        EventStatus.FALSE_POSITIVE,
        EventStatus.ESCALATED,
        EventStatus.CLOSED,
    },
    EventStatus.CONFIRMED: {
        EventStatus.ESCALATED,
        EventStatus.CLOSED,
    },
    EventStatus.FALSE_POSITIVE: {
        EventStatus.CLOSED,  # 误报确认后只允许关闭
    },
    EventStatus.ESCALATED: {
        EventStatus.CONFIRMED,
        EventStatus.FALSE_POSITIVE,
        EventStatus.CLOSED,
    },
    EventStatus.CLOSED: set(),  # 终态
}

# 流转动作 -> 目标状态（API 层可按动作名提交）
ACTION_TARGETS: dict[str, EventStatus] = {
    "confirm": EventStatus.CONFIRMED,
    "mark_false_positive": EventStatus.FALSE_POSITIVE,
    "escalate": EventStatus.ESCALATED,
    "close": EventStatus.CLOSED,
}


class EventService:
    def __init__(self, store: RiskStore) -> None:
        self.store = store

    def build_event(
        self, txn: Transaction, assessment: Assessment
    ) -> SuspiciousEvent:
        """构造（不落库）一个可疑事件，供批量评估在原子区内使用。

        调用方负责在同一原子临界区里把事件与结论一起提交；
        同一交易至多一个事件（已存在则拒绝）。
        """
        existing = self.find_by_txn(txn.txn_id)
        if existing is not None:
            raise InvalidTransitionError(
                f"交易 {txn.txn_id} 已存在事件 {existing.event_id}"
            )
        return SuspiciousEvent(
            event_id=self.store.next_event_id(),
            txn_id=txn.txn_id,
            status=EventStatus.OPEN,
            assessment_snapshot=assessment,
            transaction_snapshot=txn,
        )

    def create_event(
        self, txn: Transaction, assessment: Assessment
    ) -> SuspiciousEvent:
        """基于一笔可疑交易的结论创建事件；同一交易至多一个事件。"""
        event = self.build_event(txn, assessment)
        self.store.save_event(event)
        logger.info(
            "[EVENT-OPEN] event_id=%s txn_id=%s rule_version=%s hits=%d",
            event.event_id,
            txn.txn_id,
            assessment.rule_version,
            len(assessment.hits),
        )
        return event

    def find_by_txn(self, txn_id: str) -> SuspiciousEvent | None:
        for event in self.store.events.values():
            if event.txn_id == txn_id:
                return event
        return None

    def transition(
        self,
        event_id: str,
        to_status: EventStatus,
        operator: str,
        note: str = "",
    ) -> SuspiciousEvent:
        event = self.store.get_event(event_id)
        if event is None:
            raise InvalidTransitionError(f"事件 {event_id} 不存在")

        from_status = event.status
        if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
            err = (
                f"非法状态流转：事件 {event_id} 不允许从 "
                f"{from_status.value} 变为 {to_status.value}"
            )
            logger.warning(
                "[EVENT-REJECT] event_id=%s %s -> %s operator=%s",
                event_id,
                from_status.value,
                to_status.value,
                operator,
            )
            raise InvalidTransitionError(err)

        action = EventAction(
            action=f"{from_status.value}->{to_status.value}",
            from_status=from_status,
            to_status=to_status,
            operator=operator,
            note=note,
            acted_at=utcnow(),
        )
        event.history.append(action)
        event.status = to_status
        self.store.save_event(event)
        logger.info(
            "[EVENT-TRANSITION] event_id=%s %s -> %s operator=%s note=%r",
            event_id,
            from_status.value,
            to_status.value,
            operator,
            note,
        )
        return event

    def act(
        self, event_id: str, action: str, operator: str, note: str = ""
    ) -> SuspiciousEvent:
        if action not in ACTION_TARGETS:
            raise InvalidTransitionError(
                f"未知处置动作 {action!r}，支持: {sorted(ACTION_TARGETS)}"
            )
        return self.transition(event_id, ACTION_TARGETS[action], operator, note)
