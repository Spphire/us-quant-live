# 因子筛选与 Lot A/B 证据摘要

生成：2026-06-09

## 结论先行
因子入选标准：**低共线 + 经济互补 + 可归因 + 可赋予持仓周期 + 组合净收益改善**，而非严格数学正交。

## 已有证据
### 1. 信号层：真实 vs 随机控制
**数据源**：Phase3 baseline signals, 2017-01-03 ~ 2026-05-08 (2350 sessions)

| 股票池 | 信号 | Rank IC | Random IC | Spread | Random Spread | Paired t-stat (IC) |
|---|---|---:|---:|---:|---:|---:|
| top1000 | reversal_5d | 0.0173 | -0.00027 | 20.41 bps | 0.79 bps | 5.38 |

真实信号与 random_control 的 Spearman 相关 ~0，说明真实候选信号明显强于随机噪声。

### 2. 最终五因子样本随机对照
**数据源**：Phase7K feature_target_panel_sample.csv, 81 sessions (2016-01-04 ~ 2016-04-28, 平均 61.7 只/日)

| 真实组合 | 对照组 | 平均 Rank IC | 相对分位 |
|---|---|---:|---:|
| 当前五因子固定权重 | 200 个随机噪声因子 | 0.0243 | 95% |
| 当前五因子固定权重 | 200 组同五因子随机正权重 | 0.0243 | 75% |

**解读**：因子成员不随机，但当前权重 (0.25/0.10/0.30/0.20/0.15) 的最优性需要全历史 panel 的组合级 replay 才能严格证明。

### 3. 组合层：完整因子组 vs 消融
**数据源**：Phase7K strict_daily_metrics.csv (validation-only 口径，非最终生产)

| 组合 | 因子组 | Ann. Return | Net Sharpe | Max DD | Mean Turnover |
|---|---|---:|---:|---:|---:|
| shared_core | 全五因子 | 5.28% | 0.559 | -16.71% | 0.051 |
| shared_no_momentum | 去动量 | 4.94% | 0.531 | -13.51% | 0.045 |
| shared_slow_core | 只慢因子 | 2.25% | 0.238 | -25.27% | 0.051 |

### 4. Lot 管理 vs 无强制持有
**数据源**：早期 Phase7K 报告 (2014-11-13 ~ 2019-12-31)

| 方案 | 日均换手 | Net Sharpe |
|---|---:|---:|
| 无强制持有 Phase7J | 0.658 | ~0.55 |
| Phase7K locked-lot | 0.153 | 1.97 |

**机制**：每个 lot 记录 `(symbol, factor, weight, birth_idx, min_hold)`。不同因子不同最短持有期 (reversal=5, momentum=10, size/beta/cash=20 sessions)，锁定期内权重作为优化器下界，防止每日重优化噪声换手。

## 因子筛选硬条件
1. 不能有未来泄露：`xᵢₖₜᵃᵛᵃⁱˡᵃᵇˡᵉ ⊆ Fₜ₋₁,ᶜˡᵒˢᵉ`
2. 有清楚经济解释（人话"为什么多/空"）
3. 可行业标准化 (SIC2 内 z-score)
4. 不与已有因子高度重复：`|ρ(zₐ,zᵦ)| ≤ 0.60~0.70`
5. 通过随机控制检验：`IC_factor > IC_random`
6. 通过组合层增量检验：加入后改善 Sharpe/回撤/换手
7. 能赋予合理 lot 持仓周期

## 尚未完成的严格实验
**缺**：最终生产 AlphaCore 五因子 vs 随机抽取五因子组合的完整回测 replay。

**原因**：当前未保存 2017-2026 每日完整 AlphaCore panel（仅有单日 smoke panel）。

**推荐实验规格**：
1. 保存每日 panel（含 symbol, session_date, sic2_sector, beta, 5 个 score, composite_score, prices）
2. 真实组合：当前权重 0.25/0.10/0.30/0.20/0.15
3. 随机对照：N 个 seed × (随机五因子 或 同五因子随机正权重)
4. 同一套 `DecisionEngine + LotManager + 成本模型` replay
5. 比较真实组合在随机分布中的分位（Sharpe / MaxDD / Turnover / Cost bps/day）

---
证据文件：
- 信号层：`Phase3 baseline signals leaderboard/daily diagnostics validation.csv`
- 组合层：`Phase7K strict_daily_metrics.csv`, `lot_summary.csv`
- 当前主回测：`artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/`
