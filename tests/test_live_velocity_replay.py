"""实时通道回归：连续提交触发频次、首评与重放一致、与补录交叠不互相污染。

时间口径全部用固定常量，并把 app 内的时钟（app.store.utcnow /
app.engine.utcnow）冻结到固定锚点，因此用例在任何一天跑出来的期望都一样，
不依赖跑用例当天的日期。

覆盖业务：
1. 实时连续多笔：短时间窗内频次累计，第 3 笔（>阈值）命中并给分，前两笔不误命中；
   窗口外不累计。
2. 首评 == 不带版本重放：规则版本、命中规则、分值、最终结论逐项一致；
   即使之后发布了新版本，旧实时交易的默认重放仍锁定首评时的版本与结论。
3. 实时通道与补录通道交叠：velocity 时间窗计数相互独立、不互相污染。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.engine as engine_mod
import app.store as store_mod
from app.models import Decision

# 固定时钟锚点（晚于夹具中 v1 的生效时间 2026-01-01）。
CLOCK = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=1)  # 每笔实时提交之间时钟前进 1 秒，远小于 5 分钟窗


class _FrozenClock:
    """可手动推进的固定时钟，替换 app 内 utcnow。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def tick(self, delta: timedelta = STEP) -> None:
        self.now += delta


@pytest.fixture
def frozen_clock(monkeypatch) -> _FrozenClock:
    clock = _FrozenClock(CLOCK)
    # 实时交易发生时刻冻结、规则"当前生效版本"都走这两处时钟
    monkeypatch.setattr(store_mod, "utcnow", clock)
    monkeypatch.setattr(engine_mod, "utcnow", clock)
    return clock


def _submit_n(svc, n: int, *, start: int = 0, clock: _FrozenClock | None = None,
              amount: float = 10, counterparty: str = "NORMAL"):
    """顺序提交 n 笔实时交易（不同交易号、同一账户），返回结论列表。"""
    results = []
    for i in range(start, start + n):
        results.append(
            svc.submit_transaction(
                txn_id=f"L-{i}",
                account_id="ACC-1",
                amount=amount,
                counterparty=counterparty,
            )["assessment"]
        )
        if clock is not None:
            clock.tick()
    return results


# =====================================================================
# 业务一：实时连续多笔触发频次（阈值内不误命中，超阈值命中并给分）
# =====================================================================

class TestLiveVelocityTriggers:
    def test_first_two_pass_third_and_after_hit(self, svc, frozen_clock):
        # v1 velocity VEL-2-5M：5 分钟内 >2 笔命中，分值 50（>=1 -> review）
        a = _submit_n(svc, 4, clock=frozen_clock)
        assert (a[0].decision, a[0].score) == (Decision.PASS, 0)
        assert (a[1].decision, a[1].score) == (Decision.PASS, 0)
        # 第 3 笔窗口内 3 笔 > 2 -> 命中
        assert (a[2].decision, a[2].score) == (Decision.REVIEW, 50)
        assert [h.rule_id for h in a[2].hits] == ["VEL-2-5M"]
        # 第 4 笔窗口内 4 笔 -> 同样命中
        assert (a[3].decision, a[3].score) == (Decision.REVIEW, 50)
        assert [h.rule_id for h in a[3].hits] == ["VEL-2-5M"]
        # 命中明细规则版本与结论版本一致
        assert all(h.rule_version == a[2].rule_version for h in a[2].hits)

    def test_exactly_threshold_does_not_hit(self, svc, frozen_clock):
        # 恰好 2 笔（==max_count）：任何一笔都不能误命中
        a = _submit_n(svc, 2, clock=frozen_clock)
        assert all(x.decision is Decision.PASS and x.score == 0 for x in a)

    def test_txn_outside_window_not_counted(self, svc, frozen_clock):
        # 前两笔落在很早的时刻；把时钟推到 5 分钟窗口之外再连提两笔，
        # 旧笔不应计入新窗口 -> 仍然 pass（不触发频次）
        _submit_n(svc, 2, clock=frozen_clock)
        frozen_clock.tick(timedelta(minutes=10))
        a = _submit_n(svc, 2, start=10, clock=frozen_clock)
        assert all(x.decision is Decision.PASS and x.score == 0 for x in a)

    def test_velocity_isolated_per_account(self, svc, frozen_clock):
        # ACC-1 连提 3 笔命中；ACC-2 同时只提 1 笔，不能被 ACC-1 的频次带累
        acc1 = _submit_n(svc, 3, clock=frozen_clock)
        assert acc1[2].decision is Decision.REVIEW
        only = svc.submit_transaction(
            txn_id="O-1", account_id="ACC-2", amount=10
        )["assessment"]
        assert only.decision is Decision.PASS and only.score == 0


# =====================================================================
# 业务二：首评 == 不带版本重放（版本/命中/分值/结论完全一致）
# =====================================================================

