# Alpaca 空头整数股约束下的持仓管理与回测报告

基于 Phase7K 多空组合的 short target floor baseline、执行口径对照与资金容量影响

量化策略研究笔记

2026 年 6 月 8 日

## 摘要

Alpaca 的 fractional trading 文档明确说明：fractional orders 不支持 short sales，fractional sell orders 会被标记为 long；orders 文档同时说明开空/加空会触发 buying power 检查，market short order 按 3% above current ask price 估算占用。因此，对 Phase7K 这种多空组合而言，空头不是“最好整股”，而是必须以可执行的整数股口径管理。

本文给出推荐方案：在生成订单前，先按账户权益和参考价将每个空头目标股数向下取整，多头目标保持不变；随后订单层仍对任何会创建或增加空头的 sell order 强制整股，作为第二道保护。该 baseline 会自然降低空头名义，使组合出现净多头偏置，从而在上涨环境中吃到更多 beta，但也会放大回撤、波动和风格漂移。为量化影响，本文对 2024-05-20 至 2026-05-20 区间进行四类执行口径、四档资金量的并行回测。

回测结论非常直接：10,000 美元账户下，baseline target-floor 只部署约 60.49% 目标空头，平均净多头偏置约 40.80%，总收益从理想小数股口径的 75.58% 提升至 96.33%，但最大回撤从 -9.20% 放大至 -18.76%，年化波动从 18.52% 放大至 24.30%。资金量提升后影响迅速收敛：300,000 美元账户 baseline 的空头部署率为 98.17%，相对理想口径总收益只高 1.93 个百分点，最大回撤仅多 0.05 个百分点。

关键词：Alpaca；fractional short sale；整数股；多空组合；仓位投影；净多头偏置；beta 暴露；资金容量

## 1 问题定义

Phase7K 的优化器输出的是连续 signed weight：

\[
w_{i,t}^{*}\in\mathbb{R},\qquad
w_{i,t}^{*}>0 \text{ 表示多头},\quad w_{i,t}^{*}<0 \text{ 表示空头}. \tag{1}
\]

若账户权益为 \(E_t\)，参考价格为 \(P_{i,t}\)，则理想小数股空头目标为

\[
q_{i,t}^{*,S}=\frac{E_t|w_{i,t}^{*}|}{P_{i,t}},\qquad w_{i,t}^{*}<0. \tag{2}
\]

在支持 fractional short 的理论 broker 中，式 (2) 可以直接执行。但 Alpaca 不支持 fractional short sale，因此若 \(q_{i,t}^{*,S}\notin\mathbb{Z}_{\ge 0}\)，空头目标必须投影到整数股集合。

## 2 推荐 Baseline

### 2.1 目标仓位投影

baseline 是按资金量将做空股向下取整。对每只空头：

\[
\widehat{q}_{i,t}^{S}=\left\lfloor q_{i,t}^{*,S}\right\rfloor
=\left\lfloor \frac{E_t|w_{i,t}^{*}|}{P_{i,t}}\right\rfloor. \tag{3}
\]

空头有效权重变为

\[
\widehat{w}_{i,t}^{S}
=-\frac{\widehat{q}_{i,t}^{S}P_{i,t}}{E_t}. \tag{4}
\]

多头保持优化器原目标：

\[
\widehat{w}_{i,t}^{L}=w_{i,t}^{*},\qquad w_{i,t}^{*}>0. \tag{5}
\]

因此空头欠部署名义为

\[
\Delta N_t^S
=\sum_{i:w_{i,t}^{*}<0}
\left(E_t|w_{i,t}^{*}|-\widehat{q}_{i,t}^{S}P_{i,t}\right). \tag{6}
\]

归一化净多头偏置为

\[
B_t^{net}=\frac{\Delta N_t^S}{E_t}. \tag{7}
\]

若原组合是 \(1\times\) 多头、\(1\times\) 空头，则投影后 gross exposure 和 net exposure 近似为

\[
\widehat{G}_t
=1+\sum_{i:w_{i,t}^{*}<0}|\widehat{w}_{i,t}^{S}|,
\qquad
\widehat{N}_t
=1-\sum_{i:w_{i,t}^{*}<0}|\widehat{w}_{i,t}^{S}|
=B_t^{net}. \tag{8}
\]

这就是“多头天然多一点”的数学来源。它不是 alpha 提升，而是空头无法充分部署后产生的 market beta / long book 偏置。

### 2.2 订单层保护

仅做订单层保护是不够的。若目标空头必须是整数股，但 buy-to-cover 用小数股回补，剩余空头股数仍可能变成小数。因此推荐的实盘顺序是：

1. broker 同步当前持仓；
2. alpha/optimizer 生成连续目标 \(w^*\)；
3. 按式 (3)--(5) 将所有空头目标投影为整数股；
4. 用投影后目标生成订单；
5. 对任何会创建或增加空头的 sell order，再强制整股向下取整；
6. 记录 \(\Delta N_t^S\)、\(B_t^{net}\)、zeroed short 数和 skipped order。

本文已在代码中加入：

