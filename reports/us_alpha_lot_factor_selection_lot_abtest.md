# US Alpha-Lot Portfolio Engine：因子筛选与 Lot 仓位管理 A/B 说明

生成日期：2026-06-09  
项目名称：US Alpha-Lot Portfolio Engine  
适用市场：美股日频多空组合  
文档目的：固化 alpha 因子筛选规则，并整理当前已有的“选定因子 vs 随机控制”与“lot 级仓位管理 vs 无强制持有”的 A/B 证据。

## 0. 先说结论

本项目不应该把“严格数学正交”作为 alpha 因子入选的硬条件。更适合生产环境的要求是：

```text
低共线 + 经济逻辑互补 + 可归因 + 可赋予持仓周期 + 组合层净收益改善
```

也就是说，我们不需要把五个因子强行变成一组抽象的正交基；我们需要的是五个“买入/卖出理由”足够不同，并且每个理由都能在 lot 账本里单独记录、锁仓、释放和归因。

当前已有证据支持三件事：

1. 信号层面：真实价格信号明显强于 `random_control`。例如在 `adv10m_clean_core_beta_full` 中，`reversal_5d` 的平均 rank IC 为 `0.0180`，随机控制约为 `-0.00005`；top-bottom spread 约 `21.28 bps/5d`，随机控制约 `0.84 bps/5d`。同日 paired A/B 的 rank IC 差值 t-stat 约 `5.47`。

2. 最终五因子样本层面：在 `phase7k_feature_target_panel_sample.csv` 的 2016 年初 81 个交易日样本里，当前五因子合成分数的平均 rank IC 为 `0.0243`，相对 200 个随机噪声因子处在约 `95%` 分位；但相对“同五因子随机正权重”只处在约 `75%` 分位，所以这个结果只能说明五因子成员不随机，不能证明当前权重已经最优。

3. 组合层面：完整 shared-core 因子组在同一 lot/turnover 框架下优于削弱后的消融版本。长区间 validation-only 口径下，shared-core net Sharpe 为 `0.559`，去掉 momentum 后为 `0.531`，只保留慢因子后为 `0.238`。

4. 仓位管理层面：lot 级最小持有期和硬换手预算不是装饰模块。早期 Phase7K 报告中，未强制持有路径的日均换手约 `0.658`，引入 factor-reason lot 后 shared-core 日均换手降到 `0.153`，净 Sharpe 从约 `0.55` 提升到 `1.97`。

但也必须诚实说明：当前还没有完成“最终生产 AlphaCore 五因子组合 vs 随机抽取五因子组合”的完整组合级 replay。原因是 `F:/量化/Final` 目前没有保存 2017-2026 每日完整 AlphaCore panel，只发现了单日 smoke panel。本文把已经能被现有文件证明的 A/B 证据整理出来，并给出生产级随机因子 A/B 的下一步实验规格。

## 1. 当前生产因子定义

当前 `AlphaCore` 使用五个因子：

| 因子 | 原始定义 | 方向 | 经济含义 | 当前权重 |
|---|---|---:|---|---:|
| `reversal_score` | `-return_5d` | 越高越好 | 短期均值回复 | 0.25 |
| `momentum_score` | `momentum_l120_s20` | 越高越好 | 中期趋势延续，跳过最近 20 日以降低与短反转冲突 | 0.10 |
| `small_size_score` | `-market_cap_log` | 越高越好 | 小市值/规模溢价 | 0.30 |
| `low_beta_score` | `-beta` | 越高越好 | 低 beta、防御性和市场暴露控制 | 0.20 |
| `cash_quality_score` | `cash_to_assets` | 越高越好 | 现金质量和资产稳健性 | 0.15 |

对应代码位置：

```text
F:/量化/Final/src/alpha_core.py
F:/量化/Final/src/decision_engine.py
F:/量化/Final/src/lot_manager.py
```

## 2. 因子筛选规则固化

建议以后所有新因子都按以下规则进入候选池。

### 2.1 必须满足的硬条件

第一，不能有未来数据泄露。

