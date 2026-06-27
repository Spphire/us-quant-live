# Live Trading Dashboard

Real-time web interface for monitoring the daily trading strategy execution.

## Quick Start

### Automatic Launch (Recommended)

The dashboard automatically starts when you run the daemon:

```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

Then open browser to: **http://127.0.0.1:8766**

### Manual Launch (Standalone)

If you want to view the dashboard without running the full scheduler:

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate

python tools/dashboard_server.py \
  --artifacts-root artifacts/daily_alpaca_scheduler \
  --project-root . \
  --port 8766
```

## Features

### 📊 Overview Tab
- **Portfolio Equity**: Current account value with daily change
- **Position Count**: Total positions split by Long/Short
- **Session Index**: Current trading day counter
- **Status Timeline**: 12:00 Decision and 22:00 Execute task status
- **Equity Curve**: 30-day portfolio value chart

### 📈 Positions Tab
- **All Active Positions**: Sortable table with full details
- **Filters**: Search by symbol, filter by side (Long/Short), filter by P&L
- **Columns**: Symbol, Side, Qty, Market Value, Entry Price, Current Price, Unrealized P&L, % Change
- **Color Coding**: Green for profits, Red for losses

### 🎯 Factor Lots Tab
- **Composition Pie Chart**: Visual breakdown by factor
- **Factor Distribution**: Count of lots per factor
- **Locked vs Unlocked**: How many lots are within min_hold period
- **Lot Detail Table**: All individual lots with factor, weight, min_hold, birth_idx
- **Verification**: Confirm the lot history fix is working

## Architecture

```
Dashboard Server (Python http.server)
    ├── REST API
    │   ├── /api/overview     - Account summary
    │   ├── /api/positions    - Active positions
    │   ├── /api/lots         - Factor lots
    │   ├── /api/history      - Execution history
    │   ├── /api/config       - System config
    │   └── /api/logs         - Recent logs
    └── Static Files
        └── /                 - dashboard.html (Vanilla JS + Chart.js)

Data Sources:
    - artifacts/daily_alpaca_scheduler/state.json (scheduler state)
    - artifacts/daily_alpaca_scheduler/output/*/execution_summary.json (per-run data)
    - artifacts/daily_alpaca_scheduler/output/*/broker_positions_after.csv (positions)
    - artifacts/alpaca_executor/lot_ledger.json (factor lots)
```

## Configuration

### Scheduler Arguments

```bash
--enable-dashboard          # Launch dashboard (default: enabled)
--no-dashboard              # Disable dashboard
--dashboard-host 127.0.0.1  # Bind host
--dashboard-port 8766       # Port
```

### Auto-Refresh

Dashboard auto-refreshes every 30 seconds. Manually switch tabs to force refresh.

## Tech Stack

- **Backend**: Python stdlib (`http.server`, no Flask/FastAPI)
- **Frontend**: Vanilla JS + Chart.js (single HTML file)
- **Data**: Direct file reads from `artifacts/`
- **Dependencies**: Only Chart.js via CDN

## Security

- **localhost only** (default `--host 127.0.0.1`)
- **Read-only** API (no control endpoints)
- **No authentication** (local machine access)

## Troubleshooting

### Dashboard not loading
```bash
# Check if server is running
curl http://127.0.0.1:8766/api/overview

# Check server logs
tail -f /tmp/dashboard.log
```

### Empty data
- Make sure scheduler has run at least once
- Check `artifacts/daily_alpaca_scheduler/output/` has run directories
- Verify `artifacts/alpaca_executor/lot_ledger.json` exists

### Port already in use
```bash
# Change port
python tools/dashboard_server.py --port 8767

# Or kill existing
taskkill /F /IM python.exe
```

## Future Enhancements

Planned for next iteration:
- [ ] History tab (execution timeline)
- [ ] Logs tab (live log streaming)
- [ ] Settings tab (config display)
- [ ] Server-Sent Events for real-time updates
- [ ] Dark/Light theme toggle
- [ ] Mobile responsive design
- [ ] Historical equity comparison vs SPY

---

**Dashboard URL**: http://127.0.0.1:8766
**Source**: `tools/dashboard_server.py` + `tools/dashboard.html`
