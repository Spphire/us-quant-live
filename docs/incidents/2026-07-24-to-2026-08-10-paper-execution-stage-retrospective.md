# 模拟盘执行阶段漏洞复盘

- **复盘范围**：2026-07-24 至 2026-08-10（Asia/Tokyo 运行环境，交易时间和原始证据以 UTC 记录）
- **账户类型**：Alpaca Paper
- **覆盖对象**：从 broker preflight、目标可执行化、报价捕获、分阶段下单、重试/改价，到最终仓位和审计回填
- **结论状态**：生产 `main` 当前已移除 lot ledger；本文件记录的是这段实测期间暴露出来的漏洞和防线，不是对策略收益好坏的判断

## 1. 结论摘要

这段实测暴露的主要问题不是一个单一的“下单失败”，而是四类状态被混在了一起：

1. **策略目标不可直接执行**：raw alpha 权重还要经过总 RegT capacity、购买力安全垫、最小成交金额和空头整数股约束。这个差距属于 `strategy -> executable` 投影误差，不应归咎于券商执行。
2. **可执行目标没有被准确完成**：订单被拒绝、报价不新鲜、重试预算耗尽或提交结果未知，才属于 `executable -> actual` 执行误差。
3. **券商仓位账本本身可能不连续**：2026-08-04/05 的 `VISN`/`PRAX` 事件显示，重复读取券商 API 也可能稳定地返回错误状态。这不是本地订单并发造成的普通误差。
4. **审计口径曾经不一致**：P&L 时间窗、bars 覆盖范围、价格证据范围和执行终态曾经不完全对齐，导致健康的执行被显示成异常，或让真实异常难以归因。

截至 2026-08-10 的可见执行证据：

- 共有 **11 个执行目录**，对应 11 个完成的执行日；2026-08-06 有 prepare/decision 产物，但没有 execute 目录，因为决策阶段的连续性保护阻断了提交。
- 7/28 的执行任务经历了 3 次报价新鲜度保护性失败，第 4 次完成；这不是 3 次重复开单。
- 7/28 至 8/10 已有终态审计的 9 个执行日中，`terminal_canceled_attempt_count=0`、`terminal_unfilled_record_count=0`；共记录 27 次 `superseded_requote`，它们是改价过程中的中间取消，不等于逻辑订单失败。
- 已有可比的最终 `executable -> actual` L1 权重误差在 **0.1183% 至 0.5805%**；8/10 最终约 **0.1920%**，最大单标的误差约 **0.0114%**。
- 2026-08-10 首次运行曾把 BUD、SHOP 误判为终态未成交；券商实际已接收并成交，连接结果丢失后重试沿用了相同 `client_order_id`，得到 `40010001 client_order_id must be unique`。这是真正的“未知提交结果被当成失败”漏洞，已由 `ec9c794` 修复。

## 2. 执行日时间线

下表中的 `L1` 是执行完成后实际仓位相对于本次 `executable target` 的总绝对权重误差；不是 raw alpha 相对于 executable target 的投影误差。早期产物没有统一保存 logical attempt summary，因此 `n/a` 表示证据字段尚未标准化，不表示当日没有审计。