若在 `t-1 close` 后生成决策、在 `t open` 执行，则因子只能使用 `t-1 close` 之前已经可获得的数据：

```math
x_{i,k,t}^{available} \subseteq \mathcal F_{t-1,close}
```

第二，必须有清楚的经济解释。

一个因子必须能被翻译成“为什么这只股票应该多/空”的人话理由。比如短反转、小市值、低 beta、现金质量都可以解释；纯粹黑箱残差维度不适合作为 lot reason。

第三，必须可以行业标准化。

当前流程先按 `SIC2` 行业内标准化，行业样本不足时回退到当日全市场标准化：

```math
z_{i,k,t}
= \frac{x_{i,k,t}-\mu_{g(i,t),k,t}}{\sigma_{g(i,t),k,t}}
```

其中 `g(i,t)` 是股票所属行业。代码中 z-score 会 clip 到 `[-3,3]`，避免极端值支配组合。

第四，必须不和已有因子高度重复。

生产上不要求严格正交，但建议设置相关性门槛：

```math
|\rho(z_a,z_b)| \le \rho_{max}
```

建议初始阈值使用：

```text
rho_max = 0.60 或 0.70
```

若新因子与已有因子高度相关，则应该优先保留：

```text
经济含义更清楚、数据更稳定、信号层 IC 更强、组合层净收益改善更明显、换手成本更低
```

第五，必须通过随机控制检验。

至少要求信号层指标明显好于 `random_control`：

```math
IC_{factor} > IC_{random},
\quad
Spread_{factor} > Spread_{random},
\quad
HitRate_{factor} > HitRate_{random}
```

第六，必须通过组合层增量检验。

一个因子不应该只看单因子 IC，而应该看加入组合以后是否改善：

```text
net Sharpe、max drawdown、turnover、cost bps/day、beta drift、sector exposure
```

第七，必须能赋予合理的 lot 持仓周期。

如果一个因子无法回答“这个理由一般应该持有几天”，它就不适合作为 lot 级仓位管理的独立因子。

## 3. 为什么不追求严格正交

严格正交通常意味着：

```math
\langle z_a,z_b\rangle_t = 0, \quad a \ne b
```

或者把每个因子对前面的因子做残差化：

```math
\tilde z_k
= z_k - Z_{<k}(Z_{<k}^{\top}Z_{<k})^{-1}Z_{<k}^{\top}z_k
```

这在论文里很干净，但对实盘有几个问题。

第一，经济含义会漂移。残差化之后的 momentum 可能已经不是“动量”，而是“剔除了 size、beta、reversal 后剩下的一块统计残差”。这会让 lot 的 `factor` 字段失去可解释性。

第二，正交化有顺序依赖。先放 size 再放 momentum，和先放 momentum 再放 size，得到的残差因子不同。

第三，历史正交不代表未来正交。市场结构变化后，过去低相关的因子可能重新相关。

第四，lot 管理真正需要的是“理由可分解”，不是“向量空间正交”。一个仓位能说清楚来自短反转、动量还是现金质量，比数学上完全正交更重要。

所以本文建议将“严格正交”改写成：

```text
经济上互补、统计上低共线、组合上可归因
```

## 4. 因子合成公式

对每个因子先做行业内 z-score：

```math
z_{i,k,t}
= \operatorname{clip}\left(
\frac{x_{i,k,t}-\mu_{g(i,t),k,t}}{\sigma_{g(i,t),k,t}}, -3, 3
\right)
```

五个因子的加权合成分数为：

```math
s^{raw}_{i,t}
= \frac{
0.25z^{rev}_{i,t}
+0.10z^{mom}_{i,t}
+0.30z^{size}_{i,t}
+0.20z^{beta}_{i,t}
+0.15z^{cash}_{i,t}
}{0.25+0.10+0.30+0.20+0.15}
```

然后对当日横截面再次标准化：

```math
s_{i,t}=zscore_t(s^{raw}_{i,t})
```

`DecisionEngine` 使用这个 `composite_score` 选择多头候选和空头候选，并进入线性规划求目标权重。

## 5. A/B 一：真实信号 vs 随机控制

