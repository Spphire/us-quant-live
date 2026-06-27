# Quick Start Scripts for us-quant-live

## Important Notes

**Data Feed**: System defaults to SIP (full market coverage). **DO NOT** use `--feed iex` - IEX covers only ~2-3% of market and will cause missing data for most stocks.

**Rate Limiting**: First run may hit Alpaca's 200 req/min limit (HTTP 403/429). Wait 2 minutes and retry. Daemon mode (every 12h) never hits limit. See [ALPACA_RATE_LIMIT_GUIDE.md](ALPACA_RATE_LIMIT_GUIDE.md) for details.

## Activate Virtual Environment

**Bash/Git Bash:**
```bash
source activate.sh
```

**PowerShell:**
```powershell
. .\activate.ps1
```

**CMD:**
```cmd
venv\Scripts\activate.bat
```

## Test the Fix (Single Decision Run - No Trading)

```bash
# Activate venv first
source activate.sh

# Run decision only (generates targets, does NOT submit orders)
python src/alpaca_executor.py \
  --date 2026-06-27 \
  --trigger-mode plan_only \
  --no-submit \
  --output-root artifacts/test_decision
```

Check outputs:
- `artifacts/test_decision/decision_targets.csv` - target weights
- `artifacts/test_decision/lot_snapshot_*.json` - factor lots with min_hold
- `artifacts/alpaca_executor/lot_ledger.json` - persisted ledger

Verify the ledger contains lots with:
- `factor`: "reversal_score", "momentum_score", etc. (NOT just "broker_sync")
- `min_hold`: 5, 10, 20 (NOT just 0)

## Run Full Scheduler Test

```powershell
# Make sure you've filled in configs/alpaca_acounts/alpaca_accounts.local.json first!

# Test decision only
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce decision -Date 2026-06-27 -Force

# If decision looks good, test execute (will submit real orders if config is live!)
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -RunOnce execute -Date 2026-06-27 -Force
```

## Start Daemon (Production)

**Background mode:**
```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

**Foreground mode (for testing):**
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Foreground -Once
```

## Check Status

```powershell
# Watchdog status
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Status

# Scheduler status
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Status
```

## Stop Daemon

```powershell
# Stop watchdog
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Stop

# Stop scheduler
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\run_daily_alpaca_scheduler.ps1 -Stop
```

## Log Locations

- Scheduler: `artifacts/daily_alpaca_scheduler/daemon/scheduler.out.log`
- Watchdog: `artifacts/daily_alpaca_scheduler/watchdog/watchdog.log`
- Daily tasks: `artifacts/daily_alpaca_scheduler/logs/YYYYMMDD_*.log`

---

**First time setup checklist:**
- [ ] Create `configs/alpaca_acounts/alpaca_accounts.local.json` from template
- [ ] Fill in Alpaca API key/secret (use paper account for testing!)
- [ ] Run single decision test to verify lot persistence works
- [ ] Start daemon in foreground mode to observe one full cycle
- [ ] Switch to background mode for production
