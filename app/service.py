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
    DuplicateTransactionError,
    LIVE_SUBMISSION,
    RuleValidationError,
    SuspiciousEvent,
    Transaction,
    TransactionContentConflictError,
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
        effective_at: datetime | None = None,
    ) -> int:
        """校验并发布一批规则为新版本。

        两层拒绝原因彼此可区分：
        - 规则内容非法 -> ``RuleValidationError``（整批拒绝、不产生新版本）；
        - 内容合法但生效时间不晚于已有最新版本的生效时间（更早或撞点）
          -> ``RuleEffectiveTimeConflictError``（同样整批拒绝，不挑版本凑合）。
        当前生效版本在两种情况下都保持不变。返回新版本号。
        """
        # 先完整校验内容：失败时 validate_rules 不会触碰 store
        rules = validate_rules(payload)
        # 再校验生效时间语义（append 时在锁内做单调检查）
        rv = self.store.append_rule_version(
            rules, note=note, created_at=created_at, effective_at=effective_at
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
        # 实时交易：未给发生时间即用 LIVE_SUBMISSION 哨兵（同一笔的并发/重复
        # 提交在内容比对时完全一致）；补录走 backfill_transactions，必须显式给时。
        txn = Transaction(
            txn_id=txn_id,
            account_id=account_id,
            amount=float(amount),
            currency=currency,
            counterparty=counterparty,
            submitted_at=submitted_at or LIVE_SUBMISSION,
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

    # ------------------------------------------------------------ 历史补录

    def backfill_transactions(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """整批补录历史交易：每笔按其自身发生时刻生效的规则版本判定。

        保证：
        - 首评结论与整批落库后单独 ``replay_transaction`` 的结果完全一致
          （规则版本、命中规则、分值、结论；velocity 窗口对同批待落库交易可见）；
        - 全批原子：任一笔算不下去（重复/并发/内容冲突/账户缺失/发生时刻
          早于最早生效版本/批次内交易号冲突），整批拒绝，不落任何结论与事件；
        - 不触碰任何已存在的交易结论与事件。

        每一项必须显式给出 ``submitted_at``（交易发生时刻）。
        """
        if not isinstance(items, list) or not items:
            raise RuleValidationError("补录批次必须是非空列表")

        # ---- 阶段 0：构造与静态校验（不落库、不占位）----
        txns: list[Transaction] = []
        seen_in_batch: dict[str, Transaction] = {}
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise RuleValidationError(f"backfill[{index}] 必须是对象(dict)")
            missing = [
                k for k in ("txn_id", "account_id", "amount", "submitted_at")
                if raw.get(k) is None
            ]
            if missing:
                raise RuleValidationError(
                    f"backfill[{index}] 缺少必填字段: {missing}"
                )
            if not isinstance(raw["submitted_at"], datetime):
                raise RuleValidationError(
                    f"backfill[{index}].submitted_at 必须是 ISO 时间，实际为 "
                    f"{raw['submitted_at']!r}"
                )
            amount = raw["amount"]
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
                raise RuleValidationError(
                    f"backfill[{index}].amount 必须是正数，实际为 {amount!r}"
                )
            if self.store.get_account(raw["account_id"]) is None:
                raise RuleValidationError(
                    f"backfill[{index}] 账户 {raw['account_id']} 不存在"
                )
            txn = Transaction(
                txn_id=raw["txn_id"],
                account_id=raw["account_id"],
                amount=float(raw["amount"]),
                currency=raw.get("currency", "CNY"),
                counterparty=raw.get("counterparty", ""),
                submitted_at=raw["submitted_at"],
            )
            prior = seen_in_batch.get(txn.txn_id)
            if prior is not None:
                if RiskStore.same_content(prior, txn):
                    raise DuplicateTransactionError(
                        f"补录批次内交易 {txn.txn_id} 重复出现且内容相同"
                    )
                raise TransactionContentConflictError(
                    f"补录批次内交易 {txn.txn_id} 重复出现但关键内容不一致"
                )
            seen_in_batch[txn.txn_id] = txn
            txns.append(txn)

        # ---- 阶段 1：逐笔占位 + 按发生时刻评估（任一失败整批回滚占位）----
        claimed: list[str] = []
        try:
            for txn in txns:
                # claim 原子区分：重复 / 并发 / 内容冲突
                self.store.claim_assessment(txn)
                claimed.append(txn.txn_id)
            pending = tuple(txns)
            records = []
            for txn in txns:
                assessment = self.engine.assess_historical(txn, extra=pending)
                event = (
                    self.events.build_event(txn, assessment)
                    if assessment.decision in (Decision.REVIEW, Decision.REJECT)
                    else None
                )
                records.append((txn, assessment, event))
        except BaseException:
            for txn_id in claimed:
                self.store.release_assessment(txn_id)
            raise

        # ---- 阶段 2：单次临界区整批原子落库（交易+结论+事件）----
        try:
            self.store.commit_batch(records)
        except BaseException:
            # 理论上 commit_batch 自身不写半成品，这里兜底释放全部占位
            for txn_id in claimed:
                self.store.release_assessment(txn_id)
            raise
        for _txn, assessment, _event in records:
            self.engine.log_assessment(assessment, prefix="BACKFILL")
        logger.info("[BACKFILL-BATCH] size=%d 全部落库", len(records))
        return [
            {"assessment": assessment, "event": event}
            for _txn, assessment, event in records
        ]

    def get_assessment(self, txn_id: str) -> Assessment | None:
        return self.store.get_assessment(txn_id)

    # ------------------------------------------------------------ 事件处置

    def handle_event(
        self, event_id: str, action: str, operator: str, note: str = ""
    ) -> SuspiciousEvent:
        return self.events.act(event_id, action, operator, note)

    def get_event(self, event_id: str) -> SuspiciousEvent | None:
        return self.store.get_event(event_id)
