# 交易风险监控与可疑事件处置服务

基于 Python 3.14 + FastAPI 的本地内存实现，模拟账户、交易与规则数据，
保证同一批交易在不同入口（直接调用 / HTTP）与并发提交下得到**一致且可追溯**的判定结论。

## 1. 判定模型

### 1.1 处理流程

```
提交交易（两条入口，判定内核相同）
  │
  ├─ 实时通道 POST /transactions（未给发生时间）
  │     按"当前时刻已生效的最新版本"判定；评估入口冻结一次发生时刻，
  │     供 velocity 窗口累计与默认重放共用（live 通道独立计数）
  │
  ├─ 补录通道 POST /transactions/backfill（必须给 submitted_at）
  │     按交易自身发生时刻 effective_at <= submitted_at 的最新版本判定
  │     （historical 通道独立计数，不与实时笔数串用）
  │
  ├─ 1. 原子占位 claim_assessment（RLock 临界区）
  │       已完成结论且内容一致 ──► DuplicateTransactionError        （重复提交）
  │       在途占位且内容一致   ──► ConcurrentSubmissionError        （并发提交）
  │       同交易号但关键内容不一致 ──► TransactionContentConflictError（内容冲突）
  │
  ├─ 2. 取适用的规则版本快照（append-only，不可变）
  ├─ 3. 逐条规则评估（enabled=false 的规则跳过），累加命中分值
  ├─ 4. 阈值决策
  │       score >= 100  → reject（自动开立可疑事件）
  │       1 <= score <100 → review（自动开立可疑事件）
  │       score = 0       → pass（不开事件）
  └─ 5. 原子提交交易+结论并释放占位；review/reject 自动建事件（每笔交易至多一个）
        补录批次在单个临界区内整批提交，全有或全无
```

实时通道未给 `submitted_at` 时使用"实时"哨兵（不会因两次调用 `utcnow()` 的微秒差，
把同一笔的重复/并发重试误判为内容冲突）；补录必须显式给出真实发生时刻。
评估失败会释放占位（`release_assessment`），避免同笔交易被永久误判为"并发中"。

**实时交易的发生时刻在评估入口冻结一次**：正常当下提交（不给 `submitted_at`）时，
系统在评估开始时取一次当前 UTC 时刻作为这笔交易的"发生时刻"并随结论一起固化。
此后选规则版本、velocity 时间窗累计、默认重放都只用这个冻结时刻，
不会在每次计数/重放时重新取当前时间。因此：

- 同一账户短时间内连续提交的实时交易按真实提交间隔进入时间窗累计，
  窗口内笔数（含当前笔）超过 `max_count` 即命中并给分；未超阈值的笔不会误命中。
- 一笔实时交易的**首次结论与之后单独重放（不带版本参数）完全一致**——
  规则版本、命中规则、分值、最终结论逐项对得上；即使之后发布了更新的规则版本，
  默认重放仍按该笔冻结时刻生效的版本复算，不会出现"首评命中、重放不命中"或反过来。

**两条通道的 velocity 时间窗计数相互独立、互不污染**：实时通道（`live`）只累计
实时交易，补录通道（`historical`）只累计带发生时刻的补录交易。两条通道的交易
即便发生时刻重叠，也不会把对方的笔数算进自己的窗口。

**同一交易号的内容指纹**包含：账户、金额、币种、交易对手、发生时刻
（时区统一到 UTC，`+00:00` 与等价的 `+08:00` 表示视为同一时刻）。
只有交易号相同且指纹一致才算重复/并发；指纹不一致一律按"内容冲突"拒绝，
三种拒绝原因彼此可区分，且都不会产生第二条结论或事件。

### 1.2 规则类型（`params` 契约）

| 规则 | 参数 | 命中条件 |
|---|---|---|
| `amount_limit` | `limit > 0`，`score >= 0` | 单笔金额 > limit |
| `velocity` | `window_minutes >= 1`、`max_count >= 1`、`score >= 0` | 账户在 `(提交时间-window, 提交时间]` 窗口内（含当前笔）笔数 > max_count |
| `blocklist` | `counterparties` 非空字符串列表（不可重复）、`score >= 0` | 交易对手在黑名单内 |

