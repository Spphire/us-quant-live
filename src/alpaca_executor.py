from __future__ import annotations

import argparse
import hashlib
import json
import locale
import math
import os
import platform
import re
import socket
import subprocess
import sys
import time
import traceback
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpha_core import (  # noqa: E402
    DEFAULT_FACTOR_WEIGHTS,
    AlphaCore,
    SecApiClient,
    _resolve_industry_map_for_symbols,
    _resolve_sec_cache_paths,
)
from decision_engine import DecisionConfig, DecisionEngine  # noqa: E402
from dynamic_symbol_pool import (  # noqa: E402
    DEFAULT_CANDIDATE_SYMBOLS_PATH,
    DynamicSymbolPool,
    _build_runtime_clean_core_symbol_set,
    _build_tradable_symbol_set,
    _load_candidate_symbols,
    _resolve_alpaca_credentials,
)
from executable_target_projector import project_executable_targets  # noqa: E402
from vendors import (  # noqa: E402
    AlpacaHttpClient,
    AlpacaRequestError,
    LongbridgeCredentials,
    LongbridgeQuoteClient,
    LongbridgeQuoteError,
)


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "alpaca_executor"
DEFAULT_ACCOUNT_STATE_PATH = PROJECT_ROOT / "artifacts" / "alpaca_executor" / "account_state.json"
DEFAULT_LONGBRIDGE_CONFIG_PATH = PROJECT_ROOT / "configs" / "longbridge.local.json"
EPS = 1e-10
MAX_SAFE_EXECUTION_WORKERS = 10
TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "done_for_day",
    "stopped",
    "suspended",
    "calculated",
}


@dataclass(slots=True)
class OrderInstruction:
    symbol: str
    side: str
    qty: float
    reference_price: float
    sizing_price: float
    current_notional: float
    target_notional: float
    delta_notional: float
    opening_short: bool
    current_signed_qty: float | None = None
    target_signed_qty: float | None = None


class _DecisionPhaseTimingRecorder:
    """Persist sequential executor phase timings while a run is in progress."""

    DECISION_COMPUTE_PHASES = {
        "dynamic_symbol_pool",
        "sec_industry_map",
        "alpha_core_build",
        "portfolio_decision",
    }

    def __init__(
        self,
        *,
        output_root: Path,
        run_started_at_utc: str,
        run_started_monotonic: float,
        clock: Any | None = None,
        utc_now: Any | None = None,
    ) -> None:
        self.path = Path(output_root) / "decision_phase_timings.json"
        self.run_started_at_utc = str(run_started_at_utc)
        self.run_started_monotonic = float(run_started_monotonic)
        self._clock = clock or time.monotonic
        self._utc_now = utc_now or _utc_now
        self._phases: list[dict[str, Any]] = []
        self._active_phase: str | None = None
        self._status = "running"
        self._final_elapsed_seconds: float | None = None
        self._final_context: dict[str, Any] = {}

    def start(self, phase: str, context: Mapping[str, Any] | None = None) -> None:
        name = str(phase)
        if self._active_phase is not None:
            raise RuntimeError(f"Cannot start phase {name!r}; {self._active_phase!r} is still running.")
        if any(item.get("phase") == name for item in self._phases):
            raise RuntimeError(f"Decision timing phase already recorded: {name}")
        now = float(self._clock())
        self._phases.append(
            {
                "phase": name,
                "status": "running",
                "started_at_utc": str(self._utc_now()),
                "finished_at_utc": None,
                "elapsed_seconds": 0.0,
                "run_elapsed_start_seconds": max(0.0, now - self.run_started_monotonic),
                "run_elapsed_end_seconds": None,
                "context": dict(context or {}),
            }
        )
        self._active_phase = name
        self._persist(now=now)
        print(f"[DecisionTiming] phase={name} status=started", flush=True)

    def finish(self, phase: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        name = str(phase)
        if self._active_phase != name:
            raise RuntimeError(f"Cannot finish phase {name!r}; active phase is {self._active_phase!r}.")
        now = float(self._clock())
        row = self._phases[-1]
        row["status"] = "completed"
        row["finished_at_utc"] = str(self._utc_now())
        row["elapsed_seconds"] = max(
            0.0,
            now - self.run_started_monotonic - float(row["run_elapsed_start_seconds"]),
        )
        row["run_elapsed_end_seconds"] = max(0.0, now - self.run_started_monotonic)
        if context:
            row["context"].update(dict(context))
        self._active_phase = None
        payload = self._persist(now=now)
        print(
            f"[DecisionTiming] phase={name} status=completed elapsed={float(row['elapsed_seconds']):.3f}s",
            flush=True,
        )
        return dict(payload)

    def skip(self, phase: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        name = str(phase)
        if self._active_phase is not None:
            raise RuntimeError(f"Cannot skip phase {name!r}; {self._active_phase!r} is still running.")
        if any(item.get("phase") == name for item in self._phases):
            raise RuntimeError(f"Decision timing phase already recorded: {name}")
        now = float(self._clock())
        at_utc = str(self._utc_now())
        run_elapsed = max(0.0, now - self.run_started_monotonic)
        self._phases.append(
            {
                "phase": name,
                "status": "skipped",
                "started_at_utc": at_utc,
                "finished_at_utc": at_utc,
                "elapsed_seconds": 0.0,
                "run_elapsed_start_seconds": run_elapsed,
                "run_elapsed_end_seconds": run_elapsed,
                "context": dict(context or {}),
            }
        )
        payload = self._persist(now=now)
        print(f"[DecisionTiming] phase={name} status=skipped", flush=True)
        return payload

    def fail(self, exc: BaseException) -> dict[str, Any]:
        now = float(self._clock())
        failed_at_utc = str(self._utc_now())
        if self._active_phase is not None:
            row = self._phases[-1]
            row["status"] = "failed"
            row["finished_at_utc"] = failed_at_utc
            row["elapsed_seconds"] = max(
                0.0,
                now - self.run_started_monotonic - float(row["run_elapsed_start_seconds"]),
            )
            row["run_elapsed_end_seconds"] = max(0.0, now - self.run_started_monotonic)
            row["context"].update(
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            self._active_phase = None
        self._status = "failed"
        self._final_elapsed_seconds = max(0.0, now - self.run_started_monotonic)
        self._final_context = {
            "failed_at_utc": failed_at_utc,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        payload = self._persist(now=now)
        print(
            f"[DecisionTiming] status=failed elapsed={self._final_elapsed_seconds:.3f}s",
            flush=True,
        )
        return payload

    def finalize(self, *, status: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._active_phase is not None:
            raise RuntimeError(f"Cannot finalize timings while phase {self._active_phase!r} is running.")
        now = float(self._clock())
        self._status = str(status)
        self._final_elapsed_seconds = max(0.0, now - self.run_started_monotonic)
        self._final_context = dict(context or {})
        payload = self._persist(now=now)
        print(
            f"[DecisionTiming] status={self._status} elapsed={self._final_elapsed_seconds:.3f}s "
            f"decision_compute={float(payload['decision_compute_elapsed_seconds']):.3f}s",
            flush=True,
        )
        return payload

    def snapshot(self) -> dict[str, Any]:
        return self._build_snapshot(now=float(self._clock()))

    def _persist(self, *, now: float) -> dict[str, Any]:
        payload = self._build_snapshot(now=now)
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        _write_json_file(temporary_path, payload)
        os.replace(temporary_path, self.path)
        return payload

    def _build_snapshot(self, *, now: float) -> dict[str, Any]:
        total_elapsed = (
            float(self._final_elapsed_seconds)
            if self._final_elapsed_seconds is not None
            else max(0.0, float(now) - self.run_started_monotonic)
        )
        rows = [dict(item) for item in self._phases]
        if self._active_phase is not None and rows:
            row = rows[-1]
            row["elapsed_seconds"] = max(
                0.0,
                float(now) - self.run_started_monotonic - float(row["run_elapsed_start_seconds"]),
            )
            row["run_elapsed_end_seconds"] = max(0.0, float(now) - self.run_started_monotonic)

        timed_elapsed = sum(float(row.get("elapsed_seconds") or 0.0) for row in rows)
        for row in rows:
            row["elapsed_seconds"] = round(float(row.get("elapsed_seconds") or 0.0), 6)
            row["run_elapsed_start_seconds"] = round(float(row.get("run_elapsed_start_seconds") or 0.0), 6)
            if row.get("run_elapsed_end_seconds") is not None:
                row["run_elapsed_end_seconds"] = round(float(row["run_elapsed_end_seconds"]), 6)
            row["share_of_run_pct"] = (
                round(100.0 * float(row["elapsed_seconds"]) / total_elapsed, 3)
                if total_elapsed > EPS
                else 0.0
            )

        ranked = sorted(
            (row for row in rows if float(row.get("elapsed_seconds") or 0.0) > 0.0),
            key=lambda row: float(row.get("elapsed_seconds") or 0.0),
            reverse=True,
        )
        slowest = (
            {
                "phase": str(ranked[0]["phase"]),
                "elapsed_seconds": float(ranked[0]["elapsed_seconds"]),
                "share_of_run_pct": float(ranked[0]["share_of_run_pct"]),
            }
            if ranked
            else None
        )
        decision_compute_elapsed = sum(
            float(row.get("elapsed_seconds") or 0.0)
            for row in rows
            if row.get("phase") in self.DECISION_COMPUTE_PHASES
        )
        return {
            "schema_version": "1.0",
            "artifact_type": "decision_phase_timings",
            "measurement_scope": (
                "executor_start_through_execution_summary_preparation; "
                "final evidence-manifest and scheduler overhead are excluded"
            ),
            "run_started_at_utc": self.run_started_at_utc,
            "updated_at_utc": str(self._utc_now()),
            "status": self._status,
            "current_phase": self._active_phase,
            "elapsed_seconds": round(total_elapsed, 6),
            "timed_phase_elapsed_seconds": round(timed_elapsed, 6),
            "unattributed_elapsed_seconds": round(max(0.0, total_elapsed - timed_elapsed), 6),
            "decision_compute_elapsed_seconds": round(decision_compute_elapsed, 6),
            "completed_phase_count": sum(row.get("status") == "completed" for row in rows),
            "skipped_phase_count": sum(row.get("status") == "skipped" for row in rows),
            "slowest_phase": slowest,
            "optimization_candidates": [
                {
                    "phase": str(row["phase"]),
                    "elapsed_seconds": float(row["elapsed_seconds"]),
                    "share_of_run_pct": float(row["share_of_run_pct"]),
                }
                for row in ranked[:3]
            ],
            "final_context": dict(self._final_context),
            "phases": rows,
        }


class _PersistentRunEvents(list[dict[str, Any]]):
    def __init__(self, *, path: Path, run_started_monotonic: float) -> None:
        super().__init__()
        self.path = Path(path)
        self.run_started_monotonic = float(run_started_monotonic)

    def persist(self) -> None:
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        _write_jsonl_file(temporary_path, self)
        os.replace(temporary_path, self.path)


def _build_submission_capability_guard(
    *,
    raw_target_signed_weights: Mapping[str, float],
    capacity_adjusted_target_signed_weights: Mapping[str, float],
    executable_expected_signed_weights: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    account_equity: float,
    shorting_enabled: bool,
    material_notional_tolerance: float = 1.0,
) -> dict[str, Any]:
    equity = max(float(account_equity), 1e-9)
    tolerance = max(float(material_notional_tolerance), 0.0)

    intended_long_weight = sum(
        max(0.0, float(weight))
        for weight in capacity_adjusted_target_signed_weights.values()
    )
    intended_short_weight = sum(
        max(0.0, -float(weight))
        for weight in capacity_adjusted_target_signed_weights.values()
    )
    executable_long_weight = sum(
        max(0.0, float(weight)) for weight in executable_expected_signed_weights.values()
    )
    executable_short_weight = sum(
        max(0.0, -float(weight)) for weight in executable_expected_signed_weights.values()
    )

    short_increase_symbols: list[str] = []
    for symbol, target_weight in capacity_adjusted_target_signed_weights.items():
        target_weight = float(target_weight)
        if target_weight >= -EPS:
            continue
        intended_short_notional = -target_weight * equity
        current_short_notional = max(
            0.0,
            -float(current_signed_notional.get(str(symbol).upper(), 0.0)),
        )
        if intended_short_notional > current_short_notional + tolerance:
            short_increase_symbols.append(str(symbol).upper())

    reasons: list[str] = []
    if short_increase_symbols and not bool(shorting_enabled):
        reasons.append("account_shorting_disabled_for_required_short_increase")
    if intended_long_weight > EPS and intended_short_weight > EPS:
        if executable_long_weight <= EPS or executable_short_weight <= EPS:
            reasons.append("long_short_side_missing_after_projection")

    return {
        "status": "blocked" if reasons else "pass",
        "blocking_reasons": reasons,
        "broker_shorting_enabled": bool(shorting_enabled),
        "raw_long_symbol_count": int(
            sum(float(weight) > EPS for weight in raw_target_signed_weights.values())
        ),
        "raw_short_symbol_count": int(
            sum(float(weight) < -EPS for weight in raw_target_signed_weights.values())
        ),
        "required_short_increase_symbol_count": int(len(short_increase_symbols)),
        "required_short_increase_symbols": sorted(short_increase_symbols),
        "intended_long_gross_weight": float(intended_long_weight),
        "intended_short_gross_weight": float(intended_short_weight),
        "executable_long_gross_weight": float(executable_long_weight),
        "executable_short_gross_weight": float(executable_short_weight),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute daily AlphaCore + DecisionEngine plan on Alpaca (paper by default): "
            "broker state -> alpha decision -> open-triggered order submit -> post-trade reconciliation."
        )
    )
    parser.add_argument("--date", default=date.today().isoformat())

    parser.add_argument(
        "--accounts-json-path",
        default="configs/alpaca_acounts/alpaca_accounts.local.json",
    )
    parser.add_argument("--account-name", default="ALPACA_US_FULL")
    parser.add_argument("--data-base-url", default="https://data.alpaca.markets")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=2)

    parser.add_argument("--candidate-symbols-path", default=str(DEFAULT_CANDIDATE_SYMBOLS_PATH))
    parser.add_argument("--pool-size", type=int, default=1000)
    parser.add_argument("--lookback-sessions", type=int, default=20)
    parser.add_argument("--min-observations", type=int, default=15)
    parser.add_argument("--price-floor", type=float, default=10.0)
    parser.add_argument("--dynamic-bars-window-calendar-days", type=int, default=420)
    parser.add_argument("--dynamic-bars-chunk-size", type=int, default=120)
    parser.add_argument("--dynamic-bars-workers", type=int, default=8)
    parser.add_argument("--dynamic-beta-full-observations", type=int, default=252)
    parser.add_argument(
        "--dynamic-feed",
        default="sip",
        help="Feed for dynamic symbol pool refresh. MUST be 'sip' for 1000-symbol universe (IEX covers only ~2-3%% market).",
    )

    parser.add_argument("--feed", default="sip", help="Feed used by AlphaCore bars fetch. MUST be 'sip' for full market coverage.")
    parser.add_argument("--price-adjustment", default="all")
    parser.add_argument("--bars-window-calendar-days", type=int, default=420)
    parser.add_argument("--bars-chunk-size", type=int, default=120)
    parser.add_argument("--bars-workers", type=int, default=8)
    parser.add_argument("--benchmark-symbol", default="SPY")
    parser.add_argument("--beta-lookback-sessions", type=int, default=252)
    parser.add_argument("--beta-min-observations", type=int, default=126)
    parser.add_argument("--beta-shrinkage-target", type=float, default=1.0)
    parser.add_argument("--beta-shrinkage-strength", type=float, default=0.10)
    parser.add_argument("--beta-clip-low", type=float, default=0.0)
    parser.add_argument("--beta-clip-high", type=float, default=3.0)
    parser.add_argument("--max-price-staleness-days", type=int, default=5)

    parser.add_argument("--sec-user-agent", default="aapricity@sjtu.edu.cn")
    parser.add_argument("--sec-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--sec-max-retries", type=int, default=2)
    parser.add_argument("--sec-max-requests-per-second", type=float, default=10.0)
    parser.add_argument("--sec-sleep-seconds", type=float, default=0.0)
    parser.add_argument("--sec-submissions-workers", type=int, default=10)
    parser.add_argument("--sec-companyfacts-workers", type=int, default=10)
    parser.add_argument("--sec-cache-profile", choices=("live", "backtest"), default="live")
    parser.add_argument("--sec-cache-mode", choices=("network", "prefer", "cache_only", "auto"), default="auto")
    parser.add_argument("--sec-cache-root", default=None)
    parser.add_argument("--sec-ticker-map-cache-path", default=None)
    parser.add_argument("--sec-companyfacts-cache-dir", default=None)
    parser.add_argument("--sec-submissions-cache-dir", default=None)
    parser.add_argument("--sec-refresh-ticker-map", action="store_true")
    parser.add_argument("--sec-refresh-companyfacts", action="store_true")
    parser.add_argument("--sec-refresh-submissions", action="store_true")

    parser.add_argument("--candidate-pool-per-side", type=int, default=120)
    parser.add_argument("--max-single-name-side-weight", type=float, default=1.0 / 30.0)
    parser.add_argument("--min-nonzero-names", type=int, default=20)
    parser.add_argument("--score-weight", type=float, default=0.01)
    parser.add_argument("--sector-penalty", type=float, default=25.0)
    parser.add_argument("--turnover-penalty", type=float, default=0.005)
    parser.add_argument("--turnover-budget", type=float, default=0.15)
    parser.add_argument("--beta-band-grid", default="0.05,0.10,0.15,0.20")

    parser.add_argument("--account-state-path", default=str(DEFAULT_ACCOUNT_STATE_PATH))
    parser.add_argument("--session-idx", type=int, default=None)

    parser.add_argument(
        "--trigger-mode",
        choices=("wait_open", "wait_target_time", "immediate", "plan_only"),
        default="wait_target_time",
    )
    parser.add_argument(
        "--target-ny-time",
        default="10:00",
        help="Target US/Eastern clock time for wait_target_time mode (HH:MM).",
    )
    parser.add_argument("--open-buffer-seconds", type=int, default=5)
    parser.add_argument("--cancel-open-orders-before-submit", action="store_true")
    parser.add_argument("--order-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--order-poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--execution-order-style",
        choices=("marketable_limit", "market"),
        default="marketable_limit",
        help="Order style for live submission.",
    )
    parser.add_argument(
        "--execution-quote-provider",
        choices=("alpaca", "longbridge"),
        default="longbridge",
        help="Real-time quote provider used for sizing and order prices.",
    )
    parser.add_argument(
        "--execution-price-feed",
        default="iex",
        help="Alpaca feed used for intraday bar evidence and when provider=alpaca.",
    )
    parser.add_argument(
        "--longbridge-config-path",
        default=str(DEFAULT_LONGBRIDGE_CONFIG_PATH),
        help="Ignored local JSON containing Longbridge OpenAPI credentials.",
    )
    parser.add_argument("--longbridge-warmup-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--longbridge-max-quote-age-seconds", type=float, default=10.0)
    parser.add_argument("--longbridge-max-spread-bps", type=float, default=150.0)
    parser.add_argument("--longbridge-max-subscriptions", type=int, default=500)
    parser.add_argument(
        "--longbridge-snapshot-contexts",
        type=int,
        default=4,
        help="Maximum independent Longbridge contexts used for snapshot refresh sharding.",
    )
    parser.add_argument(
        "--longbridge-coverage-chunk-size",
        type=int,
        default=500,
        help="Batch size for non-streaming Longbridge candidate-universe coverage checks.",
    )
    parser.add_argument(
        "--audit-benchmark-symbols",
        default="SPY,QQQ,IWM,DIA",
        help="Comma-separated benchmark ETFs included in quote/bar evidence for later market-context attribution.",
    )
    parser.add_argument(
        "--adverse-price-offset-bps",
        type=float,
        default=12.0,
        help=(
            "Default adverse price offset in bps used for conservative share sizing and marketable "
            "limit prices. Buy orders use reference*(1+bps), sell orders use reference*(1-bps)."
        ),
    )
    parser.add_argument(
        "--marketable-limit-base-offset-bps",
        type=float,
        default=None,
        help=(
            "Initial marketable limit offset in bps from reference price. "
            "Defaults to --adverse-price-offset-bps."
        ),
    )
    parser.add_argument(
        "--sizing-adverse-offset-bps",
        type=float,
        default=None,
        help=(
            "Adverse bps applied to reference prices for share sizing and staged_regt buying-power checks. "
            "Defaults to --adverse-price-offset-bps."
        ),
    )
    parser.add_argument(
        "--short-buying-power-adverse-offset-bps",
        type=float,
        default=300.0,
        help=(
            "Adverse bps used to reserve buying power for opening/increasing shorts in staged_regt. "
            "Defaults to 300 bps to mirror Alpaca's short-order buying-power check proxy."
        ),
    )
    parser.add_argument(
        "--marketable-limit-requote-steps-bps",
        default="0,25,75,150",
        help="Additional bps ladder for bounded re-quote attempts, comma-separated.",
    )
    parser.add_argument(
        "--marketable-limit-requote-wait-seconds",
        type=float,
        default=6.0,
        help="How long to wait each limit attempt before cancel/requote.",
    )
    parser.add_argument(
        "--marketable-limit-max-attempts",
        type=int,
        default=4,
        help="Maximum distinct marketable-limit submissions per symbol.",
    )
    parser.add_argument(
        "--execution-workers",
        type=int,
        default=MAX_SAFE_EXECUTION_WORKERS,
        help=(
            "Maximum symbols submitted and tracked concurrently within each execution stage. "
            f"Values above {MAX_SAFE_EXECUTION_WORKERS} are capped to avoid Alpaca rate limits."
        ),
    )
    parser.add_argument(
        "--marketable-limit-max-offset-bps",
        type=float,
        default=150.0,
        help=(
            "Maximum adverse bps for repeated marketable-limit requotes within the order timeout. "
            "Set to 0 to disable the cap."
        ),
    )
    parser.add_argument(
        "--min-trade-notional",
        type=float,
        default=1.0,
        help="Absolute minimum order notional floor used with --min-trade-weight-bps.",
    )
    parser.add_argument(
        "--min-trade-weight-bps",
        type=float,
        default=1.0,
        help="Per-symbol no-trade band in account-equity bps; weight alignment remains the primary objective.",
    )
    parser.add_argument("--whole-shares-only", action="store_true")
    parser.add_argument(
        "--opening-shorts-whole-shares-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Force whole-share qty for opening shorts to satisfy broker constraints. "
            "Enabled by default."
        ),
    )
    parser.add_argument(
        "--short-sales-whole-shares-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Force whole-share qty for any sell order that creates or increases a short "
            "position. Enabled by default for Alpaca fractional short-sale constraints."
        ),
    )
    parser.add_argument(
        "--floor-short-targets-to-whole-shares",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Project target short weights to floor(target short shares) before order "
            "generation. Enabled by default because Alpaca does not support fractional short sales."
        ),
    )
    parser.add_argument("--qty-decimals", type=int, default=4)
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument(
        "--order-plan-input-path",
        default=None,
        help=(
            "Optional path to an existing order_plan.json. "
            "When provided, skip DynamicSymbolPool/AlphaCore/DecisionEngine and execute from this plan."
        ),
    )
    parser.add_argument(
        "--decision-targets-input-path",
        default=None,
        help=(
            "Optional path to a DecisionEngine target CSV. "
            "When provided, skip DynamicSymbolPool/AlphaCore/DecisionEngine and rebuild orders from "
            "the target signed weights using fresh broker state/prices."
        ),
    )
    parser.add_argument(
        "--alpha-panel-input-path",
        default=None,
        help=(
            "Optional path to a same-session AlphaCore panel CSV. When provided, "
            "reuse the cached alpha/universe data but rerun DecisionEngine from fresh "
            "broker positions before rebuilding executable targets."
        ),
    )
    parser.add_argument(
        "--position-continuity-reference-path",
        default=None,
        help=(
            "Optional prior broker_positions_after_raw.json used to detect unexplained "
            "position quantity changes before decision or execution."
        ),
    )
    parser.add_argument(
        "--position-continuity-mode",
        choices=("off", "audit", "strict"),
        default="off",
        help=(
            "off disables cross-snapshot checks; audit records differences; strict "
            "fails closed on a missing reference, unstable current quantities, or any "
            "per-symbol quantity drift."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        choices=("single_pass", "staged_regt"),
        default="single_pass",
        help=(
            "single_pass submits all generated orders in notional order. "
            "staged_regt first submits reducing/closing legs, refreshes broker state, then rebuilds "
            "and submits increasing/opening legs under a conservative buying-power cap."
        ),
    )
    parser.add_argument(
        "--entry-buying-power-buffer",
        "--buying-power-buffer",
        dest="buying_power_buffer",
        type=float,
        default=0.95,
        help="Broker-feasibility fraction of fresh buying power available to new/increasing legs.",
    )
    parser.add_argument(
        "--gross-capacity-target-ratio",
        type=float,
        default=0.95,
        help="Target final gross position as a fraction of reconstructed total RegT capacity.",
    )
    parser.add_argument(
        "--staged-release-timeout-seconds",
        type=float,
        default=None,
        help="Optional order timeout for staged_regt release legs. Defaults to --order-timeout-seconds.",
    )
    parser.add_argument(
        "--staged-entry-timeout-seconds",
        type=float,
        default=None,
        help="Optional order timeout for staged_regt entry legs. Defaults to --order-timeout-seconds.",
    )
    parser.add_argument(
        "--staged-entry-repair-rounds",
        type=int,
        default=1,
        help="Residual entry repair rounds after final positions reconcile.",
    )
    parser.add_argument(
        "--staged-entry-repair-max-attempts",
        type=int,
        default=1,
        help="Maximum aggressive limit attempts per symbol in each entry repair round.",
    )
    parser.add_argument(
        "--staged-entry-repair-wait-seconds",
        type=float,
        default=10.0,
        help="Seconds to wait for each final entry repair limit before canceling it.",
    )
    parser.add_argument(
        "--staged-release-max-rounds",
        type=int,
        default=3,
        help="Maximum rebuild/retry rounds for staged_regt release legs before aborting entry.",
    )
    parser.add_argument(
        "--staged-release-round-extra-bps",
        type=float,
        default=25.0,
        help="Additional marketable-limit bps added per staged_regt release retry round.",
    )
    parser.add_argument(
        "--staged-release-round-sleep-seconds",
        type=float,
        default=2.0,
        help="Seconds to wait between staged_regt release retry rounds.",
    )

    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv)

    # Validate feed choices - SIP is required for 1000-symbol universe
    if str(args.feed).lower() != "sip":
        print(
            f"[WARNING] --feed={args.feed} detected. For 1000-symbol universe, SIP is required.\n"
            f"          IEX covers only ~2-3% of market volume and will miss many stocks.\n"
            f"          Recommend: --feed sip (default)",
            flush=True,
        )
    if str(args.dynamic_feed).lower() != "sip":
        print(
            f"[WARNING] --dynamic-feed={args.dynamic_feed} detected. For 1000-symbol universe, SIP is required.\n"
            f"          IEX will cause symbol pool filtering to fail on missing data.\n"
            f"          Recommend: --dynamic-feed sip (default)",
            flush=True,
        )

    execution_quote_client: Any | None = None
    symbol_universe_quote_client: LongbridgeQuoteClient | None = None
    try:
        decision_date = _normalize_date(args.date)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = (
            Path(args.output_root).resolve()
            if args.output_root
            else (DEFAULT_OUTPUT_ROOT / f"{decision_date.strftime('%Y%m%d')}_{stamp}").resolve()
        )
        output_root.mkdir(parents=True, exist_ok=True)
        should_submit = not bool(args.no_submit) and str(args.trigger_mode) != "plan_only"
        run_started_at_utc = _utc_now()
        run_started_monotonic = time.monotonic()
        run_events = _PersistentRunEvents(
            path=output_root / "run_events.jsonl",
            run_started_monotonic=run_started_monotonic,
        )
        run_context_path = output_root / "run_context.json"
        phase_timings = _DecisionPhaseTimingRecorder(
            output_root=output_root,
            run_started_at_utc=run_started_at_utc,
            run_started_monotonic=run_started_monotonic,
        )
        phase_timings.start(
            "startup_evidence",
            {
                "submit_enabled": bool(should_submit),
                "trigger_mode": str(args.trigger_mode),
            },
        )
        _mark_event(
            run_events,
            "executor_started",
            {"output_root": output_root.as_posix(), "submit_enabled": bool(should_submit)},
        )
        _write_json_file(
            run_context_path,
            _build_run_context(
                args=args,
                argv=argv,
                decision_date=decision_date,
                output_root=output_root,
                should_submit=should_submit,
                run_started_at_utc=run_started_at_utc,
                events=run_events,
            ),
        )
        _write_json_file(output_root / "source_code_manifest.json", _source_code_manifest(PROJECT_ROOT))
        _write_source_git_evidence(output_root=output_root, project_root=PROJECT_ROOT)
        _write_source_code_snapshot(output_root=output_root, project_root=PROJECT_ROOT)
        _write_json_file(output_root / "python_environment.json", _python_environment_snapshot())
        _write_runtime_environment_snapshot(output_root)
        _write_run_events(output_root, run_events)
        phase_timings.finish("startup_evidence")
        phase_timings.start(
            "broker_preflight_and_state",
            {"account_name": str(args.account_name)},
        )

        credentials = _resolve_alpaca_credentials(
            accounts_json_path=str(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url=str(args.data_base_url),
            request_timeout_seconds=float(args.request_timeout_seconds),
            max_retries=int(args.max_retries),
        )
        client = AlpacaHttpClient(credentials)
        alpaca_api_audit_path = output_root / "alpaca_api_audit.jsonl"
        client.set_audit_log_path(alpaca_api_audit_path)
        _mark_event(run_events, "alpaca_api_audit_enabled", {"path": alpaca_api_audit_path.as_posix()})

        broker_calendar_window_path = output_root / "broker_calendar_window.json"
        broker_calendar_window = _collect_calendar_window(client=client, session_date=decision_date)
        _write_json_file(broker_calendar_window_path, broker_calendar_window)
        _mark_event(
            run_events,
            "broker_calendar_window_collected",
            {
                "path": broker_calendar_window_path.as_posix(),
                "ok": bool(broker_calendar_window.get("ok")),
                "row_count": len(broker_calendar_window.get("payload", {}).get("rows", []))
                if isinstance(broker_calendar_window.get("payload"), dict)
                else None,
            },
        )

        broker_clock_before = _safe_broker_call("get_clock_before", client.get_clock)
        _write_json_file(output_root / "broker_clock_before.json", broker_clock_before)
        _write_json_file(
            output_root / "broker_portfolio_history_before.json",
            _collect_portfolio_history_snapshot(client=client, session_date=decision_date, label="before"),
        )
        broker_open_orders_before = _safe_broker_call(
            "list_open_orders_before",
            lambda: client.list_orders(status="open", limit=500, direction="desc", nested=False),
        )
        _write_json_file(output_root / "broker_open_orders_before.json", broker_open_orders_before)
        _write_json_file(
            output_root / "broker_orders_all_before.json",
            _safe_broker_call(
                "list_orders_all_before",
                lambda: client.list_orders_all_pages(status="all", limit=500, direction="desc", nested=False),
            ),
        )
        account_before_initial = client.get_account()
        _write_json_file(
            output_root / "broker_account_configurations_before.json",
            _safe_broker_call("get_account_configurations_before", client.get_account_configurations),
        )

        positions_before_initial = client.list_positions()
        position_account_stability_before = _collect_position_account_stability(
            client=client,
            initial_positions=positions_before_initial,
            initial_account=account_before_initial,
            sample_count=3,
            sleep_seconds=1.0,
        )
        _write_json_file(output_root / "broker_position_account_stability_before.json", position_account_stability_before)
        positions_before = list(
            _latest_stability_payload(
                position_account_stability_before,
                payload_key="positions_payload",
                fallback=positions_before_initial,
            )
        )
        account_before = dict(
            _latest_stability_payload(
                position_account_stability_before,
                payload_key="account_payload",
                fallback=account_before_initial,
            )
        )
        positions_before_captured_at_utc = _latest_stability_collected_at(
            position_account_stability_before,
            payload_key="positions_payload",
        ) or _utc_now()
        account_before_captured_at_utc = _latest_stability_collected_at(
            position_account_stability_before,
            payload_key="account_payload",
        ) or positions_before_captured_at_utc
        _write_json_file(output_root / "broker_positions_before_raw.json", positions_before)
        _write_json_file(output_root / "broker_account_before.json", account_before)
        shorting_enabled = bool(account_before.get("shorting_enabled", True))

        position_continuity_guard = _build_position_continuity_guard(
            reference_path=(
                Path(str(args.position_continuity_reference_path)).resolve()
                if args.position_continuity_reference_path
                else None
            ),
            current_positions=positions_before,
            current_stability=position_account_stability_before,
            mode=str(args.position_continuity_mode),
            qty_decimals=int(args.qty_decimals),
        )
        position_continuity_guard_path = output_root / "position_continuity_guard.json"
        _write_json_file(position_continuity_guard_path, position_continuity_guard)
        _mark_event(
            run_events,
            "position_continuity_checked",
            {
                "status": position_continuity_guard.get("status"),
                "mode": position_continuity_guard.get("mode"),
                "drift_symbol_count": position_continuity_guard.get("drift_symbol_count"),
                "current_quantity_stable": position_continuity_guard.get("current_quantity_stable"),
                "reference_path": position_continuity_guard.get("reference_path"),
            },
        )
        if position_continuity_guard.get("status") == "blocked":
            raise RuntimeError(
                "Position continuity guard blocked the run: "
                f"reasons={position_continuity_guard.get('blocking_reasons')} "
                f"symbols={position_continuity_guard.get('drift_symbols')}"
            )

        if should_submit:
            day_open_snapshot_path = output_root / "broker_day_open_snapshot.json"
            day_open_snapshot_created = _write_json_file_if_absent(
                day_open_snapshot_path,
                {
                    "schema_version": "1.0",
                    "session_date": decision_date.isoformat(),
                    "captured_at_utc": positions_before_captured_at_utc,
                    "account_captured_at_utc": account_before_captured_at_utc,
                    "capture_semantics": "first_submit_enabled_executor_preflight_for_session",
                    "account": account_before,
                    "positions": positions_before,
                },
            )
            _mark_event(
                run_events,
                "broker_day_open_snapshot_bound",
                {
                    "path": day_open_snapshot_path.as_posix(),
                    "created": day_open_snapshot_created,
                    "position_count": len(positions_before),
                },
            )
        broker_frame_before, broker_signed_notional_before = _positions_to_frame_and_notional(positions_before)
        broker_signed_qty_before = _signed_qty_from_positions(positions_before)
        _mark_event(
            run_events,
            "broker_state_before_loaded",
            {
                "position_count": len(positions_before),
                "open_order_count": len(broker_open_orders_before.get("payload", []))
                if broker_open_orders_before.get("ok")
                else None,
                "stability_position_hash_count": position_account_stability_before.get("position_hash_count"),
                "stability_account_hash_count": position_account_stability_before.get("account_hash_count"),
            },
        )
        equity_before, equity_before_source = _resolve_account_equity(
            account=account_before,
            signed_notional=broker_signed_notional_before,
        )
        broker_weights_before = _weights_from_signed_notional(
            broker_signed_notional_before,
            equity=equity_before,
        )

        account_state_path = Path(args.account_state_path).resolve()
        account_state_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(
            output_root / "input_file_manifest.json",
            _input_file_manifest(args, account_state_path),
        )
        account_state = _load_json_dict(account_state_path)
        _write_json_file(output_root / "account_state_before.json", account_state)
        resolved_session_idx = _resolve_session_idx(
            account_state,
            args.session_idx,
            session_date=decision_date.isoformat(),
        )
        account_state.setdefault("schema_version", "1.0")
        account_state.setdefault("lifecycle_epoch", 1)
        account_state.setdefault("initial_equity", float(equity_before))
        account_state.setdefault("initial_cash", float(_safe_float(account_before.get("cash")) or equity_before))
        account_state.update(
            {
                "broker_account_id": str(account_before.get("id") or account_before.get("account_id") or ""),
                "broker_account_number": str(account_before.get("account_number") or ""),
                "last_session_idx": int(resolved_session_idx),
                "last_session_date": decision_date.isoformat(),
                "executor_last_run_started_at_utc": run_started_at_utc,
                "executor_last_submit_enabled": bool(should_submit),
                "executor_last_broker_equity": float(equity_before),
                "executor_last_broker_equity_source": str(equity_before_source),
            }
        )
        _write_json_file(account_state_path, account_state)
        phase_timings.finish(
            "broker_preflight_and_state",
            {
                "position_count": len(positions_before),
                "shorting_enabled": bool(shorting_enabled),
                "session_idx": int(resolved_session_idx),
            },
        )
        alpha_panel = pd.DataFrame()
        alpha_path: Path | None = None
        decision_targets_path: Path | None = None
        plan_input_path: str | None = None
        decision_status = "ok"
        decision_skip_reason: str | None = None
        decision_diagnostics: Mapping[str, Any] = {}
        sec_cache_source = "runtime"
        symbols: list[str] = []
        target_signed_weights: dict[str, float] = {}
        symbol_universe_snapshot: dict[str, Any] = {}
        symbol_universe_json_path = output_root / "symbol_universe_intersection.json"
        symbol_universe_csv_path = output_root / "symbol_universe_intersection.csv"
        source_universe_input_path: Path | None = None
        alpha_cache_provenance_path: Path | None = None

        strategy_inputs = [
            value
            for value in (
                args.order_plan_input_path,
                args.decision_targets_input_path,
                args.alpha_panel_input_path,
            )
            if value
        ]
        if len(strategy_inputs) > 1:
            raise ValueError(
                "Provide only one of --order-plan-input-path, "
                "--decision-targets-input-path, or --alpha-panel-input-path."
            )

        if args.order_plan_input_path:
            phase_timings.skip("dynamic_symbol_pool", {"reason": "order_plan_input"})
            phase_timings.skip("sec_industry_map", {"reason": "order_plan_input"})
            phase_timings.skip("alpha_core_build", {"reason": "order_plan_input"})
            phase_timings.start("portfolio_decision", {"source": "order_plan_input"})
            loaded_plan = _load_json_dict(Path(str(args.order_plan_input_path)).resolve())
            plan_input_path = str(Path(str(args.order_plan_input_path)).resolve().as_posix())
            source_universe_input_path = Path(str(args.order_plan_input_path)).resolve()
            target_signed_weights = _extract_target_signed_weights_from_plan(loaded_plan)
            if not target_signed_weights:
                raise ValueError(
                    "order-plan-input-path is provided but target_signed_weights cannot be resolved from the plan."
                )
            symbols = sorted(target_signed_weights)
            decision_status = str(loaded_plan.get("decision_status") or "loaded_plan")
            skip_reason_raw = loaded_plan.get("decision_skip_reason")
            decision_skip_reason = None if skip_reason_raw in (None, "", "null") else str(skip_reason_raw)
            diag_raw = loaded_plan.get("decision_diagnostics")
            decision_diagnostics = dict(diag_raw) if isinstance(diag_raw, Mapping) else {}
            sec_cache_source = str(loaded_plan.get("sec_cache_source") or "from_order_plan")
            decision_targets_path = output_root / "decision_targets.csv"
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
            phase_timings.finish(
                "portfolio_decision",
                {
                    "decision_status": decision_status,
                    "target_symbol_count": len(target_signed_weights),
                },
            )
        elif args.decision_targets_input_path:
            phase_timings.skip("dynamic_symbol_pool", {"reason": "decision_targets_input"})
            phase_timings.skip("sec_industry_map", {"reason": "decision_targets_input"})
            phase_timings.skip("alpha_core_build", {"reason": "decision_targets_input"})
            phase_timings.start("portfolio_decision", {"source": "decision_targets_input"})
            source_path = Path(str(args.decision_targets_input_path)).resolve()
            source_universe_input_path = source_path
            target_signed_weights = _load_target_signed_weights_from_csv(source_path)
            if not target_signed_weights:
                raise ValueError(
                    "decision-targets-input-path is provided but no target signed weights were resolved."
                )
            symbols = sorted(target_signed_weights)
            plan_input_path = None
            decision_status = "loaded_targets"
            decision_skip_reason = None
            decision_diagnostics = {
                "source": "decision_targets_input_path",
                "target_symbol_count": int(len(target_signed_weights)),
                "source_path": source_path.as_posix(),
            }
            sec_cache_source = "from_decision_targets"
            decision_targets_path = output_root / "decision_targets.csv"
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
            phase_timings.finish(
                "portfolio_decision",
                {
                    "decision_status": decision_status,
                    "target_symbol_count": len(target_signed_weights),
                },
            )
        elif args.alpha_panel_input_path:
            phase_timings.skip("dynamic_symbol_pool", {"reason": "alpha_panel_input"})
            phase_timings.skip("sec_industry_map", {"reason": "alpha_panel_input"})
            phase_timings.skip("alpha_core_build", {"reason": "alpha_panel_input"})
            phase_timings.start("portfolio_decision", {"source": "alpha_panel_input"})
            source_path = Path(str(args.alpha_panel_input_path)).resolve()
            source_universe_input_path = source_path
            alpha_panel = _load_cached_alpha_panel(source_path, decision_date)
            normalized_symbols = alpha_panel["symbol"]
            symbols = sorted(normalized_symbols.tolist())
            sec_cache_source = "from_alpha_panel"

            decision_broker_weights = dict(broker_weights_before)
            decision_position_mark_snapshot: dict[str, Any] = {
                "schema_version": "1.0",
                "generated_at_utc": _utc_now(),
                "provider": "alpaca_position_market_value",
                "equity": float(equity_before),
                "signed_qty_by_symbol": dict(sorted(broker_signed_qty_before.items())),
                "reference_prices": {},
                "signed_notional_by_symbol": dict(sorted(broker_signed_notional_before.items())),
                "signed_weights_by_symbol": dict(sorted(decision_broker_weights.items())),
            }
            held_symbols = sorted(broker_signed_qty_before)
            if str(args.execution_quote_provider).lower() == "longbridge" and held_symbols:
                decision_mark_client = _new_longbridge_quote_client(args)
                try:
                    decision_mark_health = decision_mark_client.start(held_symbols)
                    decision_mark_prices = _resolve_reference_prices(
                        client=decision_mark_client,
                        symbols=held_symbols,
                        fallback_prices={},
                        feed=str(decision_mark_client.feed_name),
                        prefer_live=True,
                        allow_fallback=False,
                        require_fresh=True,
                    )
                    missing_decision_marks = sorted(set(held_symbols) - set(decision_mark_prices))
                    if missing_decision_marks:
                        raise LongbridgeQuoteError(
                            "Longbridge is missing fresh marks for current broker positions: "
                            + ", ".join(missing_decision_marks)
                        )
                    decision_signed_notional = {
                        symbol: float(qty) * float(decision_mark_prices[symbol])
                        for symbol, qty in broker_signed_qty_before.items()
                    }
                    decision_broker_weights = _weights_from_signed_notional(
                        decision_signed_notional,
                        equity=equity_before,
                    )
                    decision_position_mark_snapshot.update(
                        {
                            "generated_at_utc": _utc_now(),
                            "provider": "longbridge",
                            "feed": str(decision_mark_client.feed_name),
                            "provider_health": decision_mark_health,
                            "reference_prices": dict(sorted(decision_mark_prices.items())),
                            "signed_notional_by_symbol": dict(sorted(decision_signed_notional.items())),
                            "signed_weights_by_symbol": dict(sorted(decision_broker_weights.items())),
                        }
                    )
                finally:
                    decision_mark_client.close()
            _write_json_file(
                output_root / "decision_position_mark_snapshot.json",
                decision_position_mark_snapshot,
            )

            decision_config = DecisionConfig(
                factor_weights=dict(DEFAULT_FACTOR_WEIGHTS),
                candidate_pool_per_side=int(args.candidate_pool_per_side),
                max_single_name_side_weight=float(args.max_single_name_side_weight),
                min_nonzero_names=int(args.min_nonzero_names),
                score_weight=float(args.score_weight),
                sector_penalty=float(args.sector_penalty),
                turnover_penalty=float(args.turnover_penalty),
                turnover_budget=float(args.turnover_budget),
                beta_band_grid=tuple(_parse_float_list(str(args.beta_band_grid))),
            )
            engine = DecisionEngine(decision_config)
            decision_result = engine.decide(
                alpha_frame=alpha_panel,
                previous_weights=_split_signed_weights(decision_broker_weights),
                session_idx=int(resolved_session_idx),
                session_date=decision_date.isoformat(),
            )
            decision_status = str(decision_result.status)
            decision_skip_reason = (
                None if decision_result.skip_reason in (None, "", "null") else str(decision_result.skip_reason)
            )
            decision_diagnostics = dict(decision_result.diagnostics)
            decision_diagnostics["alpha_cache_source"] = source_path.as_posix()
            decision_diagnostics["alpha_cache_sha256"] = _sha256_file(source_path)
            decision_diagnostics["previous_weight_mark_provider"] = decision_position_mark_snapshot.get(
                "provider"
            )

            alpha_path = output_root / f"alpha_core_panel_{decision_date.strftime('%Y%m%d')}.csv"
            alpha_panel.to_csv(alpha_path, index=False)
            alpha_cache_provenance_path = output_root / "alpha_cache_provenance.json"
            _write_json_file(
                alpha_cache_provenance_path,
                {
                    "schema_version": "1.0",
                    "generated_at_utc": _utc_now(),
                    "source_path": source_path.as_posix(),
                    "source_sha256": _sha256_file(source_path),
                    "copied_path": alpha_path.as_posix(),
                    "copied_sha256": _sha256_file(alpha_path),
                    "session_date": decision_date.isoformat(),
                    "row_count": len(alpha_panel),
                    "column_count": len(alpha_panel.columns),
                    "symbol_count": len(symbols),
                    "position_continuity_reference_path": position_continuity_guard.get("reference_path"),
                    "position_continuity_status": position_continuity_guard.get("status"),
                },
            )
            decision_targets_path = output_root / "decision_targets.csv"
            target_signed_weights = _signed_weights_from_decision_targets(decision_result.targets)
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
            phase_timings.finish(
                "portfolio_decision",
                {
                    "decision_status": decision_status,
                    "decision_skip_reason": decision_skip_reason,
                    "target_symbol_count": len(target_signed_weights),
                    "alpha_row_count": len(alpha_panel),
                    "alpha_source_path": source_path.as_posix(),
                    "alpha_source_sha256": _sha256_file(source_path),
                },
            )
        else:
            phase_timings.start(
                "dynamic_symbol_pool",
                {
                    "pool_size": int(args.pool_size),
                    "feed": str(args.dynamic_feed),
                    "workers": int(args.dynamic_bars_workers),
                },
            )
            candidate_symbols_path = Path(args.candidate_symbols_path).resolve()
            candidate_symbols = _load_candidate_symbols(candidate_symbols_path)
            alpaca_universe_assets = client.list_assets(status="active", asset_class="us_equity")
            symbol_universe_quote_client = _new_longbridge_quote_client(args)
            longbridge_coverage = symbol_universe_quote_client.check_symbol_coverage(
                candidate_symbols,
                chunk_size=int(args.longbridge_coverage_chunk_size),
            )
            symbol_universe_quote_client.close()
            symbol_universe_quote_client = None
            symbol_universe_snapshot = _build_decision_symbol_universe_snapshot(
                candidate_symbols_path=candidate_symbols_path,
                candidate_symbols=candidate_symbols,
                alpaca_assets=alpaca_universe_assets,
                longbridge_coverage=longbridge_coverage,
                decision_date=decision_date,
            )
            _write_symbol_universe_artifacts(output_root, symbol_universe_snapshot)
            if symbol_universe_snapshot.get("status") != "pass":
                raise LongbridgeQuoteError(
                    "Unable to build a complete non-empty Alpaca/Longbridge symbol-universe "
                    f"intersection: status={symbol_universe_snapshot.get('status')}"
                )
            intersection_candidates = list(
                symbol_universe_snapshot.get("final_intersection_symbols") or []
            )
            pool = DynamicSymbolPool(
                client=client,
                candidate_symbols=intersection_candidates,
                pool_size=int(args.pool_size),
                lookback_sessions=int(args.lookback_sessions),
                min_observations=int(args.min_observations),
                price_floor=float(args.price_floor),
                bars_window_calendar_days=int(args.dynamic_bars_window_calendar_days),
                bars_chunk_size=int(args.dynamic_bars_chunk_size),
                bars_workers=int(args.dynamic_bars_workers),
                feed=str(args.dynamic_feed),
                beta_full_observations=int(args.dynamic_beta_full_observations),
            )
            symbols = sorted(
                pool.fresh(decision_date.isoformat(), assets=alpaca_universe_assets)
            )
            if not symbols:
                raise ValueError("DynamicSymbolPool returned empty symbol list.")
            symbol_universe_snapshot.update(
                {
                    "dynamic_pool_diagnostics": asdict(pool.last_diagnostics)
                    if pool.last_diagnostics is not None
                    else None,
                    "dynamic_selected_count": len(symbols),
                    "dynamic_selected_symbols": symbols,
                }
            )
            _write_symbol_universe_artifacts(output_root, symbol_universe_snapshot)
            phase_timings.finish(
                "dynamic_symbol_pool",
                {
                    "candidate_symbol_count": len(candidate_symbols),
                    "intersection_candidate_symbol_count": len(intersection_candidates),
                    "selected_symbol_count": len(symbols),
                },
            )

            phase_timings.start("sec_industry_map", {"symbol_count": len(symbols)})
            sec_cache_mode = str(args.sec_cache_mode).strip().lower()
            if sec_cache_mode == "auto":
                sec_cache_mode = "prefer" if str(args.sec_cache_profile) == "backtest" else "network"
            ticker_map_cache_path, companyfacts_cache_dir, submissions_cache_dir, sec_cache_source = _resolve_sec_cache_paths(
                sec_cache_profile=str(args.sec_cache_profile),
                sec_cache_root=str(args.sec_cache_root) if args.sec_cache_root else None,
                ticker_map_cache_path=str(args.sec_ticker_map_cache_path) if args.sec_ticker_map_cache_path else None,
                companyfacts_cache_dir=str(args.sec_companyfacts_cache_dir) if args.sec_companyfacts_cache_dir else None,
                submissions_cache_dir=str(args.sec_submissions_cache_dir) if args.sec_submissions_cache_dir else None,
            )

            sec_client = SecApiClient(
                user_agent=str(args.sec_user_agent),
                timeout_seconds=float(args.sec_timeout_seconds),
                max_retries=int(args.sec_max_retries),
                max_requests_per_second=float(args.sec_max_requests_per_second),
                ticker_map_cache_path=ticker_map_cache_path,
                companyfacts_cache_dir=companyfacts_cache_dir,
                submissions_cache_dir=submissions_cache_dir,
                refresh_ticker_map=bool(args.sec_refresh_ticker_map),
                refresh_companyfacts=bool(args.sec_refresh_companyfacts),
                refresh_submissions=bool(args.sec_refresh_submissions),
                sleep_seconds=float(args.sec_sleep_seconds),
                cache_mode=sec_cache_mode,
                memory_cache_enabled=True,
            )

            industry_map = _resolve_industry_map_for_symbols(
                symbols=symbols,
                sec_client=sec_client,
                industry_cache_output_path=output_root / "industry_map_dynamic.csv",
                submissions_workers=int(args.sec_submissions_workers),
            )
            phase_timings.finish(
                "sec_industry_map",
                {
                    "symbol_count": len(symbols),
                    "industry_record_count": len(industry_map),
                    "cache_mode": sec_cache_mode,
                    "cache_source": sec_cache_source,
                    "submissions_workers": int(args.sec_submissions_workers),
                },
            )
            phase_timings.start(
                "alpha_core_build",
                {
                    "symbol_count": len(symbols),
                    "feed": str(args.feed),
                    "companyfacts_workers": int(args.sec_companyfacts_workers),
                    "sec_cache_mode": sec_cache_mode,
                },
            )
            alpha_core = AlphaCore(
                alpaca_client=client,
                sec_client=sec_client,
                industry_map=industry_map,
                sec_submissions_workers=int(args.sec_submissions_workers),
                sec_companyfacts_workers=int(args.sec_companyfacts_workers),
                feed=str(args.feed),
                price_adjustment=str(args.price_adjustment),
                bars_window_calendar_days=int(args.bars_window_calendar_days),
                bars_chunk_size=int(args.bars_chunk_size),
                bars_workers=int(args.bars_workers),
                benchmark_symbol=str(args.benchmark_symbol),
                beta_lookback_sessions=int(args.beta_lookback_sessions),
                beta_min_observations=int(args.beta_min_observations),
                beta_shrinkage_target=float(args.beta_shrinkage_target),
                beta_shrinkage_strength=float(args.beta_shrinkage_strength),
                beta_clip_low=float(args.beta_clip_low) if args.beta_clip_low is not None else None,
                beta_clip_high=float(args.beta_clip_high) if args.beta_clip_high is not None else None,
                max_price_staleness_days=int(args.max_price_staleness_days),
                factor_weights=DEFAULT_FACTOR_WEIGHTS,
            )
            alpha_panel = alpha_core.build_for_date(as_of_date=decision_date.isoformat(), symbols=symbols)
            alpha_path = output_root / f"alpha_core_panel_{decision_date.strftime('%Y%m%d')}.csv"
            alpha_panel.to_csv(alpha_path, index=False)
            phase_timings.finish(
                "alpha_core_build",
                {
                    "alpha_row_count": len(alpha_panel),
                    "alpha_column_count": len(alpha_panel.columns),
                    "sec_cache_source": sec_cache_source,
                },
            )

            phase_timings.start(
                "portfolio_decision",
                {
                    "source": "decision_engine",
                    "alpha_row_count": len(alpha_panel),
                },
            )
            decision_config = DecisionConfig(
                factor_weights=dict(DEFAULT_FACTOR_WEIGHTS),
                candidate_pool_per_side=int(args.candidate_pool_per_side),
                max_single_name_side_weight=float(args.max_single_name_side_weight),
                min_nonzero_names=int(args.min_nonzero_names),
                score_weight=float(args.score_weight),
                sector_penalty=float(args.sector_penalty),
                turnover_penalty=float(args.turnover_penalty),
                turnover_budget=float(args.turnover_budget),
                beta_band_grid=tuple(_parse_float_list(str(args.beta_band_grid))),
            )
            engine = DecisionEngine(decision_config)
            decision_result = engine.decide(
                alpha_frame=alpha_panel,
                previous_weights=_split_signed_weights(broker_weights_before),
                session_idx=int(resolved_session_idx),
                session_date=decision_date.isoformat(),
            )
            decision_status = str(decision_result.status)
            decision_skip_reason = (
                None if decision_result.skip_reason in (None, "", "null") else str(decision_result.skip_reason)
            )
            decision_diagnostics = dict(decision_result.diagnostics)

            decision_targets_path = output_root / "decision_targets.csv"
            target_signed_weights = _signed_weights_from_decision_targets(decision_result.targets)
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
            phase_timings.finish(
                "portfolio_decision",
                {
                    "decision_status": decision_status,
                    "decision_skip_reason": decision_skip_reason,
                    "target_symbol_count": len(target_signed_weights),
                },
            )

        if source_universe_input_path is not None:
            decision_universe_path = (
                source_universe_input_path.parent / "symbol_universe_intersection.json"
            )
            decision_universe = _load_json_dict(decision_universe_path)
            decision_final_symbols = [
                str(symbol).strip().upper()
                for symbol in (decision_universe.get("final_intersection_symbols") or [])
                if str(symbol).strip()
            ]
            coverage_symbols = sorted(
                set(decision_final_symbols)
                | set(target_signed_weights)
                | set(broker_signed_notional_before)
            )
            symbol_universe_quote_client = _new_longbridge_quote_client(args)
            current_longbridge_coverage = symbol_universe_quote_client.check_symbol_coverage(
                coverage_symbols,
                chunk_size=int(args.longbridge_coverage_chunk_size),
            )
            symbol_universe_snapshot = _build_execution_symbol_universe_snapshot(
                decision_snapshot_path=decision_universe_path,
                target_signed_weights=target_signed_weights,
                broker_weights=broker_weights_before,
                current_longbridge_coverage=current_longbridge_coverage,
                decision_date=decision_date,
            )
            _write_symbol_universe_artifacts(output_root, symbol_universe_snapshot)
            if symbol_universe_snapshot.get("status") != "pass":
                raise LongbridgeQuoteError(
                    "Decision/execute symbol-universe validation failed: "
                    f"blocking_symbols={symbol_universe_snapshot.get('blocking_symbols')}, "
                    f"coverage_errors={current_longbridge_coverage.get('errors')}"
                )
            if str(args.execution_quote_provider).lower() == "longbridge":
                execution_quote_client = symbol_universe_quote_client
                symbol_universe_quote_client = None
            else:
                symbol_universe_quote_client.close()
                symbol_universe_quote_client = None
        elif symbol_universe_snapshot:
            target_scope = _target_scope_assessment(
                target_signed_weights=target_signed_weights,
                broker_weights=broker_weights_before,
                strategy_symbols=symbol_universe_snapshot.get("final_intersection_symbols") or [],
            )
            symbol_universe_snapshot["target_scope"] = target_scope
            symbol_universe_snapshot["status"] = (
                "error" if target_scope.get("status") != "pass" else symbol_universe_snapshot.get("status")
            )
            _write_symbol_universe_artifacts(output_root, symbol_universe_snapshot)
            if target_scope.get("status") != "pass":
                raise ValueError(
                    "Decision produced out-of-intersection target exposure: "
                    f"{target_scope.get('invalid_target_scope_symbols')}"
                )
            if symbol_universe_quote_client is not None:
                symbol_universe_quote_client.close()
                symbol_universe_quote_client = None
        _mark_event(
            run_events,
            "decision_targets_resolved",
            {
                "decision_status": decision_status,
                "target_symbol_count": len(target_signed_weights),
                "decision_targets_path": decision_targets_path.as_posix() if decision_targets_path else None,
            },
        )
        phase_timings.start(
            "market_and_price_evidence",
            {"target_symbol_count": len(target_signed_weights)},
        )
        assets = client.list_assets(status="active", asset_class="us_equity")
        _write_json_file(
            output_root / "broker_assets_active_us_equity.json",
            {
                "collected_at_utc": _utc_now(),
                "count": len(assets),
                "assets": assets,
            },
        )
        assets_by_symbol = {
            str(asset.get("symbol") or "").strip().upper(): asset
            for asset in assets
            if isinstance(asset, Mapping) and str(asset.get("symbol") or "").strip()
        }

        fallback_prices = _build_fallback_price_map(
            alpha_panel=alpha_panel,
            broker_positions=broker_frame_before,
        )
        reference_price_symbols = sorted(set(target_signed_weights) | set(broker_signed_notional_before))
        benchmark_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in [str(args.benchmark_symbol), *_parse_symbol_list(str(args.audit_benchmark_symbols or ""))]
                if str(symbol or "").strip()
            }
        )
        audit_price_symbols = sorted(set(reference_price_symbols) | set(benchmark_symbols))
        configured_quote_provider = str(args.execution_quote_provider).lower()
        execution_input_run = bool(
            str(args.decision_targets_input_path or "").strip()
            or str(args.order_plan_input_path or "").strip()
            or str(args.alpha_panel_input_path or "").strip()
        )
        active_quote_provider = (
            configured_quote_provider if should_submit or execution_input_run else "alpaca"
        )
        if active_quote_provider == "longbridge":
            if not isinstance(execution_quote_client, LongbridgeQuoteClient):
                execution_quote_client = _new_longbridge_quote_client(args)
            quote_provider_health = execution_quote_client.start(audit_price_symbols)
        else:
            execution_quote_client = client
            quote_provider_health = {
                "schema_version": "1.0",
                "collected_at_utc": _utc_now(),
                "provider": "alpaca",
                "feed": str(args.execution_price_feed),
                "configured_provider": configured_quote_provider,
                "active_provider_reason": (
                    "submission_disabled" if configured_quote_provider != "alpaca" else "configured"
                ),
                "requested_symbol_count": len(audit_price_symbols),
                "status": "pass",
            }
        quote_provider_health.update(
            {
                "configured_provider": configured_quote_provider,
                "active_provider": active_quote_provider,
                "submission_enabled": bool(should_submit),
                "execution_input_run": bool(execution_input_run),
            }
        )
        _write_json_file(output_root / "execution_quote_provider_health.json", quote_provider_health)
        _mark_event(
            run_events,
            "execution_quote_provider_ready",
            {
                "configured_provider": configured_quote_provider,
                "active_provider": active_quote_provider,
                "feed": quote_provider_health.get("feed"),
                "status": quote_provider_health.get("status"),
                "requested_symbol_count": len(audit_price_symbols),
            },
        )
        execution_quote_feed = str(
            getattr(execution_quote_client, "feed_name", None) or args.execution_price_feed
        )
        reference_prices = _resolve_reference_prices(
            client=execution_quote_client,
            symbols=reference_price_symbols,
            fallback_prices=fallback_prices,
            feed=execution_quote_feed,
            prefer_live=True,
            allow_fallback=active_quote_provider != "longbridge",
            require_fresh=active_quote_provider == "longbridge",
        )
        missing_live_reference_symbols = sorted(set(reference_price_symbols) - set(reference_prices))
        if active_quote_provider == "longbridge" and missing_live_reference_symbols:
            raise LongbridgeQuoteError(
                "Longbridge is missing execution reference prices for: "
                + ", ".join(missing_live_reference_symbols)
            )
        latest_trades_snapshot = _safe_broker_call(
            "get_latest_trades_for_reference_symbols",
            lambda: execution_quote_client.get_latest_trades(
                symbols=audit_price_symbols,
                feed=execution_quote_feed,
            )
            if audit_price_symbols
            else {},
        )
        latest_trades_snapshot.update(
            {
                "provider": active_quote_provider,
                "feed": execution_quote_feed,
                "requested_symbols": audit_price_symbols,
                "requested_symbol_count": len(audit_price_symbols),
            }
        )
        _write_json_file(output_root / "execution_latest_trades_snapshot.json", latest_trades_snapshot)
        latest_quotes_snapshot = _safe_broker_call(
            "get_latest_quotes_for_reference_symbols",
            lambda: execution_quote_client.get_latest_quotes(
                symbols=audit_price_symbols,
                feed=execution_quote_feed,
            )
            if audit_price_symbols
            else {},
        )
        latest_quotes_snapshot.update(
            {
                "provider": active_quote_provider,
                "feed": execution_quote_feed,
                "requested_symbols": audit_price_symbols,
                "requested_symbol_count": len(audit_price_symbols),
            }
        )
        _write_json_file(output_root / "execution_latest_quotes_snapshot.json", latest_quotes_snapshot)
        _write_json_file(
            output_root / "execution_intraday_bars_1min.json",
            _collect_intraday_bars_snapshot(
                client=client,
                symbols=audit_price_symbols,
                session_date=decision_date,
                feed=str(args.execution_price_feed),
                label="before_submit",
            ),
        )
        _write_json_file(
            output_root / "execution_price_snapshot.json",
            {
                "collected_at_utc": _utc_now(),
                "provider": active_quote_provider,
                "feed": execution_quote_feed,
                "alpaca_intraday_bar_feed": str(args.execution_price_feed),
                "target_symbols": sorted(target_signed_weights),
                "broker_position_symbols_before": sorted(broker_signed_notional_before),
                "audit_benchmark_symbols": benchmark_symbols,
                "audit_price_symbols": audit_price_symbols,
                "fallback_prices": dict(sorted(fallback_prices.items())),
                "reference_prices": dict(sorted(reference_prices.items())),
                "missing_reference_price_symbols": sorted(
                    symbol
                    for symbol in (set(target_signed_weights) | set(broker_signed_notional_before))
                    if symbol not in reference_prices
                ),
            },
        )
        phase_timings.finish(
            "market_and_price_evidence",
            {
                "active_asset_count": len(assets),
                "audit_price_symbol_count": len(audit_price_symbols),
                "reference_price_count": len(reference_prices),
                "missing_reference_price_count": len(
                    (set(target_signed_weights) | set(broker_signed_notional_before)) - set(reference_prices)
                ),
            },
        )
        phase_timings.start(
            "account_sizing_and_projection",
            {"gross_capacity_target_ratio": float(args.gross_capacity_target_ratio)},
        )

        adverse_price_offset_bps = float(args.adverse_price_offset_bps)
        marketable_limit_base_offset_bps = (
            float(args.marketable_limit_base_offset_bps)
            if args.marketable_limit_base_offset_bps is not None
            else float(adverse_price_offset_bps)
        )
        sizing_adverse_offset_bps = (
            float(args.sizing_adverse_offset_bps)
            if args.sizing_adverse_offset_bps is not None
            else float(adverse_price_offset_bps)
        )
        short_buying_power_adverse_offset_bps = float(args.short_buying_power_adverse_offset_bps)
        marketable_limit_max_offset_bps = float(args.marketable_limit_max_offset_bps)
        if adverse_price_offset_bps < 0:
            raise ValueError("--adverse-price-offset-bps must be non-negative.")
        if marketable_limit_base_offset_bps < 0:
            raise ValueError("--marketable-limit-base-offset-bps must be non-negative.")
        if sizing_adverse_offset_bps < 0:
            raise ValueError("--sizing-adverse-offset-bps must be non-negative.")
        if short_buying_power_adverse_offset_bps < 0:
            raise ValueError("--short-buying-power-adverse-offset-bps must be non-negative.")
        if marketable_limit_max_offset_bps < 0:
            raise ValueError("--marketable-limit-max-offset-bps must be non-negative.")
        if int(args.marketable_limit_max_attempts) < 1:
            raise ValueError("--marketable-limit-max-attempts must be at least 1.")
        if int(args.execution_workers) < 1:
            raise ValueError("--execution-workers must be at least 1.")
        if float(args.min_trade_notional) < 0:
            raise ValueError("--min-trade-notional must be non-negative.")
        if float(args.min_trade_weight_bps) < 0:
            raise ValueError("--min-trade-weight-bps must be non-negative.")
        if not 0.0 <= float(args.buying_power_buffer) <= 1.0:
            raise ValueError("--entry-buying-power-buffer must be between 0 and 1.")
        if not 0.0 <= float(args.gross_capacity_target_ratio) <= 1.0:
            raise ValueError("--gross-capacity-target-ratio must be between 0 and 1.")

        account_for_sizing = client.get_account()
        account_for_sizing_captured_at_utc = _utc_now()
        _write_json_file(output_root / "broker_account_for_sizing.json", account_for_sizing)
        shorting_enabled = bool(account_for_sizing.get("shorting_enabled", shorting_enabled))
        sizing_equity, sizing_equity_source = _resolve_account_equity(
            account=account_for_sizing,
            signed_notional=broker_signed_notional_before,
        )
        sizing_buying_power, sizing_buying_power_source = _buying_power(account_for_sizing)
        (
            sizing_total_regt_capacity,
            sizing_gross_position,
            sizing_regt_buying_power,
            sizing_total_regt_capacity_source,
        ) = _total_regt_buying_power_capacity(
            account=account_for_sizing,
            signed_notional=broker_signed_notional_before,
        )
        effective_min_trade_notional = _effective_min_trade_notional(
            account_equity=sizing_equity,
            absolute_floor=float(args.min_trade_notional),
            weight_bps=float(args.min_trade_weight_bps),
        )
        raw_target_signed_weights = dict(target_signed_weights)
        target_signed_weights, target_lattice_signed_qty, executable_projection_diag = project_executable_targets(
            raw_target_signed_weights=raw_target_signed_weights,
            current_signed_qty=broker_signed_qty_before,
            current_signed_notional=broker_signed_notional_before,
            reference_prices=reference_prices,
            assets_by_symbol=assets_by_symbol,
            account_equity=sizing_equity,
            buying_power=sizing_buying_power,
            buying_power_buffer=float(args.buying_power_buffer),
            min_trade_notional=float(effective_min_trade_notional),
            qty_decimals=int(args.qty_decimals),
            whole_shares_only=bool(args.whole_shares_only),
            short_sales_whole_shares_only=bool(args.short_sales_whole_shares_only),
            shorting_enabled=shorting_enabled,
            sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
            short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
            total_buying_power_capacity=float(sizing_total_regt_capacity),
            gross_capacity_target_ratio=float(args.gross_capacity_target_ratio),
        )
        executable_expected_signed_weights = dict(
            executable_projection_diag.get("executable_expected_signed_weights") or {}
        )
        capacity_adjusted_target_signed_weights = dict(
            executable_projection_diag.get("capacity_adjusted_target_signed_weights") or {}
        )
        submission_capability_guard = _build_submission_capability_guard(
            raw_target_signed_weights=raw_target_signed_weights,
            capacity_adjusted_target_signed_weights=capacity_adjusted_target_signed_weights,
            executable_expected_signed_weights=executable_expected_signed_weights,
            current_signed_notional=broker_signed_notional_before,
            account_equity=sizing_equity,
            shorting_enabled=shorting_enabled,
            material_notional_tolerance=float(effective_min_trade_notional),
        )
        executable_projection_diag["submission_capability_guard"] = submission_capability_guard
        final_executable_projection_diag = executable_projection_diag
        target_short_floor_diag = {
            "legacy_projection_replaced": True,
            "projector": "executable_target_projector",
            "short_names": float(executable_projection_diag.get("integer_short_target_count") or 0),
            "lost_notional": float(
                executable_projection_diag.get("integer_short_absolute_notional_gap") or 0.0
            ),
            "desired_short_notional": float(
                sum(max(0.0, -float(value)) for value in raw_target_signed_weights.values()) * sizing_equity
            ),
            "realized_short_notional": float(
                sum(max(0.0, -float(value)) for value in executable_expected_signed_weights.values())
                * sizing_equity
            ),
            "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
        }
        _write_json_file(output_root / "executable_target_projection.json", executable_projection_diag)
        pd.DataFrame(executable_projection_diag.get("symbols") or []).to_csv(
            output_root / "executable_target_projection.csv",
            index=False,
        )
        _write_json_file(
            output_root / "target_weights_snapshot.json",
            {
                "collected_at_utc": _utc_now(),
                "raw_target_signed_weights": raw_target_signed_weights,
                "capacity_adjusted_target_signed_weights": capacity_adjusted_target_signed_weights,
                "projected_target_signed_weights": target_signed_weights,
                "order_target_signed_weights": target_signed_weights,
                "target_lattice_signed_qty": target_lattice_signed_qty,
                "executable_expected_signed_weights": executable_expected_signed_weights,
                "executable_target_projection": executable_projection_diag,
                "target_short_floor_diagnostics": target_short_floor_diag,
                "account_equity_for_sizing": float(sizing_equity),
                "account_equity_source": str(sizing_equity_source),
                "buying_power_for_sizing": float(sizing_buying_power),
                "buying_power_source": str(sizing_buying_power_source),
                "buying_power_buffer": float(args.buying_power_buffer),
                "gross_capacity_target_ratio": float(args.gross_capacity_target_ratio),
                "gross_position_for_capacity": float(sizing_gross_position),
                "regt_buying_power_remaining": float(sizing_regt_buying_power),
                "total_regt_buying_power_capacity": float(sizing_total_regt_capacity),
                "total_regt_buying_power_capacity_source": str(
                    sizing_total_regt_capacity_source
                ),
                "effective_min_trade_notional": float(effective_min_trade_notional),
                "min_trade_notional_absolute_floor": float(args.min_trade_notional),
                "min_trade_weight_bps": float(args.min_trade_weight_bps),
            },
        )
        input_target_path = None
        if args.decision_targets_input_path:
            input_target_path = Path(str(args.decision_targets_input_path)).resolve().as_posix()
        elif args.order_plan_input_path:
            input_target_path = Path(str(args.order_plan_input_path)).resolve().as_posix()
        target_capability_snapshot = _build_target_capability_snapshot(
            raw_target_signed_weights=raw_target_signed_weights,
            projection=executable_projection_diag,
            assets_by_symbol=assets_by_symbol,
            account_shorting_enabled=shorting_enabled,
            run_role="execute" if should_submit else "decision",
            input_target_path=input_target_path,
        )
        target_capability_snapshot_path = output_root / "target_capability_snapshot.json"
        target_capability_snapshot_csv_path = output_root / "target_capability_snapshot.csv"
        _write_json_file(target_capability_snapshot_path, target_capability_snapshot)
        pd.DataFrame(target_capability_snapshot.get("rows") or []).to_csv(
            target_capability_snapshot_csv_path,
            index=False,
        )
        prior_target_capability_path = (
            Path(input_target_path).parent / "target_capability_snapshot.json"
            if input_target_path
            else None
        )
        prior_target_capability = (
            _read_json_artifact(prior_target_capability_path, {})
            if prior_target_capability_path and prior_target_capability_path.exists()
            else None
        )
        target_capability_drift = _build_target_capability_drift(
            current_snapshot=target_capability_snapshot,
            prior_snapshot=prior_target_capability
            if isinstance(prior_target_capability, Mapping)
            else None,
            prior_snapshot_path=prior_target_capability_path,
        )
        target_capability_drift_path = output_root / "target_capability_drift.json"
        target_capability_drift_csv_path = output_root / "target_capability_drift.csv"
        _write_json_file(target_capability_drift_path, target_capability_drift)
        pd.DataFrame(target_capability_drift.get("rows") or []).to_csv(
            target_capability_drift_csv_path,
            index=False,
        )
        _mark_event(
            run_events,
            "target_capability_evidence_ready",
            {
                "blocked_target_count": target_capability_snapshot.get("blocked_target_count"),
                "nonshortable_short_target_symbols": target_capability_snapshot.get(
                    "nonshortable_short_target_symbols"
                ),
                "drift_status": target_capability_drift.get("status"),
                "execution_blocking_change_symbols": target_capability_drift.get(
                    "execution_blocking_change_symbols"
                ),
            },
        )

        if should_submit and submission_capability_guard["status"] == "blocked":
            _mark_event(
                run_events,
                "submission_capability_blocked",
                submission_capability_guard,
            )
            reasons = ", ".join(submission_capability_guard["blocking_reasons"])
            raise RuntimeError(
                "Execution blocked before order creation: projected portfolio cannot preserve "
                f"the required long/short structure ({reasons})."
            )

        instructions, skipped_orders = _build_order_instructions(
            target_signed_weights=target_signed_weights,
            current_signed_notional=broker_signed_notional_before,
            current_signed_qty=broker_signed_qty_before,
            account_equity=sizing_equity,
            reference_prices=reference_prices,
            assets_by_symbol=assets_by_symbol,
            min_trade_notional=float(effective_min_trade_notional),
            sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
            qty_decimals=int(args.qty_decimals),
            whole_shares_only=bool(args.whole_shares_only),
            opening_shorts_whole_shares_only=bool(args.opening_shorts_whole_shares_only),
            short_sales_whole_shares_only=bool(args.short_sales_whole_shares_only),
            shorting_enabled=shorting_enabled,
        )
        relevant_asset_symbols = sorted(
            set(symbols)
            | set(target_signed_weights)
            | set(raw_target_signed_weights)
            | set(broker_signed_notional_before)
            | {item.symbol for item in instructions}
        )
        _write_json_file(
            output_root / "broker_assets_relevant.json",
            {
                "collected_at_utc": _utc_now(),
                "symbol_count": len(relevant_asset_symbols),
                "symbols": relevant_asset_symbols,
                "assets_by_symbol": {
                    symbol: assets_by_symbol.get(symbol)
                    for symbol in relevant_asset_symbols
                    if assets_by_symbol.get(symbol) is not None
                },
                "missing_asset_symbols": [
                    symbol for symbol in relevant_asset_symbols if assets_by_symbol.get(symbol) is None
                ],
            },
        )
        _write_json_file(
            output_root / "portfolio_weights_snapshot.json",
            {
                "collected_at_utc": _utc_now(),
                "equity_before": float(equity_before),
                "equity_before_source": str(equity_before_source),
                "sizing_equity": float(sizing_equity),
                "sizing_equity_source": str(sizing_equity_source),
                "broker_weights_before": dict(sorted(broker_weights_before.items())),
                "broker_signed_notional_before": dict(sorted(broker_signed_notional_before.items())),
                "broker_signed_qty_before": dict(sorted(broker_signed_qty_before.items())),
                "target_signed_weights": dict(sorted(target_signed_weights.items())),
                "raw_target_signed_weights": dict(sorted(raw_target_signed_weights.items())),
                "capacity_adjusted_target_signed_weights": dict(
                    sorted(capacity_adjusted_target_signed_weights.items())
                ),
                "target_lattice_signed_qty": dict(sorted(target_lattice_signed_qty.items())),
                "executable_expected_signed_weights": dict(sorted(executable_expected_signed_weights.items())),
            },
        )
        _mark_event(
            run_events,
            "order_plan_built",
            {
                "order_count": len(instructions),
                "skipped_order_count": len(skipped_orders),
                "projection_solver_success": bool(
                    executable_projection_diag.get("solver", {}).get("success")
                ),
                "projection_tracking_error_l1_weight": executable_projection_diag.get(
                    "tracking_error_l1_weight"
                ),
                "projection_buying_power_cap": executable_projection_diag.get("buying_power_cap"),
                "projection_estimated_entry_buying_power_used": executable_projection_diag.get(
                    "estimated_entry_buying_power_used"
                ),
                "projection_gross_capacity_target_ratio": executable_projection_diag.get(
                    "gross_capacity_target_ratio"
                ),
                "projection_gross_capacity_target_notional": executable_projection_diag.get(
                    "gross_capacity_target_notional"
                ),
                "projection_projected_final_gross_notional": executable_projection_diag.get(
                    "projected_final_gross_notional"
                ),
            },
        )
        corporate_action_symbols = _relevant_corporate_action_symbols(
            universe_symbols=symbols,
            raw_target_signed_weights=raw_target_signed_weights,
            target_signed_weights=target_signed_weights,
            broker_signed_notional_before=broker_signed_notional_before,
            instructions=instructions,
        )
        broker_corporate_actions_path = output_root / "broker_corporate_actions.json"
        _write_json_file(
            broker_corporate_actions_path,
            _collect_relevant_corporate_actions(
                client=client,
                symbols=corporate_action_symbols,
                session_date=decision_date,
                lookback_days=10,
                lookahead_days=3,
            ),
        )
        _mark_event(
            run_events,
            "broker_corporate_actions_collected",
            {"symbol_count": len(corporate_action_symbols), "path": broker_corporate_actions_path.as_posix()},
        )
        plan_path = output_root / "order_plan.json"
        marketable_limit_requote_steps_bps = _parse_nonnegative_float_list(
            str(args.marketable_limit_requote_steps_bps)
        )
        if not marketable_limit_requote_steps_bps:
            marketable_limit_requote_steps_bps = [0.0]

        plan_path.write_text(
            json.dumps(
                {
                    "created_at_utc": _utc_now(),
                    "decision_date": decision_date.isoformat(),
                    "session_idx": int(resolved_session_idx),
                    "order_plan_input_path": plan_input_path,
                    "account_equity": float(sizing_equity),
                    "account_equity_source": str(sizing_equity_source),
                    "trigger_mode": str(args.trigger_mode),
                    "target_ny_time": str(args.target_ny_time),
                    "execution_mode": str(args.execution_mode),
                    "execution_order_style": str(args.execution_order_style),
                    "whole_shares_only": bool(args.whole_shares_only),
                    "opening_shorts_whole_shares_only": bool(args.opening_shorts_whole_shares_only),
                    "short_sales_whole_shares_only": bool(args.short_sales_whole_shares_only),
                    "floor_short_targets_to_whole_shares": bool(args.floor_short_targets_to_whole_shares),
                    "target_short_floor_diagnostics": target_short_floor_diag,
                    "executable_target_projection": executable_projection_diag,
                    "target_capability_summary": {
                        key: target_capability_snapshot.get(key)
                        for key in (
                            "blocked_target_count",
                            "blocked_target_symbols",
                            "nonshortable_short_target_count",
                            "nonshortable_short_target_symbols",
                            "projected_to_zero_count",
                            "projected_to_zero_symbols",
                        )
                    },
                    "target_capability_drift_summary": {
                        key: target_capability_drift.get(key)
                        for key in (
                            "status",
                            "changed_symbol_count",
                            "execution_blocking_change_count",
                            "execution_blocking_change_symbols",
                            "became_nonshortable_symbols",
                            "projected_to_zero_now_symbols",
                        )
                    },
                    "adverse_price_offset_bps": float(adverse_price_offset_bps),
                    "marketable_limit_base_offset_bps": float(marketable_limit_base_offset_bps),
                    "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
                    "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
                    "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
                    "buying_power_buffer": float(args.buying_power_buffer),
                    "gross_capacity_target_ratio": float(args.gross_capacity_target_ratio),
                    "total_regt_buying_power_capacity": float(sizing_total_regt_capacity),
                    "min_trade_notional": float(effective_min_trade_notional),
                    "min_trade_notional_absolute_floor": float(args.min_trade_notional),
                    "min_trade_weight_bps": float(args.min_trade_weight_bps),
                    "qty_decimals": int(args.qty_decimals),
                    "marketable_limit_requote_steps_bps": marketable_limit_requote_steps_bps,
                    "marketable_limit_requote_wait_seconds": float(args.marketable_limit_requote_wait_seconds),
                    "marketable_limit_max_attempts": int(args.marketable_limit_max_attempts),
                    "execution_workers": int(args.execution_workers),
                    "decision_status": decision_status,
                    "decision_skip_reason": decision_skip_reason,
                    "decision_diagnostics": decision_diagnostics,
                    "previous_broker_signed_weights": dict(sorted(broker_weights_before.items())),
                    "sec_cache_source": sec_cache_source,
                    "dynamic_symbol_count": int(len(symbols)),
                    "raw_target_signed_weights": raw_target_signed_weights,
                    "capacity_adjusted_target_signed_weights": capacity_adjusted_target_signed_weights,
                    "target_signed_weights": target_signed_weights,
                    "target_lattice_signed_qty": target_lattice_signed_qty,
                    "executable_expected_signed_weights": executable_expected_signed_weights,
                    "plan_semantics": "initial_order_plan_before_staged_refresh"
                    if str(args.execution_mode) == "staged_regt"
                    else "single_pass_order_plan",
                    "order_count": len(instructions),
                    "orders": [asdict(item) for item in instructions],
                    "skipped_orders": skipped_orders,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        phase_timings.finish(
            "account_sizing_and_projection",
            {
                "account_equity": float(sizing_equity),
                "total_regt_buying_power_capacity": float(sizing_total_regt_capacity),
                "raw_target_symbol_count": len(raw_target_signed_weights),
                "projected_target_symbol_count": len(target_signed_weights),
                "order_count": len(instructions),
                "projection_solver_success": bool(executable_projection_diag.get("solver", {}).get("success")),
                "tracking_error_l1_weight": executable_projection_diag.get("tracking_error_l1_weight"),
            },
        )
        phase_timings.start(
            "order_submission_and_tracking",
            {
                "submit_enabled": bool(should_submit),
                "order_count": len(instructions),
                "execution_mode": str(args.execution_mode),
            },
        )

        execution_records: list[dict[str, Any]] = []
        staged_diagnostics: dict[str, Any] = {}
        staged_rebuild_snapshots: list[dict[str, Any]] = []
        submit_error_count = 0
        submit_abort_reason: str | None = None
        if should_submit and instructions:
            _write_json_file(
                output_root / "broker_open_orders_before_submit.json",
                _safe_broker_call(
                    "list_open_orders_before_submit",
                    lambda: client.list_orders(status="open", limit=500, direction="desc", nested=False),
                ),
            )
            _write_json_file(
                output_root / "broker_orders_all_before_submit.json",
                _safe_broker_call(
                    "list_orders_all_before_submit",
                    lambda: client.list_orders_all_pages(status="all", limit=500, direction="desc", nested=False),
                ),
            )
            _mark_event(run_events, "order_submission_precheck", {"order_count": len(instructions)})
            if bool(args.cancel_open_orders_before_submit):
                try:
                    cancel_response = client.cancel_all_orders()
                    _write_json_file(
                        output_root / "broker_cancel_all_orders_response.json",
                        {
                            "collected_at_utc": _utc_now(),
                            "response": cancel_response,
                        },
                    )
                    _mark_event(run_events, "open_orders_cancel_requested", {})
                except AlpacaRequestError as exc:
                    print(f"[Executor] warning: cancel open orders failed: {exc}", flush=True)
                    _write_json_file(
                        output_root / "broker_cancel_all_orders_response.json",
                        {
                            "collected_at_utc": _utc_now(),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    _mark_event(run_events, "open_orders_cancel_failed", {"error": str(exc)})
                _write_json_file(
                    output_root / "broker_open_orders_after_cancel.json",
                    _safe_broker_call(
                        "list_open_orders_after_cancel",
                        lambda: client.list_orders(status="open", limit=500, direction="desc", nested=False),
                    ),
                )
                _write_json_file(
                    output_root / "broker_orders_all_after_cancel.json",
                    _safe_broker_call(
                        "list_orders_all_after_cancel",
                        lambda: client.list_orders_all_pages(status="all", limit=500, direction="desc", nested=False),
                    ),
                )

            if str(args.trigger_mode) == "wait_open":
                _wait_for_market_open(
                    client=client,
                    open_buffer_seconds=int(args.open_buffer_seconds),
                )
            elif str(args.trigger_mode) == "wait_target_time":
                _wait_for_target_ny_time(
                    client=client,
                    target_ny_time=str(args.target_ny_time),
                    open_buffer_seconds=int(args.open_buffer_seconds),
                )

            session_token = f"{int(time.time() * 1000) % 100000000:08d}"
            if str(args.execution_mode) == "staged_regt":
                release_timeout = (
                    float(args.staged_release_timeout_seconds)
                    if args.staged_release_timeout_seconds is not None
                    else float(args.order_timeout_seconds)
                )
                entry_timeout = (
                    float(args.staged_entry_timeout_seconds)
                    if args.staged_entry_timeout_seconds is not None
                    else float(args.order_timeout_seconds)
                )
                execution_records, staged_diagnostics = _submit_staged_regt_orders(
                    client=client,
                    execution_quote_client=(
                        execution_quote_client if active_quote_provider == "longbridge" else None
                    ),
                    initial_instructions=instructions,
                    target_signed_weights=target_signed_weights,
                    raw_target_signed_weights=raw_target_signed_weights,
                    assets_by_symbol=assets_by_symbol,
                    fallback_prices=fallback_prices,
                    session_token=session_token,
                    execution_price_feed=str(args.execution_price_feed),
                    account_equity=float(sizing_equity),
                    min_trade_notional_floor=float(args.min_trade_notional),
                    min_trade_weight_bps=float(args.min_trade_weight_bps),
                    sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
                    qty_decimals=int(args.qty_decimals),
                    whole_shares_only=bool(args.whole_shares_only),
                    opening_shorts_whole_shares_only=bool(args.opening_shorts_whole_shares_only),
                    short_sales_whole_shares_only=bool(args.short_sales_whole_shares_only),
                    shorting_enabled=bool(shorting_enabled),
                    buying_power_buffer=float(args.buying_power_buffer),
                    gross_capacity_target_ratio=float(args.gross_capacity_target_ratio),
                    short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
                    release_timeout_seconds=release_timeout,
                    entry_timeout_seconds=entry_timeout,
                    entry_repair_rounds=int(args.staged_entry_repair_rounds),
                    entry_repair_max_attempts=int(args.staged_entry_repair_max_attempts),
                    entry_repair_wait_seconds=float(args.staged_entry_repair_wait_seconds),
                    poll_seconds=float(args.order_poll_seconds),
                    execution_order_style=str(args.execution_order_style),
                    marketable_limit_base_offset_bps=float(marketable_limit_base_offset_bps),
                    marketable_limit_max_offset_bps=float(marketable_limit_max_offset_bps),
                    marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                    marketable_limit_requote_wait_seconds=float(args.marketable_limit_requote_wait_seconds),
                    marketable_limit_max_attempts=int(args.marketable_limit_max_attempts),
                    execution_workers=int(args.execution_workers),
                    release_max_rounds=int(args.staged_release_max_rounds),
                    release_round_extra_bps=float(args.staged_release_round_extra_bps),
                    release_round_sleep_seconds=float(args.staged_release_round_sleep_seconds),
                    stage_snapshots=staged_rebuild_snapshots,
                    initial_current_signed_qty=broker_signed_qty_before,
                )
                staged_expected_weights = (
                    staged_diagnostics.get("entry_projection", {}).get("executable_expected_signed_weights")
                    if isinstance(staged_diagnostics.get("entry_projection"), dict)
                    else None
                )
                if isinstance(staged_expected_weights, dict):
                    executable_expected_signed_weights = {
                        str(symbol): float(value) for symbol, value in staged_expected_weights.items()
                    }
                    final_executable_projection_diag = dict(staged_diagnostics["entry_projection"])
                    capacity_adjusted_target_signed_weights = dict(
                        final_executable_projection_diag.get(
                            "capacity_adjusted_target_signed_weights"
                        )
                        or capacity_adjusted_target_signed_weights
                    )
            else:
                execution_records = _submit_and_track_orders(
                    client=client,
                    instructions=instructions,
                    session_token=session_token,
                    timeout_seconds=float(args.order_timeout_seconds),
                    poll_seconds=float(args.order_poll_seconds),
                    execution_order_style=str(args.execution_order_style),
                    marketable_limit_base_offset_bps=float(marketable_limit_base_offset_bps),
                    marketable_limit_max_offset_bps=float(marketable_limit_max_offset_bps),
                    marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                    marketable_limit_requote_wait_seconds=float(args.marketable_limit_requote_wait_seconds),
                    marketable_limit_max_attempts=int(args.marketable_limit_max_attempts),
                    max_workers=int(args.execution_workers),
                    execution_price_feed=str(args.execution_price_feed),
                    execution_quote_client=(
                        execution_quote_client if active_quote_provider == "longbridge" else None
                    ),
                )
            final_logical_execution_records = _final_logical_execution_records(
                execution_records
            )
            submit_error_records = [
                record
                for record in final_logical_execution_records
                if str(record.get("status_latest") or "").lower()
                in {"submit_error", "quote_unavailable"}
            ]
            submit_error_count = int(len(submit_error_records))
            if submit_error_records:
                submit_abort_reason = str(submit_error_records[-1].get("error") or "order submission encountered errors")
                print(
                    f"[Executor] warning: submission completed with {submit_error_count} error(s): {submit_abort_reason}",
                    flush=True,
                )
            _mark_event(
                run_events,
                "order_submission_finished",
                {
                    "execution_record_count": len(execution_records),
                    "logical_execution_record_count": len(
                        final_logical_execution_records
                    ),
                    "submit_error_count": submit_error_count,
                    "submit_abort_reason": submit_abort_reason,
                },
            )
        elif should_submit:
            _mark_event(run_events, "order_submission_skipped_no_instructions", {})
        else:
            _mark_event(run_events, "order_submission_disabled", {"trigger_mode": str(args.trigger_mode)})
        post_submission_quotes_path = output_root / "execution_latest_quotes_snapshot_post_submission.json"
        if should_submit:
            post_submission_quotes = _safe_broker_call(
                "get_latest_quotes_post_submission",
                lambda: execution_quote_client.get_latest_quotes(
                    symbols=audit_price_symbols,
                    feed=execution_quote_feed,
                )
                if audit_price_symbols
                else {},
            )
            post_submission_quotes.update(
                {
                    "provider": active_quote_provider,
                    "feed": execution_quote_feed,
                    "requested_symbols": audit_price_symbols,
                    "requested_symbol_count": len(audit_price_symbols),
                }
            )
        else:
            post_submission_quotes = {
                "ok": True,
                "name": "get_latest_quotes_post_submission",
                "collected_at_utc": _utc_now(),
                "provider": active_quote_provider,
                "feed": execution_quote_feed,
                "requested_symbols": [],
                "requested_symbol_count": 0,
                "payload": {},
                "skipped": True,
                "skip_reason": "submission_disabled",
            }
        _write_json_file(post_submission_quotes_path, post_submission_quotes)
        _mark_event(
            run_events,
            "post_submission_quotes_collected",
            {
                "ok": bool(post_submission_quotes.get("ok")),
                "requested_symbol_count": post_submission_quotes.get("requested_symbol_count"),
                "path": post_submission_quotes_path.as_posix(),
            },
        )
        phase_timings.finish(
            "order_submission_and_tracking",
            {
                "execution_record_count": len(execution_records),
                "submit_error_count": int(submit_error_count),
                "skip_reason": None
                if should_submit and instructions
                else ("no_instructions" if should_submit else "submission_disabled"),
            },
        )
        phase_timings.start(
            "post_run_audit_and_finalize",
            {"execution_record_count": len(execution_records)},
        )

        if str(args.execution_mode) == "staged_regt":
            _write_json_file(
                output_root / "staged_rebuild_snapshots.json",
                {
                    "schema_version": "1.0",
                    "generated_at_utc": _utc_now(),
                    "mode": "staged_regt",
                    "snapshot_count": int(len(staged_rebuild_snapshots)),
                    "diagnostics": staged_diagnostics,
                    "snapshots": staged_rebuild_snapshots,
                },
            )
            _mark_event(
                run_events,
                "staged_rebuild_snapshots_written",
                {"snapshot_count": int(len(staged_rebuild_snapshots))},
            )

        positions_after_initial = client.list_positions()
        account_after_initial = client.get_account()
        position_account_stability_after = _collect_position_account_stability(
            client=client,
            initial_positions=positions_after_initial,
            initial_account=account_after_initial,
            sample_count=3,
            sleep_seconds=1.0,
        )
        positions_after = _latest_stability_payload(
            position_account_stability_after,
            payload_key="positions_payload",
            fallback=positions_after_initial,
        )
        account_after = _latest_stability_payload(
            position_account_stability_after,
            payload_key="account_payload",
            fallback=account_after_initial,
        )
        account_after_captured_at_utc = _utc_now()
        _write_json_file(output_root / "broker_positions_after_raw.json", positions_after)
        _write_json_file(output_root / "broker_account_after.json", account_after)
        account_snapshot_timeline_path = output_root / "broker_account_snapshot_timeline.json"
        _write_json_file(
            account_snapshot_timeline_path,
            {
                "schema_version": "1.0",
                "window_semantics": "preflight_then_sizing_to_post_trade",
                "snapshots": {
                    "preflight": {
                        "path": (output_root / "broker_account_before.json").as_posix(),
                        "captured_at_utc": account_before_captured_at_utc,
                    },
                    "sizing": {
                        "path": (output_root / "broker_account_for_sizing.json").as_posix(),
                        "captured_at_utc": account_for_sizing_captured_at_utc,
                    },
                    "post_trade": {
                        "path": (output_root / "broker_account_after.json").as_posix(),
                        "captured_at_utc": account_after_captured_at_utc,
                    },
                },
            },
        )
        _write_json_file(
            output_root / "broker_account_configurations_after.json",
            _safe_broker_call("get_account_configurations_after", client.get_account_configurations),
        )
        _write_json_file(output_root / "broker_position_account_stability_after.json", position_account_stability_after)
        broker_frame_after, broker_signed_notional_after = _positions_to_frame_and_notional(positions_after)
        _write_json_file(
            output_root / "broker_clock_after.json",
            _safe_broker_call("get_clock_after", client.get_clock),
        )
        _write_json_file(
            output_root / "broker_portfolio_history_after.json",
            _collect_portfolio_history_snapshot(client=client, session_date=decision_date, label="after"),
        )
        expanded_corporate_action_symbols = sorted(set(corporate_action_symbols) | set(broker_signed_notional_after))
        if expanded_corporate_action_symbols != corporate_action_symbols:
            corporate_action_symbols = expanded_corporate_action_symbols
            _write_json_file(
                broker_corporate_actions_path,
                _collect_relevant_corporate_actions(
                    client=client,
                    symbols=corporate_action_symbols,
                    session_date=decision_date,
                    lookback_days=10,
                    lookahead_days=3,
                ),
            )
            _mark_event(
                run_events,
                "broker_corporate_actions_expanded_after_positions",
                {"symbol_count": len(corporate_action_symbols), "path": broker_corporate_actions_path.as_posix()},
            )
        intraday_bar_symbols_after = sorted(
            set(reference_price_symbols)
            | set(benchmark_symbols)
            | set(broker_signed_notional_after)
            | {item.symbol for item in instructions}
        )
        _write_json_file(
            output_root / "execution_intraday_bars_1min_after.json",
            _collect_intraday_bars_snapshot(
                client=client,
                symbols=intraday_bar_symbols_after,
                session_date=decision_date,
                feed=str(args.execution_price_feed),
                label="after_execution",
            ),
        )
        latest_quotes_after_snapshot = _safe_broker_call(
            "get_latest_quotes_for_after_symbols",
            lambda: execution_quote_client.get_latest_quotes(
                symbols=intraday_bar_symbols_after,
                feed=execution_quote_feed,
            )
            if intraday_bar_symbols_after
            else {},
        )
        latest_quotes_after_snapshot.update(
            {
                "provider": active_quote_provider,
                "feed": execution_quote_feed,
                "requested_symbols": intraday_bar_symbols_after,
                "requested_symbol_count": len(intraday_bar_symbols_after),
            }
        )
        _write_json_file(output_root / "execution_latest_quotes_snapshot_after.json", latest_quotes_after_snapshot)
        if isinstance(execution_quote_client, LongbridgeQuoteClient):
            _write_json_file(
                output_root / "execution_quote_provider_health_after.json",
                execution_quote_client.health_snapshot(requested_symbols=intraday_bar_symbols_after),
            )
            execution_quote_client.close()
            execution_quote_client = None
        _write_json_file(
            output_root / "broker_open_orders_after.json",
            _safe_broker_call(
                "list_open_orders_after",
                lambda: client.list_orders(status="open", limit=500, direction="desc", nested=False),
            ),
        )
        _write_json_file(
            output_root / "broker_orders_all_after.json",
            _safe_broker_call(
                "list_orders_all_after",
                lambda: client.list_orders_all_pages(status="all", limit=500, direction="desc", nested=False),
            ),
        )
        equity_after, equity_after_source = _resolve_account_equity(
            account=account_after,
            signed_notional=broker_signed_notional_after,
        )
        broker_weights_after = _weights_from_signed_notional(
            broker_signed_notional_after,
            equity=equity_after,
        )
        _write_json_file(
            output_root / "portfolio_weights_after_snapshot.json",
            {
                "collected_at_utc": _utc_now(),
                "equity_after": float(equity_after),
                "equity_after_source": str(equity_after_source),
                "broker_weights_after": dict(sorted(broker_weights_after.items())),
                "broker_signed_notional_after": dict(sorted(broker_signed_notional_after.items())),
            },
        )
        _mark_event(
            run_events,
            "broker_state_after_loaded",
            {
                "position_count": len(positions_after),
                "equity_after": float(equity_after),
                "stability_position_hash_count": position_account_stability_after.get("position_hash_count"),
                "stability_account_hash_count": position_account_stability_after.get("account_hash_count"),
            },
        )
        account_state.update(
            {
                "last_session_idx": int(resolved_session_idx),
                "last_session_date": decision_date.isoformat(),
                "executor_last_run_utc": _utc_now(),
                "executor_last_order_count": int(len(instructions)),
                "executor_last_submit_enabled": bool(should_submit),
                "executor_last_post_trade_equity": float(equity_after),
                "executor_last_post_trade_equity_source": str(equity_after_source),
            }
        )
        _write_json_file(account_state_path, account_state)
        _write_json_file(output_root / "account_state_after.json", account_state)

        broker_frame_before.to_csv(output_root / "broker_positions_before.csv", index=False)
        broker_frame_after.to_csv(output_root / "broker_positions_after.csv", index=False)
        raw_fill_activities_path = output_root / "broker_fill_activities.json"
        raw_order_snapshots_path = output_root / "broker_order_snapshots.json"
        order_poll_timeline_path = output_root / "order_poll_timeline.json"
        execution_attempt_outcome_summary_path = (
            output_root / "execution_attempt_outcome_summary.json"
        )
        broker_fill_activities = _collect_broker_fill_activities(
            client=client,
            session_date=decision_date,
            execution_records=execution_records,
        )
        raw_fill_activities_path.write_text(
            json.dumps(broker_fill_activities, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        raw_account_activities_path = output_root / "broker_account_activities.json"
        _write_json_file(
            raw_account_activities_path,
            _safe_broker_call(
                "list_account_activities_all",
                lambda: client.list_account_activities(
                    date=decision_date.isoformat(),
                    direction="asc",
                    page_size=100,
                ),
            ),
        )
        broker_order_snapshots = _collect_broker_order_snapshots(client=client, execution_records=execution_records)
        raw_order_snapshots_path.write_text(
            json.dumps(broker_order_snapshots, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        order_poll_timeline = _build_order_poll_timeline(execution_records)
        _write_json_file(order_poll_timeline_path, order_poll_timeline)
        execution_attempt_outcome_summary = _execution_attempt_outcome_summary(
            execution_records
        )
        _write_json_file(
            execution_attempt_outcome_summary_path,
            execution_attempt_outcome_summary,
        )

        alignment_after = _alignment_to_target(
            target_signed_weights=executable_expected_signed_weights,
            broker_weights=broker_weights_after,
        )
        staged_abort_reason = str(staged_diagnostics.get("entry_abort_reason") or "") if staged_diagnostics else ""
        run_ok = bool(submit_error_count == 0 and not staged_abort_reason)
        phase_timings.finish(
            "post_run_audit_and_finalize",
            {
                "run_ok": bool(run_ok),
                "position_count_after": len(positions_after),
                "order_poll_event_count": int(order_poll_timeline.get("event_count") or 0),
            },
        )
        decision_phase_timing_summary = phase_timings.finalize(
            status="succeeded" if run_ok else "completed_with_errors",
            context={
                "run_ok": bool(run_ok),
                "submit_enabled": bool(should_submit),
            },
        )
        _mark_event(
            run_events,
            "decision_phase_timings_finalized",
            {
                "path": phase_timings.path.as_posix(),
                "elapsed_seconds": decision_phase_timing_summary.get("elapsed_seconds"),
                "decision_compute_elapsed_seconds": decision_phase_timing_summary.get(
                    "decision_compute_elapsed_seconds"
                ),
                "slowest_phase": decision_phase_timing_summary.get("slowest_phase"),
            },
        )
        _mark_event(run_events, "execution_summary_ready", {"ok": bool(run_ok)})
        _write_json_file(
            run_context_path,
            _build_run_context(
                args=args,
                argv=argv,
                decision_date=decision_date,
                output_root=output_root,
                should_submit=should_submit,
                run_started_at_utc=run_started_at_utc,
                events=run_events,
            ),
        )
        execution_summary = {
            "ok": run_ok,
            "decision_date": decision_date.isoformat(),
            "session_idx": int(resolved_session_idx),
            "order_plan_input_path": plan_input_path,
            "alpha_panel_input_path": (
                Path(str(args.alpha_panel_input_path)).resolve().as_posix()
                if args.alpha_panel_input_path
                else None
            ),
            "position_continuity_guard": position_continuity_guard,
            "account_equity_preflight": float(equity_before),
            "account_equity_preflight_source": str(equity_before_source),
            "account_equity_preflight_captured_at_utc": account_before_captured_at_utc,
            "account_equity": float(sizing_equity),
            "account_equity_source": str(sizing_equity_source),
            "account_equity_captured_at_utc": account_for_sizing_captured_at_utc,
            "account_equity_post_trade": float(equity_after),
            "account_equity_post_trade_source": str(equity_after_source),
            "account_equity_post_trade_captured_at_utc": account_after_captured_at_utc,
            "account_equity_window_semantics": "sizing_to_post_trade",
            "trigger_mode": str(args.trigger_mode),
            "target_ny_time": str(args.target_ny_time),
            "execution_mode": str(args.execution_mode),
            "execution_order_style": str(args.execution_order_style),
            "execution_quote_provider_configured": configured_quote_provider,
            "execution_quote_provider_active": active_quote_provider,
            "execution_quote_feed": execution_quote_feed,
            "execution_quote_provider_health": quote_provider_health,
            "alpaca_intraday_bar_feed": str(args.execution_price_feed),
            "adverse_price_offset_bps": float(adverse_price_offset_bps),
            "marketable_limit_base_offset_bps": float(marketable_limit_base_offset_bps),
            "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
            "marketable_limit_requote_steps_bps": marketable_limit_requote_steps_bps,
            "marketable_limit_requote_wait_seconds": float(
                args.marketable_limit_requote_wait_seconds
            ),
            "marketable_limit_max_attempts": int(args.marketable_limit_max_attempts),
            "staged_entry_repair_rounds": int(args.staged_entry_repair_rounds),
            "staged_entry_repair_max_attempts": int(args.staged_entry_repair_max_attempts),
            "staged_entry_repair_wait_seconds": float(args.staged_entry_repair_wait_seconds),
            "execution_workers": int(args.execution_workers),
            "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
            "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
            "min_trade_notional": float(effective_min_trade_notional),
            "min_trade_notional_absolute_floor": float(args.min_trade_notional),
            "min_trade_weight_bps": float(args.min_trade_weight_bps),
            "qty_decimals": int(args.qty_decimals),
            "decision_status": decision_status,
            "decision_skip_reason": decision_skip_reason,
            "decision_phase_timings": decision_phase_timing_summary,
            "target_capability_summary": {
                key: target_capability_snapshot.get(key)
                for key in (
                    "blocked_target_count",
                    "blocked_target_symbols",
                    "nonshortable_short_target_count",
                    "nonshortable_short_target_symbols",
                    "projected_to_zero_count",
                    "projected_to_zero_symbols",
                )
            },
            "target_capability_drift_summary": {
                key: target_capability_drift.get(key)
                for key in (
                    "status",
                    "changed_symbol_count",
                    "execution_blocking_change_count",
                    "execution_blocking_change_symbols",
                    "became_nonshortable_symbols",
                    "projected_to_zero_now_symbols",
                )
            },
            "symbol_universe_intersection_summary": {
                key: symbol_universe_snapshot.get(key)
                for key in (
                    "mode",
                    "status",
                    "configured_count",
                    "alpaca_clean_core_count",
                    "alpaca_tradable_count",
                    "longbridge_covered_count",
                    "longbridge_covered_count_at_decision",
                    "final_intersection_count",
                    "dynamic_selected_count",
                    "coverage_lost_since_decision_count",
                    "coverage_lost_since_decision_symbols",
                    "required_symbols_without_coverage",
                    "blocking_symbols",
                )
            },
            "dynamic_symbols": int(len(symbols)),
            "order_plan_count": int(len(instructions)),
            "submitted": bool(should_submit),
            "submitted_orders": int(len(execution_records)),
            "order_poll_event_count": int(order_poll_timeline.get("event_count") or 0),
            "execution_attempt_outcome_summary": execution_attempt_outcome_summary,
            "staged_rebuild_snapshot_count": int(len(staged_rebuild_snapshots)),
            "submit_error_count": int(submit_error_count),
            "submit_abort_reason": submit_abort_reason,
            "staged_diagnostics": staged_diagnostics,
            "raw_target_signed_weights": raw_target_signed_weights,
            "capacity_adjusted_target_signed_weights": capacity_adjusted_target_signed_weights,
            "order_target_signed_weights": target_signed_weights,
            "executable_expected_signed_weights": executable_expected_signed_weights,
            "gross_capacity_target_ratio": float(args.gross_capacity_target_ratio),
            "initial_executable_target_projection": executable_projection_diag,
            "executable_target_projection": final_executable_projection_diag,
            "account_state_path": account_state_path.as_posix(),
            "alignment_after_execution": alignment_after,
            "outputs": {
                "run_context_json": run_context_path.as_posix(),
                "run_events_jsonl": (output_root / "run_events.jsonl").as_posix(),
                "decision_phase_timings_json": phase_timings.path.as_posix(),
                "execution_attempt_outcome_summary_json": (
                    execution_attempt_outcome_summary_path.as_posix()
                ),
                "target_capability_snapshot_json": target_capability_snapshot_path.as_posix(),
                "target_capability_snapshot_csv": target_capability_snapshot_csv_path.as_posix(),
                "target_capability_drift_json": target_capability_drift_path.as_posix(),
                "target_capability_drift_csv": target_capability_drift_csv_path.as_posix(),
                "symbol_universe_intersection_json": symbol_universe_json_path.as_posix(),
                "symbol_universe_intersection_csv": symbol_universe_csv_path.as_posix(),
                "runtime_environment_snapshot_json": (output_root / "runtime_environment_snapshot.json").as_posix(),
                "alpaca_api_audit_jsonl": alpaca_api_audit_path.as_posix(),
                "source_code_manifest_json": (output_root / "source_code_manifest.json").as_posix(),
                "source_git_snapshot_json": (output_root / "source_git_snapshot.json").as_posix(),
                "source_git_diff_patch": (output_root / "source_git_diff.patch").as_posix(),
                "source_code_snapshot_zip": (output_root / "source_code_snapshot.zip").as_posix(),
                "source_code_snapshot_manifest_json": (output_root / "source_code_snapshot_manifest.json").as_posix(),
                "python_environment_json": (output_root / "python_environment.json").as_posix(),
                "input_file_manifest_json": (output_root / "input_file_manifest.json").as_posix(),
                "alpha_panel_csv": alpha_path.as_posix() if alpha_path else None,
                "alpha_cache_provenance_json": (
                    alpha_cache_provenance_path.as_posix() if alpha_cache_provenance_path else None
                ),
                "position_continuity_guard_json": position_continuity_guard_path.as_posix(),
                "decision_position_mark_snapshot_json": (
                    (output_root / "decision_position_mark_snapshot.json").as_posix()
                    if (output_root / "decision_position_mark_snapshot.json").exists()
                    else None
                ),
                "decision_targets_csv": decision_targets_path.as_posix() if decision_targets_path else None,
                "order_plan_json": plan_path.as_posix(),
                "broker_account_before_json": (output_root / "broker_account_before.json").as_posix(),
                "broker_account_for_sizing_json": (output_root / "broker_account_for_sizing.json").as_posix(),
                "broker_account_after_json": (output_root / "broker_account_after.json").as_posix(),
                "broker_account_snapshot_timeline_json": account_snapshot_timeline_path.as_posix(),
                "broker_account_configurations_before_json": (
                    output_root / "broker_account_configurations_before.json"
                ).as_posix(),
                "broker_account_configurations_after_json": (
                    output_root / "broker_account_configurations_after.json"
                ).as_posix(),
                "broker_calendar_window_json": broker_calendar_window_path.as_posix(),
                "broker_clock_before_json": (output_root / "broker_clock_before.json").as_posix(),
                "broker_clock_after_json": (output_root / "broker_clock_after.json").as_posix(),
                "broker_portfolio_history_before_json": (
                    output_root / "broker_portfolio_history_before.json"
                ).as_posix(),
                "broker_portfolio_history_after_json": (
                    output_root / "broker_portfolio_history_after.json"
                ).as_posix(),
                "broker_open_orders_before_json": (output_root / "broker_open_orders_before.json").as_posix(),
                "broker_orders_all_before_json": (output_root / "broker_orders_all_before.json").as_posix(),
                "broker_open_orders_before_submit_json": (output_root / "broker_open_orders_before_submit.json").as_posix(),
                "broker_orders_all_before_submit_json": (output_root / "broker_orders_all_before_submit.json").as_posix(),
                "broker_open_orders_after_cancel_json": (output_root / "broker_open_orders_after_cancel.json").as_posix(),
                "broker_orders_all_after_cancel_json": (output_root / "broker_orders_all_after_cancel.json").as_posix(),
                "broker_open_orders_after_json": (output_root / "broker_open_orders_after.json").as_posix(),
                "broker_orders_all_after_json": (output_root / "broker_orders_all_after.json").as_posix(),
                "broker_cancel_all_orders_response_json": (output_root / "broker_cancel_all_orders_response.json").as_posix(),
                "broker_positions_before_raw_json": (output_root / "broker_positions_before_raw.json").as_posix(),
                "broker_positions_after_raw_json": (output_root / "broker_positions_after_raw.json").as_posix(),
                "broker_position_account_stability_before_json": (
                    output_root / "broker_position_account_stability_before.json"
                ).as_posix(),
                "broker_position_account_stability_after_json": (
                    output_root / "broker_position_account_stability_after.json"
                ).as_posix(),
                "broker_positions_before_csv": (output_root / "broker_positions_before.csv").as_posix(),
                "broker_positions_after_csv": (output_root / "broker_positions_after.csv").as_posix(),
                "execution_records_json": (output_root / "execution_records.json").as_posix(),
                "order_poll_timeline_json": order_poll_timeline_path.as_posix(),
                "staged_rebuild_snapshots_json": (output_root / "staged_rebuild_snapshots.json").as_posix(),
                "broker_fill_activities_json": raw_fill_activities_path.as_posix(),
                "broker_account_activities_json": raw_account_activities_path.as_posix(),
                "broker_corporate_actions_json": broker_corporate_actions_path.as_posix(),
                "broker_order_snapshots_json": raw_order_snapshots_path.as_posix(),
                "account_state_before_json": (output_root / "account_state_before.json").as_posix(),
                "account_state_after_json": (output_root / "account_state_after.json").as_posix(),
                "broker_assets_active_us_equity_json": (output_root / "broker_assets_active_us_equity.json").as_posix(),
                "broker_assets_relevant_json": (output_root / "broker_assets_relevant.json").as_posix(),
                "execution_latest_trades_snapshot_json": (output_root / "execution_latest_trades_snapshot.json").as_posix(),
                "execution_quote_provider_health_json": (
                    output_root / "execution_quote_provider_health.json"
                ).as_posix(),
                "execution_quote_provider_health_after_json": (
                    (output_root / "execution_quote_provider_health_after.json").as_posix()
                    if (output_root / "execution_quote_provider_health_after.json").exists()
                    else None
                ),
                "execution_latest_quotes_snapshot_json": (output_root / "execution_latest_quotes_snapshot.json").as_posix(),
                "execution_latest_quotes_snapshot_post_submission_json": (
                    post_submission_quotes_path.as_posix()
                ),
                "execution_latest_quotes_snapshot_after_json": (
                    output_root / "execution_latest_quotes_snapshot_after.json"
                ).as_posix(),
                "execution_intraday_bars_1min_json": (output_root / "execution_intraday_bars_1min.json").as_posix(),
                "execution_intraday_bars_1min_after_json": (
                    output_root / "execution_intraday_bars_1min_after.json"
                ).as_posix(),
                "execution_price_snapshot_json": (output_root / "execution_price_snapshot.json").as_posix(),
                "target_weights_snapshot_json": (output_root / "target_weights_snapshot.json").as_posix(),
                "executable_target_projection_json": (
                    output_root / "executable_target_projection.json"
                ).as_posix(),
                "executable_target_projection_csv": (
                    output_root / "executable_target_projection.csv"
                ).as_posix(),
                "portfolio_weights_snapshot_json": (output_root / "portfolio_weights_snapshot.json").as_posix(),
                "portfolio_weights_after_snapshot_json": (output_root / "portfolio_weights_after_snapshot.json").as_posix(),
                "run_evidence_digest_json": (output_root / "run_evidence_digest.json").as_posix(),
                "run_artifact_manifest_json": (output_root / "run_artifact_manifest.json").as_posix(),
                "file_hash_manifest_json": (output_root / "file_hash_manifest.json").as_posix(),
                "artifact_completeness_snapshot_json": (
                    output_root / "artifact_completeness_snapshot.json"
                ).as_posix(),
            },
        }
        _write_json_file(output_root / "execution_records.json", execution_records)
        _write_json_file(output_root / "execution_summary.json", execution_summary)
        _finalize_run_evidence(output_root, run_events)
        print(json.dumps(execution_summary, indent=2, ensure_ascii=False))
        return 0 if run_ok else 1
    except (ValueError, FileNotFoundError, AlpacaRequestError, RuntimeError, Exception) as exc:
        if isinstance(execution_quote_client, LongbridgeQuoteClient):
            try:
                execution_quote_client.close()
            except Exception:
                pass
        if isinstance(symbol_universe_quote_client, LongbridgeQuoteClient):
            try:
                symbol_universe_quote_client.close()
            except Exception:
                pass
        failed_at_utc = _utc_now()
        decision_phase_timing_summary = None
        if "phase_timings" in locals():
            try:
                decision_phase_timing_summary = phase_timings.fail(exc)
            except Exception:
                pass
        if "run_events" in locals():
            try:
                _mark_event(
                    run_events,
                    "executor_failed",
                    {
                        "failed_at_utc": failed_at_utc,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            except Exception:
                pass
        error_summary = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
            "failed_at_utc": failed_at_utc,
        }
        if decision_phase_timing_summary is not None:
            error_summary["decision_phase_timings"] = decision_phase_timing_summary
            error_summary["outputs"] = {
                "decision_phase_timings_json": phase_timings.path.as_posix(),
            }
        if "output_root" in locals():
            try:
                Path(output_root).mkdir(parents=True, exist_ok=True)
                if "run_context_path" in locals() and "decision_date" in locals() and "run_started_at_utc" in locals():
                    _write_json_file(
                        run_context_path,
                        _build_run_context(
                            args=args,
                            argv=argv,
                            decision_date=decision_date,
                            output_root=output_root,
                            should_submit=bool("should_submit" in locals() and should_submit),
                            run_started_at_utc=run_started_at_utc,
                            events=run_events if "run_events" in locals() else [],
                            failure=error_summary,
                        ),
                    )
                (Path(output_root) / "execution_summary.json").write_text(
                    json.dumps(error_summary, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _finalize_run_evidence(Path(output_root), run_events if "run_events" in locals() else None)
            except Exception:
                pass
        print(json.dumps(error_summary, indent=2, ensure_ascii=False))
        return 1


def _normalize_date(raw: str | date | datetime) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw))


def _parse_float_list(text: str) -> list[float]:
    token = str(text).strip()
    if not token:
        return []
    return [float(piece.strip()) for piece in token.split(",") if piece.strip()]


def _parse_symbol_list(text: str) -> list[str]:
    token = str(text or "").strip()
    if not token:
        return []
    pieces = re.split(r"[\s,;]+", token)
    return [piece.strip().upper() for piece in pieces if piece.strip()]


def _parse_nonnegative_float_list(text: str) -> list[float]:
    values = _parse_float_list(text)
    out: list[float] = []
    for value in values:
        if value < 0:
            raise ValueError(f"negative value is not allowed: {value}")
        out.append(float(value))
    return out


def _parse_hhmm(text: str) -> tuple[int, int]:
    token = str(text).strip()
    parts = token.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM time: {text}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Invalid HH:MM time: {text}") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HH:MM time: {text}")
    return hour, minute


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path.as_posix()}")
    return payload


def _extract_target_signed_weights_from_plan(plan_payload: Mapping[str, Any]) -> dict[str, float]:
    from_weights = plan_payload.get("raw_target_signed_weights")
    if not isinstance(from_weights, Mapping):
        from_weights = plan_payload.get("target_signed_weights")
    out: dict[str, float] = {}
    if isinstance(from_weights, Mapping):
        for key, value in from_weights.items():
            symbol = str(key).strip().upper()
            number = _safe_float(value)
            if symbol and number is not None and abs(number) > EPS:
                out[symbol] = float(number)
        if out:
            return out

    account_equity = _safe_float(plan_payload.get("account_equity"))
    orders_raw = plan_payload.get("orders")
    if account_equity is None or account_equity <= 0 or not isinstance(orders_raw, list):
        return {}
    for item in orders_raw:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        target_notional = _safe_float(item.get("target_notional"))
        if not symbol or target_notional is None:
            continue
        weight = float(target_notional) / float(account_equity)
        if abs(weight) > EPS:
            out[symbol] = float(weight)
    return out


def _load_target_signed_weights_from_csv(path: Path) -> dict[str, float]:
    if not path.exists():
        raise FileNotFoundError(f"Decision target CSV not found: {path.as_posix()}")
    if path.stat().st_size <= 0:
        return {}
    try:
        frame = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return {}
    if "symbol" not in frame.columns:
        raise ValueError(f"Decision target CSV must contain a symbol column: {path.as_posix()}")

    out: dict[str, float] = {}
    for row in frame.itertuples(index=False):
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        if not symbol:
            continue

        signed_weight: float | None = None
        if "signed_weight" in frame.columns:
            signed_weight = _safe_float(getattr(row, "signed_weight", None))
        if signed_weight is None and "target_signed_weight" in frame.columns:
            signed_weight = _safe_float(getattr(row, "target_signed_weight", None))
        if signed_weight is None and "side_weight" in frame.columns:
            side_weight = _safe_float(getattr(row, "side_weight", None))
            side = str(getattr(row, "side", "") or "").strip().lower() if "side" in frame.columns else ""
            if side_weight is not None:
                if side == "short":
                    signed_weight = -abs(float(side_weight))
                elif side == "long":
                    signed_weight = abs(float(side_weight))
        if signed_weight is None:
            continue
        if abs(float(signed_weight)) <= EPS:
            continue
        out[symbol] = out.get(symbol, 0.0) + float(signed_weight)

    return {symbol: float(value) for symbol, value in sorted(out.items()) if abs(float(value)) > EPS}


def _target_weights_to_frame(target_signed_weights: Mapping[str, float]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, signed_weight in sorted(target_signed_weights.items()):
        value = float(signed_weight)
        if abs(value) <= EPS:
            continue
        rows.append(
            {
                "symbol": str(symbol).upper(),
                "signed_weight": value,
                "side": "long" if value > 0 else "short",
                "side_weight": abs(value),
            }
        )
    frame = pd.DataFrame(rows, columns=["symbol", "signed_weight", "side", "side_weight"])
    if not frame.empty:
        frame = frame.sort_values(["side", "side_weight"], ascending=[True, False]).reset_index(drop=True)
    return frame


def _new_longbridge_quote_client(args: argparse.Namespace) -> LongbridgeQuoteClient:
    credentials = LongbridgeCredentials.from_sources(args.longbridge_config_path)
    return LongbridgeQuoteClient(
        credentials,
        warmup_timeout_seconds=float(args.longbridge_warmup_timeout_seconds),
        max_quote_age_seconds=float(args.longbridge_max_quote_age_seconds),
        max_spread_bps=float(args.longbridge_max_spread_bps),
        max_subscriptions=int(args.longbridge_max_subscriptions),
        snapshot_context_count=int(args.longbridge_snapshot_contexts),
    )


def _coverage_rows_by_symbol(coverage: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = coverage.get("rows") if isinstance(coverage, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    return {
        str(row.get("symbol") or "").strip().upper(): dict(row)
        for row in rows
        if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
    }


def _build_decision_symbol_universe_snapshot(
    *,
    candidate_symbols_path: Path,
    candidate_symbols: Sequence[str],
    alpaca_assets: Sequence[dict[str, Any]],
    longbridge_coverage: Mapping[str, Any],
    decision_date: date,
) -> dict[str, Any]:
    configured = sorted(
        {str(symbol or "").strip().upper() for symbol in candidate_symbols if str(symbol or "").strip()}
    )
    configured_set = set(configured)
    clean_core = _build_runtime_clean_core_symbol_set(alpaca_assets)
    tradable = _build_tradable_symbol_set(alpaca_assets)
    alpaca_symbols = {
        str(asset.get("symbol") or "").strip().upper()
        for asset in alpaca_assets
        if isinstance(asset, Mapping) and str(asset.get("symbol") or "").strip()
    }
    coverage_rows = _coverage_rows_by_symbol(longbridge_coverage)
    longbridge_covered = {
        symbol for symbol, row in coverage_rows.items() if bool(row.get("covered"))
    }
    final_intersection = sorted(configured_set & clean_core & tradable & longbridge_covered)
    rows: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    for symbol in configured:
        coverage_row = coverage_rows.get(symbol, {})
        reasons: list[str] = []
        if symbol not in alpaca_symbols:
            reasons.append("missing_alpaca_active_asset")
        if symbol not in clean_core:
            reasons.append("not_alpaca_clean_core")
        if symbol not in tradable:
            reasons.append("not_alpaca_tradable")
        if symbol not in longbridge_covered:
            reasons.append(str(coverage_row.get("coverage_reason") or "longbridge_missing_quote"))
        rejection_counts.update(reasons)
        rows.append(
            {
                "symbol": symbol,
                "configured": True,
                "alpaca_active_asset": symbol in alpaca_symbols,
                "alpaca_clean_core": symbol in clean_core,
                "alpaca_tradable": symbol in tradable,
                "longbridge_returned": bool(coverage_row.get("returned")),
                "longbridge_covered": symbol in longbridge_covered,
                "longbridge_permanently_unavailable": bool(
                    coverage_row.get("permanently_unavailable")
                ),
                "longbridge_trade_status": coverage_row.get("trade_status"),
                "longbridge_last_price": coverage_row.get("last_price"),
                "longbridge_quote_timestamp_utc": coverage_row.get("quote_timestamp_utc"),
                "longbridge_coverage_reason": coverage_row.get("coverage_reason"),
                "in_final_intersection": symbol in final_intersection,
                "rejection_reasons": reasons,
            }
        )
    coverage_errors = list(longbridge_coverage.get("errors") or [])
    return {
        "schema_version": "1.0",
        "artifact_type": "symbol_universe_intersection",
        "mode": "decision",
        "generated_at_utc": _utc_now(),
        "decision_date": decision_date.isoformat(),
        "status": "error" if coverage_errors else "pass" if final_intersection else "empty",
        "configured_candidate_file": {
            "path": candidate_symbols_path.resolve().as_posix(),
            "bytes": candidate_symbols_path.stat().st_size if candidate_symbols_path.exists() else None,
            "sha256": _sha256_file(candidate_symbols_path),
        },
        "configured_count": len(configured),
        "alpaca_active_asset_count": len(configured_set & alpaca_symbols),
        "alpaca_clean_core_count": len(configured_set & clean_core),
        "alpaca_tradable_count": len(configured_set & tradable),
        "longbridge_returned_count": len(configured_set & set(coverage_rows)),
        "longbridge_covered_count": len(configured_set & longbridge_covered),
        "final_intersection_count": len(final_intersection),
        "configured_symbols": configured,
        "alpaca_clean_core_symbols": sorted(configured_set & clean_core),
        "alpaca_tradable_symbols": sorted(configured_set & tradable),
        "longbridge_covered_symbols": sorted(configured_set & longbridge_covered),
        "final_intersection_symbols": final_intersection,
        "rejected_symbols": [row["symbol"] for row in rows if not row["in_final_intersection"]],
        "rejection_reason_counts": dict(sorted(rejection_counts.items())),
        "longbridge_coverage": dict(longbridge_coverage),
        "rows": rows,
        "scope_rule": (
            "configured candidates intersect Alpaca active clean-core/tradable assets "
            "intersect Longbridge covered symbols"
        ),
    }


def _target_scope_assessment(
    *,
    target_signed_weights: Mapping[str, float],
    broker_weights: Mapping[str, float],
    strategy_symbols: Sequence[str],
) -> dict[str, Any]:
    allowed = {str(symbol).strip().upper() for symbol in strategy_symbols if str(symbol).strip()}
    target_symbols = {
        str(symbol).strip().upper()
        for symbol, weight in target_signed_weights.items()
        if str(symbol).strip() and abs(float(weight)) > EPS
    }
    held_symbols = {
        str(symbol).strip().upper()
        for symbol, weight in broker_weights.items()
        if str(symbol).strip() and abs(float(weight)) > EPS
    }
    outside = sorted(target_symbols - allowed)
    held_outside = sorted(held_symbols - allowed)
    exit_only: list[str] = []
    invalid: list[str] = []
    rows: list[dict[str, Any]] = []
    for symbol in outside:
        target_weight = float(target_signed_weights.get(symbol) or 0.0)
        current_weight = float(broker_weights.get(symbol) or 0.0)
        same_direction = target_weight * current_weight >= -EPS
        non_increasing = abs(target_weight) <= abs(current_weight) + 1e-6
        allowed_exit_only = symbol in held_symbols and same_direction and non_increasing
        (exit_only if allowed_exit_only else invalid).append(symbol)
        rows.append(
            {
                "symbol": symbol,
                "target_weight": target_weight,
                "current_weight": current_weight,
                "same_direction": same_direction,
                "non_increasing_exposure": non_increasing,
                "classification": "exit_only" if allowed_exit_only else "invalid_out_of_scope_target",
            }
        )
    return {
        "target_symbol_count": len(target_symbols),
        "held_symbol_count": len(held_symbols),
        "target_symbols": sorted(target_symbols),
        "held_symbols": sorted(held_symbols),
        "held_symbols_outside_intersection": held_outside,
        "target_symbols_outside_intersection": outside,
        "exit_only_symbols": sorted(exit_only),
        "invalid_target_scope_symbols": sorted(invalid),
        "rows": rows,
        "status": "error" if invalid else "pass",
    }


def _build_execution_symbol_universe_snapshot(
    *,
    decision_snapshot_path: Path,
    target_signed_weights: Mapping[str, float],
    broker_weights: Mapping[str, float],
    current_longbridge_coverage: Mapping[str, Any],
    decision_date: date,
) -> dict[str, Any]:
    decision_snapshot = _load_json_dict(decision_snapshot_path)
    if str(decision_snapshot.get("artifact_type") or "") != "symbol_universe_intersection":
        raise ValueError(
            f"Invalid decision symbol-universe snapshot: {decision_snapshot_path.as_posix()}"
        )
    if str(decision_snapshot.get("status") or "") != "pass":
        raise ValueError(
            f"Decision symbol-universe snapshot is not pass: {decision_snapshot_path.as_posix()}"
        )
    decision_final = sorted(
        {
            str(symbol).strip().upper()
            for symbol in (decision_snapshot.get("final_intersection_symbols") or [])
            if str(symbol).strip()
        }
    )
    if not decision_final:
        raise ValueError(
            f"Decision symbol-universe snapshot has an empty final intersection: "
            f"{decision_snapshot_path.as_posix()}"
        )
    current_covered = {
        str(symbol).strip().upper()
        for symbol in (current_longbridge_coverage.get("covered_symbols") or [])
        if str(symbol).strip()
    }
    scope = _target_scope_assessment(
        target_signed_weights=target_signed_weights,
        broker_weights=broker_weights,
        strategy_symbols=decision_final,
    )
    target_symbols = set(scope["target_symbols"])
    held_symbols = set(scope["held_symbols"])
    required_quote_symbols = sorted(target_symbols | held_symbols)
    required_without_coverage = sorted(set(required_quote_symbols) - current_covered)
    lost_coverage = sorted(set(decision_final) - current_covered)
    coverage_errors = list(current_longbridge_coverage.get("errors") or [])
    blocking_symbols = sorted(
        set(scope["invalid_target_scope_symbols"]) | set(required_without_coverage)
    )
    rows: list[dict[str, Any]] = []
    current_rows = _coverage_rows_by_symbol(current_longbridge_coverage)
    for symbol in sorted(set(decision_final) | target_symbols | held_symbols):
        current_row = current_rows.get(symbol, {})
        rows.append(
            {
                "symbol": symbol,
                "in_decision_intersection": symbol in decision_final,
                "is_target": symbol in target_symbols,
                "is_held": symbol in held_symbols,
                "required_for_execution": symbol in required_quote_symbols,
                "current_longbridge_covered": symbol in current_covered,
                "coverage_lost_since_decision": symbol in lost_coverage,
                "current_longbridge_trade_status": current_row.get("trade_status"),
                "current_longbridge_last_price": current_row.get("last_price"),
                "current_longbridge_coverage_reason": current_row.get("coverage_reason"),
                "blocking": symbol in blocking_symbols,
            }
        )
    return {
        "schema_version": "1.0",
        "artifact_type": "symbol_universe_intersection",
        "mode": "execute_validation",
        "generated_at_utc": _utc_now(),
        "decision_date": decision_date.isoformat(),
        "status": "error" if coverage_errors or blocking_symbols else "pass",
        "decision_snapshot": {
            "path": decision_snapshot_path.resolve().as_posix(),
            "bytes": decision_snapshot_path.stat().st_size,
            "sha256": _sha256_file(decision_snapshot_path),
            "generated_at_utc": decision_snapshot.get("generated_at_utc"),
            "decision_status": decision_snapshot.get("status"),
        },
        "configured_count": int(decision_snapshot.get("configured_count") or 0),
        "alpaca_clean_core_count": int(decision_snapshot.get("alpaca_clean_core_count") or 0),
        "alpaca_tradable_count": int(decision_snapshot.get("alpaca_tradable_count") or 0),
        "longbridge_covered_count_at_decision": int(
            decision_snapshot.get("longbridge_covered_count") or 0
        ),
        "final_intersection_count": len(decision_final),
        "final_intersection_symbols": decision_final,
        "current_longbridge_covered_count": len(current_covered),
        "coverage_lost_since_decision_count": len(lost_coverage),
        "coverage_lost_since_decision_symbols": lost_coverage,
        "required_quote_symbol_count": len(required_quote_symbols),
        "required_quote_symbols": required_quote_symbols,
        "required_symbols_without_coverage": required_without_coverage,
        "target_scope": scope,
        "blocking_symbols": blocking_symbols,
        "current_longbridge_coverage": dict(current_longbridge_coverage),
        "rows": rows,
    }


def _write_symbol_universe_artifacts(
    output_root: Path,
    snapshot: Mapping[str, Any],
) -> tuple[Path, Path]:
    json_path = output_root / "symbol_universe_intersection.json"
    csv_path = output_root / "symbol_universe_intersection.csv"
    _write_json_file(json_path, dict(snapshot))
    rows = snapshot.get("rows") if isinstance(snapshot, Mapping) else []
    frame_rows: list[dict[str, Any]] = []
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        for raw_row in rows:
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            rejection_reasons = row.get("rejection_reasons")
            if isinstance(rejection_reasons, Sequence) and not isinstance(
                rejection_reasons, (str, bytes)
            ):
                row["rejection_reasons"] = "|".join(str(item) for item in rejection_reasons)
            frame_rows.append(row)
    pd.DataFrame(frame_rows).to_csv(csv_path, index=False)
    return json_path, csv_path


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _resolve_account_equity(
    account: Mapping[str, Any],
    signed_notional: Mapping[str, float] | None = None,
) -> tuple[float, str]:
    for field in ("portfolio_value", "equity", "last_equity"):
        value = _safe_float(account.get(field))
        if value is not None and value > 0:
            return float(value), f"alpaca_account.{field}"

    cash = _safe_float(account.get("cash"))
    if cash is not None and cash > 0:
        return float(cash), "alpaca_account.cash"

    if signed_notional:
        gross = float(sum(abs(float(value)) for value in signed_notional.values()))
        if gross > 0:
            return float(gross), "fallback.gross_position_notional"

    raise ValueError(
        "Unable to resolve positive account equity from Alpaca account fields "
        "(portfolio_value/equity/last_equity/cash)."
    )


def _buying_power(account: Mapping[str, Any]) -> tuple[float, str]:
    for field in ("daytrading_buying_power", "buying_power", "regt_buying_power"):
        value = _safe_float(account.get(field))
        if value is not None and value > 0:
            return float(value), f"alpaca_account.{field}"
    return 0.0, "unavailable"


def _total_regt_buying_power_capacity(
    *,
    account: Mapping[str, Any],
    signed_notional: Mapping[str, float],
) -> tuple[float, float, float, str]:
    long_market_value = _safe_float(account.get("long_market_value"))
    short_market_value = _safe_float(account.get("short_market_value"))
    if long_market_value is not None and short_market_value is not None:
        gross_position = abs(float(long_market_value)) + abs(float(short_market_value))
        gross_source = "alpaca_account.long_market_value+abs(short_market_value)"
    else:
        gross_position = float(sum(abs(float(value)) for value in signed_notional.values()))
        gross_source = "broker_positions.gross_signed_notional"

    regt_buying_power = _safe_float(account.get("regt_buying_power"))
    if regt_buying_power is None:
        raise ValueError(
            "Alpaca account snapshot is missing regt_buying_power; cannot enforce final gross capacity."
        )
    total_capacity = gross_position + float(regt_buying_power)
    if total_capacity <= 0.0:
        raise ValueError(
            "Reconstructed total RegT capacity must be positive: "
            f"gross={gross_position}, regt_buying_power={regt_buying_power}."
        )
    return (
        float(total_capacity),
        float(gross_position),
        float(regt_buying_power),
        f"{gross_source}+alpaca_account.regt_buying_power",
    )


def _effective_min_trade_notional(
    *,
    account_equity: float,
    absolute_floor: float,
    weight_bps: float,
) -> float:
    weight_notional = max(0.0, float(account_equity)) * max(0.0, float(weight_bps)) / 10000.0
    return float(max(0.0, float(absolute_floor), weight_notional))


def _resolve_session_idx(
    account_state: Mapping[str, Any],
    provided: int | None,
    session_date: str | None = None,
) -> int:
    if provided is not None:
        return int(provided)
    last_idx = account_state.get("last_session_idx")
    if last_idx is not None:
        last_date = account_state.get("last_session_date")
        if session_date is not None and last_date is not None and str(last_date) == str(session_date):
            return int(last_idx)
        return int(last_idx) + 1
    return 0


def _positions_to_frame_and_notional(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[pd.DataFrame, dict[str, float]]:
    rows: list[dict[str, Any]] = []
    notional: dict[str, float] = {}
    for raw in positions:
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        side = str(raw.get("side") or "").strip().lower()
        qty = _safe_float(raw.get("qty")) or 0.0
        current_price = _safe_float(raw.get("current_price"))
        market_value = _safe_float(raw.get("market_value"))
        if market_value is None and current_price is not None:
            market_value = abs(qty) * float(current_price)
        if market_value is None:
            market_value = 0.0

        if side == "short":
            qty_signed = -abs(qty)
            market_value_signed = -abs(market_value)
        else:
            qty_signed = abs(qty)
            market_value_signed = abs(market_value)
            side = "long"

        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": float(abs(qty)),
                "signed_qty": float(qty_signed),
                "current_price": float(current_price) if current_price is not None else np.nan,
                "market_value": float(market_value_signed),
                "avg_entry_price": _safe_float(raw.get("avg_entry_price")),
                "raw": dict(raw),
            }
        )
        notional[symbol] = notional.get(symbol, 0.0) + float(market_value_signed)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["symbol", "side"]).reset_index(drop=True)
    return frame, notional


def _signed_qty_from_positions(positions: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in positions:
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        qty = _safe_float(raw.get("qty")) or 0.0
        side = str(raw.get("side") or "").strip().lower()
        signed_qty = -abs(float(qty)) if side == "short" else abs(float(qty))
        out[symbol] = out.get(symbol, 0.0) + float(signed_qty)
    return out


def _weights_from_signed_notional(
    signed_notional: Mapping[str, float],
    *,
    equity: float,
) -> dict[str, float]:
    safe_equity = max(float(equity), 1e-9)
    out: dict[str, float] = {}
    for symbol, value in signed_notional.items():
        weight = float(value) / safe_equity
        if abs(weight) > EPS:
            out[str(symbol).upper()] = float(weight)
    return out


def _split_signed_weights(signed_weights: Mapping[str, float]) -> dict[str, dict[str, float]]:
    long_weights: dict[str, float] = {}
    short_weights: dict[str, float] = {}
    for symbol, raw_weight in signed_weights.items():
        weight = float(raw_weight)
        symbol_text = str(symbol).strip().upper()
        if not symbol_text or abs(weight) <= EPS:
            continue
        if weight > 0:
            long_weights[symbol_text] = weight
        else:
            short_weights[symbol_text] = abs(weight)
    return {"long": long_weights, "short": short_weights}


def _signed_weights_from_decision_targets(targets: pd.DataFrame) -> dict[str, float]:
    if targets.empty:
        return {}
    required = {"symbol", "signed_weight"}
    if not required.issubset(targets.columns):
        raise ValueError(f"decision targets missing columns: {sorted(required - set(targets.columns))}")
    out: dict[str, float] = {}
    for row in targets[["symbol", "signed_weight"]].itertuples(index=False):
        symbol = str(row.symbol).strip().upper()
        weight = float(row.signed_weight)
        if symbol and abs(weight) > EPS:
            out[symbol] = weight
    return out


def _build_fallback_price_map(
    *,
    alpha_panel: pd.DataFrame,
    broker_positions: pd.DataFrame,
) -> dict[str, float]:
    out: dict[str, float] = {}
    if not alpha_panel.empty:
        alpha_tmp = alpha_panel.copy()
        alpha_tmp["symbol"] = alpha_tmp["symbol"].astype(str).str.upper()
        for row in alpha_tmp.itertuples(index=False):
            symbol = str(row.symbol).upper()
            px = _safe_float(getattr(row, "close", None))
            if px is None or px <= 0:
                px = _safe_float(getattr(row, "lagged_raw_close", None))
            if px is not None and px > 0:
                out[symbol] = float(px)
    if not broker_positions.empty:
        for row in broker_positions.itertuples(index=False):
            symbol = str(row.symbol).upper()
            px = _safe_float(getattr(row, "current_price", None))
            if px is not None and px > 0:
                out[symbol] = float(px)
    return out


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("chunk size must be > 0")
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


def _resolve_reference_prices(
    *,
    client: Any,
    symbols: Sequence[str],
    fallback_prices: Mapping[str, float],
    feed: str,
    prefer_live: bool = False,
    allow_fallback: bool = True,
    require_fresh: bool = False,
) -> dict[str, float]:
    fallback: dict[str, float] = {
        str(symbol).upper(): float(price)
        for symbol, price in fallback_prices.items()
        if _safe_float(price) is not None and float(price) > 0
    }
    requested = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    out: dict[str, float] = {} if prefer_live else dict(fallback)
    needed = requested if prefer_live else [symbol for symbol in requested if symbol not in out]
    if hasattr(client, "get_reference_prices"):
        live_prices = client.get_reference_prices(needed)
        for symbol, price in live_prices.items():
            px = _safe_float(price)
            if px is not None and px > 0:
                out[str(symbol).upper()] = float(px)
    else:
        for chunk in _chunks(needed, 150):
            try:
                trades = client.get_latest_trades(symbols=chunk, feed=str(feed))
            except AlpacaRequestError:
                continue
            for symbol, trade in trades.items():
                px = _safe_float(trade.get("p"))
                if px is not None and px > 0:
                    out[str(symbol).upper()] = float(px)
    if allow_fallback:
        for symbol, price in fallback.items():
            out.setdefault(str(symbol).upper(), float(price))
    if require_fresh:
        missing = sorted(set(requested) - set(out))
        if missing:
            raise LongbridgeQuoteError("Missing fresh Longbridge prices: " + ", ".join(missing))
    return out


def _quantize_qty(raw_qty: float, *, whole_shares_only: bool, decimals: int) -> float:
    qty = max(0.0, float(raw_qty))
    if whole_shares_only:
        return float(math.floor(qty))
    scale = 10 ** max(0, int(decimals))
    return float(math.floor(qty * scale) / scale)


def _is_effectively_whole_qty(value: float, *, decimals: int) -> bool:
    tolerance = max(1e-9, 0.5 * (10 ** -max(0, int(decimals))))
    return abs(float(value) - round(float(value))) <= tolerance


def _adverse_price(*, side: str, reference_price: float, offset_bps: float) -> float:
    px = max(float(reference_price), 1e-9)
    k = max(float(offset_bps), 0.0) / 10000.0
    if str(side).lower() == "buy":
        return float(px * (1.0 + k))
    return float(max(px * (1.0 - k), 1e-9))


def _project_short_targets_to_whole_shares(
    *,
    signed_weights: Mapping[str, float],
    reference_prices: Mapping[str, float],
    account_equity: float,
    sizing_adverse_offset_bps: float,
    enabled: bool,
) -> tuple[dict[str, float], dict[str, float]]:
    effective: dict[str, float] = {}
    short_names = 0
    short_zeroed = 0
    lost_notional = 0.0
    desired_short_notional = 0.0
    realized_short_notional = 0.0
    safe_equity = max(float(account_equity), 1e-9)
    for symbol_raw, raw_weight in signed_weights.items():
        symbol = str(symbol_raw).strip().upper()
        if not symbol:
            continue
        weight = float(raw_weight)
        if abs(weight) <= EPS:
            continue
        if weight >= 0.0 or not enabled:
            effective[symbol] = float(weight)
            continue

        short_names += 1
        px = _safe_float(reference_prices.get(symbol))
        if px is None or px <= 0:
            effective[symbol] = float(weight)
            continue
        sizing_px = _adverse_price(
            side="sell",
            reference_price=float(px),
            offset_bps=float(sizing_adverse_offset_bps),
        )
        desired = abs(weight) * safe_equity
        desired_short_notional += float(desired)
        floored_shares = float(math.floor(max(0.0, desired / float(sizing_px)) + 1e-12))
        realized = floored_shares * float(sizing_px)
        realized_short_notional += float(realized)
        lost_notional += max(0.0, float(desired) - float(realized))
        if floored_shares <= 0.0:
            short_zeroed += 1
            continue
        effective[symbol] = float(-(realized / safe_equity))
    return effective, {
        "short_names": float(short_names),
        "short_zeroed": float(short_zeroed),
        "lost_notional": float(lost_notional),
        "desired_short_notional": float(desired_short_notional),
        "realized_short_notional": float(realized_short_notional),
        "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
    }


def _projected_whole_share_qty(raw_qty: float, *, integer_tolerance: float = 0.20) -> float:
    if raw_qty <= EPS:
        return 0.0
    nearest = round(float(raw_qty))
    if nearest > 0 and abs(float(raw_qty) - float(nearest)) <= float(integer_tolerance):
        return float(nearest)
    return float(math.floor(float(raw_qty) + 1e-12))


def _build_order_instructions(
    *,
    target_signed_weights: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    current_signed_qty: Mapping[str, float] | None,
    account_equity: float,
    reference_prices: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    min_trade_notional: float,
    sizing_adverse_offset_bps: float,
    qty_decimals: int,
    whole_shares_only: bool,
    opening_shorts_whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    shorting_enabled: bool,
) -> tuple[list[OrderInstruction], list[dict[str, Any]]]:
    symbols = sorted(set(target_signed_weights) | set(current_signed_notional))
    instructions: list[OrderInstruction] = []
    skipped: list[dict[str, Any]] = []
    for symbol in symbols:
        target_notional = float(account_equity) * float(target_signed_weights.get(symbol, 0.0))
        current_notional = float(current_signed_notional.get(symbol, 0.0))
        delta_notional = target_notional - current_notional
        if abs(delta_notional) < float(min_trade_notional):
            continue
        px = _safe_float(reference_prices.get(symbol))
        if px is None or px <= 0:
            skipped.append({"symbol": symbol, "reason": "missing_reference_price", "delta_notional": delta_notional})
            continue
        side = "buy" if delta_notional > 0 else "sell"
        sizing_price = _adverse_price(
            side=side,
            reference_price=float(px),
            offset_bps=float(sizing_adverse_offset_bps),
        )
        signed_qty = float((current_signed_qty or {}).get(symbol, 0.0))
        release_long = side == "sell" and current_notional > EPS and target_notional >= -EPS
        cover_short = side == "buy" and current_notional < -EPS and target_notional <= EPS
        opening_short = side == "sell" and target_notional < 0 and current_notional <= EPS
        increasing_short = side == "sell" and target_notional < current_notional and target_notional < -EPS
        short_sale = bool(opening_short or increasing_short)
        if opening_short:
            if not shorting_enabled:
                skipped.append({"symbol": symbol, "reason": "account_shorting_disabled", "delta_notional": delta_notional})
                continue
            asset = assets_by_symbol.get(symbol, {})
            shortable = bool(asset.get("shortable", False))
            if not shortable:
                skipped.append({"symbol": symbol, "reason": "asset_not_shortable", "delta_notional": delta_notional})
                continue

        should_force_whole_share = bool(whole_shares_only) or (
            bool(opening_shorts_whole_shares_only) and bool(opening_short)
        ) or (
            bool(short_sales_whole_shares_only) and bool(short_sale)
        )

        raw_qty = abs(delta_notional) / float(sizing_price)
        current_short_qty = max(0.0, -float(signed_qty))
        current_short_whole_qty = _projected_whole_share_qty(current_short_qty)
        target_short_raw_qty = 0.0
        target_short_qty = 0.0
        if target_notional < -EPS:
            target_short_raw_qty = abs(target_notional) / float(sizing_price)
            target_short_qty = _projected_whole_share_qty(target_short_raw_qty)
        target_signed_qty = 0.0
        if target_notional > EPS:
            target_signed_qty = _quantize_qty(
                target_notional / float(sizing_price),
                whole_shares_only=bool(whole_shares_only),
                decimals=qty_decimals,
            )
        elif target_notional < -EPS:
            target_signed_qty = -float(target_short_qty)
        if short_sale and target_short_qty > current_short_whole_qty + EPS:
            raw_qty = target_short_qty - current_short_whole_qty
        elif cover_short:
            target_short_qty_for_cover = target_short_qty if bool(short_sales_whole_shares_only) else max(0.0, float(target_short_raw_qty))
            if bool(short_sales_whole_shares_only) and target_short_qty > EPS:
                raw_qty = max(0.0, current_short_whole_qty - target_short_qty)
            else:
                raw_qty = min(
                    current_short_qty,
                    max(0.0, current_short_qty - target_short_qty_for_cover),
                )
        if release_long:
            raw_qty = min(float(raw_qty), max(0.0, float(signed_qty)))

        force_whole_qty = bool(should_force_whole_share)
        if cover_short and bool(short_sales_whole_shares_only):
            closing_short_to_flat = target_short_qty <= EPS
            remaining_short_qty = max(0.0, current_short_qty - float(raw_qty))
            fractional_cover_to_whole_short = (
                bool(closing_short_to_flat)
                and not _is_effectively_whole_qty(raw_qty, decimals=qty_decimals)
                and not _is_effectively_whole_qty(current_short_qty, decimals=qty_decimals)
                and _is_effectively_whole_qty(remaining_short_qty, decimals=qty_decimals)
            )
            force_whole_qty = not fractional_cover_to_whole_short

        qty = _quantize_qty(raw_qty, whole_shares_only=force_whole_qty, decimals=qty_decimals)
        if qty <= 0:
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "qty_rounded_to_zero",
                    "delta_notional": delta_notional,
                    "price": px,
                    "raw_qty": raw_qty,
                    "current_short_qty": current_short_qty,
                    "current_short_whole_qty": current_short_whole_qty,
                    "target_short_raw_qty": target_short_raw_qty,
                    "target_short_qty": target_short_qty,
                    "whole_share_required": bool(force_whole_qty),
                }
            )
            continue
        est_notional = qty * float(px)
        min_trade_after_rounding = float(min_trade_notional)
        if force_whole_qty and abs(delta_notional) >= float(min_trade_notional):
            min_trade_after_rounding = min(float(min_trade_notional), max(0.0, abs(delta_notional) * 0.45))
        if est_notional < min_trade_after_rounding:
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "notional_below_threshold_after_rounding",
                    "delta_notional": delta_notional,
                    "estimated_notional": est_notional,
                    "min_trade_notional_after_rounding": min_trade_after_rounding,
                    "raw_qty": raw_qty,
                    "planned_qty": qty,
                    "current_short_qty": current_short_qty,
                    "current_short_whole_qty": current_short_whole_qty,
                    "target_short_raw_qty": target_short_raw_qty,
                    "target_short_qty": target_short_qty,
                    "whole_share_required": bool(force_whole_qty),
                }
            )
            continue
        instructions.append(
            OrderInstruction(
                symbol=symbol,
                side=side,
                qty=float(qty),
                reference_price=float(px),
                sizing_price=float(sizing_price),
                current_notional=float(current_notional),
                target_notional=float(target_notional),
                delta_notional=float(delta_notional),
                opening_short=bool(opening_short),
                current_signed_qty=float(signed_qty),
                target_signed_qty=float(target_signed_qty),
            )
        )
    instructions.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    return instructions, skipped


def _is_release_instruction(item: OrderInstruction) -> bool:
    current = float(item.current_notional)
    target = float(item.target_notional)
    if abs(current) <= EPS:
        return False
    if current > EPS:
        return item.side == "sell" and target < current - EPS
    if current < -EPS:
        return item.side == "buy" and target > current + EPS
    return False


def _split_release_entry_instructions(
    instructions: Sequence[OrderInstruction],
    *,
    current_signed_qty: Mapping[str, float] | None = None,
) -> tuple[list[OrderInstruction], list[OrderInstruction]]:
    release: list[OrderInstruction] = []
    entry: list[OrderInstruction] = []
    for item in instructions:
        current_notional = float(item.current_notional)
        target_notional = float(item.target_notional)
        sign_flip = (
            current_notional > EPS and target_notional < -EPS
        ) or (
            current_notional < -EPS and target_notional > EPS
        )
        if sign_flip:
            symbol = str(item.symbol).upper()
            current_qty_hint = _safe_float((current_signed_qty or {}).get(symbol))
            if current_qty_hint is None:
                current_qty_hint = _safe_float(item.current_signed_qty)
            if current_qty_hint is None or current_qty_hint * current_notional <= 0:
                current_qty_hint = math.copysign(
                    abs(current_notional) / max(float(item.reference_price), 1e-9),
                    current_notional,
                )

            target_qty_hint = _safe_float(item.target_signed_qty)
            if target_qty_hint is None or target_qty_hint * target_notional <= 0:
                target_qty_abs = abs(target_notional) / max(float(item.sizing_price), 1e-9)
                if target_notional < -EPS:
                    target_qty_abs = _projected_whole_share_qty(target_qty_abs)
                target_qty_hint = math.copysign(target_qty_abs, target_notional)

            close_qty = abs(float(current_qty_hint))
            open_qty = abs(float(target_qty_hint))
            if close_qty > EPS:
                release.append(
                    OrderInstruction(
                        symbol=item.symbol,
                        side="sell" if current_notional > 0 else "buy",
                        qty=float(close_qty),
                        reference_price=float(item.reference_price),
                        sizing_price=float(item.sizing_price),
                        current_notional=float(current_notional),
                        target_notional=0.0,
                        delta_notional=float(-current_notional),
                        opening_short=False,
                        current_signed_qty=float(current_qty_hint),
                        target_signed_qty=0.0,
                    )
                )
            if open_qty > EPS:
                entry.append(
                    OrderInstruction(
                        symbol=item.symbol,
                        side="sell" if target_notional < 0 else "buy",
                        qty=float(open_qty),
                        reference_price=float(item.reference_price),
                        sizing_price=float(item.sizing_price),
                        current_notional=0.0,
                        target_notional=float(target_notional),
                        delta_notional=float(target_notional),
                        opening_short=bool(target_notional < 0),
                        current_signed_qty=0.0,
                        target_signed_qty=float(target_qty_hint),
                    )
                )
            continue
        if _is_release_instruction(item):
            release.append(item)
        else:
            entry.append(item)
    release.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    entry.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    return release, entry


def _split_release_substages(
    instructions: Sequence[OrderInstruction],
) -> tuple[list[OrderInstruction], list[OrderInstruction]]:
    sell_long: list[OrderInstruction] = []
    buy_to_cover: list[OrderInstruction] = []
    for item in instructions:
        if item.side == "sell" and float(item.current_notional) > EPS:
            sell_long.append(item)
        elif item.side == "buy" and float(item.current_notional) < -EPS:
            buy_to_cover.append(item)
    sell_long.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    buy_to_cover.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    return sell_long, buy_to_cover


def _release_action_class(item: OrderInstruction) -> str:
    if item.side == "sell" and float(item.current_notional) > EPS:
        return "release_sell_long"
    if item.side == "buy" and float(item.current_notional) < -EPS:
        return "release_buy_to_cover"
    raise ValueError(
        f"Instruction {item.symbol} is not a position-reducing release order"
    )


def _order_buying_power_notional(
    item: OrderInstruction,
    *,
    short_buying_power_adverse_offset_bps: float,
) -> float:
    if item.side == "buy":
        return float(item.qty) * float(item.sizing_price)
    if item.target_notional < -EPS:
        return float(item.qty) * _short_buying_power_price(
            item,
            short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
        )
    return 0.0


def _short_buying_power_price(
    item: OrderInstruction,
    *,
    short_buying_power_adverse_offset_bps: float,
) -> float:
    return _adverse_price(
        side="buy",
        reference_price=float(item.reference_price),
        offset_bps=float(short_buying_power_adverse_offset_bps),
    )


def _order_record_fully_filled(record: Mapping[str, Any]) -> bool:
    status = str(record.get("status_latest") or "").strip().lower()
    if status != "filled":
        return False
    remaining_qty = _safe_float(record.get("remaining_qty"))
    if remaining_qty is not None and remaining_qty > 1e-6:
        return False
    submitted_qty = _safe_float(record.get("qty"))
    filled_qty = _safe_float(record.get("filled_qty"))
    if submitted_qty is not None and filled_qty is not None and filled_qty + 1e-6 < submitted_qty:
        return False
    return True


def _all_order_records_fully_filled(records: Sequence[Mapping[str, Any]]) -> bool:
    return bool(records) and all(_order_record_fully_filled(record) for record in records)


def _scale_entry_instructions_to_buying_power(
    instructions: Sequence[OrderInstruction],
    *,
    buying_power: float,
    buffer: float,
    min_trade_notional: float,
    qty_decimals: int,
    whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    short_buying_power_adverse_offset_bps: float,
) -> tuple[list[OrderInstruction], dict[str, Any]]:
    cap = max(0.0, float(buying_power) * min(max(float(buffer), 0.0), 1.0))
    used = 0.0
    out: list[OrderInstruction] = []
    scaled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in instructions:
        required = _order_buying_power_notional(
            item,
            short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
        )
        if required <= EPS:
            out.append(item)
            continue
        remaining = max(0.0, cap - used)
        if remaining <= EPS:
            skipped.append({"symbol": item.symbol, "reason": "buying_power_cap_exhausted", "required": required})
            continue
        qty = float(item.qty)
        if required > remaining:
            force_whole = bool(whole_shares_only) or (
                bool(short_sales_whole_shares_only) and item.side == "sell" and item.target_notional < -EPS
            )
            cap_price = (
                _short_buying_power_price(
                    item,
                    short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
                )
                if item.side == "sell" and item.target_notional < -EPS
                else item.sizing_price
            )
            qty = _quantize_qty(
                remaining / max(float(cap_price), 1e-9),
                whole_shares_only=force_whole,
                decimals=qty_decimals,
            )
            scaled.append(
                {
                    "symbol": item.symbol,
                    "original_qty": float(item.qty),
                    "scaled_qty": float(qty),
                    "required_notional": float(required),
                    "remaining_cap": float(remaining),
                }
            )
        est_notional = qty * (
            _short_buying_power_price(
                item,
                short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
            )
            if item.side == "sell" and item.target_notional < -EPS
            else item.sizing_price
        )
        if qty <= EPS or est_notional < float(min_trade_notional):
            skipped.append(
                {
                    "symbol": item.symbol,
                    "reason": "entry_scaled_below_min_trade_notional",
                    "scaled_qty": float(qty),
                    "estimated_notional": float(est_notional),
                }
            )
            continue
        used += float(est_notional)
        out.append(
            OrderInstruction(
                symbol=item.symbol,
                side=item.side,
                qty=float(qty),
                reference_price=float(item.reference_price),
                sizing_price=float(item.sizing_price),
                current_notional=float(item.current_notional),
                target_notional=float(item.target_notional),
                delta_notional=float(math.copysign(est_notional, item.delta_notional)),
                opening_short=bool(item.opening_short),
                current_signed_qty=item.current_signed_qty,
                target_signed_qty=item.target_signed_qty,
            )
        )
    return out, {
        "buying_power": float(buying_power),
        "buffer": float(buffer),
        "cap": float(cap),
        "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
        "estimated_used": float(used),
        "scaled": scaled,
        "skipped": skipped,
    }


def _parse_clock_timestamp(raw: Any) -> datetime:
    token = str(raw or "").strip()
    if not token:
        raise ValueError("clock timestamp is empty")
    parsed = pd.Timestamp(token).to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _wait_for_market_open(*, client: AlpacaHttpClient, open_buffer_seconds: int) -> None:
    clock = client.get_clock()
    is_open = bool(clock.get("is_open", False))
    if is_open:
        return
    next_open = _parse_clock_timestamp(clock.get("next_open"))
    now_utc = datetime.now(timezone.utc)
    wait_seconds = (next_open - now_utc).total_seconds() + max(0, int(open_buffer_seconds))
    if wait_seconds <= 0:
        return

    ny_tz = ZoneInfo("America/New_York")
    print(
        f"[Executor] waiting for market open at {next_open.astimezone(ny_tz).isoformat()} "
        f"(sleep {wait_seconds:.1f}s)",
        flush=True,
    )
    remaining = wait_seconds
    while remaining > 0:
        step = min(30.0, remaining)
        time.sleep(step)
        remaining -= step


def _wait_for_target_ny_time(
    *,
    client: AlpacaHttpClient,
    target_ny_time: str,
    open_buffer_seconds: int,
) -> None:
    ny_tz = ZoneInfo("America/New_York")
    hour, minute = _parse_hhmm(target_ny_time)
    announced = False
    buffer = max(0, int(open_buffer_seconds))

    while True:
        clock = client.get_clock()
        is_open = bool(clock.get("is_open", False))
        now_raw = clock.get("timestamp")
        if now_raw:
            now = _parse_clock_timestamp(now_raw).astimezone(ny_tz)
        else:
            now = datetime.now(timezone.utc).astimezone(ny_tz)
        next_open = _parse_clock_timestamp(clock.get("next_open")).astimezone(ny_tz)

        if is_open:
            session_open = datetime.combine(now.date(), dt_time(9, 30), tzinfo=ny_tz) + timedelta(seconds=buffer)
            target_at = datetime.combine(now.date(), dt_time(hour, minute), tzinfo=ny_tz)
            target_at = max(target_at, session_open)
        else:
            session_open = next_open + timedelta(seconds=buffer)
            target_at = datetime.combine(next_open.date(), dt_time(hour, minute), tzinfo=ny_tz)
            target_at = max(target_at, session_open)

        wait_seconds = (target_at - now).total_seconds()
        if wait_seconds <= 0 and is_open:
            return

        if not announced:
            print(
                f"[Executor] waiting for target NY time {target_at.isoformat()} "
                f"(now={now.isoformat()}, sleep ~{max(wait_seconds, 0.0):.1f}s)",
                flush=True,
            )
            announced = True

        time.sleep(min(30.0, max(1.0, wait_seconds)))


def _format_qty(qty: float) -> str:
    return f"{float(qty):.8f}".rstrip("0").rstrip(".")


def _client_order_id(
    run_token: str,
    *,
    idx: int,
    side: str,
    symbol: str,
    attempt_no: int | None = None,
) -> str:
    side_code = "b" if str(side).lower() == "buy" else "s"
    symbol_text = re.sub(r"[^a-z0-9]", "", str(symbol).lower())[:10] or "sym"
    suffix = f"a{int(attempt_no):02d}" if attempt_no is not None else "m"
    return f"sm_{str(run_token).lower()}_{int(idx):03d}_{side_code}_{symbol_text}_{suffix}"[:48]


def _format_limit_price(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _marketable_limit_price(*, side: str, reference_price: float, offset_bps: float) -> float:
    px = max(float(reference_price), 1e-9)
    k = max(float(offset_bps), 0.0) / 10000.0
    if str(side).lower() == "buy":
        raw = px * (1.0 + k)
    else:
        raw = px * (1.0 - k)
    tick = 0.01 if px >= 1.0 else 0.0001
    if str(side).lower() == "buy":
        quantized = math.ceil(raw / tick) * tick
    else:
        quantized = math.floor(raw / tick) * tick
    return max(float(quantized), tick)


def _order_status(order: Mapping[str, Any] | None) -> str:
    if not order:
        return ""
    return str(order.get("status") or "").strip().lower()


def _order_event_qtys(order: Mapping[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    if not order:
        return None, None, None
    qty = _safe_float(order.get("qty"))
    filled_qty = _safe_float(order.get("filled_qty"))
    remaining_qty: float | None = None
    if qty is not None and filled_qty is not None:
        remaining_qty = max(0.0, float(qty) - float(filled_qty))
    return qty, filled_qty, remaining_qty


def _append_order_timeline_event(
    events: list[dict[str, Any]],
    *,
    event: str,
    order_id: str,
    order: Mapping[str, Any] | None = None,
    started_monotonic: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    qty, filled_qty, remaining_qty = _order_event_qtys(order)
    now_monotonic = time.monotonic()
    item: dict[str, Any] = {
        "seq": int(len(events) + 1),
        "event": str(event),
        "at_utc": _utc_now(),
        "elapsed_ms": round((now_monotonic - started_monotonic) * 1000.0, 3)
        if started_monotonic is not None
        else None,
        "order_id": str(order_id or (order or {}).get("id") or ""),
        "client_order_id": str((order or {}).get("client_order_id") or ""),
        "symbol": str((order or {}).get("symbol") or ""),
        "side": str((order or {}).get("side") or ""),
        "order_type": str((order or {}).get("type") or ""),
        "time_in_force": str((order or {}).get("time_in_force") or ""),
        "status": _order_status(order),
        "qty": qty,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "filled_avg_price": _safe_float((order or {}).get("filled_avg_price")) if order else None,
        "limit_price": _safe_float((order or {}).get("limit_price")) if order else None,
        "submitted_at": str((order or {}).get("submitted_at") or ""),
        "updated_at": str((order or {}).get("updated_at") or ""),
        "filled_at": str((order or {}).get("filled_at") or ""),
        "canceled_at": str((order or {}).get("canceled_at") or ""),
        "expired_at": str((order or {}).get("expired_at") or ""),
        "failed_at": str((order or {}).get("failed_at") or ""),
    }
    if extra:
        item.update(dict(extra))
    events.append(item)


def _poll_order_until(
    *,
    client: AlpacaHttpClient,
    order_id: str,
    deadline_monotonic: float,
    poll_seconds: float,
    poll_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    poll_started_monotonic = time.monotonic()
    while True:
        request_started_monotonic = time.monotonic()
        try:
            latest = client.get_order(order_id)
        except Exception as exc:
            if poll_events is not None:
                _append_order_timeline_event(
                    poll_events,
                    event="poll_error",
                    order_id=order_id,
                    started_monotonic=poll_started_monotonic,
                    extra={
                        "request_elapsed_ms": round((time.monotonic() - request_started_monotonic) * 1000.0, 3),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            raise
        status = _order_status(latest)
        now_monotonic = time.monotonic()
        if poll_events is not None:
            _append_order_timeline_event(
                poll_events,
                event="poll",
                order_id=order_id,
                order=latest,
                started_monotonic=poll_started_monotonic,
                extra={
                    "request_elapsed_ms": round((now_monotonic - request_started_monotonic) * 1000.0, 3),
                    "terminal_status": bool(status in TERMINAL_ORDER_STATUSES),
                    "deadline_reached": bool(now_monotonic >= deadline_monotonic),
                    "seconds_to_deadline": round(float(deadline_monotonic - now_monotonic), 3),
                },
            )
        if status in TERMINAL_ORDER_STATUSES:
            return latest
        if now_monotonic >= deadline_monotonic:
            if poll_events is not None:
                _append_order_timeline_event(
                    poll_events,
                    event="poll_deadline_reached",
                    order_id=order_id,
                    order=latest,
                    started_monotonic=poll_started_monotonic,
                )
            return latest
        time.sleep(max(0.5, float(poll_seconds)))


def _record_order_ids(execution_records: Sequence[Mapping[str, Any]]) -> list[str]:
    order_ids: list[str] = []
    seen: set[str] = set()
    for record in execution_records:
        for raw in [record.get("order_id")]:
            order_id = str(raw or "").strip()
            if order_id and order_id not in seen:
                seen.add(order_id)
                order_ids.append(order_id)
        attempts = record.get("attempts")
        if isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                order_id = str(attempt.get("order_id") or "").strip()
                if order_id and order_id not in seen:
                    seen.add(order_id)
                    order_ids.append(order_id)
    return order_ids


def _build_order_poll_timeline(execution_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for record_index, record in enumerate(execution_records, start=1):
        base = {
            "record_index": int(record_index),
            "record_symbol": str(record.get("symbol") or ""),
            "record_side": str(record.get("side") or ""),
            "record_stage": str(record.get("stage") or ""),
            "record_execution_order_style": str(record.get("execution_order_style") or ""),
            "record_client_order_id": str(record.get("client_order_id") or ""),
            "record_order_id": str(record.get("order_id") or ""),
        }
        for event in record.get("poll_events") or []:
            if isinstance(event, Mapping):
                events.append({**base, "attempt_no": None, **dict(event)})
        attempts = record.get("attempts")
        if isinstance(attempts, Sequence) and not isinstance(attempts, (str, bytes)):
            for attempt_index, attempt in enumerate(attempts, start=1):
                if not isinstance(attempt, Mapping):
                    continue
                attempt_base = {
                    **base,
                    "attempt_index": int(attempt_index),
                    "attempt_no": int(attempt.get("attempt_no") or attempt_index),
                    "attempt_client_order_id": str(attempt.get("client_order_id") or ""),
                    "attempt_order_id": str(attempt.get("order_id") or ""),
                    "attempt_limit_price": _safe_float(attempt.get("limit_price")),
                    "attempt_offset_bps": _safe_float(attempt.get("offset_bps")),
                }
                for event in attempt.get("poll_events") or []:
                    if isinstance(event, Mapping):
                        events.append({**attempt_base, **dict(event)})
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "record_count": int(len(execution_records)),
        "event_count": int(len(events)),
        "events": events,
    }


def _collect_broker_order_snapshots(
    *,
    client: AlpacaHttpClient,
    execution_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist final broker order payloads for every submitted order id."""
    snapshots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for order_id in _record_order_ids(execution_records):
        try:
            snapshots.append(client.get_order(order_id))
        except AlpacaRequestError as exc:
            errors.append({"order_id": order_id, "error": str(exc)})
    return {
        "collected_at_utc": _utc_now(),
        "order_ids": _record_order_ids(execution_records),
        "snapshots": snapshots,
        "errors": errors,
    }


def _collect_broker_fill_activities(
    *,
    client: AlpacaHttpClient,
    session_date: date,
    execution_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist raw broker FILL activities matching this executor run.

    Alpaca order snapshots expose aggregate filled qty/avg price.  The FILL
    activity endpoint is the durable source for individual executions.  We match
    by order_id where possible and keep unmatched same-day symbols separately so
    audits can spot broker/API edge cases.
    """
    order_ids = set(_record_order_ids(execution_records))
    symbols = {
        str(record.get("symbol") or "").strip().upper()
        for record in execution_records
        if str(record.get("symbol") or "").strip()
    }
    try:
        activities = client.list_account_activities(
            activity_types="FILL",
            date=session_date.isoformat(),
            direction="asc",
            page_size=100,
        )
    except AlpacaRequestError as exc:
        return {
            "collected_at_utc": _utc_now(),
            "session_date": session_date.isoformat(),
            "order_ids": sorted(order_ids),
            "symbols": sorted(symbols),
            "activities": [],
            "matched_activities": [],
            "unmatched_same_day_symbol_activities": [],
            "errors": [{"error": str(exc)}],
        }

    matched: list[dict[str, Any]] = []
    unmatched_same_day_symbols: list[dict[str, Any]] = []
    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        activity_order_id = str(activity.get("order_id") or "").strip()
        activity_symbol = str(activity.get("symbol") or "").strip().upper()
        if activity_order_id and activity_order_id in order_ids:
            matched.append(dict(activity))
        elif activity_symbol in symbols:
            unmatched_same_day_symbols.append(dict(activity))
    return {
        "collected_at_utc": _utc_now(),
        "session_date": session_date.isoformat(),
        "order_ids": sorted(order_ids),
        "symbols": sorted(symbols),
        "activities": activities,
        "matched_activities": matched,
        "unmatched_same_day_symbol_activities": unmatched_same_day_symbols,
        "errors": [],
    }


def _relevant_corporate_action_symbols(
    *,
    universe_symbols: Sequence[str],
    raw_target_signed_weights: Mapping[str, float],
    target_signed_weights: Mapping[str, float],
    broker_signed_notional_before: Mapping[str, float],
    instructions: Sequence[OrderInstruction],
) -> list[str]:
    symbols: set[str] = set()
    for source in [
        raw_target_signed_weights.keys(),
        target_signed_weights.keys(),
        broker_signed_notional_before.keys(),
        (item.symbol for item in instructions),
    ]:
        for raw in source:
            symbol = str(raw or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    if not symbols:
        symbols.update(str(raw or "").strip().upper() for raw in universe_symbols if str(raw or "").strip())
    return sorted(symbol for symbol in symbols if symbol)


def _chunks(items: Sequence[str], chunk_size: int) -> Iterable[list[str]]:
    size = max(1, int(chunk_size))
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _collect_relevant_corporate_actions(
    *,
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    session_date: date,
    lookback_days: int = 10,
    lookahead_days: int = 3,
    chunk_size: int = 100,
) -> dict[str, Any]:
    requested_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    window_start = (session_date - timedelta(days=max(0, int(lookback_days)))).isoformat()
    window_end = (session_date + timedelta(days=max(0, int(lookahead_days)))).isoformat()
    actions: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if requested_symbols:
        for chunk_index, chunk in enumerate(_chunks(requested_symbols, chunk_size), start=1):
            try:
                chunk_actions = client.get_corporate_actions(
                    symbols=chunk,
                    start=window_start,
                    end=window_end,
                    limit=1000,
                )
                actions.extend(dict(item) for item in chunk_actions if isinstance(item, Mapping))
            except Exception as exc:
                errors.append(
                    {
                        "chunk_index": int(chunk_index),
                        "symbols": list(chunk),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    action_symbols = sorted(
        {
            str(
                action.get("symbol")
                or action.get("new_symbol")
                or action.get("old_symbol")
                or action.get("target_symbol")
                or ""
            )
            .strip()
            .upper()
            for action in actions
            if str(
                action.get("symbol")
                or action.get("new_symbol")
                or action.get("old_symbol")
                or action.get("target_symbol")
                or ""
            ).strip()
        }
    )
    return {
        "schema_version": "1.0",
        "ok": not errors,
        "name": "get_corporate_actions_relevant",
        "collected_at_utc": _utc_now(),
        "session_date": session_date.isoformat(),
        "window_start": window_start,
        "window_end": window_end,
        "lookback_days": int(max(0, int(lookback_days))),
        "lookahead_days": int(max(0, int(lookahead_days))),
        "requested_symbol_count": len(requested_symbols),
        "requested_symbols": requested_symbols,
        "chunk_size": int(max(1, int(chunk_size))),
        "chunk_count": int(math.ceil(len(requested_symbols) / max(1, int(chunk_size)))) if requested_symbols else 0,
        "action_count": len(actions),
        "action_symbols": action_symbols,
        "actions": actions,
        "errors": errors,
    }


def _collect_portfolio_history_snapshot(
    *,
    client: AlpacaHttpClient,
    session_date: date,
    label: str,
) -> dict[str, Any]:
    return _safe_broker_call(
        f"get_portfolio_history_{label}",
        lambda: client.get_portfolio_history(
            timeframe="1Min",
            intraday_reporting="market_hours",
            pnl_reset="no_reset",
            start=f"{session_date.isoformat()}T00:00:00Z",
            end=f"{(session_date + timedelta(days=1)).isoformat()}T00:00:00Z",
            extended_hours=False,
        ),
    )


def _collect_calendar_window(
    *,
    client: AlpacaHttpClient,
    session_date: date,
    lookback_days: int = 14,
    lookahead_days: int = 7,
) -> dict[str, Any]:
    start_date = session_date - timedelta(days=max(0, int(lookback_days)))
    end_date = session_date + timedelta(days=max(0, int(lookahead_days)))

    def fetch() -> dict[str, Any]:
        rows = client.get_calendar(start=start_date.isoformat(), end=end_date.isoformat())
        row_dates = [str(row.get("date") or "") for row in rows if isinstance(row, dict)]
        session_row = next(
            (dict(row) for row in rows if isinstance(row, dict) and str(row.get("date") or "") == session_date.isoformat()),
            None,
        )
        return {
            "session_date": session_date.isoformat(),
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
            "lookback_days": int(lookback_days),
            "lookahead_days": int(lookahead_days),
            "row_count": len(rows),
            "session_row": session_row,
            "calendar_dates": row_dates,
            "rows": rows,
        }

    return _safe_broker_call("get_calendar_window", fetch)


def _collect_intraday_bars_snapshot(
    *,
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    session_date: date,
    feed: str,
    label: str,
    chunk_size: int = 100,
    fallback_feed: str | None = "sip",
) -> dict[str, Any]:
    requested_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    start = f"{session_date.isoformat()}T00:00:00Z"
    end = f"{(session_date + timedelta(days=1)).isoformat()}T00:00:00Z"
    bars: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_by_symbol: dict[str, str] = {}

    def collect(request_symbols: Sequence[str], *, request_feed: str, source: str) -> None:
        for chunk_index, chunk in enumerate(_chunks(list(request_symbols), chunk_size), start=1):
            try:
                payload = client.get_stock_bars(
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Min",
                    adjustment="raw",
                    feed=str(request_feed),
                    limit=10000,
                )
                for raw in payload:
                    row = dict(raw) if isinstance(raw, Mapping) else raw
                    if isinstance(row, dict):
                        symbol = str(row.get("symbol") or "").strip().upper()
                        row["capture_feed"] = str(request_feed)
                        row["capture_source"] = str(source)
                        if symbol:
                            source_by_symbol[symbol] = str(request_feed)
                    bars.append(row)
            except Exception as exc:
                errors.append(
                    {
                        "source": str(source),
                        "feed": str(request_feed),
                        "chunk_index": int(chunk_index),
                        "symbols": list(chunk),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

    collect(requested_symbols, request_feed=str(feed), source="primary")
    primary_bar_symbols = sorted(source_by_symbol)
    fallback_requested_symbols: list[str] = []
    normalized_fallback_feed = str(fallback_feed or "").strip().lower()
    if normalized_fallback_feed and normalized_fallback_feed != str(feed).strip().lower():
        fallback_requested_symbols = sorted(set(requested_symbols) - set(primary_bar_symbols))
        if fallback_requested_symbols:
            collect(
                fallback_requested_symbols,
                request_feed=normalized_fallback_feed,
                source="fallback_for_primary_missing",
            )
    bar_symbols = sorted(
        {
            str(row.get("symbol") or "").strip().upper()
            for row in bars
            if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
        }
    )
    return {
        "schema_version": "1.0",
        "ok": not errors,
        "name": "get_intraday_bars_1min_relevant",
        "label": str(label),
        "collected_at_utc": _utc_now(),
        "session_date": session_date.isoformat(),
        "feed": str(feed),
        "primary_feed": str(feed),
        "fallback_feed": normalized_fallback_feed or None,
        "fallback_attempted": bool(fallback_requested_symbols),
        "fallback_requested_symbol_count": len(fallback_requested_symbols),
        "fallback_requested_symbols": fallback_requested_symbols,
        "primary_bar_symbol_count": len(primary_bar_symbols),
        "primary_bar_symbols": primary_bar_symbols,
        "fallback_bar_symbol_count": sum(
            source == normalized_fallback_feed for source in source_by_symbol.values()
        ),
        "fallback_bar_symbols": sorted(
            symbol for symbol, source in source_by_symbol.items() if source == normalized_fallback_feed
        ),
        "source_by_symbol": dict(sorted(source_by_symbol.items())),
        "source_counts": dict(sorted(Counter(source_by_symbol.values()).items())),
        "timeframe": "1Min",
        "adjustment": "raw",
        "start": start,
        "end": end,
        "requested_symbol_count": len(requested_symbols),
        "requested_symbols": requested_symbols,
        "bar_symbol_count": len(bar_symbols),
        "bar_symbols": bar_symbols,
        "bar_count": len(bars),
        "missing_bar_symbols": sorted(set(requested_symbols) - set(bar_symbols)),
        "chunk_size": int(max(1, int(chunk_size))),
        "primary_chunk_count": int(math.ceil(len(requested_symbols) / max(1, int(chunk_size))))
        if requested_symbols
        else 0,
        "fallback_chunk_count": int(
            math.ceil(len(fallback_requested_symbols) / max(1, int(chunk_size)))
        )
        if fallback_requested_symbols
        else 0,
        "chunk_count": (
            int(math.ceil(len(requested_symbols) / max(1, int(chunk_size))))
            if requested_symbols
            else 0
        )
        + (
            int(math.ceil(len(fallback_requested_symbols) / max(1, int(chunk_size))))
            if fallback_requested_symbols
            else 0
        ),
        "bars": [dict(row) if isinstance(row, Mapping) else row for row in bars],
        "errors": errors,
    }


def _marketable_offset_ladder(
    *,
    base_offset_bps: float,
    max_offset_bps: float,
    requote_steps_bps: Sequence[float],
    max_attempts: int,
) -> list[float]:
    base = max(0.0, float(base_offset_bps))
    cap = max(0.0, float(max_offset_bps))
    offsets: list[float] = []
    for step in sorted({max(0.0, float(value)) for value in requote_steps_bps} or {0.0}):
        value = base + step
        if cap > 0.0:
            value = min(value, cap)
        if not offsets or abs(value - offsets[-1]) > 1e-9:
            offsets.append(float(value))
    if cap > 0.0 and (not offsets or offsets[-1] < cap - 1e-9):
        offsets.append(float(cap))
    return offsets[: max(1, int(max_attempts))]


def _live_marketable_reference_price(
    *,
    client: Any,
    instruction: OrderInstruction,
    execution_price_feed: str,
    strict: bool = False,
) -> tuple[float, str, dict[str, Any] | None, str | None]:
    fallback = max(float(instruction.reference_price), 1e-9)
    try:
        if hasattr(client, "get_marketable_quote"):
            quote = client.get_marketable_quote(str(instruction.symbol))
        else:
            quotes = client.get_latest_quotes(
                symbols=[str(instruction.symbol)],
                feed=str(execution_price_feed),
            )
            quote = quotes.get(str(instruction.symbol).upper(), {})
        field = "ap" if str(instruction.side).lower() == "buy" else "bp"
        live_price = _safe_float(quote.get(field)) if isinstance(quote, Mapping) else None
        if live_price is not None and live_price > EPS:
            provider = str(quote.get("provider") or "alpaca") if isinstance(quote, Mapping) else "alpaca"
            source = f"latest_quote.{field}" if provider == "alpaca" else f"{provider}.latest_quote.{field}"
            return float(live_price), source, dict(quote), None
        if strict:
            raise LongbridgeQuoteError(
                f"{instruction.symbol}: missing positive execution quote field {field}"
            )
        return fallback, "instruction_reference_fallback", dict(quote), f"missing_positive_{field}"
    except LongbridgeQuoteError:
        if strict:
            raise
        return fallback, "instruction_reference_fallback", None, "LongbridgeQuoteError"
    except Exception as exc:
        if strict:
            raise LongbridgeQuoteError(
                f"{instruction.symbol}: execution quote refresh failed: {type(exc).__name__}: {exc}"
            ) from exc
        return fallback, "instruction_reference_fallback", None, f"{type(exc).__name__}: {exc}"


def _quote_execution_evidence(
    quote: Mapping[str, Any] | None,
    *,
    side: str,
) -> dict[str, Any]:
    payload = dict(quote) if isinstance(quote, Mapping) else {}
    observed_at = datetime.now(timezone.utc)
    bid = _safe_float(payload.get("bp"))
    ask = _safe_float(payload.get("ap"))
    bid_size = _safe_float(payload.get("bs"))
    ask_size = _safe_float(payload.get("as"))
    mid = (float(bid) + float(ask)) / 2.0 if bid and ask and bid > 0 and ask > 0 else None
    spread = float(ask) - float(bid) if bid and ask and ask >= bid else None
    spread_bps = float(spread / mid * 10000.0) if spread is not None and mid and mid > 0 else None
    quote_timestamp = str(payload.get("t") or "")
    quote_age_ms: float | None = None
    if quote_timestamp:
        try:
            quote_epoch = float(pd.Timestamp(quote_timestamp).timestamp())
            quote_age_ms = (observed_at.timestamp() - quote_epoch) * 1000.0
        except Exception:
            quote_age_ms = None
    reference_field = "ap" if str(side).lower() == "buy" else "bp"
    return {
        "quote_observed_at_utc": observed_at.isoformat(timespec="milliseconds"),
        "quote_timestamp_utc": quote_timestamp,
        "quote_age_ms": round(float(quote_age_ms), 3) if quote_age_ms is not None else None,
        "live_bid_price": float(bid) if bid is not None else None,
        "live_ask_price": float(ask) if ask is not None else None,
        "live_mid_price": float(mid) if mid is not None else None,
        "live_spread": float(spread) if spread is not None else None,
        "live_spread_bps": float(spread_bps) if spread_bps is not None else None,
        "live_bid_size": float(bid_size) if bid_size is not None else None,
        "live_ask_size": float(ask_size) if ask_size is not None else None,
        "live_bid_exchange": str(payload.get("bx") or ""),
        "live_ask_exchange": str(payload.get("ax") or ""),
        "live_tape": str(payload.get("z") or ""),
        "quote_provider": str(payload.get("provider") or "alpaca"),
        "quote_feed": str(payload.get("feed") or ""),
        "provider_symbol": str(payload.get("provider_symbol") or ""),
        "depth_received_at_utc": str(payload.get("depth_received_at_utc") or quote_timestamp),
        "depth_local_age_ms": _safe_float(payload.get("depth_local_age_ms")),
        "last_trade_price_at_quote": _safe_float(payload.get("last_trade_price")),
        "last_trade_timestamp_utc": str(payload.get("last_trade_timestamp_utc") or ""),
        "quote_trade_status": str(payload.get("trade_status") or ""),
        "quote_trade_session": str(payload.get("trade_session") or ""),
        "quote_validation_error": str(payload.get("validation_error") or ""),
        "marketable_reference_field": reference_field,
    }


def _order_batch_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    requested_workers: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    status_counts = Counter(str(item.get("status_latest") or "unknown") for item in records)
    work_seconds = sum(max(0.0, float(item.get("order_wall_time_seconds") or 0.0)) for item in records)
    effective_workers = max(
        (int(item.get("batch_effective_workers") or 0) for item in records),
        default=0,
    )
    return {
        "record_count": int(len(records)),
        "requested_workers": int(max(1, requested_workers)),
        "worker_safety_cap": int(MAX_SAFE_EXECUTION_WORKERS),
        "effective_workers": int(effective_workers),
        "elapsed_seconds": float(max(0.0, elapsed_seconds)),
        "aggregate_order_work_seconds": float(work_seconds),
        "parallel_speedup_ratio": (
            float(work_seconds / elapsed_seconds) if elapsed_seconds > EPS else None
        ),
        "max_queue_wait_ms": float(
            max((float(item.get("queue_wait_ms") or 0.0) for item in records), default=0.0)
        ),
        "max_order_wall_time_seconds": float(
            max((float(item.get("order_wall_time_seconds") or 0.0) for item in records), default=0.0)
        ),
        "attempt_count": int(sum(int(item.get("attempt_count") or 0) for item in records)),
        "filled_record_count": int(
            sum(float(item.get("filled_qty") or 0.0) > EPS for item in records)
        ),
        "unfilled_record_count": int(
            sum(float(item.get("remaining_qty") or 0.0) > EPS for item in records)
        ),
        "status_counts": dict(sorted(status_counts.items())),
    }


def _execution_logical_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    symbol = str(record.get("symbol") or "").upper()
    side = str(record.get("side") or "").lower()
    stage = str(record.get("stage") or "single_pass")
    if stage in {"entry", "entry_repair"}:
        stage = "entry"
    return symbol, side, stage


def _final_logical_execution_records(
    execution_records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    final_record_index_by_key: dict[tuple[str, str, str], int] = {}
    for index, record in enumerate(execution_records):
        final_record_index_by_key[_execution_logical_key(record)] = index
    return [
        execution_records[index]
        for index in sorted(final_record_index_by_key.values())
    ]


def _execution_attempt_outcome_summary(
    execution_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:

    final_record_index_by_key: dict[tuple[str, str, str], int] = {}
    record_count_by_key: Counter[tuple[str, str, str]] = Counter()
    for index, record in enumerate(execution_records):
        key = _execution_logical_key(record)
        final_record_index_by_key[key] = index
        record_count_by_key[key] += 1

    attempt_count = 0
    canceled_attempt_count = 0
    superseded_canceled_attempt_count = 0
    terminal_canceled_attempt_count = 0
    canceled_attempt_reason_counts: Counter[str] = Counter()
    canceled_attempts: list[dict[str, Any]] = []
    for index, record in enumerate(execution_records):
        key = _execution_logical_key(record)
        record_is_final = final_record_index_by_key.get(key) == index
        record_finally_filled = _order_record_fully_filled(record)
        for attempt in record.get("attempts") or []:
            attempt_count += 1
            if str(attempt.get("status_latest") or "").lower() != "canceled":
                continue
            canceled_attempt_count += 1
            cancel_reason = str(attempt.get("cancel_reason") or "legacy_unspecified")
            if record_finally_filled or not record_is_final:
                superseded_canceled_attempt_count += 1
                outcome = "superseded_requote"
            else:
                terminal_canceled_attempt_count += 1
                outcome = "terminal_unfilled"
            canceled_attempt_reason_counts[cancel_reason] += 1
            canceled_attempts.append(
                {
                    "symbol": str(record.get("symbol") or "").upper(),
                    "side": str(record.get("side") or "").lower(),
                    "stage": str(record.get("stage") or "single_pass"),
                    "order_id": str(attempt.get("order_id") or ""),
                    "attempt_no": int(attempt.get("attempt_no") or 0),
                    "cancel_reason": cancel_reason,
                    "outcome": outcome,
                    "cancel_requested_at_utc": str(
                        attempt.get("cancel_requested_at_utc") or ""
                    ),
                    "limit_price": _safe_float(attempt.get("limit_price")),
                    "offset_bps": _safe_float(attempt.get("offset_bps")),
                    "live_reference_price": _safe_float(
                        attempt.get("live_reference_price")
                    ),
                    "quote_age_ms": _safe_float(attempt.get("quote_age_ms")),
                }
            )

    final_records = _final_logical_execution_records(execution_records)
    terminal_unfilled_records = [
        record for record in final_records if not _order_record_fully_filled(record)
    ]
    repaired_entry_symbols = sorted(
        {
            key[0]
            for key, count in record_count_by_key.items()
            if key[2] == "entry"
            and count > 1
            and _order_record_fully_filled(
                execution_records[final_record_index_by_key[key]]
            )
        }
    )
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "raw_record_count": int(len(execution_records)),
        "logical_record_count": int(len(final_records)),
        "broker_attempt_count": int(attempt_count),
        "canceled_attempt_count": int(canceled_attempt_count),
        "superseded_requote_canceled_attempt_count": int(
            superseded_canceled_attempt_count
        ),
        "terminal_canceled_attempt_count": int(terminal_canceled_attempt_count),
        "canceled_attempt_reason_counts": dict(
            sorted(canceled_attempt_reason_counts.items())
        ),
        "canceled_attempts": canceled_attempts,
        "terminal_unfilled_record_count": int(len(terminal_unfilled_records)),
        "terminal_unfilled_symbols": sorted(
            {
                str(record.get("symbol") or "").upper()
                for record in terminal_unfilled_records
                if str(record.get("symbol") or "").strip()
            }
        ),
        "terminal_unfilled_records": [dict(record) for record in terminal_unfilled_records],
        "repaired_entry_symbol_count": int(len(repaired_entry_symbols)),
        "repaired_entry_symbols": repaired_entry_symbols,
        "interpretation": {
            "superseded_requote_canceled_attempts": (
                "Expected cancel-and-reprice attempts whose logical instruction later filled."
            ),
            "terminal_unfilled_records": (
                "Logical instructions still incomplete after all configured repair attempts."
            ),
        },
    }


def _submit_and_track_orders(
    *,
    client: AlpacaHttpClient,
    instructions: Sequence[OrderInstruction],
    session_token: str,
    timeout_seconds: float,
    poll_seconds: float,
    execution_order_style: str,
    marketable_limit_base_offset_bps: float,
    marketable_limit_max_offset_bps: float,
    marketable_limit_requote_steps_bps: Sequence[float],
    marketable_limit_requote_wait_seconds: float,
    marketable_limit_max_attempts: int = 4,
    max_workers: int = 1,
    execution_price_feed: str = "iex",
    execution_quote_client: Any | None = None,
    max_attempts_by_symbol: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    instruction_list = list(instructions)
    requested_workers = max(1, int(max_workers))
    effective_workers = min(
        requested_workers,
        MAX_SAFE_EXECUTION_WORKERS,
        max(1, len(instruction_list)),
    )
    symbol_attempt_limits = {
        str(symbol).upper(): max(1, int(limit))
        for symbol, limit in (max_attempts_by_symbol or {}).items()
    }
    batch_started_at_utc = _utc_now()
    batch_started_monotonic = time.monotonic()

    if len(instruction_list) > 1 and effective_workers > 1:
        indexed_records: dict[int, dict[str, Any]] = {}

        def run_one(index: int, instruction: OrderInstruction) -> tuple[int, dict[str, Any]]:
            worker_started = time.monotonic()
            child_records = _submit_and_track_orders(
                client=client,
                instructions=[instruction],
                session_token=f"{session_token}_i{index:03d}",
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
                execution_order_style=execution_order_style,
                marketable_limit_base_offset_bps=marketable_limit_base_offset_bps,
                marketable_limit_max_offset_bps=marketable_limit_max_offset_bps,
                marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                marketable_limit_requote_wait_seconds=marketable_limit_requote_wait_seconds,
                marketable_limit_max_attempts=marketable_limit_max_attempts,
                max_workers=1,
                execution_price_feed=execution_price_feed,
                execution_quote_client=execution_quote_client,
                max_attempts_by_symbol=symbol_attempt_limits,
            )
            record = dict(child_records[0])
            record.update(
                {
                    "instruction_index": int(index),
                    "dispatch_rank": int(index),
                    "batch_instruction_count": int(len(instruction_list)),
                    "batch_requested_workers": int(requested_workers),
                    "batch_worker_safety_cap": int(MAX_SAFE_EXECUTION_WORKERS),
                    "batch_effective_workers": int(effective_workers),
                    "batch_wave_index": int((index - 1) // effective_workers + 1),
                    "batch_wave_count": int(math.ceil(len(instruction_list) / effective_workers)),
                    "dispatch_policy": "instruction_order_fifo_thread_pool",
                    "batch_started_at_utc": batch_started_at_utc,
                    "queue_wait_ms": round(
                        (worker_started - batch_started_monotonic) * 1000.0,
                        3,
                    ),
                    "order_wall_time_seconds": float(time.monotonic() - worker_started),
                }
            )
            return index, record

        with ThreadPoolExecutor(
            max_workers=effective_workers,
            thread_name_prefix="alpaca-order",
        ) as executor:
            futures = {
                executor.submit(run_one, index, instruction): index
                for index, instruction in enumerate(instruction_list, start=1)
            }
            for future in as_completed(futures):
                index, record = future.result()
                indexed_records[index] = record
        return [indexed_records[index] for index in sorted(indexed_records)]

    records: list[dict[str, Any]] = []
    for idx, item in enumerate(instruction_list, start=1):
        order_started_monotonic = time.monotonic()
        item_max_attempts = symbol_attempt_limits.get(
            str(item.symbol).upper(),
            max(1, int(marketable_limit_max_attempts)),
        )
        base_record = {
            "symbol": item.symbol,
            "side": item.side,
            "qty": float(item.qty),
            "delta_notional": float(item.delta_notional),
            "reference_price": float(item.reference_price),
            "submitted_at_utc": _utc_now(),
            "instruction_index": int(idx),
            "dispatch_rank": int(idx),
            "batch_instruction_count": int(len(instruction_list)),
            "batch_requested_workers": int(requested_workers),
            "batch_worker_safety_cap": int(MAX_SAFE_EXECUTION_WORKERS),
            "batch_effective_workers": int(effective_workers),
            "batch_wave_index": int((idx - 1) // effective_workers + 1),
            "batch_wave_count": int(math.ceil(len(instruction_list) / effective_workers)),
            "dispatch_policy": "instruction_order_fifo_thread_pool",
            "batch_started_at_utc": batch_started_at_utc,
            "queue_wait_ms": 0.0,
            "marketable_limit_max_attempts": int(item_max_attempts),
        }
        try:
            if str(execution_order_style) == "market":
                client_order_id = _client_order_id(session_token, idx=idx, side=item.side, symbol=item.symbol)
                placed_order = client.submit_order(
                    symbol=item.symbol,
                    side=item.side,
                    type="market",
                    time_in_force="day",
                    qty=_format_qty(item.qty),
                    client_order_id=client_order_id,
                )
                order_id = str(placed_order.get("id") or "")
                deadline = time.monotonic() + max(1.0, float(timeout_seconds))
                latest_order = placed_order
                poll_events: list[dict[str, Any]] = []
                _append_order_timeline_event(
                    poll_events,
                    event="submitted",
                    order_id=order_id,
                    order=placed_order,
                    extra={"timeout_seconds": float(timeout_seconds), "poll_seconds": float(poll_seconds)},
                )
                if order_id:
                    latest_order = _poll_order_until(
                        client=client,
                        order_id=order_id,
                        deadline_monotonic=deadline,
                        poll_seconds=poll_seconds,
                        poll_events=poll_events,
                    )
                record = {
                    **base_record,
                    "execution_order_style": "market",
                    "client_order_id": client_order_id,
                    "order_id": order_id,
                    "status_initial": _order_status(placed_order),
                    "status_latest": _order_status(latest_order),
                    "filled_avg_price": _safe_float(latest_order.get("filled_avg_price")),
                    "filled_qty": _safe_float(latest_order.get("filled_qty")),
                    "updated_at": str(latest_order.get("updated_at") or ""),
                    "poll_event_count": int(len(poll_events)),
                    "poll_events": poll_events,
                    "placed_order_raw": placed_order,
                    "latest_order_raw": latest_order,
                    "order_wall_time_seconds": float(
                        time.monotonic() - order_started_monotonic
                    ),
                }
                records.append(record)
                continue

            remaining_qty = float(item.qty)
            fractional_close_retry_count = 0
            fractional_close_retry_original_qty: float | None = None
            fractional_close_retry_qty: float | None = None
            fractional_close_residual_qty = 0.0
            total_filled_qty = 0.0
            attempts: list[dict[str, Any]] = []
            latest_status = ""
            latest_filled_avg_price: float | None = None
            latest_updated_at = ""
            global_deadline = time.monotonic() + max(1.0, float(timeout_seconds))
            max_offset_bps = max(0.0, float(marketable_limit_max_offset_bps))
            offset_ladder = _marketable_offset_ladder(
                base_offset_bps=float(marketable_limit_base_offset_bps),
                max_offset_bps=float(max_offset_bps),
                requote_steps_bps=marketable_limit_requote_steps_bps,
                max_attempts=int(item_max_attempts),
            )

            attempt_no = 0
            while (
                remaining_qty > EPS
                and time.monotonic() < global_deadline
                and attempt_no < len(offset_ladder)
            ):
                attempt_no += 1
                step_index = attempt_no - 1
                cycle_no = 0
                if remaining_qty <= EPS:
                    break
                if time.monotonic() >= global_deadline:
                    break

                total_offset_bps = float(offset_ladder[step_index])
                live_reference_price, reference_source, live_quote, quote_error = (
                    _live_marketable_reference_price(
                        client=execution_quote_client or client,
                        instruction=item,
                        execution_price_feed=execution_price_feed,
                        strict=execution_quote_client is not None,
                    )
                )
                quote_evidence = _quote_execution_evidence(live_quote, side=item.side)
                limit_price = _marketable_limit_price(
                    side=item.side,
                    reference_price=live_reference_price,
                    offset_bps=total_offset_bps,
                )
                client_order_id = _client_order_id(
                    session_token,
                    idx=idx,
                    side=item.side,
                    symbol=item.symbol,
                    attempt_no=attempt_no,
                )
                try:
                    placed_order = client.submit_order(
                        symbol=item.symbol,
                        side=item.side,
                        type="limit",
                        time_in_force="day",
                        qty=_format_qty(remaining_qty),
                        limit_price=_format_limit_price(limit_price),
                        client_order_id=client_order_id,
                    )
                except AlpacaRequestError as exc:
                    retry_qty = _fractional_long_close_retry_qty(
                        instruction=item,
                        rejected_qty=remaining_qty,
                        exc=exc,
                    )
                    if retry_qty is None:
                        raise
                    fractional_close_retry_count += 1
                    fractional_close_retry_original_qty = float(remaining_qty)
                    fractional_close_retry_qty = float(retry_qty)
                    current_available_qty = _safe_float(item.current_signed_qty)
                    fractional_close_residual_qty += max(
                        0.0,
                        min(
                            float(remaining_qty),
                            float(current_available_qty)
                            if current_available_qty is not None
                            else float(remaining_qty),
                        )
                        - float(retry_qty),
                    )
                    remaining_qty = float(retry_qty)
                    placed_order = client.submit_order(
                        symbol=item.symbol,
                        side=item.side,
                        type="limit",
                        time_in_force="day",
                        qty=_format_qty(remaining_qty),
                        limit_price=_format_limit_price(limit_price),
                        client_order_id=client_order_id,
                    )
                order_id = str(placed_order.get("id") or "")
                attempt_deadline = min(
                    global_deadline,
                    time.monotonic() + max(1.0, float(marketable_limit_requote_wait_seconds)),
                )
                latest_order = placed_order
                attempt_poll_events: list[dict[str, Any]] = []
                cancel_reason = ""
                cancel_requested_at_utc = ""
                cancel_error_type = ""
                cancel_error = ""
                _append_order_timeline_event(
                    attempt_poll_events,
                    event="submitted",
                    order_id=order_id,
                    order=placed_order,
                    extra={
                        "attempt_no": int(attempt_no),
                        "requote_step_index": int(step_index + 1),
                        "requote_cycle": int(cycle_no + 1),
                        "timeout_seconds": round(float(attempt_deadline - time.monotonic()), 3),
                        "global_seconds_to_deadline": round(float(global_deadline - time.monotonic()), 3),
                        "poll_seconds": float(poll_seconds),
                        "max_offset_bps": float(max_offset_bps),
                        "live_reference_price": float(live_reference_price),
                        "reference_price_source": str(reference_source),
                        "quote_refresh_error": quote_error,
                        **quote_evidence,
                    },
                )
                if order_id:
                    latest_order = _poll_order_until(
                        client=client,
                        order_id=order_id,
                        deadline_monotonic=attempt_deadline,
                        poll_seconds=poll_seconds,
                        poll_events=attempt_poll_events,
                    )

                status = _order_status(latest_order)
                need_cancel = remaining_qty > EPS and status not in TERMINAL_ORDER_STATUSES
                if need_cancel and order_id:
                    cancel_reason = (
                        "global_order_timeout"
                        if time.monotonic() >= global_deadline - 1e-6
                        else "requote_wait_elapsed"
                    )
                    cancel_requested_at_utc = _utc_now()
                    try:
                        client.cancel_order(order_id)
                        _append_order_timeline_event(
                            attempt_poll_events,
                            event="cancel_requested",
                            order_id=order_id,
                            order=latest_order,
                            extra={
                                "cancel_requested_at_utc": cancel_requested_at_utc,
                                "cancel_reason": cancel_reason,
                            },
                        )
                    except AlpacaRequestError as exc:
                        cancel_error_type = type(exc).__name__
                        cancel_error = str(exc)
                        _append_order_timeline_event(
                            attempt_poll_events,
                            event="cancel_error",
                            order_id=order_id,
                            order=latest_order,
                            extra={
                                "cancel_requested_at_utc": cancel_requested_at_utc,
                                "cancel_reason": cancel_reason,
                                "error_type": cancel_error_type,
                                "error": cancel_error,
                            },
                        )
                        pass
                    latest_order = client.get_order(order_id)
                    status = _order_status(latest_order)
                    _append_order_timeline_event(
                        attempt_poll_events,
                        event="after_cancel_snapshot",
                        order_id=order_id,
                        order=latest_order,
                    )

                filled_qty_this_attempt = max(0.0, float(_safe_float(latest_order.get("filled_qty")) or 0.0))
                filled_qty_this_attempt = min(remaining_qty, filled_qty_this_attempt)
                remaining_qty = max(0.0, remaining_qty - filled_qty_this_attempt)
                total_filled_qty += filled_qty_this_attempt

                latest_status = status
                latest_filled_avg_price = _safe_float(latest_order.get("filled_avg_price"))
                latest_updated_at = str(latest_order.get("updated_at") or "")

                attempts.append(
                    {
                        "attempt_no": int(attempt_no),
                        "requote_step_index": int(step_index + 1),
                        "requote_cycle": int(cycle_no + 1),
                        "client_order_id": client_order_id,
                        "order_id": order_id,
                        "qty_submitted": float(
                            _safe_float(placed_order.get("qty")) or remaining_qty + filled_qty_this_attempt
                        ),
                        "limit_price": float(limit_price),
                        "offset_bps": float(total_offset_bps),
                        "max_offset_bps": float(max_offset_bps),
                        "live_reference_price": float(live_reference_price),
                        "reference_price_source": str(reference_source),
                        "live_quote": live_quote,
                        "quote_refresh_error": quote_error,
                        **quote_evidence,
                        "status_latest": latest_status,
                        "cancel_reason": cancel_reason,
                        "cancel_requested_at_utc": cancel_requested_at_utc,
                        "cancel_error_type": cancel_error_type,
                        "cancel_error": cancel_error,
                        "filled_qty": float(filled_qty_this_attempt),
                        "filled_avg_price": latest_filled_avg_price,
                        "updated_at": latest_updated_at,
                        "poll_event_count": int(len(attempt_poll_events)),
                        "poll_events": attempt_poll_events,
                        "placed_order_raw": placed_order,
                        "latest_order_raw": latest_order,
                    }
                )

            record = {
                **base_record,
                "execution_order_style": "marketable_limit",
                "client_order_id": attempts[-1]["client_order_id"] if attempts else "",
                "order_id": attempts[-1]["order_id"] if attempts else "",
                "status_latest": latest_status,
                "filled_qty": float(total_filled_qty),
                "remaining_qty": float(max(0.0, remaining_qty)),
                "filled_avg_price": latest_filled_avg_price,
                "updated_at": latest_updated_at,
                "attempt_count": int(len(attempts)),
                "fractional_close_retry_count": int(fractional_close_retry_count),
                "fractional_close_retry_original_qty": fractional_close_retry_original_qty,
                "fractional_close_retry_qty": fractional_close_retry_qty,
                "fractional_close_residual_qty": float(fractional_close_residual_qty),
                "offset_ladder_bps": [float(value) for value in offset_ladder],
                "attempts": attempts,
            }
            record["order_wall_time_seconds"] = float(time.monotonic() - order_started_monotonic)
            records.append(record)
        except LongbridgeQuoteError as exc:
            records.append(
                {
                    **base_record,
                    "execution_order_style": str(execution_order_style),
                    "status_latest": "quote_unavailable",
                    "filled_qty": 0.0,
                    "remaining_qty": float(item.qty),
                    "requested_qty": float(item.qty),
                    "submit_error_class": "execution_quote_unavailable",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "abort_remaining_orders": False,
                    "order_wall_time_seconds": float(time.monotonic() - order_started_monotonic),
                }
            )
        except AlpacaRequestError as exc:
            error_payload = _alpaca_error_payload(exc)
            submit_error_class = "insufficient_buying_power" if _is_insufficient_buying_power_error(exc) else (
                "insufficient_qty_available" if _is_insufficient_qty_available_error(exc) else "alpaca_submit_error"
            )
            abort_remaining = bool(_is_insufficient_buying_power_error(exc))
            records.append(
                {
                    **base_record,
                    "execution_order_style": str(execution_order_style),
                    "status_latest": "submit_error",
                    "filled_qty": 0.0,
                    "remaining_qty": float(item.qty),
                    "requested_qty": float(item.qty),
                    "submit_error_class": submit_error_class,
                    "broker_error_code": error_payload.get("code"),
                    "broker_error_message": error_payload.get("message"),
                    "broker_error_symbol": error_payload.get("symbol"),
                    "broker_available_qty": _safe_float(error_payload.get("available")),
                    "broker_existing_qty": _safe_float(error_payload.get("existing_qty")),
                    "broker_held_for_orders_qty": _safe_float(error_payload.get("held_for_orders")),
                    "broker_error_payload": error_payload,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "abort_remaining_orders": abort_remaining,
                    "order_wall_time_seconds": float(time.monotonic() - order_started_monotonic),
                }
            )
            if abort_remaining:
                break

    for record in records:
        record.setdefault(
            "order_wall_time_seconds",
            float(time.monotonic() - order_started_monotonic),
        )
    return records


def _instruction_payloads(instructions: Sequence[OrderInstruction]) -> list[dict[str, Any]]:
    return [asdict(item) for item in instructions]


def _raw_dict_list(items: Sequence[Any]) -> list[Any]:
    return [dict(item) if isinstance(item, dict) else item for item in items]


def _instruction_symbols(instructions: Sequence[OrderInstruction]) -> list[str]:
    return sorted({str(item.symbol).upper() for item in instructions if str(item.symbol).strip()})


def _wait_for_release_position_reconciliation(
    *,
    client: AlpacaHttpClient,
    release_instructions: Sequence[OrderInstruction],
    qty_decimals: int,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_signed_qty: dict[str, float] = {}
    for item in release_instructions:
        symbol = str(item.symbol).upper()
        current_qty = _safe_float(item.current_signed_qty)
        if current_qty is None:
            current_qty = math.copysign(
                abs(float(item.current_notional)) / max(float(item.reference_price), 1e-9),
                float(item.current_notional),
            )
        signed_order_qty = float(item.qty) if item.side == "buy" else -float(item.qty)
        expected_signed_qty[symbol] = float(current_qty) + signed_order_qty

    return _wait_for_expected_position_reconciliation(
        client=client,
        expected_signed_qty=expected_signed_qty,
        qty_decimals=int(qty_decimals),
        timeout_seconds=float(timeout_seconds),
        poll_seconds=float(poll_seconds),
    )


def _wait_for_order_position_reconciliation(
    *,
    client: AlpacaHttpClient,
    instructions: Sequence[OrderInstruction],
    execution_records: Sequence[Mapping[str, Any]],
    qty_decimals: int,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    instructions_by_symbol = {
        str(item.symbol).upper(): item for item in instructions
    }
    filled_qty_by_symbol: Counter[str] = Counter()
    for record in execution_records:
        symbol = str(record.get("symbol") or "").upper()
        filled_qty = max(0.0, float(_safe_float(record.get("filled_qty")) or 0.0))
        if symbol in instructions_by_symbol and filled_qty > EPS:
            filled_qty_by_symbol[symbol] += filled_qty

    expected_signed_qty: dict[str, float] = {}
    for symbol, filled_qty in filled_qty_by_symbol.items():
        item = instructions_by_symbol[symbol]
        current_qty = _safe_float(item.current_signed_qty)
        if current_qty is None:
            current_qty = math.copysign(
                abs(float(item.current_notional)) / max(float(item.reference_price), 1e-9),
                float(item.current_notional),
            )
        signed_fill_qty = float(filled_qty) if item.side == "buy" else -float(filled_qty)
        expected_signed_qty[symbol] = float(current_qty) + signed_fill_qty

    positions, diagnostics = _wait_for_expected_position_reconciliation(
        client=client,
        expected_signed_qty=expected_signed_qty,
        qty_decimals=int(qty_decimals),
        timeout_seconds=float(timeout_seconds),
        poll_seconds=float(poll_seconds),
    )
    diagnostics["filled_qty_by_symbol"] = dict(sorted(filled_qty_by_symbol.items()))
    diagnostics["instruction_symbols"] = _instruction_symbols(instructions)
    return positions, diagnostics


def _wait_for_expected_position_reconciliation(
    *,
    client: AlpacaHttpClient,
    expected_signed_qty: Mapping[str, float],
    qty_decimals: int,
    timeout_seconds: float,
    poll_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_expected_signed_qty = {
        str(symbol).upper(): float(qty)
        for symbol, qty in expected_signed_qty.items()
    }

    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_seconds))
    tolerance = max(1e-8, 1.5 * (10.0 ** -max(0, int(qty_decimals))))
    polls = 0
    positions: list[dict[str, Any]] = []
    actual_signed_qty: dict[str, float] = {}
    pending_symbols: list[str] = sorted(normalized_expected_signed_qty)
    while True:
        polls += 1
        positions = [dict(item) for item in client.list_positions()]
        actual_signed_qty = _signed_qty_from_positions(positions)
        pending_symbols = sorted(
            symbol
            for symbol, expected in normalized_expected_signed_qty.items()
            if abs(float(actual_signed_qty.get(symbol, 0.0)) - float(expected)) > tolerance
        )
        if not pending_symbols or time.monotonic() >= deadline:
            break
        time.sleep(min(1.0, max(0.05, float(poll_seconds))))

    elapsed = max(0.0, time.monotonic() - started)
    return positions, {
        "schema_version": "1.0",
        "status": "pass" if not pending_symbols else "timeout",
        "timeout_seconds": float(max(0.0, timeout_seconds)),
        "elapsed_seconds": float(elapsed),
        "poll_count": int(polls),
        "qty_tolerance": float(tolerance),
        "expected_signed_qty": dict(sorted(normalized_expected_signed_qty.items())),
        "actual_signed_qty": {
            symbol: float(actual_signed_qty.get(symbol, 0.0))
            for symbol in sorted(normalized_expected_signed_qty)
        },
        "pending_symbols": pending_symbols,
    }


def _submit_staged_regt_orders(
    *,
    client: AlpacaHttpClient,
    execution_quote_client: Any | None = None,
    initial_instructions: Sequence[OrderInstruction],
    target_signed_weights: Mapping[str, float],
    raw_target_signed_weights: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    fallback_prices: Mapping[str, float],
    session_token: str,
    execution_price_feed: str,
    account_equity: float,
    min_trade_notional_floor: float,
    min_trade_weight_bps: float,
    sizing_adverse_offset_bps: float,
    qty_decimals: int,
    whole_shares_only: bool,
    opening_shorts_whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    shorting_enabled: bool,
    buying_power_buffer: float,
    gross_capacity_target_ratio: float,
    short_buying_power_adverse_offset_bps: float,
    release_timeout_seconds: float,
    entry_timeout_seconds: float,
    entry_repair_rounds: int = 1,
    entry_repair_max_attempts: int = 1,
    entry_repair_wait_seconds: float = 10.0,
    poll_seconds: float,
    execution_order_style: str,
    marketable_limit_base_offset_bps: float,
    marketable_limit_max_offset_bps: float,
    marketable_limit_requote_steps_bps: Sequence[float],
    marketable_limit_requote_wait_seconds: float,
    marketable_limit_max_attempts: int,
    execution_workers: int,
    release_max_rounds: int,
    release_round_extra_bps: float,
    release_round_sleep_seconds: float,
    stage_snapshots: list[dict[str, Any]] | None = None,
    initial_current_signed_qty: Mapping[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots = stage_snapshots if stage_snapshots is not None else []
    release_instructions, deferred_entry_instructions = _split_release_entry_instructions(
        initial_instructions,
        current_signed_qty=initial_current_signed_qty,
    )
    release_sell_long, release_buy_to_cover = _split_release_substages(release_instructions)
    records: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "mode": "staged_regt",
        "release_execution_mode": "unified_reduce_exposure",
        "initial_order_count": int(len(initial_instructions)),
        "initial_release_count": int(len(release_instructions)),
        "initial_deferred_entry_count": int(len(deferred_entry_instructions)),
        "release_sell_long_count": int(len(release_sell_long)),
        "release_buy_to_cover_count": int(len(release_buy_to_cover)),
        "release_max_rounds": int(max(1, release_max_rounds)),
        "release_round_extra_bps": float(max(0.0, release_round_extra_bps)),
        "execution_workers": int(max(1, execution_workers)),
        "marketable_limit_max_attempts": int(max(1, marketable_limit_max_attempts)),
        "release_records": 0,
        "release_rounds": [],
        "release_substages": [],
        "release_fully_filled": True,
        "entry_aborted": False,
        "entry_abort_reason": None,
        "entry_records": 0,
        "entry_rebuild_release_residual_count": 0,
        "entry_rebuild_release_residual_records": 0,
        "entry_rebuild_release_residual_fully_filled": True,
        "entry_repair_rounds_configured": int(max(0, entry_repair_rounds)),
        "entry_repair_rounds_completed": 0,
        "entry_repair_records": 0,
        "entry_repair_final_unfilled_symbols": [],
        "entry_rebuild_skipped_orders": [],
        "entry_buying_power_cap": {},
        "entry_projection": {},
    }

    def abort_after_quote_failure(
        *,
        stage: str,
        error: LongbridgeQuoteError,
        affected_symbols: Sequence[str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        broker_mutation_record_count = sum(
            bool(str(record.get("order_id") or "").strip())
            or any(
                str(attempt.get("order_id") or "").strip()
                for attempt in (record.get("attempts") or [])
                if isinstance(attempt, Mapping)
            )
            for record in records
        )
        if broker_mutation_record_count <= 0:
            raise error
        reason = f"{stage}_quote_validation_failed_after_broker_mutation"
        diagnostics["entry_aborted"] = True
        diagnostics["entry_abort_reason"] = reason
        diagnostics["quote_validation_failure_stage"] = str(stage)
        diagnostics["quote_validation_failure_error_type"] = type(error).__name__
        diagnostics["quote_validation_failure_error"] = str(error)
        diagnostics["quote_validation_failure_symbols"] = sorted(
            {str(symbol).upper() for symbol in affected_symbols if str(symbol).strip()}
        )
        snapshots.append(
            {
                "schema_version": "1.0",
                "snapshot_type": "entry_abort",
                "captured_at_utc": _utc_now(),
                "stage": str(stage),
                "entry_abort_reason": reason,
                "quote_validation_failure_error_type": type(error).__name__,
                "quote_validation_failure_error": str(error),
                "affected_symbols": list(diagnostics["quote_validation_failure_symbols"]),
                "broker_mutation_record_count": int(broker_mutation_record_count),
            }
        )
        return records, diagnostics

    release_reference_prices = dict(fallback_prices)
    release_target_signed_weights = {
        str(item.symbol).upper(): float(item.target_notional) / max(float(account_equity), 1e-9)
        for item in release_instructions
    }
    diagnostics["release_target_signed_weights"] = dict(sorted(release_target_signed_weights.items()))
    diagnostics["initial_deferred_entry_instructions"] = _instruction_payloads(
        deferred_entry_instructions
    )
    release_symbols = sorted({str(item.symbol).upper() for item in release_instructions})
    release_action_class_by_symbol = {
        str(item.symbol).upper(): _release_action_class(item)
        for item in release_instructions
    }
    release_records_total: list[dict[str, Any]] = []
    release_fully_filled = not release_instructions
    release_remaining_instructions = list(release_instructions)
    current_release_instructions = list(release_instructions)
    release_attempt_cap = max(1, int(marketable_limit_max_attempts))
    release_attempt_counts: Counter[str] = Counter()
    release_budget_exhausted_symbols: list[str] = []

    for round_no in range(1, max(1, int(release_max_rounds)) + 1):
        if not current_release_instructions:
            break
        round_attempt_budget_before = {
            str(item.symbol).upper(): max(
                0,
                release_attempt_cap
                - int(release_attempt_counts[str(item.symbol).upper()]),
            )
            for item in current_release_instructions
        }
        round_input_instructions = [
            item
            for item in current_release_instructions
            if round_attempt_budget_before.get(str(item.symbol).upper(), 0) > 0
        ]
        if not round_input_instructions:
            release_budget_exhausted_symbols = sorted(
                {str(item.symbol).upper() for item in current_release_instructions}
            )
            break

        round_action_class_by_symbol = {
            str(item.symbol).upper(): _release_action_class(item)
            for item in round_input_instructions
        }
        round_offset_bps = float(marketable_limit_base_offset_bps) + max(
            0.0, float(release_round_extra_bps)
        ) * float(round_no - 1)
        release_batch_started = time.monotonic()
        release_records = _submit_and_track_orders(
            client=client,
            instructions=round_input_instructions,
            session_token=f"{session_token}_rel_r{round_no:02d}",
            timeout_seconds=float(release_timeout_seconds),
            poll_seconds=poll_seconds,
            execution_order_style=execution_order_style,
            marketable_limit_base_offset_bps=round_offset_bps,
            marketable_limit_max_offset_bps=marketable_limit_max_offset_bps,
            marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
            marketable_limit_requote_wait_seconds=marketable_limit_requote_wait_seconds,
            marketable_limit_max_attempts=int(marketable_limit_max_attempts),
            max_workers=int(execution_workers),
            execution_price_feed=str(execution_price_feed),
            execution_quote_client=execution_quote_client,
            max_attempts_by_symbol=round_attempt_budget_before,
        )
        release_batch_summary = _order_batch_summary(
            release_records,
            requested_workers=int(execution_workers),
            elapsed_seconds=float(time.monotonic() - release_batch_started),
        )
        for record in release_records:
            record_symbol = str(record.get("symbol") or "").upper()
            attempts_used = int(record.get("attempt_count") or 0)
            if attempts_used <= 0 and str(record.get("status_latest") or ""):
                attempts_used = 1
            attempts_before = int(release_attempt_counts[record_symbol])
            release_attempt_counts[record_symbol] = min(
                release_attempt_cap,
                attempts_before + attempts_used,
            )
            record["stage"] = round_action_class_by_symbol[record_symbol]
            record["macro_stage"] = "reduce_exposure"
            record["release_action_class"] = round_action_class_by_symbol[record_symbol]
            record["release_round"] = int(round_no)
            record["stage_symbol_attempt_cap"] = int(release_attempt_cap)
            record["stage_symbol_attempt_count_before"] = int(attempts_before)
            record["stage_symbol_attempt_count_after"] = int(
                release_attempt_counts[record_symbol]
            )
            record["stage_symbol_attempts_remaining"] = int(
                max(0, release_attempt_cap - release_attempt_counts[record_symbol])
            )
        records.extend(release_records)
        release_records_total.extend(release_records)
        diagnostics["release_records"] = int(diagnostics["release_records"]) + int(
            len(release_records)
        )

        refreshed_release_positions = client.list_positions()
        _, refreshed_release_signed_notional = _positions_to_frame_and_notional(
            refreshed_release_positions
        )
        refreshed_release_signed_qty = _signed_qty_from_positions(
            refreshed_release_positions
        )
        refreshed_release_account = client.get_account()
        refreshed_release_buying_power, refreshed_release_buying_power_source = (
            _buying_power(refreshed_release_account)
        )
        refreshed_release_equity, refreshed_release_equity_source = (
            _resolve_account_equity(
                account=refreshed_release_account,
                signed_notional=refreshed_release_signed_notional,
            )
        )
        release_price_symbols = sorted(
            set(release_symbols) | set(refreshed_release_signed_notional)
        )
        try:
            release_reference_prices = _resolve_reference_prices(
                client=execution_quote_client or client,
                symbols=release_price_symbols,
                fallback_prices=release_reference_prices,
                feed=execution_price_feed,
                prefer_live=True,
                allow_fallback=execution_quote_client is None,
                require_fresh=execution_quote_client is not None,
            )
        except LongbridgeQuoteError as exc:
            return abort_after_quote_failure(
                stage="reduce_exposure_rebuild",
                error=exc,
                affected_symbols=release_price_symbols,
            )
        release_min_trade_notional = _effective_min_trade_notional(
            account_equity=float(refreshed_release_equity),
            absolute_floor=float(min_trade_notional_floor),
            weight_bps=float(min_trade_weight_bps),
        )
        rebuilt_instructions, rebuilt_skipped = _build_order_instructions(
            target_signed_weights=release_target_signed_weights,
            current_signed_notional=refreshed_release_signed_notional,
            current_signed_qty=refreshed_release_signed_qty,
            account_equity=float(account_equity),
            reference_prices=release_reference_prices,
            assets_by_symbol=assets_by_symbol,
            min_trade_notional=float(release_min_trade_notional),
            sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
            qty_decimals=int(qty_decimals),
            whole_shares_only=bool(whole_shares_only),
            opening_shorts_whole_shares_only=bool(
                opening_shorts_whole_shares_only
            ),
            short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
            shorting_enabled=bool(shorting_enabled),
        )
        rebuilt_release, _ = _split_release_entry_instructions(
            rebuilt_instructions,
            current_signed_qty=refreshed_release_signed_qty,
        )
        rebuilt_release = [
            item
            for item in rebuilt_release
            if str(item.symbol).upper() in set(release_symbols)
        ]
        round_filled_symbols = {
            str(record.get("symbol") or "").upper()
            for record in release_records
            if _order_record_fully_filled(record)
        }
        filled_instruction_suppressed_rebuild_symbols = sorted(
            {
                str(item.symbol).upper()
                for item in rebuilt_release
                if str(item.symbol).upper() in round_filled_symbols
            }
        )
        release_remaining_instructions = [
            item
            for item in rebuilt_release
            if str(item.symbol).upper() not in round_filled_symbols
        ]
        release_remaining_symbols = [
            str(item.symbol).upper() for item in release_remaining_instructions
        ]
        release_budget_exhausted_symbols = sorted(
            {
                str(item.symbol).upper()
                for item in release_remaining_instructions
                if int(release_attempt_counts[str(item.symbol).upper()])
                >= release_attempt_cap
            }
        )
        current_release_instructions = [
            item
            for item in release_remaining_instructions
            if int(release_attempt_counts[str(item.symbol).upper()])
            < release_attempt_cap
        ]
        round_fully_filled = not release_remaining_instructions
        round_action_class_counts = Counter(
            str(record.get("release_action_class") or "")
            for record in release_records
        )
        remaining_action_class_counts = Counter(
            _release_action_class(item) for item in release_remaining_instructions
        )
        release_round_payload = {
            "stage": "reduce_exposure",
            "macro_stage": "reduce_exposure",
            "round": int(round_no),
            "order_count": int(len(release_records)),
            "record_count": int(len(release_records)),
            "action_class_counts": dict(sorted(round_action_class_counts.items())),
            "fully_filled": bool(round_fully_filled),
            "remaining_order_count": int(len(release_remaining_instructions)),
            "remaining_action_class_counts": dict(
                sorted(remaining_action_class_counts.items())
            ),
            "attempt_budget_eligible_order_count": int(
                len(current_release_instructions)
            ),
            "remaining_symbols": list(release_remaining_symbols),
            "rebuilt_skipped_orders": rebuilt_skipped,
            "limit_base_offset_bps": float(round_offset_bps),
            "marketable_limit_max_offset_bps": float(
                marketable_limit_max_offset_bps
            ),
            "stage_symbol_attempt_cap": int(release_attempt_cap),
            "stage_symbol_attempt_counts": dict(
                sorted(release_attempt_counts.items())
            ),
            "stage_attempt_budget_exhausted_symbols": list(
                release_budget_exhausted_symbols
            ),
            "buying_power_after_stage": float(refreshed_release_buying_power),
            "buying_power_source": str(refreshed_release_buying_power_source),
            "execution_batch_summary": release_batch_summary,
        }
        diagnostics["release_rounds"].append(release_round_payload)
        for action_class in ("release_sell_long", "release_buy_to_cover"):
            class_records = [
                record
                for record in release_records
                if record.get("release_action_class") == action_class
            ]
            class_remaining = [
                item
                for item in release_remaining_instructions
                if _release_action_class(item) == action_class
            ]
            if not class_records and not class_remaining:
                continue
            class_symbols = {
                str(item.symbol).upper()
                for item in release_instructions
                if release_action_class_by_symbol[str(item.symbol).upper()]
                == action_class
            }
            diagnostics["release_substages"].append(
                {
                    "stage": action_class,
                    "macro_stage": "reduce_exposure",
                    "concurrent": True,
                    "round": int(round_no),
                    "order_count": int(len(class_records)),
                    "record_count": int(len(class_records)),
                    "fully_filled": not class_remaining,
                    "remaining_order_count": int(len(class_remaining)),
                    "remaining_symbols": [
                        str(item.symbol).upper() for item in class_remaining
                    ],
                    "stage_symbol_attempt_counts": {
                        symbol: int(release_attempt_counts[symbol])
                        for symbol in sorted(class_symbols)
                    },
                    "shared_execution_batch": True,
                }
            )
        snapshots.append(
            {
                "schema_version": "1.0",
                "snapshot_type": "release_round",
                "captured_at_utc": _utc_now(),
                "stage": "reduce_exposure",
                "macro_stage": "reduce_exposure",
                "round": int(round_no),
                "stage_symbols": list(release_symbols),
                "action_class_counts": dict(sorted(round_action_class_counts.items())),
                "session_token": f"{session_token}_rel_r{round_no:02d}",
                "limit_base_offset_bps": float(round_offset_bps),
                "marketable_limit_max_offset_bps": float(
                    marketable_limit_max_offset_bps
                ),
                "marketable_limit_requote_steps_bps": [
                    float(value) for value in marketable_limit_requote_steps_bps
                ],
                "marketable_limit_requote_wait_seconds": float(
                    marketable_limit_requote_wait_seconds
                ),
                "marketable_limit_max_attempts": int(marketable_limit_max_attempts),
                "stage_symbol_attempt_cap": int(release_attempt_cap),
                "stage_symbol_attempt_counts": dict(
                    sorted(release_attempt_counts.items())
                ),
                "stage_symbol_attempt_budget_before": dict(
                    sorted(round_attempt_budget_before.items())
                ),
                "stage_attempt_budget_exhausted_symbols": list(
                    release_budget_exhausted_symbols
                ),
                "execution_workers": int(execution_workers),
                "execution_batch_summary": release_batch_summary,
                "input_instructions": _instruction_payloads(
                    round_input_instructions
                ),
                "submitted_records": release_records,
                "refreshed_positions_raw": _raw_dict_list(
                    refreshed_release_positions
                ),
                "refreshed_signed_notional": dict(
                    sorted(refreshed_release_signed_notional.items())
                ),
                "refreshed_signed_qty": dict(
                    sorted(refreshed_release_signed_qty.items())
                ),
                "refreshed_account_raw": dict(refreshed_release_account)
                if isinstance(refreshed_release_account, dict)
                else refreshed_release_account,
                "buying_power_after_stage": float(refreshed_release_buying_power),
                "buying_power_source": str(refreshed_release_buying_power_source),
                "account_equity_after_stage": float(refreshed_release_equity),
                "account_equity_source": str(refreshed_release_equity_source),
                "effective_min_trade_notional": float(release_min_trade_notional),
                "min_trade_weight_bps": float(min_trade_weight_bps),
                "reference_prices": dict(sorted(release_reference_prices.items())),
                "rebuilt_all_instructions": _instruction_payloads(
                    rebuilt_instructions
                ),
                "rebuilt_release_instructions": _instruction_payloads(
                    rebuilt_release
                ),
                "rebuilt_reduce_exposure_instructions": _instruction_payloads(
                    release_remaining_instructions
                ),
                "rebuilt_stage_instructions": _instruction_payloads(
                    release_remaining_instructions
                ),
                "round_fully_filled_symbols": sorted(round_filled_symbols),
                "filled_instruction_suppressed_rebuild_symbols": (
                    filled_instruction_suppressed_rebuild_symbols
                ),
                "attempt_budget_eligible_stage_instructions": _instruction_payloads(
                    current_release_instructions
                ),
                "rebuilt_skipped_orders": rebuilt_skipped,
                "remaining_order_count": int(len(release_remaining_instructions)),
                "attempt_budget_eligible_order_count": int(
                    len(current_release_instructions)
                ),
                "remaining_symbols": list(release_remaining_symbols),
                "fully_filled": bool(round_fully_filled),
            }
        )
        if round_fully_filled:
            release_fully_filled = True
            break
        if not current_release_instructions:
            break
        if (
            round_no < max(1, int(release_max_rounds))
            and float(release_round_sleep_seconds) > 0
        ):
            time.sleep(float(release_round_sleep_seconds))

    if not release_fully_filled:
        release_remaining_symbols = [
            str(item.symbol).upper() for item in release_remaining_instructions
        ]
        release_unfilled_action_classes = sorted(
            {_release_action_class(item) for item in release_remaining_instructions}
        )
        diagnostics["release_fully_filled"] = False
        diagnostics["entry_aborted"] = True
        diagnostics["entry_abort_reason"] = (
            "reduce_exposure_attempt_budget_exhausted"
            if release_budget_exhausted_symbols
            else "reduce_exposure_not_fully_filled_after_"
            f"{int(max(1, release_max_rounds))}_rounds"
        )
        diagnostics["release_unfilled_stage"] = "reduce_exposure"
        diagnostics["release_unfilled_action_classes"] = (
            release_unfilled_action_classes
        )
        diagnostics["release_unfilled_symbols"] = list(release_remaining_symbols)
        diagnostics["release_attempt_cap_per_symbol"] = int(release_attempt_cap)
        diagnostics["release_attempt_counts_by_symbol"] = dict(
            sorted(release_attempt_counts.items())
        )
        diagnostics["release_attempt_budget_exhausted_symbols"] = list(
            release_budget_exhausted_symbols
        )
        diagnostics["release_stage_records"] = int(len(release_records_total))
        snapshots.append(
            {
                "schema_version": "1.0",
                "snapshot_type": "entry_abort",
                "captured_at_utc": _utc_now(),
                "stage": "reduce_exposure",
                "macro_stage": "reduce_exposure",
                "entry_abort_reason": diagnostics["entry_abort_reason"],
                "remaining_symbols": list(release_remaining_symbols),
                "release_unfilled_action_classes": release_unfilled_action_classes,
                "stage_symbol_attempt_cap": int(release_attempt_cap),
                "stage_symbol_attempt_counts": dict(
                    sorted(release_attempt_counts.items())
                ),
                "stage_attempt_budget_exhausted_symbols": list(
                    release_budget_exhausted_symbols
                ),
                "release_stage_record_count": int(len(release_records_total)),
                "release_fully_filled": False,
            }
        )
        return records, diagnostics

    if release_instructions:
        refreshed_positions, release_position_reconciliation = (
            _wait_for_release_position_reconciliation(
                client=client,
                release_instructions=release_instructions,
                qty_decimals=int(qty_decimals),
                timeout_seconds=min(30.0, max(5.0, float(release_timeout_seconds) / 4.0)),
                poll_seconds=float(poll_seconds),
            )
        )
        diagnostics["release_position_reconciliation"] = release_position_reconciliation
        snapshots.append(
            {
                "schema_version": "1.0",
                "snapshot_type": "release_position_reconciliation",
                "captured_at_utc": _utc_now(),
                **release_position_reconciliation,
                "positions_raw": _raw_dict_list(refreshed_positions),
            }
        )
        if release_position_reconciliation["pending_symbols"]:
            diagnostics["release_fully_filled"] = False
            diagnostics["entry_aborted"] = True
            diagnostics["entry_abort_reason"] = "release_position_reconciliation_timeout"
            diagnostics["release_unfilled_symbols"] = list(
                release_position_reconciliation["pending_symbols"]
            )
            return records, diagnostics
    else:
        refreshed_positions = client.list_positions()
    _, refreshed_signed_notional = _positions_to_frame_and_notional(refreshed_positions)
    refreshed_signed_qty = _signed_qty_from_positions(refreshed_positions)
    refreshed_account = client.get_account()
    buying_power, buying_power_source = _buying_power(refreshed_account)
    (
        total_regt_capacity,
        refreshed_gross_position,
        refreshed_regt_buying_power,
        total_regt_capacity_source,
    ) = _total_regt_buying_power_capacity(
        account=refreshed_account,
        signed_notional=refreshed_signed_notional,
    )
    refreshed_equity, refreshed_equity_source = _resolve_account_equity(
        account=refreshed_account,
        signed_notional=refreshed_signed_notional,
    )
    entry_price_symbols = sorted(set(target_signed_weights) | set(refreshed_signed_notional))
    try:
        refreshed_prices = _resolve_reference_prices(
            client=execution_quote_client or client,
            symbols=entry_price_symbols,
            fallback_prices=fallback_prices,
            feed=execution_price_feed,
            prefer_live=True,
            allow_fallback=execution_quote_client is None,
            require_fresh=execution_quote_client is not None,
        )
    except LongbridgeQuoteError as exc:
        return abort_after_quote_failure(
            stage="entry_rebuild",
            error=exc,
            affected_symbols=entry_price_symbols,
        )
    entry_min_trade_notional = _effective_min_trade_notional(
        account_equity=float(refreshed_equity),
        absolute_floor=float(min_trade_notional_floor),
        weight_bps=float(min_trade_weight_bps),
    )
    entry_target_signed_weights, entry_target_lattice_signed_qty, entry_projection = project_executable_targets(
        raw_target_signed_weights=raw_target_signed_weights,
        current_signed_qty=refreshed_signed_qty,
        current_signed_notional=refreshed_signed_notional,
        reference_prices=refreshed_prices,
        assets_by_symbol=assets_by_symbol,
        account_equity=float(refreshed_equity),
        buying_power=float(buying_power),
        buying_power_buffer=float(buying_power_buffer),
        min_trade_notional=float(entry_min_trade_notional),
        qty_decimals=int(qty_decimals),
        whole_shares_only=bool(whole_shares_only),
        short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
        shorting_enabled=bool(shorting_enabled),
        sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
        short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
        total_buying_power_capacity=float(total_regt_capacity),
        gross_capacity_target_ratio=float(gross_capacity_target_ratio),
    )
    entry_instructions, entry_skipped = _build_order_instructions(
        target_signed_weights=entry_target_signed_weights,
        current_signed_notional=refreshed_signed_notional,
        current_signed_qty=refreshed_signed_qty,
        account_equity=float(refreshed_equity),
        reference_prices=refreshed_prices,
        assets_by_symbol=assets_by_symbol,
        min_trade_notional=float(entry_min_trade_notional),
        sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
        qty_decimals=int(qty_decimals),
        whole_shares_only=bool(whole_shares_only),
        opening_shorts_whole_shares_only=bool(opening_shorts_whole_shares_only),
        short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
        shorting_enabled=bool(shorting_enabled),
    )
    rebuilt_all_entry_instructions = list(entry_instructions)
    rebuilt_release_residual, entry_instructions = _split_release_entry_instructions(
        entry_instructions,
        current_signed_qty=refreshed_signed_qty,
    )
    entry_instructions_before_cap = list(entry_instructions)
    entry_release_residual_records: list[dict[str, Any]] = []
    entry_release_residual_batch_summary: dict[str, Any] = {}
    entry_release_residual_reconciliation: dict[str, Any] = {}
    entry_buying_power_for_submission = float(buying_power)
    entry_buying_power_source_for_submission = str(buying_power_source)

    if rebuilt_release_residual:
        residual_snapshot: dict[str, Any] = {
            "schema_version": "1.0",
            "snapshot_type": "entry_rebuild_release_residual",
            "captured_at_utc": _utc_now(),
            "stage": "reduce_exposure",
            "macro_stage": "reduce_exposure",
            "origin": "entry_rebuild",
            "session_token": f"{session_token}_ent_rel",
            "execution_workers": int(execution_workers),
            "input_instructions": _instruction_payloads(rebuilt_release_residual),
            "submitted_records": [],
        }
        snapshots.append(residual_snapshot)
        residual_batch_started = time.monotonic()
        entry_release_residual_records = _submit_and_track_orders(
            client=client,
            instructions=rebuilt_release_residual,
            session_token=f"{session_token}_ent_rel",
            timeout_seconds=float(release_timeout_seconds),
            poll_seconds=poll_seconds,
            execution_order_style=execution_order_style,
            marketable_limit_base_offset_bps=marketable_limit_base_offset_bps,
            marketable_limit_max_offset_bps=marketable_limit_max_offset_bps,
            marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
            marketable_limit_requote_wait_seconds=marketable_limit_requote_wait_seconds,
            marketable_limit_max_attempts=int(marketable_limit_max_attempts),
            max_workers=int(execution_workers),
            execution_price_feed=str(execution_price_feed),
            execution_quote_client=execution_quote_client,
        )
        entry_release_residual_batch_summary = _order_batch_summary(
            entry_release_residual_records,
            requested_workers=int(execution_workers),
            elapsed_seconds=float(time.monotonic() - residual_batch_started),
        )
        residual_action_by_symbol = {
            str(item.symbol).upper(): _release_action_class(item)
            for item in rebuilt_release_residual
        }
        for record in entry_release_residual_records:
            symbol = str(record.get("symbol") or "").upper()
            record["stage"] = residual_action_by_symbol.get(
                symbol, "reduce_exposure"
            )
            record["macro_stage"] = "reduce_exposure"
            record["release_origin"] = "entry_rebuild"
        records.extend(entry_release_residual_records)
        residual_snapshot["submitted_records"] = entry_release_residual_records
        residual_snapshot["submitted_record_count"] = int(
            len(entry_release_residual_records)
        )
        residual_snapshot["execution_batch_summary"] = (
            entry_release_residual_batch_summary
        )

        residual_instruction_symbols = {
            str(item.symbol).upper() for item in rebuilt_release_residual
        }
        residual_filled_symbols = {
            str(record.get("symbol") or "").upper()
            for record in entry_release_residual_records
            if str(record.get("symbol") or "").strip()
            and _order_record_fully_filled(record)
        }
        residual_unfilled_symbols = sorted(
            residual_instruction_symbols - residual_filled_symbols
        )
        diagnostics["entry_rebuild_release_residual_count"] = int(
            len(rebuilt_release_residual)
        )
        diagnostics["entry_rebuild_release_residual_records"] = int(
            len(entry_release_residual_records)
        )
        diagnostics["entry_rebuild_release_residual_batch_summary"] = (
            entry_release_residual_batch_summary
        )
        diagnostics["entry_rebuild_release_residual_fully_filled"] = bool(
            not residual_unfilled_symbols
        )
        if residual_unfilled_symbols:
            diagnostics["entry_aborted"] = True
            diagnostics["entry_abort_reason"] = (
                "entry_rebuild_release_residual_not_fully_filled"
            )
            diagnostics["release_unfilled_symbols"] = residual_unfilled_symbols
            residual_snapshot["fully_filled"] = False
            residual_snapshot["remaining_symbols"] = residual_unfilled_symbols
            snapshots.append(
                {
                    "schema_version": "1.0",
                    "snapshot_type": "entry_abort",
                    "captured_at_utc": _utc_now(),
                    "stage": "entry_rebuild_release_residual",
                    "entry_abort_reason": diagnostics["entry_abort_reason"],
                    "remaining_symbols": residual_unfilled_symbols,
                }
            )
            return records, diagnostics

        residual_positions, entry_release_residual_reconciliation = (
            _wait_for_release_position_reconciliation(
                client=client,
                release_instructions=rebuilt_release_residual,
                qty_decimals=int(qty_decimals),
                timeout_seconds=min(
                    30.0, max(5.0, float(release_timeout_seconds) / 4.0)
                ),
                poll_seconds=float(poll_seconds),
            )
        )
        diagnostics["entry_rebuild_release_residual_reconciliation"] = (
            entry_release_residual_reconciliation
        )
        residual_snapshot["position_reconciliation"] = (
            entry_release_residual_reconciliation
        )
        residual_snapshot["positions_raw"] = _raw_dict_list(residual_positions)
        residual_pending_symbols = list(
            entry_release_residual_reconciliation.get("pending_symbols") or []
        )
        if residual_pending_symbols:
            diagnostics["entry_rebuild_release_residual_fully_filled"] = False
            diagnostics["entry_aborted"] = True
            diagnostics["entry_abort_reason"] = (
                "entry_rebuild_release_residual_reconciliation_timeout"
            )
            diagnostics["release_unfilled_symbols"] = residual_pending_symbols
            residual_snapshot["fully_filled"] = False
            residual_snapshot["remaining_symbols"] = residual_pending_symbols
            snapshots.append(
                {
                    "schema_version": "1.0",
                    "snapshot_type": "entry_abort",
                    "captured_at_utc": _utc_now(),
                    "stage": "entry_rebuild_release_residual",
                    "entry_abort_reason": diagnostics["entry_abort_reason"],
                    "remaining_symbols": residual_pending_symbols,
                }
            )
            return records, diagnostics

        _, residual_signed_notional = _positions_to_frame_and_notional(
            residual_positions
        )
        residual_account = client.get_account()
        (
            entry_buying_power_for_submission,
            entry_buying_power_source_for_submission,
        ) = _buying_power(residual_account)
        residual_snapshot["fully_filled"] = True
        residual_snapshot["buying_power_after_residual"] = float(
            entry_buying_power_for_submission
        )
        residual_snapshot["buying_power_source_after_residual"] = str(
            entry_buying_power_source_for_submission
        )
        residual_snapshot["signed_notional_after_residual"] = dict(
            sorted(residual_signed_notional.items())
        )

    entry_instructions, cap_diag = _scale_entry_instructions_to_buying_power(
        entry_instructions,
        buying_power=float(entry_buying_power_for_submission),
        buffer=float(buying_power_buffer),
        min_trade_notional=float(entry_min_trade_notional),
        qty_decimals=int(qty_decimals),
        whole_shares_only=bool(whole_shares_only),
        short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
        short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
    )
    diagnostics.update(
        {
            "fresh_buying_power": float(buying_power),
            "fresh_buying_power_source": str(buying_power_source),
            "fresh_gross_position": float(refreshed_gross_position),
            "fresh_regt_buying_power": float(refreshed_regt_buying_power),
            "fresh_total_regt_capacity": float(total_regt_capacity),
            "fresh_total_regt_capacity_source": str(total_regt_capacity_source),
            "gross_capacity_target_ratio": float(gross_capacity_target_ratio),
            "initial_account_equity": float(account_equity),
            "fresh_account_equity": float(refreshed_equity),
            "fresh_account_equity_source": str(refreshed_equity_source),
            "effective_min_trade_notional": float(entry_min_trade_notional),
            "min_trade_weight_bps": float(min_trade_weight_bps),
            "entry_rebuild_order_count": int(len(entry_instructions)),
            "entry_rebuild_skipped_orders": entry_skipped,
            "entry_buying_power_for_submission": float(
                entry_buying_power_for_submission
            ),
            "entry_buying_power_source_for_submission": str(
                entry_buying_power_source_for_submission
            ),
            "entry_buying_power_cap": cap_diag,
            "entry_projection": entry_projection,
        }
    )
    entry_snapshot = {
        "schema_version": "1.0",
        "snapshot_type": "entry_rebuild",
        "captured_at_utc": _utc_now(),
        "stage": "entry",
        "session_token": f"{session_token}_ent",
        "marketable_limit_base_offset_bps": float(marketable_limit_base_offset_bps),
        "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
        "refreshed_positions_raw": _raw_dict_list(refreshed_positions),
        "refreshed_signed_notional": dict(sorted(refreshed_signed_notional.items())),
        "refreshed_signed_qty": dict(sorted(refreshed_signed_qty.items())),
        "refreshed_account_raw": dict(refreshed_account) if isinstance(refreshed_account, dict) else refreshed_account,
        "fresh_buying_power": float(buying_power),
        "fresh_buying_power_source": str(buying_power_source),
        "fresh_gross_position": float(refreshed_gross_position),
        "fresh_regt_buying_power": float(refreshed_regt_buying_power),
        "fresh_total_regt_capacity": float(total_regt_capacity),
        "fresh_total_regt_capacity_source": str(total_regt_capacity_source),
        "gross_capacity_target_ratio": float(gross_capacity_target_ratio),
        "fresh_account_equity": float(refreshed_equity),
        "fresh_account_equity_source": str(refreshed_equity_source),
        "effective_min_trade_notional": float(entry_min_trade_notional),
        "min_trade_weight_bps": float(min_trade_weight_bps),
        "reference_prices": dict(sorted(refreshed_prices.items())),
        "raw_target_signed_weights": dict(sorted(raw_target_signed_weights.items())),
        "entry_order_target_signed_weights": dict(sorted(entry_target_signed_weights.items())),
        "entry_target_lattice_signed_qty": dict(sorted(entry_target_lattice_signed_qty.items())),
        "entry_executable_expected_signed_weights": dict(
            sorted((entry_projection.get("executable_expected_signed_weights") or {}).items())
        ),
        "entry_executable_target_projection": entry_projection,
        "rebuilt_all_instructions": _instruction_payloads(rebuilt_all_entry_instructions),
        "rebuilt_release_residual_instructions": _instruction_payloads(rebuilt_release_residual),
        "release_residual_submitted_records": entry_release_residual_records,
        "release_residual_execution_batch_summary": entry_release_residual_batch_summary,
        "release_residual_position_reconciliation": entry_release_residual_reconciliation,
        "entry_buying_power_for_submission": float(entry_buying_power_for_submission),
        "entry_buying_power_source_for_submission": str(
            entry_buying_power_source_for_submission
        ),
        "entry_instructions_before_buying_power_cap": _instruction_payloads(entry_instructions_before_cap),
        "entry_skipped_orders": entry_skipped,
        "entry_buying_power_cap": cap_diag,
        "final_entry_instructions": _instruction_payloads(entry_instructions),
        "final_entry_symbols": _instruction_symbols(entry_instructions),
        "submitted_records": [],
    }
    snapshots.append(entry_snapshot)

    entry_records: list[dict[str, Any]] = []
    if entry_instructions:
        entry_batch_started = time.monotonic()
        entry_records = _submit_and_track_orders(
            client=client,
            instructions=entry_instructions,
            session_token=f"{session_token}_ent",
            timeout_seconds=float(entry_timeout_seconds),
            poll_seconds=poll_seconds,
            execution_order_style=execution_order_style,
            marketable_limit_base_offset_bps=marketable_limit_base_offset_bps,
            marketable_limit_max_offset_bps=marketable_limit_max_offset_bps,
            marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
            marketable_limit_requote_wait_seconds=marketable_limit_requote_wait_seconds,
            marketable_limit_max_attempts=int(marketable_limit_max_attempts),
            max_workers=int(execution_workers),
            execution_price_feed=str(execution_price_feed),
            execution_quote_client=execution_quote_client,
        )
        entry_batch_summary = _order_batch_summary(
            entry_records,
            requested_workers=int(execution_workers),
            elapsed_seconds=float(time.monotonic() - entry_batch_started),
        )
        for record in entry_records:
            record["stage"] = "entry"
        records.extend(entry_records)
        diagnostics["entry_records"] = int(len(entry_records))
        diagnostics["entry_execution_batch_summary"] = entry_batch_summary
        entry_snapshot["submitted_records"] = entry_records
        entry_snapshot["submitted_record_count"] = int(len(entry_records))
        entry_snapshot["execution_batch_summary"] = entry_batch_summary
    else:
        entry_snapshot["entry_submission_skipped_reason"] = "no_entry_instructions_after_rebuild_or_buying_power_cap"
        entry_snapshot["submitted_record_count"] = 0

    latest_entry_instructions = list(entry_instructions)
    latest_entry_records = list(entry_records)
    for repair_round in range(1, max(0, int(entry_repair_rounds)) + 1):
        repair_candidate_symbols = sorted(
            {
                str(record.get("symbol") or "").upper()
                for record in latest_entry_records
                if str(record.get("symbol") or "").strip()
                and not _order_record_fully_filled(record)
            }
        )
        if not repair_candidate_symbols:
            break

        repair_positions, repair_position_reconciliation = (
            _wait_for_order_position_reconciliation(
                client=client,
                instructions=latest_entry_instructions,
                execution_records=latest_entry_records,
                qty_decimals=int(qty_decimals),
                timeout_seconds=min(20.0, max(5.0, float(entry_timeout_seconds) / 4.0)),
                poll_seconds=float(poll_seconds),
            )
        )
        _, repair_signed_notional = _positions_to_frame_and_notional(repair_positions)
        repair_signed_qty = _signed_qty_from_positions(repair_positions)
        repair_account = client.get_account()
        repair_buying_power, repair_buying_power_source = _buying_power(repair_account)
        (
            repair_total_regt_capacity,
            repair_gross_position,
            repair_regt_buying_power,
            repair_total_regt_capacity_source,
        ) = _total_regt_buying_power_capacity(
            account=repair_account,
            signed_notional=repair_signed_notional,
        )
        repair_equity, repair_equity_source = _resolve_account_equity(
            account=repair_account,
            signed_notional=repair_signed_notional,
        )
        repair_price_symbols = sorted(set(target_signed_weights) | set(repair_signed_notional))
        try:
            repair_prices = _resolve_reference_prices(
                client=execution_quote_client or client,
                symbols=repair_price_symbols,
                fallback_prices=refreshed_prices,
                feed=execution_price_feed,
                prefer_live=True,
                allow_fallback=execution_quote_client is None,
                require_fresh=execution_quote_client is not None,
            )
        except LongbridgeQuoteError as exc:
            diagnostics["entry_repair_final_unfilled_symbols"] = list(
                repair_candidate_symbols
            )
            return abort_after_quote_failure(
                stage=f"entry_repair_round_{repair_round}",
                error=exc,
                affected_symbols=repair_price_symbols,
            )
        repair_min_trade_notional = _effective_min_trade_notional(
            account_equity=float(repair_equity),
            absolute_floor=float(min_trade_notional_floor),
            weight_bps=float(min_trade_weight_bps),
        )
        (
            repair_target_signed_weights,
            repair_target_lattice_signed_qty,
            repair_projection,
        ) = project_executable_targets(
            raw_target_signed_weights=raw_target_signed_weights,
            current_signed_qty=repair_signed_qty,
            current_signed_notional=repair_signed_notional,
            reference_prices=repair_prices,
            assets_by_symbol=assets_by_symbol,
            account_equity=float(repair_equity),
            buying_power=float(repair_buying_power),
            buying_power_buffer=float(buying_power_buffer),
            min_trade_notional=float(repair_min_trade_notional),
            qty_decimals=int(qty_decimals),
            whole_shares_only=bool(whole_shares_only),
            short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
            shorting_enabled=bool(shorting_enabled),
            sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
            short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
            total_buying_power_capacity=float(repair_total_regt_capacity),
            gross_capacity_target_ratio=float(gross_capacity_target_ratio),
        )
        rebuilt_repair_instructions, repair_skipped = _build_order_instructions(
            target_signed_weights=repair_target_signed_weights,
            current_signed_notional=repair_signed_notional,
            current_signed_qty=repair_signed_qty,
            account_equity=float(repair_equity),
            reference_prices=repair_prices,
            assets_by_symbol=assets_by_symbol,
            min_trade_notional=float(repair_min_trade_notional),
            sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
            qty_decimals=int(qty_decimals),
            whole_shares_only=bool(whole_shares_only),
            opening_shorts_whole_shares_only=bool(opening_shorts_whole_shares_only),
            short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
            shorting_enabled=bool(shorting_enabled),
        )
        repair_release_residual, repair_entry_instructions = _split_release_entry_instructions(
            rebuilt_repair_instructions,
            current_signed_qty=repair_signed_qty,
        )
        candidate_symbol_set = set(repair_candidate_symbols)
        repair_entry_instructions = [
            item
            for item in repair_entry_instructions
            if str(item.symbol).upper() in candidate_symbol_set
        ]
        repair_weight_gap_bps = {
            str(item.symbol).upper(): float(
                abs(float(item.target_notional) - float(item.current_notional))
                / max(float(repair_equity), 1e-9)
                * 10_000.0
            )
            for item in repair_entry_instructions
        }
        repair_entry_instructions.sort(
            key=lambda item: (
                -float(repair_weight_gap_bps.get(str(item.symbol).upper(), 0.0)),
                str(item.symbol).upper(),
            )
        )
        repair_instructions_before_cap = list(repair_entry_instructions)
        repair_entry_instructions, repair_cap_diag = _scale_entry_instructions_to_buying_power(
            repair_entry_instructions,
            buying_power=float(repair_buying_power),
            buffer=float(buying_power_buffer),
            min_trade_notional=float(repair_min_trade_notional),
            qty_decimals=int(qty_decimals),
            whole_shares_only=bool(whole_shares_only),
            short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
            short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
        )
        repair_offset_bps = float(max(0.0, marketable_limit_max_offset_bps))
        repair_snapshot = {
            "schema_version": "1.0",
            "snapshot_type": "entry_repair",
            "captured_at_utc": _utc_now(),
            "stage": "entry_repair",
            "round": int(repair_round),
            "session_token": f"{session_token}_erp_r{repair_round:02d}",
            "candidate_symbols": repair_candidate_symbols,
            "candidate_weight_gap_bps": dict(sorted(repair_weight_gap_bps.items())),
            "priority_rule": "absolute_weight_gap_bps_descending",
            "position_reconciliation": repair_position_reconciliation,
            "positions_raw": _raw_dict_list(repair_positions),
            "signed_notional": dict(sorted(repair_signed_notional.items())),
            "signed_qty": dict(sorted(repair_signed_qty.items())),
            "account_raw": dict(repair_account)
            if isinstance(repair_account, dict)
            else repair_account,
            "account_equity": float(repair_equity),
            "account_equity_source": str(repair_equity_source),
            "buying_power": float(repair_buying_power),
            "buying_power_source": str(repair_buying_power_source),
            "gross_position": float(repair_gross_position),
            "regt_buying_power": float(repair_regt_buying_power),
            "total_regt_capacity": float(repair_total_regt_capacity),
            "total_regt_capacity_source": str(repair_total_regt_capacity_source),
            "reference_prices": dict(sorted(repair_prices.items())),
            "target_signed_weights": dict(sorted(repair_target_signed_weights.items())),
            "target_lattice_signed_qty": dict(sorted(repair_target_lattice_signed_qty.items())),
            "executable_target_projection": repair_projection,
            "rebuilt_all_instructions": _instruction_payloads(rebuilt_repair_instructions),
            "release_residual_instructions_not_retried": _instruction_payloads(
                repair_release_residual
            ),
            "candidate_instructions_before_buying_power_cap": _instruction_payloads(
                repair_instructions_before_cap
            ),
            "repair_instructions": _instruction_payloads(repair_entry_instructions),
            "repair_skipped_orders": repair_skipped,
            "buying_power_cap": repair_cap_diag,
            "limit_offset_bps": float(repair_offset_bps),
            "max_attempts_per_symbol": int(max(1, entry_repair_max_attempts)),
            "wait_seconds": float(max(0.1, entry_repair_wait_seconds)),
            "submitted_records": [],
        }
        snapshots.append(repair_snapshot)
        diagnostics["entry_projection"] = repair_projection
        diagnostics["entry_repair_rounds_completed"] = int(repair_round)

        if not repair_entry_instructions:
            repair_snapshot["submission_skipped_reason"] = (
                "no_candidate_entry_residual_after_reconciliation_projection_or_cap"
            )
            break

        repair_batch_started = time.monotonic()
        repair_records = _submit_and_track_orders(
            client=client,
            instructions=repair_entry_instructions,
            session_token=f"{session_token}_erp_r{repair_round:02d}",
            timeout_seconds=float(entry_timeout_seconds),
            poll_seconds=float(poll_seconds),
            execution_order_style=execution_order_style,
            marketable_limit_base_offset_bps=float(repair_offset_bps),
            marketable_limit_max_offset_bps=float(repair_offset_bps),
            marketable_limit_requote_steps_bps=[float(repair_offset_bps)],
            marketable_limit_requote_wait_seconds=float(
                max(0.1, entry_repair_wait_seconds)
            ),
            marketable_limit_max_attempts=int(max(1, entry_repair_max_attempts)),
            max_workers=int(execution_workers),
            execution_price_feed=str(execution_price_feed),
            execution_quote_client=execution_quote_client,
        )
        repair_batch_summary = _order_batch_summary(
            repair_records,
            requested_workers=int(execution_workers),
            elapsed_seconds=float(time.monotonic() - repair_batch_started),
        )
        for record in repair_records:
            record["stage"] = "entry_repair"
            record["entry_repair_round"] = int(repair_round)
            record["repair_priority_weight_gap_bps"] = repair_weight_gap_bps.get(
                str(record.get("symbol") or "").upper()
            )
        records.extend(repair_records)
        diagnostics["entry_repair_records"] = int(
            diagnostics["entry_repair_records"]
        ) + int(len(repair_records))
        repair_snapshot["submitted_records"] = repair_records
        repair_snapshot["submitted_record_count"] = int(len(repair_records))
        repair_snapshot["execution_batch_summary"] = repair_batch_summary
        latest_entry_instructions = list(repair_entry_instructions)
        latest_entry_records = list(repair_records)

    diagnostics["entry_repair_final_unfilled_symbols"] = sorted(
        {
            str(record.get("symbol") or "").upper()
            for record in latest_entry_records
            if str(record.get("symbol") or "").strip()
            and not _order_record_fully_filled(record)
        }
    )
    if latest_entry_records:
        final_entry_positions, final_entry_position_reconciliation = (
            _wait_for_order_position_reconciliation(
                client=client,
                instructions=latest_entry_instructions,
                execution_records=latest_entry_records,
                qty_decimals=int(qty_decimals),
                timeout_seconds=min(20.0, max(5.0, float(entry_timeout_seconds) / 4.0)),
                poll_seconds=float(poll_seconds),
            )
        )
        diagnostics["entry_final_position_reconciliation"] = (
            final_entry_position_reconciliation
        )
        snapshots.append(
            {
                "schema_version": "1.0",
                "snapshot_type": "entry_final_position_reconciliation",
                "captured_at_utc": _utc_now(),
                **final_entry_position_reconciliation,
                "positions_raw": _raw_dict_list(final_entry_positions),
            }
        )

    return records, diagnostics


def _alpaca_error_payload(exc: Exception) -> dict[str, Any]:
    text = str(exc)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _is_insufficient_qty_available_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient qty available" in text


def _fractional_long_close_retry_qty(
    *,
    instruction: OrderInstruction,
    rejected_qty: float,
    exc: Exception,
    qty_decimals: int = 4,
) -> float | None:
    """Back off one minimum unit when Alpaca rejects a fractional long close at zero."""
    text = str(exc).lower()
    error_payload = _alpaca_error_payload(exc)
    broker_available_qty = _safe_float(error_payload.get("available"))
    current_qty = _safe_float(instruction.current_signed_qty)
    target_qty = _safe_float(instruction.target_signed_qty)
    if (
        not (
            "fractional orders cannot be sold short" in text
            or "insufficient qty available" in text
        )
        or str(instruction.side).lower() != "sell"
        or current_qty is None
        or current_qty <= EPS
        or (target_qty is not None and target_qty < -EPS)
        or _is_effectively_whole_qty(rejected_qty, decimals=qty_decimals)
    ):
        return None
    scale = 10 ** max(0, int(qty_decimals))
    available_candidates = [float(rejected_qty), float(current_qty)]
    if broker_available_qty is not None and broker_available_qty > EPS:
        available_candidates.append(float(broker_available_qty))
    retry_qty = math.floor((min(available_candidates) * scale) - 1.0) / scale
    if retry_qty <= EPS or retry_qty >= float(rejected_qty) - EPS:
        return None
    return float(retry_qty)


def _is_insufficient_buying_power_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if _is_insufficient_qty_available_error(exc):
        return False
    return ("insufficient buying power" in text) or ("insufficient day trading buying power" in text) or ("40310000" in text)


def _alignment_to_target(
    *,
    target_signed_weights: Mapping[str, float],
    broker_weights: Mapping[str, float],
) -> dict[str, Any]:
    universe = sorted(set(target_signed_weights) | set(broker_weights))
    diffs = [abs(float(target_signed_weights.get(symbol, 0.0)) - float(broker_weights.get(symbol, 0.0))) for symbol in universe]
    return {
        "symbol_count": int(len(universe)),
        "abs_weight_diff_sum": float(sum(diffs)),
        "max_abs_weight_diff": float(max(diffs)) if diffs else 0.0,
    }


def _constraint_reason_list(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return sorted({str(item).strip() for item in value if str(item).strip()})
    return sorted({item.strip() for item in str(value or "").split(";") if item.strip()})


def _build_target_capability_snapshot(
    *,
    raw_target_signed_weights: Mapping[str, float],
    projection: Mapping[str, Any],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    account_shorting_enabled: bool,
    run_role: str,
    input_target_path: str | None,
) -> dict[str, Any]:
    projection_rows = {
        str(item.get("symbol") or "").upper(): dict(item)
        for item in (projection.get("symbols") or [])
        if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    for symbol in sorted(raw_target_signed_weights):
        raw_weight = float(raw_target_signed_weights.get(symbol) or 0.0)
        target_side = "long" if raw_weight > EPS else "short" if raw_weight < -EPS else "flat"
        asset = dict(assets_by_symbol.get(symbol) or {})
        projected = projection_rows.get(symbol, {})
        reasons = _constraint_reason_list(projected.get("constraint_reasons"))
        issues: list[str] = []
        if not asset:
            issues.append("asset_metadata_missing")
        if asset and not bool(asset.get("tradable", False)):
            issues.append("asset_not_tradable")
        if target_side == "short" and not bool(account_shorting_enabled):
            issues.append("account_shorting_disabled")
        if target_side == "short" and asset and not bool(asset.get("shortable", False)):
            issues.append("asset_not_shortable")
        if target_side == "short" and str(asset.get("borrow_status") or "").lower() == "hard_to_borrow":
            issues.append("hard_to_borrow")
        executable_weight = float(projected.get("executable_expected_signed_weight") or 0.0)
        projected_to_zero = bool(abs(raw_weight) > EPS and abs(executable_weight) <= EPS)
        if projected_to_zero:
            issues.append("projected_to_zero")
        blocking_issues = {
            "asset_not_tradable",
            "account_shorting_disabled",
            "asset_not_shortable",
            "projected_to_zero",
        }
        capability_status = (
            "blocked"
            if blocking_issues.intersection(issues)
            else "attention"
            if issues
            else "pass"
        )
        rows.append(
            {
                "symbol": symbol,
                "target_side": target_side,
                "raw_target_signed_weight": raw_weight,
                "capacity_adjusted_target_signed_weight": _safe_float(
                    projected.get("capacity_adjusted_target_signed_weight")
                ),
                "executable_expected_signed_weight": executable_weight,
                "target_lattice_signed_qty": _safe_float(projected.get("target_lattice_signed_qty")),
                "reference_price": _safe_float(projected.get("reference_price")),
                "asset_metadata_present": bool(asset),
                "tradable": bool(asset.get("tradable", False)) if asset else None,
                "shortable": bool(asset.get("shortable", False)) if asset else None,
                "easy_to_borrow": bool(asset.get("easy_to_borrow", False)) if asset else None,
                "borrow_status": str(asset.get("borrow_status") or ""),
                "fractionable": bool(asset.get("fractionable", False)) if asset else None,
                "marginable": bool(asset.get("marginable", False)) if asset else None,
                "maintenance_margin_requirement": _safe_float(
                    asset.get("maintenance_margin_requirement")
                ),
                "constraint_reasons": reasons,
                "capability_issues": sorted(set(issues)),
                "capability_status": capability_status,
                "projected_to_zero": projected_to_zero,
            }
        )
    blocked = [row for row in rows if row["capability_status"] == "blocked"]
    nonshortable = [
        row["symbol"]
        for row in rows
        if row["target_side"] == "short" and row.get("shortable") is False
    ]
    projected_zero = [row["symbol"] for row in rows if row["projected_to_zero"]]
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "run_role": str(run_role),
        "input_target_path": input_target_path,
        "account_shorting_enabled": bool(account_shorting_enabled),
        "target_symbol_count": len(rows),
        "target_long_count": sum(row["target_side"] == "long" for row in rows),
        "target_short_count": sum(row["target_side"] == "short" for row in rows),
        "blocked_target_count": len(blocked),
        "blocked_target_symbols": [row["symbol"] for row in blocked],
        "nonshortable_short_target_count": len(nonshortable),
        "nonshortable_short_target_symbols": nonshortable,
        "projected_to_zero_count": len(projected_zero),
        "projected_to_zero_symbols": projected_zero,
        "rows": rows,
    }


def _build_target_capability_drift(
    *,
    current_snapshot: Mapping[str, Any],
    prior_snapshot: Mapping[str, Any] | None,
    prior_snapshot_path: Path | None,
) -> dict[str, Any]:
    if not prior_snapshot or not isinstance(prior_snapshot.get("rows"), list):
        return {
            "schema_version": "1.0",
            "generated_at_utc": _utc_now(),
            "status": "not_applicable" if prior_snapshot_path is None else "prior_snapshot_missing",
            "prior_snapshot_path": prior_snapshot_path.as_posix() if prior_snapshot_path else None,
            "current_snapshot_role": current_snapshot.get("run_role"),
            "changed_symbol_count": 0,
            "execution_blocking_change_count": 0,
            "became_nonshortable_symbols": [],
            "rows": [],
        }
    prior_by_symbol = {
        str(item.get("symbol") or "").upper(): dict(item)
        for item in prior_snapshot.get("rows", [])
        if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
    }
    current_by_symbol = {
        str(item.get("symbol") or "").upper(): dict(item)
        for item in current_snapshot.get("rows", [])
        if isinstance(item, Mapping) and str(item.get("symbol") or "").strip()
    }
    capability_fields = [
        "asset_metadata_present",
        "tradable",
        "shortable",
        "easy_to_borrow",
        "borrow_status",
        "fractionable",
        "marginable",
        "maintenance_margin_requirement",
    ]
    rows: list[dict[str, Any]] = []
    for symbol in sorted(set(prior_by_symbol) | set(current_by_symbol)):
        prior = prior_by_symbol.get(symbol, {})
        current = current_by_symbol.get(symbol, {})
        changed_fields = [field for field in capability_fields if prior.get(field) != current.get(field)]
        prior_weight = _safe_float(prior.get("executable_expected_signed_weight")) or 0.0
        current_weight = _safe_float(current.get("executable_expected_signed_weight")) or 0.0
        target_side = str(current.get("target_side") or prior.get("target_side") or "")
        became_nonshortable = bool(
            target_side == "short"
            and prior.get("shortable") is True
            and current.get("shortable") is False
        )
        projected_to_zero_now = bool(abs(prior_weight) > EPS and abs(current_weight) <= EPS)
        capability_changed = bool(changed_fields)
        if not capability_changed and not projected_to_zero_now:
            continue
        rows.append(
            {
                "symbol": symbol,
                "target_side": target_side,
                "changed_fields": changed_fields,
                "prior_tradable": prior.get("tradable"),
                "current_tradable": current.get("tradable"),
                "prior_shortable": prior.get("shortable"),
                "current_shortable": current.get("shortable"),
                "prior_easy_to_borrow": prior.get("easy_to_borrow"),
                "current_easy_to_borrow": current.get("easy_to_borrow"),
                "prior_borrow_status": prior.get("borrow_status"),
                "current_borrow_status": current.get("borrow_status"),
                "prior_executable_expected_signed_weight": prior_weight,
                "current_executable_expected_signed_weight": current_weight,
                "executable_weight_delta": current_weight - prior_weight,
                "current_constraint_reasons": current.get("constraint_reasons") or [],
                "current_capability_issues": current.get("capability_issues") or [],
                "became_nonshortable": became_nonshortable,
                "projected_to_zero_now": projected_to_zero_now,
                "execution_blocking_change": bool(became_nonshortable or projected_to_zero_now),
            }
        )
    blocking = [row for row in rows if row["execution_blocking_change"]]
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "status": "attention" if blocking else "pass",
        "prior_snapshot_path": prior_snapshot_path.as_posix() if prior_snapshot_path else None,
        "prior_generated_at_utc": prior_snapshot.get("generated_at_utc"),
        "current_generated_at_utc": current_snapshot.get("generated_at_utc"),
        "changed_symbol_count": len(rows),
        "execution_blocking_change_count": len(blocking),
        "execution_blocking_change_symbols": [row["symbol"] for row in blocking],
        "became_nonshortable_symbols": [row["symbol"] for row in rows if row["became_nonshortable"]],
        "projected_to_zero_now_symbols": [row["symbol"] for row in rows if row["projected_to_zero_now"]],
        "rows": rows,
    }


def _mark_event(events: list[dict[str, Any]], name: str, payload: Mapping[str, Any] | None = None) -> None:
    now_monotonic = float(time.monotonic())
    run_started_monotonic = getattr(events, "run_started_monotonic", None)
    event = {
        "seq": int(len(events) + 1),
        "name": str(name),
        "at_utc": _utc_now(),
        "monotonic_seconds": now_monotonic,
        "run_elapsed_seconds": (
            max(0.0, now_monotonic - float(run_started_monotonic))
            if run_started_monotonic is not None
            else None
        ),
    }
    if payload:
        event["payload"] = dict(payload)
    events.append(event)
    persist = getattr(events, "persist", None)
    if callable(persist):
        persist()


def _stable_json_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default).encode(
        "utf-8",
        errors="replace",
    )
    return hashlib.sha256(encoded).hexdigest()


def _position_snapshot_meta(positions: Any) -> dict[str, Any]:
    rows = [dict(item) for item in positions if isinstance(item, Mapping)] if isinstance(positions, Sequence) else []
    symbols = sorted({str(item.get("symbol") or "").upper().strip() for item in rows if str(item.get("symbol") or "").strip()})
    signed_qty: dict[str, float] = {}
    signed_market_value: dict[str, float] = {}
    for item in rows:
        symbol = str(item.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        side = str(item.get("side") or "").lower()
        qty = _safe_float(item.get("qty")) or 0.0
        mv = _safe_float(item.get("market_value")) or 0.0
        signed_qty[symbol] = signed_qty.get(symbol, 0.0) + (-abs(qty) if side == "short" else abs(qty))
        signed_market_value[symbol] = signed_market_value.get(symbol, 0.0) + (-abs(mv) if side == "short" else abs(mv))
    return {
        "position_count": len(rows),
        "symbol_count": len(symbols),
        "symbols": symbols,
        "gross_market_value_abs": sum(abs(value) for value in signed_market_value.values()),
        "net_market_value": sum(signed_market_value.values()),
        "signed_qty_by_symbol": dict(sorted(signed_qty.items())),
        "signed_market_value_by_symbol": dict(sorted(signed_market_value.items())),
        "payload_sha256": _stable_json_digest(rows),
    }


def _account_snapshot_meta(account: Any) -> dict[str, Any]:
    payload = dict(account) if isinstance(account, Mapping) else {}
    keys = ["portfolio_value", "equity", "cash", "buying_power", "long_market_value", "short_market_value"]
    return {
        "present": bool(payload),
        "payload_sha256": _stable_json_digest(payload),
        **{key: payload.get(key) for key in keys},
    }


def _collect_position_account_stability(
    *,
    client: AlpacaHttpClient,
    initial_positions: Sequence[Mapping[str, Any]],
    initial_account: Mapping[str, Any],
    sample_count: int = 3,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for sample_index in range(1, max(1, int(sample_count)) + 1):
        if sample_index == 1:
            positions_result = {"ok": True, "payload": list(initial_positions)}
            account_result = {"ok": True, "payload": dict(initial_account)}
        else:
            time.sleep(max(0.0, float(sleep_seconds)))
            positions_result = _safe_broker_call(
                f"list_positions_after_stability_{sample_index}",
                client.list_positions,
            )
            account_result = _safe_broker_call(
                f"get_account_after_stability_{sample_index}",
                client.get_account,
            )
        positions_payload = positions_result.get("payload") if isinstance(positions_result, dict) else None
        account_payload = account_result.get("payload") if isinstance(account_result, dict) else None
        samples.append(
            {
                "sample_index": int(sample_index),
                "collected_at_utc": _utc_now(),
                "positions_ok": bool(positions_result.get("ok")) if isinstance(positions_result, dict) else False,
                "positions_error": positions_result.get("error") if isinstance(positions_result, dict) else None,
                "positions_meta": _position_snapshot_meta(positions_payload),
                "positions_payload": positions_payload if isinstance(positions_payload, list) else [],
                "account_ok": bool(account_result.get("ok")) if isinstance(account_result, dict) else False,
                "account_error": account_result.get("error") if isinstance(account_result, dict) else None,
                "account_meta": _account_snapshot_meta(account_payload),
                "account_payload": account_payload if isinstance(account_payload, Mapping) else {},
            }
        )
    position_hashes = [
        str((sample.get("positions_meta") or {}).get("payload_sha256") or "")
        for sample in samples
        if sample.get("positions_ok")
    ]
    position_quantity_hashes = [
        _stable_json_digest((sample.get("positions_meta") or {}).get("signed_qty_by_symbol") or {})
        for sample in samples
        if sample.get("positions_ok")
    ]
    account_hashes = [
        str((sample.get("account_meta") or {}).get("payload_sha256") or "")
        for sample in samples
        if sample.get("account_ok")
    ]
    position_counts = [
        int((sample.get("positions_meta") or {}).get("symbol_count") or 0)
        for sample in samples
        if sample.get("positions_ok")
    ]
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "sample_count": int(len(samples)),
        "position_hash_count": int(len(set(position_hashes))),
        "position_quantity_hash_count": int(len(set(position_quantity_hashes))),
        "account_hash_count": int(len(set(account_hashes))),
        "position_symbol_counts": position_counts,
        "position_symbol_count_stable": len(set(position_counts)) <= 1 if position_counts else False,
        "position_quantity_stable": (
            len(position_quantity_hashes) == len(samples)
            and len(set(position_quantity_hashes)) <= 1
        ),
        "position_payload_stable": len(set(position_hashes)) <= 1 if position_hashes else False,
        "account_payload_stable": len(set(account_hashes)) <= 1 if account_hashes else False,
        "samples": samples,
        "note": "Multiple after-run broker snapshots help distinguish real position changes from transient broker/API snapshot drift.",
    }


def _latest_stability_payload(stability: Mapping[str, Any], *, payload_key: str, fallback: Any) -> Any:
    samples = stability.get("samples") if isinstance(stability, Mapping) else None
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        return fallback
    for sample in reversed(samples):
        if not isinstance(sample, Mapping):
            continue
        ok_key = "positions_ok" if payload_key == "positions_payload" else "account_ok"
        payload = sample.get(payload_key)
        if sample.get(ok_key) and payload not in (None, ""):
            return payload
    return fallback


def _latest_stability_collected_at(stability: Mapping[str, Any], *, payload_key: str) -> str | None:
    samples = stability.get("samples") if isinstance(stability, Mapping) else None
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        return None
    for sample in reversed(samples):
        if not isinstance(sample, Mapping):
            continue
        ok_key = "positions_ok" if payload_key == "positions_payload" else "account_ok"
        if sample.get(ok_key) and sample.get(payload_key) not in (None, ""):
            captured = str(sample.get("collected_at_utc") or "").strip()
            return captured or None
    return None


def _load_position_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Position snapshot must be a JSON array: {path}")
    return [dict(item) for item in payload if isinstance(item, Mapping)]


def _load_cached_alpha_panel(path: Path, decision_date: date) -> pd.DataFrame:
    source_path = Path(path).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Cached alpha panel not found: {source_path}")
    alpha_panel = pd.read_csv(source_path)
    if alpha_panel.empty:
        raise ValueError(f"Cached alpha panel is empty: {source_path}")
    if "symbol" not in alpha_panel.columns or "session_date" not in alpha_panel.columns:
        raise ValueError(
            "Cached alpha panel must contain symbol and session_date columns: "
            f"{source_path}"
        )
    panel_session_dates = sorted(
        {
            str(value).strip()[:10]
            for value in alpha_panel["session_date"].dropna().tolist()
            if str(value).strip()
        }
    )
    if panel_session_dates != [decision_date.isoformat()]:
        raise ValueError(
            "Cached alpha panel session_date does not match requested decision date: "
            f"requested={decision_date.isoformat()} panel={panel_session_dates}"
        )
    symbol_values = alpha_panel["symbol"]
    normalized_symbols = symbol_values.astype(str).str.strip().str.upper()
    if (
        symbol_values.isna().any()
        or normalized_symbols.eq("").any()
        or normalized_symbols.duplicated().any()
    ):
        raise ValueError("Cached alpha panel contains blank or duplicate symbols.")
    normalized = alpha_panel.copy()
    normalized["symbol"] = normalized_symbols
    return normalized


def _build_position_continuity_guard(
    *,
    reference_path: Path | None,
    current_positions: Sequence[Mapping[str, Any]],
    current_stability: Mapping[str, Any],
    mode: str,
    qty_decimals: int,
) -> dict[str, Any]:
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in {"off", "audit", "strict"}:
        raise ValueError(f"Unsupported position continuity mode: {mode}")

    tolerance = max(1e-8, 0.5 * (10.0 ** (-max(0, int(qty_decimals)))))
    current_qty = _signed_qty_from_positions(current_positions)
    sample_qty_maps: list[dict[str, float]] = []
    samples = current_stability.get("samples") if isinstance(current_stability, Mapping) else None
    if isinstance(samples, Sequence) and not isinstance(samples, (str, bytes)):
        for sample in samples:
            if not isinstance(sample, Mapping) or not sample.get("positions_ok"):
                continue
            payload = sample.get("positions_payload")
            if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
                sample_qty_maps.append(
                    {
                        symbol: round(float(qty), max(0, int(qty_decimals)))
                        for symbol, qty in _signed_qty_from_positions(payload).items()
                    }
                )
    quantity_hashes = [_stable_json_digest(dict(sorted(item.items()))) for item in sample_qty_maps]
    expected_sample_count = int(current_stability.get("sample_count") or 0)
    current_quantity_stable = bool(
        sample_qty_maps
        and len(sample_qty_maps) == expected_sample_count
        and len(set(quantity_hashes)) == 1
    )

    blocking_reasons: list[str] = []
    reference_rows: list[dict[str, Any]] = []
    reference_error: str | None = None
    if reference_path is not None:
        try:
            reference_rows = _load_position_rows(reference_path)
        except Exception as exc:
            reference_error = f"{type(exc).__name__}: {exc}"
    elif normalized_mode == "strict":
        reference_error = "strict mode requires --position-continuity-reference-path"

    reference_qty = _signed_qty_from_positions(reference_rows)
    drift_rows: list[dict[str, Any]] = []
    if reference_path is not None and reference_error is None:
        for symbol in sorted(set(reference_qty) | set(current_qty)):
            before_qty = float(reference_qty.get(symbol, 0.0))
            now_qty = float(current_qty.get(symbol, 0.0))
            delta_qty = now_qty - before_qty
            if abs(delta_qty) <= tolerance:
                continue
            if abs(before_qty) <= tolerance:
                change_type = "appeared"
            elif abs(now_qty) <= tolerance:
                change_type = "disappeared"
            else:
                change_type = "quantity_changed"
            drift_rows.append(
                {
                    "symbol": symbol,
                    "change_type": change_type,
                    "reference_signed_qty": before_qty,
                    "current_signed_qty": now_qty,
                    "delta_signed_qty": delta_qty,
                    "absolute_delta_qty": abs(delta_qty),
                }
            )

    if normalized_mode == "strict":
        if reference_error is not None:
            blocking_reasons.append("reference_unavailable")
        if not current_quantity_stable:
            blocking_reasons.append("current_position_quantities_unstable")
        if drift_rows:
            blocking_reasons.append("cross_snapshot_position_quantity_drift")

    if normalized_mode == "off":
        status = "disabled"
    elif blocking_reasons:
        status = "blocked"
    elif reference_path is None or reference_error is not None:
        status = "not_applicable"
    elif drift_rows:
        status = "attention"
    else:
        status = "pass"

    reference_captured_at_utc = None
    if reference_path is not None:
        stability_path = reference_path.parent / "broker_position_account_stability_after.json"
        if stability_path.exists():
            try:
                reference_stability = json.loads(stability_path.read_text(encoding="utf-8"))
                reference_captured_at_utc = _latest_stability_collected_at(
                    reference_stability,
                    payload_key="positions_payload",
                )
            except Exception:
                reference_captured_at_utc = None

    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "mode": normalized_mode,
        "status": status,
        "blocking_reasons": blocking_reasons,
        "qty_tolerance": tolerance,
        "reference_path": reference_path.as_posix() if reference_path is not None else None,
        "reference_exists": bool(reference_path is not None and reference_path.exists()),
        "reference_error": reference_error,
        "reference_sha256": (
            _sha256_file(reference_path)
            if reference_path is not None and reference_path.is_file()
            else None
        ),
        "reference_captured_at_utc": reference_captured_at_utc,
        "reference_position_count": len(reference_rows),
        "current_position_count": len(current_positions),
        "current_stability_sample_count": expected_sample_count,
        "current_successful_quantity_sample_count": len(sample_qty_maps),
        "current_quantity_hash_count": len(set(quantity_hashes)),
        "current_quantity_stable": current_quantity_stable,
        "drift_symbol_count": len(drift_rows),
        "drift_symbols": [row["symbol"] for row in drift_rows],
        "drift_rows": drift_rows,
        "semantics": (
            "Signed broker quantities are compared across task boundaries; market-value and "
            "price changes are intentionally ignored. Strict mode fails closed before target "
            "construction or order submission."
        ),
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
        encoding="utf-8",
    )


def _write_json_file_if_absent(path: Path, payload: Any) -> bool:
    """Atomically preserve first-run evidence across same-session restarts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=_json_default)
    except FileExistsError:
        return False
    return True


def _write_jsonl_file(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False, default=_json_default) + "\n")


def _write_run_events(output_root: Path, events: Sequence[Mapping[str, Any]]) -> Path:
    path = output_root / "run_events.jsonl"
    persist = getattr(events, "persist", None)
    event_path = getattr(events, "path", None)
    if callable(persist) and event_path is not None and Path(event_path).resolve() == path.resolve():
        persist()
    else:
        _write_jsonl_file(path, events)
    return path


def _redact_value(key: str, value: Any) -> Any:
    key_l = str(key).lower()
    if any(token in key_l for token in ("secret", "password", "token", "api_key", "key_id")):
        if value in (None, ""):
            return value
        text = str(value)
        return f"<redacted:{len(text)} chars>"
    return value


def _args_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(vars(args).items()):
        if isinstance(value, Path):
            value = value.as_posix()
        out[str(key)] = _redact_value(str(key), value)
    return out


def _git_snapshot(project_root: Path) -> dict[str, Any]:
    def run_git(command: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *command],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip()

    return {
        "commit": run_git(["rev-parse", "HEAD"]),
        "branch": run_git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "status_short": run_git(["status", "--short"]),
        "diff_name_status": run_git(["diff", "--name-status"]),
    }


def _build_run_context(
    *,
    args: argparse.Namespace,
    argv: Sequence[str] | None,
    decision_date: date,
    output_root: Path,
    should_submit: bool,
    run_started_at_utc: str,
    events: list[dict[str, Any]],
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_started_at_utc": run_started_at_utc,
        "context_written_at_utc": _utc_now(),
        "decision_date": decision_date.isoformat(),
        "output_root": output_root.as_posix(),
        "submit_enabled": bool(should_submit),
        "argv": list(sys.argv[1:] if argv is None else argv),
        "parsed_args": _args_snapshot(args),
        "process": {
            "pid": os.getpid(),
            "cwd": Path.cwd().as_posix(),
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "code": {
            "project_root": PROJECT_ROOT.as_posix(),
            "script_path": Path(__file__).resolve().as_posix(),
            "git": _git_snapshot(PROJECT_ROOT),
        },
        "environment": {
            "timezone": os.environ.get("TZ"),
            "alpaca_trading_base_url_env_set": bool(os.environ.get("ALPACA_TRADING_BASE_URL")),
            "alpaca_data_base_url_env_set": bool(os.environ.get("ALPACA_DATA_BASE_URL")),
        },
        "runtime_environment_snapshot_path": (output_root / "runtime_environment_snapshot.json").as_posix(),
        "run_events_path": (output_root / "run_events.jsonl").as_posix(),
        "decision_phase_timings_path": (output_root / "decision_phase_timings.json").as_posix(),
        "file_hash_manifest_path": (output_root / "file_hash_manifest.json").as_posix(),
        "artifact_completeness_snapshot_path": (output_root / "artifact_completeness_snapshot.json").as_posix(),
        "events": list(events),
        "failure": dict(failure) if failure else None,
    }


def _safe_broker_call(name: str, func: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "name": str(name), "collected_at_utc": _utc_now(), "payload": func()}
    except Exception as exc:
        return {
            "ok": False,
            "name": str(name),
            "collected_at_utc": _utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _artifact_entry(path: Path, root: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        rel = path.relative_to(root)
    except Exception:
        stat = None
        rel = path
    return {
        "path": path.as_posix(),
        "relative_path": rel.as_posix(),
        "bytes": int(stat.st_size) if stat else None,
        "modified_at_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
        if stat
        else None,
        "sha256": _sha256_file(path),
    }


def _write_run_manifest(output_root: Path) -> Path:
    manifest_path = output_root / "run_artifact_manifest.json"
    files = [
        path
        for path in sorted(output_root.rglob("*"))
        if path.is_file() and path.resolve() != manifest_path.resolve()
    ]
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "root": output_root.as_posix(),
        "file_count": len(files),
        "files": [_artifact_entry(path, output_root) for path in files],
    }
    _write_json_file(manifest_path, payload)
    return manifest_path


def _interesting_environment() -> dict[str, Any]:
    prefixes = (
        "ALPACA",
        "APCA",
        "PYTHON",
        "PIP",
        "CONDA",
        "VIRTUAL",
        "PATH",
        "TZ",
        "LC_",
        "LANG",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_",
        "COMPUTERNAME",
        "USERNAME",
        "USERDOMAIN",
    )
    out: dict[str, Any] = {}
    for key in sorted(os.environ):
        if not key.upper().startswith(prefixes):
            continue
        value = os.environ.get(key)
        if key.upper() == "PATH" and value:
            out[key] = {
                "entry_count": len(value.split(os.pathsep)),
                "entries": value.split(os.pathsep)[:80],
                "truncated": len(value.split(os.pathsep)) > 80,
            }
        else:
            out[key] = _redact_value(key, value)
    return out


def _runtime_environment_snapshot() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "host": {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "node": platform.node(),
        },
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid() if hasattr(os, "getppid") else None,
            "cwd": Path.cwd().as_posix(),
            "executable": sys.executable,
            "argv": list(sys.argv),
            "python_version": sys.version,
            "python_prefix": sys.prefix,
            "python_base_prefix": getattr(sys, "base_prefix", ""),
            "path": list(sys.path),
        },
        "locale": {
            "preferred_encoding": locale.getpreferredencoding(False),
            "filesystem_encoding": sys.getfilesystemencoding(),
            "default_locale": locale.getlocale(),
        },
        "time": {
            "time_zone_env": os.environ.get("TZ"),
            "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "utc_time": _utc_now(),
            "monotonic_seconds": float(time.monotonic()),
        },
        "environment": _interesting_environment(),
    }


def _write_runtime_environment_snapshot(output_root: Path) -> Path:
    path = output_root / "runtime_environment_snapshot.json"
    _write_json_file(path, _runtime_environment_snapshot())
    return path


def _write_file_hash_manifest(output_root: Path) -> Path:
    path = output_root / "file_hash_manifest.json"
    files = [item for item in sorted(output_root.rglob("*")) if item.is_file() and item.resolve() != path.resolve()]
    suffix_counts: Counter[str] = Counter(item.suffix.lower() or "__no_suffix__" for item in files)
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "root": output_root.as_posix(),
        "file_count": len(files),
        "total_bytes": sum(int(item.stat().st_size) for item in files if item.exists()),
        "suffix_counts": dict(sorted(suffix_counts.items())),
        "files": [_artifact_entry(item, output_root) for item in files],
    }
    _write_json_file(path, payload)
    return path


def _run_submission_context(output_root: Path) -> dict[str, Any]:
    summary = _read_json_artifact(output_root / "execution_summary.json", {})
    if not isinstance(summary, Mapping):
        summary = {}
    trigger_mode = str(summary.get("trigger_mode") or "")
    submitted = bool(summary.get("submitted")) if "submitted" in summary else trigger_mode not in {"", "plan_only"}
    plan_only = trigger_mode == "plan_only" or output_root.name.endswith("_decision")
    return {
        "execution_summary_exists": (output_root / "execution_summary.json").exists(),
        "trigger_mode": trigger_mode,
        "submitted": submitted,
        "plan_only": plan_only,
    }


def _expected_artifact_categories(output_root: Path) -> dict[str, list[str]]:
    context = _run_submission_context(output_root)
    submitted = bool(context.get("submitted"))
    categories = {
        "scheduler": [
            "scheduler_task_context.json",
            "scheduler_task_result.json",
        ],
        "runtime": [
            "run_context.json",
            "run_events.jsonl",
            "decision_phase_timings.json",
            "runtime_environment_snapshot.json",
            "python_environment.json",
            "input_file_manifest.json",
            "source_code_manifest.json",
            "source_git_snapshot.json",
            "source_git_diff.patch",
            "source_code_snapshot.zip",
            "source_code_snapshot_manifest.json",
        ],
        "broker_state": [
            "broker_account_before.json",
            "broker_account_for_sizing.json",
            "broker_account_after.json",
            "broker_positions_before_raw.json",
            "broker_positions_after_raw.json",
            "broker_position_account_stability_before.json",
            "broker_position_account_stability_after.json",
            "position_continuity_guard.json",
            "broker_account_configurations_before.json",
            "broker_account_configurations_after.json",
            "broker_clock_before.json",
            "broker_clock_after.json",
        ],
        "orders_and_activity": [
            "order_plan.json",
            "execution_records.json",
            "order_poll_timeline.json",
            "execution_attempt_outcome_summary.json",
            "broker_open_orders_before.json",
            "broker_orders_all_before.json",
            "broker_open_orders_after.json",
            "broker_orders_all_after.json",
            "broker_order_snapshots.json",
            "broker_fill_activities.json",
            "broker_account_activities.json",
        ],
        "market_context": [
            "execution_quote_provider_health.json",
            "execution_price_snapshot.json",
            "execution_latest_trades_snapshot.json",
            "execution_latest_quotes_snapshot.json",
            "execution_latest_quotes_snapshot_post_submission.json",
            "execution_latest_quotes_snapshot_after.json",
            "execution_intraday_bars_1min.json",
            "execution_intraday_bars_1min_after.json",
            "broker_calendar_window.json",
            "broker_corporate_actions.json",
            "broker_portfolio_history_before.json",
            "broker_portfolio_history_after.json",
            "broker_assets_active_us_equity.json",
            "broker_assets_relevant.json",
        ],
        "portfolio_intent": [
            "decision_targets.csv",
            "alpha_core_panel_" + output_root.name[:8] + ".csv",
            "symbol_universe_intersection.json",
            "symbol_universe_intersection.csv",
            "target_weights_snapshot.json",
            "executable_target_projection.json",
            "executable_target_projection.csv",
            "target_capability_snapshot.json",
            "target_capability_snapshot.csv",
            "target_capability_drift.json",
            "target_capability_drift.csv",
            "portfolio_weights_snapshot.json",
            "portfolio_weights_after_snapshot.json",
        ],
        "meta_manifests": [
            "run_evidence_digest.json",
            "run_artifact_manifest.json",
            "file_hash_manifest.json",
        ],
    }
    if submitted:
        categories["orders_and_activity"].extend(
            [
                "broker_open_orders_before_submit.json",
                "broker_orders_all_before_submit.json",
            ]
        )
    return categories


def _paired_decision_artifact_path(output_root: Path, artifact_name: str) -> Path | None:
    if not output_root.name.endswith("_execute") or not artifact_name.startswith("alpha_core_panel_"):
        return None
    run_context = _read_json_artifact(output_root / "run_context.json", {})
    parsed_args = run_context.get("parsed_args", {}) if isinstance(run_context, Mapping) else {}
    targets_input = (
        parsed_args.get("decision_targets_input_path")
        if isinstance(parsed_args, Mapping)
        else None
    )
    candidate_roots: list[Path] = []
    if targets_input:
        candidate_roots.append(Path(str(targets_input)).resolve().parent)
    candidate_roots.append(
        output_root.with_name(output_root.name[: -len("_execute")] + "_decision")
    )
    for root in candidate_roots:
        candidate = root / artifact_name
        if candidate.exists():
            return candidate
    return None


def _write_artifact_completeness_snapshot(output_root: Path) -> Path:
    path = output_root / "artifact_completeness_snapshot.json"
    context = _run_submission_context(output_root)
    categories = _expected_artifact_categories(output_root)
    category_status: dict[str, Any] = {}
    for category, names in categories.items():
        rows = []
        for name in names:
            item = output_root / name
            source_scope = "run"
            if not item.exists():
                paired_item = _paired_decision_artifact_path(output_root, name)
                if paired_item is not None:
                    item = paired_item
                    source_scope = "paired_decision"
            rows.append(
                {
                    "artifact": name,
                    "exists": item.exists(),
                    "path": item.as_posix(),
                    "source_scope": source_scope,
                    "bytes": item.stat().st_size if item.exists() else None,
                    "sha256": _sha256_file(item) if item.exists() else None,
                }
            )
        missing = [row["artifact"] for row in rows if not row.get("exists")]
        category_status[category] = {
            "status": "pass" if not missing else "partial",
            "expected_count": len(rows),
            "present_count": len(rows) - len(missing),
            "missing_count": len(missing),
            "missing": missing,
            "artifacts": rows,
        }
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "root": output_root.as_posix(),
        "run_context": context,
        "status": "pass"
        if all(item.get("status") == "pass" for item in category_status.values())
        else "partial",
        "categories": category_status,
    }
    _write_json_file(path, payload)
    return path


def _finalize_run_evidence(
    output_root: Path,
    events: Sequence[Mapping[str, Any]] | None = None,
    *,
    refresh_runtime_environment: bool = True,
) -> None:
    """Refresh self-referential evidence files after all primary artifacts exist."""
    if events is not None:
        _write_run_events(output_root, events)
    if refresh_runtime_environment:
        _write_runtime_environment_snapshot(output_root)
    _write_run_evidence_digest(output_root)
    _write_run_manifest(output_root)
    _write_file_hash_manifest(output_root)
    _write_artifact_completeness_snapshot(output_root)
    _write_run_evidence_digest(output_root)
    _write_run_manifest(output_root)
    _write_file_hash_manifest(output_root)


def _read_json_artifact(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _json_artifact_payload(raw: Any) -> Any:
    if isinstance(raw, Mapping) and "ok" in raw and "payload" in raw:
        return raw.get("payload")
    return raw


def _safe_len(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def _json_artifact_status(path: Path) -> dict[str, Any]:
    entry = _artifact_entry(path, path.parent)
    parsed = _read_json_artifact(path, None)
    payload = _json_artifact_payload(parsed)
    status = {
        "exists": bool(path.exists()),
        "path": path.as_posix(),
        "relative_path": entry.get("relative_path"),
        "bytes": entry.get("bytes"),
        "sha256": entry.get("sha256"),
        "json_type": type(parsed).__name__ if parsed is not None else "",
        "payload_type": type(payload).__name__ if payload is not None else "",
        "payload_count": _safe_len(payload),
    }
    if isinstance(parsed, Mapping) and "ok" in parsed:
        status["ok"] = bool(parsed.get("ok"))
        status["error_type"] = parsed.get("error_type")
        status["error"] = parsed.get("error")
    return status


def _read_jsonl_count(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "line_count": 0, "parse_error_count": 0}
    line_count = 0
    parse_error_count = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                line_count += 1
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    parse_error_count += 1
    except Exception:
        parse_error_count += 1
    return {
        "exists": True,
        "line_count": int(line_count),
        "parse_error_count": int(parse_error_count),
    }


def _position_meta_from_file(path: Path) -> dict[str, Any]:
    raw = _read_json_artifact(path, [])
    payload = _json_artifact_payload(raw)
    return {
        **_json_artifact_status(path),
        "position_meta": _position_snapshot_meta(payload if isinstance(payload, Sequence) else []),
    }


def _account_meta_from_file(path: Path) -> dict[str, Any]:
    raw = _read_json_artifact(path, {})
    payload = _json_artifact_payload(raw)
    return {
        **_json_artifact_status(path),
        "account_meta": _account_snapshot_meta(payload if isinstance(payload, Mapping) else {}),
    }


def _numeric_delta(before: Any, after: Any) -> float | None:
    before_num = _safe_float(before)
    after_num = _safe_float(after)
    if before_num is None or after_num is None:
        return None
    return float(after_num) - float(before_num)


def _broker_state_digest(output_root: Path) -> dict[str, Any]:
    account_before = _read_json_artifact(output_root / "broker_account_before.json", {})
    account_after = _read_json_artifact(output_root / "broker_account_after.json", {})
    if not isinstance(account_before, Mapping):
        account_before = {}
    if not isinstance(account_after, Mapping):
        account_after = {}

    positions_before = _position_meta_from_file(output_root / "broker_positions_before_raw.json")
    positions_after = _position_meta_from_file(output_root / "broker_positions_after_raw.json")
    before_meta = positions_before.get("position_meta", {}) if isinstance(positions_before.get("position_meta"), Mapping) else {}
    after_meta = positions_after.get("position_meta", {}) if isinstance(positions_after.get("position_meta"), Mapping) else {}
    before_symbols = set(before_meta.get("symbols") or [])
    after_symbols = set(after_meta.get("symbols") or [])
    account_delta_fields = [
        "portfolio_value",
        "equity",
        "cash",
        "buying_power",
        "long_market_value",
        "short_market_value",
        "initial_margin",
        "maintenance_margin",
    ]
    return {
        "account_before": _account_meta_from_file(output_root / "broker_account_before.json"),
        "account_for_sizing": _account_meta_from_file(output_root / "broker_account_for_sizing.json"),
        "account_after": _account_meta_from_file(output_root / "broker_account_after.json"),
        "account_field_deltas": {
            field: _numeric_delta(account_before.get(field), account_after.get(field))
            for field in account_delta_fields
        },
        "positions_before": positions_before,
        "positions_after": positions_after,
        "position_symbol_added": sorted(after_symbols - before_symbols),
        "position_symbol_removed": sorted(before_symbols - after_symbols),
        "position_symbol_union_count": int(len(before_symbols | after_symbols)),
        "position_gross_market_value_abs_delta": _numeric_delta(
            before_meta.get("gross_market_value_abs"),
            after_meta.get("gross_market_value_abs"),
        ),
        "position_net_market_value_delta": _numeric_delta(
            before_meta.get("net_market_value"),
            after_meta.get("net_market_value"),
        ),
        "stability_before": _json_artifact_status(output_root / "broker_position_account_stability_before.json"),
        "stability_after": _json_artifact_status(output_root / "broker_position_account_stability_after.json"),
        "account_config_before": _json_artifact_status(output_root / "broker_account_configurations_before.json"),
        "account_config_after": _json_artifact_status(output_root / "broker_account_configurations_after.json"),
        "calendar_window": _json_artifact_status(output_root / "broker_calendar_window.json"),
        "portfolio_history_before": _json_artifact_status(output_root / "broker_portfolio_history_before.json"),
        "portfolio_history_after": _json_artifact_status(output_root / "broker_portfolio_history_after.json"),
    }


def _execution_evidence_digest(output_root: Path) -> dict[str, Any]:
    records = _read_json_artifact(output_root / "execution_records.json", [])
    records_list = [dict(item) for item in records if isinstance(item, Mapping)] if isinstance(records, list) else []
    status_counts = Counter(str(item.get("status_latest") or item.get("status") or "__missing__") for item in records_list)
    filled_records = [
        item
        for item in records_list
        if (_safe_float(item.get("filled_qty")) or 0.0) > 0
    ]
    return {
        "execution_summary": _json_artifact_status(output_root / "execution_summary.json"),
        "order_plan": _json_artifact_status(output_root / "order_plan.json"),
        "execution_records": {
            **_json_artifact_status(output_root / "execution_records.json"),
            "record_count": int(len(records_list)),
            "filled_record_count": int(len(filled_records)),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "order_poll_timeline": _json_artifact_status(output_root / "order_poll_timeline.json"),
        "execution_attempt_outcome_summary": _json_artifact_status(
            output_root / "execution_attempt_outcome_summary.json"
        ),
        "broker_order_snapshots": _json_artifact_status(output_root / "broker_order_snapshots.json"),
        "broker_fill_activities": _json_artifact_status(output_root / "broker_fill_activities.json"),
        "broker_account_activities": _json_artifact_status(output_root / "broker_account_activities.json"),
        "broker_orders_all_before": _json_artifact_status(output_root / "broker_orders_all_before.json"),
        "broker_orders_all_before_submit": _json_artifact_status(output_root / "broker_orders_all_before_submit.json"),
        "broker_orders_all_after_cancel": _json_artifact_status(output_root / "broker_orders_all_after_cancel.json"),
        "broker_orders_all_after": _json_artifact_status(output_root / "broker_orders_all_after.json"),
        "alpaca_api_audit": {
            **_artifact_entry(output_root / "alpaca_api_audit.jsonl", output_root),
            **_read_jsonl_count(output_root / "alpaca_api_audit.jsonl"),
        },
    }


def _market_evidence_digest(output_root: Path) -> dict[str, Any]:
    return {
        "symbol_universe_intersection": _json_artifact_status(
            output_root / "symbol_universe_intersection.json"
        ),
        "execution_price_snapshot": _json_artifact_status(output_root / "execution_price_snapshot.json"),
        "target_weights_snapshot": _json_artifact_status(output_root / "target_weights_snapshot.json"),
        "target_capability_snapshot": _json_artifact_status(
            output_root / "target_capability_snapshot.json"
        ),
        "target_capability_drift": _json_artifact_status(
            output_root / "target_capability_drift.json"
        ),
        "executable_target_projection": _json_artifact_status(
            output_root / "executable_target_projection.json"
        ),
        "portfolio_weights_snapshot": _json_artifact_status(output_root / "portfolio_weights_snapshot.json"),
        "portfolio_weights_after_snapshot": _json_artifact_status(
            output_root / "portfolio_weights_after_snapshot.json"
        ),
        "latest_trades_before": _json_artifact_status(output_root / "execution_latest_trades_snapshot.json"),
        "latest_quotes_before": _json_artifact_status(output_root / "execution_latest_quotes_snapshot.json"),
        "latest_quotes_post_submission": _json_artifact_status(
            output_root / "execution_latest_quotes_snapshot_post_submission.json"
        ),
        "latest_quotes_after": _json_artifact_status(output_root / "execution_latest_quotes_snapshot_after.json"),
        "intraday_bars_before": _json_artifact_status(output_root / "execution_intraday_bars_1min.json"),
        "intraday_bars_after": _json_artifact_status(output_root / "execution_intraday_bars_1min_after.json"),
        "corporate_actions": _json_artifact_status(output_root / "broker_corporate_actions.json"),
        "assets_active_us_equity": _json_artifact_status(output_root / "broker_assets_active_us_equity.json"),
        "assets_relevant": _json_artifact_status(output_root / "broker_assets_relevant.json"),
    }


def _runtime_evidence_digest(output_root: Path) -> dict[str, Any]:
    return {
        "run_context": _json_artifact_status(output_root / "run_context.json"),
        "decision_phase_timings": _json_artifact_status(output_root / "decision_phase_timings.json"),
        "run_events": {
            **_artifact_entry(output_root / "run_events.jsonl", output_root),
            **_read_jsonl_count(output_root / "run_events.jsonl"),
        },
        "runtime_environment_snapshot": _json_artifact_status(output_root / "runtime_environment_snapshot.json"),
        "source_code_manifest": _json_artifact_status(output_root / "source_code_manifest.json"),
        "source_git_snapshot": _json_artifact_status(output_root / "source_git_snapshot.json"),
        "source_git_diff": _artifact_entry(output_root / "source_git_diff.patch", output_root),
        "source_code_snapshot": _artifact_entry(output_root / "source_code_snapshot.zip", output_root),
        "source_code_snapshot_manifest": _json_artifact_status(output_root / "source_code_snapshot_manifest.json"),
        "python_environment": _json_artifact_status(output_root / "python_environment.json"),
        "input_file_manifest": _json_artifact_status(output_root / "input_file_manifest.json"),
        "file_hash_manifest": _json_artifact_status(output_root / "file_hash_manifest.json"),
        "artifact_completeness_snapshot": _json_artifact_status(
            output_root / "artifact_completeness_snapshot.json"
        ),
        "scheduler_task_context": _json_artifact_status(output_root / "scheduler_task_context.json"),
        "scheduler_task_result": _json_artifact_status(output_root / "scheduler_task_result.json"),
    }


def _write_run_evidence_digest(output_root: Path) -> Path:
    digest_path = output_root / "run_evidence_digest.json"
    expected_files = [
        "scheduler_task_context.json",
        "scheduler_task_result.json",
        "execution_summary.json",
        "run_context.json",
        "run_events.jsonl",
        "decision_phase_timings.json",
        "runtime_environment_snapshot.json",
        "order_plan.json",
        "execution_records.json",
        "execution_attempt_outcome_summary.json",
        "broker_account_before.json",
        "broker_account_after.json",
        "broker_positions_before_raw.json",
        "broker_positions_after_raw.json",
        "broker_position_account_stability_before.json",
        "broker_position_account_stability_after.json",
        "position_continuity_guard.json",
        "broker_fill_activities.json",
        "broker_account_activities.json",
        "broker_order_snapshots.json",
        "order_poll_timeline.json",
        "alpaca_api_audit.jsonl",
        "execution_price_snapshot.json",
        "execution_quote_provider_health.json",
        "symbol_universe_intersection.json",
        "symbol_universe_intersection.csv",
        "executable_target_projection.json",
        "executable_target_projection.csv",
        "target_capability_snapshot.json",
        "target_capability_snapshot.csv",
        "target_capability_drift.json",
        "target_capability_drift.csv",
        "execution_intraday_bars_1min.json",
        "execution_intraday_bars_1min_after.json",
        "execution_latest_quotes_snapshot.json",
        "execution_latest_quotes_snapshot_post_submission.json",
        "execution_latest_quotes_snapshot_after.json",
        "broker_portfolio_history_before.json",
        "broker_portfolio_history_after.json",
        "broker_calendar_window.json",
        "broker_corporate_actions.json",
        "source_code_manifest.json",
        "source_git_snapshot.json",
        "source_git_diff.patch",
        "source_code_snapshot.zip",
        "python_environment.json",
        "file_hash_manifest.json",
        "artifact_completeness_snapshot.json",
    ]
    file_statuses = {
        name: {
            **_artifact_entry(output_root / name, output_root),
            "exists": bool((output_root / name).exists()),
        }
        for name in expected_files
    }
    missing = sorted(name for name, item in file_statuses.items() if not item.get("exists"))
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "output_root": output_root.as_posix(),
        "status": "pass" if not missing else "partial",
        "expected_file_count": int(len(expected_files)),
        "present_file_count": int(len(expected_files) - len(missing)),
        "missing_file_count": int(len(missing)),
        "missing_files": missing,
        "file_statuses": file_statuses,
        "broker_state": _broker_state_digest(output_root),
        "execution": _execution_evidence_digest(output_root),
        "market": _market_evidence_digest(output_root),
        "runtime": _runtime_evidence_digest(output_root),
        "note": (
            "Semantic digest of raw run evidence. It is intentionally redundant with raw JSON/CSV files "
            "and exists to make future replay, attribution, and evidence-gap review faster."
        ),
    }
    _write_json_file(digest_path, payload)
    return digest_path


def _safe_run_command(command: list[str], *, cwd: Path | None = None, timeout: float = 10.0) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "command": command,
        }


def _source_code_manifest(project_root: Path) -> dict[str, Any]:
    unique = _source_snapshot_files(project_root)
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "project_root": project_root.as_posix(),
        "file_count": len(unique),
        "files": [_artifact_entry(path, project_root) for path in unique],
    }


def _source_snapshot_files(project_root: Path) -> list[Path]:
    include_roots = [project_root / "src", project_root / "tools"]
    files: list[Path] = []
    for root in include_roots:
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if path.is_file())
            files.extend(path for path in root.rglob("*.ps1") if path.is_file())
            files.extend(path for path in root.rglob("*.bat") if path.is_file())
    for extra in ["Start.bat", "README.md", "TRAY_LAUNCHER_GUIDE.md"]:
        path = project_root / extra
        if path.exists() and path.is_file():
            files.append(path)
    return sorted({path.resolve() for path in files})


def _write_source_git_evidence(*, output_root: Path, project_root: Path) -> None:
    commands = {
        "rev_parse_head": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        "status_short": ["git", "status", "--short"],
        "diff_name_status": ["git", "diff", "--name-status", "--", "src", "tools", "Start.bat", "README.md", "TRAY_LAUNCHER_GUIDE.md"],
        "diff_stat": ["git", "diff", "--stat", "--", "src", "tools", "Start.bat", "README.md", "TRAY_LAUNCHER_GUIDE.md"],
        "ls_files_others": ["git", "ls-files", "--others", "--exclude-standard", "--", "src", "tools", "Start.bat", "README.md", "TRAY_LAUNCHER_GUIDE.md"],
    }
    results = {name: _safe_run_command(command, cwd=project_root, timeout=10) for name, command in commands.items()}
    diff_result = _safe_run_command(
        ["git", "diff", "--", "src", "tools", "Start.bat", "README.md", "TRAY_LAUNCHER_GUIDE.md"],
        cwd=project_root,
        timeout=20,
    )
    (output_root / "source_git_diff.patch").write_text(str(diff_result.get("stdout") or ""), encoding="utf-8")
    _write_json_file(
        output_root / "source_git_snapshot.json",
        {
            "schema_version": "1.0",
            "generated_at_utc": _utc_now(),
            "project_root": project_root.as_posix(),
            "commands": results,
            "diff_patch_path": (output_root / "source_git_diff.patch").as_posix(),
            "diff_patch_sha256": _sha256_file(output_root / "source_git_diff.patch"),
            "note": "Diff is restricted to source/tool/startup/doc paths and intentionally excludes local credential/config artifacts.",
        },
    )


def _write_source_code_snapshot(*, output_root: Path, project_root: Path) -> None:
    files = _source_snapshot_files(project_root)
    zip_path = output_root / "source_code_snapshot.zip"
    manifest_entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            try:
                rel = path.relative_to(project_root).as_posix()
                data = path.read_bytes()
            except Exception:
                continue
            info = zipfile.ZipInfo(rel)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
            manifest_entries.append(
                {
                    "path": path.as_posix(),
                    "relative_path": rel,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        manifest_payload = json.dumps(
            {
                "schema_version": "1.0",
                "generated_at_utc": _utc_now(),
                "project_root": project_root.as_posix(),
                "file_count": len(manifest_entries),
                "files": manifest_entries,
                "note": "Snapshot is limited to source/tool/startup/doc files and excludes local credentials/config artifacts.",
            },
            indent=2,
            ensure_ascii=False,
        )
        info = zipfile.ZipInfo("SOURCE_SNAPSHOT_MANIFEST.json")
        info.date_time = (1980, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, manifest_payload.encode("utf-8"))
    _write_json_file(
        output_root / "source_code_snapshot_manifest.json",
        {
            "schema_version": "1.0",
            "generated_at_utc": _utc_now(),
            "zip_path": zip_path.as_posix(),
            "zip_bytes": zip_path.stat().st_size if zip_path.exists() else None,
            "zip_sha256": _sha256_file(zip_path),
            "project_root": project_root.as_posix(),
            "file_count": len(manifest_entries),
            "files": manifest_entries,
        },
    )


def _python_environment_snapshot() -> dict[str, Any]:
    freeze = _safe_run_command([sys.executable, "-m", "pip", "freeze"], timeout=20)
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "platform": platform.platform(),
        "pip_freeze": freeze,
    }


def _input_file_manifest(args: argparse.Namespace, account_state_path: Path) -> dict[str, Any]:
    paths: dict[str, Path] = {
        "accounts_json_path": Path(str(args.accounts_json_path)).resolve(),
        "candidate_symbols_path": Path(str(args.candidate_symbols_path)).resolve(),
        "account_state_path": account_state_path.resolve(),
    }
    if str(getattr(args, "execution_quote_provider", "alpaca")) == "longbridge":
        paths["longbridge_config_path"] = Path(str(args.longbridge_config_path)).resolve()
    optional_keys = [
        "decision_targets_input_path",
        "order_plan_input_path",
        "alpha_panel_input_path",
        "position_continuity_reference_path",
        "sec_ticker_map_cache_path",
        "sec_companyfacts_cache_dir",
        "sec_submissions_cache_dir",
        "sec_cache_root",
    ]
    for key in optional_keys:
        raw = getattr(args, key, None)
        if raw:
            paths[key] = Path(str(raw)).resolve()
    source_input = (
        getattr(args, "decision_targets_input_path", None)
        or getattr(args, "order_plan_input_path", None)
        or getattr(args, "alpha_panel_input_path", None)
    )
    if source_input:
        paths["decision_symbol_universe_intersection"] = (
            Path(str(source_input)).resolve().parent / "symbol_universe_intersection.json"
        )
    entries: dict[str, Any] = {}
    for key, path in paths.items():
        if path.is_dir():
            dir_files = [item for item in sorted(path.rglob("*")) if item.is_file()]
            entries[key] = {
                "path": path.as_posix(),
                "exists": True,
                "is_dir": True,
                "file_count": len(dir_files),
                "files": [_artifact_entry(item, path) for item in dir_files[:200]],
                "truncated": len(dir_files) > 200,
            }
        else:
            entries[key] = _artifact_entry(path, path.parent if path.parent.exists() else PROJECT_ROOT)
            entries[key]["is_dir"] = False
    return {
        "schema_version": "1.0",
        "generated_at_utc": _utc_now(),
        "inputs": entries,
        "note": "Secret-bearing files are hashed for identity; contents are not copied here.",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
