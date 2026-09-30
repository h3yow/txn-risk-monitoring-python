"""风险判定引擎：去重/并发/内容冲突、按规则版本快照评估、历史补录与重放。

一致性保证：
- 评估资格通过 :meth:`RiskStore.claim_assessment` 原子占位，
  已完成结论且内容一致 -> ``DuplicateTransactionError``；
  在途占位且内容一致 -> ``ConcurrentSubmissionError``；
  同交易号但关键内容对不上 -> ``TransactionContentConflictError``，
  三种拒绝原因彼此可区分，且不会产生重复命中/计分/告警。
- 实时交易按当前已生效版本判定；补录交易（``assess_historical``）按交易
  自身发生时刻生效的版本判定，同批待落库交易进入 velocity 窗口，
  使批次首评与落库后重放结论一致。
- 结论中固化规则版本号与命中明细，规则版本只追加不改写，
  因此历史结论永不被后续规则升级影响，并可按当时版本原样重放。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.models import (
    Assessment,
    Decision,
    LIVE_SUBMISSION,
    Rule,
    RuleHit,
    RuleType,
    RuleVersion,
    RuleValidationError,
    Transaction,
    utcnow,
)
from app.store import RiskStore, resolve_txn_time

logger = logging.getLogger("risk.engine")

# 决策阈值：累计分值达到 REJECT 即拒绝；有命中但未到 REJECT 即人审
REVIEW_SCORE = 1
REJECT_SCORE = 100


class AssessmentEngine:
    def __init__(self, store: RiskStore) -> None:
        self.store = store

    # ------------------------------------------------------------ 公开入口

    def assess(self, txn: Transaction) -> Assessment:
        """评估一笔实时交易；重复/并发/内容冲突提交将被明确拒绝。

        实时交易按"当前时刻已生效的最新规则版本"判定，行为与补录不同：
        补录交易按交易自身的发生时刻选取规则版本（见
        :meth:`RiskMonitoringService.backfill_transactions`）。
        """
        self.store.claim_assessment(txn)
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
        if version is not None:
            rv = self.store.get_rule_version(version)
        elif txn.submitted_at == LIVE_SUBMISSION:
            # 实时交易：按重放时刻已生效的最新版本（通常与首评版本相同）
            rv = self.store.current_rule_version()
            if rv is None:
                raise RuleValidationError("当前没有任何生效的规则版本，无法重放")
        else:
            rv = self.store.effective_rule_version(txn.submitted_at)
        # 重放时交易已落库，时间窗查询会包含目标笔本身
        assessment = self._evaluate(
            txn, rv, include_current=False, extra=()
        )
        self._log_assessment(assessment, rv, prefix="REPLAY")
        return assessment

    def assess_historical(
        self,
        txn: Transaction,
        extra: tuple[Transaction, ...] = (),
    ) -> Assessment:
        """按交易自身发生时刻生效的规则版本评估一笔（补录）交易。

        ``extra`` 是同一补录批次中尚未落库的交易，用于 velocity 计数，
        保证批次首评的窗口与整批落库后单独重放的窗口完全一致。
        交易发生时刻早于最早生效版本时抛 ``NoEffectiveRuleVersionError``。
        """
        if txn.submitted_at == LIVE_SUBMISSION:
            raise RuleValidationError("补录交易必须显式给出真实发生时间 submitted_at")
        rv = self.store.effective_rule_version(txn.submitted_at)
        # extra 已含目标笔自身，统计口径与落库后重放完全一致，不再额外 +1
        return self._evaluate(txn, rv, include_current=False, extra=extra)

    # ------------------------------------------------------------ 评估内核

    def _evaluate(
        self,
        txn: Transaction,
        rv: RuleVersion,
        include_current: bool,
        extra: tuple[Transaction, ...] = (),
    ) -> Assessment:
        hits: list[RuleHit] = []
        score = 0
        for rule in rv.rules:
            if not rule.enabled:
                continue
            hit = self._check_rule(rule, txn, rv.version, include_current, extra)
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
        extra: tuple[Transaction, ...] = (),
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
            occurred_at = resolve_txn_time(txn)
            count = self.store.count_account_transactions(
                txn.account_id,
                after=occurred_at - window,
                at_or_before=occurred_at,
                extra=extra,
            )
            if include_current:
                count += 1  # 实时/补录首评时目标笔尚未落库
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

    def log_assessment(
        self, assessment: Assessment, prefix: str = "ASSESS"
    ) -> None:
        rv = self.store.get_rule_version(assessment.rule_version)
        self._log_assessment(assessment, rv, prefix=prefix)

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
