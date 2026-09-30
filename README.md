# 交易风险监控与可疑事件处置服务

基于 Python 3.14 + FastAPI 的本地内存实现，模拟账户、交易与规则数据，
保证同一批交易在不同入口（直接调用 / HTTP）与并发提交下得到**一致且可追溯**的判定结论。

## 1. 判定模型

### 1.1 处理流程

```
提交交易（实时 / 补录）
  │
  ├─ 1. 原子占位 claim_assessment（RLock 临界区）
  │       已完成且内容一致 ──► DuplicateTransactionError        （重复提交，HTTP 409）
  │       已完成但关键内容不一致 ──► TransactionContentMismatchError（同号异内容，HTTP 409）
  │       进行中占位 ──► ConcurrentSubmissionError              （并发提交，HTTP 409）
  │
  ├─ 2. 按交易【发生时刻】取生效规则版本快照（append-only，不可变）
  │       submitted_at 早于最早生效版本 ──► NoEffectiveRuleVersionError（HTTP 422）
  ├─ 3. 逐条规则评估（enabled=false 的规则跳过），累加命中分值
  ├─ 4. 阈值决策
  │       score >= 100  → reject（自动开立可疑事件）
  │       1 <= score <100 → review（自动开立可疑事件）
  │       score = 0       → pass（不开事件）
  └─ 5. 原子提交交易+结论并释放占位；review/reject 自动建事件（每笔交易至多一个）
```

- **实时交易**：不传 `submitted_at`（缺省为当前时刻），按当前生效版本判定，行为与历史一致。
- **补录交易**：传交易实际发生的 `submitted_at`，按**该时刻生效**的版本判定。
  因此一笔补录交易**第一次算出的结论**，与之后单独 `replay` 这笔交易的结论
  完全一致（规则版本、命中规则、分值、最终决策对得上）。
- 评估失败会释放占位（`release_assessment`），避免同笔交易被永久误判为"并发中"。

### 1.2 规则类型（`params` 契约）

| 规则 | 参数 | 命中条件 |
|---|---|---|
| `amount_limit` | `limit > 0`，`score >= 0` | 单笔金额 > limit |
| `velocity` | `window_minutes >= 1`、`max_count >= 1`、`score >= 0` | 账户在 `(提交时间-window, 提交时间]` 窗口内（含当前笔）笔数 > max_count |
| `blocklist` | `counterparties` 非空字符串列表（不可重复）、`score >= 0` | 交易对手在黑名单内 |

决策结果 `Assessment` 固化：`txn_id`、`decision`、`score`、`rule_version`、
每条命中的 `rule_id / rule_type / rule_version / reason`、评估时间。

## 2. 规则版本与生效时间

### 2.1 append-only 与版本快照

- **append-only**：每次发布生成单调递增的新版本（`RiskStore.rule_versions`），
  旧版本永不就地修改。规则的新增、停用（`enabled=false`）与升级（换 rule_id/参数）
  都通过发布新版本完成。
- **历史不变**：历史交易的结论落库后不再变化；事件中也保存结论快照。
  规则升级后，旧交易仍保留当时的 `rule_version` 与命中明细；补录更晚的交易
  也不会改动任何已有结论与事件。
- **可重放**：
  - `POST /transactions/{txn_id}/replay`（不带版本）→ 取交易 `submitted_at`
    时刻生效的规则版本复算，与补录/实时首评结论**逐字段一致**；
  - 显式传 `{"version": n}` → 用任意历史/新版本复算以展示差异，
    但**不会覆盖**已落库的历史结论。
  - velocity 规则只统计"发生时间在窗口内且不晚于目标笔"的交易；
    实时首评把目标笔自身计入，整批复算把批内全部交易计入，重放时全部已落库，
    三种路径计数口径一致，窗口不会滑入未来交易，保证结论可复现。

### 2.2 生效时间语义

- 每个规则版本带显式的 `effective_at`（发布时不传则取发布当下），
  **从该时刻起才对交易生效**；一笔交易适用的版本是
  `effective_at <= submitted_at` 的最新版本。
