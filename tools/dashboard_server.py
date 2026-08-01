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
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from tools.daily_pnl_attribution import build_daily_side_pnl_attribution
except ModuleNotFoundError:
    from daily_pnl_attribution import build_daily_side_pnl_attribution


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
            account_state = self._read_account_state()
            account_epoch = self._account_epoch(account_state)
            reset_pending = self._account_reset_pending(account_epoch, latest_summary)
            audit_rollup = self._read_audit_rollup()
            latest_audit = self._latest_audit_row(audit_rollup)

            equity = (
                account_epoch.get("initial_equity", 0.0)
                if reset_pending
                else latest_summary.get("account_equity_post_trade", 0.0)
            )
            session_idx = account_state.get("last_session_idx", 0)

            positions_data = [] if reset_pending else self._read_latest_positions()
            long_count = sum(1 for p in positions_data if p.get("side") == "long")
            short_count = sum(1 for p in positions_data if p.get("side") == "short")

            # Extract the latest session's decision/execute status from the
            # scheduler's sessions-keyed state.json.
            latest_date, decision_task, execute_task = self._latest_session_tasks(state)
            if reset_pending and str(latest_date or "") < str(account_epoch.get("effective_session") or ""):
                decision_task = {}
                execute_task = {}

            display_session_date = (
                str(account_epoch.get("effective_session") or latest_date)
                if reset_pending
                else latest_date
            )
            return {
                "equity": equity,
                "session_idx": session_idx,
                "positions_count": {"long": long_count, "short": short_count, "total": len(positions_data)},
                "session_date": display_session_date,
                "account_epoch": {**account_epoch, "reset_pending": reset_pending},
                "last_decision": decision_task,
                "last_execute": execute_task,
                "audit": {
                    "latest": latest_audit,
                    "status_counts": audit_rollup.get("audit_status_counts", {}),
                    "trading_day_count": audit_rollup.get("trading_day_count"),
                    "first_session_date": audit_rollup.get("first_session_date"),
                    "last_session_date": audit_rollup.get("last_session_date"),
                    "official_calendar_gaps": audit_rollup.get("official_calendar_gaps", {}),
                    "large_calendar_gaps": audit_rollup.get("large_calendar_gaps", []),
                    "strict_attribution_ready_days": audit_rollup.get("strict_attribution_ready_days"),
                    "strict_account_position_replay_ready_days": audit_rollup.get(
                        "strict_account_position_replay_ready_days"
                    ),
                    "totals": audit_rollup.get("totals", {}),
                },
                "next_decision_time": state.get("next_decision_time"),
                "next_execute_time": state.get("next_execute_time"),
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

    def get_positions(self) -> list[dict[str, Any]]:
        """Get current positions."""
        with self.cache_lock:
            account_epoch = self._account_epoch(self._read_account_state())
            if self._account_reset_pending(account_epoch, self._get_latest_execution_summary()):
                return []
            return self._read_latest_positions()

    def get_history(self, limit: int = 30) -> list[dict[str, Any]]:
        """Get execution history across all run directories."""
        with self.cache_lock:
            account_state = self._read_account_state()
            account_epoch = self._account_epoch(account_state)
            summaries = []
            if account_epoch.get("effective_session"):
                summaries.append(
                    {
                        "run_dir": f"account_reset_{str(account_epoch.get('effective_session')).replace('-', '')}",
                        "run_type": "account_reset",
                        "session_date": account_epoch.get("effective_session"),
                        "decision_date": account_epoch.get("effective_session"),
                        "decision_status": "account_reset",
                        "ok": True,
                        "submitted": False,
                        "dynamic_symbols": 0,
                        "order_plan_count": 0,
                        "account_equity_post_trade": account_epoch.get("initial_equity", 0.0),
                        "capital_epoch": account_epoch.get("capital_epoch", 1),
                        "audit_status": "not_applicable",
                        "strict_attribution_status": "not_applicable",
                        "startup_binding_status": "not_applicable",
                        "run_failure_status": "pass",
                        "run_failure_class": "account_reset_boundary",
                    }
                )
            audit_rows = self._audit_rows_by_session_date()
            for run_dir in self._iter_run_dirs():
                if len(summaries) >= limit:
                    break
                summary_file = run_dir / "execution_summary.json"
                if not summary_file.exists():
                    continue
                try:
                    summary = json.loads(summary_file.read_text(encoding="utf-8"))
                    summary["run_dir"] = run_dir.name
                    run_type = (
                        "execute"
                        if run_dir.name.endswith("_execute")
                        else "decision"
                        if run_dir.name.endswith("_decision")
                        else "remediation"
                        if run_dir.name.endswith("_emergency_flatten")
                        else ""
                    )
                    summary["run_type"] = run_type
                    session_date = self._run_dir_session_date(run_dir, summary)
                    summary["session_date"] = session_date
                    summary["decision_date"] = session_date or summary.get("decision_date") or run_dir.name[:8]
                    summary["capital_epoch"] = self._capital_epoch_for_session(
                        account_epoch,
                        session_date,
                    )
                    if run_type == "execute":
                        summary.update(self._read_side_return_snapshot(run_dir))
                    if run_type == "execute":
                        audit = audit_rows.get(session_date) or audit_rows.get(self._compact_date_to_iso(session_date)) or {}
                    else:
                        audit = self._read_lightweight_run_audit(run_dir)
                    summary["audit"] = audit
                    summary["audit_status"] = audit.get("audit_status", "not_applicable")
                    summary["audit_issues"] = audit.get("audit_issues", 0)
                    summary["strict_attribution_status"] = (
                        audit.get("strict_attribution_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["strict_attribution_blocking_items"] = (
                        audit.get("strict_attribution_blocking_items") if run_type == "execute" else 0
                    )
                    summary["position_snapshot_integrity_status"] = (
                        audit.get("position_snapshot_integrity_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["residual_diagnosis_status"] = (
                        audit.get("residual_diagnosis_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["market_price_status"] = audit.get("market_price_status") if run_type == "execute" else "not_applicable"
                    summary["market_price_execution_relevant_symbols"] = (
                        audit.get("market_price_execution_relevant_symbols") if run_type == "execute" else 0
                    )
                    summary["market_price_missing_reference_symbols"] = (
                        audit.get("market_price_missing_reference_symbols") if run_type == "execute" else 0
                    )
                    summary["market_price_context_missing_reference_symbols"] = (
                        audit.get("market_price_context_missing_reference_symbols") if run_type == "execute" else 0
                    )
                    summary["market_price_fallback_only_symbols"] = (
                        audit.get("market_price_fallback_only_symbols") if run_type == "execute" else 0
                    )
                    summary["market_price_large_reference_moves"] = (
                        audit.get("market_price_large_reference_moves") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_status"] = (
                        audit.get("intraday_bar_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["intraday_bar_execution_relevant_symbols"] = (
                        audit.get("intraday_bar_execution_relevant_symbols") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_missing_symbols"] = (
                        audit.get("intraday_bar_missing_symbols") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_context_missing_symbols"] = (
                        audit.get("intraday_bar_context_missing_symbols") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_filled_symbols_missing"] = (
                        audit.get("intraday_bar_filled_symbols_missing") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_error_count"] = (
                        audit.get("intraday_bar_error_count") if run_type == "execute" else 0
                    )
                    summary["intraday_bar_context_error_count"] = (
                        audit.get("intraday_bar_context_error_count") if run_type == "execute" else 0
                    )
                    summary["quote_status"] = audit.get("quote_status") if run_type == "execute" else "not_applicable"
                    summary["quote_execution_relevant_symbols"] = (
                        audit.get("quote_execution_relevant_symbols") if run_type == "execute" else 0
                    )
                    summary["quote_missing_symbols"] = (
                        audit.get("quote_missing_symbols") if run_type == "execute" else 0
                    )
                    summary["quote_context_missing_symbols"] = (
                        audit.get("quote_context_missing_symbols") if run_type == "execute" else 0
                    )
                    summary["quote_invalid_symbols"] = (
                        audit.get("quote_invalid_symbols") if run_type == "execute" else 0
                    )
                    summary["quote_wide_spread_symbols"] = (
                        audit.get("quote_wide_spread_symbols") if run_type == "execute" else 0
                    )
                    summary["quote_wide_spread_threshold_bps"] = (
                        audit.get("quote_wide_spread_threshold_bps") if run_type == "execute" else 0
                    )
                    summary["quote_error_count"] = audit.get("quote_error_count") if run_type == "execute" else 0
                    summary["quote_context_error_count"] = (
                        audit.get("quote_context_error_count") if run_type == "execute" else 0
                    )
                    summary["account_activity_status"] = (
                        audit.get("account_activity_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["decision_intent_status"] = (
                        audit.get("decision_intent_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["decision_intent_unexplained_no_order_symbols"] = (
                        audit.get("decision_intent_unexplained_no_order_symbols") if run_type == "execute" else 0
                    )
                    summary["decision_intent_below_min_trade_symbols"] = (
                        audit.get("decision_intent_below_min_trade_symbols") if run_type == "execute" else 0
                    )
                    summary["decision_intent_min_trade_boundary_symbols"] = (
                        audit.get("decision_intent_min_trade_boundary_symbols") if run_type == "execute" else 0
                    )
                    summary["account_activity_capture_ok"] = (
                        audit.get("account_activity_capture_ok") if run_type == "execute" else None
                    )
                    summary["account_activity_window_semantics"] = (
                        audit.get("account_activity_window_semantics") if run_type == "execute" else ""
                    )
                    summary["account_activity_unknown_net_amount"] = (
                        audit.get("account_activity_unknown_net_amount") if run_type == "execute" else 0
                    )
                    summary["account_activity_non_trade_equity_impact"] = (
                        audit.get("account_activity_non_trade_equity_impact") if run_type == "execute" else 0
                    )
                    summary["corporate_action_status"] = (
                        audit.get("corporate_action_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["corporate_action_rows"] = audit.get("corporate_action_rows") if run_type == "execute" else 0
                    summary["corporate_action_matched_position_residual_symbols"] = (
                        audit.get("corporate_action_matched_position_residual_symbols") if run_type == "execute" else 0
                    )
                    summary["portfolio_history_status"] = (
                        audit.get("portfolio_history_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["portfolio_history_rows"] = audit.get("portfolio_history_rows") if run_type == "execute" else 0
                    summary["portfolio_history_summary_vs_after_delta"] = (
                        audit.get("portfolio_history_summary_vs_after_delta") if run_type == "execute" else 0
                    )
                    summary["portfolio_history_timeframe"] = (
                        audit.get("portfolio_history_timeframe") if run_type == "execute" else ""
                    )
                    summary["portfolio_history_sample_age_seconds"] = (
                        audit.get("portfolio_history_sample_age_seconds") if run_type == "execute" else None
                    )
                    summary["calendar_status"] = (
                        audit.get("calendar_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["calendar_session_date_in_calendar"] = (
                        audit.get("calendar_session_date_in_calendar") if run_type == "execute" else False
                    )
                    summary["calendar_expected_previous_trading_date"] = (
                        audit.get("calendar_expected_previous_trading_date") if run_type == "execute" else ""
                    )
                    summary["calendar_expected_next_trading_date"] = (
                        audit.get("calendar_expected_next_trading_date") if run_type == "execute" else ""
                    )
                    summary["calendar_session_is_half_day"] = (
                        audit.get("calendar_session_is_half_day") if run_type == "execute" else False
                    )
                    summary["account_state_bridge_status"] = (
                        audit.get("account_state_bridge_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["account_state_window_semantics"] = (
                        audit.get("account_state_window_semantics") if run_type == "execute" else ""
                    )
                    summary["account_state_window_start_source"] = (
                        audit.get("account_state_window_start_source") if run_type == "execute" else ""
                    )
                    summary["account_state_preflight_to_sizing_equity_delta"] = (
                        audit.get("account_state_preflight_to_sizing_equity_delta") if run_type == "execute" else 0
                    )
                    summary["account_state_equity_delta"] = (
                        audit.get("account_state_equity_delta") if run_type == "execute" else 0
                    )
                    summary["account_state_cash_delta"] = (
                        audit.get("account_state_cash_delta") if run_type == "execute" else 0
                    )
                    summary["account_state_gross_exposure_delta"] = (
                        audit.get("account_state_gross_exposure_delta") if run_type == "execute" else 0
                    )
                    summary["account_state_equity_delta_vs_summary_delta"] = (
                        audit.get("account_state_equity_delta_vs_summary_delta") if run_type == "execute" else 0
                    )
                    summary["market_context_status"] = (
                        audit.get("market_context_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["market_context_snapshot_plus_realized_pnl"] = (
                        audit.get("market_context_snapshot_plus_realized_pnl") if run_type == "execute" else 0
                    )
                    summary["market_context_pnl_window_semantics"] = (
                        audit.get("market_context_pnl_window_semantics") if run_type == "execute" else ""
                    )
                    summary["market_context_net_beta_exposure_to_gross"] = (
                        audit.get("market_context_net_beta_exposure_to_gross") if run_type == "execute" else None
                    )
                    summary["market_context_benchmark_symbols_with_bars"] = (
                        audit.get("market_context_benchmark_symbols_with_bars") if run_type == "execute" else 0
                    )
                    summary["attribution_dossier_status"] = (
                        audit.get("attribution_dossier_status") if run_type == "execute" else "not_applicable"
                    )
                    summary["attribution_focus_symbol_count"] = (
                        audit.get("attribution_focus_symbol_count") if run_type == "execute" else 0
                    )
                    summary["attribution_evidence_gap_count"] = (
                        audit.get("attribution_evidence_gap_count") if run_type == "execute" else 0
                    )
                    summary["attribution_symbol_evidence_gap_item_count"] = (
                        audit.get("attribution_symbol_evidence_gap_item_count") if run_type == "execute" else 0
                    )
                    summary["attribution_coverage_area_gap_count"] = (
                        audit.get("attribution_coverage_area_gap_count") if run_type == "execute" else 0
                    )
                    summary["attribution_primary_bucket_counts"] = (
                        audit.get("attribution_primary_bucket_counts") if run_type == "execute" else ""
                    )
                    summary["run_evidence_digest_status"] = (
                        audit.get("run_evidence_digest_status", "not_applicable")
                    )
                    summary["run_evidence_digest_missing_files"] = (
                        audit.get("run_evidence_digest_missing_files", 0)
                    )
                    summary["run_evidence_digest_strict_missing_files"] = (
                        audit.get("run_evidence_digest_strict_missing_files", 0)
                    )
                    summary["run_evidence_digest_run_event_count"] = (
                        audit.get("run_evidence_digest_run_event_count", 0)
                    )
                    summary["run_evidence_digest_hash_manifest_file_count"] = (
                        audit.get("run_evidence_digest_hash_manifest_file_count", 0)
                    )
                    summary["run_evidence_digest_artifact_completeness_status"] = (
                        audit.get("run_evidence_digest_artifact_completeness_status", "")
                    )
                    summary["run_evidence_digest_artifact_completeness_partial_category_count"] = (
                        audit.get("run_evidence_digest_artifact_completeness_partial_category_count", 0)
                    )
                    summary["run_evidence_digest_resolved_from_paired_decision_count"] = (
                        audit.get("run_evidence_digest_resolved_from_paired_decision_count", 0)
                    )
                    summary["startup_binding_status"] = (
                        audit.get("startup_binding_status", "not_applicable")
                    )
                    summary["startup_binding_issue_count"] = (
                        audit.get("startup_binding_issue_count", 0)
                    )
                    summary["startup_autostart_registered"] = (
                        audit.get("startup_autostart_registered", False)
                    )
                    summary["startup_process_health_status"] = (
                        audit.get("startup_process_health_status", "not_applicable")
                    )
                    summary["run_failure_status"] = (
                        audit.get("run_failure_status", "not_applicable")
                    )
                    summary["run_failure_task_status"] = (
                        audit.get("run_failure_task_status", "")
                    )
                    summary["run_failure_class"] = (
                        audit.get("run_failure_class", "")
                    )
                    summary["run_failure_error_type"] = (
                        audit.get("run_failure_error_type", "")
                    )
                    summaries.append(summary)
                    if len(summaries) >= limit:
                        break
                except Exception:
                    continue

            return summaries

    def get_audit(self) -> dict[str, Any]:
        """Get daily audit rollup and latest detailed audit summaries."""
        with self.cache_lock:
            rollup = self._read_audit_rollup()
            latest = self._latest_audit_row(rollup)
            latest_run_dir = Path(str(latest.get("run_dir") or "")) if latest else None
            audit_dir = latest_run_dir / "audit" if latest_run_dir else None
            latest_details = {}
            if audit_dir and audit_dir.exists():
                latest_details = {
                    "audit_checks": self._read_json_file(audit_dir / "14_audit_checks.json"),
                    "market_price_evidence": self._read_json_file(audit_dir / "49_market_price_evidence_summary.json"),
                    "market_context": self._read_json_file(audit_dir / "67_market_context_summary.json"),
                    "attribution_dossier": self._read_json_file(audit_dir / "69_attribution_dossier.json"),
                    "run_evidence_digest": self._read_json_file(audit_dir / "72_run_evidence_digest_summary.json"),
                    "startup_binding": self._read_json_file(audit_dir / "75_startup_binding_summary.json"),
                    "run_failure_diagnosis": self._read_json_file(audit_dir / "77_run_failure_diagnosis_summary.json"),
                    "account_activity_attribution": self._read_json_file(
                        audit_dir / "51_account_activity_attribution_summary.json"
                    ),
                    "strict_attribution_checklist": self._read_json_file(
                        audit_dir / "53_strict_attribution_checklist_summary.json"
                    ),
                    "corporate_action_trace": self._read_json_file(audit_dir / "55_corporate_action_summary.json"),
                    "portfolio_history": self._read_json_file(audit_dir / "57_portfolio_history_summary.json"),
                    "intraday_bar_evidence": self._read_json_file(audit_dir / "59_intraday_bar_summary.json"),
                    "quote_evidence": self._read_json_file(audit_dir / "61_quote_summary.json"),
                    "calendar": self._read_json_file(audit_dir / "63_calendar_summary.json"),
                    "account_state_bridge": self._read_json_file(
                        audit_dir / "65_account_state_bridge_summary.json"
                    ),
                    "position_snapshot_integrity": self._read_json_file(
                        audit_dir / "37_position_snapshot_integrity.json"
                    ),
                    "residual_diagnosis": self._read_json_file(audit_dir / "38_residual_diagnosis.json"),
                    "position_capacity": self._read_json_file(
                        audit_dir / "82_position_capacity_summary.json"
                    ),
                    "execution_attempt_outcomes": self._read_json_file(
                        audit_dir / "83_execution_attempt_outcome_summary.json"
                    ),
                }
            return {
                "rollup": rollup,
                "latest": latest,
                "latest_details": latest_details,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }

    def get_execution_timeline(self, run_name: str | None = None) -> dict[str, Any]:
        """Build a multi-track timeline for one 22:00 execution run."""
        with self.cache_lock:
            run_dirs = [
                path
                for path in self._iter_run_dirs()
                if path.name.endswith("_execute")
                and (path / "decision_phase_timings.json").exists()
            ]
            available_runs = []
            for path in run_dirs:
                timing = self._read_json_file(path / "decision_phase_timings.json")
                records = self._read_json_value(path / "execution_records.json", [])
                available_runs.append(
                    {
                        "run_name": path.name,
                        "session_date": self._run_dir_session_date(path),
                        "status": str(timing.get("status") or "unknown"),
                        "elapsed_seconds": self._safe_timeline_float(
                            timing.get("elapsed_seconds")
                        ),
                        "order_count": len(records) if isinstance(records, list) else 0,
                    }
                )

            requested = str(run_name or "").strip()
            selected = next(
                (path for path in run_dirs if path.name == requested),
                None,
            ) if requested else (run_dirs[0] if run_dirs else None)
            if selected is None:
                return {
                    "schema_version": "1.0",
                    "scope": "execution_only",
                    "status": "not_found" if requested else "empty",
                    "message": "Execution timeline run not found." if requested else "No execution run is available.",
                    "selected_run": requested,
                    "available_runs": available_runs,
                    "groups": [],
                    "tracks": [],
                }
            return self._build_execution_timeline(selected, available_runs)

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
        account_state = self._read_account_state()

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
                "decision_time_cn": "12:30",
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
            "account_state": {
                "last_session_idx": account_state.get("last_session_idx"),
                "last_session_date": account_state.get("last_session_date"),
                "last_run_at_utc": account_state.get("executor_last_run_utc"),
                "last_broker_equity": account_state.get("executor_last_broker_equity"),
                "last_post_trade_equity": account_state.get("executor_last_post_trade_equity"),
            },
            "scheduler_command": scheduler_cmd,
            "scheduler_due_latest": self._read_json_file(
                self.artifacts_root / "daemon" / "scheduler_due_latest.json"
            ),
            "scheduler_runtime_latest": self._read_json_file(
                self.artifacts_root / "daemon" / "scheduler_runtime_latest.json"
            ),
            "startup_binding": self.get_startup_binding(),
            "process_health": self.get_process_health(),
        }

    def get_startup_binding(self) -> dict[str, Any]:
        """Return startup/autostart evidence for diagnosing missing tray runs."""
        daemon_dir = self.artifacts_root / "daemon"
        startup_log = daemon_dir / "startup.bat.log"
        pid_paths = {
            "launcher": daemon_dir / "tray_launcher.pid",
            "scheduler": daemon_dir / "scheduler.pid",
            "watchdog": self.artifacts_root / "watchdog" / "watchdog.pid",
        }
        task_status = self._autostart_task_status()
        return {
            "schema_version": "1.0",
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "start_bat": {
                "path": (self.project_root / "Start.bat").as_posix(),
                "exists": (self.project_root / "Start.bat").exists(),
            },
            "startup_log": self._file_tail_info(startup_log, max_lines=80, max_bytes=20000),
            "pid_files": {key: self._file_tail_info(path, max_lines=1, max_bytes=2000) for key, path in pid_paths.items()},
            "autostart_task": task_status,
        }

    def _file_tail_info(self, path: Path, *, max_lines: int = 80, max_bytes: int = 20000) -> dict[str, Any]:
        try:
            stat = path.stat()
            data = path.read_bytes()[-max_bytes:]
            text = data.decode("utf-8", errors="replace")
            lines = text.splitlines()[-max_lines:]
            return {
                "path": path.as_posix(),
                "exists": True,
                "bytes": int(stat.st_size),
                "modified_at_utc": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                "tail": "\n".join(lines),
                "tail_line_count": len(lines),
                "truncated": bool(stat.st_size > len(data)),
            }
        except Exception as exc:
            return {
                "path": path.as_posix(),
                "exists": path.exists(),
                "bytes": None,
                "modified_at_utc": None,
                "tail": "",
                "tail_line_count": 0,
                "truncated": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    def _autostart_task_status(self) -> dict[str, Any]:
        if platform.system().lower() != "windows":
            return {"available": False, "reason": "not_windows"}
        script = self.project_root / "tools" / "install_autostart_task.ps1"
        if not script.exists():
            return {"available": False, "reason": "install_autostart_task_script_missing", "script": script.as_posix()}
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ProjectRoot",
            str(self.project_root),
            "-Status",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
        except Exception as exc:
            return {
                "available": False,
                "script": script.as_posix(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        return {
            "available": True,
            "script": script.as_posix(),
            "ok": result.returncode == 0,
            "returncode": int(result.returncode),
            "registered": "task registered" in stdout.lower(),
            "not_registered": "task not registered" in stdout.lower(),
            "stdout_tail": "\n".join(stdout.splitlines()[-40:]),
            "stderr_tail": "\n".join(stderr.splitlines()[-20:]),
        }

    def get_process_health(self) -> dict[str, Any]:
        """Return a compact process-tree view for tray/scheduler/dashboard binding."""
        pid_files = {
            "launcher": self.artifacts_root / "daemon" / "tray_launcher.pid",
            "scheduler": self.artifacts_root / "daemon" / "scheduler.pid",
        }

        def read_pid(path: Path) -> int | None:
            try:
                return int(path.read_text(encoding="ascii").strip())
            except Exception:
                return None

        target_pids = {key: read_pid(path) for key, path in pid_files.items()}
        processes = self._project_processes()
        by_pid = {int(p["pid"]): p for p in processes if p.get("pid") is not None}
        dashboard_pid = os.getpid()
        listener_pid = self._port_listener_pid(18076)
        launcher_pid = target_pids.get("launcher")
        scheduler_pid = target_pids.get("scheduler")

        def ancestors(pid: int | None) -> list[int]:
            out: list[int] = []
            seen: set[int] = set()
            current = pid
            while current and current not in seen:
                seen.add(current)
                proc = by_pid.get(current)
                if not proc:
                    break
                parent = proc.get("parent_pid")
                if not isinstance(parent, int) or parent <= 0:
                    break
                out.append(parent)
                current = parent
            return out

        dashboard_ancestors = ancestors(dashboard_pid)
        scheduler_ancestors = ancestors(scheduler_pid)
        scheduler_bound_to_launcher = bool(launcher_pid and scheduler_pid and launcher_pid in scheduler_ancestors)
        dashboard_bound_to_scheduler = bool(scheduler_pid and dashboard_pid and scheduler_pid in dashboard_ancestors)
        dashboard_listening = bool(listener_pid == dashboard_pid)
        role_counts = dict(sorted(Counter(str(p.get("role") or "other") for p in processes).items()))
        stub_chain_detected = any(str(p.get("executable_path") or "").lower().endswith(r"\venv\scripts\python.exe") for p in processes) or any(
            str(p.get("executable_path") or "").lower().endswith(r"\venv\scripts\pythonw.exe") for p in processes
        )

        status = "pass"
        issues: list[str] = []
        if not launcher_pid or launcher_pid not in by_pid:
            status = "attention"
            issues.append("launcher_pid_missing_or_not_running")
        if not scheduler_pid or scheduler_pid not in by_pid:
            status = "attention"
            issues.append("scheduler_pid_missing_or_not_running")
        if not dashboard_listening:
            status = "attention"
            issues.append("dashboard_port_not_owned_by_current_process")
        if launcher_pid and scheduler_pid and not scheduler_bound_to_launcher:
            status = "attention"
            issues.append("scheduler_not_descendant_of_launcher")
        if scheduler_pid and not dashboard_bound_to_scheduler:
            status = "attention"
            issues.append("dashboard_not_descendant_of_scheduler")

        return {
            "schema_version": "1.0",
            "generated_at_utc": datetime.utcnow().isoformat() + "Z",
            "status": status,
            "issues": issues,
            "pid_files": {
                "launcher": launcher_pid,
                "scheduler": scheduler_pid,
                "dashboard": dashboard_pid,
                "dashboard_port_listener": listener_pid,
            },
            "bindings": {
                "scheduler_bound_to_launcher": scheduler_bound_to_launcher,
                "dashboard_bound_to_scheduler": dashboard_bound_to_scheduler,
                "dashboard_listening": dashboard_listening,
                "stub_chain_detected": bool(stub_chain_detected),
            },
            "role_counts": role_counts,
            "processes": processes,
            "note": (
                "On Windows venv launchers may appear as a parent stub plus a real Python child; "
                "that is expected when the parent/child chain is intact."
            ),
        }

    def _project_processes(self) -> list[dict[str, Any]]:
        if platform.system().lower() != "windows":
            return [
                {
                    "pid": os.getpid(),
                    "parent_pid": None,
                    "name": "python",
                    "role": "dashboard",
                    "executable_path": sys.executable,
                    "command_line": " ".join([sys.executable, *sys.argv]),
                }
            ]
        root = str(self.project_root)
        ps = (
            "$root = " + self._ps_single_quoted(root) + "; "
            "$self = [int]$PID; "
            "$needles=@('tools\\tray_launcher.py','tools\\daily_alpaca_scheduler.py','tools\\dashboard_server.py','tools\\watch_daily_alpaca_scheduler.ps1'); "
            "$items = @(Get-CimInstance Win32_Process | Where-Object { "
            "  if([int]$_.ProcessId -eq $self){ return $false }; "
            "  $cmd=[string]$_.CommandLine; "
            "  if(-not $cmd){ return $false }; "
            "  ($cmd.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0) -and "
            "  (($needles | Where-Object { $cmd.IndexOf($_,[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0) "
            "} | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine); "
            "$items | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except Exception:
            return []
        text = (result.stdout or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        raw_items = parsed if isinstance(parsed, list) else [parsed]
        processes: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            cmd = str(item.get("CommandLine") or "")
            role = "other"
            lower = cmd.lower()
            if "tray_launcher.py" in lower:
                role = "tray"
            elif "daily_alpaca_scheduler.py" in lower:
                role = "scheduler"
            elif "dashboard_server.py" in lower:
                role = "dashboard"
            elif "watch_daily_alpaca_scheduler.ps1" in lower:
                role = "watchdog"
            try:
                pid = int(item.get("ProcessId"))
            except Exception:
                continue
            try:
                parent_pid = int(item.get("ParentProcessId") or 0)
            except Exception:
                parent_pid = None
            processes.append(
                {
                    "pid": pid,
                    "parent_pid": parent_pid,
                    "name": str(item.get("Name") or ""),
                    "role": role,
                    "executable_path": str(item.get("ExecutablePath") or ""),
                    "command_line": cmd,
                }
            )
        return sorted(processes, key=lambda item: int(item.get("pid") or 0))

    def _port_listener_pid(self, port: int) -> int | None:
        if platform.system().lower() != "windows":
            return None
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except Exception:
            return None
        needle = f":{int(port)}"
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(needle) and parts[3].upper() == "LISTENING":
                try:
                    return int(parts[-1])
                except Exception:
                    return None
        return None

    @staticmethod
    def _ps_single_quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def get_artifact_mtimes(self) -> dict[str, float]:
        """Return mtimes of key artifact files for change detection (SSE)."""
        files = {
            "account_state": self.project_root / "artifacts" / "alpaca_executor" / "account_state.json",
            "state": self.artifacts_root / "state.json",
            "audit_rollup": self.artifacts_root / "audit_rollup.json",
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
        """Enumerate all scheduler run output directories, newest trading day first.

        The scheduler writes per-session output to:
            artifacts/daily_alpaca_scheduler/<YYYYMMDD>_decision/
            artifacts/daily_alpaca_scheduler/<YYYYMMDD>_execute/
            artifacts/daily_alpaca_scheduler/<YYYYMMDD>_emergency_flatten/
        Older/test data may also live under:
            artifacts/daily_alpaca_scheduler/output/<run>/
        We collect both so the dashboard works regardless of layout. Sorting by
        directory mtime is misleading because audits rewrite old run folders;
        use the trading date encoded in the run instead.
        """
        run_dirs: list[Path] = []
        # New layout: daily runs and explicit post-run remediation events.
        if self.artifacts_root.exists():
            for d in self.artifacts_root.iterdir():
                if d.is_dir() and (
                    d.name.endswith("_decision")
                    or d.name.endswith("_execute")
                    or d.name.endswith("_emergency_flatten")
                ):
                    run_dirs.append(d)
        # Legacy layout: output/<run>/
        output_root = self.artifacts_root / "output"
        if output_root.exists():
            for d in output_root.iterdir():
                if d.is_dir():
                    run_dirs.append(d)
        run_dirs.sort(key=self._run_dir_sort_key, reverse=True)
        return run_dirs

    def _run_dir_sort_key(self, run_dir: Path) -> tuple[str, int, float]:
        summary = self._read_json_file(run_dir / "execution_summary.json")
        session_date = self._run_dir_session_date(run_dir, summary)
        run_type = str(summary.get("run_type") or "")
        if not run_type:
            run_type = (
                "execute"
                if run_dir.name.endswith("_execute")
                else "decision"
                if run_dir.name.endswith("_decision")
                else "remediation"
                if run_dir.name.endswith("_emergency_flatten")
                else ""
            )
        run_type_order = {"decision": 1, "execute": 2, "remediation": 3}.get(run_type, 0)
        try:
            mtime = run_dir.stat().st_mtime
        except Exception:
            mtime = 0.0
        return (session_date, run_type_order, mtime)

    def _run_dir_session_date(self, run_dir: Path, summary: dict[str, Any] | None = None) -> str:
        summary = summary if isinstance(summary, dict) else {}
        raw = str(summary.get("decision_date") or summary.get("session_date") or "").strip()
        if raw:
            return self._compact_date_to_iso(raw)
        token = run_dir.name.split("_", 1)[0]
        return self._compact_date_to_iso(token)

    def _read_scheduler_state(self) -> dict[str, Any]:
        """Read scheduler state.json."""
        state_file = self.artifacts_root / "state.json"
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _read_json_value(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _read_json_lines(path: Path, max_rows: int | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not path.exists():
            return rows
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if max_rows is not None and len(rows) >= max_rows:
                        break
                    try:
                        value = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except Exception:
            return []
        return rows

    @staticmethod
    def _safe_timeline_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number

    @staticmethod
    def _parse_timeline_timestamp(value: Any) -> float | None:
        token = str(value or "").strip()
        if not token:
            return None
        try:
            normalized = token[:-1] + "+00:00" if token.endswith("Z") else token
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _partition_timeline_lanes(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        lanes: list[list[dict[str, Any]]] = []
        lane_ends: list[float] = []
        ordered = sorted(
            items,
            key=lambda item: (
                float(item.get("start_seconds") or 0.0),
                float(item.get("end_seconds") or 0.0),
                str(item.get("id") or ""),
            ),
        )
        for item in ordered:
            start = float(item.get("start_seconds") or 0.0)
            end = max(start, float(item.get("end_seconds") or start))
            lane_index = next(
                (index for index, lane_end in enumerate(lane_ends) if lane_end <= start + 1e-6),
                None,
            )
            if lane_index is None:
                lanes.append([item])
                lane_ends.append(end)
            else:
                lanes[lane_index].append(item)
                lane_ends[lane_index] = end
        return lanes

    @staticmethod
    def _timeline_api_operation(method: str, path: str) -> str:
        method_token = str(method or "GET").upper()
        path_token = str(path or "").lower().rstrip("/")
        if "/orders" in path_token:
            if method_token == "POST":
                return "Submit order"
            if method_token == "DELETE":
                return "Cancel order"
            return "Poll order" if path_token.count("/") >= 3 else "List orders"
        if "/positions" in path_token:
            return "Positions"
        if "/account/portfolio/history" in path_token:
            return "Portfolio history"
        if "/account/configurations" in path_token:
            return "Account settings"
        if path_token.endswith("/account"):
            return "Account"
        if "/assets" in path_token:
            return "Assets"
        if "/calendar" in path_token:
            return "Calendar"
        if "/clock" in path_token:
            return "Market clock"
        if any(token in path_token for token in ("/bars", "/quotes", "/snapshots", "/trades")):
            return "Market data"
        if "corporate-actions" in path_token:
            return "Corporate actions"
        return "Other API"

    def _build_execution_timeline(
        self,
        run_dir: Path,
        available_runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        timing = self._read_json_file(run_dir / "decision_phase_timings.json")
        records_value = self._read_json_value(run_dir / "execution_records.json", [])
        records = records_value if isinstance(records_value, list) else []
        events = self._read_json_lines(run_dir / "run_events.jsonl", max_rows=5000)
        api_rows = self._read_json_lines(run_dir / "alpaca_api_audit.jsonl", max_rows=50000)

        run_start_epoch = self._parse_timeline_timestamp(timing.get("run_started_at_utc"))
        if run_start_epoch is None:
            event_starts = [
                self._parse_timeline_timestamp(row.get("at_utc"))
                for row in events
            ]
            run_start_epoch = next((value for value in event_starts if value is not None), None)
        if run_start_epoch is None:
            return {
                "schema_version": "1.0",
                "scope": "execution_only",
                "status": "invalid",
                "message": "Execution run has no valid start timestamp.",
                "selected_run": run_dir.name,
                "available_runs": available_runs,
                "groups": [],
                "tracks": [],
            }

        phase_items: list[dict[str, Any]] = []
        trading_phase_start_run = None
        trading_phase_end_run = None
        for phase in timing.get("phases", []):
            if not isinstance(phase, dict) or phase.get("phase") != "order_submission_and_tracking":
                continue
            trading_phase_start_run = self._safe_timeline_float(
                phase.get("run_elapsed_start_seconds")
            )
            trading_phase_end_run = self._safe_timeline_float(
                phase.get("run_elapsed_end_seconds")
            )
            duration = self._safe_timeline_float(phase.get("elapsed_seconds"))
            if trading_phase_start_run is not None and trading_phase_end_run is None:
                trading_phase_end_run = trading_phase_start_run + max(0.0, duration or 0.0)
            break

        if trading_phase_start_run is None:
            trading_phase_start_run = 0.0
        if trading_phase_end_run is None:
            trading_phase_end_run = max(
                trading_phase_start_run,
                self._safe_timeline_float(timing.get("elapsed_seconds")) or 0.0,
            )
        timeline_start_epoch = run_start_epoch + trading_phase_start_run
        total_seconds = max(0.0, trading_phase_end_run - trading_phase_start_run)
        trading_phase_start = 0.0
        trading_phase_end = total_seconds

        for index, phase in enumerate(timing.get("phases", [])):
            if not isinstance(phase, dict):
                continue
            name = str(phase.get("phase") or f"phase_{index + 1}")
            if name != "order_submission_and_tracking":
                continue
            start = self._safe_timeline_float(phase.get("run_elapsed_start_seconds"))
            end = self._safe_timeline_float(phase.get("run_elapsed_end_seconds"))
            duration = self._safe_timeline_float(phase.get("elapsed_seconds"))
            if start is None:
                continue
            if end is None:
                end = start + max(0.0, duration or 0.0)
            if end <= start + 1e-6:
                continue
            start = max(0.0, start - trading_phase_start_run)
            end = min(total_seconds, max(start, end - trading_phase_start_run))
            phase_items.append(
                {
                    "id": f"phase-{index + 1}",
                    "label": name.replace("_", " ").title(),
                    "start_seconds": round(max(0.0, start), 6),
                    "end_seconds": round(max(start, end), 6),
                    "duration_seconds": round(max(0.0, end - start), 6),
                    "status": str(phase.get("status") or "unknown"),
                    "color_key": "trading",
                    "detail": {
                        "phase": name,
                        "share_of_run_pct": self._safe_timeline_float(phase.get("share_of_run_pct")),
                    },
                }
            )

        order_items: list[dict[str, Any]] = []
        attempt_count = 0
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            batch_epoch = self._parse_timeline_timestamp(record.get("batch_started_at_utc"))
            wall_seconds = max(
                0.0,
                self._safe_timeline_float(record.get("order_wall_time_seconds")) or 0.0,
            )
            queue_wait_ms = max(
                0.0,
                self._safe_timeline_float(record.get("queue_wait_ms")) or 0.0,
            )
            if batch_epoch is None:
                continue
            start = max(0.0, batch_epoch - timeline_start_epoch + queue_wait_ms / 1000.0)
            end = min(total_seconds, start + max(wall_seconds, 0.001))
            if start > total_seconds + 1e-6 or end <= start:
                continue
            status = str(record.get("status_latest") or "unknown")
            side = str(record.get("side") or "").lower()
            attempts = record.get("attempts") if isinstance(record.get("attempts"), list) else []
            attempt_count += len(attempts)
            children: list[dict[str, Any]] = []
            for attempt_index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    continue
                timestamps = [
                    self._parse_timeline_timestamp(attempt.get("quote_observed_at_utc")),
                    self._parse_timeline_timestamp(attempt.get("cancel_requested_at_utc")),
                    self._parse_timeline_timestamp(attempt.get("updated_at")),
                ]
                poll_events = attempt.get("poll_events") if isinstance(attempt.get("poll_events"), list) else []
                timestamps.extend(
                    self._parse_timeline_timestamp(event.get("at_utc"))
                    for event in poll_events
                    if isinstance(event, dict)
                )
                relative = sorted(
                    max(start, min(end, timestamp - timeline_start_epoch))
                    for timestamp in timestamps
                    if timestamp is not None
                )
                child_start = relative[0] if relative else start
                child_end = relative[-1] if relative else end
                if child_end <= child_start + 1e-6:
                    child_end = min(end, child_start + 0.05)
                children.append(
                    {
                        "id": f"order-{index + 1}-attempt-{attempt_index + 1}",
                        "label": f"Attempt {int(attempt.get('attempt_no') or attempt_index + 1)}",
                        "start_seconds": round(child_start, 6),
                        "end_seconds": round(max(child_start, child_end), 6),
                        "duration_seconds": round(max(0.0, child_end - child_start), 6),
                        "status": str(attempt.get("status_latest") or status),
                        "detail": {
                            "attempt_no": int(attempt.get("attempt_no") or attempt_index + 1),
                            "quote_age_ms": self._safe_timeline_float(attempt.get("quote_age_ms")),
                            "offset_bps": self._safe_timeline_float(attempt.get("offset_bps")),
                            "spread_bps": self._safe_timeline_float(attempt.get("live_spread_bps")),
                            "poll_event_count": len(poll_events),
                            "reference_price_source": str(attempt.get("reference_price_source") or ""),
                        },
                    }
                )
            order_items.append(
                {
                    "id": f"order-{index + 1}",
                    "label": str(record.get("symbol") or f"Order {index + 1}"),
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(max(0.0, end - start), 6),
                    "status": status,
                    "color_key": "buy" if side == "buy" else "sell",
                    "detail": {
                        "symbol": str(record.get("symbol") or ""),
                        "side": side,
                        "qty": self._safe_timeline_float(record.get("qty")),
                        "stage": str(record.get("stage") or ""),
                        "macro_stage": str(record.get("macro_stage") or ""),
                        "release_action_class": str(
                            record.get("release_action_class") or ""
                        ),
                        "status": status,
                        "queue_wait_ms": round(queue_wait_ms, 3),
                        "attempt_count": len(attempts),
                        "batch_wave_index": record.get("batch_wave_index"),
                        "batch_worker_safety_cap": record.get("batch_worker_safety_cap"),
                        "batch_effective_workers": record.get("batch_effective_workers"),
                        "fractional_close_retry_count": record.get("fractional_close_retry_count"),
                        "fractional_close_residual_qty": record.get("fractional_close_residual_qty"),
                    },
                    "children": children,
                }
            )

        order_lanes = self._partition_timeline_lanes(order_items)
        stage_items: list[dict[str, Any]] = []
        if order_items:
            by_stage: dict[str, list[dict[str, Any]]] = {}
            for item in order_items:
                detail = item.get("detail", {})
                stage = str(
                    detail.get("macro_stage")
                    or detail.get("stage")
                    or "orders"
                )
                by_stage.setdefault(stage, []).append(item)
            stage_rows = sorted(
                by_stage.items(),
                key=lambda pair: min(float(item["start_seconds"]) for item in pair[1]),
            )
            first_order_start = min(float(item["start_seconds"]) for item in order_items)
            last_order_end = max(float(item["end_seconds"]) for item in order_items)
            if trading_phase_start is not None and first_order_start > trading_phase_start + 0.05:
                stage_items.append(
                    {
                        "id": "stage-prepare",
                        "label": "Prepare submission",
                        "start_seconds": round(trading_phase_start, 6),
                        "end_seconds": round(first_order_start, 6),
                        "duration_seconds": round(first_order_start - trading_phase_start, 6),
                        "status": "completed",
                        "color_key": "preparation",
                        "detail": {"stage": "prepare_submission"},
                    }
                )
            previous_end = first_order_start
            for stage_index, (stage, items) in enumerate(stage_rows):
                start = min(float(item["start_seconds"]) for item in items)
                end = max(float(item["end_seconds"]) for item in items)
                if start > previous_end + 0.05:
                    stage_items.append(
                        {
                            "id": f"stage-gap-{stage_index + 1}",
                            "label": "Reconcile / reproject",
                            "start_seconds": round(previous_end, 6),
                            "end_seconds": round(start, 6),
                            "duration_seconds": round(start - previous_end, 6),
                            "status": "completed",
                            "color_key": "reconcile",
                            "detail": {"stage": "inter_stage_gap"},
                        }
                    )
                stage_items.append(
                    {
                        "id": f"stage-{stage_index + 1}",
                        "label": stage.replace("_", " ").title(),
                        "start_seconds": round(start, 6),
                        "end_seconds": round(end, 6),
                        "duration_seconds": round(end - start, 6),
                        "status": "completed",
                        "color_key": "repair" if "repair" in stage else "trading",
                        "detail": {"stage": stage, "order_count": len(items)},
                    }
                )
                previous_end = max(previous_end, end)
            if trading_phase_end is not None and trading_phase_end > last_order_end + 0.05:
                stage_items.append(
                    {
                        "id": "stage-finalize",
                        "label": "Finalize trading",
                        "start_seconds": round(last_order_end, 6),
                        "end_seconds": round(trading_phase_end, 6),
                        "duration_seconds": round(trading_phase_end - last_order_end, 6),
                        "status": "completed",
                        "color_key": "reconcile",
                        "detail": {"stage": "finalize_trading"},
                    }
                )

        milestone_items: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            elapsed = self._safe_timeline_float(event.get("run_elapsed_seconds"))
            if elapsed is None:
                event_epoch = self._parse_timeline_timestamp(event.get("at_utc"))
                elapsed = event_epoch - run_start_epoch if event_epoch is not None else None
            if elapsed is not None:
                elapsed -= trading_phase_start_run
            if elapsed is None or elapsed < -0.001 or elapsed > total_seconds + 0.001:
                continue
            name = str(event.get("name") or f"Event {index + 1}")
            milestone_items.append(
                {
                    "id": f"milestone-{index + 1}",
                    "label": name.replace("_", " ").title(),
                    "start_seconds": round(max(0.0, elapsed), 6),
                    "end_seconds": round(max(0.0, elapsed), 6),
                    "duration_seconds": 0.0,
                    "status": "completed",
                    "color_key": "milestone",
                    "detail": {"event": name, "sequence": event.get("seq")},
                }
            )

        api_items: list[dict[str, Any]] = []
        api_window_end = timeline_start_epoch + total_seconds
        for index, row in enumerate(api_rows):
            started_epoch = self._parse_timeline_timestamp(row.get("started_at_utc"))
            elapsed_ms = max(0.0, self._safe_timeline_float(row.get("elapsed_ms")) or 0.0)
            if started_epoch is None or started_epoch < timeline_start_epoch or started_epoch > api_window_end:
                continue
            start = max(0.0, started_epoch - timeline_start_epoch)
            end = min(total_seconds, start + max(elapsed_ms / 1000.0, 0.001))
            if end <= start:
                continue
            parsed_path = urlparse(str(row.get("url") or "")).path
            operation = self._timeline_api_operation(str(row.get("method") or "GET"), parsed_path)
            ok = bool(row.get("ok"))
            api_items.append(
                {
                    "id": f"api-{index + 1}",
                    "label": operation,
                    "start_seconds": round(start, 6),
                    "end_seconds": round(end, 6),
                    "duration_seconds": round(max(0.0, end - start), 6),
                    "status": "success" if ok else "failed",
                    "color_key": "api" if ok else "failure",
                    "detail": {
                        "operation": operation,
                        "method": str(row.get("method") or "GET").upper(),
                        "elapsed_ms": round(elapsed_ms, 3),
                        "ok": ok,
                        "status_code": row.get("status_code"),
                        "sequence": row.get("seq"),
                    },
                }
            )
        api_lanes = self._partition_timeline_lanes(api_items)
        tracks: list[dict[str, Any]] = [
            {
                "id": "pipeline",
                "group": "pipeline",
                "label": "Pipeline",
                "kind": "phase",
                "items": phase_items,
            },
            {
                "id": "execution-stages",
                "group": "pipeline",
                "label": "Execution stages",
                "kind": "stage",
                "items": stage_items,
            },
            {
                "id": "milestones",
                "group": "milestones",
                "label": "Milestones",
                "kind": "milestone",
                "items": milestone_items,
            },
        ]
        tracks.extend(
            {
                "id": f"order-lane-{index + 1}",
                "group": "orders",
                "label": f"Order lane {index + 1}",
                "kind": "order",
                "items": lane,
            }
            for index, lane in enumerate(order_lanes)
        )
        tracks.extend(
            {
                "id": f"api-lane-{index + 1}",
                "group": "api",
                "label": f"API lane {index + 1}",
                "kind": "api",
                "items": lane,
            }
            for index, lane in enumerate(api_lanes)
        )
        groups = [
            {"id": "pipeline", "label": "Execution pipeline"},
            {"id": "milestones", "label": "Milestones"},
            {"id": "orders", "label": "Parallel orders"},
            {"id": "api", "label": "Broker API calls"},
        ]
        trading_complete_seconds = trading_phase_end
        if trading_complete_seconds is None and order_items:
            trading_complete_seconds = max(float(item["end_seconds"]) for item in order_items)
        ended_at = datetime.fromtimestamp(timeline_start_epoch + total_seconds, tz=timezone.utc).isoformat()
        return {
            "schema_version": "1.0",
            "scope": "execution_only",
            "status": "pass",
            "selected_run": run_dir.name,
            "available_runs": available_runs,
            "run": {
                "run_name": run_dir.name,
                "session_date": self._run_dir_session_date(run_dir),
                "started_at_utc": datetime.fromtimestamp(timeline_start_epoch, tz=timezone.utc).isoformat(),
                "ended_at_utc": ended_at,
                "elapsed_seconds": round(total_seconds, 6),
                "status": str(timing.get("status") or "unknown"),
            },
            "summary": {
                "total_process_seconds": round(total_seconds, 6),
                "trading_complete_seconds": round(trading_complete_seconds, 6) if trading_complete_seconds is not None else None,
                "order_count": len(order_items),
                "attempt_count": attempt_count,
                "max_order_concurrency": len(order_lanes),
                "api_call_count": len(api_items),
                "max_api_concurrency": len(api_lanes),
            },
            "groups": groups,
            "tracks": tracks,
        }

    def _read_audit_rollup(self) -> dict[str, Any]:
        """Read audit_rollup.json if available."""
        return self._read_json_file(self.artifacts_root / "audit_rollup.json")

    def _previous_completed_execute_cycle(
        self,
        run_dir: Path,
        current_summary: dict[str, Any],
    ) -> dict[str, Any]:
        """Find the prior same-epoch execute completion for cycle PnL."""
        current_date = self._run_dir_session_date(run_dir, current_summary)
        current_epoch = current_summary.get("capital_epoch")
        candidates: list[tuple[str, Path, dict[str, Any]]] = []
        for candidate in self._iter_run_dirs():
            if candidate == run_dir or not candidate.name.endswith("_execute"):
                continue
            candidate_summary = self._read_json_file(candidate / "execution_summary.json")
            if not candidate_summary:
                continue
            candidate_date = self._run_dir_session_date(candidate, candidate_summary)
            if not candidate_date or candidate_date >= current_date:
                continue
            if current_epoch is not None and candidate_summary.get("capital_epoch") not in (None, current_epoch):
                continue
            if not (candidate / "broker_account_after.json").exists():
                continue
            candidates.append((candidate_date, candidate, candidate_summary))
        if not candidates:
            return {}
        _, previous_run_dir, previous_summary = max(candidates, key=lambda item: item[0])
        account = self._read_json_file(previous_run_dir / "broker_account_after.json")
        if not account:
            return {}
        return {
            "payload": account,
            "captured_at_utc": previous_summary.get("account_equity_post_trade_captured_at_utc", ""),
            "run_dir": previous_run_dir.name,
        }

    def _read_side_return_snapshot(self, run_dir: Path) -> dict[str, Any]:
        """Expose legacy side snapshots plus account-consistent daily contributions."""
        risk = self._read_json_file(run_dir / "audit" / "07_risk_snapshot.json")
        by_side = risk.get("snapshot_intraday_pnl_by_side", {})
        if not isinstance(by_side, dict):
            by_side = {}

        long_pnl = self._safe_timeline_float(by_side.get("long"))
        short_pnl = self._safe_timeline_float(by_side.get("short"))
        long_exposure = self._safe_timeline_float(
            risk.get("gross_long_market_value_after")
        )
        short_exposure = self._safe_timeline_float(
            risk.get("gross_short_market_value_abs_after")
        )

        def normalized_return(pnl: float | None, exposure: float | None) -> float | None:
            if pnl is None or exposure is None or exposure <= 0:
                return None
            return pnl / exposure

        daily_attribution = self._read_json_file(
            run_dir / "audit" / "84_daily_side_pnl_attribution_summary.json"
        )
        if not daily_attribution:
            current_summary = self._read_json_file(run_dir / "execution_summary.json")
            cycle_start_capture = self._previous_completed_execute_cycle(
                run_dir,
                current_summary,
            )
            daily_attribution = build_daily_side_pnl_attribution(
                account_after_capture=self._read_json_file(run_dir / "broker_account_after.json"),
                account_start_capture=cycle_start_capture,
                risk_snapshot=risk,
                account_activity_summary=self._read_json_file(
                    run_dir / "audit" / "51_account_activity_attribution_summary.json"
                ),
                opening_snapshot_available=(run_dir / "broker_day_open_snapshot.json").exists(),
                account_start_run_dir=str(cycle_start_capture.get("run_dir") or ""),
                account_end_run_dir=run_dir.name,
                account_end_captured_at_utc=str(
                    current_summary.get("account_equity_post_trade_captured_at_utc", "")
                ),
            )

        return {
            "side_return_snapshot_source": (
                "audit/07_risk_snapshot.json" if risk else ""
            ),
            "long_snapshot_intraday_pnl": long_pnl,
            "short_snapshot_intraday_pnl": short_pnl,
            "long_gross_exposure_after": long_exposure,
            "short_gross_exposure_after": short_exposure,
            "long_snapshot_intraday_return": normalized_return(
                long_pnl,
                long_exposure,
            ),
            "short_snapshot_intraday_return": normalized_return(
                short_pnl,
                short_exposure,
            ),
            "daily_side_pnl_attribution_status": daily_attribution.get("status", "unavailable"),
            "daily_side_pnl_window_semantics": daily_attribution.get("window_semantics", ""),
            "daily_side_pnl_denominator_semantics": daily_attribution.get(
                "denominator_semantics", ""
            ),
            "daily_side_pnl_semantics": daily_attribution.get("side_pnl_semantics", ""),
            "daily_account_cycle_start_equity": daily_attribution.get("account_cycle_start_equity"),
            "daily_account_cycle_end_equity": daily_attribution.get("account_cycle_end_equity"),
            "daily_account_cycle_start_run_dir": daily_attribution.get("account_cycle_start_run_dir", ""),
            "daily_account_cycle_end_run_dir": daily_attribution.get("account_cycle_end_run_dir", ""),
            "daily_account_last_equity": daily_attribution.get("account_last_equity"),
            "daily_account_pnl": daily_attribution.get("account_daily_pnl"),
            "daily_account_return": daily_attribution.get("account_daily_return"),
            "daily_long_pnl": daily_attribution.get("long_pnl"),
            "daily_short_pnl": daily_attribution.get("short_pnl"),
            "daily_known_non_trade_pnl": daily_attribution.get("known_non_trade_pnl"),
            "daily_unattributed_residual_pnl": daily_attribution.get("unattributed_residual_pnl"),
            "daily_long_equity_contribution": daily_attribution.get("long_equity_contribution"),
            "daily_short_equity_contribution": daily_attribution.get("short_equity_contribution"),
            "daily_known_non_trade_equity_contribution": daily_attribution.get(
                "known_non_trade_equity_contribution"
            ),
            "daily_unattributed_equity_contribution": daily_attribution.get(
                "unattributed_equity_contribution"
            ),
            "daily_side_pnl_residual_abs_bps": daily_attribution.get(
                "residual_abs_bps_of_cycle_start_equity",
                daily_attribution.get("residual_abs_bps_of_last_equity"),
            ),
            "daily_side_pnl_reconciliation_threshold_bps": daily_attribution.get(
                "reconciliation_threshold_bps"
            ),
            "daily_side_opening_snapshot_available": daily_attribution.get(
                "opening_snapshot_available", False
            ),
        }

    @staticmethod
    def _compact_date_to_iso(value: str) -> str:
        token = str(value or "").strip()
        if len(token) == 8 and token.isdigit():
            return f"{token[:4]}-{token[4:6]}-{token[6:8]}"
        return token

    def _audit_rows_by_session_date(self) -> dict[str, dict[str, Any]]:
        rollup = self._read_audit_rollup()
        rows = rollup.get("rows", []) if isinstance(rollup.get("rows"), list) else []
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            session_date = str(row.get("session_date") or "")
            if session_date:
                out[session_date] = row
        return out

    def _read_lightweight_run_audit(self, run_dir: Path) -> dict[str, Any]:
        """Read task-level audit files for decision or partial run directories."""
        audit_dir = run_dir / "audit"
        if not audit_dir.exists():
            return {}
        run_failure = self._read_json_file(audit_dir / "77_run_failure_diagnosis_summary.json")
        evidence = self._read_json_file(audit_dir / "72_run_evidence_digest_summary.json")
        startup = self._read_json_file(audit_dir / "75_startup_binding_summary.json")
        if not any((run_failure, evidence, startup)):
            return {}

        statuses = [
            str(run_failure.get("status") or ""),
            str(evidence.get("status") or ""),
            str(startup.get("status") or ""),
        ]
        if "fail" in statuses:
            audit_status = "fail"
        elif any(status and status not in {"pass", "not_applicable"} for status in statuses):
            audit_status = "attention"
        else:
            audit_status = "pass"

        return {
            "run_dir": run_dir.as_posix(),
            "session_date": str(run_failure.get("session_date") or run_dir.name[:8]),
            "audit_status": audit_status,
            "audit_issues": int(run_failure.get("issue_count") or 0) + int(startup.get("issue_count") or 0),
            "run_failure_status": run_failure.get("status", "not_applicable"),
            "run_failure_task_status": run_failure.get("task_status", ""),
            "run_failure_class": run_failure.get("failure_class", ""),
            "run_failure_error_type": run_failure.get("error_type", ""),
            "run_failure_error": run_failure.get("error", ""),
            "run_evidence_digest_status": evidence.get("status", "not_applicable"),
            "run_evidence_digest_missing_files": int(evidence.get("missing_file_count") or 0),
            "run_evidence_digest_strict_missing_files": int(evidence.get("strict_missing_file_count") or 0),
            "run_evidence_digest_run_event_count": int(evidence.get("run_event_count") or 0),
            "run_evidence_digest_hash_manifest_file_count": int(
                evidence.get("file_hash_manifest_file_count") or 0
            ),
            "run_evidence_digest_artifact_completeness_status": evidence.get(
                "artifact_completeness_status", ""
            ),
            "run_evidence_digest_artifact_completeness_partial_category_count": int(
                evidence.get("artifact_completeness_partial_category_count") or 0
            ),
            "startup_binding_status": startup.get("status", "not_applicable"),
            "startup_binding_issue_count": int(startup.get("issue_count") or 0),
            "startup_autostart_registered": bool(startup.get("autostart_registered")),
            "startup_process_health_status": startup.get("process_health_status", "not_applicable"),
        }

    @staticmethod
    def _latest_audit_row(rollup: dict[str, Any]) -> dict[str, Any]:
        rows = rollup.get("rows", []) if isinstance(rollup.get("rows"), list) else []
        if not rows:
            return {}
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if not valid_rows:
            return {}
        return sorted(valid_rows, key=lambda row: str(row.get("session_date") or ""))[-1]

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

    def _read_account_state(self) -> dict[str, Any]:
        """Read the executor account lifecycle state."""
        return self._read_json_file(
            self.project_root / "artifacts" / "alpaca_executor" / "account_state.json"
        )

    @staticmethod
    def _account_epoch(account_state: dict[str, Any]) -> dict[str, Any]:
        state = account_state if isinstance(account_state, dict) else {}
        effective_session = str(state.get("account_reset_effective_session") or "").strip()
        return {
            "capital_epoch": max(1, int(state.get("lifecycle_epoch") or 1)),
            "effective_session": effective_session,
            "initial_equity": float(state.get("initial_equity") or 0.0),
            "initial_cash": float(state.get("initial_cash") or 0.0),
            "reset_at_utc": str(state.get("account_reset_at_utc") or ""),
        }

    @staticmethod
    def _account_reset_pending(account_epoch: dict[str, Any], latest_summary: dict[str, Any]) -> bool:
        effective_session = str(account_epoch.get("effective_session") or "")
        if not effective_session:
            return False
        latest_session = str(
            latest_summary.get("_dashboard_session_date")
            or latest_summary.get("decision_date")
            or latest_summary.get("session_date")
            or ""
        )
        latest_session = DataAggregator._compact_date_to_iso(latest_session)
        return not latest_session or latest_session < effective_session

    @staticmethod
    def _capital_epoch_for_session(account_epoch: dict[str, Any], session_date: str) -> int:
        active_epoch = max(1, int(account_epoch.get("capital_epoch") or 1))
        effective_session = str(account_epoch.get("effective_session") or "")
        normalized_session = DataAggregator._compact_date_to_iso(str(session_date or ""))
        if effective_session and normalized_session and normalized_session < effective_session:
            return max(1, active_epoch - 1)
        return active_epoch

    def _get_latest_execution_summary(self) -> dict[str, Any]:
        """Get most recent execution_summary.json across all run directories."""
        for run_dir in self._iter_run_dirs():
            summary_file = run_dir / "execution_summary.json"
            if summary_file.exists():
                try:
                    summary = json.loads(summary_file.read_text(encoding="utf-8"))
                    if isinstance(summary, dict):
                        summary["_dashboard_session_date"] = self._run_dir_session_date(
                            run_dir,
                            summary,
                        )
                        return summary
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
        elif path == "/api/history":
            limit = int(query.get("limit", ["30"])[0])
            self._send_json(self.aggregator.get_history(limit))
        elif path == "/api/audit":
            self._send_json(self.aggregator.get_audit())
        elif path == "/api/execution-timeline":
            run_name = query.get("run", [None])[0]
            self._send_json(self.aggregator.get_execution_timeline(run_name))
        elif path == "/api/logs":
            lines = int(query.get("lines", ["100"])[0])
            level = query.get("level", ["all"])[0]
            source = query.get("source", ["all"])[0]
            self._send_json(self.aggregator.get_logs(lines, level, source))
        elif path == "/api/config":
            self._send_json(self.aggregator.get_config())
        elif path == "/api/process-health":
            self._send_json(self.aggregator.get_process_health())
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
    parser.add_argument("--port", type=int, default=18076, help="Server port")
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
