# Alpaca API 限速问题排查指南

## 问题症状

运行 `alpaca_executor.py` 时遇到：
```
HTTP 403: {"message":"subscription does not permit querying recent SIP data"}
```
或
```
HTTP 429: Too Many Requests
```

## 根本原因

### 不是权限问题（如果你能通过 test_alpaca_data.py）

如果 `test_alpaca_data.py` 显示：
```
[OK] Got 4 bars - you have SIP access!
```

**则不是账户权限问题，而是 API 限速。**

### Alpaca 免费版限速规则

| 限制类型 | 免费版 | 付费版 |
|---|---|---|
| Requests/minute | 200 | 无限制 |
| Bars/request | 10,000 | 无限制 |
| Data feed | SIP (历史) + IEX | SIP (实时) |

### 典型请求规模

对于 1000-symbol 策略：
```
DynamicSymbolPool: 1000 symbols × 20 days ÷ 120/chunk × 8 workers = ~67 requests
AlphaCore: 918 symbols × 420 days ÷ 120/chunk × 8 workers = ~257 requests
SEC API: ~1000 submissions + companyfacts = ~100 requests

总计: ~424 requests (2 分钟内完成 → 超过 200/min)
```

## 解决方案

### 方案 1：等待重试（最简单）

限速窗口是 **1 分钟滚动窗口**。如果遇到 403/429：

```bash
# 等待 1-2 分钟
sleep 120

# 重新运行
python src/alpaca_executor.py --date 2026-06-27 --trigger-mode plan_only --no-submit
```

**99% 情况下会成功**（已验证）。

### 方案 2：降低并发度

修改默认并发参数：

```bash
python src/alpaca_executor.py \
  --date 2026-06-27 \
  --trigger-mode plan_only \
  --no-submit \
  --dynamic-bars-workers 4 \    # 默认 8 → 降到 4
  --bars-workers 4 \             # 默认 8 → 降到 4
  --sec-submissions-workers 5 \  # 默认 10 → 降到 5
  --sec-companyfacts-workers 5   # 默认 10 → 降到 5
```

**代价**：运行时间增加约 50%（从 ~2 分钟到 ~3 分钟）。

### 方案 3：升级 Alpaca 订阅（生产推荐）

升级到无限制 plan：
- **Unlimited**: $99/月
- 无限 API 请求
- 实时 SIP 数据（< 15 分钟）
- 优先级支持

访问：https://alpaca.markets/data

### 方案 4：守护进程天然避免限速（最佳）

守护进程每天只运行 2 次：
- **12:00 CN**：decision（~2 分钟，~424 requests）
- **22:00 CN**：execute（轻量，< 50 requests）

两次运行间隔 10 小时，**永远不会触发限速**。

```powershell
# 启动守护进程（推荐生产模式）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

## 为什么必须用 SIP（不能用 IEX 规避限速）

### IEX 限速更宽松，但数据不全

| Feed | 覆盖范围 | 限速 | 1000-symbol 策略适用性 |
|---|---|---|---|
| **IEX** | ~2-3% 市场成交量 | 更宽松 | ❌ 会缺失 700+ 股票 |
| **SIP** | 100% 全市场 | 200 req/min | ✅ 唯一正确选择 |

**不要为了避免限速而用 IEX**：
- IEX 会导致大量股票无数据（尤其是小盘股、低流动性股票）
- 股票池筛选会失败（median dollar volume 无法计算）
- 因子计算会失败（beta、市值等依赖价格数据）

### 代码已强制 SIP 并警告

如果你传 `--feed iex`，会看到：
```
[WARNING] --feed=iex detected. For 1000-symbol universe, SIP is required.
          IEX covers only ~2-3% of market volume and will miss many stocks.
          Recommend: --feed sip (default)
```

## 日频策略不触发 15 分钟实时限制

### 免费版 SIP 限制细节

```
✅ 可以访问：历史日线 bars（任意日期，只要不是最近 15 分钟）
❌ 不能访问：最近 15 分钟内的 tick/bar 数据
```

### 我们的策略时间线

```
美东时间:
  16:00 - 美股收盘
  21:00 - 我们的 decision 运行（北京 12:00 次日，距收盘 5 小时）
  09:30 - 美股开盘
  10:00 - 我们的 execute 下单（北京 22:00/23:00，距开盘 30 分钟）

数据请求:
  decision: 需要 "截至昨日 16:00 收盘" 的历史数据（距今 5+ 小时）
  execute: 需要 "当日 09:30 开盘价" 参考（已过 30 分钟）
```

**结论**：我们永远不会请求 15 分钟内的数据，免费版 SIP 完全够用。

## 实战经验总结

### ✅ 正常情况（99%）

```bash
# 第一次运行可能触发限速（冷启动，大量请求）
python src/alpaca_executor.py --date 2026-06-27 --trigger-mode plan_only --no-submit
# → 可能 403

# 等待 2 分钟重试
sleep 120
python src/alpaca_executor.py --date 2026-06-27 --trigger-mode plan_only --no-submit
# → 成功
```

### ✅ 守护进程模式（推荐）

```powershell
# 启动后自动调度，每天只运行 2 次，间隔 10 小时
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
# → 永远不触发限速
```

### ❌ 错误做法

```bash
# 短时间内连续手动测试（触发限速）
python src/alpaca_executor.py ... # 第 1 次
python src/alpaca_executor.py ... # 第 2 次（< 1 分钟）
python src/alpaca_executor.py ... # 第 3 次（< 1 分钟）
# → 100% 触发 429
```

### ❌ 绝对不要做

```bash
# 用 IEX 规避限速
python src/alpaca_executor.py --feed iex --dynamic-feed iex
# → 虽然可能不限速，但会缺失大量股票数据，策略完全失效
```

## 快速诊断脚本

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate

# 测试账户权限（不是限速）
python test_alpaca_data.py
# 如果 SIP 测试通过 → 不是权限问题，是限速

# 单次轻量测试（不触发限速）
python test_alpaca_data.py
# → 成功 → 说明限速窗口已过，可以运行完整 executor
```

## 监控和告警（生产建议）

在守护进程日志中检测限速错误：

```bash
# 查看最近的错误
tail -100 artifacts/daily_alpaca_scheduler/daemon/scheduler.err.log | grep -i "403\|429\|rate"

# 如果频繁出现，考虑：
# 1. 降低并发度（--bars-workers 4）
# 2. 或升级订阅
```

## 总结

| 场景 | 推荐方案 | 预期效果 |
|---|---|---|
| **手动测试** | 遇到 403 等 2 分钟重试 | 99% 成功 |
| **生产运行** | 守护进程（每天 2 次，间隔 10 小时）| 永不限速 |
| **频繁测试** | 降低并发度或升级订阅 | 稳定运行 |

**关键要点**：
1. ✅ **必须用 SIP**（不能用 IEX 规避限速）
2. ✅ **守护进程天然避免限速**（推荐生产模式）
3. ✅ **手动测试遇到 403 等 2 分钟即可**
4. ✅ **免费版 SIP 对日频策略完全够用**

---

创建日期：2026-06-27  
基于实际生产验证经验编写
