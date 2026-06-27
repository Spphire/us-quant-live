# 决策链路审计追踪系统设计

## 当前已有的记录

系统已经记录了完整的决策-执行链路，每次运行生成一个时间戳目录（如 `20260627_120000`）：

### 📊 现有文件清单

| 文件 | 内容 | 用途 |
|------|------|------|
| `alpha_core_panel_YYYYMMDD.csv` | 全宇宙因子面板 | 每只股票的 10+ 因子原始值、标准化值、综合得分、排名 |
| `decision_targets.csv` | 目标组合权重 | DecisionEngine 输出的多空权重（优化器结果） |
| `broker_positions_before.csv` | 执行前持仓快照 | Alpaca 账户当前持仓（执行前） |
| `broker_positions_after.csv` | 执行后持仓快照 | Alpaca 账户实际持仓（执行后） |
| `order_plan.json` | 详细订单计划 | 每笔订单的 symbol/side/qty/notional/limit_price |
| `execution_summary.json` | 执行总结 | 账户权益、提交订单数、错误、lot ledger 状态 |
| `execution_records.json` | 逐笔成交记录 | 每笔订单的 Alpaca order_id、filled_qty、avg_price |
| `industry_map_dynamic.csv` | 行业映射 | SIC code → 行业名（用于仪表盘分组展示） |
| `lot_snapshot_YYYYMMDD.json` | 当日 lot 快照 | 每个 lot 的开仓日期、持仓天数、锁定状态 |

### 🔍 现有审计能力

**当前系统可回答的问题**：

1. ✅ **为什么买入/卖出 X 股票？**
   - 查看 `alpha_core_panel`: 该股票的因子得分（momentum/reversal/size/beta/cash_quality）
   - 查看 `decision_targets.csv`: 优化器给它分配的权重

2. ✅ **为什么今天没交易某只票？**
   - `order_plan.json` 的 `decision_diagnostics.fallback_reason` 说明是否优化失败
   - `decision_status` 标记是否用了 fallback 策略（carry/repair）

3. ✅ **订单执行价格是否合理？**
   - `order_plan.json` 记录了计划价格（sizing 用的 adverse price）
   - `execution_records.json` 记录了实际成交均价
   - 对比可得滑点

4. ✅ **Lot 锁定逻辑是否正确？**
   - `lot_snapshot_YYYYMMDD.json` 记录每个 lot 的年龄
   - `execution_summary.json` 的 `alignment_after_execution` 检查持仓是否对齐

5. ✅ **账户权益变化原因？**
   - `execution_summary.json` 的 `account_equity_pre_trade` vs `account_equity_post_trade`
   - 结合 `broker_positions_before/after` 可回溯市值变化

---

## 需要增强的审计点

虽然现有日志很完善，但以下几个关键决策节点**缺少明确记录**：

### 🔴 缺失 1：优化器输入输出中间状态

**问题**：
- `alpha_core_panel` 有原始因子 → `decision_targets` 有最终权重
- 但**优化器约束、目标函数、求解日志**没有保存

**影响**：
- 优化器失败时（如 HiGHS Status 8: Infeasible），无法知道是哪个约束冲突
- Fallback 策略触发时（carry_repair_low_turnover），不知道是什么导致原问题 infeasible

**建议增加**：
```json
optimizer_diagnostics.json:
{
  "status": "infeasible",
  "solver": "highs",
  "solver_exit_code": 8,
  "constraints": {
    "long_leverage": {"min": 0.95, "max": 1.0, "actual": null},
    "short_leverage": {"min": 0.95, "max": 1.0, "actual": null},
    "turnover": {"max": 0.3, "actual": null},
    "position_limit": 0.05
  },
  "objective_function": "maximize_composite_score_minus_turnover_cost",
  "turnover_penalty": 0.001,
  "solve_time_seconds": 1.23,
  "iterations": null,
  "infeasibility_reason": "short_leverage_constraint_conflicts_with_turnover_limit",
  "fallback_triggered": true,
  "fallback_method": "carry_repair_low_turnover"
}
```

### 🔴 缺失 2：动态股票池过滤日志

**问题**：
- `alpha_core_panel` 有 918 只股票，但**不知道从原始数据源筛选掉了哪些股票**
- 不知道哪些票因为 `market_cap < threshold` 被过滤
- 不知道哪些票因为 `fundamental data missing` 被过滤