| 日期 | 最终任务 | 计划订单 | 券商尝试 / 逻辑订单 | 中间取消 | 终态未完成 | 执行耗时 | 完成后 L1 / 最大单标的误差 | 备注 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 07-24 | completed | 60 | n/a | n/a | n/a | 158.5s | 0.4935% / 0.0261% | 早期 lot ledger 仍在生产路径 |
| 07-27 | completed | 49 | n/a | n/a | n/a | 163.5s | 0.3862% / 0.0612% | 早期审计字段仍未完全统一 |
| 07-28 | completed（第 4 次） | 37 | 39 / 37 | 2 | 0 | 116.0s | 0.2493% / 0.0207% | 前 3 次被 stale quote 防线阻断 |
| 07-29 | completed | 64 | 66 / 63 | 3 | 0 | 165.3s | 0.2273% / 0.0143% | 并行执行和改价记录生效 |
| 07-30 | completed | 70 | 75 / 69 | 6 | 0 | 182.5s | 0.2565% / 0.0791% | 主分支移除 lot ledger |
| 07-31 | completed | 68 | 72 / 69 | 3 | 0 | 186.1s | 0.2788% / 0.0173% | P&L/执行周期口径开始收敛 |
| 08-03 | completed | 72 | 77 / 74 | 3 | 0 | 202.0s | 0.5805% / 0.1804% | 多空释放合并为 unified reduction pool |
| 08-04 | completed | 74 | 79 / 74 | 1 | 0 | 223.7s | 0.1183% / 0.0061% | `VISN` 券商仓位消失事故 |
| 08-05 | completed | 81 | 88 / 86 | 1 | 0 | 206.9s | 0.2088% / 0.0143% | `PRAX` 消失、`VISN` 恢复造成连续性破坏 |
| 08-06 | 无 execute | n/a | n/a | n/a | n/a | n/a | n/a | strict continuity 因 `PRAX` 数量漂移阻断决策 |
| 08-07 | completed | 75 | 80 / 76 | 4 | 0 | 225.2s | 0.1696% / 0.0251% | rebalance 模式接受稳定的当前券商状态 |
| 08-10 | 首次 completed_with_errors；随后重跑 completed | 86 | 115 / 99 | 4 | 0 | 380.8s | 0.1920% / 0.0114% | BUD/SHOP 的未知提交结果被错误分类，已修复 |

证据来源为对应目录下的 `execution_summary.json`、`execution_quality.json`、`execution_attempt_outcome_summary.json`、`scheduler_task_result.json` 和 `alignment_after_execution`。例如：
`artifacts/daily_alpaca_scheduler/20260810_execute/`。

## 3. 漏洞目录

### 3.1 把剩余 buying power 当成稳定的目标分母

**触发条件**：账户已有持仓、前序释放单尚未完成、价格发生变化，或者使用了不同阶段的 buying power 快照。

**错误机制**：剩余购买力会随着持仓、挂单和成交实时变化。如果把它直接当作每次执行的目标分母，目标资金规模会随执行阶段漂移，面板显示的“资金缺口”也会失去稳定含义。

**影响**：同一份 alpha 权重在不同阶段被缩放成不同的名义仓位；策略到可执行目标的 gap 和执行到实际仓位的 gap 无法比较。

**修复和当前口径**：

- 目标规模改为总 RegT capacity 的固定比例，当前生产配置为 `gross_capacity_target_ratio=0.95`。
- `95%` 是目标持仓的总 gross capacity 比例，不能解释成“剩余 buying power 还剩 95%”。
- 目标可执行化的优先级是：先最小化各标的资金权重误差，再在不恶化一级目标的条件下尽量接近 gross capacity。
- 相关证据字段：`total_buying_power_capacity`、`gross_capacity_target_notional`、`gross_capacity_target_gap_notional`、`tracking_error_l1_weight`。

**状态**：已修复；仍需保证每次 sizing 和 entry submit 前使用清晰标注时间的最新账户快照。

### 3.2 空头 fractional share 与整数股约束

**触发条件**：空头目标按资金权重换算出的数量不是整数，或调仓只产生小于一股的空头增量/减量。

**错误机制**：Alpaca 空头开仓/加仓要求整数股。若只在订单提交层 `floor`，会产生三种问题：

- 优化器认为可以实现的目标，订单层被舍零；
- 接近零交叉时，整数化方向可能把目标从 short 错推到 long，或反过来；
- 多空资金占比误差被错误标成“执行失败”，其实是离散可行域造成的 projection floor。

**影响**：小仓位账户尤其容易出现净多偏置；例如 8/10 诊断中多个空头差额只有数美元，因 `qty_rounded_to_zero` 没有生成订单。这些不是异常漏单，但必须进入 projection 误差和复盘记录。

**修复和当前口径**：

