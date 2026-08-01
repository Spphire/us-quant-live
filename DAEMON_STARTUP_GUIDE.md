# 从零部署与守护进程指南

本文是 Windows 生产部署的权威流程。默认组合是：

| 服务 | 用途 | 最低要求 |
|---|---|---|
| Alpaca | 账户、持仓、交易、交易日历、历史 SIP 日线 | Trading API key；账户状态 `ACTIVE`；允许做空 |
| 长桥 OpenAPI | 实时执行价格、bid/ask、深度和标的覆盖 | App Key、App Secret、Access Token；美股 LV1/NBBO 权限 |

订单只提交到 Alpaca。长桥只提供行情，不接管持仓或下单。Decision 使用两边可用标的的交集，因此两套账户和权限都必须正常。

## 1. 准备机器

- Windows 10/11 x64
- Git
- Python 3.13 x64
- 可访问 Alpaca 与长桥 OpenAPI 的网络
- 需要显示托盘图标时，Windows 用户必须保持登录；开机自启任务使用交互式登录会话

```powershell
git clone git@github.com:Spphire/us-quant-live.git
cd .\us-quant-live
py -3.13 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 配置 Alpaca

先使用 Alpaca Paper Trading 账户完成验收。创建 Trading API key 后复制模板：

```powershell
Copy-Item .\configs\alpaca_acounts\alpaca_accounts.local.json.template `
  .\configs\alpaca_acounts\alpaca_accounts.local.json
notepad .\configs\alpaca_acounts\alpaca_accounts.local.json
```

本地文件格式：

```json
{
  "ALPACA_US_FULL": {
    "api_key": "YOUR_ALPACA_API_KEY",
    "secret_key": "YOUR_ALPACA_SECRET_KEY",
    "base_url": "https://paper-api.alpaca.markets"
  }
}
```

注意：

- 字段名是 `secret_key`，不是 `api_secret`。
- `base_url` 不要附加 `/v2`。
- Paper 使用 `https://paper-api.alpaca.markets`；真实资金才使用 `https://api.alpaca.markets`。
- Alpaca 控制台需要显示账户为 Active，且 `shorting_enabled=true`。本策略包含空头，关闭做空时预检会失败。

## 3. 配置长桥

在长桥 OpenAPI 页面创建应用并取得 App Key、App Secret、Access Token。账户必须能报告美股 `USAA + NBBO` 行情权限；只有基础延迟行情不满足执行要求。

```powershell
Copy-Item .\configs\longbridge.example.json .\configs\longbridge.local.json
notepad .\configs\longbridge.local.json
```

本地文件格式：

```json
{
  "app_key": "YOUR_LONGPORT_APP_KEY",
  "app_secret": "YOUR_LONGPORT_APP_SECRET",
  "access_token": "YOUR_LONGPORT_ACCESS_TOKEN",
  "enable_overnight": false
}
```

Access Token 失效或行情套餐到期后，Decision/Execute 会被阻止，不会静默退回 IEX 或收盘价。

两份 `*.local.json` 已被 Git 忽略。配置后可确认：

```powershell
git check-ignore .\configs\alpaca_acounts\alpaca_accounts.local.json
git check-ignore .\configs\longbridge.local.json
```

## 4. 运行只读部署预检

先检查文件和依赖，不访问外部服务：

```powershell
.\venv\Scripts\python.exe .\tools\check_deployment_readiness.py --config-only
```

再进行只读联网检查：

```powershell
.\venv\Scripts\python.exe .\tools\check_deployment_readiness.py
```

通过标准：

- 总状态为 `pass`
- Alpaca `status=ACTIVE`
- Alpaca `shorting_enabled=true`，所有 blocked flag 为 false
- Alpaca 能读取至少一根历史 SIP 日线
- 长桥 `us_nbbo_reported=true`
- AAPL/MSFT 样本覆盖完整

预检不会提交、修改或取消订单。

## 5. 首次 Decision 与 Paper Execute

