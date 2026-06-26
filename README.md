# us-quant-live

美股多因子横截面多空策略实盘系统。项目覆盖动态股票池、Alpha/Decision Engine、lot 最短持仓锁定、Reg T 两阶段调仓执行、Alpaca/IBKR 执行接口与后台守护调度。

## 守护进程

后台守护由两层组成：

- `daily_alpaca_scheduler.py`：按北京时间 `12:00` 运行当日 `decision`，按北京时间 `22:00` 执行当日目标仓位。
- `watch_daily_alpaca_scheduler.ps1`：监控 scheduler 心跳、任务日志和 executor 子进程；scheduler 掉线时自动拉起。

以下命令默认在项目根目录运行。

启动后台常驻：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

查看状态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Status
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Status
```

停止本机守护进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Stop
```

手动补跑当日 decision：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce decision -Date YYYY-MM-DD -Force
```

手动执行会提交真实订单，运行前必须确认目标文件、账户和市场状态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce execute -Date YYYY-MM-DD -Force
```

## 本地配置

实盘账号配置放在本机私有文件中，不提交到 git：

```text
configs/alpaca_acounts/alpaca_accounts.local.json
```

运行前需要确保该文件存在，并包含对应的 `ALPACA_US_FULL` 账户配置。
