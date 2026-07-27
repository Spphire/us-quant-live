# us-quant-live

美股多因子横截面多空策略实盘系统。项目覆盖动态股票池、Alpha/Decision Engine、lot 最短持仓锁定、Reg T 两阶段调仓执行、Alpaca/IBKR 执行接口与后台守护调度。

## 重要提示

### 数据源要求

**必须使用 SIP (Securities Information Processor) 数据源**：
- IEX 仅覆盖 ~2-3% 美股成交量，会导致大量股票数据缺失
- SIP 覆盖全市场（NYSE/NASDAQ/AMEX），确保 1000-symbol 策略完整运行
- 历史因子和动态股票池默认使用 Alpaca SIP，**不要修改 `--feed` 和 `--dynamic-feed` 参数**
- 实时执行定价默认使用长桥 OpenAPI 美股 LV1/NBBO，不再使用 Alpaca IEX 作为目标股数和限价单参考

长桥执行行情依赖项目虚拟环境中的 `longport` SDK，并从 Git 忽略的本地配置读取凭证：

```powershell
.\venv\Scripts\python.exe -m pip install longport==4.3.3
```

```text
configs/longbridge.local.json
```

执行器启动后会一次订阅目标、当前持仓和审计基准标的。缺失、过期、反向、过宽盘口或 NBBO 权限异常会阻止对应执行，不会静默退回 IEX 或历史收盘价。

流式盘口超过新鲜度阈值时，执行器会先通过长桥主动刷新报价和深度快照，再重新执行完整校验；快照刷新失败仍会阻止下单。分阶段 RegT 执行将反向换仓严格拆成平仓到零和反向建仓，并在建仓前等待 Alpaca 持仓查询与已成交股数完成一致性同步，避免经纪商状态短暂滞后导致重复平仓。

Decision 会先将配置候选与 Alpaca active/clean-core/tradable 资产、长桥静态行情覆盖取交集，再交给动态流动性池和 AlphaCore。交集及逐标的剔除原因写入 `symbol_universe_intersection.json/.csv`；Execute 必须读取同日 decision 快照，重新检查长桥覆盖漂移，并阻止越界目标或缺少行情的目标/持仓退出单。已有持仓若离开候选域，只允许同方向减仓或退出。

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

`staged_regt` 保持阶段间顺序：先减多头、再回补空头、刷新持仓后再加仓；每个阶段内默认以 `6` 个 worker 并行执行。marketable-limit 每次使用不超过 `5s` 的实时 bid/ask 定价，每个标的跨 release 重建轮次合计最多尝试 `4` 个不重复报价档位（`0/25/75/150 bps`，每档等待 `6s`）。entry 完成后会重新同步仓位，只对最终未成交且仍有可执行权重缺口的标的按误差从大到小进行一次残差修复；修复默认仅提交一个受 `150 bps` 下限/上限保护的限价尝试。审计会分别记录正常改价撤单、修复成功和最终漏单。

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
configs/longbridge.local.json
```

运行前需要确保该文件存在，并包含对应的 `ALPACA_US_FULL` 账户配置。
