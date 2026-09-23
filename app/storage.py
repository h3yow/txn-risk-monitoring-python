"""线程安全的本地内存存储。

用 RLock 保护全部读写；交易去重采用「预留槽位 + 状态标记」两阶段方式，
保证同 txn_id 的并发提交中恰好一个成功，其余被明确拒绝。
"""
from __future__ import annotations

import threading
from typing import Literal

from app.models import (
    Account,
    DecisionRecord,
    Event,
    RuleSetVersion,
    TransactionInput,
    utcnow,
)

ReserveResult = Literal["accepted", "duplicate", "concurrent"]


class Storage:
    """模拟本地数据库；所有公开方法均持锁，保证并发下一致。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accounts: dict[str, Account] = {}
        # 交易槽位：input 存在即表示该 txn_id 已被占用
        self._txn_inputs: dict[str, TransactionInput] = {}
        # 预留后、结论落库前的进行中集合
        self._inflight: set[str] = set()
        self._decisions: dict[str, DecisionRecord] = {}
        self._events_by_id: dict[str, Event] = {}
        self._events_by_txn: dict[str, str] = {}
        self._rule_versions: dict[int, RuleSetVersion] = {}
        self._rule_seq = 0
        self._event_seq = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 账户 ----
    def upsert_account(self, account: Account) -> None:
        with self._lock:
            self._accounts[account.account_id] = account

    def get_account(self, account_id: str) -> Account | None:
        with self._lock:
            return self._accounts.get(account_id)

    # ---- 交易去重（两阶段预留） ----
    def reserve_txn(self, txn: TransactionInput) -> ReserveResult:
        """在锁内原子地预留交易槽位。

        - accepted：首次提交，槽位已置为 in-flight，调用方完成评估后须调用 save_decision
        - concurrent：同一 txn_id 正被另一线程评估
        - duplicate：该交易已有最终结论
        """
        with self._lock:
            if txn.txn_id in self._txn_inputs:
                if txn.txn_id in self._inflight:
                    return "concurrent"
                return "duplicate"
            self._txn_inputs[txn.txn_id] = txn
            self._inflight.add(txn.txn_id)
            return "accepted"

    def release_txn(self, txn_id: str) -> None:
        """评估失败时回滚预留，避免失败的提交永久占位。"""
        with self._lock:
            self._inflight.discard(txn_id)
            self._txn_inputs.pop(txn_id, None)

    def get_txn_input(self, txn_id: str) -> TransactionInput | None:
        with self._lock:
            return self._txn_inputs.get(txn_id)

    # ---- 结论（与事件一起在锁内落库） ----
    def save_decision(
        self,
        record: DecisionRecord,
        event: Event | None = None,
    ) -> None:
        with self._lock:
            self._decisions[record.txn_id] = record
            if event is not None:
                self._events_by_id[event.event_id] = event
                self._events_by_txn[event.txn_id] = event.event_id
            self._inflight.discard(record.txn_id)

    def get_decision(self, txn_id: str) -> DecisionRecord | None:
        with self._lock:
            return self._decisions.get(txn_id)

    # ---- 事件 ----
    def next_event_id(self) -> str:
        with self._lock:
            self._event_seq += 1
            return f"EVT-{self._event_seq:06d}"

    def save_event(self, event: Event) -> None:
        with self._lock:
            self._events_by_id[event.event_id] = event
            self._events_by_txn[event.txn_id] = event.event_id

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            return self._events_by_id.get(event_id)

    def get_event_by_txn(self, txn_id: str) -> Event | None:
        with self._lock:
            event_id = self._events_by_txn.get(txn_id)
            return self._events_by_id.get(event_id) if event_id else None

    # ---- 规则版本 ----
    def next_rule_version(self) -> int:
        with self._lock:
            self._rule_seq += 1
            return self._rule_seq

    def save_rule_version(self, version: RuleSetVersion) -> None:
        with self._lock:
            self._rule_versions[version.version] = version

    def get_rule_version(self, version: int) -> RuleSetVersion | None:
        with self._lock:
            return self._rule_versions.get(version)

    def latest_rule_version(self) -> RuleSetVersion | None:
        with self._lock:
            if not self._rule_versions:
                return None
            return self._rule_versions[max(self._rule_versions)]

    def list_rule_versions(self) -> list[RuleSetVersion]:
        with self._lock:
            return [self._rule_versions[v] for v in sorted(self._rule_versions)]