决策结果 `Assessment` 固化：`txn_id`、`decision`、`score`、`rule_version`、
每条命中的 `rule_id / rule_type / rule_version / reason`、评估时间。

## 2. 规则版本与生效时间语义

- **append-only + 单调版本号**：每次发布生成单调递增的新版本，旧版本永不就地修改。
  规则的新增、停用（`enabled=false`）与升级（换 rule_id/参数）都通过发布新版本完成。
- **显式生效时间 `effective_at`**：每个版本只从自己的生效时间开始才对交易生效。
  判定适用版本时取 `effective_at <= 交易发生时刻` 的最新版本。
  - 发布时未给 `effective_at`，则取发布时刻（适合实时上线）。
  - 新版本的 `effective_at` 必须**严格晚于**已发布的最新版本的生效时间；
    给了更早的时间、或与某个已存在版本撞在同一时间点，整批拒绝
    （`RuleEffectiveTimeConflictError` / HTTP 409，code=`RULE_EFFECTIVE_TIME_CONFLICT`），
    **不挑一个版本凑合生效**。该原因与"规则内容本身非法"
    （`RuleValidationError` / HTTP 422，code=`INVALID_RULESET`）相互可区分；
    两种失败都不产生新版本，当前生效版本不变。
  - 允许发布"未来生效"的版本：在生效时间到达前，实时交易仍按当前已生效版本判定。
- **历史不变**：历史交易的结论落库后不再变化；事件中也保存结论快照。
  规则升级后，旧交易仍保留当时的 `rule_version` 与命中明细。
- **可重放**：
  - `POST /transactions/{txn_id}/replay`（不带版本）→ 取该笔交易"发生时刻"
    生效的规则版本复算，默认与首次结论一致：
    - 补录交易的发生时刻即其 `submitted_at`；
    - 实时交易的发生时刻即评估时冻结的那个当前时刻（而非重放时的当前时刻）。
  - 显式传 `{"version": n}` → 用任意历史/新版本复算以展示差异，
    但**不会覆盖**已落库的历史结论。
  - velocity 规则只按"该笔发生时刻之前（含自身）"的交易计数，
    重放时窗口不会滑入更晚的交易，结论可复现；且 live / historical
    两个通道各自计数，互不串用。
- **整批校验、整批拒绝**：一批规则中任意一条结构/取值非法（类型不支持、参数越界、
  `rule_id` 缺失或批次内重复、`enabled` 非布尔、params 非对象等），
  立即抛 `RuleValidationError`（HTTP 422），**不产生新版本**。

## 2.1 历史交易补录（backfill）

上游晚到的历史交易走补录通道 `POST /transactions/backfill`，每笔都必须显式带
`submitted_at`（交易真实发生时刻），系统按该时刻生效的规则版本判定。

- **首评 == 事后重放**：一笔补录交易第一次算出的结论，与之后单独调用
  `replay` 得到的结论完全一致——规则版本、命中规则、分值、最终结论逐项对得上。
  实现上，补录批次在评估 velocity 时把"同批尚未落库的交易"也计入窗口，
  因此批次首评看到的窗口与整批落库后重放看到的窗口相同，与批次内的提交顺序无关。
- **不碰既有数据**：补录只追加新交易、新结论、新事件，绝不修改任何已存在的
  交易结论及其事件（包括事件处置历史）。
- **早于最早版本明确拒绝**：补录交易的发生时刻若早于系统中最早生效的版本，
  抛 `NoEffectiveRuleVersionError`（HTTP 422，code=`NO_EFFECTIVE_RULE_VERSION`），
  不拿最新规则硬套结论。
- **整批原子（全有或全无）**：一批中任意一笔失败——重复提交、并发提交、内容冲突、
  账户不存在、缺 `submitted_at`、早于最早生效版本、批次内交易号重复/冲突——
  整批拒绝，已占位的资格全部回滚，**不留下半截结论，也不会凭空多出事件**；
  批次内互不冲突的其余交易在失败后仍可重新提交。
