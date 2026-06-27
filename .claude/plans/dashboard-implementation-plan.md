# Live Trading Dashboard - Implementation Plan

## Executive Summary

Create a real-time web dashboard to monitor the daily trading strategy execution, integrated with the daemon scheduler. Users can view portfolio status, position details, factor lot composition, execution history, and performance metrics through an intuitive single-page interface.

## Architecture Design

### Component Stack
```
watch_daily_alpaca_scheduler.ps1 (watchdog)
    ↓
run_daily_alpaca_scheduler.ps1 (launcher)
    ↓
daily_alpaca_scheduler.py (scheduler) ←→ dashboard_server.py (NEW: HTTP server)
    ↓                                          ↓
alpaca_executor.py (executor)              dashboard.html (NEW: SPA)
    ↓
artifacts/ (JSON/CSV outputs)
```

**Integration Point**: Add dashboard server as a child process of `daily_alpaca_scheduler.py`, launched on startup and monitored like the executor.

### Tech Stack (Minimal Dependencies)
- **Backend**: Python `http.server` (stdlib, no Flask/FastAPI)
- **Frontend**: Vanilla JS + Chart.js (single HTML file, no build step)
- **Data**: Direct file reads from `artifacts/` (no database)
- **Auto-refresh**: SSE (Server-Sent Events) for live updates

## Data Sources (Already Available)

### 1. Scheduler State (`artifacts/daily_alpaca_scheduler/state.json`)
```json
{
  "last_decision": {"status": "completed", "started_at": "...", "completed_at": "..."},
  "last_execute": {"status": "completed", "started_at": "...", "completed_at": "..."},
  "next_decision_time": "...",
  "next_execute_time": "..."
}
```

### 2. Execution Summary (`output/YYYYMMDD_HHMMSS/execution_summary.json`)
- Account equity (before/after)
- Decision status, session_idx
- Order counts, submit errors
- Alignment metrics

### 3. Lot Ledger (`artifacts/alpaca_executor/lot_ledger.json`)
- Per-factor lot breakdown
- Min-hold periods
- Entry dates and birth indices
- Meta: last sync, session_idx

### 4. Positions (`output/.../broker_positions_after.csv`)
- Symbol, side, qty, market_value
- Unrealized P&L, avg_entry_price

### 5. Decision Targets (`output/.../decision_targets.csv`)
- Target weights by symbol
- Long/short classification

### 6. Scheduler Logs (`logs/YYYYMMDD_decision.out.log`, etc.)
- Execution logs for debugging

## Dashboard Views (Single-Page App with Tabs)

### Tab 1: Overview (Landing Page)
**Key Metrics Cards**:
- Current Equity: $94,542.10 (+2.3% today)
- Positions: 60 long, 57 short
- Session Index: 42
- Next Decision: Today 12:00 CN (in 2h 15m)
- Next Execute: Today 22:00 CN (in 12h 15m)
- Last Decision: Completed 2h ago
- Last Execute: Completed 14h ago

**Status Timeline** (Visual):
```
[●] 12:00 Decision ──✓──> [●] 22:00 Execute ──✓──> [○] 12:00 Next
     Completed 2h ago         Completed 14h ago         In 2h 15m
```

**Equity Chart** (Line):
- X: Last 30 days
- Y: Portfolio value
- Data: Aggregate from past `execution_summary.json`

### Tab 2: Positions
**Table** (sortable, searchable):
| Symbol | Side | Qty | Market Value | Unrealized P&L | % | Entry Price | Current Price |
|---|---|---|---|---|---|---|---|
| AAOI | Long | 21.88 | $2,969 | -$841 | -22.1% | $174.12 | $135.69 |
| ABBV | Short | -13.97 | -$3,539 | +$562 | +18.9% | $213.03 | $253.35 |

**Filters**:
- Side: All / Long / Short
- P&L: All / Profitable / Losing
- Search by symbol

**Summary Footer**:
- Long Exposure: $47,271 (50.0%)
- Short Exposure: $47,271 (50.0%)
- Net Exposure: $0 (0.0%)
- Total Unrealized P&L: -$1,234 (-1.3%)

### Tab 3: Factor Lots (NEW: Key Feature)
**Lot Composition Pie Chart**:
```
reversal_score: 35% (172 lots)
momentum_score: 15% (73 lots)
small_size_score: 25% (122 lots)
low_beta_score: 15% (73 lots)
cash_quality_score: 10% (49 lots)
```

**Lot Table** (grouped by symbol, expandable):
```
▼ AAOI (3 lots, total 0.08% weight)
  ├─ reversal_score   | 0.0003 | min_hold:  5 | birth_idx: 0 | entry: 2026-06-27
  ├─ momentum_score   | 0.0003 | min_hold: 10 | birth_idx: 0 | entry: 2026-06-27
  └─ small_size_score | 0.0002 | min_hold: 20 | birth_idx: 0 | entry: 2026-06-27
```

