"""
Live Trading Dashboard HTTP Server

Serves a real-time web interface for monitoring daily trading strategy execution.
Provides REST API and Server-Sent Events for live updates.

Usage:
    python tools/dashboard_server.py --artifacts-root artifacts/daily_alpaca_scheduler

Typically launched automatically by daily_alpaca_scheduler.py.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse


class DataAggregator:
    """Aggregate and cache data from artifacts directory."""

    def __init__(self, artifacts_root: Path, project_root: Path):
        self.artifacts_root = artifacts_root
        self.project_root = project_root
        self.cache: dict[str, Any] = {}
        self.cache_lock = Lock()
        self._last_refresh = 0.0

    def get_overview(self) -> dict[str, Any]:
        """Get overview metrics."""
        with self.cache_lock:
            state = self._read_scheduler_state()
            latest_summary = self._get_latest_execution_summary()
            lot_ledger = self._read_lot_ledger()

            equity = latest_summary.get("account_equity_post_trade", 0.0)
            session_idx = lot_ledger.get("meta", {}).get("last_session_idx", 0)

            positions_data = self._read_latest_positions()
            long_count = sum(1 for p in positions_data if p.get("side") == "long")
            short_count = sum(1 for p in positions_data if p.get("side") == "short")

            # Extract the latest session's decision/execute status from the
            # scheduler's sessions-keyed state.json.
            latest_date, decision_task, execute_task = self._latest_session_tasks(state)

            return {
                "equity": equity,
                "session_idx": session_idx,
                "positions_count": {"long": long_count, "short": short_count, "total": len(positions_data)},
                "session_date": latest_date,
                "last_decision": decision_task,
                "last_execute": execute_task,
                "next_decision_time": state.get("next_decision_time"),
                "next_execute_time": state.get("next_execute_time"),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

    def get_positions(self) -> list[dict[str, Any]]:
        """Get current positions."""
        with self.cache_lock:
            return self._read_latest_positions()

    def get_lots(self) -> dict[str, Any]:
        """Get factor lot breakdown."""
        with self.cache_lock:
            lot_ledger = self._read_lot_ledger()
            long_lots = lot_ledger.get("ledger", {}).get("long", [])
            short_lots = lot_ledger.get("ledger", {}).get("short", [])

            # Count by factor
            factor_counts: dict[str, int] = {}
            factor_weights: dict[str, float] = {}
            for lot in long_lots + short_lots:
                factor = lot.get("factor", "unknown")
                factor_counts[factor] = factor_counts.get(factor, 0) + 1
                factor_weights[factor] = factor_weights.get(factor, 0.0) + float(lot.get("weight", 0.0))

            # Count locked vs unlocked
            session_idx = lot_ledger.get("meta", {}).get("last_session_idx", 0)
            locked = sum(1 for lot in long_lots + short_lots if self._is_locked(lot, session_idx))
            unlocked = len(long_lots) + len(short_lots) - locked

            return {
                "composition": {"counts": factor_counts, "weights": factor_weights},
                "lots": long_lots + short_lots,
                "locked_count": locked,
                "unlocked_count": unlocked,
                "total_count": len(long_lots) + len(short_lots),
            }

    def get_history(self, limit: int = 30) -> list[dict[str, Any]]:
        """Get execution history across all run directories."""
        with self.cache_lock:
            summaries = []
            for run_dir in self._iter_run_dirs():
                summary_file = run_dir / "execution_summary.json"
                if not summary_file.exists():
                    continue
                try:
                    summary = json.loads(summary_file.read_text(encoding="utf-8"))
                    summary["run_dir"] = run_dir.name
                    summaries.append(summary)
                    if len(summaries) >= limit:
                        break
                except Exception:
                    continue

            return summaries

    def get_logs(self, lines: int = 100, level: str = "all", source: str = "all") -> dict[str, Any]:
        """Get recent logs with parsed timestamp/level/source."""
        import re
        with self.cache_lock:
            log_dir = self.artifacts_root / "logs"
            daemon_dir = self.artifacts_root / "daemon"
            watchdog_dir = self.artifacts_root / "watchdog"

            log_files: list[Path] = []
            for d in (log_dir, daemon_dir, watchdog_dir):
                if d.exists():
                    log_files.extend(d.glob("*.log"))
            log_files = sorted(log_files, key=lambda p: p.stat().st_mtime, reverse=True)

            # Pattern: [YYYY-MM-DD HH:MM:SS] [Component] message
            #   or:   [Component] message
            ts_pattern = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[^\]]*)\]\s*(.*)$")
            tag_pattern = re.compile(r"^\[([A-Za-z]+)\]\s*(.*)$")
            level_keywords = {
                "ERROR": ["error", "exception", "traceback", "failed", "fatal"],
                "WARNING": ["warning", "warn"],
            }

            def detect_level(text: str) -> str:
                lower = text.lower()
                for lvl, kws in level_keywords.items():
                    if any(kw in lower for kw in kws):
                        return lvl
                return "INFO"

            all_logs: list[dict[str, Any]] = []
            for log_file in log_files[:8]:
                try:
                    # Read last ~10KB only (avoid huge file scans)
                    file_size = log_file.stat().st_size
                    read_bytes = min(file_size, 200_000)
                    with open(log_file, "rb") as f:
                        f.seek(file_size - read_bytes)
                        content = f.read().decode("utf-8", errors="ignore")
                    log_lines = content.strip().split("\n")
                    file_source = log_file.parent.name + "/" + log_file.stem
                    for line in log_lines:
                        line = line.strip()
                        if not line:
                            continue
                        ts = ""
                        msg = line
                        component = file_source
                        m = ts_pattern.match(line)
                        if m:
                            ts = m.group(1)
                            msg = m.group(2)
                        tm = tag_pattern.match(msg)
                        if tm:
                            component = tm.group(1)
                            msg = tm.group(2)
                        lvl = detect_level(msg)
                        # Apply filters
                        if level != "all" and lvl.lower() != level.lower():
                            continue
                        if source != "all" and source.lower() not in component.lower() and source.lower() not in file_source.lower():
                            continue
                        all_logs.append(
                            {
                                "timestamp": ts,
                                "level": lvl,
                                "source": component,
                                "file": file_source,
                                "message": msg,
                            }
                        )
                except Exception:
                    continue

            return {"logs": all_logs[-lines:], "total_files": len(log_files)}

    def get_config(self) -> dict[str, Any]:
        """Get system configuration and live status."""
        import os
        import sys
        import platform
        lot_ledger = self._read_lot_ledger()
        meta = lot_ledger.get("meta", {})

        # Try to read scheduler command for actual config
        scheduler_cmd_file = self.artifacts_root / "daemon" / "scheduler.command.txt"
        scheduler_cmd = ""
        if scheduler_cmd_file.exists():
            try:
                scheduler_cmd = scheduler_cmd_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        # Detect PIDs
        scheduler_pid_file = self.artifacts_root / "daemon" / "scheduler.pid"
        watchdog_pid_file = self.artifacts_root / "watchdog" / "watchdog.pid"

        def read_pid(path: Path) -> int | None:
            if not path.exists():
                return None
            try:
                return int(path.read_text().strip())
            except Exception:
                return None

        scheduler_pid = read_pid(scheduler_pid_file)
        watchdog_pid = read_pid(watchdog_pid_file)

        return {
            "trading": {
                "account": "ALPACA_US_FULL",
                "feed": "sip",
                "execution_mode": "staged_regt",
                "target_ny_time": "10:00",
                "decision_time_cn": "12:00",
                "execute_time_cn": "22:00",
            },
            "system": {
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "platform": platform.system() + " " + platform.release(),
                "project_root": str(self.project_root),
                "pid_self": os.getpid(),
                "pid_scheduler": scheduler_pid,
                "pid_watchdog": watchdog_pid,
            },
            "ledger": {
                "last_session_idx": meta.get("last_session_idx"),
                "last_session_date": meta.get("last_session_date"),
                "last_sync_at_utc": meta.get("executor_last_sync_at_utc"),
                "last_broker_equity": meta.get("executor_last_broker_equity"),
                "last_sync_applied": meta.get("executor_last_sync_applied"),
            },
            "scheduler_command": scheduler_cmd,
        }

    def get_artifact_mtimes(self) -> dict[str, float]:
        """Return mtimes of key artifact files for change detection (SSE)."""
        files = {
            "lot_ledger": self.project_root / "artifacts" / "alpaca_executor" / "lot_ledger.json",
            "state": self.artifacts_root / "state.json",
        }
        # Also include the most recent execution_summary across all run directories
        run_dirs = self._iter_run_dirs()
        for run_dir in run_dirs:
            summary = run_dir / "execution_summary.json"
            if summary.exists():
                files["latest_summary"] = summary
                break

        mtimes: dict[str, float] = {}
        for key, path in files.items():
            try:
                if path.exists():
                    mtimes[key] = path.stat().st_mtime
            except Exception:
                pass
        return mtimes

    def _iter_run_dirs(self) -> list[Path]:
        """Enumerate all scheduler run output directories, newest first.

        The scheduler writes per-session output to:
            artifacts/daily_alpaca_scheduler/<YYYYMMDD>_decision/
            artifacts/daily_alpaca_scheduler/<YYYYMMDD>_execute/
        Older/test data may also live under:
            artifacts/daily_alpaca_scheduler/output/<run>/
        We collect both so the dashboard works regardless of layout, sorted by
        directory mtime (newest first) so "latest" picks the most recent run.
        """
        run_dirs: list[Path] = []
        # New layout: <date>_decision / <date>_execute directly under artifacts_root
        if self.artifacts_root.exists():
            for d in self.artifacts_root.iterdir():
                if d.is_dir() and (d.name.endswith("_decision") or d.name.endswith("_execute")):
                    run_dirs.append(d)
        # Legacy layout: output/<run>/
        output_root = self.artifacts_root / "output"
        if output_root.exists():
            for d in output_root.iterdir():
                if d.is_dir():
                    run_dirs.append(d)
        # Newest first by mtime
        run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return run_dirs

    def _read_scheduler_state(self) -> dict[str, Any]:
        """Read scheduler state.json."""
        state_file = self.artifacts_root / "state.json"
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _latest_session_tasks(self, state: dict[str, Any]) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
        """Extract the latest session's decision/execute task records from state.

        state.json format:
            {"version": 1, "sessions": {"2026-06-29": {"decision": {...}, "execute": {...}}}}
        Returns (latest_session_date, decision_task, execute_task). Missing pieces
        come back as empty dicts.
        """
        sessions = state.get("sessions", {})
        if not isinstance(sessions, dict) or not sessions:
            return None, {}, {}
        # Session keys are ISO dates (YYYY-MM-DD); lexicographic sort == chronological
        latest_date = max(sessions.keys())
        session = sessions.get(latest_date, {}) or {}
        decision = session.get("decision", {}) or {}
        execute = session.get("execute", {}) or {}
        return latest_date, decision, execute

    def _read_lot_ledger(self) -> dict[str, Any]:
        """Read lot_ledger.json."""
        ledger_file = self.project_root / "artifacts" / "alpaca_executor" / "lot_ledger.json"
        if not ledger_file.exists():
            return {"ledger": {"long": [], "short": []}, "meta": {}}
        try:
            return json.loads(ledger_file.read_text(encoding="utf-8"))
        except Exception:
            return {"ledger": {"long": [], "short": []}, "meta": {}}

    def _get_latest_execution_summary(self) -> dict[str, Any]:
        """Get most recent execution_summary.json across all run directories."""
        for run_dir in self._iter_run_dirs():
            summary_file = run_dir / "execution_summary.json"
            if summary_file.exists():
                try:
                    return json.loads(summary_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
        return {}

    def _read_latest_positions(self) -> list[dict[str, Any]]:
        """Read broker_positions_after.csv from the most recent run directory."""
        latest_dir = None
        for run_dir in self._iter_run_dirs():
            if (run_dir / "broker_positions_after.csv").exists():
                latest_dir = run_dir
                break

        if not latest_dir:
            return []

        try:
            import csv

            positions = []
            with open(latest_dir / "broker_positions_after.csv", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    positions.append(
                        {
                            "symbol": row.get("symbol", ""),
                            "side": row.get("side", ""),
                            "qty": float(row.get("qty", 0)),
                            "signed_qty": float(row.get("signed_qty", 0)),
                            "current_price": float(row.get("current_price", 0)),
                            "market_value": float(row.get("market_value", 0)),
                            "avg_entry_price": float(row.get("avg_entry_price", 0)),
                        }
                    )
            return positions
        except Exception:
            return []

    @staticmethod
    def _is_locked(lot: dict[str, Any], current_session_idx: int) -> bool:
        """Check if lot is locked (within min_hold period)."""
        birth_idx = int(lot.get("birth_idx", 0))
        min_hold = int(lot.get("min_hold", 0))
        return (current_session_idx - birth_idx) < min_hold


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for dashboard."""

    aggregator: DataAggregator
    html_path: Path

    def _load_html(self) -> str:
        """Load dashboard HTML from disk (read on each request for hot-reload)."""
        if self.html_path.exists():
            return self.html_path.read_text(encoding="utf-8")
        return "<html><body><h1>Dashboard HTML not found</h1></body></html>"

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # API endpoints
        if path == "/api/overview":
            self._send_json(self.aggregator.get_overview())
        elif path == "/api/positions":
            self._send_json(self.aggregator.get_positions())
        elif path == "/api/lots":
            self._send_json(self.aggregator.get_lots())
        elif path == "/api/history":
            limit = int(query.get("limit", ["30"])[0])
            self._send_json(self.aggregator.get_history(limit))
        elif path == "/api/logs":
            lines = int(query.get("lines", ["100"])[0])
            level = query.get("level", ["all"])[0]
            source = query.get("source", ["all"])[0]
            self._send_json(self.aggregator.get_logs(lines, level, source))
        elif path == "/api/config":
            self._send_json(self.aggregator.get_config())
        elif path == "/api/stream":
            self._send_sse()
        elif path == "/" or path == "/index.html":
            self._send_html(self._load_html())
        else:
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _send_json(self, data: Any) -> None:
        """Send JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        """Send HTML response."""
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self) -> None:
        """Send Server-Sent Events stream with file change detection."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_mtimes = self.aggregator.get_artifact_mtimes()
        last_heartbeat = time.time()
        heartbeat_interval = 15.0  # send heartbeat every 15s to keep connection alive
        poll_interval = 1.5  # check for file changes every 1.5s

        # Send initial snapshot event so client knows connection is live
        try:
            initial = json.dumps({"event": "connected", "mtimes": last_mtimes, "timestamp": datetime.utcnow().isoformat()})
            self.wfile.write(f"data: {initial}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        try:
            while True:
                time.sleep(poll_interval)
                now = time.time()
                current = self.aggregator.get_artifact_mtimes()
                changed_keys = [k for k, v in current.items() if last_mtimes.get(k) != v]

                if changed_keys:
                    payload = json.dumps({"event": "artifacts_changed", "changed": changed_keys, "timestamp": datetime.utcnow().isoformat()})
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_mtimes = current
                    last_heartbeat = now
                elif now - last_heartbeat >= heartbeat_interval:
                    hb = json.dumps({"event": "heartbeat", "timestamp": datetime.utcnow().isoformat()})
                    self.wfile.write(f"data: {hb}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_heartbeat = now
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        """Send error response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode("utf-8"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        """Suppress default logging."""
        return


def load_dashboard_html(tools_dir: Path) -> str:
    """Load dashboard.html from tools directory."""
    html_path = tools_dir / "dashboard.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<html><body><h1>Dashboard HTML not found</h1></body></html>"


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Live trading dashboard HTTP server")
    parser.add_argument("--artifacts-root", required=True, help="Scheduler artifacts root directory")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8766, help="Server port")
    args = parser.parse_args(argv)

    artifacts_root = Path(args.artifacts_root).resolve()
    project_root = Path(args.project_root).resolve()
    tools_dir = project_root / "tools"
    html_path = tools_dir / "dashboard.html"

    # Initialize data aggregator
    aggregator = DataAggregator(artifacts_root, project_root)

    # Set class attributes for handler (HTML loaded on each request for hot-reload)
    DashboardHandler.aggregator = aggregator
    DashboardHandler.html_path = html_path

    # Start server
    server = ThreadingHTTPServer((str(args.host), int(args.port)), DashboardHandler)
    print(f"[Dashboard] Server started at http://{args.host}:{args.port}", flush=True)
    print(f"[Dashboard] Artifacts: {artifacts_root}", flush=True)
    print(f"[Dashboard] HTML: {html_path} (exists: {html_path.exists()})", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Dashboard] Server stopped", flush=True)
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
