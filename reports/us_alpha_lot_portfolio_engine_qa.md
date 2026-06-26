# US Alpha-Lot Portfolio Engine Q&A

生成日期：2026-06-09  
项目定位：美股多因子 alpha + lot 级仓位管理 + broker 执行约束的一体化组合引擎。

## Q1. Alpha 因子是如何挑选的？是不是挑选了相互正交的因子，依据是什么？

这个项目里的 alpha 不是从大量因子里做黑箱筛选，而是先根据金融含义选出五类互补的、解释上尽量不重叠的因子，再通过行业内标准化、共享资金组合优化和 lot 级持仓管理来让这些因子共同表达。

当前生产版本使用五个因子：

| 因子 | 原始定义 | 方向 | 直觉 |
|---|---|---:|---|
| `reversal_score` | `-return_5d` | 越高越好 | 短期跌幅较大的股票存在均值回复机会 |
| `momentum_score` | `momentum_l120_s20` | 越高越好 | 中期趋势延续，且跳过最近 20 日避免和短反转冲突 |
| `small_size_score` | `-log(market_cap)` | 越高越好 | 小市值/规模因子，捕捉 size premium 与弹性 |
| `low_beta_score` | `-beta` | 越高越好 | 偏低 beta，用来降低组合市场暴露和波动 |
| `cash_quality_score` | `cash_to_assets` | 越高越好 | 现金/资产较高，代表更稳健的财务质量特征 |

更严格地说，这些因子不是数学意义上的严格正交。严格正交要求在样本横截面上满足

```math
\langle z_a, z_b \rangle_t = \sum_{i \in \Omega_t} z_{i,a,t} z_{i,b,t} = 0,
\quad \forall a \ne b,
```

或者相关系数矩阵非对角项全部为 0。当前代码没有对因子做 Gram-Schmidt 之类的正交化处理，所以不能说它们“严格正交”。更准确的表述是：这些因子在设计上追求低共线、低逻辑重叠和风险来源互补。

是否应该强制选择严格正交因子？结论是：不建议把“严格正交”作为生产因子的硬要求，但应该把“低共线、可归因、可监控”作为因子入选标准。

严格正交的好处是很直观的：如果两个因子互不相关，那么仓位归因会更干净，lot 账本里“这笔仓位为什么存在”也更容易解释。但严格正交也有几个实际问题：

1. 真实金融因子天然会重叠。小市值、低 beta、现金质量、动量之间可能存在结构性相关，强行消除相关性可能会把有经济含义的 alpha 也一起剔掉。

2. 正交化有路径依赖。若使用残差化：

```math
\tilde z_k = z_k - Z_{<k}(Z_{<k}^{\top}Z_{<k})^{-1}Z_{<k}^{\top}z_k,
```

则先放入哪个因子会影响后续因子的定义。这样得到的 `\tilde z_k` 在统计上更干净，但金融解释会变弱。

3. 横截面相关结构不稳定。今天近似正交的因子，换一个市场阶段、行业结构或股票池后可能重新相关。如果每期都动态正交化，因子的含义会漂移；如果用全样本正交化，又容易引入未来信息。

4. lot 级仓位管理需要“理由可解释”。一个 lot 记录的是 `symbol, factor, weight, birth_idx, min_hold`。如果因子经过复杂旋转或残差化，`factor` 字段会从“短反转/动量/质量”变成“某个统计残差维度”，这不利于解释和实盘运维。

所以更适合本项目的选择标准是：

```math
|\rho_{a,b}| \le \rho_{\max}, \quad a \ne b,
```

同时要求每个因子有独立金融含义、不同持有周期、稳定的正向暴露，以及加入组合后能改善净收益/回撤/换手后的表现。换句话说，我们追求的是“经济上互补 + 统计上不过度共线”，而不是为了形式上正交而牺牲可解释性。

依据主要有三层：

1. 金融含义互补。短反转和中期动量刻画不同时间尺度；小市值是横截面规模暴露；低 beta 是风险暴露控制；现金质量来自基本面资产结构。它们不是同一个价格动量因子的不同参数版本。

