"""门面服务：聚合存储、规则版本、判定引擎与事件处置。

使用方式：
    svc = RiskMonitoringService()
    svc.publish_rules([...], note="v1")
    svc.create_account("ACC-1")
    result = svc.submit_transaction(txn_id=..., account_id=..., amount=...)
    # 补录一批历史交易（按各自发生时刻判定，整批原子）：
    results = svc.submit_transactions_batch([{...}, ...])
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
    ensure_utc,
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
        effective_at: datetime | None = None,
    ) -> int:
        """校验并发布一批规则为新版本。

        - 规则内容非法：抛 ``RuleValidationError``，整批拒绝、不产生新版本；
        - 内容合法但生效时间不晚于已有版本（含同一时刻）：
          抛 ``RuleEffectiveTimeConflictError``，同样整批拒绝，
          两种拒绝原因可区分。
        返回新版本号。
        """
        # 先完整校验规则内容：失败时 validate_rules 不触碰 store
        rules = validate_rules(payload)
        rv = self.store.append_rule_version(
            rules, note=note, effective_at=effective_at
        )
        logger.info(
            "[RULES-PUBLISH] version=%d rules=%d effective_at=%s note=%r",
            rv.version,
            len(rules),
            rv.effective_at.isoformat(),
            note,
        )
        return rv.version

    def current_rule_version(self) -> int | None:
        """当前时刻生效的规则版本号（区别于"最新发布"）。"""
        rv = self._effective_now()
        return rv.version if rv else None

    def _effective_now(self):
        return self.store.effective_rule_version_safe(utcnow())

    # ------------------------------------------------------------ 交易

    @staticmethod
    def _build_txn(
        *,
        txn_id: str,
        account_id: str,
        amount: float,
        currency: str = "CNY",
        counterparty: str = "",
        submitted_at: datetime | None = None,
    ) -> Transaction:
        return Transaction(
            txn_id=txn_id,
            account_id=account_id,
            amount=float(amount),
            currency=currency,
            counterparty=counterparty,
            submitted_at=ensure_utc(submitted_at) if submitted_at else utcnow(),
        )

    def _check_account(self, account_id: str) -> None:
        if self.store.get_account(account_id) is None:
            raise RuleValidationError(f"账户 {account_id} 不存在")

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
        """提交单笔交易（实时或单笔补录）。

        - 不带 ``submitted_at``：实时交易，按当前生效版本判定；
        - 带 ``submitted_at``：补录，按该发生时刻生效的版本判定，
          早于最早生效版本将被拒绝（不硬套最新规则）。
        """
        self._check_account(account_id)
        txn = self._build_txn(
            txn_id=txn_id,
            account_id=account_id,
            amount=amount,
            currency=currency,
            counterparty=counterparty,
            submitted_at=submitted_at,
        )
        assessment = self.engine.assess(txn)

        event: SuspiciousEvent | None = None
        if assessment.decision in (Decision.REVIEW, Decision.REJECT):
            event = self.events.create_event(txn, assessment)

        return {
            "assessment": assessment,
            "event": event,
        }

    def submit_transactions_batch(
        self, payload: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """整批补录历史交易：按各自发生时刻判定，整批原子落库。

        批内任一笔失败（账户不存在、发生时刻早于最早生效版本、
        重复/并发/同号异内容等）都整批拒绝，不留半截结论、不多出事件。
        """
        if not isinstance(payload, list) or not payload:
            raise RuleValidationError("补录批次必须是非空列表")

        txns: list[Transaction] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise RuleValidationError("批次中的每一笔都必须是对象(dict)")
            account_id = raw.get("account_id")
            self._check_account(account_id)
            txns.append(
                self._build_txn(
                    txn_id=raw["txn_id"],
                    account_id=account_id,
                    amount=raw["amount"],
                    currency=raw.get("currency", "CNY"),
                    counterparty=raw.get("counterparty", ""),
                    submitted_at=raw.get("submitted_at"),
                )
            )

        triples = self.engine.assess_batch(
            txns, event_builder=self.events.build_event
        )
        for txn, assessment, event in triples:
            if event is not None:
                logger.info(
                    "[EVENT-OPEN-BATCH] event_id=%s txn_id=%s rule_version=%s hits=%d",
                    event.event_id,
                    txn.txn_id,
                    assessment.rule_version,
                    len(assessment.hits),
                )
        return [
            {"assessment": assessment, "event": event}
            for _txn, assessment, event in triples
        ]

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