**影响**：
- 如果某只热门票从未出现在组合中，无法判断是因子得分低，还是根本没进入候选池

**建议增加**：
```json
universe_filtering.json:
{
  "total_symbols_from_alpaca": 12534,
  "filtered_out": {
    "missing_price": 3421,
    "missing_fundamental": 5124,
    "market_cap_too_small": 2891,
    "shares_outstanding_missing": 180,
    "beta_obs_insufficient": 0
  },
  "passed_filters": 918,
  "filter_rules": {
    "min_market_cap": null,  // 当前没有硬过滤
    "min_price": 1.0,
    "require_fundamental": true,
    "min_beta_obs": 60
  }
}
```

### 🔴 缺失 3：因子计算失败/异常值日志

**问题**：
- `alpha_core_panel` 只有最终因子值，**不知道计算中间是否有异常**
- 某些票 `momentum_score = NaN` 时，无法知道是因为价格缺失还是计算溢出

**建议增加**：
```json
factor_computation_warnings.json:
{
  "symbols_with_warnings": [
    {
      "symbol": "AAPL",
      "warnings": [
        "reversal_score: lagged_raw_close is NaN, filled with current close",
        "beta_raw: only 120 observations (< 252 ideal), using available"
      ]
    },
    {
      "symbol": "TSLA",
      "warnings": [
        "cash_to_assets: assets = 0, set cash_quality_score to neutral"
      ]
    }
  ],
  "total_symbols_with_warnings": 2,
  "total_symbols_clean": 916
}
```

### 🟡 缺失 4：订单提交失败详细原因

**问题**：
- `execution_summary.json` 有 `submit_error_count` 和 `submit_abort_reason`
- 但**没有逐笔订单失败的详细错误**（Alpaca API 返回的错误信息）

**建议增强** `execution_records.json`：
```json
[
  {
    "symbol": "AAPL",
    "side": "buy",
    "qty": 10.0,
    "limit_price": 150.25,
    "submit_status": "rejected",
    "alpaca_error_code": "insufficient_buying_power",
    "alpaca_error_message": "Buying power: $1000, required: $1502.50",
    "retry_count": 0
  },
  {
    "symbol": "TSLA",
    "side": "sell",
    "qty": 5.0,
    "limit_price": 250.00,
    "submit_status": "filled",
    "alpaca_order_id": "abc123",
    "filled_qty": 5.0,
    "filled_avg_price": 249.98,
    "fill_time_utc": "2026-06-27T14:00:12Z"
  }
]
```

### 🟡 缺失 5：Lot ledger 更新日志（已部分实现）

**现状**：
- `lot_ledger.json` 有当前状态
- `lot_snapshot_YYYYMMDD.json` 有某日快照
- **缺少：本次执行对 lot ledger 做了哪些修改**

**建议增加** `lot_ledger_delta.json`：
```json
{
  "session_date": "2026-06-27",
  "session_idx": 0,
  "ledger_write_enabled": true,
  "changes": [
    {
      "action": "open_new_lot",
      "symbol": "AAPL",
      "lot_id": "AAPL_20260627_0",
      "qty": 10.0,
      "open_date": "2026-06-27"
    },
    {
      "action": "close_lot",
      "symbol": "TSLA",
      "lot_id": "TSLA_20260620_0",
      "qty": 5.0,
      "open_date": "2026-06-20",
      "hold_days": 7,
      "locked": false
    },
    {
      "action": "partial_close",
      "symbol": "MSFT",
      "lot_id": "MSFT_20260615_0",
      "closed_qty": 3.0,
      "remaining_qty": 7.0,
      "hold_days": 12,
      "locked": true
    }
  ]
}
```

---

## 推荐的增强方案

### 方案 A：最小增强（推荐，快速实现）

**只增加 3 个文件**：
1. `optimizer_diagnostics.json` — 优化器求解详情
2. `universe_filtering.json` — 股票池过滤统计
3. 增强 `execution_records.json` — 加入失败原因

**优点**：
- 工作量小（~200 行代码）
- 覆盖 80% 的复盘需求
- 对现有代码侵入小

**实现位置**：
- `src/decision_engine.py` 增加 optimizer 日志输出
- `src/decision_engine.py` 增加 universe filtering 统计
- `src/alpaca_executor.py` 增强 execution_records

### 方案 B：完整审计系统（完美但工作量大）

