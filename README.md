# us-quant-live

美股多因子横截面多空策略实盘系统。项目覆盖动态股票池、Alpha/Decision Engine、lot 最短持仓锁定、Reg T 两阶段调仓执行、Alpaca/IBKR 执行接口与后台守护调度。

## 重要提示

### 数据源要求

**必须使用 SIP (Securities Information Processor) 数据源**：
- IEX 仅覆盖 ~2-3% 美股成交量，会导致大量股票数据缺失
- SIP 覆盖全市场（NYSE/NASDAQ/AMEX），确保 1000-symbol 策略完整运行
- 代码默认使用 SIP，**不要修改 `--feed` 参数**

### API 限速说明

Alpaca 免费版限制 **200 requests/minute**：
- **守护进程模式**（推荐）：每天仅运行 2 次，间隔 10 小时，永不触发限速
- **手动测试**：首次运行可能触发限速（HTTP 403/429），等待 2 分钟重试即可
- **详细排查**：见 [ALPACA_RATE_LIMIT_GUIDE.md](ALPACA_RATE_LIMIT_GUIDE.md)

## 🚀 一键启动（推荐）

使用系统托盘启动器，最简单的启动方式：

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate
python tools/tray_launcher.py
```

或构建 .exe 后双击：

```bash
python tools/build_exe.py
# 之后双击 dist/USQuantLive.exe
```

启动器会：
- ✅ 自动启动 scheduler 守护进程
- ✅ 自动启动 dashboard（http://127.0.0.1:18076）
- ✅ 在系统托盘显示 K 线图标，右键菜单可访问所有功能
- ✅ 单例保护（不会重复启动）
- ✅ 自动监督（scheduler 崩溃时自动重启）
- ✅ 退出时干净清理所有子进程

详细使用方法见 [TRAY_LAUNCHER_GUIDE.md](TRAY_LAUNCHER_GUIDE.md)。

## 守护进程（手动模式）

后台守护由两层组成：

- `daily_alpaca_scheduler.py`：按北京时间 `12:30` 运行当日 `decision`，按北京时间 `22:00` 执行当日目标仓位。
- `watch_daily_alpaca_scheduler.ps1`：监控 scheduler 心跳、任务日志和 executor 子进程；scheduler 掉线时自动拉起。

执行器将 raw alpha 多空权重统一缩放到总 RegT 容量的 `95%`，并以最终 gross 仓位不超过该目标作为硬约束；动态剩余 `buying_power` 仅用于新增订单的券商可行性保护。

`staged_regt` 保持阶段间顺序：先减多头、再回补空头、刷新持仓后再加仓；每个阶段内默认以 `6` 个 worker 并行执行。marketable-limit 每次使用实时 bid/ask 定价，默认最多尝试 `4` 个不重复报价档位（`0/25/75/150 bps`，每档等待 `6s`），并记录批次耗时、排队时间、实时参考价和逐次成交结果。

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