这部分来自 Phase3 的信号层验证。它不是最终组合回测，而是回答一个更基础的问题：真实信号是不是明显好于随机噪音。

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase3_baseline_signals_20260418/phase3_baseline_signal_leaderboard_validation.csv
```

检验目标：`forward_beta_residual_return_5d`。  
验证区间：`2017-01-03` 至 `2026-05-08`。  
控制信号：`random_control`。

| 股票池变体 | 真实信号 | Rank IC | 随机 Rank IC | Top-Bottom spread | 随机 spread | Hit Rate | 随机 Hit Rate |
|---|---|---:|---:|---:|---:|---:|---:|
| `adv10m_clean_core_beta_full` | `reversal_5d` | 0.0180 | -0.00005 | 21.28 bps | 0.84 bps | 53.23% | 50.51% |
| `adv20m_clean_core_beta_full` | `reversal_5d` | 0.0185 | -0.00015 | 21.01 bps | 0.45 bps | 52.43% | 50.38% |
| `top1000_clean_core_beta_full` | `reversal_5d` | 0.0173 | -0.00027 | 20.41 bps | 0.79 bps | 52.43% | 50.68% |

进一步做同日配对 A/B。方法是：在同一个 `session_date`、同一个股票池变体内，将 `reversal_5d` 和 `random_control` 的日度指标配对，计算真实信号减随机信号的均值差和 paired t-stat。

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase3_baseline_signals_20260418/phase3_baseline_signal_daily_diagnostics_validation.csv
```

输出文件：

```text
F:/量化/Final/artifacts/abtest_factor_lot_summary/factor_signal_random_paired_ab.csv
```

| 股票池变体 | 指标 | 配对交易日 | 真实均值 | 随机均值 | 差值 | t-stat | 真实优于随机比例 |
|---|---|---:|---:|---:|---:|---:|---:|
| `adv10m_clean_core_beta_full` | Rank IC | 2350 | 0.0180 | -0.00005 | 0.0181 | 5.47 | 53.32% |
| `adv10m_clean_core_beta_full` | Top-Bottom Spread | 2350 | 21.28 bps | 0.84 bps | 20.44 bps | 3.85 | 51.83% |
| `adv20m_clean_core_beta_full` | Rank IC | 2350 | 0.0185 | -0.00015 | 0.0186 | 5.55 | 53.11% |
| `adv20m_clean_core_beta_full` | Top-Bottom Spread | 2350 | 21.01 bps | 0.45 bps | 20.56 bps | 3.82 | 51.36% |
| `top1000_clean_core_beta_full` | Rank IC | 2350 | 0.0173 | -0.00027 | 0.0176 | 5.38 | 53.32% |
| `top1000_clean_core_beta_full` | Top-Bottom Spread | 2350 | 20.41 bps | 0.79 bps | 19.63 bps | 3.70 | 51.62% |

这里的“真实优于随机比例”不高得夸张，这是正常的：日频横截面 alpha 本来就是低信噪比系统。关键不是每天都赢随机，而是长期均值差稳定为正，且 paired t-stat 显著为正。

随机控制与真实信号的 Spearman 相关也接近 0。例如在 `adv10m_clean_core_beta_full` 中：

| 左信号 | 右信号 | Spearman corr |
|---|---|---:|
| `random_control` | `reversal_5d` | -0.000020 |
| `random_control` | `momentum_20d` | -0.000355 |
| `random_control` | `momentum_60d` | -0.000103 |
| `random_control` | `transparent_composite` | 0.000286 |

这一层能说明：真实候选信号不是随机排序，至少在信号诊断层面明显强于随机控制。但它仍然是信号层 A/B，不等价于完整组合级随机五因子 replay。

### 5.1 最终五因子样本随机对照

