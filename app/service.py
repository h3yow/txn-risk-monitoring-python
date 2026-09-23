"""门面服务：聚合存储、规则版本、判定引擎与事件处置。

使用方式：
    svc = RiskMonitoringService()
    svc.publish_rules([...], note="v1")
    svc.create_account("ACC-1")
    result = svc.submit_transaction(txn_id=..., account_id=..., amount=..., ...)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.engine import AssessmentEngine
from app.events import EventService
from app.models import (
    Account,
    Assessment,
    Decision,
    RuleValidationError,
    SuspiciousEvent,
    Transaction,
    utcnow,
)
from app.rules import validate_rules
from app.store import RiskStore

logger = logging.getLogger("risk.service")


class RiskMonitoringService:
    def __init__(self) -> None:
        self.store = RiskStore()
        self.engine = AssessmentEngine(self.store)
        self.events = EventService(self.store)

    # ------------------------------------------------------------ 账户

    def create_account(self, account_id: str, risk_level: str = "normal") -> Account:
        return self.store.create_account(
            Account(account_id=account_id, risk_level=risk_level)
        )

    # ------------------------------------------------------------ 规则版本

    def publish_rules(
        self,
        payload: list[dict[str, Any]],
        note: str = "",
        created_at: datetime | None = None,
    ) -> int:
        """校验并发布一批规则为新版本。

        非法规则抛出 ``RuleValidationError``，整批拒绝、不产生新版本，
        当前生效版本保持不变（不部分生效、不静默降级）。
        返回新版本号。
        """
        # 先完整校验：失败时 validate_rules 内部不会触碰 store
        rules = validate_rules(payload)
        rv = self.store.append_rule_version(
            rules, note=note, created_at=created_at
        )
        logger.info(
            "[RULES-PUBLISH] version=%d rules=%d note=%r",
            rv.version,
            len(rules),
            note,
        )
        return rv.version

    def current_rule_version(self) -> int | None:
        rv = self.store.current_rule_version()
        return rv.version if rv else None

    # ------------------------------------------------------------ 交易

    def submit_transaction(
        self,
        *,
        txn_id: str,
        account_id: str,
        amount: float,
        currency: str = "CNY",
        counterparty: str = "",
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        if self.store.get_account(account_id) is None:
            raise RuleValidationError(f"账户 {account_id} 不存在")
        txn = Transaction(
            txn_id=txn_id,
            account_id=account_id,
            amount=float(amount),
            currency=currency,
            counterparty=counterparty,
            submitted_at=submitted_at or utcnow(),
        )
        assessment = self.engine.assess(txn)

        event: SuspiciousEvent | None = None
        if assessment.decision in (Decision.REVIEW, Decision.REJECT):
            event = self.events.create_event(txn, assessment)

        return {
            "assessment": assessment,
            "event": event,
        }

    def replay_transaction(
        self, txn_id: str, version: int | None = None
    ) -> Assessment:
        return self.engine.replay(txn_id, version=version)

    def get_assessment(self, txn_id: str) -> Assessment | None:
        return self.store.get_assessment(txn_id)

    # ------------------------------------------------------------ 事件处置

    def handle_event(
        self, event_id: str, action: str, operator: str, note: str = ""
    ) -> SuspiciousEvent:
        return self.events.act(event_id, action, operator, note)

    def get_event(self, event_id: str) -> SuspiciousEvent | None:
        return self.store.get_event(event_id)