- 在 `src/executable_target_projector.py` 的可执行目标阶段显式建模 `short_sales_whole_shares_only`。
- 将整数股、购买力和总 gross cap 一起作为硬约束，先优化 L1 权重误差；购买力利用率仅是次级目标。
- 每日保留 `projection_error_floor_l1_weight_pct`、`tracking_error_short_l1_weight_pct`、`integer_short_absolute_notional_gap`、`blocked_target_count`。

**状态**：生产执行规则已修复；数学上的整数化误差不会为零，应与真实下单失败分开看。账户规模变化后应重新评估整数股对 short 侧的相对影响。

### 3.3 分阶段释放过于串行，重试预算按轮次重复计算

**触发条件**：同时有大量多头减仓、空头回补和新仓位进入，且每个标的都等待报价、提交、轮询和改价。

**错误机制**：早期流程把 `release_sell_long`、`release_buy_to_cover` 和 entry 的处理拆得过细，释放阶段按阶段串行，重试预算又可能在新一轮重新计算。这样会出现“最多重复 14 次流程”一类行为，延长报价有效期并放大状态漂移。

**影响**：

- 释放单整体延迟增加，entry 开始时使用的账户和报价已经不是规划时的状态；
- 释放完成前没有及时重建 entry 计划；
- 单个标的的重试次数看似受限，但跨 round 后实际尝试数超出预期。

**修复**：

- `1d81c36`：将同一阶段内的 Alpaca 订单并行化；
- `52a26a8`：将每个标的的 attempt budget 设为跨 round 的全局预算；
- `7049ee7`：把多头释放和空头回补统一到 `unified_reduce_exposure` pool，并在释放后以新仓位重建 entry；
- 当前诊断保留 `release_rounds`、`release_substages`、`entry_repair_rounds_completed`、`execution_workers` 和各阶段 reconciliation。

**验证**：8/3 以后产物已明确标注 `release_execution_mode=unified_reduce_exposure`；上述 8/3、8/4、8/5、8/7、8/10 均在第 1 个 release round 完成释放，未再出现 14 轮级别的重复流程。

**状态**：已修复主要串行瓶颈；仍需监控 worker 数、券商 rate limit、订单轮询和报价供应商的并发上限之间的平衡。

### 3.4 把改价过程中的取消当成终态失败

**触发条件**：marketable limit order 等待窗口结束，系统主动 cancel 后用更新报价重新下单。

**错误机制**：一条逻辑交易指令可能有多个 broker attempts。若按 attempt 统计，取消数会比逻辑订单数大；若没有 `cancel_reason` 和 logical instruction ID，会把正常的 `requote_wait_elapsed` 误认为订单被券商拒绝或策略没有执行。

**影响**：面板出现“订单被取消”“执行失败”，但实际最终已成交；执行成功率和失败率都被扭曲。

**修复**：

- `88e1667` 记录显式取消原因；
- `89b3eb0` 引入 residual entry repair 和 logical order outcome 分类；
- `execution_attempt_outcome_summary.json` 区分 `broker_attempt_count`、`canceled_attempt_count`、`superseded_requote_canceled_attempt_count`、`terminal_canceled_attempt_count` 和 `terminal_unfilled_record_count`。

**验证**：7/28 至 8/10 终态审计共记录 27 次 `superseded_requote`，终态取消为 0、终态未完成逻辑订单为 0。今后看“是否执行失败”应优先看 `terminal_unfilled_record_count`，再看 submit error，而不能只看 canceled attempts。

### 3.5 报价覆盖不足、报价过期和执行价格来源混用

**触发条件**：动态 pool 的标的数量超过 IEX 覆盖能力，Longbridge 订阅分片/刷新未及时完成，或决策价格被错误复用到实际提交。

**错误机制**：执行需要的是提交前的实时价位。历史 bar、决策阶段缓存价或过期 quote 不能静默作为可成交订单的价格证据。7/28 的执行在 Longbridge 报价年龄超过 5 秒时被安全阻断：