为了更贴近“当前五因子本身 vs 随机因子”的问题，我又用 Phase7K 保存的 sample panel 做了一个数据受限 sanity check。

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_tb0p15/phase7k_feature_target_panel_sample.csv
```

输出文件：

```text
F:/量化/Final/artifacts/abtest_factor_lot_summary/final_five_sample_random_ab_summary.csv
```

样本范围非常有限：`5000` 行、`81` 个交易日，日期为 `2016-01-04` 至 `2016-04-28`，平均每天约 `61.7` 只股票。因此这只能作为最终五因子的 sanity check，不能替代 2017-2026 的生产级 replay。

| 真实组合 | 对照组 | 样本日期 | 平均 Rank IC | 平均 Spread | Rank IC 相对分位 | Spread 相对分位 | 解读 |
|---|---|---:|---:|---:|---:|---:|---|
| 当前五因子固定权重 | 200 个随机噪声因子 | 81 | 0.0243 | 21.04 bps | 95% | 91% | 明显强于纯随机噪声 |
| 当前五因子固定权重 | 200 组同五因子随机正权重 | 81 | 0.0243 | 21.04 bps | 75% | 53% | 因子成员有效，但权重最优性不能靠这个样本证明 |

这个补充实验的意义是：当前五因子合成不是随机噪声；但它也提醒我们，当前 `0.25/0.10/0.30/0.20/0.15` 权重不应被神化。严格生产结论仍然要依赖全历史 AlphaCore panel 的组合级 replay。

## 6. A/B 二：完整因子组 vs 因子消融

这部分来自 Phase7K 的组合层消融。它比较的是同一个 lot/turnover 框架下，不同因子组合的表现。

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_tb0p15/phase7k_strict_daily_metrics.csv
```

口径说明：validation-only，非最终生产实盘口径；用于判断因子组结构，不用于宣传最终收益。

| 组合 | 因子组 | Final Equity | Ann. Return | Ann. Vol | Net Sharpe | Max DD | Mean Turnover | Cost bps/day |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `shared_core` | 反转、动量、小市值、低 beta、现金质量 | 1.679 | 5.28% | 9.45% | 0.559 | -16.71% | 0.051 | 0.205 |
| `shared_no_momentum` | 去掉动量 | 1.617 | 4.94% | 9.30% | 0.531 | -13.51% | 0.045 | 0.180 |
| `shared_slow_core` | 只保留慢因子 | 1.248 | 2.25% | 9.45% | 0.238 | -25.27% | 0.051 | 0.203 |

解释：

1. 完整 shared-core 的净 Sharpe 高于去掉动量版本，说明中期趋势虽然权重低，但对组合有增量贡献。

2. 只保留慢因子的版本显著变弱，说明纯小市值/低 beta/现金质量不足以支撑完整策略，需要短周期价格行为提供更高频的 alpha 更新。

3. 这不是随机五因子组合 A/B，而是因子消融 A/B。它证明的是“当前五因子结构相对削弱版本更完整”，不是“当前五因子一定打败所有随机五因子组合”。

## 7. A/B 三：Lot 管理 vs 无强制持有

lot 级仓位管理的核心不是为了让账本复杂，而是让每笔仓位有“出生时间、来源因子、最短持有期、释放规则”。

一个 lot 定义为：

```math
\ell=(symbol=i, side=s, factor=k, weight=a_{\ell,t}, birth=b_\ell, minhold=h_k)
```

锁仓条件为：

```math
Locked(\ell,t)=\mathbf 1(t-b_\ell<h_k)
```

当前默认最小持有期：

| 因子 | 最小持有期 |
|---|---:|
| `reversal_score` | 5 sessions |
| `momentum_score` | 10 sessions |
| `small_size_score` | 20 sessions |
| `low_beta_score` | 20 sessions |
| `cash_quality_score` | 20 sessions |

新增仓位按因子支持度拆分。多头支持度：

```math
support^+_{i,k,t}=\max(0,\lambda_k z_{i,k,t})
```

空头支持度：

```math
support^-_{i,k,t}=\max(0,-\lambda_k z_{i,k,t})
```

新增 lot 权重：

```math
a^{new}_{i,k,t}
=\Delta w^s_{i,t}
\frac{support^s_{i,k,t}}{\sum_j support^s_{i,j,t}}
```

