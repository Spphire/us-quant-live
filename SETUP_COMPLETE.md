# ✅ 环境已配置完成

## 已完成的工作

### 1. 虚拟环境 (venv) ✅
- 路径: `W:\实验室项目\us-quant-live\venv\`
- Python 版本: 3.13.5
- 已安装依赖:
  - pandas 3.0.3
  - numpy 2.5.0
  - scipy 1.18.0
  - requests 2.34.2
  - 及相关依赖 (tzdata, python-dateutil, certifi, etc.)

### 2. 激活脚本 ✅
- **Bash/Git Bash**: `source activate.sh`
- **PowerShell**: `. .\activate.ps1`
- **CMD**: `venv\Scripts\activate.bat`

### 3. 快速启动文档 ✅
- `QUICKSTART.md` - 常用命令速查
- `DAEMON_STARTUP_GUIDE.md` - 守护进程完整指南

---

## ⚠️ 你现在需要做的（启动前必须）

### 创建 Alpaca 账户配置文件

```bash
cd W:\实验室项目\us-quant-live

# 1. 复制模板
cp configs/alpaca_acounts/alpaca_accounts.local.json.template configs/alpaca_acounts/alpaca_accounts.local.json

# 2. 编辑配置（填入你的 API key）
notepad configs/alpaca_acounts/alpaca_accounts.local.json
```

在打开的文件中修改：
```json
{
  "ALPACA_US_FULL": {
    "api_key": "填入你的 Alpaca API key",
    "api_secret": "填入你的 Alpaca secret key",
    "base_url": "https://paper-api.alpaca.markets",
    "comment": "首次测试建议用 paper account"
  }
}
```

**获取 Alpaca API key**:
1. 注册 Alpaca 账户: https://alpaca.markets/
2. 创建 Paper Trading Account (模拟账户，无需真实资金)
3. 在 Dashboard → API Keys 生成 API key 和 secret

---

## 🧪 测试修复是否生效

配置好后，运行单次 decision 测试：

```bash
# 激活 venv
source activate.sh

# 运行 decision (不下单，只生成目标)
python src/alpaca_executor.py \
  --date 2026-06-27 \
  --trigger-mode plan_only \
  --no-submit \
  --output-root artifacts/test_decision
```

**验证修复成功的标志**：
1. 查看 `artifacts/alpaca_executor/lot_ledger.json`
2. 确认包含多个 `factor` 值（不只是 "broker_sync"）:
   - "reversal_score"
   - "momentum_score"
   - "small_size_score"
   - "low_beta_score"
   - "cash_quality_score"
3. 确认 `min_hold` 有多种值（不只是 0）:
   - reversal: 5
   - momentum: 10
   - size/beta/cash: 20

**修复前的坏状态**（如果你看到这样说明没生效）：
```json
{
  "ledger": {
    "long": [
      {"factor": "broker_sync", "min_hold": 0, ...},
      {"factor": "broker_sync", "min_hold": 0, ...}
    ],
    "short": [...]
  }
}
```

**修复后的好状态**（应该看到这样）：
```json
{
  "ledger": {
    "long": [
      {"factor": "reversal_score", "min_hold": 5, ...},
      {"factor": "momentum_score", "min_hold": 10, ...},
      {"factor": "small_size_score", "min_hold": 20, ...}
    ],
    "short": [...]
  }
}
```

---

## 🚀 启动守护进程

测试通过后，启动定时调度：

```powershell
cd W:\实验室项目\us-quant-live

# 前台测试模式（推荐首次使用）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Foreground -Once

# 后台守护模式（生产环境）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

---

## 📚 文档索引

- **QUICKSTART.md** - 常用命令速查表
- **DAEMON_STARTUP_GUIDE.md** - 完整守护进程设置指南
- **README.md** - 项目概述

---

## 🐛 关键 Bug 已修复

**问题**: 实盘流程中，按因子分割、各自带不同锁仓时间的 lot 记忆从未真正持久化，导致 min-hold 锁仓机制失效。

**修复**: 
- Decision 阶段现在会持久化因子 lot（即使不下单）
- session_idx 现在按交易日计数（不再按进程调用次数）
- Execute 阶段正确加载带因子信息的 lot

**影响**: 修复后，换手率应该显著降低，与回测中的 lot 机制保持一致（Phase7J 无锁仓换手 0.658 → Phase7K 有锁仓换手 0.153）。

---

准备好后，运行上述测试命令即可验证！