**Locked vs Unlocked**:
- Locked lots (within min_hold): 195 lots, 61.3% weight
- Unlocked lots: 123 lots, 38.7% weight

### Tab 4: Execution History
**Table** (last 30 runs):
| Date | Time | Type | Status | Duration | Orders | Errors | Equity After |
|---|---|---|---|---|---|---|---|
| 2026-06-27 | 22:15 | Execute | ✓ Completed | 1m 23s | 0 | 0 | $94,542 |
| 2026-06-27 | 12:03 | Decision | ✓ Completed | 2m 45s | - | 0 | $94,528 |
| 2026-06-26 | 22:18 | Execute | ✓ Completed | 1m 18s | 12 | 0 | $94,102 |

**Detail Modal** (click row):
- Full execution_summary.json
- Logs (tail -100)
- Download artifacts

### Tab 5: Logs (Live Stream)
**Log Viewer** (auto-scroll):
```
[2026-06-27 12:03:45] [Scheduler] Starting decision task for 2026-06-27
[2026-06-27 12:03:46] [Executor] Step 1/3: fetching active us_equity assets...
[2026-06-27 12:04:12] [Executor] Step 2/3: building AlphaCore panel...
[2026-06-27 12:05:58] [Executor] DecisionEngine status: repair
[2026-06-27 12:06:01] [Scheduler] Decision task completed
```

**Filters**:
- Level: All / INFO / WARNING / ERROR
- Source: All / Scheduler / Executor / Watchdog
- Date: Today / Last 3 days / Last 7 days

### Tab 6: Settings (Read-Only Info)
**Configuration Display**:
- Account: ALPACA_US_FULL (Paper)
- Feed: SIP
- Execution Mode: staged_regt
- Target NY Time: 10:00
- Decision Time CN: 12:00
- Execute Time CN: 22:00

**System Info**:
- Python: 3.13.5
- Project Root: W:/实验室项目/us-quant-live
- Watchdog Status: Running (PID 12345)
- Scheduler Status: Running (PID 12346)
- Dashboard Status: Running (PID 12347)

## API Endpoints (REST + SSE)

### REST Endpoints
```
GET /api/overview
  → {equity, positions_count, session_idx, next_times, last_runs}

GET /api/positions
  → [{symbol, side, qty, market_value, unrealized_pl, ...}]

GET /api/lots
  → {composition: {...}, lots: [...], locked_count, unlocked_count}

GET /api/history?limit=30
  → [{date, time, type, status, duration, ...}]

GET /api/logs?lines=100&level=all&source=all
  → {logs: [...]}

GET /api/config
  → {account, feed, execution_mode, ...}
```

### SSE Endpoint
```
GET /api/stream
  → Server-Sent Events stream
  → Pushes updates when:
    - execution_summary.json changes
    - lot_ledger.json changes
    - scheduler state changes
    - new log lines
```

## Implementation Files

### Backend: `tools/dashboard_server.py` (~400 lines)
```python
"""
Live trading dashboard HTTP server.
Serves static dashboard.html and provides REST/SSE API for real-time monitoring.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from watchdog.observers import Observer  # file watcher for auto-refresh
```

**Key Classes**:
- `DashboardHandler(BaseHTTPRequestHandler)`: Handle HTTP requests
- `DataAggregator`: Read and cache artifacts
- `FileWatcher`: Monitor artifacts/ for changes

### Frontend: `tools/dashboard.html` (~800 lines, single file)
```html
<!DOCTYPE html>
<html>
<head>
  <title>Live Trading Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
  <style>/* Embedded CSS */</style>
</head>
<body>
  <!-- Tab navigation -->
  <!-- Tab content containers -->
  <script>/* Vanilla JS, no frameworks */</script>
</body>
</html>
```

**Key Functions**:
- `fetchOverview()`, `fetchPositions()`, `fetchLots()`, etc.
- `renderEquityChart()` using Chart.js
- `connectSSE()` for live updates
- `updateUI()` on data change

### Integration: Modify `tools/daily_alpaca_scheduler.py`
**Add dashboard server as child process**:
```python
def _start_dashboard_server(args):
    """Launch dashboard server as subprocess"""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "dashboard_server.py"),
        "--artifacts-root", str(args.output_root),
        "--host", "127.0.0.1",
        "--port", "8766",
    ]
    return subprocess.Popen(cmd, ...)

# In main():
dashboard_proc = _start_dashboard_server(args)
# Monitor and restart if crashes
```

## User Experience Flow

### Startup
```powershell
PS> powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\watch_daily_alpaca_scheduler.ps1 -Force

[Watchdog] Starting scheduler...
[Scheduler] Starting daily scheduler for 2026-06-27
[Scheduler] Next decision: 2026-06-27 12:00:00 CN
[Scheduler] Next execute: 2026-06-27 22:00:00 CN
[Dashboard] Starting HTTP server at http://127.0.0.1:8766
[Dashboard] Open in browser: http://127.0.0.1:8766
```

