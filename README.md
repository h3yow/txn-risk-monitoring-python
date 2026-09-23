# 交易风险监控与可疑事件处置服务

基于 FastAPI 的本地服务，用**线程安全的内存存储**模拟账户、交易与规则数据，实现：

1. 交易受理与**去重/并发**判定（每笔交易只评估一次）；
2. 规则集**版本化**管理与**历史重放**（规则升级不改变历史结论）；
3. 可疑事件的**状态机流转**与全程**可追溯**处置；
4. 覆盖并发提交、规则升级重放、非法流转的测试与审计日志。

## 一、判定模型

### 规则类型

| 规则类型 `rule_type` | 参数 | 命中条件 |
| --- | --- | --- |
| `amount_threshold` | `threshold`(正数), `score`(正整数) | `交易金额 >= threshold` |
| `blacklist` | `payees`(非空字符串数组), `score` | `收款人 payee ∈ payees` |
| `high_risk_country` | `countries`(非空字符串数组), `score` | `交易发起国 country ∈ countries` |

一次评估对**当前规则版本内所有 `active` 规则**逐条求值，命中分累加：

```
总分 < 50           -> allow   （放行）
50 <= 总分 < 100    -> review  （人工复核，生成可疑事件）
总分 >= 100         -> block   （拦截，生成可疑事件）
```

- 评估函数是**纯函数**：输入 `(交易输入, 账户, 规则版本快照)`，输出 `(decision, score, 命中明细)`，不依赖当前时间或可变全局状态，因此同一输入在同一版本下结果必然可复现。
- 每条命中明细 `RuleHit` 都带 `rule_id / rule_type / rule_version / score / reason`（判定依据的自然语言描述）。
- 只有 `review / block` 会创建一个可疑事件；`allow` 不产生事件，因此重复提交不可能产生重复告警。

### 交易去重：两阶段预留 + 三种可区分拒绝原因

`Storage.reserve_txn()` 在同一把 `RLock` 内原子完成"检查并占位"：

| 场景 | 时机 | HTTP | 错误码 |
| --- | --- | --- | --- |
| 同一 `txn_id` 正被另一请求评估（已占位、结论未落库） | 并发 | 409 | `concurrent_conflict` |
| 同一 `txn_id` 已有最终结论，且**载荷完全一致** | 重复提交 | 409 | `duplicate_submission` |
| 同一 `txn_id` 已有结论，但金额/账户/币种/国家/收款人**任一不一致** | 冲突重放 | 422 | `payload_mismatch` |

评估过程中若发生异常会 `release_txn` 回滚占位，避免失败请求把交易永久锁死。
结论与事件在同一把锁内一起落库，杜绝重复计分、重复事件。

## 二、规则版本与兼容规则

- 每次 `POST /rules/versions` 生成一个**只增、不可变**的新版本（自增版本号 + 发布时间快照）；规则的新增、停用、升级都通过发布新版本完成，**旧版本永不被改写**。
- **整批校验（all-or-nothing）**：发布前对整批规则做结构与取值校验（类型、必填参数、正数阈值、正整数分值、非空名单、规则 ID 批次内唯一、未知参数拒绝），任一非法即整批拒绝并返回全部错误明细，**不产生新版本、不部分生效、不静默降级**。
- **历史结论绑定版本**：`DecisionRecord` 固化 `rule_version`、总分、命中明细与每规则的版本；随后无论发布多少新版本，历史结论与已生成事件均不变。
- **历史重放**：`POST /transactions/{txn_id}/replay` 用当时保存的交易输入 + 指定历史版本快照重新纯计算，只返回重放结果，绝不改写既有结论/计分/事件；重放结果与既有结论（可能跨版本）一并返回并标注 `consistent`。
- 受理交易时默认使用最新版本，也可通过 `rule_version` 显式钉住某一历史版本（重算/补录场景）。
- 停用规则：新版本中不包含该规则即可（新版本是完整快照，而不是补丁）。

> 说明：当前为本地内存实现，重启后数据重置；版本模型本身与"追加写 + 快照"一致，替换为持久化存储时不需要改变判定语义。

