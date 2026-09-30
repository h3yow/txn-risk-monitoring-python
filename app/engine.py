"""风险判定引擎：去重、并发冲突、按规则版本快照评估、历史重放、补录整批评估。

一致性保证：
- 评估资格通过 :meth:`RiskStore.claim_assessment` 原子占位，
  已完成结论且内容一致 -> ``DuplicateTransactionError``；
  同号但关键内容不一致 -> ``TransactionContentMismatchError``；
  进行中 -> ``ConcurrentSubmissionError``，三种拒绝原因可区分，
  且不会产生重复命中/计分/告警。
- **按交易发生时刻取版本**：无论实时提交还是补录，首评都使用
  ``submitted_at`` 时刻生效的规则版本，因此首评结论与之后单独重放
  这笔交易得到的结论完全一致（版本、命中规则、分值、决策对得上）。
  发生时间早于最早生效版本时抛 ``NoEffectiveRuleVersionError``，
  不拿最新规则硬套。
- velocity 计数统一为"窗口内、发生时间不晚于目标笔"的口径；实时单笔
  把自身作为 pending 传入，补录整批把批内全部交易作为 pending 传入，
  与重放时（全部已落库）的口径一致，保证结论可复现。
- 结论中固化规则版本号与命中明细，规则版本只追加不改写，
  历史结论永不被后续规则升级影响，并可按当时版本原样重放。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from app.models import (
    Assessment,
    Decision,
    DuplicateTransactionError,
    Rule,
    RuleHit,
    RuleType,
    RuleValidationError,
    RuleVersion,
    SuspiciousEvent,
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
        """评估一笔交易（实时或单笔补录）；重复/并发/同号异内容均被明确拒绝。"""
        self.store.claim_assessment(txn)
        try:
            rv = self.store.effective_rule_version(txn.submitted_at)
            assessment = self._evaluate(txn, rv, pending=[txn])
            self.store.commit_assessment(txn, assessment)
            self._log_assessment(assessment, rv)
            return assessment
        except BaseException:
            # 评估失败必须释放占位，否则同笔交易将被永久误判为"并发中"
            self.store.release_assessment(txn.txn_id)
            raise

    def assess_batch(
        self,
        txns: list[Transaction],
        event_builder: Callable[[Transaction, Assessment], SuspiciousEvent] | None = None,
    ) -> list[tuple[Transaction, Assessment, SuspiciousEvent | None]]:
        """整批评估（补录一批历史交易），整批原子：要么全落库，要么一笔不落。

        - 批内同号（无论内容是否一致）：按重复提交拒绝整批；
        - 与已落库交易冲突：重复 / 同号异内容 / 与进行中提交并发，
          三种原因彼此可区分；
        - 任一笔发生时刻早于最早生效版本或评估失败：整批不留任何
          结论与事件（占位全部释放）。

        ``event_builder`` 由服务层提供：在同一原子区内构造 review/reject
        的可疑事件，保证"半截结论/凭空事件"不可能跨出临界区可见。
        """
        results: list[tuple[Transaction, Assessment, SuspiciousEvent | None]] = []
        claimed: list[str] = []
        with self.store.batch_lock():
            try:
                seen: set[str] = set()
                for txn in txns:
                    if txn.txn_id in seen:
                        # 批内重复交易号：与"跨批重复"同一拒绝原因
                        raise DuplicateTransactionError(
                            f"交易 {txn.txn_id} 在同一批次内重复出现，整批拒绝"
                        )
                    seen.add(txn.txn_id)
                    self.store.claim_assessment(txn)
                    claimed.append(txn.txn_id)

                for txn in txns:
                    rv = self.store.effective_rule_version(txn.submitted_at)
                    assessment = self._evaluate(txn, rv, pending=txns)
                    event = (
                        event_builder(txn, assessment)
                        if event_builder is not None
                        and assessment.decision in (Decision.REVIEW, Decision.REJECT)
                        else None
                    )
                    results.append((txn, assessment, event))

                self.store.commit_many(
                    [(txn, a) for txn, a, _e in results],
                    [e for _t, _a, e in results if e is not None],
                )
            except BaseException:
                self.store.release_many(claimed)
                raise

        for _txn, assessment, _event in results:
            self._log_assessment(assessment, None, prefix="ASSESS-BATCH")
        return results

    def replay(self, txn_id: str, version: int | None = None) -> Assessment:
        """按指定（或当时生效的）规则版本复现历史交易的判定结果。

        - ``version`` 为 None：取交易发生时刻生效的规则版本（与首次评估一致）。
        - 显式传入版本号：可用新版本复算以展示差异，
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
        # 重放时目标笔及其同批补录的交易均已落库，计数口径与首评一致
        assessment = self._evaluate(txn, rv, pending=None)
        self._log_assessment(assessment, rv, prefix="REPLAY")
        return assessment

    # ------------------------------------------------------------ 评估内核

    def _evaluate(
        self,
        txn: Transaction,
        rv: RuleVersion,
        pending: list[Transaction] | None,
    ) -> Assessment:
        hits: list[RuleHit] = []
        score = 0
        for rule in rv.rules:
            if not rule.enabled:
                continue
            hit = self._check_rule(rule, txn, rv.version, pending)
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
        pending: list[Transaction] | None,
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
                pending=pending,
            )
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
        assessment: Assessment, rv: RuleVersion | None, prefix: str = "ASSESS"
    ) -> None:
        basis = (
            "; ".join(
                f"{h.rule_id}(v{h.rule_version}, {h.rule_type.value}): {h.reason}"
                for h in assessment.hits
            )
            or "无规则命中"
        )
        version = rv.version if rv is not None else assessment.rule_version
        logger.info(
            "[%s] txn_id=%s rule_version=%s decision=%s score=%s basis=[%s]",
            prefix,
            assessment.txn_id,
            version,
            assessment.decision.value,
            assessment.score,
            basis,
        )