- **与实时通道并发交叠**：补录批次评估期间，实时通道提交同一交易号同样经过
  原子占位仲裁，并发冲突 / 重复 / 内容冲突三种原因可区分。
- **时间窗计数互不污染**：补录交易（historical）与实时交易（live）即使发生时刻
  交叠，velocity 也只在各自通道内累计——补录不会抬高实时窗口的笔数，实时连续
  提交也不会抬高补录窗口的笔数。
- **实时通道行为**：正常当下提交（不给 `submitted_at`）按当前已生效的最新版本
  判定，短时间内连续多笔正常参与频次累计（超阈值命中、给分；未超不误命中），
  首评与不带版本重放一致；幂等与并发拒绝语义保持不变。

兼容范围说明：补录结论的可复现性以"补录批次整批落库完成后"的存储状态为准；
velocity 窗口天然只回看发生时刻之前（含自身）的交易，因此之后补入更早的交易
不会改动任何已落库结论（对更早那笔自身而言，它在首评时也只会看到自己批次内
与更早的交易）。

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
| `POST /transactions` | 提交实时交易并评估（幂等拒绝） |
| `POST /transactions/backfill` | 整批补录历史交易（必带 `submitted_at`，全有或全无） |
| `POST /transactions/{txn_id}/replay` | 按当时/指定版本重放 |
| `POST /events/{id}/actions` | 事件处置：`confirm`/`mark_false_positive`/`escalate`/`close` |
| `GET /events/{id}` | 事件详情（输入 + 结论快照 + 处置历史） |

拒绝原因通过响应体 `error.code` 可区分：

| code | HTTP | 触发场景 |
|---|---|---|
| `DUPLICATE_TRANSACTION` | 409 | 同交易号、同内容，已完成评估后的重复提交（含批次内重复） |
| `CONCURRENT_SUBMISSION` | 409 | 同交易号、同内容，评估进行中的并发提交 |
| `TRANSACTION_CONTENT_CONFLICT` | 409 | 同交易号但关键内容对不上（无论对方已完成还是在途） |
| `RULE_EFFECTIVE_TIME_CONFLICT` | 409 | 规则内容合法，但新版本生效时间早于/等于已有最新版本 |
| `INVALID_TRANSITION` | 409 | 非法事件状态流转 / 未知动作 / 事件不存在 |
| `INVALID_RULESET` | 422 | 规则内容非法 / 补录批次结构非法，整批拒绝 |
| `NO_EFFECTIVE_RULE_VERSION` | 422 | 交易发生时刻早于系统中最早生效的规则版本 |

## 5. 本地验证

```bash
uv sync                                   # 安装依赖（含 pytest）
uv run pytest                             # 全部单测（50 个）；实时输出业务日志
uv run pytest -q                          # 只看用例结果（-v 与 log_cli 已在 pyproject 内默认开启）
uv run pytest tests/test_live_velocity_replay.py   # 只跑实时频次/重放/交叠回归
uv run python main.py                     # 启动服务 http://127.0.0.1:8000/docs
# 或：uv run uvicorn app.api:app --reload
```

`pytest` 默认开启 `-v` 与 `log_cli`：每个用例名下会实时打印 `risk.*` 业务日志
（`[ASSESS]` 首评、`[REPLAY]` 重放、`[BACKFILL]` 补录、`[RULES-PUBLISH]` 发版、
`[EVENT-*]` 事件处置），可直接看出该用例覆盖了哪块业务、命中了哪些规则、
分值与最终结论，同时屏蔽了 httpx 传输层噪音。

手工冒烟（实时频次：连续提交同账户多笔，第 3 笔命中 velocity；首评/重放一致）：

```bash
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"account_id":"ACC-1"}'
# 发布带明确生效时间的 v1：5 分钟内 >2 笔命中
curl -s -X POST localhost:8000/rules/versions -H 'Content-Type: application/json' \
  -d '{"note":"v1","effective_at":"2026-01-01T00:00:00Z","rules":[
    {"rule_id":"VEL-2-5M","rule_type":"velocity","params":{"window_minutes":5,"max_count":2,"score":50}}]}'
# 同账户连续提交 3 笔（不同交易号），第 3 笔 decision=review、score=50
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":10}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-2","account_id":"ACC-1","amount":10}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-3","account_id":"ACC-1","amount":10}'
# 不带版本重放 T-3：规则版本/命中/分值/结论与首评完全一致
curl -s -X POST localhost:8000/transactions/T-3/replay -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":10}'   # 409 DUPLICATE_TRANSACTION
```

