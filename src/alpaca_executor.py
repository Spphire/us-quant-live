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
    _load_candidate_symbols,
    _resolve_alpaca_credentials,
)
from executable_target_projector import project_executable_targets  # noqa: E402
from lot_manager import DEFAULT_FACTOR_MIN_HOLDS, LotManager  # noqa: E402
from vendors import AlpacaHttpClient, AlpacaRequestError  # noqa: E402


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "alpaca_executor"
DEFAULT_LEDGER_PATH = PROJECT_ROOT / "artifacts" / "alpaca_executor" / "lot_ledger.json"
EPS = 1e-10
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute daily AlphaCore + DecisionEngine plan on Alpaca (paper by default): "
            "broker/lot sync -> alpha decision -> open-triggered order submit -> post-trade lot sync."
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

    parser.add_argument("--ledger-path", default=str(DEFAULT_LEDGER_PATH))
    parser.add_argument("--session-idx", type=int, default=None)
    parser.add_argument("--lot-sync-mode", choices=("check", "auto_fix"), default="auto_fix")
    parser.add_argument("--lot-sync-tolerance", type=float, default=0.01)

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
    parser.add_argument("--execution-price-feed", default="sip", help="Feed for execution price reference. MUST be 'sip' for accurate market-wide pricing.")
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
        default="0,10,20",
        help="Additional bps steps for re-quote attempts, comma-separated.",
    )
    parser.add_argument(
        "--marketable-limit-requote-wait-seconds",
        type=float,
        default=20.0,
        help="How long to wait each limit attempt before cancel/requote.",
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
        "--buying-power-buffer",
        type=float,
        default=0.90,
        help="Fraction of fresh buying power that staged_regt may consume for new/increasing legs.",
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
        ledger_write_enabled = bool(should_submit)
        run_started_at_utc = _utc_now()
        run_events: list[dict[str, Any]] = []
        run_context_path = output_root / "run_context.json"
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
        account_before = client.get_account()
        _write_json_file(output_root / "broker_account_before.json", account_before)
        _write_json_file(
            output_root / "broker_account_configurations_before.json",
            _safe_broker_call("get_account_configurations_before", client.get_account_configurations),
        )
        shorting_enabled = bool(account_before.get("shorting_enabled", True))

        positions_before = client.list_positions()
        _write_json_file(output_root / "broker_positions_before_raw.json", positions_before)
        position_account_stability_before = _collect_position_account_stability(
            client=client,
            initial_positions=positions_before,
            initial_account=account_before,
            sample_count=3,
            sleep_seconds=1.0,
        )
        _write_json_file(output_root / "broker_position_account_stability_before.json", position_account_stability_before)
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

        ledger_path = Path(args.ledger_path).resolve()
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(output_root / "input_file_manifest.json", _input_file_manifest(args, ledger_path))
        lot_manager = LotManager.from_json(ledger_path)
        resolved_session_idx = _resolve_session_idx(
            lot_manager,
            args.session_idx,
            session_date=decision_date.isoformat(),
        )

        lot_check = _check_lot_alignment(
            lot_manager=lot_manager,
            broker_weights=broker_weights_before,
            tolerance=float(args.lot_sync_tolerance),
        )
        lot_sync_applied = False
        if lot_check["abs_weight_diff_sum"] > float(args.lot_sync_tolerance):
            if str(args.lot_sync_mode) == "check":
                raise ValueError(
                    "Lot/Broker mismatch exceeded tolerance in check mode. "
                    f"abs_weight_diff_sum={lot_check['abs_weight_diff_sum']:.6f} "
                    f"tolerance={float(args.lot_sync_tolerance):.6f}"
                )
            _sync_lot_to_broker(
                lot_manager=lot_manager,
                broker_weights=broker_weights_before,
                session_idx=resolved_session_idx,
                session_date=decision_date.isoformat(),
            )
            lot_sync_applied = True

        lot_manager.meta.update(
            {
                "executor_last_sync_mode": str(args.lot_sync_mode),
                "executor_last_sync_applied": bool(lot_sync_applied),
                "executor_last_sync_at_utc": _utc_now(),
                "executor_last_broker_equity": float(equity_before),
                "executor_last_broker_equity_source": str(equity_before_source),
            }
        )
        # Always record session_idx / session_date in meta, even if ledger_write
        # is disabled — if this run computes targets with DecisionEngine, the resulting
        # lot structure must be captured so that the subsequent execute phase can rebuild
        # the lot-lock state. If we only write it when should_submit=True, then the
        # plan_only (decision) run computes per-factor lots but discards them, and the
        # execute run only sees broker_sync lots with min_hold=0, thus defeating the
        # entire lot-locking mechanism. Note that we do NOT write the ledger file yet;
        # that happens at the end after post-trade reconciliation.
        lot_manager.meta.update(
            {
                "last_session_idx": int(resolved_session_idx),
                "last_session_date": decision_date.isoformat(),
            }
        )
        pre_trade_lot_snapshot_path = output_root / (
            f"lot_snapshot_before_execution_{decision_date.strftime('%Y%m%d')}.json"
            if should_submit
            else f"lot_snapshot_before_decision_{decision_date.strftime('%Y%m%d')}.json"
        )
        lot_manager.to_json(
            pre_trade_lot_snapshot_path,
            extra_meta={
                "snapshot_type": "pre_execution" if should_submit else "pre_decision",
                "submit_enabled": bool(should_submit),
            },
        )
        if ledger_write_enabled:
            lot_manager.to_json(ledger_path)
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

        if args.order_plan_input_path and args.decision_targets_input_path:
            raise ValueError("Provide only one of --order-plan-input-path or --decision-targets-input-path.")

        if args.order_plan_input_path:
            loaded_plan = _load_json_dict(Path(str(args.order_plan_input_path)).resolve())
            plan_input_path = str(Path(str(args.order_plan_input_path)).resolve().as_posix())
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
        elif args.decision_targets_input_path:
            source_path = Path(str(args.decision_targets_input_path)).resolve()
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
        else:
            candidate_symbols = _load_candidate_symbols(Path(args.candidate_symbols_path))
            pool = DynamicSymbolPool(
                client=client,
                candidate_symbols=candidate_symbols,
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
            symbols = sorted(pool.fresh(decision_date.isoformat()))
            if not symbols:
                raise ValueError("DynamicSymbolPool returned empty symbol list.")

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

            decision_config = DecisionConfig(
                factor_weights=dict(DEFAULT_FACTOR_WEIGHTS),
                factor_min_holds=dict(DEFAULT_FACTOR_MIN_HOLDS),
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
                lot_manager=lot_manager,
                session_idx=int(resolved_session_idx),
                session_date=decision_date.isoformat(),
            )
            decision_status = str(decision_result.status)
            decision_skip_reason = (
                None if decision_result.skip_reason in (None, "", "null") else str(decision_result.skip_reason)
            )
            decision_diagnostics = dict(decision_result.diagnostics)

            # CRITICAL FIX for live lot history: DecisionEngine.decide() has now split
            # the target positions into per-factor lots with individual min_hold periods.
            # In the scheduler's plan_only (decision) phase, should_submit=False, so
            # ledger_write_enabled=False, which means the factor-lot structure would
            # normally be discarded at the end (line ~784). But if we discard it now,
            # the subsequent execute phase (22:00 CN) will only see broker_sync lots
            # with min_hold=0, defeating the entire lot-locking mechanism. The fix: when
            # DecisionEngine was invoked (i.e. we are NOT loading an existing plan or
            # decision_targets CSV), persist the lot ledger immediately after decide(),
            # even if should_submit=False. This ensures the 22:00 execute phase loads
            # the factor-lot structure and can properly respect locked lots. The end-of-
            # run ledger write (line ~784) will then be a post-trade reconciliation update.
            lot_manager.to_json(ledger_path)

            decision_targets_path = output_root / "decision_targets.csv"
            target_signed_weights = _signed_weights_from_lot(lot_manager)
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
        _mark_event(
            run_events,
            "decision_targets_resolved",
            {
                "decision_status": decision_status,
                "target_symbol_count": len(target_signed_weights),
                "decision_targets_path": decision_targets_path.as_posix() if decision_targets_path else None,
            },
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
        reference_prices = _resolve_reference_prices(
            client=client,
            symbols=sorted(set(target_signed_weights) | set(broker_signed_notional_before)),
            fallback_prices=fallback_prices,
            feed=str(args.execution_price_feed),
            prefer_live=True,
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
        latest_trades_snapshot = _safe_broker_call(
            "get_latest_trades_for_reference_symbols",
            lambda: client.get_latest_trades(symbols=audit_price_symbols, feed=str(args.execution_price_feed))
            if audit_price_symbols
            else {},
        )
        _write_json_file(output_root / "execution_latest_trades_snapshot.json", latest_trades_snapshot)
        latest_quotes_snapshot = _safe_broker_call(
            "get_latest_quotes_for_reference_symbols",
            lambda: client.get_latest_quotes(symbols=audit_price_symbols, feed=str(args.execution_price_feed))
            if audit_price_symbols
            else {},
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
                "feed": str(args.execution_price_feed),
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
        if float(args.min_trade_notional) < 0:
            raise ValueError("--min-trade-notional must be non-negative.")
        if float(args.min_trade_weight_bps) < 0:
            raise ValueError("--min-trade-weight-bps must be non-negative.")

        account_for_sizing = client.get_account()
        _write_json_file(output_root / "broker_account_for_sizing.json", account_for_sizing)
        shorting_enabled = bool(account_for_sizing.get("shorting_enabled", shorting_enabled))
        sizing_equity, sizing_equity_source = _resolve_account_equity(
            account=account_for_sizing,
            signed_notional=broker_signed_notional_before,
        )
        sizing_buying_power, sizing_buying_power_source = _buying_power(account_for_sizing)
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
        )
        executable_expected_signed_weights = dict(
            executable_projection_diag.get("executable_expected_signed_weights") or {}
        )
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
                "effective_min_trade_notional": float(effective_min_trade_notional),
                "min_trade_notional_absolute_floor": float(args.min_trade_notional),
                "min_trade_weight_bps": float(args.min_trade_weight_bps),
            },
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
                    "adverse_price_offset_bps": float(adverse_price_offset_bps),
                    "marketable_limit_base_offset_bps": float(marketable_limit_base_offset_bps),
                    "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
                    "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
                    "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
                    "min_trade_notional": float(effective_min_trade_notional),
                    "min_trade_notional_absolute_floor": float(args.min_trade_notional),
                    "min_trade_weight_bps": float(args.min_trade_weight_bps),
                    "qty_decimals": int(args.qty_decimals),
                    "marketable_limit_requote_steps_bps": marketable_limit_requote_steps_bps,
                    "marketable_limit_requote_wait_seconds": float(args.marketable_limit_requote_wait_seconds),
                    "decision_status": decision_status,
                    "decision_skip_reason": decision_skip_reason,
                    "decision_diagnostics": decision_diagnostics,
                    "lot_sync_before_decision": lot_check,
                    "sec_cache_source": sec_cache_source,
                    "dynamic_symbol_count": int(len(symbols)),
                    "raw_target_signed_weights": raw_target_signed_weights,
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
                    short_buying_power_adverse_offset_bps=float(short_buying_power_adverse_offset_bps),
                    release_timeout_seconds=release_timeout,
                    entry_timeout_seconds=entry_timeout,
                    poll_seconds=float(args.order_poll_seconds),
                    execution_order_style=str(args.execution_order_style),
                    marketable_limit_base_offset_bps=float(marketable_limit_base_offset_bps),
                    marketable_limit_max_offset_bps=float(marketable_limit_max_offset_bps),
                    marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                    marketable_limit_requote_wait_seconds=float(args.marketable_limit_requote_wait_seconds),
                    release_max_rounds=int(args.staged_release_max_rounds),
                    release_round_extra_bps=float(args.staged_release_round_extra_bps),
                    release_round_sleep_seconds=float(args.staged_release_round_sleep_seconds),
                    stage_snapshots=staged_rebuild_snapshots,
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
                )
            submit_error_records = [
                record for record in execution_records if str(record.get("status_latest") or "").lower() == "submit_error"
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
                    "submit_error_count": submit_error_count,
                    "submit_abort_reason": submit_abort_reason,
                },
            )
        elif should_submit:
            _mark_event(run_events, "order_submission_skipped_no_instructions", {})
        else:
            _mark_event(run_events, "order_submission_disabled", {"trigger_mode": str(args.trigger_mode)})

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
        _write_json_file(output_root / "broker_positions_after_raw.json", positions_after)
        _write_json_file(output_root / "broker_account_after.json", account_after)
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
            lambda: client.get_latest_quotes(symbols=intraday_bar_symbols_after, feed=str(args.execution_price_feed))
            if intraday_bar_symbols_after
            else {},
        )
        _write_json_file(output_root / "execution_latest_quotes_snapshot_after.json", latest_quotes_after_snapshot)
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
        post_trade_lot_manager = lot_manager.clone()
        _sync_lot_to_broker(
            lot_manager=post_trade_lot_manager,
            broker_weights=broker_weights_after,
            session_idx=int(resolved_session_idx),
            session_date=decision_date.isoformat(),
        )
        post_trade_lot_manager.meta.update(
            {
                "last_session_idx": int(resolved_session_idx),
                "last_session_date": decision_date.isoformat(),
                "executor_last_run_utc": _utc_now(),
                "executor_last_order_count": int(len(instructions)),
                "executor_last_submit_enabled": bool(should_submit),
                "executor_last_ledger_write_enabled": bool(ledger_write_enabled),
                "executor_last_post_trade_equity": float(equity_after),
                "executor_last_post_trade_equity_source": str(equity_after_source),
            }
        )
        day_lot_snapshot_path = output_root / f"lot_snapshot_{decision_date.strftime('%Y%m%d')}.json"
        if ledger_write_enabled:
            post_trade_lot_manager.to_json(ledger_path)
        post_trade_lot_manager.to_json(day_lot_snapshot_path, extra_meta={"snapshot_type": "post_execution_daily"})

        broker_frame_before.to_csv(output_root / "broker_positions_before.csv", index=False)
        broker_frame_after.to_csv(output_root / "broker_positions_after.csv", index=False)
        raw_fill_activities_path = output_root / "broker_fill_activities.json"
        raw_order_snapshots_path = output_root / "broker_order_snapshots.json"
        order_poll_timeline_path = output_root / "order_poll_timeline.json"
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

        alignment_after = _alignment_to_target(
            target_signed_weights=executable_expected_signed_weights,
            broker_weights=broker_weights_after,
        )
        staged_abort_reason = str(staged_diagnostics.get("entry_abort_reason") or "") if staged_diagnostics else ""
        run_ok = bool(submit_error_count == 0 and not staged_abort_reason)
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
            "account_equity": float(sizing_equity),
            "account_equity_source": str(sizing_equity_source),
            "account_equity_post_trade": float(equity_after),
            "account_equity_post_trade_source": str(equity_after_source),
            "trigger_mode": str(args.trigger_mode),
            "target_ny_time": str(args.target_ny_time),
            "execution_mode": str(args.execution_mode),
            "execution_order_style": str(args.execution_order_style),
            "adverse_price_offset_bps": float(adverse_price_offset_bps),
            "marketable_limit_base_offset_bps": float(marketable_limit_base_offset_bps),
            "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
            "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
            "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
            "min_trade_notional": float(effective_min_trade_notional),
            "min_trade_notional_absolute_floor": float(args.min_trade_notional),
            "min_trade_weight_bps": float(args.min_trade_weight_bps),
            "qty_decimals": int(args.qty_decimals),
            "decision_status": decision_status,
            "decision_skip_reason": decision_skip_reason,
            "dynamic_symbols": int(len(symbols)),
            "order_plan_count": int(len(instructions)),
            "submitted": bool(should_submit),
            "submitted_orders": int(len(execution_records)),
            "order_poll_event_count": int(order_poll_timeline.get("event_count") or 0),
            "staged_rebuild_snapshot_count": int(len(staged_rebuild_snapshots)),
            "submit_error_count": int(submit_error_count),
            "submit_abort_reason": submit_abort_reason,
            "ledger_write_enabled": bool(ledger_write_enabled),
            "staged_diagnostics": staged_diagnostics,
            "raw_target_signed_weights": raw_target_signed_weights,
            "order_target_signed_weights": target_signed_weights,
            "executable_expected_signed_weights": executable_expected_signed_weights,
            "initial_executable_target_projection": executable_projection_diag,
            "executable_target_projection": final_executable_projection_diag,
            "lot_ledger_path": ledger_path.as_posix(),
            "daily_lot_snapshot_path": day_lot_snapshot_path.as_posix(),
            "pre_trade_lot_snapshot_path": pre_trade_lot_snapshot_path.as_posix(),
            "alignment_after_execution": alignment_after,
            "outputs": {
                "run_context_json": run_context_path.as_posix(),
                "run_events_jsonl": (output_root / "run_events.jsonl").as_posix(),
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
                "decision_targets_csv": decision_targets_path.as_posix() if decision_targets_path else None,
                "order_plan_json": plan_path.as_posix(),
                "broker_account_before_json": (output_root / "broker_account_before.json").as_posix(),
                "broker_account_for_sizing_json": (output_root / "broker_account_for_sizing.json").as_posix(),
                "broker_account_after_json": (output_root / "broker_account_after.json").as_posix(),
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
                "pre_trade_lot_snapshot_json": pre_trade_lot_snapshot_path.as_posix(),
                "broker_assets_active_us_equity_json": (output_root / "broker_assets_active_us_equity.json").as_posix(),
                "broker_assets_relevant_json": (output_root / "broker_assets_relevant.json").as_posix(),
                "execution_latest_trades_snapshot_json": (output_root / "execution_latest_trades_snapshot.json").as_posix(),
                "execution_latest_quotes_snapshot_json": (output_root / "execution_latest_quotes_snapshot.json").as_posix(),
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
        failed_at_utc = _utc_now()
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
        raise FileNotFoundError(f"JSON file not found: {path.as_posix()}")
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


def _effective_min_trade_notional(
    *,
    account_equity: float,
    absolute_floor: float,
    weight_bps: float,
) -> float:
    weight_notional = max(0.0, float(account_equity)) * max(0.0, float(weight_bps)) / 10000.0
    return float(max(0.0, float(absolute_floor), weight_notional))


def _resolve_session_idx(
    lot_manager: LotManager,
    provided: int | None,
    session_date: str | None = None,
) -> int:
    if provided is not None:
        return int(provided)
    last_idx = lot_manager.meta.get("last_session_idx")
    if last_idx is not None:
        # session_idx must count trading days, not process invocations. The same
        # trading day is touched multiple times (12:00 decision, 22:00 execute, plus
        # any manual retry); all of them must share one index so that per-factor
        # min-hold (measured in sessions) is not silently consumed faster than the
        # market actually advances. Only roll the index forward when we observe a
        # genuinely new session date.
        last_date = lot_manager.meta.get("last_session_date")
        if session_date is not None and last_date is not None and str(last_date) == str(session_date):
            return int(last_idx)
        return int(last_idx) + 1
    return int(lot_manager.max_birth_idx()) + 1


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


def _signed_weights_from_lot(lot_manager: LotManager) -> dict[str, float]:
    previous = lot_manager.previous_weights()
    out: dict[str, float] = {}
    for symbol, value in previous.get("long", {}).items():
        out[str(symbol).upper()] = out.get(str(symbol).upper(), 0.0) + float(value)
    for symbol, value in previous.get("short", {}).items():
        out[str(symbol).upper()] = out.get(str(symbol).upper(), 0.0) - float(value)
    return {symbol: float(value) for symbol, value in out.items() if abs(value) > EPS}


def _check_lot_alignment(
    *,
    lot_manager: LotManager,
    broker_weights: Mapping[str, float],
    tolerance: float,
) -> dict[str, Any]:
    lot_signed = _signed_weights_from_lot(lot_manager)
    universe = sorted(set(lot_signed) | set(broker_weights))
    diffs = [abs(float(lot_signed.get(symbol, 0.0)) - float(broker_weights.get(symbol, 0.0))) for symbol in universe]
    abs_sum = float(sum(diffs))
    max_abs = float(max(diffs)) if diffs else 0.0
    return {
        "lot_symbol_count": int(len(lot_signed)),
        "broker_symbol_count": int(len(broker_weights)),
        "union_symbol_count": int(len(universe)),
        "abs_weight_diff_sum": abs_sum,
        "max_abs_weight_diff": max_abs,
        "within_tolerance": bool(abs_sum <= float(tolerance)),
    }


def _sync_lot_to_broker(
    *,
    lot_manager: LotManager,
    broker_weights: Mapping[str, float],
    session_idx: int,
    session_date: str | None = None,
) -> None:
    lot_manager.sync_to_broker_weights(
        broker_weights=broker_weights,
        session_idx=int(session_idx),
        session_date=str(session_date) if session_date is not None else None,
        sync_factor="broker_sync",
        sync_time_utc=_utc_now(),
    )


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
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    fallback_prices: Mapping[str, float],
    feed: str,
    prefer_live: bool = False,
) -> dict[str, float]:
    fallback: dict[str, float] = {
        str(symbol).upper(): float(price)
        for symbol, price in fallback_prices.items()
        if _safe_float(price) is not None and float(price) > 0
    }
    requested = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    out: dict[str, float] = {} if prefer_live else dict(fallback)
    needed = requested if prefer_live else [symbol for symbol in requested if symbol not in out]
    for chunk in _chunks(needed, 150):
        try:
            trades = client.get_latest_trades(symbols=chunk, feed=str(feed))
        except AlpacaRequestError:
            continue
        for symbol, trade in trades.items():
            px = _safe_float(trade.get("p"))
            if px is not None and px > 0:
                out[str(symbol).upper()] = float(px)
    for symbol, price in fallback.items():
        out.setdefault(str(symbol).upper(), float(price))
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
) -> tuple[list[OrderInstruction], list[OrderInstruction]]:
    release: list[OrderInstruction] = []
    entry: list[OrderInstruction] = []
    for item in instructions:
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
            period="1D",
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
) -> dict[str, Any]:
    requested_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()})
    start = f"{session_date.isoformat()}T00:00:00Z"
    end = f"{(session_date + timedelta(days=1)).isoformat()}T00:00:00Z"
    bars: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(_chunks(requested_symbols, chunk_size), start=1):
        try:
            bars.extend(
                client.get_stock_bars(
                    symbols=chunk,
                    start=start,
                    end=end,
                    timeframe="1Min",
                    adjustment="raw",
                    feed=str(feed),
                    limit=10000,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "chunk_index": int(chunk_index),
                    "symbols": list(chunk),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
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
        "chunk_count": int(math.ceil(len(requested_symbols) / max(1, int(chunk_size)))) if requested_symbols else 0,
        "bars": [dict(row) if isinstance(row, Mapping) else row for row in bars],
        "errors": errors,
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
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for idx, item in enumerate(instructions, start=1):
        base_record = {
            "symbol": item.symbol,
            "side": item.side,
            "qty": float(item.qty),
            "delta_notional": float(item.delta_notional),
            "reference_price": float(item.reference_price),
            "submitted_at_utc": _utc_now(),
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
                }
                records.append(record)
                continue

            remaining_qty = float(item.qty)
            total_filled_qty = 0.0
            attempts: list[dict[str, Any]] = []
            latest_status = ""
            latest_filled_avg_price: float | None = None
            latest_updated_at = ""
            global_deadline = time.monotonic() + max(1.0, float(timeout_seconds))
            requote_steps = [max(0.0, float(step)) for step in marketable_limit_requote_steps_bps] or [0.0]
            cycle_increment_bps = max(requote_steps) if requote_steps else 0.0
            max_offset_bps = max(0.0, float(marketable_limit_max_offset_bps))

            attempt_no = 0
            while remaining_qty > EPS and time.monotonic() < global_deadline:
                attempt_no += 1
                step_index = (attempt_no - 1) % len(requote_steps)
                cycle_no = (attempt_no - 1) // len(requote_steps)
                step_bps = requote_steps[step_index]
                if remaining_qty <= EPS:
                    break
                if time.monotonic() >= global_deadline:
                    break

                total_offset_bps = max(
                    0.0,
                    float(marketable_limit_base_offset_bps)
                    + float(step_bps)
                    + (float(cycle_increment_bps) * float(cycle_no)),
                )
                if max_offset_bps > 0:
                    total_offset_bps = min(float(total_offset_bps), float(max_offset_bps))
                limit_price = _marketable_limit_price(
                    side=item.side,
                    reference_price=item.reference_price,
                    offset_bps=total_offset_bps,
                )
                client_order_id = _client_order_id(
                    session_token,
                    idx=idx,
                    side=item.side,
                    symbol=item.symbol,
                    attempt_no=attempt_no,
                )
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
                    cancel_requested_at_utc = _utc_now()
                    try:
                        client.cancel_order(order_id)
                        _append_order_timeline_event(
                            attempt_poll_events,
                            event="cancel_requested",
                            order_id=order_id,
                            order=latest_order,
                            extra={"cancel_requested_at_utc": cancel_requested_at_utc},
                        )
                    except AlpacaRequestError as exc:
                        _append_order_timeline_event(
                            attempt_poll_events,
                            event="cancel_error",
                            order_id=order_id,
                            order=latest_order,
                            extra={
                                "cancel_requested_at_utc": cancel_requested_at_utc,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
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
                        "status_latest": latest_status,
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
                "attempts": attempts,
            }
            records.append(record)
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
                }
            )
            if abort_remaining:
                break

    return records


def _instruction_payloads(instructions: Sequence[OrderInstruction]) -> list[dict[str, Any]]:
    return [asdict(item) for item in instructions]


def _raw_dict_list(items: Sequence[Any]) -> list[Any]:
    return [dict(item) if isinstance(item, dict) else item for item in items]


def _instruction_symbols(instructions: Sequence[OrderInstruction]) -> list[str]:
    return sorted({str(item.symbol).upper() for item in instructions if str(item.symbol).strip()})


def _submit_staged_regt_orders(
    *,
    client: AlpacaHttpClient,
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
    short_buying_power_adverse_offset_bps: float,
    release_timeout_seconds: float,
    entry_timeout_seconds: float,
    poll_seconds: float,
    execution_order_style: str,
    marketable_limit_base_offset_bps: float,
    marketable_limit_max_offset_bps: float,
    marketable_limit_requote_steps_bps: Sequence[float],
    marketable_limit_requote_wait_seconds: float,
    release_max_rounds: int,
    release_round_extra_bps: float,
    release_round_sleep_seconds: float,
    stage_snapshots: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots = stage_snapshots if stage_snapshots is not None else []
    release_instructions, _ = _split_release_entry_instructions(initial_instructions)
    release_sell_long, release_buy_to_cover = _split_release_substages(release_instructions)
    records: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "mode": "staged_regt",
        "initial_order_count": int(len(initial_instructions)),
        "initial_release_count": int(len(release_instructions)),
        "release_sell_long_count": int(len(release_sell_long)),
        "release_buy_to_cover_count": int(len(release_buy_to_cover)),
        "release_max_rounds": int(max(1, release_max_rounds)),
        "release_round_extra_bps": float(max(0.0, release_round_extra_bps)),
        "release_records": 0,
        "release_substages": [],
        "release_fully_filled": True,
        "entry_aborted": False,
        "entry_abort_reason": None,
        "entry_records": 0,
        "entry_rebuild_skipped_orders": [],
        "entry_buying_power_cap": {},
        "entry_projection": {},
    }

    release_reference_prices = dict(fallback_prices)
    release_target_signed_weights = dict(target_signed_weights)
    for stage_name, stage_token, stage_instructions in (
        ("release_sell_long", "rsl", release_sell_long),
        ("release_buy_to_cover", "rbc", release_buy_to_cover),
    ):
        if not stage_instructions:
            continue
        stage_symbols = sorted({item.symbol for item in stage_instructions})
        stage_records_total: list[dict[str, Any]] = []
        stage_fully_filled = False
        stage_remaining_symbols: list[str] = list(stage_symbols)
        current_stage_instructions = list(stage_instructions)

        for round_no in range(1, max(1, int(release_max_rounds)) + 1):
            if not current_stage_instructions:
                stage_fully_filled = True
                break
            round_offset_bps = float(marketable_limit_base_offset_bps) + max(0.0, float(release_round_extra_bps)) * float(round_no - 1)
            round_input_instructions = list(current_stage_instructions)
            release_records = _submit_and_track_orders(
                client=client,
                instructions=round_input_instructions,
                session_token=f"{session_token}_{stage_token}_r{round_no:02d}",
                timeout_seconds=float(release_timeout_seconds),
                poll_seconds=poll_seconds,
                execution_order_style=execution_order_style,
                marketable_limit_base_offset_bps=round_offset_bps,
                marketable_limit_max_offset_bps=marketable_limit_max_offset_bps,
                marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                marketable_limit_requote_wait_seconds=marketable_limit_requote_wait_seconds,
            )
            for record in release_records:
                record["stage"] = stage_name
                record["release_round"] = int(round_no)
            records.extend(release_records)
            stage_records_total.extend(release_records)
            diagnostics["release_records"] = int(diagnostics["release_records"]) + int(len(release_records))

            refreshed_substage_positions = client.list_positions()
            _, refreshed_substage_signed_notional = _positions_to_frame_and_notional(refreshed_substage_positions)
            refreshed_substage_signed_qty = _signed_qty_from_positions(refreshed_substage_positions)
            refreshed_substage_account = client.get_account()
            refreshed_substage_buying_power, refreshed_substage_buying_power_source = _buying_power(
                refreshed_substage_account
            )
            refreshed_substage_equity, refreshed_substage_equity_source = _resolve_account_equity(
                account=refreshed_substage_account,
                signed_notional=refreshed_substage_signed_notional,
            )
            release_reference_prices = _resolve_reference_prices(
                client=client,
                symbols=sorted(set(stage_symbols) | set(refreshed_substage_signed_notional)),
                fallback_prices=release_reference_prices,
                feed=execution_price_feed,
                prefer_live=True,
            )
            release_min_trade_notional = _effective_min_trade_notional(
                account_equity=float(refreshed_substage_equity),
                absolute_floor=float(min_trade_notional_floor),
                weight_bps=float(min_trade_weight_bps),
            )
            rebuilt_instructions, rebuilt_skipped = _build_order_instructions(
                target_signed_weights=release_target_signed_weights,
                current_signed_notional=refreshed_substage_signed_notional,
                current_signed_qty=refreshed_substage_signed_qty,
                account_equity=float(account_equity),
                reference_prices=release_reference_prices,
                assets_by_symbol=assets_by_symbol,
                min_trade_notional=float(release_min_trade_notional),
                sizing_adverse_offset_bps=float(sizing_adverse_offset_bps),
                qty_decimals=int(qty_decimals),
                whole_shares_only=bool(whole_shares_only),
                opening_shorts_whole_shares_only=bool(opening_shorts_whole_shares_only),
                short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
                shorting_enabled=bool(shorting_enabled),
            )
            rebuilt_release, _ = _split_release_entry_instructions(rebuilt_instructions)
            rebuilt_sell_long, rebuilt_buy_to_cover = _split_release_substages(rebuilt_release)
            current_stage_instructions = rebuilt_sell_long if stage_name == "release_sell_long" else rebuilt_buy_to_cover
            current_stage_instructions = [item for item in current_stage_instructions if item.symbol in set(stage_symbols)]
            stage_remaining_symbols = [item.symbol for item in current_stage_instructions]
            round_fully_filled = not current_stage_instructions
            snapshots.append(
                {
                    "schema_version": "1.0",
                    "snapshot_type": "release_round",
                    "captured_at_utc": _utc_now(),
                    "stage": stage_name,
                    "round": int(round_no),
                    "stage_symbols": list(stage_symbols),
                    "session_token": f"{session_token}_{stage_token}_r{round_no:02d}",
                    "limit_base_offset_bps": float(round_offset_bps),
                    "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
                    "marketable_limit_requote_steps_bps": [
                        float(value) for value in marketable_limit_requote_steps_bps
                    ],
                    "marketable_limit_requote_wait_seconds": float(marketable_limit_requote_wait_seconds),
                    "input_instructions": _instruction_payloads(round_input_instructions),
                    "submitted_records": release_records,
                    "refreshed_positions_raw": _raw_dict_list(refreshed_substage_positions),
                    "refreshed_signed_notional": dict(sorted(refreshed_substage_signed_notional.items())),
                    "refreshed_signed_qty": dict(sorted(refreshed_substage_signed_qty.items())),
                    "refreshed_account_raw": dict(refreshed_substage_account)
                    if isinstance(refreshed_substage_account, dict)
                    else refreshed_substage_account,
                    "buying_power_after_stage": float(refreshed_substage_buying_power),
                    "buying_power_source": str(refreshed_substage_buying_power_source),
                    "account_equity_after_stage": float(refreshed_substage_equity),
                    "account_equity_source": str(refreshed_substage_equity_source),
                    "effective_min_trade_notional": float(release_min_trade_notional),
                    "min_trade_weight_bps": float(min_trade_weight_bps),
                    "reference_prices": dict(sorted(release_reference_prices.items())),
                    "rebuilt_all_instructions": _instruction_payloads(rebuilt_instructions),
                    "rebuilt_release_instructions": _instruction_payloads(rebuilt_release),
                    "rebuilt_stage_instructions": _instruction_payloads(current_stage_instructions),
                    "rebuilt_skipped_orders": rebuilt_skipped,
                    "remaining_order_count": int(len(current_stage_instructions)),
                    "remaining_symbols": list(stage_remaining_symbols),
                    "fully_filled": bool(round_fully_filled),
                }
            )
            diagnostics["release_substages"].append(
                {
                    "stage": stage_name,
                    "round": int(round_no),
                    "order_count": int(len(release_records)),
                    "record_count": int(len(release_records)),
                    "fully_filled": bool(round_fully_filled),
                    "remaining_order_count": int(len(current_stage_instructions)),
                    "remaining_symbols": list(stage_remaining_symbols),
                    "rebuilt_skipped_orders": rebuilt_skipped,
                    "limit_base_offset_bps": float(round_offset_bps),
                    "marketable_limit_max_offset_bps": float(marketable_limit_max_offset_bps),
                    "buying_power_after_stage": float(refreshed_substage_buying_power),
                    "buying_power_source": str(refreshed_substage_buying_power_source),
                }
            )
            if round_fully_filled:
                stage_fully_filled = True
                break
            if round_no < max(1, int(release_max_rounds)) and float(release_round_sleep_seconds) > 0:
                time.sleep(float(release_round_sleep_seconds))

        if not stage_fully_filled:
            diagnostics["release_fully_filled"] = False
            diagnostics["entry_aborted"] = True
            diagnostics["entry_abort_reason"] = f"{stage_name}_not_fully_filled_after_{int(max(1, release_max_rounds))}_rounds"
            diagnostics["release_unfilled_stage"] = stage_name
            diagnostics["release_unfilled_symbols"] = list(stage_remaining_symbols)
            diagnostics["release_stage_records"] = int(len(stage_records_total))
            snapshots.append(
                {
                    "schema_version": "1.0",
                    "snapshot_type": "entry_abort",
                    "captured_at_utc": _utc_now(),
                    "stage": stage_name,
                    "entry_abort_reason": diagnostics["entry_abort_reason"],
                    "remaining_symbols": list(stage_remaining_symbols),
                    "release_stage_record_count": int(len(stage_records_total)),
                    "release_fully_filled": False,
                }
            )
            return records, diagnostics

    refreshed_positions = client.list_positions()
    _, refreshed_signed_notional = _positions_to_frame_and_notional(refreshed_positions)
    refreshed_signed_qty = _signed_qty_from_positions(refreshed_positions)
    refreshed_account = client.get_account()
    buying_power, buying_power_source = _buying_power(refreshed_account)
    refreshed_equity, refreshed_equity_source = _resolve_account_equity(
        account=refreshed_account,
        signed_notional=refreshed_signed_notional,
    )
    refreshed_prices = _resolve_reference_prices(
        client=client,
        symbols=sorted(set(target_signed_weights) | set(refreshed_signed_notional)),
        fallback_prices=fallback_prices,
        feed=execution_price_feed,
        prefer_live=True,
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
    rebuilt_release_residual, entry_instructions = _split_release_entry_instructions(entry_instructions)
    entry_instructions_before_cap = list(entry_instructions)
    entry_instructions, cap_diag = _scale_entry_instructions_to_buying_power(
        entry_instructions,
        buying_power=float(buying_power),
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
            "initial_account_equity": float(account_equity),
            "fresh_account_equity": float(refreshed_equity),
            "fresh_account_equity_source": str(refreshed_equity_source),
            "effective_min_trade_notional": float(entry_min_trade_notional),
            "min_trade_weight_bps": float(min_trade_weight_bps),
            "entry_rebuild_order_count": int(len(entry_instructions)),
            "entry_rebuild_skipped_orders": entry_skipped,
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
        "entry_instructions_before_buying_power_cap": _instruction_payloads(entry_instructions_before_cap),
        "entry_skipped_orders": entry_skipped,
        "entry_buying_power_cap": cap_diag,
        "final_entry_instructions": _instruction_payloads(entry_instructions),
        "final_entry_symbols": _instruction_symbols(entry_instructions),
        "submitted_records": [],
    }
    snapshots.append(entry_snapshot)

    if entry_instructions:
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
        )
        for record in entry_records:
            record["stage"] = "entry"
        records.extend(entry_records)
        diagnostics["entry_records"] = int(len(entry_records))
        entry_snapshot["submitted_records"] = entry_records
        entry_snapshot["submitted_record_count"] = int(len(entry_records))
    else:
        entry_snapshot["entry_submission_skipped_reason"] = "no_entry_instructions_after_rebuild_or_buying_power_cap"
        entry_snapshot["submitted_record_count"] = 0

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


def _mark_event(events: list[dict[str, Any]], name: str, payload: Mapping[str, Any] | None = None) -> None:
    event = {
        "name": str(name),
        "at_utc": _utc_now(),
        "monotonic_seconds": float(time.monotonic()),
    }
    if payload:
        event["payload"] = dict(payload)
    events.append(event)


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
        "account_hash_count": int(len(set(account_hashes))),
        "position_symbol_counts": position_counts,
        "position_symbol_count_stable": len(set(position_counts)) <= 1 if position_counts else False,
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


def _write_jsonl_file(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False, default=_json_default) + "\n")


