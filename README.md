# 交易风险监控与可疑事件处置服务

基于 Python 3.14 + FastAPI 的本地内存实现，模拟账户、交易与规则数据，
保证同一批交易在不同入口（直接调用 / HTTP）与并发提交下得到**一致且可追溯**的判定结论。

## 1. 判定模型

### 1.1 处理流程

```
提交交易
  │
  ├─ 1. 原子占位 claim_assessment（RLock 临界区）
  │       已完成结论 ──► DuplicateTransactionError   （重复提交，HTTP 409）
  │       进行中占位 ──► ConcurrentSubmissionError   （并发提交，HTTP 409）
  │
  ├─ 2. 取当前规则版本快照（append-only，不可变）
  ├─ 3. 逐条规则评估（enabled=false 的规则跳过），累加命中分值
  ├─ 4. 阈值决策
  │       score >= 100  → reject（自动开立可疑事件）
  │       1 <= score <100 → review（自动开立可疑事件）
  │       score = 0       → pass（不开事件）
  └─ 5. 原子提交交易+结论并释放占位；review/reject 自动建事件（每笔交易至多一个）
```

评估失败会释放占位（`release_assessment`），避免同笔交易被永久误判为"并发中"。

### 1.2 规则类型（`params` 契约）

| 规则 | 参数 | 命中条件 |
|---|---|---|
| `amount_limit` | `limit > 0`，`score >= 0` | 单笔金额 > limit |
| `velocity` | `window_minutes >= 1`、`max_count >= 1`、`score >= 0` | 账户在 `(提交时间-window, 提交时间]` 窗口内（含当前笔）笔数 > max_count |
| `blocklist` | `counterparties` 非空字符串列表（不可重复）、`score >= 0` | 交易对手在黑名单内 |

决策结果 `Assessment` 固化：`txn_id`、`decision`、`score`、`rule_version`、
每条命中的 `rule_id / rule_type / rule_version / reason`、评估时间。

## 2. 规则版本兼容规则

- **append-only**：每次发布生成单调递增的新版本（`RiskStore.rule_versions`），
  旧版本永不就地修改。规则的新增、停用（`enabled=false`）与升级（换 rule_id/参数）
  都通过发布新版本完成。
- **历史不变**：历史交易的结论落库后不再变化；事件中也保存结论快照。
  规则升级后，旧交易仍保留当时的 `rule_version` 与命中明细。
- **可重放**：
  - `POST /transactions/{txn_id}/replay`（不带版本）→ 取交易 `submitted_at`
    时刻生效的规则版本复算，默认与首次结论一致；
  - 显式传 `{"version": n}` → 用任意历史/新版本复算以展示差异，
    但**不会覆盖**已落库的历史结论。
  - velocity 规则仅按 `submitted_at` 之前（含自身）的交易计数，
    重放时窗口不会滑入未来交易，保证结论可复现。
- **整批校验、整批拒绝**：一批规则中任意一条结构/取值非法（类型不支持、参数越界、
  `rule_id` 缺失或批次内重复、`enabled` 非布尔、params 非对象等），
  立即抛 `RuleValidationError`（HTTP 422），**不产生新版本**，
  当前生效版本不变 —— 不部分生效、不静默降级。

## 3. 可疑事件状态机

```
        ┌──────────┬──────────────┬────────────┐
        ▼          ▼              ▼            ▼
     OPEN ──► ESCALATED ──► CONFIRMED ──► CLOSED（终态）
       │           │             ▲
       │           ├─► FALSE_POSITIVE ──► CLOSED
       ├─► CONFIRMED         （误报后只允许关闭）
       ├─► FALSE_POSITIVE
       └─► CLOSED
  CONFIRMED 可再 ESCALATED；ESCALATED 可转 CONFIRMED/FALSE_POSITIVE/CLOSED；
  CLOSED 为终态，任何转出（含再 close）都拒绝。
```

非法流转抛 `InvalidTransitionError`（HTTP 409，code=`INVALID_TRANSITION`），
**不写入审计历史**。每次合法处置追加不可变记录：动作、from/to 状态、操作人、备注、时间。
事件详情 `GET /events/{id}` 返回触发时的**交易输入**、**判定结论（含命中规则及版本）**与完整处置链。

## 4. HTTP 接口与拒绝原因

| 方法/路径 | 说明 |
|---|---|
| `POST /accounts` | 建账户 |
| `POST /rules/versions` | 校验并发布规则新版本 |
| `POST /transactions` | 提交交易并评估（幂等拒绝） |
| `POST /transactions/{txn_id}/replay` | 按当时/指定版本重放 |
| `POST /events/{id}/actions` | 事件处置：`confirm`/`mark_false_positive`/`escalate`/`close` |
| `GET /events/{id}` | 事件详情（输入 + 结论快照 + 处置历史） |

拒绝原因通过响应体 `error.code` 可区分（同为 HTTP 409 但 code 不同）：

| code | 触发场景 |
|---|---|
| `DUPLICATE_TRANSACTION` | 交易已完成评估后的重复提交 |
| `CONCURRENT_SUBMISSION` | 同一交易评估进行中的并发提交 |
| `INVALID_TRANSITION` | 非法事件状态流转 / 未知动作 / 事件不存在 |
| `INVALID_RULESET` | 规则集非法，整批拒绝（HTTP 422） |

## 5. 本地验证

```bash
uv sync                                   # 安装依赖（含 pytest）
uv run pytest                             # 全部单测（20 个），日志含 txn_id / rule_version / 判定依据
uv run pytest --log-cli-level=INFO        # 查看 ASSESS / REPLAY / EVENT 判定日志
uv run python main.py                     # 启动服务 http://127.0.0.1:8000/docs
# 或：uv run uvicorn app.api:app --reload
```

手工冒烟：

```bash
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"account_id":"ACC-1"}'
curl -s -X POST localhost:8000/rules/versions -H 'Content-Type: application/json' -d '{"note":"v1","rules":[{"rule_id":"AMT-1000","rule_type":"amount_limit","params":{"limit":1000,"score":120}}]}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":5000}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":5000}'   # 409 DUPLICATE_TRANSACTION
```

### 测试覆盖

| 文件 | 覆盖点 |
|---|---|
| `tests/test_dedup_concurrency.py` | 顺序重复提交、并发提交恰好一次成功、两种拒绝原因可区分、无重复结论/事件 |
| `tests/test_rule_versions_replay.py` | 非法规则整批拒绝且不产生新版本、v2 升级不改历史结论、默认/指定版本重放、重放不覆盖历史 |
| `tests/test_event_transitions.py` | 合法全链路、非法流转拒绝且不留痕、终态约束、未知动作、事件快照可追溯 |
| `tests/test_api.py` | HTTP 错误码区分、422 整批拒绝、非法流转 409、事件追溯详情 |

## 6. 模块结构

```
app/models.py    枚举、值对象、业务异常
app/store.py     线程安全本地存储；评估原子占位；append-only 规则版本
app/rules.py     规则批量校验（整批拒绝）
app/engine.py    判定引擎：去重/并发、版本快照评估、打分决策、历史重放、判定日志
app/events.py    事件状态机与审计历史
app/service.py   门面服务（账户/发布规则/提交交易/重放/处置）
app/api.py       FastAPI 入口与可区分错误码
```
