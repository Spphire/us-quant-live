from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
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
from lot_manager import DEFAULT_FACTOR_MIN_HOLDS, LotManager  # noqa: E402
from vendors import (  # noqa: E402
    AlpacaHttpClient,
    AlpacaRequestError,
    IbkrCredentials,
    IbkrHttpClient,
    IbkrRequestError,
)


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "ibkr_executor"
DEFAULT_LEDGER_PATH = PROJECT_ROOT / "artifacts" / "ibkr_executor" / "lot_ledger.json"
EPS = 1e-10
TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "inactive",
    "api cancelled",
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
    current_notional: float
    target_notional: float
    delta_notional: float
    opening_short: bool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute daily AlphaCore + DecisionEngine plan with IBKR execution: "
            "broker/lot sync -> alpha decision -> open-triggered order submit -> post-trade lot sync."
        )
    )
    parser.add_argument("--date", default=date.today().isoformat())

    parser.add_argument(
        "--accounts-json-path",
        default="configs/alpaca_acounts/alpaca_accounts.local.json",
        help="Alpaca account config used for market-data pulls (not for execution).",
    )
    parser.add_argument("--account-name", default="ALPACA_US_FULL", help="Account key in Alpaca accounts JSON.")
    parser.add_argument("--data-base-url", default="https://data.alpaca.markets", help="Alpaca data API base URL.")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument(
        "--ibkr-base-url",
        default="https://127.0.0.1:5000/v1/api",
        help="IBKR Client Portal API base URL (Gateway/TWS CPAPI).",
    )
    parser.add_argument("--ibkr-account-id", default=None)
    parser.add_argument("--ibkr-verify-tls", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--ibkr-max-retries", type=int, default=2)
    parser.add_argument("--ibkr-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--ibkr-auth-init-if-needed", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ibkr-auth-compete", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--ibkr-enforce-shortable-check",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether opening shorts must pass broker asset shortable metadata check.",
    )

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
        help="Feed for dynamic symbol pool refresh.",
    )

    parser.add_argument("--feed", default="sip", help="Feed used by AlphaCore bars fetch.")
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
    parser.add_argument("--execution-price-feed", default="sip")
    parser.add_argument(
        "--marketable-limit-base-offset-bps",
        type=float,
        default=12.0,
        help="Initial marketable limit offset in bps from reference price.",
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
    parser.add_argument("--min-trade-notional", type=float, default=200.0)
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
        default=False,
        help=(
            "Force whole-share qty for any sell order that creates or increases a short "
            "position. Disabled by default for IBKR, but useful for Alpaca-compatible plans."
        ),
    )
    parser.add_argument(
        "--floor-short-targets-to-whole-shares",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Project target short weights to floor(target short shares) before order generation.",
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

    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv)

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

        alpaca_credentials = _resolve_alpaca_credentials(
            accounts_json_path=str(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url=str(args.data_base_url),
            request_timeout_seconds=float(args.request_timeout_seconds),
            max_retries=int(args.max_retries),
        )
        alpaca_client = AlpacaHttpClient(alpaca_credentials)

        ibkr_credentials = IbkrCredentials(
            base_url=str(args.ibkr_base_url).strip().rstrip("/"),
            account_id=(str(args.ibkr_account_id).strip().upper() if args.ibkr_account_id else None),
            request_timeout_seconds=float(args.ibkr_timeout_seconds),
            max_retries=int(args.ibkr_max_retries),
            verify_tls=bool(args.ibkr_verify_tls),
        )
        broker_client = IbkrHttpClient(ibkr_credentials)
        broker_client.ensure_authenticated(
            init_if_needed=bool(args.ibkr_auth_init_if_needed),
            compete=bool(args.ibkr_auth_compete),
        )
        account_before = broker_client.get_account()
        broker_account_id = str(account_before.get("account_id") or "").strip().upper()
        shorting_enabled = bool(account_before.get("shorting_enabled", True))

        positions_before = broker_client.list_positions()
        broker_frame_before, broker_signed_notional_before = _positions_to_frame_and_notional(positions_before)
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
        lot_manager = LotManager.from_json(ledger_path)
        resolved_session_idx = _resolve_session_idx(lot_manager, args.session_idx)

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
                "executor_broker": "ibkr_client_portal",
                "executor_broker_account_id": broker_account_id,
            }
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
        else:
            candidate_symbols = _load_candidate_symbols(Path(args.candidate_symbols_path))
            pool = DynamicSymbolPool(
                client=alpaca_client,
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
                alpaca_client=alpaca_client,
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

            decision_targets_path = output_root / "decision_targets.csv"
            target_signed_weights = _signed_weights_from_lot(lot_manager)
            _target_weights_to_frame(target_signed_weights).to_csv(decision_targets_path, index=False)
        assets_by_symbol: dict[str, Mapping[str, Any]] = {}

        fallback_prices = _build_fallback_price_map(
            alpha_panel=alpha_panel,
            broker_positions=broker_frame_before,
        )
        reference_prices = _resolve_reference_prices(
            client=alpaca_client,
            symbols=sorted(set(target_signed_weights) | set(broker_signed_notional_before)),
            fallback_prices=fallback_prices,
            feed=str(args.execution_price_feed),
        )

        account_for_sizing = broker_client.get_account()
        shorting_enabled = bool(account_for_sizing.get("shorting_enabled", shorting_enabled))
        sizing_equity, sizing_equity_source = _resolve_account_equity(
            account=account_for_sizing,
            signed_notional=broker_signed_notional_before,
        )
        raw_target_signed_weights = dict(target_signed_weights)
        target_signed_weights, target_short_floor_diag = _project_short_targets_to_whole_shares(
            signed_weights=target_signed_weights,
            reference_prices=reference_prices,
            account_equity=sizing_equity,
            enabled=bool(args.floor_short_targets_to_whole_shares),
        )

        instructions, skipped_orders = _build_order_instructions(
            target_signed_weights=target_signed_weights,
            current_signed_notional=broker_signed_notional_before,
            account_equity=sizing_equity,
            reference_prices=reference_prices,
            assets_by_symbol=assets_by_symbol,
            min_trade_notional=float(args.min_trade_notional),
            qty_decimals=int(args.qty_decimals),
            whole_shares_only=bool(args.whole_shares_only),
            opening_shorts_whole_shares_only=bool(args.opening_shorts_whole_shares_only),
            short_sales_whole_shares_only=bool(args.short_sales_whole_shares_only),
            shorting_enabled=shorting_enabled,
            enforce_shortable_check=bool(args.ibkr_enforce_shortable_check),
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
                    "execution_broker": "ibkr_client_portal",
                    "broker_account_id": broker_account_id,
                    "account_equity": float(sizing_equity),
                    "account_equity_source": str(sizing_equity_source),
                    "trigger_mode": str(args.trigger_mode),
                    "target_ny_time": str(args.target_ny_time),
                    "execution_order_style": str(args.execution_order_style),
                    "whole_shares_only": bool(args.whole_shares_only),
                    "opening_shorts_whole_shares_only": bool(args.opening_shorts_whole_shares_only),
                    "short_sales_whole_shares_only": bool(args.short_sales_whole_shares_only),
                    "floor_short_targets_to_whole_shares": bool(args.floor_short_targets_to_whole_shares),
                    "target_short_floor_diagnostics": target_short_floor_diag,
                    "marketable_limit_base_offset_bps": float(args.marketable_limit_base_offset_bps),
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
        if should_submit and instructions:
            if bool(args.cancel_open_orders_before_submit):
                try:
                    broker_client.cancel_all_orders()
                except IbkrRequestError as exc:
                    print(f"[Executor] warning: cancel open orders failed: {exc}", flush=True)

            if str(args.trigger_mode) == "wait_open":
                _wait_for_market_open(
                    client=alpaca_client,
                    open_buffer_seconds=int(args.open_buffer_seconds),
                )
            elif str(args.trigger_mode) == "wait_target_time":
                _wait_for_target_ny_time(
                    client=alpaca_client,
                    target_ny_time=str(args.target_ny_time),
                    open_buffer_seconds=int(args.open_buffer_seconds),
                )

            execution_records = _submit_and_track_orders(
                client=broker_client,
                instructions=instructions,
                session_token=f"{decision_date.strftime('%Y%m%d')}_{int(resolved_session_idx):05d}",
                timeout_seconds=float(args.order_timeout_seconds),
                poll_seconds=float(args.order_poll_seconds),
                execution_order_style=str(args.execution_order_style),
                marketable_limit_base_offset_bps=float(args.marketable_limit_base_offset_bps),
                marketable_limit_requote_steps_bps=marketable_limit_requote_steps_bps,
                marketable_limit_requote_wait_seconds=float(args.marketable_limit_requote_wait_seconds),
            )

        positions_after = broker_client.list_positions()
        broker_frame_after, broker_signed_notional_after = _positions_to_frame_and_notional(positions_after)
        account_after = broker_client.get_account()
        equity_after, equity_after_source = _resolve_account_equity(
            account=account_after,
            signed_notional=broker_signed_notional_after,
        )
        broker_weights_after = _weights_from_signed_notional(
            broker_signed_notional_after,
            equity=equity_after,
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
                "executor_broker": "ibkr_client_portal",
                "executor_broker_account_id": broker_account_id,
            }
        )
        day_lot_snapshot_path = output_root / f"lot_snapshot_{decision_date.strftime('%Y%m%d')}.json"
        if ledger_write_enabled:
            post_trade_lot_manager.to_json(ledger_path)
        post_trade_lot_manager.to_json(day_lot_snapshot_path, extra_meta={"snapshot_type": "post_execution_daily"})

        broker_frame_before.to_csv(output_root / "broker_positions_before.csv", index=False)
        broker_frame_after.to_csv(output_root / "broker_positions_after.csv", index=False)

        alignment_after = _alignment_to_target(
            target_signed_weights=target_signed_weights,
            broker_weights=broker_weights_after,
        )
        execution_summary = {
            "ok": True,
            "decision_date": decision_date.isoformat(),
            "session_idx": int(resolved_session_idx),
            "order_plan_input_path": plan_input_path,
            "execution_broker": "ibkr_client_portal",
            "broker_account_id": broker_account_id,
            "account_equity": float(sizing_equity),
            "account_equity_source": str(sizing_equity_source),
            "account_equity_post_trade": float(equity_after),
            "account_equity_post_trade_source": str(equity_after_source),
            "trigger_mode": str(args.trigger_mode),
            "target_ny_time": str(args.target_ny_time),
            "execution_order_style": str(args.execution_order_style),
            "decision_status": decision_status,
            "decision_skip_reason": decision_skip_reason,
            "dynamic_symbols": int(len(symbols)),
            "order_plan_count": int(len(instructions)),
            "submitted": bool(should_submit),
            "submitted_orders": int(len(execution_records)),
            "ledger_write_enabled": bool(ledger_write_enabled),
            "lot_ledger_path": ledger_path.as_posix(),
            "daily_lot_snapshot_path": day_lot_snapshot_path.as_posix(),
            "alignment_after_execution": alignment_after,
            "outputs": {
                "alpha_panel_csv": alpha_path.as_posix() if alpha_path else None,
                "decision_targets_csv": decision_targets_path.as_posix() if decision_targets_path else None,
                "order_plan_json": plan_path.as_posix(),
                "broker_positions_before_csv": (output_root / "broker_positions_before.csv").as_posix(),
                "broker_positions_after_csv": (output_root / "broker_positions_after.csv").as_posix(),
                "execution_records_json": (output_root / "execution_records.json").as_posix(),
            },
        }
        (output_root / "execution_records.json").write_text(
            json.dumps(execution_records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (output_root / "execution_summary.json").write_text(
            json.dumps(execution_summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(json.dumps(execution_summary, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, FileNotFoundError, AlpacaRequestError, IbkrRequestError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
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


def _extract_numeric_from_unknown(value: Any) -> float | None:
    if value is None:
        return None
    number = _safe_float(value)
    if number is not None:
        return number
    if isinstance(value, Mapping):
        for key in ("amount", "value", "val", "raw"):
            number = _safe_float(value.get(key))
            if number is not None:
                return number
    return None


def _resolve_account_equity(
    account: Mapping[str, Any],
    signed_notional: Mapping[str, float] | None = None,
) -> tuple[float, str]:
    for field in (
        "portfolio_value",
        "equity",
        "last_equity",
        "netliquidationvalue",
        "net_liquidation",
        "netLiquidation",
    ):
        value = _safe_float(account.get(field))
        if value is not None and value > 0:
            return float(value), f"account.{field}"

    ledger = account.get("ledger")
    if isinstance(ledger, Mapping):
        base_ledger = ledger.get("BASE")
        if isinstance(base_ledger, Mapping):
            for field in ("netliquidationvalue", "cashbalance", "stockmarketvalue"):
                value = _safe_float(base_ledger.get(field))
                if value is not None and value > 0:
                    return float(value), f"account.ledger.BASE.{field}"

    summary = account.get("summary")
    if isinstance(summary, Mapping):
        for field in ("netliquidation", "netLiquidation", "equitywithloanvalue"):
            value = _safe_float(summary.get(field))
            if value is None:
                value = _extract_numeric_from_unknown(summary.get(field))
            if value is not None and value > 0:
                return float(value), f"account.summary.{field}"

    for field in ("cash", "cashbalance", "cashBalance"):
        cash = _safe_float(account.get(field))
        if cash is not None and cash > 0:
            return float(cash), f"account.{field}"

    if signed_notional:
        gross = float(sum(abs(float(value)) for value in signed_notional.values()))
        if gross > 0:
            return float(gross), "fallback.gross_position_notional"

    raise ValueError(
        "Unable to resolve positive account equity from broker account fields "
        "(portfolio_value/equity/netliquidationvalue/ledger/summary/cash)."
    )


def _resolve_session_idx(lot_manager: LotManager, provided: int | None) -> int:
    if provided is not None:
        return int(provided)
    if "last_session_idx" in lot_manager.meta:
        return int(lot_manager.meta["last_session_idx"]) + 1
    return int(lot_manager.max_birth_idx()) + 1


def _positions_to_frame_and_notional(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[pd.DataFrame, dict[str, float]]:
    rows: list[dict[str, Any]] = []
    notional: dict[str, float] = {}
    for raw in positions:
        symbol = str(raw.get("symbol") or raw.get("ticker") or "").strip().upper()
        if not symbol:
            continue
        raw_side = str(raw.get("side") or "").strip().lower()
        position_signed = _safe_float(raw.get("position"))
        qty_raw = _safe_float(raw.get("qty"))
        if position_signed is not None:
            qty_signed = float(position_signed)
        else:
            qty_abs_raw = abs(float(qty_raw or 0.0))
            qty_signed = -qty_abs_raw if raw_side == "short" else qty_abs_raw

        qty = abs(float(qty_signed))
        current_price = _safe_float(raw.get("current_price"))
        if current_price is None:
            current_price = _safe_float(raw.get("mktPrice"))
        if current_price is None:
            current_price = _safe_float(raw.get("market_price"))
        market_value = _safe_float(raw.get("market_value"))
        if market_value is None:
            market_value = _safe_float(raw.get("mktValue"))
        if market_value is None and current_price is not None:
            market_value = qty * float(current_price)
        if market_value is None:
            market_value = 0.0

        if qty_signed < 0:
            side = "short"
            market_value_signed = -abs(market_value)
        else:
            side = "long"
            market_value_signed = abs(market_value)

        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "qty": float(abs(qty)),
                "signed_qty": float(qty_signed),
                "current_price": float(current_price) if current_price is not None else np.nan,
                "market_value": float(market_value_signed),
                "avg_entry_price": _safe_float(raw.get("avg_entry_price")) or _safe_float(raw.get("avgCost")),
                "raw": dict(raw),
            }
        )
        notional[symbol] = notional.get(symbol, 0.0) + float(market_value_signed)

    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["symbol", "side"]).reset_index(drop=True)
    return frame, notional


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
) -> dict[str, float]:
    out: dict[str, float] = {
        str(symbol).upper(): float(price)
        for symbol, price in fallback_prices.items()
        if _safe_float(price) is not None and float(price) > 0
    }
    needed = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip() and str(symbol).upper() not in out})
    for chunk in _chunks(needed, 150):
        try:
            trades = client.get_latest_trades(symbols=chunk, feed=str(feed))
        except AlpacaRequestError:
            continue
        for symbol, trade in trades.items():
            px = _safe_float(trade.get("p"))
            if px is not None and px > 0:
                out[str(symbol).upper()] = float(px)
    return out


def _quantize_qty(raw_qty: float, *, whole_shares_only: bool, decimals: int) -> float:
    qty = max(0.0, float(raw_qty))
    if whole_shares_only:
        return float(math.floor(qty))
    scale = 10 ** max(0, int(decimals))
    return float(math.floor(qty * scale) / scale)


def _project_short_targets_to_whole_shares(
    *,
    signed_weights: Mapping[str, float],
    reference_prices: Mapping[str, float],
    account_equity: float,
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
        desired = abs(weight) * safe_equity
        desired_short_notional += float(desired)
        floored_shares = float(math.floor(max(0.0, desired / float(px)) + 1e-12))
        realized = floored_shares * float(px)
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
    }


def _build_order_instructions(
    *,
    target_signed_weights: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    account_equity: float,
    reference_prices: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    min_trade_notional: float,
    qty_decimals: int,
    whole_shares_only: bool,
    opening_shorts_whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    shorting_enabled: bool,
    enforce_shortable_check: bool,
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
        opening_short = side == "sell" and target_notional < 0 and current_notional <= EPS
        increasing_short = side == "sell" and target_notional < current_notional and target_notional < -EPS
        short_sale = bool(opening_short or increasing_short)
        if opening_short:
            if not shorting_enabled:
                skipped.append({"symbol": symbol, "reason": "account_shorting_disabled", "delta_notional": delta_notional})
                continue
            if bool(enforce_shortable_check):
                asset = assets_by_symbol.get(symbol, {})
                shortable = bool(asset.get("shortable", False))
                if not shortable:
                    skipped.append(
                        {"symbol": symbol, "reason": "asset_not_shortable", "delta_notional": delta_notional}
                    )
                    continue

        should_force_whole_share = bool(whole_shares_only) or (
            bool(opening_shorts_whole_shares_only) and bool(opening_short)
        ) or (
            bool(short_sales_whole_shares_only) and bool(short_sale)
        )

        qty = _quantize_qty(
            abs(delta_notional) / float(px),
            whole_shares_only=should_force_whole_share,
            decimals=qty_decimals,
        )
        if qty <= 0:
            skipped.append({"symbol": symbol, "reason": "qty_rounded_to_zero", "delta_notional": delta_notional, "price": px})
            continue
        est_notional = qty * float(px)
        if est_notional < float(min_trade_notional):
            skipped.append(
                {
                    "symbol": symbol,
                    "reason": "notional_below_threshold_after_rounding",
                    "delta_notional": delta_notional,
                    "estimated_notional": est_notional,
                }
            )
            continue
        instructions.append(
            OrderInstruction(
                symbol=symbol,
                side=side,
                qty=float(qty),
                reference_price=float(px),
                current_notional=float(current_notional),
                target_notional=float(target_notional),
                delta_notional=float(delta_notional),
                opening_short=bool(opening_short),
            )
        )
    instructions.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    return instructions, skipped


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
    return str(order.get("order_status") or order.get("status") or "").strip().lower()


def _order_filled_qty(order: Mapping[str, Any] | None) -> float:
    if not order:
        return 0.0
    for key in ("filled_qty", "filledQuantity", "filled", "cum_qty"):
        value = _safe_float(order.get(key))
        if value is not None:
            return max(0.0, float(value))
    return 0.0


def _order_filled_avg_price(order: Mapping[str, Any] | None) -> float | None:
    if not order:
        return None
    for key in ("filled_avg_price", "avg_price", "avgPrice"):
        value = _safe_float(order.get(key))
        if value is not None and value > 0:
            return float(value)
    return None


def _order_updated_at(order: Mapping[str, Any] | None) -> str:
    if not order:
        return ""
    for key in ("updated_at", "lastExecutionTime", "lastExecutionTime_r"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _poll_order_until(
    *,
    client: IbkrHttpClient,
    order_id: str,
    deadline_monotonic: float,
    poll_seconds: float,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    while True:
        latest = client.get_order(order_id)
        status = _order_status(latest)
        if status in TERMINAL_ORDER_STATUSES:
            return latest
        if time.monotonic() >= deadline_monotonic:
            return latest
        time.sleep(max(0.5, float(poll_seconds)))


def _submit_and_track_orders(
    *,
    client: IbkrHttpClient,
    instructions: Sequence[OrderInstruction],
    session_token: str,
    timeout_seconds: float,
    poll_seconds: float,
    execution_order_style: str,
    marketable_limit_base_offset_bps: float,
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

        if str(execution_order_style) == "market":
            client_order_id = f"sm_{session_token}_{idx:04d}_{item.side}_{item.symbol}".lower()[:48]
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
            if order_id:
                latest_order = _poll_order_until(
                    client=client,
                    order_id=order_id,
                    deadline_monotonic=deadline,
                    poll_seconds=poll_seconds,
                )
            record = {
                **base_record,
                "execution_order_style": "market",
                "client_order_id": client_order_id,
                "order_id": order_id,
                "status_initial": _order_status(placed_order),
                "status_latest": _order_status(latest_order),
                "filled_avg_price": _order_filled_avg_price(latest_order),
                "filled_qty": _order_filled_qty(latest_order),
                "updated_at": _order_updated_at(latest_order),
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

        for attempt_no, step_bps in enumerate(marketable_limit_requote_steps_bps, start=1):
            if remaining_qty <= EPS:
                break
            if time.monotonic() >= global_deadline:
                break

            total_offset_bps = max(0.0, float(marketable_limit_base_offset_bps) + float(step_bps))
            limit_price = _marketable_limit_price(
                side=item.side,
                reference_price=item.reference_price,
                offset_bps=total_offset_bps,
            )
            client_order_id = (
                f"sm_{session_token}_{idx:04d}_{item.side}_{item.symbol}_a{attempt_no:02d}".lower()[:48]
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
            if order_id:
                latest_order = _poll_order_until(
                    client=client,
                    order_id=order_id,
                    deadline_monotonic=attempt_deadline,
                    poll_seconds=poll_seconds,
                )

            status = _order_status(latest_order)
            need_cancel = remaining_qty > EPS and status not in TERMINAL_ORDER_STATUSES
            if need_cancel and order_id:
                try:
                    client.cancel_order(order_id)
                except IbkrRequestError:
                    pass
                latest_order = client.get_order(order_id)
                status = _order_status(latest_order)

            filled_qty_this_attempt = _order_filled_qty(latest_order)
            filled_qty_this_attempt = min(remaining_qty, filled_qty_this_attempt)
            remaining_qty = max(0.0, remaining_qty - filled_qty_this_attempt)
            total_filled_qty += filled_qty_this_attempt

            latest_status = status
            latest_filled_avg_price = _order_filled_avg_price(latest_order)
            latest_updated_at = _order_updated_at(latest_order)

            attempts.append(
                {
                    "attempt_no": int(attempt_no),
                    "client_order_id": client_order_id,
                    "order_id": order_id,
                    "qty_submitted": float(_safe_float(placed_order.get("qty")) or remaining_qty + filled_qty_this_attempt),
                    "limit_price": float(limit_price),
                    "offset_bps": float(total_offset_bps),
                    "status_latest": latest_status,
                    "filled_qty": float(filled_qty_this_attempt),
                    "filled_avg_price": latest_filled_avg_price,
                    "updated_at": latest_updated_at,
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

    return records


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
