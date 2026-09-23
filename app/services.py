"""服务编排层：交易评估、规则版本管理、事件处置。"""
from __future__ import annotations

from app.engine import evaluate, validate_rules
from app.errors import (
    ConcurrentConflictError,
    DuplicateSubmissionError,
    NotFoundError,
    PayloadMismatchError,
)
from app.events import apply_transition
from app.logging_config import get_logger
from app.models import (
    Account,
    Decision,
    DecisionRecord,
    Event,
    EventStatus,
    RuleHit,
    RuleSetVersion,
    TransactionInput,
    utcnow,
)
from app.storage import Storage

_audit = get_logger()

# 非放行结论会生成可疑事件
_EVENT_DECISIONS = {Decision.REVIEW, Decision.BLOCK}


class RiskService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    # ---- 账户 ----
    def create_account(self, account: Account) -> Account:
        self.storage.upsert_account(account)
        _audit.info("account_registered account_id=%s country=%s risk=%s",
                    account.account_id, account.country, account.risk_level)
        return account

    # ---- 交易受理与去重 ----
    def submit_transaction(
        self,
        txn: TransactionInput,
        *,
        rule_version: int | None = None,
    ) -> DecisionRecord:
        # 1) 原子预留：区分并发冲突与重复提交
        outcome = self.storage.reserve_txn(txn)
        if outcome == "concurrent":
            _audit.warning("txn_concurrent_conflict txn_id=%s", txn.txn_id)
            raise ConcurrentConflictError(
                f"交易 {txn.txn_id} 正在被另一请求并发评估，请稍后查询既有结论",
                details={"txn_id": txn.txn_id},
            )
        if outcome == "duplicate":
            self._reject_duplicate(txn)  # 内部按载荷一致性区分异常类型

        # 2) 预留成功 -> 取规则版本并评估
        try:
            rule_set = (
                self.get_rule_version(rule_version)
                if rule_version is not None
                else self._require_latest_rules()
            )
            account = self.storage.get_account(txn.account_id)
            decision, score, hits = evaluate(txn, account, rule_set)

            event: Event | None = None
            event_id: str | None = None
            if decision in _EVENT_DECISIONS:
                event = Event(
                    event_id=self.storage.next_event_id(),
                    txn_id=txn.txn_id,
                    status=EventStatus.OPEN,
                    rule_version=rule_set.version,
                    hit_rule_ids=[h.rule_id for h in hits],
                    score=score,
                    decision=decision,
                )
                event.history.append({
                    "from": None,
                    "to": EventStatus.OPEN.value,
                    "operator": "system",
                    "note": "风险判定触发，自动建单",
                    "at": utcnow().isoformat(),
                })
                event_id = event.event_id

            record = DecisionRecord(
                txn_id=txn.txn_id,
                account_id=txn.account_id,
                amount=txn.amount,
                currency=txn.currency,
                country=txn.country,
                payee=txn.payee,
                decision=decision,
                score=score,
                hits=hits,
                rule_version=rule_set.version,
                evaluated_at=utcnow(),
                event_id=event_id,
            )
            # 3) 结论与事件在同一把锁内一起落库
            self.storage.save_decision(record, event)
        except BaseException:
            # 评估失败必须回滚预留，否则该 txn_id 会被永久锁死
            self.storage.release_txn(txn.txn_id)
            raise

        self._log_decision(record)
        return record

    def _reject_duplicate(self, txn: TransactionInput) -> None:
        existing = self.storage.get_txn_input(txn.txn_id)
        assert existing is not None
        same = (
            existing.account_id == txn.account_id
            and existing.amount == txn.amount
            and existing.currency == txn.currency
            and existing.country == txn.country
            and existing.payee == txn.payee
        )
        prior = self.storage.get_decision(txn.txn_id)
        version = prior.rule_version if prior else "-"
        if not same:
            _audit.warning(
                "txn_payload_mismatch txn_id=%s existing_version=%s", txn.txn_id, version
            )
            raise PayloadMismatchError(
                f"交易 {txn.txn_id} 已受理，但本次提交载荷与首次提交不一致，拒绝覆盖",
                details={
                    "txn_id": txn.txn_id,
                    "existing": {
                        "account_id": existing.account_id,
                        "amount": existing.amount,
                        "currency": existing.currency,
                        "country": existing.country,
                        "payee": existing.payee,
                    },
                },
            )
        _audit.warning(
            "txn_duplicate_submission txn_id=%s rule_version=%s", txn.txn_id, version
        )
        raise DuplicateSubmissionError(
            f"交易 {txn.txn_id} 已评估过，不得重复计分/告警",
            details={"txn_id": txn.txn_id, "rule_version": version},
        )

    def get_decision(self, txn_id: str) -> DecisionRecord:
        record = self.storage.get_decision(txn_id)
        if record is None:
            raise NotFoundError(
                f"交易 {txn_id} 不存在", details={"txn_id": txn_id}
            )
        return record

    # ---- 历史重放 ----
    def replay_transaction(
        self,
        txn_id: str,
        rule_version: int,
    ) -> tuple[Decision, int, list[RuleHit]]:
        """以历史输入按指定规则版本重新评估（纯计算，不改写既有结论/事件/计分）。"""
        txn = self.storage.get_txn_input(txn_id)
        if txn is None:
            raise NotFoundError(
                f"交易 {txn_id} 不存在，无法重放", details={"txn_id": txn_id}
            )
        rule_set = self.get_rule_version(rule_version)
        account = self.storage.get_account(txn.account_id)
        decision, score, hits = evaluate(txn, account, rule_set)
        stored = self.storage.get_decision(txn_id)
        self._log_replay(txn_id, rule_set.version, decision, score, hits, stored)
        return decision, score, hits

    # ---- 规则版本 ----
    def publish_rules(self, rules: list[dict], note: str = "") -> RuleSetVersion:
        # 先整批校验：非法直接抛错，不产生新版本，旧版本继续生效
        validated = validate_rules(rules)
        version_no = self.storage.next_rule_version()
        version = RuleSetVersion(
            version=version_no, rules=validated, published_at=utcnow(), note=note
        )
        self.storage.save_rule_version(version)
        _audit.info(
            "rules_published version=%d rule_count=%d note=%r",
            version_no, len(validated), note,
        )
        return version

    def list_rule_versions(self) -> list[RuleSetVersion]:
        return self.storage.list_rule_versions()

    def get_rule_version(self, version: int) -> RuleSetVersion:
        rule_set = self.storage.get_rule_version(version)
        if rule_set is None:
            raise NotFoundError(
                f"规则版本 v{version} 不存在",
                details={"rule_version": version},
            )
        return rule_set

    def _require_latest_rules(self) -> RuleSetVersion:
        rule_set = self.storage.latest_rule_version()
        if rule_set is None:
            raise NotFoundError(
                "当前没有任何已发布的规则版本", details={"rule_version": None}
            )
        return rule_set

    # ---- 事件处置 ----
    def get_event(self, event_id: str) -> Event:
        event = self.storage.get_event(event_id)
        if event is None:
            raise NotFoundError(
                f"事件 {event_id} 不存在", details={"event_id": event_id}
            )
        return event

    def transition_event(
        self,
        event_id: str,
        target: EventStatus,
        *,
        operator: str,
        note: str = "",
    ) -> Event:
        # 读-改在存储锁内完成，避免并发处置互相覆盖
        with self.storage.lock:
            event = self.get_event(event_id)
            before = event.status
            apply_transition(event, target, operator=operator, note=note)
            self.storage.save_event(event)
        _audit.info(
            "event_transition event_id=%s txn_id=%s %s->%s operator=%s rule_version=%s",
            event_id, event.txn_id, before.value, target.value, operator,
            event.rule_version,
        )
        return event

    # ---- 审计日志 ----
    @staticmethod
    def _log_decision(record: DecisionRecord) -> None:
        reasons = "; ".join(f"{h.rule_id}(v{h.rule_version},+{h.score}):{h.reason}"
                            for h in record.hits) or "无命中"
        _audit.info(
            "txn_evaluated txn_id=%s rule_version=v%s decision=%s score=%s "
            "event_id=%s basis=[%s]",
            record.txn_id, record.rule_version, record.decision.value,
            record.score, record.event_id, reasons,
        )

    @staticmethod
    def _log_replay(
        txn_id: str,
        version: int,
        decision: Decision,
        score: int,
        hits: list[RuleHit],
        stored: DecisionRecord | None,
    ) -> None:
        reasons = "; ".join(f"{h.rule_id}(+{h.score}):{h.reason}" for h in hits) or "无命中"
        stored_desc = (
            f"v{stored.rule_version}/{stored.decision.value}/{stored.score}"
            if stored else "无既有结论"
        )
        _audit.info(
            "txn_replayed txn_id=%s replay_rule_version=v%s replay_decision=%s "
            "replay_score=%s stored=%s basis=[%s]",
            txn_id, version, decision.value, score, stored_desc, reasons,
        )