```text
LongbridgeQuoteError: DLR: stale_quote_age_ms=5990.356; ... VISN: stale_quote_age_ms=5994.568
```

当天随后两轮仍出现相同类型的 stale quote，直到第 4 次才完成。这是“报价防线过于严格/刷新不够及时”的可用性问题，不应通过放宽到历史价来掩盖。

**修复**：

- `c4b96fe` 引入 Longbridge 作为执行报价源；
- `94dacd0` 将 Longbridge snapshot refresh 分片并发化；
- `eb0d964` 强化 staged execution 中的 quote refresh、freshness、spread 和 halt 检查；
- `symbol_universe_intersection` 统一信号、报价和执行可覆盖范围；
- `27d08b0`、`f0fbae8` 将 context bars、execution bars 和 price evidence 的用途分开，禁止对可执行订单静默使用历史价格；
- 生产参数已从 5 秒边界调整为 10 秒，但仍记录实际 `quote_age_ms`，并保留无报价/过期/价差/停牌的阻断原因。

**状态**：主要逻辑已修复。剩余风险是 Longbridge 网络/订阅质量和 Alpaca/Longbridge 标的交集随时间变化；覆盖健康度必须在提交前成为硬门槛并进入告警。

### 3.6 决策和执行之间的仓位状态可能过期

**触发条件**：12:30 的全量数据决策与 22:00 的实际执行相隔较久；期间券商仓位可能因成交、外部操作、资金注入或券商账本修复发生变化。

**错误机制**：如果直接沿用早先的 current position，优化器算出的 delta 已经不是当前真实仓位到目标仓位的 delta。执行阶段越努力地完成旧 delta，结果反而可能越偏。

**修复**：

- `34613f3` 将全量 Alpha/动态 pool 数据作为缓存，在执行前用 Alpaca 真实仓位重新运行 DecisionEngine 和 executable projection；
- prepare、decision、execute、reconcile 的实际仓位源统一为券商 `GET /v2/positions` 快照；
- `position-continuity-mode=rebalance` 在当前三次数量采样内部稳定时接受当前状态，把它作为新的策略输入；
- `88eb00b` 在 execution timeline 标出 target recalculation 阶段，避免把重算时间藏在普通执行阶段里。

**8/6 的具体表现**：当天 prepare/decision 使用 `strict` 模式，连续性保护检测到 `PRAX` 跨快照数量漂移，明确报错并没有提交订单。这个行为保护了账户，但可用性上会造成一个无 execute 目录的交易日。8/7 改用 `rebalance` 后，3 次当前仓位采样稳定、漂移为 0，决策和执行正常完成。

**状态**：已从“旧目标无条件执行”改成“执行前刷新真实仓位后重算”。仍需对外部仓位变化分类：交易成交、人工操作、公司行为和券商账本修复不能只用同一个 drift 标签。

### 3.7 券商仓位消失/恢复导致的错误补仓

**触发条件**：券商 `GET /v2/positions` 暂时或持久地漏掉一个已有仓位，而本地执行器把该 API 视为真实状态源。

**事件**：

- 8/4，Alpaca API 的多次独立调用都漏掉原有 `VISN`，没有对应 sale、fill、transfer、corporate action 或现金收入。执行器按真实 API 状态补买了约 `182.577` 股；
- 8/5，原 `VISN` 数量恢复，与补买数量叠加，同时 `PRAX` 消失，执行器卖出恢复后的 VISN 超额并补回 PRAX；
- 结果形成显著 holding residual 和不再严格可归因的 P&L。

**判断**：这不是“本地没有调用券商 API”的证据。现有证据反而表明 prepare、reconcile 和 preflight 都调用了 Alpaca；问题在于券商 API 可能稳定地返回内部错误状态。重复读取只能证明响应稳定，不能证明券商账本本身正确。

**影响**：错误补仓、短时间重复风险敞口、持仓连续性断裂，以及无法将收益严格归因给 alpha 或执行。