### 7.1 早期有/无强制持有对照

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/docs/us-equities-pure-alpha-phase7k-locked-turnover-shared-core-report-zh-20260518.md
```

| 方案 | 日均换手 | Net Sharpe | 说明 |
|---|---:|---:|---|
| 无强制持有 Phase7J 严格路径 | 0.658 | 约 0.55 | 每天重优化，容易产生噪声换手 |
| Phase7K shared-core lot 锁仓 | 0.153 | 1.97 | factor-reason min-hold + 硬换手预算 |

这个 A/B 是 lot 管理最重要的证据。它说明收益质量提升不只是因为少交成本，更重要的是减少了每日重优化造成的“信号抖动交易”。

证据边界也要写清楚：当前工作区里没有找到 Phase7J 无强制持有路径的机器可读原始结果文件，因此上表中 `0.658` 和 `0.55` 引用的是早期中文报告中的结论数字；Phase7K lot 锁仓侧则同时有报告和 `phase7k_lot_summary.csv`、`phase7k_strict_daily_metrics.csv` 等机器可读文件支持。后续如果要把 lot A/B 做成完全可复现包，应补跑一个 `min_hold=0` 或 `disable_lot_lock=true` 的 replay，并保存与 Phase7K 同格式的 curve、metrics、turnover、lot summary。

### 7.2 长区间 lot 账本诊断

数据来源：

```text
F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_tb0p15/phase7k_lot_summary.csv
```

| 组合 | Mean Locked Weight | Median Locked Weight | Mean Locked Lots | Mean Active Lots | Carry Session Rate | Budget Used Fraction |
|---|---:|---:|---:|---:|---:|---:|
| `shared_core` | 0.327 | 0.280 | 74.18 | 401.62 | 75.70% | 35.02% |
| `shared_no_momentum` | 0.287 | 0.248 | 56.66 | 345.02 | 79.19% | 30.22% |
| `shared_slow_core` | 0.481 | 0.404 | 72.86 | 280.16 | 60.54% | 37.25% |

解释：

1. `shared_core` 平均有约 32.7% 的组合权重处于锁定状态，说明它不是每日完全推倒重来。

2. 平均 active lots 约 401.6，说明真实持仓不是简单的股票列表，而是大量 factor-reason 小仓位的聚合。

3. carry session rate 约 75.7%，说明多数交易日不需要强行重构整个组合，lot 机制确实在降低策略抖动。

## 8. 组合优化与 lot 之间的关系

`DecisionEngine` 可以简化理解为一个线性规划：

```math
\min_w
-\eta\alpha^\top w
+\lambda_{turn}\|w-w_{t-1}\|_1
+\lambda_{sector}\|B_{sector}w\|_1
```

主要约束包括：

```math
\sum_i w^+_{i,t}=1,
\quad
\sum_i w^-_{i,t}=1
```

```math
\sum_i \beta_i w^+_{i,t}-\sum_i \beta_i w^-_{i,t}=0
```

```math
\|w_t-w_{t-1}\|_1 \le TurnoverBudget
```

以及单票上限、最低持仓数、行业暴露惩罚和 locked lot 下界。

lot 管理给 LP 优化器提供了“哪些仓位不能随便动”的下界：

```math
w_{i,t}^{side} \ge \sum_{\ell\in Locked(i,side,t)} a_{\ell,t}
```

所以 lot 管理不是 LP 之外的账本附属品，而是会直接改变优化可行域。

## 9. 当前生产式 open-to-open 回测基准

为了和最新工程版本对齐，这里也列出当前更接近实盘执行逻辑的长区间回测。

数据来源：

```text
F:/量化/Final/artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/backtest_summary.json
```

区间：预热 252 个交易日后，报告期 `2017-01-03` 至 `2026-05-19`。  
路径：`t-1 close` 后生成决策，`t open` 执行，收益按 open-to-open 记账。  
成本：执行成本 `8 bps`，另计 SEC fee 和 TAF。

| 指标 | 策略 | SPY | QQQ |
|---|---:|---:|---:|
| Final Equity | 6.796 | 3.760 | 6.295 |
| Ann. Return | 22.74% | 15.21% | 21.74% |
| Ann. Vol | 15.97% | 17.56% | 22.23% |
| Sharpe | 1.36 | 0.90 | 1.00 |
| Max Drawdown | -26.53% | -32.05% | -36.58% |
| Avg Turnover | 6.03% | - | - |

这组是当前更接近实盘执行的主口径；前面的 Phase3/Phase7K A/B 则是用于解释因子选择与 lot 机制的研究证据。

## 10. 尚未完成但应该补的生产级随机因子 A/B

如果要严格回答“当前筛选出来的五个因子是否优于随机选出的五个因子组合”，建议新增一个 replay 实验，而不是只看信号层随机控制。

推荐实验规格：

1. 固定股票池、日期、价格源、SEC 缓存、成本模型和执行路径。

2. 保存每个交易日完整 AlphaCore panel，至少包括：

```text
symbol, session_date, sic2_sector, beta,
reversal_score, momentum_score, small_size_score, low_beta_score, cash_quality_score,
composite_score, open/close reference prices
```

3. 设定真实组合：

```text
0.25 reversal + 0.10 momentum + 0.30 small_size + 0.20 low_beta + 0.15 cash_quality
```

4. 生成随机对照组合。例如对每个 seed 随机生成五个噪声因子或随机抽取候选因子，并保持同样标准化、同样 LP、同样 lot、同样交易成本。

5. 对每个 seed 跑完整 `DecisionEngine + LotManager` replay。

6. 比较真实组合在随机分布中的位置：

```text
Sharpe percentile
Max DD percentile
Annual return percentile
Turnover percentile
Cost bps/day percentile
Realized beta drift
Sector exposure drift
```

生产级结论应该写成：

```text
真实因子组合超过随机组合中位数 / 75分位 / 95分位
```

而不是简单说“真实因子比随机好”。

## 11. 最终写法建议

在正式策略文档中，建议这样描述：

> 本策略不追求严格数学正交，而追求经济逻辑互补、统计低共线和 lot 层面的可归因。五个 alpha 因子分别覆盖短期反转、中期趋势、规模、低 beta 与现金质量。每个因子经过行业内标准化和横截面标准化后进入共享资金 LP 优化器；新增仓位按因子支持度拆成 factor-reason lots，并根据不同因子周期设置最小持有期。已有研究证据显示，真实信号在 rank IC 和 top-bottom spread 上明显强于随机控制；完整 shared-core 因子组优于削弱后的因子消融版本；lot 级锁仓和换手预算显著降低无意义换手并改善收益质量。严格的生产级随机五因子组合 A/B 需要在保存全历史 AlphaCore panel 后继续 replay 验证。

## 12. 证据文件

| 内容 | 路径 |
|---|---|
| 当前 AlphaCore 因子定义 | `F:/量化/Final/src/alpha_core.py` |
| 决策 LP 与换手预算 | `F:/量化/Final/src/decision_engine.py` |
| Lot 账本与 factor-reason min-hold | `F:/量化/Final/src/lot_manager.py` |
| 信号层随机控制 leaderboard | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase3_baseline_signals_20260418/phase3_baseline_signal_leaderboard_validation.csv` |
| 信号层 paired A/B 输出 | `F:/量化/Final/artifacts/abtest_factor_lot_summary/factor_signal_random_paired_ab.csv` |
| 最终五因子样本随机对照 | `F:/量化/Final/artifacts/abtest_factor_lot_summary/final_five_sample_random_ab_summary.csv` |
| 信号相关性检查 | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase3_baseline_signals_20260418/phase3_signal_correlation_validation.csv` |
| 因子消融 metrics | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_tb0p15/phase7k_strict_daily_metrics.csv` |
| Lot summary | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_tb0p15/phase7k_lot_summary.csv` |
| 早期有/无强制持有对照报告 | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/docs/us-equities-pure-alpha-phase7k-locked-turnover-shared-core-report-zh-20260518.md` |
| 当前 open-to-open 主回测 | `F:/量化/Final/artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/backtest_summary.json` |
| 本次 A/B 摘要目录 | `F:/量化/Final/artifacts/abtest_factor_lot_summary` |