## 三、可疑事件状态机

```
OPEN ──► CONFIRMED ──► ESCALATED ──► CONFIRMED
  │          │             │
  │          ▼             ▼
  │       CLOSED         CLOSED
  ├──► ESCALATED
  ├──► FALSE_POSITIVE   （终态）
  └──► CLOSED           （终态）
```

- 非法流转（含终态后再处置、转到未知状态、转到当前同一状态）一律拒绝并返回 `illegal_transition`（409）及 `from/to/allowed` 明细。
- 每次流转记录 `{from, to, operator, note, at}` 处置历史；事件本身固化 `txn_id、rule_version、hit_rule_ids、score、decision`，
  配合 `DecisionRecord` 可一直追溯到**触发时的原始交易输入、命中规则及其版本**。

## 四、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 登记账户（可选，账户信息不直接参与当前三类规则） |
| POST | `/transactions` | 受理并评估交易（幂等去重） |
| GET  | `/transactions/{txn_id}` | 查询既有结论（与受理入口返回完全一致） |
| POST | `/transactions/{txn_id}/replay` | 按指定规则版本历史重放 |
| POST | `/rules/versions` | 整批发布新规则版本 |
| GET  | `/rules/versions` | 列出所有规则版本 |
| GET  | `/events/{event_id}` | 查询事件与处置历史 |
| POST | `/events/{event_id}/transition` | 确认/误报/升级/关闭 |

业务错误统一返回体：`{"error": <原因码>, "message": ..., "details": {...}}`。

## 五、本地验证方法

需要 Python 3.14 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装依赖（含 pytest）
uv sync

# 2. 运行全部测试（日志中会打印 交易标识 / 规则版本 / 判定依据）
uv run pytest -q

# 3. 启动服务
uv run uvicorn main:app --reload
# 打开 http://127.0.0.1:8000/docs 可交互调试
```

### 手工演示脚本

```bash
# 发布 v1 规则
curl -s -X POST localhost:8000/rules/versions -H 'Content-Type: application/json' -d '{
  "rules":[{"rule_id":"r-amount","rule_type":"amount_threshold",
            "params":{"threshold":1000,"score":60}}],
  "note":"v1"}'

# 受理交易 -> review + 事件号
curl -s -X POST localhost:8000/transactions -H 'Content-Type: application/json' -d '{
  "txn_id":"T1","account_id":"A1","amount":5000,
  "currency":"CNY","country":"CN","payee":"P1"}'

# 再发一次 -> 409 duplicate_submission；并发窗口内则为 409 concurrent_conflict
# 载荷改金额 -> 422 payload_mismatch

# 发布 v2（阈值上调）后重放历史交易，观察 stored=v1/review 与 replay=v2/allow 并存
curl -s -X POST localhost:8000/transactions/T1/replay \
  -H 'Content-Type: application/json' -d '{"rule_version":2}'
```

## 六、审计日志

logger 名 `risk.audit`，测试与服务共用：

- `txn_evaluated txn_id=.. rule_version=v.. decision=.. score=.. event_id=.. basis=[规则(版本,+分):依据, ...]`
- `txn_duplicate_submission` / `txn_concurrent_conflict` / `txn_payload_mismatch`
- `rules_published version=.. rule_count=..`
- `txn_replayed txn_id=.. replay_rule_version=v.. replay_decision=.. stored=v../decision/score basis=[..]`
- `event_transition event_id=.. txn_id=.. from->to operator=.. rule_version=..`

## 七、代码结构

```
app/
  models.py         领域模型/枚举
  errors.py         业务异常（携带可区分 code）
  storage.py        线程安全内存存储 + 两阶段去重占位
  engine.py         规则整批校验 + 纯函数评估引擎
  events.py         事件状态机
  services.py       编排：受理/去重/发布/重放/处置 + 审计日志
  schemas.py        HTTP 请求模型
  api.py            FastAPI 路由与错误码映射
  logging_config.py 审计日志配置
tests/              去重并发 / 版本与重放 / 事件流转 / HTTP 集成
main.py             ASGI 入口
```
