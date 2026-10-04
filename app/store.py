"""线程安全的本地内存存储（模拟账户、交易、规则版本、结论、事件数据）。

所有变更均在同一把 RLock 下完成，保证判定入口与并发提交下的一致性。
规则版本采用追加式（append-only）保存，历史版本永不就地修改，
这是"规则升级不改变历史结论、可按当时版本重放"的基础。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.models import (
    Account,
    Assessment,
    ConcurrentSubmissionError,
    DuplicateTransactionError,
    LIVE_SUBMISSION,
    NoEffectiveRuleVersionError,
    Rule,
    RuleEffectiveTimeConflictError,
    RuleValidationError,
    RuleVersion,
    SuspiciousEvent,
    Transaction,
    TransactionContentConflictError,
    utcnow,
)


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 按 UTC 处理，保证跨时区/无时区输入的比较口径一致。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _txn_time(txn: Transaction) -> datetime:
    """交易用于判定/计数的发生时刻；实时哨兵取当前时刻。

    注意：对实时交易本函数每次调用都返回不同的"当前时刻"，只适用于
    评估入口一次性解析；已落库交易的计数/重放必须改用
    :meth:`RiskStore.occurred_time` 读取首评时冻结的时刻。
    """
    return utcnow() if txn.submitted_at == LIVE_SUBMISSION else _as_utc(txn.submitted_at)


def resolve_txn_time(txn: Transaction) -> datetime:
    """交易发生时刻的公开解析入口：实时交易 -> 当前 UTC 时刻。"""
    return _txn_time(txn)


class RiskStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.transactions: dict[str, Transaction] = {}
        # 已完成的评估结论（每笔交易至多一条）
        self.assessments: dict[str, Assessment] = {}
        # 进行中的评估占位：txn_id -> 提交内容，用于并发冲突与内容一致性检测
        self.in_flight: dict[str, Transaction] = {}
        # 已落库交易首评时冻结的发生时刻：txn_id -> occurred_at。
        # 实时交易的 submitted_at 是哨兵值，若不冻结，每次计数都取新的
        # utcnow()，会导致已落库实时交易永远落在窗口右边界之外（数不进窗口），
        # 且重放窗口随重放时刻滑动、与首评对不上。
        self.occurred_at: dict[str, datetime] = {}
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

    @staticmethod
    def same_content(a: Transaction, b: Transaction) -> bool:
        """同一交易号的两次提交关键内容是否一致。

        比对字段：账户、金额、币种、交易对手、交易发生时间（统一到 UTC，
        因此时区表示不同但时刻相同的输入视为一致；两笔都是"实时"提交时
        视为同一时刻，不因 utcnow 的微秒差误判为内容冲突）。
        """
        if not (
            a.account_id == b.account_id
            and a.amount == b.amount
            and a.currency == b.currency
            and a.counterparty == b.counterparty
        ):
            return False
        if a.submitted_at == LIVE_SUBMISSION or b.submitted_at == LIVE_SUBMISSION:
            return a.submitted_at == b.submitted_at
        return _as_utc(a.submitted_at) == _as_utc(b.submitted_at)

    def claim_assessment(self, txn: Transaction) -> None:
        """占位一笔交易的评估资格。

        - 已有完成结论且内容一致 -> DuplicateTransactionError
        - 已有完成结论但关键内容对不上 -> TransactionContentConflictError
        - 已有进行中占位且内容一致 -> ConcurrentSubmissionError
        - 已有进行中占位但内容对不上 -> TransactionContentConflictError
        调用方可据此给出彼此可区分的拒绝原因。
        """
        with self._lock:
            done = self.transactions.get(txn.txn_id)
            if done is not None:
                if self.same_content(done, txn):
                    raise DuplicateTransactionError(
                        f"交易 {txn.txn_id} 已完成评估，重复提交被拒绝"
                    )
                raise TransactionContentConflictError(
                    f"交易 {txn.txn_id} 已完成评估，但提交内容与原提交不一致"
                )
            holder = self.in_flight.get(txn.txn_id)
            if holder is not None:
                if self.same_content(holder, txn):
                    raise ConcurrentSubmissionError(
                        f"交易 {txn.txn_id} 正在评估中，并发提交被拒绝"
                    )
                raise TransactionContentConflictError(
                    f"交易 {txn.txn_id} 正在评估中，但提交内容与在途提交不一致"
                )
            self.in_flight[txn.txn_id] = txn

    def release_assessment(self, txn_id: str) -> None:
        with self._lock:
            self.in_flight.pop(txn_id, None)

    def commit_assessment(
        self,
        txn: Transaction,
        assessment: Assessment,
        occurred_at: datetime | None = None,
    ) -> None:
        """原子地落库交易与结论，并释放占位。

        ``occurred_at`` 为首评时一次性解析的发生时刻（实时交易即评估入口的
        utcnow()），随交易一起冻结，供后续窗口计数与重放使用。
        """
        with self._lock:
            self.transactions.setdefault(txn.txn_id, txn)
            self.assessments[txn.txn_id] = assessment
            self.occurred_at[txn.txn_id] = _as_utc(occurred_at or _txn_time(txn))
            self.in_flight.pop(txn.txn_id, None)

    def commit_batch(
        self, records: list[tuple[Transaction, Assessment, SuspiciousEvent | None]]
    ) -> None:
        """原子地整批落库：交易+结论（+事件）要么全部写入，要么一个都不写。

        调用前必须已通过 claim 占位；本方法只负责单次临界区内的最终写入，
        失败（如编号分配异常）时所有 dict 变更随异常回滚，不留半截结论。
        """
        with self._lock:
            # 预分配事件编号：若中途异常，尚未写入任何正式数据
            prepared: list[tuple[Transaction, Assessment, SuspiciousEvent | None]] = []
            for txn, assessment, event in records:
                if event is not None and not event.event_id:
                    event.event_id = self.next_event_id_locked()
                prepared.append((txn, assessment, event))
            for txn, assessment, event in prepared:
                self.transactions.setdefault(txn.txn_id, txn)
                self.assessments[txn.txn_id] = assessment
                # 补录交易的发生时刻即其显式 submitted_at，一并冻结登记
                self.occurred_at[txn.txn_id] = _txn_time(txn)
                self.in_flight.pop(txn.txn_id, None)
                if event is not None:
                    self.events[event.event_id] = event

    def get_assessment(self, txn_id: str) -> Assessment | None:
        with self._lock:
            return self.assessments.get(txn_id)

    def get_transaction(self, txn_id: str) -> Transaction | None:
        with self._lock:
            return self.transactions.get(txn_id)

    def occurred_time(self, txn: Transaction) -> datetime:
        """交易用于窗口计数/重放的发生时刻。

        已落库交易一律读首评时冻结的时刻（实时交易不再每次取新的 utcnow()）；
        未落库交易（如补录批次内待落库笔）按 submitted_at 解析。
        """
        with self._lock:
            frozen = self.occurred_at.get(txn.txn_id)
        return frozen if frozen is not None else _txn_time(txn)

    def count_account_transactions(
        self,
        account_id: str,
        after: datetime,
        at_or_before: datetime,
        extra: tuple[Transaction, ...] = (),
    ) -> int:
        """统计账户在 (after, at_or_before] 时间窗内提交的交易数。

        默认只数已落库交易（按首评冻结的发生时刻）；``extra`` 为补录批次中
        尚未落库的交易，使同一批补录在窗口内互相可见，批次首评结论与落库后
        单独重放一致。仅依据冻结/显式的发生时刻，因此任意时刻重放历史交易时，
        窗口都不会滑入未来交易，结论可复现。
        """
        after_u = _as_utc(after)
        before_u = _as_utc(at_or_before)
        with self._lock:
            sources = [
                (txn, self.occurred_at.get(txn.txn_id))
                for txn in self.transactions.values()
            ]
            sources.extend((txn, None) for txn in extra)
            return sum(
                1
                for txn, frozen in sources
                if txn.account_id == account_id
                and after_u < (frozen or _txn_time(txn)) <= before_u
            )

    # ------------------------------------------------------------ 规则版本

    def append_rule_version(
        self,
        rules: tuple[Rule, ...],
        note: str = "",
        created_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> RuleVersion:
        """追加一个新版本；版本号单调递增，旧版本保持不变。

        生效时间语义：``effective_at`` 必须严格晚于已存在的最新版本的
        生效时间（更早或与某个已存在版本撞点都会让历史结论说不清），
        违反时抛 :class:`RuleEffectiveTimeConflictError`，整批拒绝，
        不追加任何版本。未显式给出生效时间时取当前时刻。
        """
        with self._lock:
            created = _as_utc(created_at or utcnow())
            effective = _as_utc(effective_at or created)
            if self.rule_versions:
                last_effective = _as_utc(self.rule_versions[-1].effective_at)
                if effective <= last_effective:
                    raise RuleEffectiveTimeConflictError(
                        f"新版本生效时间 {effective.isoformat()} 不晚于已有最新版本的"
                        f"生效时间 {last_effective.isoformat()}，整批拒绝"
                        "（不挑选版本凑合生效）"
                    )
            version = len(self.rule_versions) + 1
            rv = RuleVersion(
                version=version,
                rules=tuple(rules),
                created_at=created,
                effective_at=effective,
                note=note,
            )
            self.rule_versions.append(rv)
            return rv

    def current_rule_version(self) -> RuleVersion | None:
        """当前时刻已生效的最新版本；所有版本都在未来生效时返回 None。"""
        with self._lock:
            return self.effective_rule_version_at_utc(utcnow())

    def latest_rule_version(self) -> RuleVersion | None:
        """已发布的最新版本（不考虑是否已生效）。"""
        with self._lock:
            return self.rule_versions[-1] if self.rule_versions else None

    def effective_rule_version_at_utc(self, at: datetime) -> RuleVersion | None:
        """返回某时刻生效的规则版本（effective_at <= at 的最新版本），无则 None。"""
        with self._lock:
            at_u = _as_utc(at)
            chosen: RuleVersion | None = None
            for rv in self.rule_versions:
                if _as_utc(rv.effective_at) <= at_u and (
                    chosen is None or rv.version > chosen.version
                ):
                    chosen = rv
            return chosen

    def get_rule_version(self, version: int) -> RuleVersion:
        with self._lock:
            if 1 <= version <= len(self.rule_versions):
                return self.rule_versions[version - 1]
            raise RuleValidationError(f"规则版本 {version} 不存在")

    def effective_rule_version(self, at: datetime) -> RuleVersion:
        """返回某时刻生效的规则版本；早于最早版本生效时间则明确拒绝。"""
        chosen = self.effective_rule_version_at_utc(at)
        if chosen is None:
            raise NoEffectiveRuleVersionError(
                f"交易发生时刻 {_as_utc(at).isoformat()} 早于系统中最早生效的"
                "规则版本，不允许拿最新规则套用历史交易"
            )
        return chosen

    # ------------------------------------------------------------ 可疑事件

    def next_event_id(self) -> str:
        with self._lock:
            return self.next_event_id_locked()

    def next_event_id_locked(self) -> str:
        self._event_seq += 1
        return f"EVT-{self._event_seq:06d}"

    def save_event(self, event: SuspiciousEvent) -> SuspiciousEvent:
        with self._lock:
            self.events[event.event_id] = event
            return event

    def get_event(self, event_id: str) -> SuspiciousEvent | None:
        with self._lock:
            return self.events.get(event_id)