class TestLiveFirstAssessmentEqualsReplay:
    def test_replay_matches_first_for_every_txn_in_run(self, svc, frozen_clock):
        firsts = _submit_n(svc, 4, clock=frozen_clock)
        for i, first in enumerate(firsts):
            replay = svc.replay_transaction(f"L-{i}")
            assert replay.rule_version == first.rule_version
            assert replay.decision == first.decision
            assert replay.score == first.score
            assert [(h.rule_id, h.rule_version) for h in replay.hits] == \
                [(h.rule_id, h.rule_version) for h in first.hits]

    def test_replay_remains_identical_after_new_version_published(self, svc,
                                                                  frozen_clock):
        # 首评在 v1 下，第 3 笔命中 v1 的 velocity
        firsts = _submit_n(svc, 3, clock=frozen_clock)
        hit_first = firsts[2]
        assert hit_first.rule_version == 1
        assert hit_first.decision is Decision.REVIEW

        # 之后发布 v2：velocity 停用、限额规则改变。v2 在首评时刻之后生效。
        svc.publish_rules(
            [
                {"rule_id": "AMT-9999", "rule_type": "amount_limit",
                 "params": {"limit": 9999, "score": 120}},
                {"rule_id": "VEL-2-5M", "rule_type": "velocity",
                 "enabled": False,
                 "params": {"window_minutes": 5, "max_count": 2, "score": 50}},
            ],
            effective_at=CLOCK + timedelta(days=30),
        )
        # 默认重放（不带版本）：按首评冻结时刻选版 -> 仍是 v1，结论完全一致
        replay = svc.replay_transaction("L-2")
        assert replay.rule_version == 1
        assert replay.decision == hit_first.decision
        assert replay.score == hit_first.score
        assert [h.rule_id for h in replay.hits] == \
            [h.rule_id for h in hit_first.hits]
        # 显式用 v2 重算只展示差异，不影响已落库的首评结论
        replay_v2 = svc.replay_transaction("L-2", version=2)
        assert replay_v2.rule_version == 2
        assert replay_v2.decision is Decision.PASS
        stored = svc.get_assessment("L-2")
        assert stored == hit_first


# =====================================================================
# 业务三：实时通道与补录交叠，velocity 计数互不污染
# =====================================================================

class TestLiveBackfillWindowIsolation:
    def test_backfill_does_not_inflate_live_window(self, svc, frozen_clock):
        # 补录 3 笔（historical 通道），发生时刻与当前实时时刻重叠
        base = CLOCK - timedelta(minutes=2)
        svc.backfill_transactions([
            {"txn_id": f"BF-{i}", "account_id": "ACC-1", "amount": 10,
             "counterparty": "NORMAL",
             "submitted_at": base + timedelta(seconds=i)}
            for i in range(3)
        ])
        # 实时连提两笔：若被补录污染，第 2 笔就会误命中；通道隔离后应全 pass
        a = _submit_n(svc, 2, clock=frozen_clock)
        assert all(x.decision is Decision.PASS and x.score == 0 for x in a)
        # 第 3 笔实时提交：live 通道内累计到 3 笔 -> 命中
        third = svc.submit_transaction(
            txn_id="L-2", account_id="ACC-1", amount=10
        )["assessment"]
        assert (third.decision, third.score) == (Decision.REVIEW, 50)

    def test_live_does_not_inflate_backfill_window(self, svc, frozen_clock):
        # 先在实时通道连提 3 笔（第 3 笔命中）
        _submit_n(svc, 3, clock=frozen_clock)
        # 再补录两笔落在同一 5 分钟窗内：historical 通道独立，
        # 不被 live 的 3 笔污染 -> 两笔都应 pass
        base = CLOCK + timedelta(minutes=1)
        results = svc.backfill_transactions([
            {"txn_id": "BF-A", "account_id": "ACC-1", "amount": 10,
             "counterparty": "NORMAL", "submitted_at": base},
            {"txn_id": "BF-B", "account_id": "ACC-1", "amount": 10,
             "counterparty": "NORMAL",
             "submitted_at": base + timedelta(seconds=1)},
        ])
        for r in results:
            a = r["assessment"]
            assert a.decision is Decision.PASS and a.score == 0
        # 补录落库后重放这两笔，结论仍与首评一致
        for txn_id in ("BF-A", "BF-B"):
            replay = svc.replay_transaction(txn_id)
            first = svc.get_assessment(txn_id)
            assert (replay.decision, replay.score, replay.rule_version) == \
                (first.decision, first.score, first.rule_version)

    def test_backfill_velocity_still_hits_within_its_own_channel(self, svc,
                                                                 frozen_clock):
        # 交叠场景下补录自身的频次语义不能被破坏：同批 3 笔在窗内，第 3 笔命中
        base = CLOCK
        results = svc.backfill_transactions([
            {"txn_id": f"BF-V-{i}", "account_id": "ACC-1", "amount": 10,
             "counterparty": "NORMAL",
             "submitted_at": base + timedelta(seconds=i)}
            for i in range(3)
        ])
        scores = [r["assessment"].score for r in results]
        assert scores == [0, 0, 50]
        # 同账户实时笔数为 0：再提一笔实时交易不应命中
        live = svc.submit_transaction(
            txn_id="LONE-1", account_id="ACC-1", amount=10
        )["assessment"]
        assert live.decision is Decision.PASS and live.score == 0
