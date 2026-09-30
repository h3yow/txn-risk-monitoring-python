"""线程安全的本地内存存储（模拟账户、交易、规则版本、结论、事件数据）。

所有变更均在同一把 RLock 下完成，保证判定入口与并发提交下的一致性。
规则版本采用追加式（append-only）保存，历史版本永不就地修改，
这是"规则升级不改变历史结论、可按当时版本重放"的基础。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

from app.models import (
    Account,
    Assessment,
    ConcurrentSubmissionError,
    DuplicateTransactionError,
    NoEffectiveRuleVersionError,
    Rule,
    RuleEffectiveTimeConflictError,
    RuleValidationError,
    RuleVersion,
    SuspiciousEvent,
    Transaction,
    TransactionContentMismatchError,
    ensure_utc,
    utcnow,
)


def _same_identity(a: Transaction, b: Transaction) -> bool:
    """同一交易号两次提交的关键业务内容是否一致（幂等判定）。

    比较影响风险结论的交易内容：账户、金额、币种、交易对手。
    ``submitted_at`` 是"发生时间"这一评估属性（决定适用的规则版本），
    实时提交缺省取当前时间，两次幂等重试之间会相差几微秒，属正常现象，
    因此不作为交易身份的一部分参与比对。
    """
    return (
        a.account_id == b.account_id
        and a.amount == b.amount
        and a.currency == b.currency
        and a.counterparty == b.counterparty
    )


class RiskStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.transactions: dict[str, Transaction] = {}
        # 已完成的评估结论（每笔交易至多一条）
        self.assessments: dict[str, Assessment] = {}
        # 进行中的评估占位，用于并发提交冲突检测
        self.in_flight: set[str] = set()
        # 规则版本（追加式，永不就地修改）
        self.rule_versions: list[RuleVersion] = []
        # 可疑事件
        self.events: dict[str, SuspiciousEvent] = {}
        self._event_seq = 0

    # ------------------------------------------------------------ 账户

    @contextmanager
    def batch_lock(self):
        """整批评估的原子区：占位、评估、事件构造、整批落库在同一把锁内完成。

        RLock 可重入，临界区内调用 claim / count / next_event_id 等
        同样会获取本锁的方法不会死锁。单笔实时评估不进入该临界区，
        保持既有的"占位即报并发冲突"行为不变。
        """
        with self._lock:
            yield

    def create_account(self, account: Account) -> Account:
        with self._lock:
            self.accounts[account.account_id] = account
            return account

    def get_account(self, account_id: str) -> Account | None:
        with self._lock:
            return self.accounts.get(account_id)

    # ------------------------------------------------------------ 评估占位

    def claim_assessment(self, txn: Transaction) -> None:
        """占位一笔交易的评估资格。

        - 已有完成结论且关键内容一致 -> DuplicateTransactionError（重复提交）
        - 已有完成结论但关键内容不一致 -> TransactionContentMismatchError
          （同号不同内容，与单纯重复可区分）
        - 已有进行中占位 -> ConcurrentSubmissionError（并发提交）
        三种拒绝原因彼此可区分。
        """
        with self._lock:
            existing = self.transactions.get(txn.txn_id)
            if existing is not None:
                if not _same_identity(existing, txn):
                    raise TransactionContentMismatchError(
                        f"交易 {txn.txn_id} 已存在且关键内容与本次提交不一致，拒绝评估"
                    )
                raise DuplicateTransactionError(
                    f"交易 {txn.txn_id} 已完成评估，重复提交被拒绝"
                )
            if txn.txn_id in self.assessments:
                # 防御：有结论没交易的情况不应该出现，按重复提交处理
                raise DuplicateTransactionError(
                    f"交易 {txn.txn_id} 已完成评估，重复提交被拒绝"
                )
            if txn.txn_id in self.in_flight:
                raise ConcurrentSubmissionError(
                    f"交易 {txn.txn_id} 正在评估中，并发提交被拒绝"
                )
            self.in_flight.add(txn.txn_id)

    def release_assessment(self, txn_id: str) -> None:
        with self._lock:
            self.in_flight.discard(txn_id)

    def release_many(self, txn_ids: list[str]) -> None:
        with self._lock:
            for txn_id in txn_ids:
                self.in_flight.discard(txn_id)

    def commit_assessment(self, txn: Transaction, assessment: Assessment) -> None:
        """原子地落库交易与结论，并释放占位。"""
        with self._lock:
            self.transactions.setdefault(txn.txn_id, txn)
            self.assessments[txn.txn_id] = assessment
            self.in_flight.discard(txn.txn_id)

    def commit_many(
        self,
        items: list[tuple[Transaction, Assessment]],
        events: list[SuspiciousEvent] | None = None,
    ) -> None:
        """整批原子落库：要么所有交易+结论+事件全部可见，要么一个都不可见。

        事件 id 在进入临界区前已分配（:meth:`next_event_id` 本身加锁），
        此处在同一把锁内一次性写入，杜绝批次中途失败留下半截结论/事件。
        """
        with self._lock:
            for txn, assessment in items:
                self.transactions.setdefault(txn.txn_id, txn)
                self.assessments[txn.txn_id] = assessment
                self.in_flight.discard(txn.txn_id)
            for event in events or []:
                self.events.setdefault(event.event_id, event)

    def get_assessment(self, txn_id: str) -> Assessment | None:
        with self._lock:
            return self.assessments.get(txn_id)

    def get_transaction(self, txn_id: str) -> Transaction | None:
        with self._lock:
            return self.transactions.get(txn_id)

    def count_account_transactions(
        self,
        account_id: str,
        after: datetime,
        at_or_before: datetime,
        pending: list[Transaction] | None = None,
    ) -> int:
        """统计账户在 (after, at_or_before] 时间窗内、发生时间不晚于该时刻的交易数。

        仅依据 submitted_at，因此在任意时刻重放历史交易时，
        窗口都不会包含目标交易之后的交易，结论可复现。

        ``pending`` 为当前批次已占位、尚未落库的交易：补录整批评估时，
        velocity 的计数必须把批内其他交易也算进去，才能保证"整批首次结论"
        与"逐笔单独重放"完全一致。计数口径与重放时（全部交易均已落库）相同。
        """
        with self._lock:
            committed = sum(
                1
                for txn in self.transactions.values()
                if txn.account_id == account_id
                and after < txn.submitted_at <= at_or_before
            )
        extra = sum(
            1
            for txn in (pending or [])
            if txn.account_id == account_id
            and after < txn.submitted_at <= at_or_before
        )
        return committed + extra

    # ------------------------------------------------------------ 规则版本

    def append_rule_version(
        self, rules: tuple[Rule, ...], note: str = "", effective_at: datetime | None = None
    ) -> RuleVersion:
        """追加一个新版本；版本号单调递增，旧版本保持不变。

        生效时间语义：新版本的 ``effective_at`` 必须**严格晚于**所有已有版本
        （包括与某个已有版本同一时刻的情况）。否则历史结论将无法说清，
        抛 :class:`RuleEffectiveTimeConflictError`，整批拒绝、不产生新版本。
        该拒绝原因与"规则内容非法"（:class:`RuleValidationError`）明确区分。
        """
        effective_at = ensure_utc(effective_at or utcnow())
        with self._lock:
            if self.rule_versions:
                latest = max(rv.effective_at for rv in self.rule_versions)
                if effective_at <= latest:
                    raise RuleEffectiveTimeConflictError(
                        f"新版本生效时间 {effective_at.isoformat()} 不晚于已有版本的"
                        f"最晚生效时间 {latest.isoformat()}，整批拒绝"
                    )
            version = len(self.rule_versions) + 1
            rv = RuleVersion(
                version=version,
                rules=tuple(rules),
                effective_at=effective_at,
                note=note,
            )
            self.rule_versions.append(rv)
            return rv

    def current_rule_version(self) -> RuleVersion | None:
        """当前时刻生效的版本；允许发布"未来生效"版本时，当前可能为 None。"""
        return self.effective_rule_version_safe(utcnow())

    def effective_rule_version_safe(self, at: datetime) -> RuleVersion | None:
        """同 :meth:`effective_rule_version`，但无生效版本时返回 None 而非抛错。"""
        with self._lock:
            chosen: RuleVersion | None = None
            for rv in self.rule_versions:
                if rv.effective_at <= at and (chosen is None or rv.version > chosen.version):
                    chosen = rv
            return chosen

    def get_rule_version(self, version: int) -> RuleVersion:
        with self._lock:
            if 1 <= version <= len(self.rule_versions):
                return self.rule_versions[version - 1]
            raise RuleValidationError(f"规则版本 {version} 不存在")

    def effective_rule_version(self, at: datetime) -> RuleVersion:
        """返回某时刻生效的规则版本（effective_at <= at 的最新版本）。"""
        with self._lock:
            chosen: RuleVersion | None = None
            for rv in self.rule_versions:
                if rv.effective_at <= at and (chosen is None or rv.version > chosen.version):
                    chosen = rv
            if chosen is None:
                raise NoEffectiveRuleVersionError(
                    f"时刻 {at.isoformat()} 早于最早生效的规则版本，没有可适用的规则"
                )
            return chosen

    # ------------------------------------------------------------ 可疑事件

    def next_event_id(self) -> str:
        with self._lock:
            self._event_seq += 1
            return f"EVT-{self._event_seq:06d}"

    def save_event(self, event: SuspiciousEvent) -> SuspiciousEvent:
        with self._lock:
            self.events[event.event_id] = event
            return event

    def get_event(self, event_id: str) -> SuspiciousEvent | None:
        with self._lock:
            return self.events.get(event_id)