### User Opens Browser
1. Navigate to `http://127.0.0.1:8766`
2. See Overview tab with current status
3. SSE connection established → live updates start
4. Click through tabs to explore positions, lots, history

### During Decision Run (12:00 CN)
- Overview: "Decision Running..." spinner
- Logs tab: Live log stream
- SSE push: `{event: "decision_started", data: {...}}`
- After completion: Equity updated, new positions loaded

### During Execute Run (22:00 CN)
- Overview: "Execute Running..." spinner
- Positions tab: Real-time updates as orders fill
- SSE push: `{event: "execute_progress", data: {...}}`
- After completion: P&L updated, history table refreshed

## Technical Decisions & Trade-offs

### Why No Database?
- **Pro**: Zero setup, no migrations, portable
- **Con**: Limited to ~1000 days history (file scan performance)
- **Decision**: Start with files, add SQLite if needed later

### Why Vanilla JS?
- **Pro**: No build step, single HTML file, instant load
- **Con**: More verbose than React/Vue
- **Decision**: 800 lines manageable, simplicity > features

### Why Embedded Server?
- **Pro**: Single command startup, auto-lifecycle with scheduler
- **Con**: Can't access dashboard when scheduler stopped
- **Alternative**: Standalone server (user runs separately)
- **Decision**: Embedded for better UX (dashboard always matches scheduler state)

### Why SSE not WebSocket?
- **Pro**: Simpler (HTTP), auto-reconnect, server-to-client only
- **Con**: No client-to-server push (but we don't need it)
- **Decision**: SSE perfect for read-only monitoring

## File Watchers for Auto-Refresh

Use `watchdog` library (lightweight):
```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class ArtifactsWatcher(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith('execution_summary.json'):
            # Notify SSE clients
            sse_broadcast({'event': 'summary_updated'})
```

Watch:
- `artifacts/alpaca_executor/lot_ledger.json`
- `artifacts/daily_alpaca_scheduler/state.json`
- `artifacts/daily_alpaca_scheduler/output/*/execution_summary.json` (glob)

## Performance Considerations

- **Cache artifacts in memory**: Refresh only on file change
- **Limit history**: Last 30 days only (config file retention)
- **Lazy load logs**: Only fetch on Logs tab open
- **Debounce SSE**: Max 1 update/second

## Security

- **localhost only**: `--host 127.0.0.1` (no external access)
- **Read-only**: No write/control endpoints (monitoring only)
- **No auth needed**: Local machine access only

## Testing Plan

1. **Unit tests**: `test_dashboard_data_aggregator.py`
2. **Manual test**: Start scheduler, open dashboard, verify all tabs
3. **Live test**: Wait for 12:00 decision, verify real-time updates
4. **Error test**: Kill scheduler, verify dashboard shows "disconnected"

## Documentation Updates

Add to `README.md`:
```markdown
## Dashboard

View real-time trading status at http://127.0.0.1:8766 (auto-started with scheduler).

Features:
- Portfolio overview and equity chart
- Real-time positions with P&L
- Factor lot composition and locked status
- Execution history and logs
- Live updates via Server-Sent Events
```

## Implementation Order

1. **Phase 1**: Backend data aggregator (~2h)
   - `dashboard_server.py` skeleton
   - REST endpoints for overview, positions, lots
   - Test with curl/Postman

2. **Phase 2**: Frontend static UI (~3h)
   - `dashboard.html` with all 6 tabs
   - Mock data rendering
   - Chart.js integration

3. **Phase 3**: Backend SSE (~1h)
   - File watcher setup
   - SSE broadcast logic
   - Frontend SSE connection

4. **Phase 4**: Integration (~1h)
   - Modify `daily_alpaca_scheduler.py`
   - Add dashboard as child process
   - Test full lifecycle

5. **Phase 5**: Polish (~1h)
   - Error handling
   - Loading states
   - Documentation

**Total Estimate**: 8 hours development + 2 hours testing = 10 hours

## Dependencies

Add to requirements (if not already present):
```
watchdog==3.0.0  # File system monitoring
```

## Open Questions for User

1. **Port preference**: 8766 OK or prefer 8080/3000?
2. **Standalone mode**: Want option to run dashboard separately from scheduler?
3. **Control features**: Should dashboard allow triggering manual decision/execute? (I recommend read-only first)
4. **Theme**: Dark mode, light mode, or both?
5. **Historical depth**: 30 days enough or need more?

## Success Criteria

✅ User can see current portfolio status at a glance  
✅ User can drill down into positions and factor lots  
✅ User can monitor execution in real-time  
✅ User can review historical runs  
✅ Dashboard auto-updates without refresh  
✅ Zero-config startup (embedded in scheduler)  
✅ Clean, professional UI (inspired by trading platforms)

---

**Ready to proceed with implementation?**
