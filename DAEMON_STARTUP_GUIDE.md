# 守护进程启动指南

## 前置条件

### 1. 创建 Alpaca 账户配置
复制模板并填入你的 API key：
```powershell
cd W:\实验室项目\us-quant-live
copy configs\alpaca_acounts\alpaca_accounts.local.json.template configs\alpaca_acounts\alpaca_accounts.local.json
notepad configs\alpaca_acounts\alpaca_accounts.local.json
```

在打开的文件中填入：
- `api_key`: 你的 Alpaca API key
- `api_secret`: 你的 Alpaca secret key
- `base_url`: 
  - Paper trading (测试): `https://paper-api.alpaca.markets`
  - Live trading (真实资金): `https://api.alpaca.markets`

**重要**：首次运行强烈建议用 paper account 测试！

### 2. 安装 Python 依赖
```bash
cd W:\实验室项目\us-quant-live
pip install pandas numpy scipy requests
```

## 守护进程架构

```
watch_daily_alpaca_scheduler.ps1 (看门狗)
  └─> run_daily_alpaca_scheduler.ps1 (launcher)
      └─> daily_alpaca_scheduler.py (主调度器)
          ├─> 12:00 CN: alpaca_executor.py --trigger-mode plan_only (decision)
          └─> 22:00 CN: alpaca_executor.py --decision-targets-input-path ... (execute)
```

**执行时间**：
- **12:00 北京时间**：运行 DecisionEngine，生成当日目标权重（含因子 lot），不下单
- **22:00 北京时间**：启动执行器，等到纽约时间 10:00 (开盘后 30 分钟) 真实下单

## 启动命令

### 方式 1：后台守护模式（推荐生产）
```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

看门狗会：
- 启动调度器
- 每 60 秒检查一次心跳
- 调度器掉线时自动重启
- 输出到 `artifacts/daily_alpaca_scheduler/watchdog/watchdog.log`

### 方式 2：前台测试模式（首次建议）
```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Foreground -Once
```

前台运行，Ctrl+C 即停止，适合观察日志。

### 方式 3：单次手动测试（最安全）
先只跑 decision（不下单）：
```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce decision -Date 2026-06-27 -Force
```

检查生成的 `artifacts/daily_alpaca_scheduler/output/*/decision_targets.csv` 和 `lot_snapshot_*.json`。

如果确认无误，再手动跑 execute（**这会真实下单！**）：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce execute -Date 2026-06-27 -Force
```

## 查看状态

```powershell
cd W:\实验室项目\us-quant-live

# 看门狗状态
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Status

# 调度器状态
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Status
```

## 停止守护进程

```powershell
cd W:\实验室项目\us-quant-live

# 停止看门狗
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Stop

# 停止调度器
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Stop
```

## 日志位置

- 调度器主日志：`artifacts/daily_alpaca_scheduler/daemon/scheduler.out.log`
- 调度器错误：`artifacts/daily_alpaca_scheduler/daemon/scheduler.err.log`
- 看门狗日志：`artifacts/daily_alpaca_scheduler/watchdog/watchdog.log`
- 每日任务日志：`artifacts/daily_alpaca_scheduler/logs/YYYYMMDD_decision.out.log` 等
- 执行产物：`artifacts/daily_alpaca_scheduler/output/*/`

## 关键配置参数

可通过 `-SchedulerLauncherArgs` 传递给调度器：
```powershell
-SchedulerLauncherArgs @(
    "--decision-time-cn", "12:00",
    "--execute-time-cn", "22:00",
    "--target-ny-time", "10:00",
    "--execution-mode", "staged_regt",
    "--buying-power-buffer", "0.88"
)
```

## 安全检查清单

在生产运行前确认：
- [ ] 用的是 **paper account** 或已充分回测/模拟测试
- [ ] `alpaca_accounts.local.json` 中 `base_url` 正确
- [ ] 至少手动跑过一次 `--RunOnce decision` 确认目标合理
- [ ] 理解 22:00 CN = 启动执行器，实际下单在 NY 10:00 (夏令时约北京次日 22:00, 冬令时 23:00)
- [ ] 看过 lot_ledger 确认因子 lot 正确持久化（本次已修复）

## 故障排查

**Q: 看门狗启动失败"execution policy"**
A: 以管理员身份运行一次：
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Q: 调度器反复重启**
A: 检查 `scheduler.err.log`，常见原因：
- 配置文件路径/格式错误
- Python 依赖缺失
- API key 无效

**Q: decision 成功但 execute 不运行**
A: 检查：
- `state.json` 中 decision.status 是否 completed
- `decision_targets.csv` 是否存在且非空
- 是否在交易日（周末/节假日会跳过）

**Q: lot 历史丢失（已修复）**
A: 本次修复确保 decision 阶段落盘因子 lot，execute 阶段正确加载。升级到最新代码后首次运行会重建 ledger。

---
**首次建议**：先用 `-Foreground -Once` 模式观察一个完整周期，确认无误后再用 `-Force` 启动后台守护。