- 新版本的生效时间必须**严格晚于**所有已有版本的生效时间。
  出现下列"会让历史结论说不清"的发布时，**整批拒绝、不产生新版本**，
  且拒绝原因与规则内容本身非法明确区分：
  - 生效时间早于或等于当前最晚的已生效版本；
  - 与某个已有版本的生效时间落在同一时刻。
  此时抛 `RuleEffectiveTimeConflictError`（HTTP 409，
  code=`RULE_EFFECTIVE_TIME_CONFLICT`），而**不是**内容非法的
  `RuleValidationError`（HTTP 422，code=`INVALID_RULESET`），系统不会自行挑版本凑合。
- 补录交易的 `submitted_at` 若**早于系统中最早生效版本**的生效时间，
  明确拒绝（`NoEffectiveRuleVersionError`，HTTP 422，
  code=`NO_EFFECTIVE_RULE_VERSION`），不拿最新规则硬套结论。
  恰好等于最早生效时间的交易按该版本判定（边界含等号）。

### 2.3 整批补录与整批校验

- `POST /transactions/batch` 一次补录一批历史交易：按各自 `submitted_at`
  独立选版本判定，整批在同一原子区内**要么全部落库，要么一笔不落**——
  任一笔失败都不会留下半截结论，也不会凭空多出事件。
- 补录批次与正在处理的实时交易并发交叠时，同一交易号的占位互斥依然成立。
- **整批校验、整批拒绝**：一批规则中任意一条结构/取值非法（类型不支持、参数越界、
  `rule_id` 缺失或批次内重复、`enabled` 非布尔、params 非对象等），
  立即抛 `RuleValidationError`（HTTP 422），**不产生新版本**，
  当前生效版本不变 —— 不部分生效、不静默降级。

### 2.4 同一交易号只评估一次

同一 `txn_id` 无论重复提交、并发提交，还是同号但关键内容对不上，都只能评估一次，
三种拒绝原因彼此可区分（关键内容 = 账户、金额、币种、交易对手；
`submitted_at` 是评估属性，实时重试间相差微秒不视为内容不一致）：

| 情形 | 异常 / code |
|---|---|
| 已完成，关键内容完全一致（含批次内同号） | `DuplicateTransactionError` / `DUPLICATE_TRANSACTION` |
| 已完成，但关键内容对不上 | `TransactionContentMismatchError` / `TRANSACTION_CONTENT_MISMATCH` |
| 另一路提交正在评估中 | `ConcurrentSubmissionError` / `CONCURRENT_SUBMISSION` |

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
| `POST /rules/versions` | 校验并发布规则新版本（可带 `effective_at`） |
| `POST /transactions` | 提交单笔交易并评估（实时/补录；幂等拒绝） |
| `POST /transactions/batch` | 整批补录历史交易（整批原子） |
| `POST /transactions/{txn_id}/replay` | 按当时/指定版本重放 |
| `POST /events/{id}/actions` | 事件处置：`confirm`/`mark_false_positive`/`escalate`/`close` |
| `GET /events/{id}` | 事件详情（输入 + 结论快照 + 处置历史） |

拒绝原因通过响应体 `error.code` 可区分：

| code | HTTP | 触发场景 |
|---|---|---|
| `DUPLICATE_TRANSACTION` | 409 | 交易已完成评估后的重复提交（含补录批次内同号） |
| `TRANSACTION_CONTENT_MISMATCH` | 409 | 同交易号但关键业务内容（账户/金额/币种/对手）对不上 |
| `CONCURRENT_SUBMISSION` | 409 | 同一交易评估进行中的并发提交（含批次与实时交叠） |
| `INVALID_TRANSITION` | 409 | 非法事件状态流转 / 未知动作 / 事件不存在 |
| `RULE_EFFECTIVE_TIME_CONFLICT` | 409 | 规则内容合法，但生效时间不晚于/等于已有版本，整批拒绝 |
| `NO_EFFECTIVE_RULE_VERSION` | 422 | 交易发生时刻早于最早生效版本（含补录批次中的此类交易） |
| `INVALID_RULESET` | 422 | 规则集/补录批次内容非法，整批拒绝 |

## 5. 本地验证

