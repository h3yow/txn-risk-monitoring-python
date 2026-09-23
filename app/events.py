"""可疑事件状态流转。

状态机：
    OPEN ──► CONFIRMED ──► ESCALATED ──► CONFIRMED
      │          │             │
      │          ▼             ▼
      │       CLOSED        CLOSED
      ├──► ESCALATED
      ├──► FALSE_POSITIVE（终态）
      └──► CLOSED（终态）

终态（CLOSED / FALSE_POSITIVE）不允许任何进一步流转。
"""
from __future__ import annotations

from app.errors import IllegalTransitionError
from app.models import Event, EventStatus, utcnow

ALLOWED_TRANSITIONS: dict[EventStatus, set[EventStatus]] = {
    EventStatus.OPEN: {
        EventStatus.CONFIRMED,
        EventStatus.FALSE_POSITIVE,
        EventStatus.ESCALATED,
        EventStatus.CLOSED,
    },
    EventStatus.CONFIRMED: {EventStatus.ESCALATED, EventStatus.CLOSED},
    EventStatus.ESCALATED: {EventStatus.CONFIRMED, EventStatus.CLOSED},
    EventStatus.FALSE_POSITIVE: set(),
    EventStatus.CLOSED: set(),
}

TERMINAL_STATUSES = {EventStatus.CLOSED, EventStatus.FALSE_POSITIVE}


def can_transition(current: EventStatus, target: EventStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def apply_transition(
    event: Event,
    target: EventStatus,
    *,
    operator: str,
    note: str = "",
) -> Event:
    """校验并就地应用状态流转；非法流转抛 IllegalTransitionError。"""
    current = event.status
    if current is target:
        raise IllegalTransitionError(
            f"事件 {event.event_id} 已处于 {current.value}，无需重复流转",
            details={
                "event_id": event.event_id,
                "from": current.value,
                "to": target.value,
            },
        )
    if not can_transition(current, target):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        raise IllegalTransitionError(
            f"非法状态流转：{current.value} -> {target.value}（允许: {allowed}）",
            details={
                "event_id": event.event_id,
                "from": current.value,
                "to": target.value,
                "allowed": allowed,
            },
        )

    event.history.append(
        {
            "from": current.value,
            "to": target.value,
            "operator": operator,
            "note": note,
            "at": utcnow().isoformat(),
        }
    )
    event.status = target
    return event
