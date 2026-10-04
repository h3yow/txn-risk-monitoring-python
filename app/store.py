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


# velocity 计数的两个通道：实时提交与补录各自独立累计，互不污染。
# - live：未显式给发生时间的实时交易，发生时刻在评估时冻结到 occurred_at；
# - historical：补录（以及显式带发生时间）的交易，发生时刻取 submitted_at。
CHANNEL_LIVE = "live"
CHANNEL_HISTORICAL = "historical"


def txn_channel(txn: Transaction) -> str:
    """一笔交易属于实时通道还是补录通道。"""
    return (
        CHANNEL_LIVE
        if txn.submitted_at == LIVE_SUBMISSION
        else CHANNEL_HISTORICAL
    )


class RiskStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.transactions: dict[str, Transaction] = {}
        # 已完成的评估结论（每笔交易至多一条）
        self.assessments: dict[str, Assessment] = {}
        # 进行中的评估占位：txn_id -> 提交内容，用于并发冲突与内容一致性检测
        self.in_flight: dict[str, Transaction] = {}
        # 实时交易在评估时刻冻结的真实发生时刻：txn_id -> datetime(UTC)。
        # 哨兵 Transaction 本身不可变，故另存一份；重放/计数都以它为准，
        # 保证"首评 == 重放"且实时连续提交能按真实间隔进入时间窗。
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

        实时交易（哨兵）必须传入评估时刻冻结的 ``occurred_at``，
        作为后续 velocity 计数与默认重放选版的唯一时间口径。
        """
        with self._lock:
            self.transactions.setdefault(txn.txn_id, txn)
            self.assessments[txn.txn_id] = assessment
            if occurred_at is not None:
                self.occurred_at.setdefault(txn.txn_id, _as_utc(occurred_at))
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
                self.in_flight.pop(txn.txn_id, None)
                if event is not None:
                    self.events[event.event_id] = event

    def get_assessment(self, txn_id: str) -> Assessment | None:
        with self._lock:
            return self.assessments.get(txn_id)

    def get_transaction(self, txn_id: str) -> Transaction | None:
        with self._lock:
            return self.transactions.get(txn_id)

    def get_occurred_at(self, txn_id: str) -> datetime | None:
        """实时交易冻结的发生时刻；非实时或不存在时返回 None。"""
        with self._lock:
            return self.occurred_at.get(txn_id)

    def resolve_occurred_at(self, txn: Transaction) -> datetime:
        """解析一笔（已落库或传入的）交易用于判定的发生时刻。

        - 实时交易：返回评估落库时冻结的时刻，没有冻结记录时退化为当前时刻；
        - 补录/显式时间交易：返回统一到 UTC 的 submitted_at。
        """
        if txn.submitted_at == LIVE_SUBMISSION:
            frozen = self.occurred_at.get(txn.txn_id)
            return frozen if frozen is not None else utcnow()
        return _as_utc(txn.submitted_at)

    def count_account_transactions(
        self,
        account_id: str,
        after: datetime,
        at_or_before: datetime,
        extra: tuple[Transaction, ...] = (),
        channel: str = CHANNEL_HISTORICAL,
    ) -> int:
        """统计账户在 (after, at_or_before] 时间窗内、指定通道的交易数。

        通道隔离：实时交易（``live``）与补录/显式时间交易（``historical``）
        各自累计，时间窗计数互不污染。
        ``extra`` 为同一补录批次中尚未落库的交易（均属 historical 通道），
        使同批补录在窗口内互相可见，批次首评与落库后单独重放一致。
        所有时间都按交易自身的冻结发生时刻取值，因此任意时刻重放，
        窗口都不会滑入"之后"的交易，结论可复现。
        """
        after_u = _as_utc(after)
        before_u = _as_utc(at_or_before)
        with self._lock:
            count = 0
            for txn in self.transactions.values():
                if txn_channel(txn) != channel:
                    continue
                if txn.account_id != account_id:
                    continue
                when = self.resolve_occurred_at(txn)
                if after_u < when <= before_u:
                    count += 1
            if channel == CHANNEL_HISTORICAL:
                for txn in extra:
                    if txn.account_id != account_id:
                        continue
                    when = self.resolve_occurred_at(txn)
                    if after_u < when <= before_u:
                        count += 1
            return count

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