**处理**：完整时间线、请求 ID、账户活动、公司行为排除、证据包和 Alpaca ticket `#332101` 记录在 [2026-08-04 Alpaca 事故报告](2026-08-04-alpaca-paper-visn-position-disappearance.md)。生产代码保留稳定性采样、数量 hash 和 continuity artifact；该事故仍应标记为 broker-side pending，而不是伪装成策略或执行器 bug。

**状态**：本地可检测和可审计，无法由执行器单方面修复券商账本。未收到券商确认前，不能把“3 次返回一致”当作绝对正确性证明。

### 3.8 lot ledger 的归因和整数空头拆分风险

**触发条件**：真实持仓是按标的净数量，而 lot ledger 试图同时维护因子 lineage、min-hold 和多空 lot；空头又只能整数股。

**错误机制**：整数空头调仓、部分成交、零交叉和 broker sync 回填会让一个真实券商数量无法稳定映射回原始 lot。此前 `broker_sync` 占 gross lot weight 的比例曾缓慢上升到约 92%，导致因子收益、min-hold 和换手继承都失去可信度。

**决策**：`16941d9` 将 lot ledger 从生产 `main` 移除，相关研究保留在 `lot-ledger` 分支。7/24 产物中仍可见 `lot_snapshot_*.json`，这正是迁移前的历史证据；之后的生产执行不应再依赖 lot 归属来计算真实仓位或下单数量。

**状态**：生产路径已剥离；lot 分支仍需单独证明整数空头拆分、部分成交和恢复语义后才能考虑回合并。

### 3.9 审计时间窗、分母和覆盖范围不一致

**触发条件**：用固定时刻快照、执行完成周期和收盘周期混合计算，或把全量 pool 的 bars 缺失数直接当成执行标的缺失。

**错误机制**：

- `Portfolio Equity`、daily long/short、execution-cycle side P&L 使用了不同时间端点；
- `Min Bars` 可能统计与本次执行无关的 universe 标的；
- Price Evidence、holding residual 和 account bridge 的 snapshot scope 不一致；
- `strategy -> executable` 和 `executable -> actual` 误差在面板中共用“error”字样。

**影响**：7/28 的 long/short 下跌而 Portfolio Equity 上涨等表象无法直接判断是策略问题还是口径错位；审计 residual 被放大或重复展示。

**修复**：

- `65a4e77`、`27d08b0`、`f0fbae8`、`a01695b`、`1c89d15`、`2023d8d`、`3422fc0` 逐步统一时间点和 coverage；
- 当前执行周期定义为“上次执行完成 -> 本次执行完成”；
- side contribution 定义为“上次执行完成 -> 本次执行开始前”，以同一组 pre-trade holdings 切分 long/short；
- execution effect 单独显示“本次执行开始前 -> 本次执行完成”；三段相加才应还原完整 execution-completion-to-completion return；
- bars 和 price evidence 以各自用途和标的集合判断，不再把全量下载缺口等同于执行缺口。

**状态**：生产面板和审计口径已统一到新定义；旧日期的历史数据不会自动获得缺失的旧快照，因此跨版本比较必须标注 `schema_version` 和是否 strict-ready。

### 3.10 下单 POST 的未知结果被当成失败并直接重试

**触发条件**：客户端发送 POST 后连接在响应返回前断开，调用方无法知道订单是否已被券商接受/成交。

**错误机制**：把 `RemoteDisconnected` 或其他 transport error 直接视为“未提交”，再次使用相同的 `client_order_id` 重试。若第一次请求实际上已到达 Alpaca，重试会收到：

```text
HTTP 422: {"code":40010001,"message":"client_order_id must be unique"}
```

8/10 的 BUD 和 SHOP 就触发了这一链路。第一轮将它们记成 terminal unfilled，scheduler 于是重跑；券商订单审计和最终 reconcile 表明原请求其实已成交。

**风险**：没有恢复策略时，换成新 ID 又可能产生重复成交；继续用旧 ID 又可能把已经成交的订单误报为失败。

**修复**：`ec9c794` 实现了以下顺序：