**增加 6 个文件 + 1 个汇总**：
1. `optimizer_diagnostics.json`
2. `universe_filtering.json`
3. `factor_computation_warnings.json`
4. 增强 `execution_records.json`
5. `lot_ledger_delta.json`
6. `pricing_data_quality.json` — 价格数据缺失/异常统计
7. `audit_trail_summary.json` — 汇总所有审计文件的 hash 和版本

**优点**：
- 100% 可复盘（任何决策都能追溯根因）
- 符合量化基金的合规审计要求
- 可自动生成"决策链路可视化"

**缺点**：
- 工作量 ~800 行代码
- 存储空间增加 ~50%
- 日志写入耗时 +2-5 秒/次

---

## 立即可做的"零代码"复盘技巧

在增强日志前，现有文件已经能支持大部分复盘：

### 示例：为什么 2026-06-27 买入 OSCR？

```bash
# 1. 查看因子得分
cd artifacts/daily_alpaca_scheduler/output/20260627_120000
grep "^OSCR," alpha_core_panel_20260627.csv | cut -d, -f1,47-52

# 输出示例：
# OSCR,reversal_score=1.2,momentum_score=0.8,small_size_score=1.5,
#      low_beta_score=0.9,cash_quality_score=1.1,composite_score=2.8

# 2. 查看分配权重
grep "^OSCR," decision_targets.csv
# OSCR,0.04719398436861047,long,0.04719398436861047

# 3. 查看执行计划
cat order_plan.json | jq '.orders[] | select(.symbol=="OSCR")'
# {"symbol":"OSCR","side":"buy","qty":28.5,"notional":4461.2,"limit_price":156.50}

# 4. 查看实际成交
cat execution_records.json | jq '.[] | select(.symbol=="OSCR")'
# {"symbol":"OSCR","filled_qty":28.5,"filled_avg_price":156.48,"order_id":"xyz"}
```

### 示例：为什么今天优化器失败？

```bash
cat execution_summary.json | jq '.decision_diagnostics'
# {
#   "status": "repair",
#   "fallback_method": "carry_repair_low_turnover",
#   "fallback_reason": "optimizer_failed:The_problem_is_infeasible._(HiGHS_Status_8)"
# }
```

---

## 建议实施步骤

### 第一步（本周）：方案 A 最小增强

1. 修改 `src/decision_engine.py`：
   - 输出 `optimizer_diagnostics.json`
   - 输出 `universe_filtering.json`

2. 修改 `src/alpaca_executor.py`：
   - 增强 `execution_records.json` 包含失败原因

3. 测试验证：
   - 运行一次完整决策-执行
   - 确认 3 个新文件生成且格式正确

### 第二步（可选，下周）：Dashboard 增加"审计追踪"页

在 `dashboard.html` 新增 Tab：**Audit Trail**

显示：
- 优化器求解状态（成功/失败/fallback）
- 股票池过滤统计（通过/过滤）
- 订单提交成功率（成功/失败）
- 可下载当日所有审计文件的 .zip

### 第三步（可选，长期）：离线复盘工具

创建 `tools/backtest_audit.py`：
```python
# 用法：python tools/backtest_audit.py --start 2026-06-01 --end 2026-06-30
# 输出：每日决策质量评分、平均滑点、优化器成功率、因子贡献归因
```

---

## 存储成本估算

**当前单次运行**：~900 KB
**方案 A 增强后**：~950 KB (+5%)
**方案 B 完整审计**：~1.3 MB (+44%)

**年度存储**：
- 当前：900 KB × 252 trading days = ~220 MB/年
- 方案 A：950 KB × 252 = ~234 MB/年
- 方案 B：1.3 MB × 252 = ~320 MB/年

**结论**：即使完整审计，年度存储 < 350 MB，完全可接受。

---

## 总结

**✅ 当前日志已经很强**：
- 因子面板完整
- 订单计划详细
- 执行记录清晰
- Lot 快照完备

**🔧 需要补充**（按优先级）：
1. **HIGH**：优化器诊断（为什么 infeasible）
2. **HIGH**：股票池过滤统计（为什么某票不在池内）
3. **MEDIUM**：订单失败详细原因（Alpaca API 错误信息）
4. **LOW**：因子计算 warnings（NaN/异常值）
5. **LOW**：Lot ledger delta（本次改了什么）

**建议**：先实施方案 A（工作量 1-2 小时），覆盖 80% 复盘需求。
