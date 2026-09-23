"""风险判定引擎：去重、并发冲突、按规则版本快照评估、历史重放。

一致性保证：
- 评估资格通过 :meth:`RiskStore.claim_assessment` 原子占位，
  已完成结论 -> ``DuplicateTransactionError``；进行中 -> ``ConcurrentSubmissionError``，
  两种拒绝原因可区分，且不会产生重复命中/计分/告警。
- 结论中固化规则版本号与命中明细，规则版本只追加不改写，
  因此历史结论永不被后续规则升级影响，并可按当时版本原样重放。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.models import (
    Assessment,
    Decision,
    Rule,
    RuleHit,
    RuleType,
    RuleVersion,
    RuleValidationError,
    Transaction,
    utcnow,
)
from app.store import RiskStore

logger = logging.getLogger("risk.engine")

# 决策阈值：累计分值达到 REJECT 即拒绝；有命中但未到 REJECT 即人审
REVIEW_SCORE = 1
REJECT_SCORE = 100


class AssessmentEngine:
    def __init__(self, store: RiskStore) -> None:
        self.store = store

    # ------------------------------------------------------------ 公开入口

    def assess(self, txn: Transaction) -> Assessment:
        """评估一笔交易；重复/并发提交将被明确拒绝。"""
        self.store.claim_assessment(txn.txn_id)
        try:
            rv = self.store.current_rule_version()
            if rv is None:
                raise RuleValidationError("当前没有任何生效的规则版本，无法评估")
            assessment = self._evaluate(txn, rv, include_current=True)
            self.store.commit_assessment(txn, assessment)
            self._log_assessment(assessment, rv)
            return assessment
        except BaseException:
            # 评估失败必须释放占位，否则同笔交易将被永久误判为"并发中"
            self.store.release_assessment(txn.txn_id)
            raise

    def replay(self, txn_id: str, version: int | None = None) -> Assessment:
        """按指定（或当时生效的）规则版本复现历史交易的判定结果。

        - ``version`` 为 None：取交易提交时刻生效的规则版本（与首次评估一致）。
        - 显式传入版本号：可用新版本复算以展示升级带来的差异，
          但不会修改已落库的历史结论。
        """
        txn = self.store.get_transaction(txn_id)
        if txn is None:
            raise RuleValidationError(f"交易 {txn_id} 不存在，无法重放")
        rv = (
            self.store.get_rule_version(version)
            if version is not None
            else self.store.effective_rule_version(txn.submitted_at)
        )
        # 重放时交易已落库，时间窗查询会包含目标笔本身
        assessment = self._evaluate(txn, rv, include_current=False)
        self._log_assessment(assessment, rv, prefix="REPLAY")
        return assessment

    # ------------------------------------------------------------ 评估内核

    def _evaluate(
        self, txn: Transaction, rv: RuleVersion, include_current: bool
    ) -> Assessment:
        hits: list[RuleHit] = []
        score = 0
        for rule in rv.rules:
            if not rule.enabled:
                continue
            hit = self._check_rule(rule, txn, rv.version, include_current)
            if hit is not None:
                hits.append(hit)
                score += int(rule.params["score"])

        if score >= REJECT_SCORE:
            decision = Decision.REJECT
        elif score >= REVIEW_SCORE:
            decision = Decision.REVIEW
        else:
            decision = Decision.PASS

        return Assessment(
            txn_id=txn.txn_id,
            decision=decision,
            score=score,
            rule_version=rv.version,
            hits=tuple(hits),
            assessed_at=utcnow(),
        )

    def _check_rule(
        self,
        rule: Rule,
        txn: Transaction,
        version: int,
        include_current: bool,
    ) -> RuleHit | None:
        p = rule.params
        if rule.rule_type == RuleType.AMOUNT_LIMIT:
            if txn.amount > p["limit"]:
                return RuleHit(
                    rule.rule_id,
                    rule.rule_type,
                    version,
                    f"金额 {txn.amount} {txn.currency} 超过限额 {p['limit']}",
                )
        elif rule.rule_type == RuleType.BLOCKLIST:
            if txn.counterparty in p["counterparties"]:
                return RuleHit(
                    rule.rule_id,
                    rule.rule_type,
                    version,
                    f"交易对手 {txn.counterparty} 命中黑名单",
                )
        elif rule.rule_type == RuleType.VELOCITY:
            window = timedelta(minutes=int(p["window_minutes"]))
            count = self.store.count_account_transactions(
                txn.account_id,
                after=txn.submitted_at - window,
                at_or_before=txn.submitted_at,
            )
            if include_current:
                count += 1  # 实时评估时目标笔尚未落库
            if count > p["max_count"]:
                return RuleHit(
                    rule.rule_id,
                    rule.rule_type,
                    version,
                    (
                        f"账户 {txn.account_id} 在 {p['window_minutes']} 分钟内"
                        f"交易 {count} 笔，超过阈值 {p['max_count']}"
                    ),
                )
        return None

    @staticmethod
    def _log_assessment(
        assessment: Assessment, rv: RuleVersion, prefix: str = "ASSESS"
    ) -> None:
        basis = (
            "; ".join(
                f"{h.rule_id}(v{h.rule_version}, {h.rule_type.value}): {h.reason}"
                for h in assessment.hits
            )
            or "无规则命中"
        )
        logger.info(
            "[%s] txn_id=%s rule_version=%s decision=%s score=%s basis=[%s]",
            prefix,
            assessment.txn_id,
            rv.version,
            assessment.decision.value,
            assessment.score,
            basis,
        )
