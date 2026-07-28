# Current Project Mapping

Use this repository's Alpaca path as a working adapter example, not as a universal API design.

| Concern | Current implementation |
| --- | --- |
| Executor | `src/alpaca_executor.py` |
| Target projection | `src/executable_target_projector.py` |
| Broker clients | `src/vendors/` |
| Scheduler | `tools/daily_alpaca_scheduler.py` |
| Isolated experiment | `tools/parallel_execution_experiment.py` |
| Execution timeline backend | `tools/dashboard_server.py` |
| Execution timeline frontend | `tools/dashboard.html` |
| Execution regression suite | `tools/test_alpaca_execution_gap_fixes.py` |
| Timeline regression suite | `tools/test_dashboard_execution_timeline.py` |
| Restart entry point | `Start.bat` |
| Production artifacts | `artifacts/daily_alpaca_scheduler/` |
| Experiment artifacts | `artifacts/parallel_execution_experiment/` |

## Reusable Alpaca Findings

- Six concurrent workers produced long queue waits.
- Ten workers reduced the representative execution window from about 126 seconds to 95 seconds while preserving 100% fills, zero 429 responses, executable-to-actual L1 near 0.00185, and gross utilization near 94.97%.
- Fourteen workers crossed the API boundary: 30 rate-limit responses, five unfilled orders, and materially worse target error.
- The executor therefore defaults to ten and independently caps requested execution concurrency at ten.
- Exact fractional long closes can be rejected at the zero boundary. The narrow fallback retries one minimum unit below available quantity and records the residual.

Treat these values as Alpaca-paper evidence, not defaults for another platform. Repeat the boundary experiment after implementing a new adapter.

## Existing Verification Commands

```powershell
.\venv\Scripts\python.exe tools\test_alpaca_execution_gap_fixes.py
.\venv\Scripts\python.exe tools\test_dashboard_execution_timeline.py
.\venv\Scripts\python.exe tools\test_dashboard_account_epoch.py
.\venv\Scripts\python.exe -m py_compile src\alpaca_executor.py tools\daily_alpaca_scheduler.py tools\dashboard_server.py
```

Do not run `tools/test_tray_launcher_e2e.py` during live operation. Before `Start.bat`, verify no decision, execution, or experiment process is active.

## Migration Sequence

1. Add the new broker client under `src/vendors/` with the adapter contract.
2. Add profile configuration without committing secrets.
3. Implement an isolated experiment runner with immutable test/production ID checks.
4. Normalize artifacts to the canonical schema.
5. Reuse the target projector and staged execution semantics where the platform supports them.
6. Add platform-specific adapter tests and generic execution tests.
7. Run baseline and one-variable boundary probes on paper/sandbox.
8. Add new platform defaults and safety caps only after evidence passes the gates.