```bash
uv sync                                   # 安装依赖（含 pytest）
uv run pytest                             # 全部单测（41 个），日志含 txn_id / rule_version / 判定依据
uv run pytest --log-cli-level=INFO        # 查看 ASSESS / ASSESS-BATCH / REPLAY / EVENT 判定日志
uv run python main.py                     # 启动服务 http://127.0.0.1:8000/docs
# 或：uv run uvicorn app.api:app --reload
```

> 所有用例使用固定时间常量（基线版本从 2000-01-01 生效、交易集中在 2026 年内），
> **不依赖跑用例当天的日期**；已用模拟时钟在 2025/2026/2027/2030 四个
> "当前日期"下验证，任意一天执行结果都一致。

手工冒烟：

```bash
# 账户 + 带生效时间的规则版本
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"account_id":"ACC-1"}'
curl -s -X POST localhost:8000/rules/versions -H 'Content-Type: application/json' \
  -d '{"note":"v1","effective_at":"2026-01-01T00:00:00Z","rules":[{"rule_id":"AMT-1000","rule_type":"amount_limit","params":{"limit":1000,"score":120}}]}'

# 补录一笔历史交易（按发生时刻 2026-03-01 生效的版本判定）
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' \
  -d '{"txn_id":"T-HIST","account_id":"ACC-1","amount":5000,"submitted_at":"2026-03-01T10:00:00Z"}'
# 首评结论 == 单独重放
curl -s -X POST localhost:8000/transactions/T-HIST/replay

# 整批补录（整批原子）
curl -s -X POST localhost:8000/transactions/batch -H 'Content-Type: application/json' \
  -d '{"transactions":[{"txn_id":"B-1","account_id":"ACC-1","amount":5000,"submitted_at":"2026-03-02T10:00:00Z"},{"txn_id":"B-2","account_id":"ACC-1","amount":10,"submitted_at":"2026-03-03T10:00:00Z"}]}'

# 同号重复 -> 409 DUPLICATE_TRANSACTION；改金额 -> 409 TRANSACTION_CONTENT_MISMATCH
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' \
  -d '{"txn_id":"T-HIST","account_id":"ACC-1","amount":5000,"submitted_at":"2026-03-01T10:00:00Z"}'
```

### 兼容范围

- 规则发布：可显式指定 `effective_at`，也可不传（取发布当下，向后兼容旧调用）。
- 交易提交：可显式指定 `submitted_at`（补录），也可不传（实时，行为不变）。
- 历史重放与既有结论、事件结构保持不变；新版本号仍单调递增。
- 时间统一按 UTC 归一化：带时区的时间戳按其绝对时刻比较，不带时区的按 UTC 处理。

### 测试覆盖

| 文件 | 覆盖点 |
|---|---|
| `tests/test_dedup_concurrency.py` | 顺序重复提交、并发提交恰好一次成功、拒绝原因可区分、无重复结论/事件 |
| `tests/test_rule_versions_replay.py` | 非法规则整批拒绝且不产生新版本、v2 升级不改历史结论、默认/指定版本重放、重放不覆盖历史 |
| `tests/test_event_transitions.py` | 合法全链路、非法流转拒绝且不留痕、终态约束、未知动作、事件快照可追溯 |
| `tests/test_api.py` | HTTP 错误码区分、422 整批拒绝、非法流转 409、事件追溯、补录/生效时间/批量端点 |
| `tests/test_backfill_effective_time.py` | 补录按发生时刻判定、首评==重放、生效时间冲突与内容非法可区分、早于最早版本拒绝、整批原子、批内 velocity 可复现、批次与实时交叠的并发拒绝 |

## 6. 模块结构

```
app/models.py    枚举、值对象、业务异常（含生效时间冲突/无生效版本/同号异内容）
app/store.py     线程安全本地存储；评估占位与关键内容比对；append-only 规则版本与
                 生效时间严格递增校验；整批原子落库；批内 pending 计数
app/rules.py     规则批量校验（整批拒绝）
app/engine.py    判定引擎：按发生时刻取版本、去重/并发、打分决策、整批补录、历史重放、日志
app/events.py    事件状态机、审计历史、原子区内只构造不落库的事件
app/service.py   门面服务（账户/发布规则/单笔与整批提交/重放/处置）
app/api.py       FastAPI 入口与可区分错误码
```