选择最近一个美股交易日，只跑 Decision。它会下载完整历史输入，可能需要数分钟，但不会下单：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\run_daily_alpaca_scheduler.ps1 `
  -Python .\venv\Scripts\python.exe `
  -RunOnce decision -Date YYYY-MM-DD -Force
```

重点检查：

- `artifacts\daily_alpaca_scheduler\YYYYMMDD_decision\decision_targets.csv`
- `symbol_universe_intersection.json/.csv` 的最终交集和剔除原因
- Dashboard 的 Decision Intent、Price Evidence、Min Bars 和 Quotes

只有 Alpaca 配置明确指向 Paper 时，才进行首次 Execute。该命令会向 Paper 账户真实提交模拟订单：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\run_daily_alpaca_scheduler.ps1 `
  -Python .\venv\Scripts\python.exe `
  -RunOnce execute -Date YYYY-MM-DD -Force
```

验收执行完成、无 terminal misses、持仓对账通过，并检查理想到实际权重误差。

## 6. 启动托盘绑定的生产进程

统一使用根目录的 `Start.bat`：

```powershell
.\Start.bat
```

它会先停止本项目已有的托盘、scheduler 和 dashboard，再启动新的托盘进程；如果 Decision/Execute 正在运行，会拒绝重启以避免中断交易。正常进程关系是：

```text
tray_launcher.py
  -> daily_alpaca_scheduler.py
       -> dashboard_server.py
       -> alpaca_executor.py (仅任务运行期间存在)
```

默认时刻：

- 北京时间 `12:30`：Decision
- 北京时间 `22:00`：启动 Execute，并对齐纽约时间 `10:00`
- Dashboard：`http://127.0.0.1:18076/`

## 7. 注册开机自启

开机自启准确说是“用户登录后启动”，这样托盘图标才能出现在右下角：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_autostart_task.ps1 -RunNow
```

查看任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_autostart_task.ps1 -Status
```

移除任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\install_autostart_task.ps1 -Unregister
```

## 8. 部署后健康检查

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\tools\check_process_health.ps1
```

期望输出 `status=pass`，并且：

- `scheduler_bound_to_launcher=True`
- `dashboard_bound_to_scheduler=True`
- `dashboard_listening=True`
- Windows 通知区域能看到托盘图标

也可直接访问：

- Dashboard：`http://127.0.0.1:18076/`
- 进程健康：`http://127.0.0.1:18076/api/process-health`

## 9. 日志与故障定位

| 内容 | 路径 |
|---|---|
| Start.bat | `artifacts/daily_alpaca_scheduler/daemon/startup.bat.log` |
| 托盘 | `artifacts/daily_alpaca_scheduler/daemon/tray_launcher.log` |
| Scheduler stdout/stderr | `artifacts/daily_alpaca_scheduler/daemon/` |
| 每日任务 | `artifacts/daily_alpaca_scheduler/logs/` |
| Decision/Execute 审计 | `artifacts/daily_alpaca_scheduler/YYYYMMDD_*/audit/` |

常见失败：

- `missing api_key or secret_key`：Alpaca 模板字段或本地 JSON 错误。
- `alpaca_shorting_disabled`：账户尚未获得做空权限，不能部署本策略。
- `does not report the required US NBBO`：长桥行情权限不足或套餐已过期。
- `Longbridge quote warmup incomplete`：网络、订阅、标的权限或盘前/盘后行情状态异常。
- 右下角没有图标：确认使用的是 `Start.bat`/登录任务，而不是只启动了 headless scheduler。

## 上线清单

- [ ] Python 依赖安装完成
- [ ] 两份本地凭据文件均被 Git 忽略
- [ ] 只读部署预检为 `pass`
- [ ] Decision 成功且标的交集合理
- [ ] Alpaca Paper Execute 完成并通过审计
- [ ] `Start.bat` 启动后进程健康为 `pass`
- [ ] 托盘图标可见且 Restart Scheduler 可用
- [ ] 开机自启任务已注册并实际登录验证一次
