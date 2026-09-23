"""线程安全的本地内存存储（模拟账户、交易、规则版本、结论、事件数据）。

所有变更均在同一把 RLock 下完成，保证判定入口与并发提交下的一致性。
规则版本采用追加式（append-only）保存，历史版本永不就地修改，
这是"规则升级不改变历史结论、可按当时版本重放"的基础。
"""

from __future__ import annotations

import threading
from datetime import datetime

from app.models import (
    Account,
    Assessment,
    ConcurrentSubmissionError,
    DuplicateTransactionError,
    Rule,
    RuleValidationError,
    RuleVersion,
    SuspiciousEvent,
    Transaction,
    utcnow,
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

    def create_account(self, account: Account) -> Account:
        with self._lock:
            self.accounts[account.account_id] = account
            return account

    def get_account(self, account_id: str) -> Account | None:
        with self._lock:
            return self.accounts.get(account_id)

    # ------------------------------------------------------------ 评估占位

    def claim_assessment(self, txn_id: str) -> None:
        """占位一笔交易的评估资格。

        - 已有完成结论 -> DuplicateTransactionError
        - 已有进行中占位 -> ConcurrentSubmissionError
        二者调用方可据此给出可区分的拒绝原因。
        """
        with self._lock:
            if txn_id in self.assessments:
                raise DuplicateTransactionError(
                    f"交易 {txn_id} 已完成评估，重复提交被拒绝"
                )
            if txn_id in self.in_flight:
                raise ConcurrentSubmissionError(
                    f"交易 {txn_id} 正在评估中，并发提交被拒绝"
                )
            self.in_flight.add(txn_id)

    def release_assessment(self, txn_id: str) -> None:
        with self._lock:
            self.in_flight.discard(txn_id)

    def commit_assessment(self, txn: Transaction, assessment: Assessment) -> None:
        """原子地落库交易与结论，并释放占位。"""
        with self._lock:
            self.transactions.setdefault(txn.txn_id, txn)
            self.assessments[txn.txn_id] = assessment
            self.in_flight.discard(txn.txn_id)

    def get_assessment(self, txn_id: str) -> Assessment | None:
        with self._lock:
            return self.assessments.get(txn_id)

    def get_transaction(self, txn_id: str) -> Transaction | None:
        with self._lock:
            return self.transactions.get(txn_id)

    def count_account_transactions(
        self, account_id: str, after: datetime, at_or_before: datetime
    ) -> int:
        """统计账户在 (after, at_or_before] 时间窗内提交的交易数（含目标笔）。

        仅依据 submitted_at，因此在任意时刻重放历史交易时，
        窗口都不会包含目标交易之后的交易，结论可复现。
        """
        with self._lock:
            return sum(
                1
                for txn in self.transactions.values()
                if txn.account_id == account_id
                and after < txn.submitted_at <= at_or_before
            )

    # ------------------------------------------------------------ 规则版本

    def append_rule_version(
        self, rules: tuple[Rule, ...], note: str = "", created_at: datetime | None = None
    ) -> RuleVersion:
        """追加一个新版本；版本号单调递增，旧版本保持不变。"""
        with self._lock:
            version = len(self.rule_versions) + 1
            rv = RuleVersion(
                version=version,
                rules=tuple(rules),
                created_at=created_at or utcnow(),
                note=note,
            )
            self.rule_versions.append(rv)
            return rv

    def current_rule_version(self) -> RuleVersion | None:
        with self._lock:
            return self.rule_versions[-1] if self.rule_versions else None

    def get_rule_version(self, version: int) -> RuleVersion:
        with self._lock:
            if 1 <= version <= len(self.rule_versions):
                return self.rule_versions[version - 1]
            raise RuleValidationError(f"规则版本 {version} 不存在")

    def effective_rule_version(self, at: datetime) -> RuleVersion:
        """返回某时刻生效的规则版本（created_at <= at 的最新版本）。"""
        with self._lock:
            chosen: RuleVersion | None = None
            for rv in self.rule_versions:
                if rv.created_at <= at and (chosen is None or rv.version > chosen.version):
                    chosen = rv
            if chosen is None:
                raise RuleValidationError(f"时刻 {at.isoformat()} 没有生效的规则版本")
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