- 回测：`--execution-scenarios`、`--short-sales-whole-shares-only`、`--floor-short-targets-to-whole-shares`；
- Alpaca executor：默认启用 `--floor-short-targets-to-whole-shares` 和 `--short-sales-whole-shares-only`；
- IBKR executor：同样支持上述参数，但默认不启用 target floor。

## 3 对照组定义

本文同时回测四类执行口径：

| 场景 | 定义 | 用途 |
|---|---|---|
| ideal_fractional | 多空均允许小数股 | 理想上限，对照策略本体 |
| opening_short_integer | 只有新开空订单整股 | 旧口径/弱约束诊断 |
| short_sale_integer | 创建或增加空头的 sell order 整股 | Alpaca short-sale 订单层约束 |
| baseline_floor_targets | 先投影所有空头目标为整数股，再执行 short-sale 整股保护 | 推荐 baseline |

上述四个场景使用同一组 alpha panel、同一组 decision target 和同一条 lot ledger，只改变执行层和目标仓位投影，因此比较的是 broker 约束对持仓实现的影响。

## 4 回测设置

主回测目录：

```text
artifacts/phase7k_backtest/short_integer_compare_20240520_20260520
```

参数设置：

| 项目 | 取值 |
|---|---:|
| 回测区间 | 2024-05-20 至 2026-05-20 |
| 有效交易区间 | 501 |
| 初始权益 | 10,000 / 50,000 / 100,000 / 300,000 |
| 股票池 | Alpaca active tradable clean-core，动态 Top1000 |
| 成本 | execution 8 bps + SEC fee + TAF |
| 交易口径 | open-to-open |
| 对照基准 | SPY / QQQ |
| SEC 数据 | backtest cache only |

回测命令：

```text
python src/backtest/phase7k_backtest.py --start-date 2024-05-20 --end-date 2026-05-20 --performance-warmup-sessions 0 --initial-equities 10000,50000,100000,300000 --execution-scenarios ideal_fractional,opening_short_integer,short_sale_integer,baseline_floor_targets --output-root artifacts/phase7k_backtest/short_integer_compare_20240520_20260520 --live-checkpoint-every-sessions 50 --sec-cache-mode cache_only
```

## 5 回测结果

### 5.1 绩效对比

| 场景 | 资金 | 期末净值 | 总收益 | 年化收益 | 最大回撤 | 年化波动 | 夏普 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ideal_fractional | 10,000 | 1.7558 | 75.58% | 32.73% | -9.20% | 18.52% | 1.61 |
| short_sale_integer | 10,000 | 1.7838 | 78.38% | 33.79% | -15.30% | 21.74% | 1.44 |
| baseline_floor_targets | 10,000 | 1.9633 | 96.33% | 40.40% | -18.76% | 24.30% | 1.52 |
| ideal_fractional | 50,000 | 1.7579 | 75.79% | 32.81% | -10.28% | 19.16% | 1.57 |
| short_sale_integer | 50,000 | 1.7791 | 77.91% | 33.61% | -10.13% | 19.44% | 1.58 |
| baseline_floor_targets | 50,000 | 1.8121 | 81.21% | 34.85% | -10.40% | 20.31% | 1.57 |
| ideal_fractional | 100,000 | 1.7533 | 75.33% | 32.64% | -10.39% | 19.37% | 1.55 |
| short_sale_integer | 100,000 | 1.7699 | 76.99% | 33.27% | -10.36% | 19.50% | 1.56 |
| baseline_floor_targets | 100,000 | 1.7866 | 78.66% | 33.90% | -10.56% | 19.97% | 1.55 |
| ideal_fractional | 300,000 | 1.7393 | 73.93% | 32.10% | -10.50% | 19.38% | 1.53 |
| short_sale_integer | 300,000 | 1.7454 | 74.54% | 32.33% | -10.48% | 19.43% | 1.53 |
| baseline_floor_targets | 300,000 | 1.7586 | 75.86% | 32.84% | -10.55% | 19.57% | 1.54 |

### 5.2 相对理想小数股口径的变化

| 场景 | 资金 | 总收益变化 | 年化收益变化 | 最大回撤变化 | 波动变化 | 夏普变化 |
|---|---:|---:|---:|---:|---:|---:|
| short_sale_integer | 10,000 | +2.80 pp | +1.06 pp | -6.10 pp | +3.22 pp | -0.17 |
| baseline_floor_targets | 10,000 | +20.75 pp | +7.67 pp | -9.55 pp | +5.78 pp | -0.10 |
| short_sale_integer | 50,000 | +2.11 pp | +0.80 pp | +0.15 pp | +0.28 pp | +0.01 |
| baseline_floor_targets | 50,000 | +5.41 pp | +2.04 pp | -0.12 pp | +1.15 pp | -0.00 |
| short_sale_integer | 100,000 | +1.66 pp | +0.63 pp | +0.03 pp | +0.13 pp | +0.02 |
| baseline_floor_targets | 100,000 | +3.32 pp | +1.26 pp | -0.18 pp | +0.60 pp | +0.01 |
| short_sale_integer | 300,000 | +0.61 pp | +0.23 pp | +0.02 pp | +0.05 pp | +0.01 |
| baseline_floor_targets | 300,000 | +1.93 pp | +0.73 pp | -0.05 pp | +0.19 pp | +0.02 |