def _write_run_events(output_root: Path, events: Sequence[Mapping[str, Any]]) -> Path:
    path = output_root / "run_events.jsonl"
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
            "broker_account_configurations_before.json",
            "broker_account_configurations_after.json",
            "broker_clock_before.json",
            "broker_clock_after.json",
        ],
        "orders_and_activity": [
            "order_plan.json",
            "execution_records.json",
            "order_poll_timeline.json",
            "broker_open_orders_before.json",
            "broker_orders_all_before.json",
            "broker_open_orders_after.json",
            "broker_orders_all_after.json",
            "broker_order_snapshots.json",
            "broker_fill_activities.json",
            "broker_account_activities.json",
        ],
        "market_context": [
            "execution_price_snapshot.json",
            "execution_latest_trades_snapshot.json",
            "execution_latest_quotes_snapshot.json",
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
            "target_weights_snapshot.json",
            "executable_target_projection.json",
            "executable_target_projection.csv",
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


def _write_artifact_completeness_snapshot(output_root: Path) -> Path:
    path = output_root / "artifact_completeness_snapshot.json"
    context = _run_submission_context(output_root)
    categories = _expected_artifact_categories(output_root)
    category_status: dict[str, Any] = {}
    for category, names in categories.items():
        rows = []
        for name in names:
            item = output_root / name
            rows.append(
                {
                    "artifact": name,
                    "exists": item.exists(),
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
        "execution_price_snapshot": _json_artifact_status(output_root / "execution_price_snapshot.json"),
        "target_weights_snapshot": _json_artifact_status(output_root / "target_weights_snapshot.json"),
        "executable_target_projection": _json_artifact_status(
            output_root / "executable_target_projection.json"
        ),
        "portfolio_weights_snapshot": _json_artifact_status(output_root / "portfolio_weights_snapshot.json"),
        "portfolio_weights_after_snapshot": _json_artifact_status(
            output_root / "portfolio_weights_after_snapshot.json"
        ),
        "latest_trades_before": _json_artifact_status(output_root / "execution_latest_trades_snapshot.json"),
        "latest_quotes_before": _json_artifact_status(output_root / "execution_latest_quotes_snapshot.json"),
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
        "runtime_environment_snapshot.json",
        "order_plan.json",
        "execution_records.json",
        "broker_account_before.json",
        "broker_account_after.json",
        "broker_positions_before_raw.json",
        "broker_positions_after_raw.json",
        "broker_position_account_stability_before.json",
        "broker_position_account_stability_after.json",
        "broker_fill_activities.json",
        "broker_account_activities.json",
        "broker_order_snapshots.json",
        "order_poll_timeline.json",
        "alpaca_api_audit.jsonl",
        "execution_price_snapshot.json",
        "executable_target_projection.json",
        "executable_target_projection.csv",
        "execution_intraday_bars_1min.json",
        "execution_intraday_bars_1min_after.json",
        "execution_latest_quotes_snapshot.json",
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


def _input_file_manifest(args: argparse.Namespace, ledger_path: Path) -> dict[str, Any]:
    paths: dict[str, Path] = {
        "accounts_json_path": Path(str(args.accounts_json_path)).resolve(),
        "candidate_symbols_path": Path(str(args.candidate_symbols_path)).resolve(),
        "ledger_path": ledger_path.resolve(),
    }
    optional_keys = [
        "decision_targets_input_path",
        "order_plan_input_path",
        "sec_ticker_map_cache_path",
        "sec_companyfacts_cache_dir",
        "sec_submissions_cache_dir",
        "sec_cache_root",
    ]
    for key in optional_keys:
        raw = getattr(args, key, None)
        if raw:
            paths[key] = Path(str(raw)).resolve()
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