历史补录冒烟：

```bash
curl -s -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"account_id":"ACC-1"}'
# 发布带明确生效时间的 v1
curl -s -X POST localhost:8000/rules/versions -H 'Content-Type: application/json' \
  -d '{"note":"v1","effective_at":"2026-01-01T00:00:00Z","rules":[{"rule_id":"AMT-1000","rule_type":"amount_limit","params":{"limit":1000,"score":120}}]}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":5000}'
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{"txn_id":"T-1","account_id":"ACC-1","amount":5000}'   # 409 DUPLICATE_TRANSACTION
# 补录一批历史交易：按各自 submitted_at 时刻生效的版本判定，整批原子
curl -s -X POST localhost:8000/transactions/backfill -H 'Content-Type: application/json' -d '{
  "transactions":[
    {"txn_id":"H-1","account_id":"ACC-1","amount":5000,"submitted_at":"2026-06-01T10:00:00Z"}
  ]}'
curl -s -X POST localhost:8000/transactions/H-1/replay -H 'Content-Type: application/json'   # 与补录首评逐项一致
```

### 测试覆盖

| 文件 | 覆盖点 |
|---|---|
| `tests/test_dedup_concurrency.py` | 顺序重复提交、并发提交恰好一次成功、两种拒绝原因可区分、无重复结论/事件 |
| `tests/test_rule_versions_replay.py` | 非法规则整批拒绝且不产生新版本、v2 升级不改历史结论、默认/指定版本重放、重放不覆盖历史 |
| `tests/test_event_transitions.py` | 合法全链路、非法流转拒绝且不留痕、终态约束、未知动作、事件快照可追溯 |
| `tests/test_backfill.py` | 生效时间单调/撞点/早于的区分化拒绝、未来版本不提前生效、补录按发生时刻取版、首评==重放（含 velocity 同批互见）、不改既有结论与事件、批量全有或全无、重复/并发/内容冲突三因可分、批次内重复/冲突、与实时通道并发交叠、时区等价表示、实时通道行为不变 |
| `tests/test_live_velocity_replay.py` | 实时连续多笔：阈值内不误命中、第 3 笔（>阈值）命中给分、窗口外不累计、按账户隔离；首评==不带版本重放（版本/命中/分值/结论），发新版后默认重放仍锁定首评版本；实时与补录时间窗计数双向不污染、补录自身频次语义不被破坏 |
| `tests/test_api.py` | HTTP 错误码区分、422 整批拒绝、非法流转 409、事件追溯详情、补录/生效时间/无生效版本的 HTTP 码 |

所有补录与版本相关用例都基于固定时间常量（如 `2026-01-01T00:00:00Z`、
`2026-06-01T10:00:00Z`）；实时通道相关用例则通过把 `app` 内时钟
（`app.store.utcnow` / `app.engine.utcnow`）冻结到固定锚点 `2026-06-01T12:00:00Z`
并手动推进来模拟"短时间内连续提交"。期望值不依赖跑用例当天的日期，任何一天运行结果一致。

## 6. 模块结构

```
app/models.py    枚举、值对象、业务异常、实时交易哨兵
app/store.py     线程安全本地存储；评估原子占位与内容指纹；实时发生时刻冻结；live/historical 双通道计数；append-only 规则版本与生效时间选择；整批原子落库
app/rules.py     规则批量校验（整批拒绝）
app/engine.py    判定引擎：去重/并发/内容冲突、版本快照评估、实时时刻冻结与按发生时刻补录、打分决策、历史重放、判定日志
app/events.py    事件状态机与审计历史
app/service.py   门面服务（账户/发布规则/实时提交/整批补录/重放/处置）
app/api.py       FastAPI 入口与可区分错误码
```
