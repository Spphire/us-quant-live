# Phase7K 美股多空策略技术摘要

**策略类型**：横截面多因子市场中性多空，日频调仓，locked-lot 换手控制  
**数据源**：Alpaca 日线 (IEX, 全复权) + SEC XBRL 基本面  
**主回测区间**：2017-01-03 至 2026-05-19 (252 sessions warmup)  
**主结果 (10k 初始)**：年化 22.74%，波动 15.97%，Sharpe 1.36，最大回撤 -26.53%

## 核心设计

### 动态股票池
固定候选 1000 只 (`top1000_fixed_symbols_20260417.txt`)，按滞后流动性筛选：
- 历史 ≥252 bar，近 20 日观测 ≥15，价格 ≥$10，中位成交额 >0
- 按 20 日中位美元成交额降序取前 1000
- 防前视：只用 `session_date < target_date` 的数据

### 5 因子定义
| 因子 | 定义 | 当前权重 |
|---|---|---:|
| reversal | `-return_5d` | 0.25 |
| momentum | `(close_{t-20} / close_{t-140}) - 1` | 0.10 |
| small_size | `-log(market_cap)` | 0.30 |
| low_beta | `-beta` (252d 滚动, 收缩至 1.0, clip [0,3]) | 0.20 |
| cash_quality | `cash / assets` | 0.15 |

行业内 z-score (SIC2) → 加权合成 → 当日横截面再 z-score。

### 组合优化 (DecisionEngine LP)
```
min_w  -η·score + λ_τ·turnover + λ_sector·sector_exposure

s.t.
  Σw_L=1, Σw_S=1  (多空各 100%, gross 200%)
  Σβ_L·w_L - Σβ_S·w_S = 0  (beta 中性, 放松网格 0.05/0.10/0.15/0.20)
  turnover ≤ 0.15 + deploy_gap
  0 ≤ w ≤ 1/30 (单名上限 3.33%)
  w ≥ locked_weights (lot 下界)
```
当前 η=0.01, λ_τ=0.005, λ_sector=25.0。

### Locked-lot 持仓管理
每个仓位拆成多个 `(symbol, factor, weight, birth_idx, min_hold)` lot。
```
min_hold: reversal=5, momentum=10, size/beta/cash=20 sessions
locked(lot, t) = (t - birth_idx < min_hold)
```
新增权重按因子支持度拆分：
```
support_L = max(0, λ_k · z_k)  (多头)
support_S = max(0, -λ_k · z_k) (空头)
```

### 成本模型 (回测)
- 执行滑点：8 bps × trade notional
- SEC fee: 0.0000278 × sell notional
- TAF: min(0.000195 × shares, 9.79) per sell trade
- **未计借券费/融资成本** (P0 级高估风险)

## 主回测结果 (open-to-open, 多资金档)
| 初始权益 | 期末净值 | 年化收益 | 最大回撤 | 波动 | Sharpe |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 6.80 | 22.74% | -26.53% | 15.97% | 1.36 |
| 50,000 | 7.43 | 23.92% | -23.98% | 15.71% | 1.44 |
| 100,000 | 7.34 | 23.75% | -24.15% | 15.71% | 1.43 |
| 300,000 | 7.22 | 23.53% | -24.20% | 15.72% | 1.42 |

- 平均日换手 6.03%
- 平均动态池 815 只
- Gross exposure 2.0, Net ~0

## Lot 机制的价值
早期无强制持有 (Phase7J)：日均换手 0.658, Sharpe ~0.55  
Phase7K (locked-lot + turnover budget)：日均换手 0.153, Sharpe 1.97 (validation 口径)

## 已知限制与优先修复项
1. **P0-1 完全忽略做空借券费/隔夜融资成本** → 系统性高估净收益 (数百 bps/年)
2. **P0-2 开盘价完美成交假设** → 回测滑点/买入力模型弱于 live adverse offset + 缩量
3. **P0-3 幸存者偏差** → 候选池用"当前 active 资产"重建历史，漏掉退市标的
4. P1-1 回测与 live 下单逻辑各有一份实现 → 易漂移，应合并为共享模块

## 文件位置
- 代码：`src/alpha_core.py`, `decision_engine.py`, `lot_manager.py`
- 主回测：`artifacts/phase7k_backtest/multi_cap_open_open_20160101_20260520_rerun/`
- 早期验证报告引用区间：2014-11-13 至 2019-12-31 (当前主口径区间更长更新)

---
*策略核心不是单纯 top/bottom 选股，而是在交易成本、持仓延续、风险中性约束下的可成交 alpha 最大化。*