### 5.3 Baseline 空头部署率

| 资金 | 目标空头部署率 | 空头欠部署率 | 平均净多头偏置 | 95% 分位净多头偏置 | 最大净多头偏置 |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 60.49% | 39.51% | 40.80% | 56.17% | 62.46% |
| 50,000 | 90.14% | 9.86% | 10.14% | 15.25% | 17.85% |
| 100,000 | 94.78% | 5.22% | 5.38% | 8.44% | 9.83% |
| 300,000 | 98.17% | 1.83% | 1.89% | 3.22% | 3.66% |

该表解释了收益变化的来源：10,000 美元账户下，每天有大量目标空头因为单票目标名义不足 1 股而被下取整到 0 或显著缩小，组合实质上更像“多头 + 不完整空头 hedge”。在 2024--2026 的上涨环境里，这提高了收益；但如果市场反向，风险同样会被放大。

## 6 结果解释

### 6.1 小资金账户

10,000 美元账户的 baseline target-floor 总收益最高，但这不是免费的 alpha。其平均净多头偏置约 40.80%，最大回撤达到 -18.76%，明显高于理想小数股口径的 -9.20%。这说明小资金阶段最大的风险不是 optimizer，而是空头离散化导致的 hedge 失真。

### 6.2 中等资金账户

50,000 至 100,000 美元账户仍有离散化影响，但已经可控。50,000 美元 baseline 的空头部署率为 90.14%，100,000 美元为 94.78%。收益较理想口径分别增加 5.41 和 3.32 个百分点，回撤变化很小，说明该资金段开始接近策略原始风险结构。

### 6.3 较大资金账户

300,000 美元账户下，baseline 空头部署率达到 98.17%，净多头偏置均值约 1.89%。此时整数股约束主要表现为轻微 residual beta，而不是主导风险来源。

## 7 最终方案

建议采用以下生产口径：

1. Alpaca 执行器默认启用 `--floor-short-targets-to-whole-shares`；
2. 同时保留 `--short-sales-whole-shares-only`，防止同步误差或临时订单导致 fractional short sale；
3. 订单计划必须保存 `raw_target_signed_weights` 与投影后的 `target_signed_weights`；
4. 每日记录 `target_short_floor_diagnostics`，至少包括 desired short notional、realized short notional、lost notional 和 zeroed short count；
5. 小资金账户单独设置风险阈值：若 \(B_t^{net}>b_{\max}\)，则降低 long book 或提高单名最低目标名义。

### 7.1 小资金风险修正选项

若希望减少净多头偏置，有三种可选增强：

1. 对多头按同等欠部署比例缩放：

\[
\widehat{w}_{i,t}^{L,scaled}=(1-B_t^{net})w_{i,t}^{L}. \tag{9}
\]

2. 对空头侧做整数 knapsack，把剩余可用名义分配给最接近 1 股且 alpha 排名最高的空头候选；
3. 设置最低可空名义阈值，只选择满足

\[
\frac{E_t|w_{i,t}^{S}|}{P_{i,t}}\ge 1 \tag{10}
\]

的空头，并让 optimizer 在候选层提前知道离散化约束。

本文建议 baseline 先不上述修正，因为当前回测显示 50,000 美元以上资金量已经较稳定；10,000 美元账户若要实盘，应将 baseline 当作“净多头增强版策略”，不能当作严格 market neutral 策略。

## 8 代码与产物

### 8.1 已更新代码

| 文件 | 更新 |
|---|---|
| `src/backtest/phase7k_backtest.py` | 支持多执行场景、short-sale 整股、target short floor、执行诊断 |
| `src/alpaca_executor.py` | 默认启用 target short floor 与 short-sale 整股保护 |
| `src/ibkr_executor.py` | 增加兼容参数，默认不启用 target floor |

### 8.2 回测产物

| 文件 | 内容 |
|---|---|
| `artifacts/phase7k_backtest/short_integer_compare_20240520_20260520/backtest_summary.json` | 主回测摘要 |
| `artifacts/phase7k_backtest/short_integer_compare_20240520_20260520/daily_backtest_results.csv` | 日度场景结果与执行诊断 |
| `artifacts/phase7k_backtest/short_integer_compare_20240520_20260520/scenario_metrics_summary.csv` | 场景绩效汇总 |
| `artifacts/phase7k_backtest/short_integer_compare_20240520_20260520/scenario_delta_vs_ideal.csv` | 相对理想小数股口径的变化 |
| `artifacts/phase7k_backtest/short_integer_compare_20240520_20260520/baseline_underhedge_summary.csv` | baseline 空头部署率与净多头偏置 |

## 9 参考资料

1. Alpaca Fractional Trading: https://docs.alpaca.markets/us/docs/fractional-trading
2. Alpaca Placing Orders: https://docs.alpaca.markets/us/docs/orders-at-alpaca
3. Alpaca Margin and Short Selling: https://docs.alpaca.markets/us/docs/margin-and-short-selling