2. 时间尺度互补。`reversal_score` 使用近 5 日反转，`momentum_score` 使用 120 日动量并跳过最近 20 日，即

```math
x^{rev}_{i,t} = -R_{i,t-5:t},
\qquad
x^{mom}_{i,t} = R_{i,t-120:t-20}.
```

这样做的目的就是降低短期反转和中期趋势在同一窗口内互相抵消或重复表达。

3. 实证暴露验证。历史 Phase7K 报告中的 `phase7k_factor_exposure_summary.csv` 显示，shared-core 组合长期保持了对五个目标因子的正暴露。以 top1000 full、`tb0p15` 版本为例，`shared_core_lambda_0p005_tb0p15` 的平均暴露大致为：

| 因子 | 平均暴露 | 正暴露占比 |
|---|---:|---:|
| `reversal_score` | 0.706 | 94.61% |
| `momentum_score` | 0.651 | 98.98% |
| `small_size_score` | 3.348 | 99.75% |
| `low_beta_score` | 0.024 | 75.08% |
| `cash_quality_score` | 1.597 | 100.00% |

其中 `low_beta_score` 暴露较小，是因为组合优化里有 beta 中性约束，会压缩低 beta 因子的独立表达空间。

因子标准化流程如下。对任一原始因子 `x_{i,k,t}`，先在同一交易日、同一 SIC2 行业内做 z-score：

```math
z_{i,k,t}
= \frac{x_{i,k,t} - \mu_{g(i,t),k,t}}{\sigma_{g(i,t),k,t}},
```

其中 `g(i,t)` 是股票 `i` 在 `t` 日的 SIC2 行业。若行业内样本不足，则退回当日全市场 z-score。然后做加权合成：

```math
s^{raw}_{i,t}
= \frac{0.25z^{rev}_{i,t} + 0.10z^{mom}_{i,t} + 0.30z^{size}_{i,t}
+ 0.20z^{beta}_{i,t} + 0.15z^{cash}_{i,t}}{0.25+0.10+0.30+0.20+0.15}.
```

最后再对 `s^{raw}_{i,t}` 做当日横截面 z-score，得到最终 `composite_score`。

## Q2. Lot 级仓位管理的核心思路是什么？

这个项目的仓位管理不是简单地每天根据最新 alpha 重新全量调仓。它把组合拆成两层：

1. 组合层：最终仍然是一套共享资金的 long-short book。
2. 理由层：每个持仓会被拆成若干 factor-reason lots，记录这笔仓位是由哪些因子支持的、每个理由占多少权重、什么时候出生、最少要持有多久。

形式化地，设多头目标权重为 `w^+_{i,t}`，空头目标权重为 `w^-_{i,t}`。组合层满足：

```math
\sum_i w^+_{i,t} = 1,\qquad
\sum_i w^-_{i,t} = 1,\qquad
w^+_{i,t}, w^-_{i,t} \ge 0.
```

每一边的实际持仓由 lot 账本聚合得到。对 side `s in {long, short}`：

```math
w^s_{i,t} = \sum_{\ell \in \mathcal L^s_t: symbol(\ell)=i} a_{\ell,t},
```

其中一个 lot 可以写成：

```math
\ell = (symbol=i,\ factor=k,\ weight=a_{\ell,t},\ birth=b_\ell,\ minhold=h_k).
```

不同因子的最小持有期不同：

| factor | min hold |
|---|---:|
| `reversal_score` | 5 sessions |
| `momentum_score` | 10 sessions |
| `small_size_score` | 20 sessions |
| `low_beta_score` | 20 sessions |
| `cash_quality_score` | 20 sessions |

一个 lot 是否锁仓由下面的条件决定：

```math
Locked(\ell,t)=1(t-b_\ell < h_k).
```

这意味着：如果一只股票今天不再是最优目标，但它的某些 factor-reason lots 还没到最小持有期，系统不会随意把这部分仓位卖掉。它会优先保留锁仓 lots，只在未锁部分和新增部分上做优化。

当某只股票需要新开仓或加仓时，新增权重不会只记成“买了这只股票”。系统会根据因子支持度把新增权重分配到不同理由上。对多头：

