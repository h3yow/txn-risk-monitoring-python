"""实时通道的频次（velocity）判定、首评/重放一致性、与补录交叠的边界。

背景回归：实时交易（未显式给发生时间）曾因为发生时刻在计数时被重复取
utcnow()，导致已落库实时交易永远落在窗口右边界之外、频次规则完全不生效。
本文件固定以下行为：

- 实时连续多笔：窗口内笔数超过阈值即命中并给分，未到阈值不误命中；
- 实时首评 == 之后单独重放（不带版本参数）：规则版本、命中、分值、结论逐项一致，
  即使中途发布了新规则版本；
- 实时与补录的时间窗互不污染：历史补录不进实时窗口，未来实时交易不进历史窗口；
- 重复/并发/内容冲突三种拒绝在实时通道保持原样、彼此可区分。

实时用例使用真实当前时刻，补录用固定历史锚点（2026-06-01），
期望值不依赖跑用例当天的日期。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ConcurrentSubmissionError,
    Decision,
    DuplicateTransactionError,
    TransactionContentConflictError,
)

# 固定历史锚点：与"现在"永远相距 5 分钟以上，保证与实时窗口不相交
T1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)


def _submit_live(svc, txn_id: str, account_id: str = "ACC-1", amount: float = 10):
    return svc.submit_transaction(
        txn_id=txn_id, account_id=account_id, amount=amount,
        counterparty="NORMAL-CORP",
    )


def _assert_same_conclusion(first, replayed) -> None:
    """首评与重放逐项一致：规则版本、命中规则、分值、最终结论。"""
    assert replayed.rule_version == first.rule_version
    assert replayed.decision == first.decision
    assert replayed.score == first.score
    assert [(h.rule_id, h.rule_version) for h in replayed.hits] == [
        (h.rule_id, h.rule_version) for h in first.hits
    ]


# ---------------------------------------------------------------- 实时连续多笔触发频次

def test_live_rapid_submissions_trigger_velocity_on_third(svc):
    """同一账户短时间内连提 3 笔（阈值：5 分钟内 >2 笔）：第 3 笔命中给分。"""
    r1 = _submit_live(svc, "LIVE-V-1")
    r2 = _submit_live(svc, "LIVE-V-2")
    r3 = _submit_live(svc, "LIVE-V-3")

    # 未到阈值的两笔不误命中
    assert r1["assessment"].decision is Decision.PASS
    assert r1["assessment"].score == 0
    assert r1["assessment"].hits == ()
    assert r2["assessment"].decision is Decision.PASS
    assert r2["assessment"].score == 0
    assert r2["assessment"].hits == ()

    # 第 3 笔窗口内 3 笔 > 2 -> 命中 VEL-2-5M，50 分 -> review 并开立事件
    a3 = r3["assessment"]
    assert a3.decision is Decision.REVIEW
    assert a3.score == 50
    assert [h.rule_id for h in a3.hits] == ["VEL-2-5M"]
    assert "3 笔" in a3.hits[0].reason
    assert r3["event"] is not None
    assert r1["event"] is None and r2["event"] is None


def test_live_below_threshold_never_hits(svc):
    """只提交 2 笔（未超阈值）：无论提交多少次都不误命中。"""
    for i in range(2):
        r = _submit_live(svc, f"LIVE-B-{i}")
        assert r["assessment"].decision is Decision.PASS
        assert r["assessment"].score == 0
        assert r["assessment"].hits == ()
        assert r["event"] is None


def test_live_velocity_window_is_per_account(svc):
    """频次按账户独立累计：ACC-1 的 2 笔不计入 ACC-2 的窗口。"""
    _submit_live(svc, "LIVE-A-1")
    _submit_live(svc, "LIVE-A-2")
    r = _submit_live(svc, "LIVE-A-3", account_id="ACC-2")
    assert r["assessment"].decision is Decision.PASS
    assert r["assessment"].hits == ()


# ---------------------------------------------------------------- 实时首评 == 重放

def test_live_first_assessment_equals_replay(svc):
    """实时首评与之后单独重放（不带版本参数）逐项一致，含命中与未命中的笔。"""
    for i in range(3):
        _submit_live(svc, f"LIVE-R-{i}")
    for i in range(3):
        first = svc.get_assessment(f"LIVE-R-{i}")
        replayed = svc.replay_transaction(f"LIVE-R-{i}")
        _assert_same_conclusion(first, replayed)
    # 第 3 笔首评命中、重放也必须命中（回归：首评命中而重放不命中）
    assert svc.get_assessment("LIVE-R-2").decision is Decision.REVIEW
    assert svc.replay_transaction("LIVE-R-2").decision is Decision.REVIEW


def test_live_replay_consistent_even_after_rule_upgrade(svc):
    """首评后发布新规则版本：不带版本参数的重放仍复现首评结论。"""
    for i in range(3):
        _submit_live(svc, f"LIVE-U-{i}")
    first = svc.get_assessment("LIVE-U-2")
    assert first.rule_version == 1
    assert first.decision is Decision.REVIEW

    # v2：停用频次规则（默认即时生效，生效时间单调晚于 v1）
    svc.publish_rules(
        [{"rule_id": "AMT-1000", "rule_type": "amount_limit",
          "params": {"limit": 1000, "score": 120}}],
        note="v2 停用 velocity",
    )
    assert svc.current_rule_version() == 2

    replayed = svc.replay_transaction("LIVE-U-2")
    _assert_same_conclusion(first, replayed)
    # 显式指定 v2 复算可以看到差异，但不改写已落库结论
    replayed_v2 = svc.replay_transaction("LIVE-U-2", version=2)
    assert replayed_v2.decision is Decision.PASS
    assert svc.get_assessment("LIVE-U-2") == first


# ---------------------------------------------------------------- 与补录交叠的边界

def test_backfill_history_does_not_pollute_live_window(svc):
    """先补录 3 笔历史交易，实时窗口只数实时交易：第 3 笔实时仍按 3 笔命中。"""
    results = svc.backfill_transactions([
        {"txn_id": f"BF-H-{i}", "account_id": "ACC-1", "amount": 10,
         "counterparty": "NORMAL-CORP",
         "submitted_at": T1 + timedelta(minutes=i)}
        for i in range(3)
    ])
    # 补录侧自身语义不变：第 3 笔在其历史窗口内命中
    assert results[2]["assessment"].decision is Decision.REVIEW

    # 实时连提：窗口内只有实时笔，第 3 笔按 3 笔命中（不是 6 笔）
    for i in range(2):
        r = _submit_live(svc, f"LIVE-P-{i}")
        assert r["assessment"].hits == ()
    r3 = _submit_live(svc, "LIVE-P-2")
    assert [h.rule_id for h in r3["assessment"].hits] == ["VEL-2-5M"]
    assert "3 笔" in r3["assessment"].hits[0].reason


def test_live_transactions_do_not_pollute_backfill_window(svc):
    """先有实时交易，再补录历史交易：补录窗口不含任何实时笔。"""
    for i in range(3):
        _submit_live(svc, f"LIVE-Q-{i}", account_id="ACC-2")

    # 补录 2 笔历史交易（窗口内仅 2 笔，不超阈值）：若被实时笔污染会误命中
    results = svc.backfill_transactions([
        {"txn_id": f"BF-Q-{i}", "account_id": "ACC-2", "amount": 10,
         "counterparty": "NORMAL-CORP",
         "submitted_at": T1 + timedelta(minutes=i)}
        for i in range(2)
    ])
    for r in results:
        assert r["assessment"].decision is Decision.PASS
        assert r["assessment"].hits == ()

    # 补录落库后再重放：窗口依然不含实时笔，结论不变
    for i in range(2):
        replayed = svc.replay_transaction(f"BF-Q-{i}")
        _assert_same_conclusion(svc.get_assessment(f"BF-Q-{i}"), replayed)


def test_backfill_replay_ignores_future_live_transactions(svc):
    """补录首评命中后，实时通道再进多少笔都不改变其重放结论。"""
    results = svc.backfill_transactions([
        {"txn_id": f"BF-F-{i}", "account_id": "ACC-1", "amount": 10,
         "counterparty": "NORMAL-CORP",
         "submitted_at": T1 + timedelta(minutes=i)}
        for i in range(3)
    ])
    first = results[2]["assessment"]
    assert first.decision is Decision.REVIEW

    for i in range(5):
        _submit_live(svc, f"LIVE-F-{i}")

    replayed = svc.replay_transaction("BF-F-2")
    _assert_same_conclusion(first, replayed)


# ---------------------------------------------------------------- 实时通道拒绝语义保持

def test_live_rejection_reasons_stay_distinguishable(svc, monkeypatch):
    """实时通道：重复 / 并发 / 内容冲突三种拒绝彼此可区分。"""
    import app.engine as engine_mod

    _submit_live(svc, "LIVE-D-1", amount=100)
    # 已完成 + 内容一致 -> 重复
    with pytest.raises(DuplicateTransactionError):
        _submit_live(svc, "LIVE-D-1", amount=100)
    # 已完成 + 内容对不上 -> 内容冲突
    with pytest.raises(TransactionContentConflictError):
        _submit_live(svc, "LIVE-D-1", amount=999)

    # 在途占位 + 内容一致 -> 并发
    release = threading.Event()
    entered = threading.Event()
    original = engine_mod.AssessmentEngine._evaluate

    def blocking(self, txn, rv, include_current, **kw):
        entered.set()
        release.wait(timeout=5)
        return original(self, txn, rv, include_current, **kw)

    monkeypatch.setattr(engine_mod.AssessmentEngine, "_evaluate", blocking)

    t = threading.Thread(target=_submit_live, args=(svc, "LIVE-D-2"))
    t.start()
    entered.wait(timeout=5)
    with pytest.raises(ConcurrentSubmissionError):
        _submit_live(svc, "LIVE-D-2")
    with pytest.raises(TransactionContentConflictError):
        _submit_live(svc, "LIVE-D-2", amount=999)
    release.set()
    t.join(timeout=10)

    # 每种拒绝都没有产生第二条结论或事件
    assert len(svc.store.assessments) == 2
    assert len(svc.store.events) == 0
