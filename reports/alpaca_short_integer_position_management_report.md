# Alpaca 空头整数股约束管理摘要

**问题**：Alpaca 不支持 fractional short sale，空头必须整数股，导致多空组合净多头偏置。  
**推荐方案**：目标生成阶段对空头权重 floor 到整数股，多头保持连续；订单层再次强制开空/加空整股。  
**回测区间**：2024-05-20 至 2026-05-20 (501 sessions)  

## 核心公式
空头目标向下取整：
```
q̂ᵢᵗˢ = ⌊Eₜ|wᵢᵗ*| / Pᵢᵗ⌋  (空头必须整数股)
ŵᵢᵗˢ = -q̂ᵢᵗˢ·Pᵢᵗ / Eₜ  (有效空头权重)
```
多头保持原目标：`ŵᵢᵗᴸ = wᵢᵗ*`。

净多头偏置：
```
Bₜⁿᵉᵗ = (理想空头名义 - 实现空头名义) / Eₜ
```

## 四类执行口径对比 (回测)
| 场景 | 定义 | 用途 |
|---|---|---|
| ideal_fractional | 多空均允许小数股 | 理想上限 |
| opening_short_integer | 只有新开空订单整股 | 旧口径诊断 |
| short_sale_integer | 创建/增加空头的 sell order 整股 | Alpaca 订单层约束 |
| **baseline_floor_targets** | 先投影空头目标为整数股，再执行 short-sale 整股 | **推荐生产** |

## 主要结果 (10k 账户)
| 口径 | 总收益 | 年化 | 最大回撤 | 波动 | 空头部署率 | 平均净多头偏置 |
|---|---:|---:|---:|---:|---:|---:|
| ideal_fractional | 75.58% | 32.73% | -9.20% | 18.52% | 100% | 0% |
| baseline_floor_targets | 96.33% | 40.40% | -18.76% | 24.30% | 60.49% | 40.80% |

**资金容量效应**：
| 初始权益 | 空头部署率 | 净多头偏置均值 | 95% 分位 |
|---:|---:|---:|---:|
| 10,000 | 60.49% | 40.80% | 56.17% |
| 50,000 | 90.14% | 10.14% | 15.25% |
| 100,000 | 94.78% | 5.38% | 8.44% |
| 300,000 | 98.17% | 1.89% | 3.22% |

## 解释
- **10k 账户**：大量空头目标因单票名义不足 1 股被 floor 到 0，空头 hedge 失真，在上涨环境收益高但回撤/波动放大。这**不是免费 alpha**，而是被迫的净多头暴露。
- **50k+**：整股影响迅速收敛。300k 账户 baseline 与 ideal 几乎无差异。

## 生产实现
代码已实现：
- `src/alpaca_executor.py`: 默认启用 `--floor-short-targets-to-whole-shares` + `--short-sales-whole-shares-only`
- `src/backtest/phase7k_backtest.py`: `--execution-scenarios` 支持四类口径对比

每日应记录诊断：`desired_short_notional`, `realized_short_notional`, `lost_notional`, `zeroed_short_count`, `net_long_bias`。

## 小资金风险修正选项 (未上线)
若要减少净多头偏置：
1. 对多头按同等比例缩放：`ŵᵢᴸ·ˢᶜᵃˡᵉᵈ = (1 - Bₜⁿᵉᵗ)·wᵢᴸ`
2. 空头侧整数 knapsack：把剩余可用名义分配给最接近 1 股且 alpha 高的候选
3. 在候选池层提前排除 `Eₜ|wᵢˢ|/Pᵢ < 1` 的标的

当前 baseline 先不上修正，10k 账户应将该策略视为"净多头增强版"而非严格 market neutral。

---
*Alpaca fractional trading 文档：<https://docs.alpaca.markets/us/docs/fractional-trading>*