```math
support^+_{i,k,t}=\max(0,\lambda_k z_{i,k,t}),
```

对空头：

```math
support^-_{i,k,t}=\max(0,-\lambda_k z_{i,k,t}),
```

其中 `\lambda_k` 是因子权重，`z_{i,k,t}` 是行业标准化后的因子分数。新增仓位 `\Delta w^s_{i,t}` 会按支持度归一化拆成 lots：

```math
a^{new}_{i,k,t}
= \Delta w^s_{i,t}
\frac{support^s_{i,k,t}}{\sum_j support^s_{i,j,t}}.
```

这就是 lot 级仓位管理和因子选择之间的关系：因子不一定要严格正交，但必须能清楚地解释“为什么这笔仓位应该存在”。如果一个股票同时有短反转、小市值和现金质量支持，它可以在同一笔实际仓位里拥有多个 lots；未来短反转理由过期后，可以释放这部分仓位，但小市值或现金质量理由仍可能继续锁定。

组合优化层再在这些锁仓约束之上求目标权重。简化写法是：

```math
\min_w
-\alpha^\top w
+ \lambda_{turn}\|w-w_{t-1}\|_1
+ \lambda_{sector}\|B_{sector}w\|_1
```

约束包括：

```math
\sum_i w^+_{i,t}=1,\quad
\sum_i w^-_{i,t}=1,
```

```math
\sum_i \beta_i w^+_{i,t} - \sum_i \beta_i w^-_{i,t}=0,
```

```math
\|w_t-w_{t-1}\|_1 \le TurnoverBudget,
```

以及单票权重上限和锁仓 lot 的下界约束。实际实现用线性规划求解；这个结构使策略不会因为每天 alpha 有一点波动就全量换仓。

直观地说，lot 管理解决了三个问题：

1. 它让持仓有记忆。仓位不是每天“重新投票”，而是有出生时间和到期时间。

2. 它让换手有纪律。短周期因子可以更快释放，慢周期因子不会被短期噪声反复打断。

3. 它让共享资金更高效。同一只股票可以同时承载多个因子理由，不需要把资金机械切成五个互相隔离的 sleeve。

这也解释了为什么“严格正交”不是必要条件。lot 系统真正需要的是可归因的因子理由，而不是完全无相关的统计维度。严格正交可以作为研究检查项，但生产上更重要的是因子含义清楚、相关性不过高、暴露稳定、换手成本可控。

## Q3. 夏普比、最大回撤、换手率、成本 bps 是多少？

这里建议区分两套口径：一套是当前更接近实盘执行逻辑的 open-to-open 长区间回测；另一套是早期 Phase7K validation-only 研究口径。

### 当前 open-to-open 长区间回测

来源：`F:/量化/Final/artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/backtest_summary.json`。  
区间：预热 252 个交易日后，从 `2017-01-03` 到 `2026-05-19`。  
路径：`t-1 close` 后生成决策，`t open` 执行，收益按 open-to-open 记账。  
成本模型：执行成本 `8 bps`，SEC fee `0.0000278`，TAF `0.000195/share`，TAF cap `9.79/trade`。

| 指标 | 策略 | SPY | QQQ |
|---|---:|---:|---:|
| 终值净值 | 6.796 | 3.760 | 6.295 |
| 总收益 | 579.63% | 276.01% | 529.50% |
| 年化收益 | 22.74% | 15.21% | 21.74% |
| 年化波动 | 15.97% | 17.56% | 22.23% |
| Sharpe, rf=0 | 1.36 | 0.90 | 1.00 |
| 最大回撤 | -26.53% | -32.05% | -36.58% |

交易侧指标：

| 指标 | 数值 |
|---|---:|
| 平均日换手 | 6.03% |
| 平均动态股票池规模 | 814.62 |
| 执行成本假设 | 8 bps |
| 总成本 | 7,662.17 |

换手率在回测中定义为：

```math
Turnover_t = \frac{1}{2}\frac{\sum_i |\Delta N_{i,t}|}{E_t},
```