1. transport/响应异常后先按 `client_order_id` 查询券商订单；
2. 校验 symbol、side、qty、type、limit price 等关键字段后接管已存在且匹配的订单；
3. 已存在但被取消时执行 requote；
4. 查不到匹配订单时才生成新的 UUID-based ID；
5. 将 `submit_recovery_outcome` 写入审计，并对终态未成交指令设置明确 retry status。

**状态**：代码已修复；8/10 事故发生时的第一次运行是修复前暴露，第二次最终运行 `terminal_unfilled=0`、无 open orders，最终 107 个 live positions 与保存状态数量一致且数量 drift 为 0。未知 POST 结果永远不能用“无条件新单重试”处理。

## 4. 修复验证矩阵

| 风险 | 主要修复 | 证据/验收标准 | 当前状态 |
| --- | --- | --- | --- |
| capacity 分母漂移 | 总 RegT capacity + 95% gross target | `gross_capacity_target_ratio=0.95`；entry 前刷新账户 | 已修复 |
| 空头小数股 | projector 和 order layer 同时强制整数股 | `short_sales_whole_shares_only`、integer gap、short L1 | 已修复，存在可解释离散误差 |
| 释放阶段串行 | unified reduction + workers | 8/3 后 `unified_reduce_exposure`；release round 完成 | 已修复 |
| round 重试膨胀 | 全局 symbol attempt budget | `stage_symbol_attempt_count_*`、bounded rounds | 已修复 |
| 改价取消误报 | logical/attempt outcome 分类 | `superseded_requote` 与 terminal outcome 分离 | 已修复 |
| quote 覆盖/新鲜度 | Longbridge + intersection + hard freshness gate | `quote_provider_health`、`symbol_universe_intersection` | 已修复，需持续监控 |
| 决策状态过期 | execute 前 fresh broker positions + cached alpha rerun | 8/7、8/10 continuity pass | 已修复 |
| broker 账本异常 | stability sampling + evidence + external escalation | VISN/PRAX 事故包、ticket `#332101` | 外部待确认 |
| lot lineage 污染 | 从生产 main 移除 | `16941d9`，研究在 `lot-ledger` | 生产已隔离 |
| audit scope mismatch | canonical execution-cycle semantics | `schema_version`、strict-ready、side/effect 三段 | 已修复，旧日需标注 |
| unknown POST result | idempotent recovery by client order ID | `submit_recovery_outcome`、订单字段校验 | 已修复 |

## 5. 当前生产不变量

每次执行完成后，以下条件应作为“可接受”的最低判定，而不是只看程序返回码：

1. **真实状态源**：prepare、decision preflight、release 后 reconcile、entry 后 reconcile、final snapshot 都来自券商 API；本地缓存只允许承载 Alpha/Universe，不得承载真实持仓。
2. **状态稳定性**：同一阶段的多次数量采样稳定；若不稳定必须记录 symbol、旧数量、新数量、请求时间和 disposition。
3. **目标层次清楚**：raw strategy target、capacity-adjusted target、executable target、actual broker position 四者分别落盘。
4. **误差优先级清楚**：先看每标的 signed capital weight error 的 L1/L∞，再看总 gross target 与 95% capacity 的差；不能拿 capacity gap 替代 weight error。
5. **订单终态清楚**：`terminal_unfilled_record_count`、`terminal_canceled_attempt_count`、submit errors 和 superseded requote 分开统计。
6. **未知响应可恢复**：任何 transport error 后必须先查询旧 `client_order_id`，不能直接换 ID 或重复提交。
7. **空头可执行**：空头 sell/增仓数量必须是整数；舍零、min notional 和 zero-crossing 必须进入 projection diagnostics。
8. **最终收敛**：final broker positions、expected signed qty、open orders 和 account buying power 都重新读取并写入 artifact；不能只根据本地 fill response 判定完成。
9. **审计可复现**：保留 command、git commit、输入 hash、报价 provider、quote age、订单 attempts、阶段时间轴和最终 reconciliation。

## 6. 后续回归和监控清单

