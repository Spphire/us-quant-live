# US Alpha-Lot 引擎 Q&A 精简版

## Q1. 因子是否严格正交？
**不是严格正交，而是低共线 + 经济互补 + 可归因**。

5 因子：reversal (0.25), momentum (0.10), small_size (0.30), low_beta (0.20), cash_quality (0.15)。

**不追求严格正交的原因**：
1. 真实金融因子天然有结构性相关，强行消除会削弱经济含义
2. 正交化有路径依赖（残差化顺序影响结果）
3. 横截面相关结构不稳定，今天正交不代表未来正交
4. lot 管理需要"可解释的理由"，而非抽象的统计维度

**选择标准**：`|ρ(a,b)| ≤ 0.6~0.7` + 金融含义互补 + 时间尺度互补 + 组合层增量改善。

## Q2. Lot 级仓位管理的核心思路？
**每个持仓拆成多个 factor-reason lots**，记录 `(symbol, factor, weight, birth_idx, min_hold)`。

**min_hold 周期**：reversal=5, momentum=10, size/beta/cash=20 sessions。

**锁定判定**：`session_idx - birth_idx < min_hold` → 优化器下界约束。

**新增权重拆分**：
```
support_L = max(0, λ_k · z_k)  (多头)
support_S = max(0, -λ_k · z_k) (空头)
新增 lot 权重 = Δw · support / Σsupport
```

**解决三个问题**：
1. 持仓有记忆（出生+到期时间）
2. 换手有纪律（快/慢因子分层释放）
3. 共享资金高效（同股多理由并存）

## Q3. 业绩、换手、成本？
**当前 open-to-open 长区间回测** (2017-01-03 ~ 2026-05-19):
| 指标 | 10k 策略 | SPY | QQQ |
|---|---:|---:|---:|
| 年化收益 | 22.74% | 15.21% | 21.74% |
| 波动 | 15.97% | 17.56% | 22.23% |
| Sharpe | 1.36 | 0.90 | 1.00 |
| 最大回撤 | -26.53% | -32.05% | -36.58% |
| 日均换手 | 6.03% | - | - |

**Lot 机制价值**：
- 无强制持有 (Phase7J)：日均换手 0.658, Sharpe ~0.55
- Phase7K (locked-lot)：日均换手 0.153, Sharpe 1.97

**成本假设**：执行 8 bps + SEC fee + TAF。

## Q4. 因子/Lot A/B 证据在哪？
见独立报告：`reports/us_alpha_lot_factor_selection_lot_abtest.md`

三层证据：
1. 信号层：真实信号 vs random_control (rank IC 差值 t-stat ~5.5)
2. 组合层：shared-core vs 去动量/慢因子消融 (净 Sharpe 0.559 vs 0.531/0.238)
3. 仓位层：lot 锁仓和硬换手预算 vs 无强制持有 (换手降 4 倍，Sharpe 提升 3.6 倍)

**尚缺**：最终生产 AlphaCore 五因子 vs 随机五因子组合的完整 replay（需先保存全历史每日 panel）。

---
代码位置：`src/alpha_core.py`, `decision_engine.py`, `lot_manager.py`  
主回测：`artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/`
