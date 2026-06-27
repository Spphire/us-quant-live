# Live Trading Dashboard

Real-time web interface for monitoring the daily trading strategy execution.
**Now with full 6-tab UI, SSE live updates, and detailed log viewing.**

## Quick Start

### Automatic Launch (Recommended)

The dashboard automatically starts when you run the daemon:

```powershell
cd W:\实验室项目\us-quant-live
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force
```

Then open browser to: **http://127.0.0.1:8766**

### Manual Launch (Standalone)

```bash
cd W:\实验室项目\us-quant-live
source venv/Scripts/activate

python tools/dashboard_server.py \
  --artifacts-root artifacts/daily_alpaca_scheduler \
  --project-root . \
  --port 8766
```

## Features (Full Version)

### 📊 Overview Tab
- **Portfolio Equity**: Current account value
- **Position Count**: Total + Long/Short breakdown
- **Session Index**: Current trading day counter
- **Task Timeline**: 12:00 Decision and 22:00 Execute status with animated indicators
- **30-Day Equity Curve**: Chart.js line chart with hover tooltips

### 📈 Positions Tab
- **Filter Bar**: Search by symbol, filter by side (Long/Short), filter by P&L (Profitable/Losing)
- **Sortable Table**: Click headers to sort
- **Columns**: Symbol, Side, Qty, Market Value, Entry Price, Current Price, P&L, % Change
- **Color Coding**: Green for profits, Red for losses

### 🎯 Factor Lots Tab (Key Feature)
- **Composition Doughnut Chart**: Visual factor distribution
- **Factor Legend**: Count per factor with color codes
- **Metrics**: Total / Locked / Unlocked lots
- **Detailed Lot Table**: First 200 lots with symbol/factor/weight/min_hold/birth_idx

### 📅 History Tab
- **Last 50 Executions**: Date, session, status, symbols, orders, equity
- **Details Modal**: Click "Details" for full execution_summary.json
- **Status Badges**: Color-coded by ok/error/repair

### 📝 Logs Tab
- **Filter Bar**: Level filter (INFO/WARNING/ERROR), search box
- **Parsed Logs**: Auto-detect timestamp, level, component
- **Multi-source**: Combines scheduler/daemon/watchdog/task logs
- **Auto-scroll**: Scrolls to bottom on update
- **Manual Refresh**: Button to force reload

### ⚙️ Settings Tab
- **Trading Config**: Account, feed, execution mode, schedule times
- **System Info**: Python version, platform, PIDs (self/scheduler/watchdog)
- **Ledger State**: Last session_idx, date, sync time, equity
- **Scheduler Command**: Full command line (if available)

## Real-Time Updates (SSE)

Dashboard uses **Server-Sent Events** for live updates:
- ✅ Detects changes to `lot_ledger.json` within ~1.5 seconds
- ✅ Detects changes to `state.json` (scheduler progress)
- ✅ Detects new `execution_summary.json` files
- ✅ Auto-reconnect on disconnection (5s retry)
- ✅ Connection indicator in header (green=Live, red=Disconnected)
- ✅ Heartbeat every 15s to keep connection alive

When changes detected → current tab data automatically refreshes.

**Polling fallback**: If SSE fails, polls every 30s.

## API Endpoints

```
GET /api/overview      - Account summary + position counts
GET /api/positions     - All active positions with P&L
GET /api/lots          - Factor lots with composition
GET /api/history       - Execution history (?limit=N)
GET /api/logs          - Parsed logs (?lines=N&level=X&source=Y)
GET /api/config        - Trading + system + ledger config
GET /api/stream        - Server-Sent Events for live updates
GET /                  - Dashboard HTML SPA
```

## Architecture

```
┌─────────────────────────────────────────────┐
│  Dashboard Server (Python http.server)       │
│  ├── DataAggregator (reads artifacts)        │
│  ├── REST API (6 endpoints)                  │
│  ├── SSE Stream (file change detection)      │
│  └── Static HTML serve (hot-reload)          │
└─────────────────────────────────────────────┘
              ▲
              │ HTTP/SSE
              ▼
┌─────────────────────────────────────────────┐
│  dashboard.html (Vanilla JS + Chart.js)      │
│  ├── 6 Tabs: Overview / Positions / Lots /   │
│  │           History / Logs / Settings       │
│  ├── SSE Client (auto-reconnect)             │
│  ├── Filter/Search/Sort                      │
│  └── Modal for detail views                  │
└─────────────────────────────────────────────┘
```

## Data Sources
- `artifacts/daily_alpaca_scheduler/state.json` - scheduler state
- `artifacts/daily_alpaca_scheduler/output/*/execution_summary.json` - per-run data
- `artifacts/daily_alpaca_scheduler/output/*/broker_positions_after.csv` - positions
- `artifacts/daily_alpaca_scheduler/logs/*.log` - task logs
- `artifacts/daily_alpaca_scheduler/daemon/*.log` - scheduler logs
- `artifacts/daily_alpaca_scheduler/watchdog/*.log` - watchdog logs
- `artifacts/alpaca_executor/lot_ledger.json` - factor lots

## Configuration

### Scheduler Arguments
```bash
--enable-dashboard          # Launch dashboard (default: enabled)
--no-dashboard              # Disable dashboard
--dashboard-host 127.0.0.1  # Bind host (default: localhost only)
--dashboard-port 8766       # Port (default: 8766)
```

## Tech Stack
- **Backend**: Python stdlib (`http.server`, no Flask/FastAPI)
- **Frontend**: Vanilla JS + Chart.js (via CDN)
- **Real-time**: Server-Sent Events (SSE)
- **Total Size**: ~50KB HTML, ~16KB Python

## Security
- **localhost only** (default `--host 127.0.0.1`)
- **Read-only** API (no control endpoints, view-only)
- **No authentication** (local machine access)

## UI Design
- 🌑 **Dark theme**: Professional trading platform aesthetic
- 🎨 **Color palette**: Deep navy (#0a0e27) base, blue accents (#60a5fa)
- 💚 **Status colors**: Green for profits, Red for losses, Orange for locked
- ⚡ **Smooth animations**: Tab transitions, pulse indicators
- 📱 **Responsive**: Adapts to screen size (charts stack on mobile)

## Troubleshooting

### Dashboard shows "Disconnected"
- Check if server is running: `curl http://127.0.0.1:8766/api/overview`
- Server may have crashed; check `/tmp/dashboard.log`
- Restart: kill process and rerun

### Empty data
- Make sure scheduler has run at least once (or use test data)
- Check `artifacts/daily_alpaca_scheduler/output/` has run directories
- Verify `artifacts/alpaca_executor/lot_ledger.json` exists

### Port already in use
```bash
python tools/dashboard_server.py --port 8767
```

## Verification

**All endpoints verified working**:
```
GET /                  HTTP 200 (~50KB HTML)
GET /api/overview      HTTP 200 (account + counts)
GET /api/positions     HTTP 200 (60 positions)
GET /api/lots          HTTP 200 (449 lots with factors)
GET /api/history       HTTP 200 (execution history)
GET /api/logs          HTTP 200 (parsed log entries)
GET /api/config        HTTP 200 (trading + system + ledger)
GET /api/stream        HTTP 200 (SSE event stream)
```

**SSE verified**: File changes detected within 3 seconds, pushed to client.

---

**Dashboard URL**: http://127.0.0.1:8766
**Source**: `tools/dashboard_server.py` + `tools/dashboard.html`