### 每个交易日自动检查

- [ ] 任务状态和 execute artifact 是否存在；无 execute 目录必须标为 `blocked_before_submission`、`scheduler_failure` 或 `not_scheduled`，不能显示成普通 0 交易。
- [ ] `position_continuity_guard` 是否 pass；若 drift，列出 symbol 和 signed quantity，而不是只给一个红色 attention。
- [ ] `quote_provider_health` 的覆盖数、最大 quote age、spread、halt 和 fallback-only 数量。
- [ ] logical instructions、broker attempts、superseded cancels、terminal unfilled 的四个数量。
- [ ] `submit_recovery_outcome` 是否存在于所有 transport error；同一 `client_order_id` 是否只对应一个逻辑订单。
- [ ] release 和 entry 的并发 worker、round 数、阶段开始/完成时间及每个 symbol 的 attempt budget。
- [ ] `strategy_to_executable`、`executable_to_actual`、`gross_capacity_gap` 三条指标分别判断，不合并成 `error`。
- [ ] final positions 与 broker API 二次只读检查的数量、数量 hash、open orders 和账户权益是否一致。

### 换券商前的纸面/模拟盘验收

- [ ] 证明目标 universe 是 alpha、报价和下单能力的交集，并输出缺失标的清单。
- [ ] 用真实报价适配器完成全标的并发 snapshot，验证 freshness、spread、halt 和重连。
- [ ] 随机生成多空均权目标，至少重复 10 次，比较串行和并行的总耗时、quote age、submit error、terminal unfilled 和 executable-to-actual L1。
- [ ] 注入 POST 响应丢失、重复 ID、HTTP 429/5xx、订单取消、部分成交和券商仓位漂移，确认每种场景都不会重复成交。
- [ ] 用不同账户规模测试空头整数股的 projection floor，确认报告把离散不可行误差标为约束成本，而不是执行失败。
- [ ] 将一次完整流程导出成 execution timeline：quote capture -> projection -> release -> release reconcile -> entry -> repair -> final reconcile。
- [ ] 对历史旧 schema 做回放时，先检查 evidence coverage 和时间窗，再决定是否允许与新口径合并比较。

## 7. 证据索引

- 每日执行产物：`artifacts/daily_alpaca_scheduler/YYYYMMDD_execute/`
- 每日运行日志：`artifacts/daily_alpaca_scheduler/logs/YYYYMMDD_execute.out.log`
- 7/28 报价新鲜度失败：`artifacts/daily_alpaca_scheduler/logs/20260728_execute.out.log`
- 8/6 连续性阻断：`artifacts/daily_alpaca_scheduler/logs/20260806_decision.out.log`
- 8/10 未知提交结果：`artifacts/daily_alpaca_scheduler/logs/20260810_execute.out.log`
- 8/4-8/5 券商仓位事件：[2026-08-04 Alpaca Paper Position Disappearance And Restoration Recurrence](2026-08-04-alpaca-paper-visn-position-disappearance.md)
- 生产移除 lot ledger：`git show 16941d9`
- staged execution 并行和重试边界：`git show 1d81c36`、`git show 52a26a8`、`git show 7049ee7`
- Longbridge quote execution：`git show c4b96fe`、`git show eb0d964`、`git show 94dacd0`
- 执行审计语义：`git show 65a4e77`、`git show 27d08b0`、`git show f0fbae8`、`git show a01695b`
- cached decision / continuity：`git show 34613f3`、`git show 88eb00b`
- executable target projector：`git show 1705e84`
- ambiguous submission recovery：`git show ec9c794`

## 8. 复盘边界

本文件不能据此证明策略 alpha 有效或无效。要分析策略本身，必须先剔除以下非策略项：broker ledger continuity break、quote stale/coverage block、integer-short projection floor、min-trade filter、订单提交未知结果，以及旧日期的审计口径不一致。只有在同一 execution-completion-to-completion 周期、同一实际仓位来源和同一 symbol intersection 下，long/short P&L 与 account return 才适合做策略归因。