其中 `\Delta N_{i,t}` 是交易名义金额变化，`E_t` 是当日交易前账户权益。乘以 `1/2` 是因为多空组合里买卖两边同时变化时，双边名义金额会把一次组合换仓计两遍。

成本定义为：

```math
Cost_t = \frac{bps_{exec}}{10000}\sum_i |\Delta N_{i,t}| + SEC_t + TAF_t.
```

其中默认 `bps_exec = 8`。卖出相关监管费近似为：

```math
SEC_t = 0.0000278 \times SellNotional_t,
```

TAF 近似为：

```math
TAF_t = \sum_{sell\ trades} \min(0.000195 \times shares, 9.79).
```

### 早期 Phase7K validation-only 研究口径

来源：`docs/us-equities-pure-alpha-phase7k-locked-turnover-shared-core-report-zh-20260518.md`。  
区间：`2014-11-13` 到 `2019-12-31`。  
主策略：`shared_core_lambda_0p005_tb0p15`。  
成本假设：`4 bps/side`。

| 指标 | 数值 |
|---|---:|
| 年化收益 | 21.35% |
| 年化波动 | 10.84% |
| Sharpe | 1.97 |
| 最大回撤 | -10.77% |
| 日均换手 | 0.153 |
| 日均成本 | 0.61 bps/day |

这组结果适合解释“为什么要引入 lot 级持仓管理”。没有强制持有约束的 Phase7J 路径日均换手约 `0.658`，而 shared-core 降到约 `0.153`；净 Sharpe 从约 `0.55` 提升到 `1.97`。所以仓位管理不是附属模块，而是策略收益质量的重要来源。

## Q4. 简短回答版

如果面试或 GitHub README 里只放一段，可以这样写：

> 本策略使用五个互补 alpha：短反转、中期动量、小市值、低 beta、现金质量。它们不是经过数学正交化后的严格正交因子，而是按金融含义、时间尺度和风险来源挑选的低重叠因子组；生产上更重视低共线、可归因和暴露稳定。每个持仓会被拆成 factor-reason lots，不同因子理由有不同最小持有期，因此仓位管理可以保留仍有效的慢周期理由，同时释放已经过期的短周期理由。最新 open-to-open 回测在 2017-01-03 至 2026-05-19 的报告区间内，策略年化收益 22.74%，年化波动 15.97%，Sharpe 1.36，最大回撤 -26.53%，平均日换手 6.03%，默认执行成本为 8 bps，并额外计入 SEC fee 和 TAF。

## Q5. 因子筛选和 Lot 管理 A/B 测试放在哪里？

已整理成独立中文报告：`F:/量化/Final/reports/us_alpha_lot_factor_selection_lot_abtest.md`。

这份报告把三类证据分开：

1. 信号层：真实信号 vs `random_control`。
2. 组合层：完整 shared-core vs 去动量/慢因子消融。
3. 仓位层：lot 锁仓和硬换手预算 vs 无强制持有路径。

报告也明确说明：当前还没有完成最终生产 AlphaCore 的“随机五因子组合级 replay”，因为全历史每日 AlphaCore panel 尚未完整保存；后续若要做严格随机组合 A/B，应先保存每日 panel，再用同一套 `DecisionEngine + LotManager + 成本模型` 重放。
## 证据文件

| 内容 | 路径 |
|---|---|
| Alpha 因子定义 | `F:/量化/Final/src/alpha_core.py` |
| 组合优化与换手预算 | `F:/量化/Final/src/decision_engine.py` |
| 回测成本模型 | `F:/量化/Final/src/backtest/phase7k_backtest.py` |
| 当前长区间回测 summary | `F:/量化/Final/artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/backtest_summary.json` |
| 早期 Phase7K 中文报告 | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/docs/us-equities-pure-alpha-phase7k-locked-turnover-shared-core-report-zh-20260518.md` |
| 因子暴露 summary | `F:/量化/StockMachine-20260321-codex-stable-ops-status-20260322/artifacts/strategy_projects/us_equities_pure_alpha_h5/research/phase7k_locked_turnover_shared_capital_2016_20260518_top1000full_tb0p15/phase7k_factor_exposure_summary.csv` |

