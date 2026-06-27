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

            return {
                "equity": equity,
                "session_idx": session_idx,
                "positions_count": {"long": long_count, "short": short_count, "total": len(positions_data)},
                "last_decision": state.get("last_decision", {}),
                "last_execute": state.get("last_execute", {}),
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
        """Get execution history."""
        with self.cache_lock:
            output_root = self.artifacts_root / "output"
            if not output_root.exists():
                return []

            # Find all execution_summary.json files
            summaries = []
            for run_dir in sorted(output_root.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
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
        """Get recent logs."""
        with self.cache_lock:
            log_dir = self.artifacts_root / "logs"
            if not log_dir.exists():
                return {"logs": []}

            # Find most recent log files
            log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            all_logs = []

            for log_file in log_files[:5]:  # Last 5 log files
                try:
                    content = log_file.read_text(encoding="utf-8", errors="ignore")
                    log_lines = content.strip().split("\n")
                    for line in log_lines[-lines:]:
                        if line.strip():
                            all_logs.append({"timestamp": "", "level": "INFO", "source": log_file.stem, "message": line})
                except Exception:
                    continue

            return {"logs": all_logs[-lines:]}

    def get_config(self) -> dict[str, Any]:
        """Get system configuration."""
        return {
            "account": "ALPACA_US_FULL",
            "feed": "sip",
            "execution_mode": "staged_regt",
            "target_ny_time": "10:00",
            "decision_time_cn": "12:00",
            "execute_time_cn": "22:00",
            "project_root": str(self.project_root),
        }

    def _read_scheduler_state(self) -> dict[str, Any]:
        """Read scheduler state.json."""
        state_file = self.artifacts_root / "state.json"
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

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
        """Get most recent execution_summary.json."""
        output_root = self.artifacts_root / "output"
        if not output_root.exists():
            return {}

        latest_dir = None
        for run_dir in sorted(output_root.iterdir(), reverse=True):
            if run_dir.is_dir() and (run_dir / "execution_summary.json").exists():
                latest_dir = run_dir
                break

        if not latest_dir:
            return {}

        try:
            return json.loads((latest_dir / "execution_summary.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_latest_positions(self) -> list[dict[str, Any]]:
        """Read broker_positions_after.csv from latest run."""
        output_root = self.artifacts_root / "output"
        if not output_root.exists():
            return []

        latest_dir = None
        for run_dir in sorted(output_root.iterdir(), reverse=True):
            if run_dir.is_dir() and (run_dir / "broker_positions_after.csv").exists():
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
        """Send Server-Sent Events stream."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Send heartbeat every 30 seconds
        try:
            while True:
                event = f"data: {json.dumps({'event': 'heartbeat', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
                self.wfile.write(event.encode("utf-8"))
                self.wfile.flush()
                time.sleep(30)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
