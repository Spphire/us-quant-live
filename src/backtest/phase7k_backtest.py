from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _resolve_project_root() -> Path:
    """Locate project root by walking upward until src/alpha_core.py exists."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "src" / "alpha_core.py").exists():
            return candidate
    # Fallback for unexpected layouts.
    return here.parent.parent


PROJECT_ROOT = _resolve_project_root()
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpha_core import (  # noqa: E402
    DEFAULT_FACTOR_WEIGHTS,
    AlphaCore,
    SecApiClient,
    RAW_FACT_COLUMNS,
    _extract_fundamental_snapshot,
    _extract_share_snapshot,
    _normalize_cik,
    _print_progress,
    _resolve_industry_map_for_symbols,
    _resolve_sec_cache_paths,
)
from decision_engine import DecisionConfig, DecisionEngine  # noqa: E402
from dynamic_symbol_pool import (  # noqa: E402
    DEFAULT_CANDIDATE_SYMBOLS_PATH,
    _build_runtime_clean_core_symbol_set,
    _build_tradable_symbol_set,
    _load_candidate_symbols,
    _resolve_alpaca_credentials,
)
from lot_manager import DEFAULT_FACTOR_MIN_HOLDS, LotManager  # noqa: E402
from vendors import AlpacaHttpClient, AlpacaRequestError  # noqa: E402


DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "phase7k_backtest"


@dataclass
class CostModel:
    execution_bps: float
    sell_notional_fraction: float
    sec_fee_rate: float
    taf_per_share: float
    taf_cap_per_trade: float


@dataclass
class PortfolioState:
    tag: str
    scenario: str
    initial_equity: float
    cash: float
    shares: dict[str, float]
    last_known_prices: dict[str, float]


@dataclass(frozen=True)
class ExecutionScenario:
    key: str
    label: str
    opening_shorts_whole_shares_only: bool
    short_sales_whole_shares_only: bool
    floor_short_targets_to_whole_shares: bool


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a long-horizon Phase7K-style backtest: "
            "dynamic symbol pool + AlphaCore + DecisionEngine + lot ledger."
        )
    )
    parser.add_argument("--start-date", default="2016-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())

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
    parser.add_argument("--dynamic-beta-full-observations", type=int, default=252)

    parser.add_argument("--feed", default="iex")
    parser.add_argument("--price-adjustment", default="all", help="raw/split/dividend/all")
    parser.add_argument("--bars-chunk-size", type=int, default=120)
    parser.add_argument("--bars-workers", type=int, default=8)
    parser.add_argument("--bars-window-calendar-days", type=int, default=420)
    parser.add_argument(
        "--prefetch-buffer-days",
        type=int,
        default=30,
        help="Extra calendar-day buffer before start date when prefetching Alpaca bars.",
    )

    parser.add_argument("--benchmark-symbol", default="SPY")
    parser.add_argument("--compare-symbol", default="QQQ")

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
    parser.add_argument("--sec-cache-profile", choices=("live", "backtest"), default="backtest")
    parser.add_argument(
        "--sec-cache-mode",
        choices=("network", "prefer", "cache_only", "auto"),
        default="auto",
        help="SEC cache mode; auto=prefer for backtest profile and network for live profile.",
    )
    parser.add_argument("--sec-cache-root", default=None)
    parser.add_argument("--sec-ticker-map-cache-path", default=None)
    parser.add_argument("--sec-companyfacts-cache-dir", default=None)
    parser.add_argument("--sec-submissions-cache-dir", default=None)
    parser.add_argument("--sec-refresh-ticker-map", action="store_true")
    parser.add_argument("--sec-refresh-companyfacts", action="store_true")
    parser.add_argument("--sec-refresh-submissions", action="store_true")

    parser.add_argument("--initial-equity", type=float, default=1_000_000.0)
    parser.add_argument(
        "--initial-equities",
        default="",
        help=(
            "Comma-separated multi-tier initial equities (for example: "
            "10000,50000,100000,300000). Empty means use --initial-equity only."
        ),
    )
    parser.add_argument(
        "--performance-warmup-sessions",
        type=int,
        default=252,
        help="Only evaluate and normalize equity/benchmark after this many benchmark sessions.",
    )
    parser.add_argument("--candidate-pool-per-side", type=int, default=120)
    parser.add_argument("--max-single-name-side-weight", type=float, default=1.0 / 30.0)
    parser.add_argument("--min-nonzero-names", type=int, default=20)
    parser.add_argument("--score-weight", type=float, default=0.01)
    parser.add_argument("--sector-penalty", type=float, default=25.0)
    parser.add_argument("--turnover-penalty", type=float, default=0.005)
    parser.add_argument("--turnover-budget", type=float, default=0.15)
    parser.add_argument("--beta-band-grid", default="0.05,0.10,0.15,0.20")

    parser.add_argument("--execution-bps", type=float, default=8.0)
    parser.add_argument("--sell-notional-fraction", type=float, default=0.50)
    parser.add_argument("--sec-fee-rate", type=float, default=0.0000278)
    parser.add_argument("--taf-per-share", type=float, default=0.000195)
    parser.add_argument("--taf-cap-per-trade", type=float, default=9.79)
    parser.add_argument("--min-trade-notional", type=float, default=200.0)
    parser.add_argument("--whole-shares-only", action="store_true")
    parser.add_argument(
        "--opening-shorts-whole-shares-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--short-sales-whole-shares-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Force whole-share qty for any sell order that creates or increases a short "
            "position. This is stricter than --opening-shorts-whole-shares-only and "
            "matches Alpaca's no fractional short-sale constraint."
        ),
    )
    parser.add_argument(
        "--floor-short-targets-to-whole-shares",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Project target short weights to floor(target short shares) per account tier "
            "before order generation; long targets are unchanged."
        ),
    )
    parser.add_argument(
        "--execution-scenarios",
        default="",
        help=(
            "Comma-separated scenario keys to run on the same alpha/decision path. "
            "Supported: ideal_fractional, opening_short_integer, short_sale_integer, "
            "baseline_floor_targets. Empty means use the top-level execution flags only."
        ),
    )
    parser.add_argument("--qty-decimals", type=int, default=4)
    parser.add_argument(
        "--shorting-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    parser.add_argument("--output-root", default=None)
    parser.add_argument("--save-alpha-panels", action="store_true")
    parser.add_argument(
        "--live-checkpoint-every-sessions",
        type=int,
        default=10,
        help="Write live checkpoint files every N sessions; set 0 to disable.",
    )
    parser.add_argument(
        "--live-checkpoint-prefix",
        default="live",
        help="Filename prefix for live checkpoint artifacts.",
    )
    parser.add_argument(
        "--live-checkpoint-plots",
        action="store_true",
        help="Render live equity/drawdown PNG files during backtest loop.",
    )
    args = parser.parse_args(argv)

    try:
        start_date = _normalize_date(args.start_date)
        end_date = _normalize_date(args.end_date)
        if end_date < start_date:
            raise ValueError("end-date must be >= start-date")
        initial_equities = _resolve_initial_equities(
            initial_equities_text=str(args.initial_equities),
            fallback_initial_equity=float(args.initial_equity),
        )
        if not initial_equities:
            raise ValueError("At least one initial equity is required.")
        performance_warmup_sessions = max(0, int(args.performance_warmup_sessions))

        output_root = (
            Path(args.output_root)
            if args.output_root
            else DEFAULT_OUTPUT_ROOT / f"{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}"
        )
        output_root.mkdir(parents=True, exist_ok=True)
        alpha_output_root = output_root / "alpha_panels"
        if args.save_alpha_panels:
            alpha_output_root.mkdir(parents=True, exist_ok=True)

        print(
            f"[Backtest] date range: {start_date.isoformat()} -> {end_date.isoformat()}",
            flush=True,
        )
        print(f"[Backtest] output_root: {output_root.as_posix()}", flush=True)
        print(
            f"[Backtest] initial_equities={','.join(f'{value:g}' for value in initial_equities)} "
            f"warmup_sessions={performance_warmup_sessions}",
            flush=True,
        )
        execution_scenarios = _resolve_execution_scenarios(
            scenario_text=str(args.execution_scenarios),
            opening_shorts_whole_shares_only=bool(args.opening_shorts_whole_shares_only),
            short_sales_whole_shares_only=bool(args.short_sales_whole_shares_only),
            floor_short_targets_to_whole_shares=bool(args.floor_short_targets_to_whole_shares),
        )
        primary_equity_tag = _scenario_equity_tag(execution_scenarios[0].key, initial_equities[0])
        print(
            "[Backtest] execution_scenarios="
            + ",".join(f"{scenario.key}:{scenario.label}" for scenario in execution_scenarios),
            flush=True,
        )

        credentials = _resolve_alpaca_credentials(
            accounts_json_path=str(args.accounts_json_path),
            account_name=str(args.account_name),
            data_base_url=str(args.data_base_url),
            request_timeout_seconds=float(args.request_timeout_seconds),
            max_retries=int(args.max_retries),
        )
        alpaca_client = AlpacaHttpClient(credentials)

        candidate_symbols = _load_candidate_symbols(Path(args.candidate_symbols_path))
        print(f"[Backtest] raw candidate symbols: {len(candidate_symbols)}", flush=True)

        print("[Backtest] Step 1/6: fetching Alpaca assets ...", flush=True)
        assets = alpaca_client.list_assets(status="active", asset_class="us_equity")
        assets_by_symbol = {
            str(asset.get("symbol") or "").strip().upper(): asset
            for asset in assets
            if isinstance(asset, Mapping) and str(asset.get("symbol") or "").strip()
        }
        clean_core_symbols = _build_runtime_clean_core_symbol_set(assets)
        tradable_symbols = _build_tradable_symbol_set(assets)
        clean_core_candidates = sorted(set(candidate_symbols).intersection(clean_core_symbols))
        tradable_candidates = sorted(set(clean_core_candidates).intersection(tradable_symbols))
        if not tradable_candidates:
            raise ValueError("No tradable symbols after runtime clean_core filters.")
        print(
            f"[Backtest] Step 1/6 done: assets={len(assets)}, "
            f"clean_core_candidates={len(clean_core_candidates)}, tradable_candidates={len(tradable_candidates)}",
            flush=True,
        )

        warmup_days = int(args.bars_window_calendar_days) + int(args.prefetch_buffer_days)
        prefetch_start = start_date - timedelta(days=warmup_days)
        prefetch_end = end_date

        all_prefetch_symbols = sorted(
            set(tradable_candidates).union({str(args.benchmark_symbol).upper(), str(args.compare_symbol).upper()})
        )
        print(
            f"[Backtest] Step 2/6: prefetching Alpaca bars for {len(all_prefetch_symbols)} symbols "
            f"({prefetch_start.isoformat()} -> {prefetch_end.isoformat()}) ...",
            flush=True,
        )
        bars = _collect_bars_parallel(
            client=alpaca_client,
            symbols=all_prefetch_symbols,
            start=prefetch_start.isoformat(),
            end=prefetch_end.isoformat(),
            chunk_size=int(args.bars_chunk_size),
            workers=int(args.bars_workers),
            feed=str(args.feed),
            adjustment=str(args.price_adjustment),
        )
        if not bars:
            raise ValueError("No bars fetched from Alpaca for backtest range.")
        bars_index = _build_bars_index(bars)
        panel = _bars_to_panel_for_backtest(bars)
        if panel.empty:
            raise ValueError("Bars panel is empty after normalization.")
        print(
            f"[Backtest] Step 2/6 done: raw_bars={len(bars)}, panel_rows={len(panel)}",
            flush=True,
        )

        benchmark_symbol = str(args.benchmark_symbol).strip().upper()
        compare_symbol = str(args.compare_symbol).strip().upper()
        benchmark_dates = _extract_trading_dates(
            panel=panel,
            symbol=benchmark_symbol,
            start_date=start_date,
            end_date=end_date,
        )
        if not benchmark_dates:
            raise ValueError(f"No benchmark sessions for {benchmark_symbol} in requested range.")
        print(
            f"[Backtest] benchmark sessions: {len(benchmark_dates)} ({benchmark_dates[0]} -> {benchmark_dates[-1]})",
            flush=True,
        )

        print("[Backtest] Step 3/6: preparing dynamic liquidity table ...", flush=True)
        liquidity_daily = _prepare_liquidity_daily_table(
            panel=panel,
            symbols=tradable_candidates,
            lookback_sessions=int(args.lookback_sessions),
            min_observations=int(args.min_observations),
            price_floor=float(args.price_floor),
            beta_full_observations=int(args.dynamic_beta_full_observations),
            start_date=start_date,
            end_date=end_date,
        )
        print(
            f"[Backtest] Step 3/6 done: daily_rows={len(liquidity_daily)}",
            flush=True,
        )

        ticker_map_cache_path, companyfacts_cache_dir, submissions_cache_dir, sec_cache_source = _resolve_sec_cache_paths(
            sec_cache_profile=str(args.sec_cache_profile),
            sec_cache_root=str(args.sec_cache_root) if args.sec_cache_root else None,
            ticker_map_cache_path=str(args.sec_ticker_map_cache_path) if args.sec_ticker_map_cache_path else None,
            companyfacts_cache_dir=str(args.sec_companyfacts_cache_dir) if args.sec_companyfacts_cache_dir else None,
            submissions_cache_dir=str(args.sec_submissions_cache_dir) if args.sec_submissions_cache_dir else None,
        )
        sec_cache_mode = str(args.sec_cache_mode).strip().lower()
        if sec_cache_mode == "auto":
            sec_cache_mode = "prefer" if str(args.sec_cache_profile) == "backtest" else "network"
        print(
            "[Backtest] SEC config: "
            f"profile={args.sec_cache_profile} source={sec_cache_source} mode={sec_cache_mode} "
            f"ticker_map={ticker_map_cache_path.as_posix()}",
            flush=True,
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

        print("[Backtest] Step 4/6: resolving SEC dynamic industry map once ...", flush=True)
        industry_map = _resolve_industry_map_for_symbols(
            symbols=tradable_candidates,
            sec_client=sec_client,
            industry_cache_output_path=output_root / "industry_map_dynamic.csv",
            submissions_workers=int(args.sec_submissions_workers),
        )
        print("[Backtest] Step 4/6 done.", flush=True)

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
            benchmark_symbol=benchmark_symbol,
            beta_lookback_sessions=int(args.beta_lookback_sessions),
            beta_min_observations=int(args.beta_min_observations),
            beta_shrinkage_target=float(args.beta_shrinkage_target),
            beta_shrinkage_strength=float(args.beta_shrinkage_strength),
            beta_clip_low=float(args.beta_clip_low) if args.beta_clip_low is not None else None,
            beta_clip_high=float(args.beta_clip_high) if args.beta_clip_high is not None else None,
            max_price_staleness_days=int(args.max_price_staleness_days),
            factor_weights=DEFAULT_FACTOR_WEIGHTS,
        )
        alpha_core._collect_bars_for_symbols = _bind_cached_bar_collector(alpha_core, bars_index)  # type: ignore[attr-defined]
        print("[Backtest] Step 4.5/6: building SEC snapshot timelines from cache ...", flush=True)
        sec_symbol_snapshots = _build_sec_symbol_snapshots(
            sec_client=sec_client,
            symbols=tradable_candidates,
            max_workers=int(args.sec_companyfacts_workers),
            max_cutoff_date=(end_date - timedelta(days=1)).isoformat(),
        )
        alpha_core._build_sec_features = _bind_cached_sec_feature_builder(alpha_core, sec_symbol_snapshots)  # type: ignore[attr-defined]
        print(
            f"[Backtest] Step 4.5/6 done: sec_snapshots={len(sec_symbol_snapshots)} symbols.",
            flush=True,
        )

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
        lot_manager = LotManager()

        cost_model = CostModel(
            execution_bps=float(args.execution_bps),
            sell_notional_fraction=float(args.sell_notional_fraction),
            sec_fee_rate=float(args.sec_fee_rate),
            taf_per_share=float(args.taf_per_share),
            taf_cap_per_trade=float(args.taf_cap_per_trade),
        )

        close_pivot = (
            panel.pivot_table(index="session_date", columns="symbol", values="close", aggfunc="last")
            .sort_index()
        )
        open_pivot = (
            panel.pivot_table(index="session_date", columns="symbol", values="open", aggfunc="last")
            .sort_index()
        )

        print("[Backtest] Step 5/6: running daily alpha -> decision -> open-execution loop ...", flush=True)
        daily_rows: list[dict[str, Any]] = []
        checkpoint_every = max(0, int(args.live_checkpoint_every_sessions))
        checkpoint_prefix = str(args.live_checkpoint_prefix or "live").strip() or "live"
        portfolio_states = [
            PortfolioState(
                tag=_scenario_equity_tag(scenario.key, initial_equity),
                scenario=str(scenario.key),
                initial_equity=float(initial_equity),
                cash=float(initial_equity),
                shares={},
                last_known_prices={},
            )
            for scenario in execution_scenarios
            for initial_equity in initial_equities
        ]
        strategy_tags = [state.tag for state in portfolio_states]
        benchmark_base_equity = float(initial_equities[0])
        spy_equity_raw = float(benchmark_base_equity)
        qqq_equity_raw = float(benchmark_base_equity)
        n_days = len(benchmark_dates)
        n_intervals = max(0, n_days - 1)
        if n_intervals <= 0:
            raise ValueError("Need at least two benchmark sessions for open->open backtest.")
        if performance_warmup_sessions >= n_intervals:
            raise ValueError(
                f"performance-warmup-sessions={performance_warmup_sessions} must be < intervals={n_intervals}."
            )
        _print_progress(label="Backtest sessions", current=0, total=n_intervals)
        for session_idx in range(n_intervals):
            session_date = benchmark_dates[session_idx]
            next_session_date = benchmark_dates[session_idx + 1]
            session_ts = pd.Timestamp(session_date)
            next_session_ts = pd.Timestamp(next_session_date)
            session_open = open_pivot.loc[session_ts] if session_ts in open_pivot.index else pd.Series(dtype=float)
            next_open = open_pivot.loc[next_session_ts] if next_session_ts in open_pivot.index else pd.Series(dtype=float)
            session_close = close_pivot.loc[session_ts] if session_ts in close_pivot.index else pd.Series(dtype=float)
            next_close = close_pivot.loc[next_session_ts] if next_session_ts in close_pivot.index else pd.Series(dtype=float)
            in_performance_window = bool(session_idx >= performance_warmup_sessions)

            symbols_today = _select_dynamic_pool_for_date(
                liquidity_daily=liquidity_daily,
                session_date=session_date,
                pool_size=int(args.pool_size),
            )
            session_day = _normalize_date(session_date)
            data_cutoff_day = session_day - timedelta(days=1)
            symbols_ready = _filter_symbols_by_recent_price(
                symbols=symbols_today,
                bars_index=bars_index,
                cutoff_date=data_cutoff_day.isoformat(),
                max_staleness_days=int(args.max_price_staleness_days),
            )
            if not symbols_today:
                diagnostics = {"status": "skip", "skip_reason": "dynamic_pool_empty", "target_turnover": 0.0}
                status = "skip"
            elif not symbols_ready:
                diagnostics = {
                    "status": "skip",
                    "skip_reason": "all_symbols_stale_after_filter",
                    "target_turnover": 0.0,
                }
                status = "skip"
            else:
                alpha_panel = alpha_core.build_for_date(as_of_date=session_date, symbols=symbols_ready)
                if args.save_alpha_panels:
                    alpha_path = alpha_output_root / f"alpha_core_panel_{session_date.replace('-', '')}.csv"
                    alpha_panel.to_csv(alpha_path, index=False)
                result = engine.decide(
                    alpha_frame=alpha_panel,
                    lot_manager=lot_manager,
                    session_idx=int(session_idx),
                    session_date=session_date,
                )
                diagnostics = dict(result.diagnostics or {})
                status = str(result.status)

            target_signed_weights = _signed_weights_from_lot(lot_manager)
            spy_ret = _open_to_open_return(
                symbol=benchmark_symbol,
                open_today=session_open,
                open_next=next_open,
                close_today=session_close,
                close_next=next_close,
            )
            qqq_ret = _open_to_open_return(
                symbol=compare_symbol,
                open_today=session_open,
                open_next=next_open,
                close_today=session_close,
                close_next=next_close,
            )
            if in_performance_window:
                spy_equity_raw *= 1.0 + spy_ret
                qqq_equity_raw *= 1.0 + qqq_ret

            tier_metrics: dict[str, dict[str, Any]] = {}
            for state in portfolio_states:
                scenario_cfg = next(
                    (scenario for scenario in execution_scenarios if scenario.key == state.scenario),
                    execution_scenarios[0],
                )
                equity_start, current_signed_notional, before_missing = _portfolio_equity_and_notional(
                    state=state,
                    primary_prices=session_open,
                    fallback_prices=session_close,
                )
                reference_prices = _build_reference_price_map_for_backtest(
                    symbols=set(target_signed_weights).union(current_signed_notional),
                    open_prices=session_open,
                    close_prices=session_close,
                    last_known_prices=state.last_known_prices,
                )
                effective_target_signed_weights, target_floor_diag = _project_short_targets_to_whole_shares(
                    signed_weights=target_signed_weights,
                    reference_prices=reference_prices,
                    account_equity=equity_start,
                    enabled=bool(scenario_cfg.floor_short_targets_to_whole_shares),
                )
                instructions, skipped_orders, order_diag = _build_backtest_order_instructions(
                    target_signed_weights=effective_target_signed_weights,
                    current_signed_notional=current_signed_notional,
                    account_equity=equity_start,
                    reference_prices=reference_prices,
                    assets_by_symbol=assets_by_symbol,
                    min_trade_notional=float(args.min_trade_notional),
                    qty_decimals=int(args.qty_decimals),
                    whole_shares_only=bool(args.whole_shares_only),
                    opening_shorts_whole_shares_only=bool(scenario_cfg.opening_shorts_whole_shares_only),
                    short_sales_whole_shares_only=bool(scenario_cfg.short_sales_whole_shares_only),
                    shorting_enabled=bool(args.shorting_enabled),
                )
                trade_cost_detail = _apply_backtest_orders(
                    state=state,
                    instructions=instructions,
                    cost_model=cost_model,
                )
                equity_next, _, after_missing = _portfolio_equity_and_notional(
                    state=state,
                    primary_prices=next_open,
                    fallback_prices=next_close,
                )
                gross_return = 0.0
                net_return = 0.0
                cost_rate = 0.0
                if equity_start > 0:
                    gross_return = float((equity_next + trade_cost_detail["total_cost"]) / equity_start - 1.0)
                    net_return = float(equity_next / equity_start - 1.0)
                    cost_rate = float(trade_cost_detail["total_cost"] / equity_start)
                turnover = float(0.5 * trade_cost_detail["trade_notional"] / max(equity_start, 1e-9))
                tier_metrics[state.tag] = {
                    "gross_return": float(gross_return),
                    "cost_rate": float(cost_rate),
                    "net_return": float(net_return),
                    "turnover": float(turnover),
                    "missing_price_symbols": int(before_missing + after_missing),
                    "cost_execution": float(trade_cost_detail["execution_cost"]),
                    "cost_sec_fee": float(trade_cost_detail["sec_fee_cost"]),
                    "cost_taf": float(trade_cost_detail["taf_cost"]),
                    "cost_total": float(trade_cost_detail["total_cost"]),
                    "equity_raw": float(equity_next),
                    "scenario": str(scenario_cfg.key),
                    "scenario_label": str(scenario_cfg.label),
                    "short_floor_names": int(order_diag["opening_short_names"]),
                    "short_floor_zeroed": int(order_diag["opening_short_zeroed"]),
                    "short_floor_lost_notional": float(order_diag["opening_short_lost_notional"]),
                    "short_sale_floor_names": int(order_diag["short_sale_names"]),
                    "short_sale_floor_zeroed": int(order_diag["short_sale_zeroed"]),
                    "short_sale_floor_lost_notional": float(order_diag["short_sale_lost_notional"]),
                    "target_short_floor_names": int(target_floor_diag["short_names"]),
                    "target_short_floor_zeroed": int(target_floor_diag["short_zeroed"]),
                    "target_short_floor_lost_notional": float(target_floor_diag["lost_notional"]),
                    "target_short_floor_desired_notional": float(target_floor_diag["desired_short_notional"]),
                    "target_short_floor_realized_notional": float(target_floor_diag["realized_short_notional"]),
                    "order_count": int(len(instructions)),
                    "skipped_order_count": int(len(skipped_orders)),
                }

            primary_metrics = tier_metrics[primary_equity_tag]
            primary_turnover = float(primary_metrics["turnover"])
            row: dict[str, Any] = {
                "session_idx": int(session_idx),
                "session_date": session_date,
                "next_session_date": next_session_date,
                "status": status,
                "in_performance_window": in_performance_window,
                "dynamic_symbols": int(len(symbols_today)),
                "symbols_after_stale_filter": int(len(symbols_ready)),
                "long_names": int(len(lot_manager.previous_weights()["long"])),
                "short_names": int(len(lot_manager.previous_weights()["short"])),
                "target_turnover": primary_turnover,
                "gross_return": float(primary_metrics["gross_return"]),
                "cost_rate": float(primary_metrics["cost_rate"]),
                "net_return": float(primary_metrics["net_return"]),
                "equity_raw": float(primary_metrics["equity_raw"]),
                "spy_return": float(spy_ret),
                "spy_equity_raw": float(spy_equity_raw),
                "qqq_return": float(qqq_ret),
                "qqq_equity_raw": float(qqq_equity_raw),
                "missing_price_symbols": int(primary_metrics["missing_price_symbols"]),
                "cost_execution": float(primary_metrics["cost_execution"]),
                "cost_sec_fee": float(primary_metrics["cost_sec_fee"]),
                "cost_taf": float(primary_metrics["cost_taf"]),
                "cost_total": float(primary_metrics["cost_total"]),
                "gross_exposure": float(lot_manager.snapshot(session_idx=session_idx).get("gross_exposure", np.nan)),
                "net_exposure": float(lot_manager.snapshot(session_idx=session_idx).get("net_exposure", np.nan)),
                "skip_reason": str(diagnostics.get("skip_reason", "") or diagnostics.get("carry_reason", "")),
            }
            for tag in strategy_tags:
                metrics = tier_metrics[tag]
                row[f"strategy_equity_raw_{tag}"] = float(metrics["equity_raw"])
                row[f"strategy_net_return_{tag}"] = float(metrics["net_return"])
                row[f"strategy_turnover_{tag}"] = float(metrics["turnover"])
                row[f"strategy_cost_total_{tag}"] = float(metrics["cost_total"])
                row[f"strategy_short_floor_names_{tag}"] = int(metrics["short_floor_names"])
                row[f"strategy_short_floor_zeroed_{tag}"] = int(metrics["short_floor_zeroed"])
                row[f"strategy_short_floor_lost_notional_{tag}"] = float(metrics["short_floor_lost_notional"])
                row[f"strategy_short_sale_floor_names_{tag}"] = int(metrics["short_sale_floor_names"])
                row[f"strategy_short_sale_floor_zeroed_{tag}"] = int(metrics["short_sale_floor_zeroed"])
                row[f"strategy_short_sale_floor_lost_notional_{tag}"] = float(metrics["short_sale_floor_lost_notional"])
                row[f"strategy_target_short_floor_names_{tag}"] = int(metrics["target_short_floor_names"])
                row[f"strategy_target_short_floor_zeroed_{tag}"] = int(metrics["target_short_floor_zeroed"])
                row[f"strategy_target_short_floor_lost_notional_{tag}"] = float(
                    metrics["target_short_floor_lost_notional"]
                )
                row[f"strategy_target_short_floor_desired_notional_{tag}"] = float(
                    metrics["target_short_floor_desired_notional"]
                )
                row[f"strategy_target_short_floor_realized_notional_{tag}"] = float(
                    metrics["target_short_floor_realized_notional"]
                )
                row[f"strategy_orders_{tag}"] = int(metrics["order_count"])
                row[f"strategy_skipped_orders_{tag}"] = int(metrics["skipped_order_count"])
            daily_rows.append(row)

            if checkpoint_every > 0 and ((session_idx + 1) % checkpoint_every == 0 or (session_idx + 1) == n_intervals):
                try:
                    live_daily = _build_reporting_daily_frame(
                        daily_rows=daily_rows,
                        strategy_tags=strategy_tags,
                        primary_strategy_tag=primary_equity_tag,
                    )
                    if live_daily.empty:
                        continue
                    _write_live_checkpoint(
                        daily=live_daily,
                        output_root=output_root,
                        benchmark_symbol=benchmark_symbol,
                        compare_symbol=compare_symbol,
                        prefix=checkpoint_prefix,
                        sessions_done=session_idx + 1,
                        sessions_total=n_intervals,
                        render_plots=bool(args.live_checkpoint_plots),
                    )
                except Exception as exc:  # best-effort live reporting should not break backtest
                    print(f"\n[Backtest] warning: failed to write live checkpoint: {exc}", flush=True)

            _print_progress(label="Backtest sessions", current=session_idx + 1, total=n_intervals)

        print("[Backtest] Step 5/6 done.", flush=True)

        daily = _build_reporting_daily_frame(
            daily_rows=daily_rows,
            strategy_tags=strategy_tags,
            primary_strategy_tag=primary_equity_tag,
        )
        if daily.empty:
            raise ValueError("Backtest produced no daily rows after warmup window.")

        summary = _build_backtest_summary(
            daily=daily,
            initial_equity=1.0,
            cost_model=cost_model,
            benchmark_symbol=benchmark_symbol,
            compare_symbol=compare_symbol,
        )
        summary["multi_tier"] = _build_multi_tier_summary(daily=daily, strategy_tags=strategy_tags)
        summary["warmup"] = {
            "sessions": int(performance_warmup_sessions),
            "report_start": str(daily["session_date"].iloc[0]) if len(daily) else "",
        }
        summary["execution_scenarios"] = {
            scenario.key: {
                "label": scenario.label,
                "opening_shorts_whole_shares_only": bool(scenario.opening_shorts_whole_shares_only),
                "short_sales_whole_shares_only": bool(scenario.short_sales_whole_shares_only),
                "floor_short_targets_to_whole_shares": bool(scenario.floor_short_targets_to_whole_shares),
            }
            for scenario in execution_scenarios
        }

        print("[Backtest] Step 6/6: writing outputs ...", flush=True)
        daily_path = output_root / "daily_backtest_results.csv"
        curve_path = output_root / "equity_curve_compare.csv"
        drawdown_path = output_root / "drawdown_curve_compare.csv"
        summary_path = output_root / "backtest_summary.json"
        ledger_path = output_root / "final_lot_ledger.json"
        curve_png_path = output_root / "equity_curve_compare.png"
        drawdown_png_path = output_root / "drawdown_curve_compare.png"

        daily.to_csv(daily_path, index=False)
        curve_columns = ["session_date"] + [f"strategy_equity_{tag}" for tag in strategy_tags] + ["spy_equity", "qqq_equity"]
        drawdown_columns = ["session_date"] + [f"strategy_drawdown_{tag}" for tag in strategy_tags] + ["spy_drawdown", "qqq_drawdown"]
        curve_existing = [column for column in curve_columns if column in daily.columns]
        drawdown_existing = [column for column in drawdown_columns if column in daily.columns]
        daily[curve_existing].to_csv(curve_path, index=False)
        daily[drawdown_existing].to_csv(drawdown_path, index=False)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        lot_manager.to_json(ledger_path)
        _plot_equity_curves(
            daily=daily,
            benchmark_symbol=benchmark_symbol,
            compare_symbol=compare_symbol,
            output_path=curve_png_path,
        )
        _plot_drawdown_curves(
            daily=daily,
            benchmark_symbol=benchmark_symbol,
            compare_symbol=compare_symbol,
            output_path=drawdown_png_path,
        )
        print("[Backtest] Step 6/6 done.", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted by user (Ctrl+C)."}, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    except (ValueError, FileNotFoundError, AlpacaRequestError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
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


def _equity_tag(initial_equity: float) -> str:
    value = float(initial_equity)
    rounded = int(round(value))
    if abs(value - rounded) <= 1e-9:
        return str(rounded)
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", "p")


def _scenario_equity_tag(scenario_key: str, initial_equity: float) -> str:
    return f"{str(scenario_key).strip()}_{_equity_tag(float(initial_equity))}"


def _resolve_execution_scenarios(
    *,
    scenario_text: str,
    opening_shorts_whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    floor_short_targets_to_whole_shares: bool,
) -> list[ExecutionScenario]:
    defaults = {
        "ideal_fractional": ExecutionScenario(
            key="ideal_fractional",
            label="Ideal fractional long/short",
            opening_shorts_whole_shares_only=False,
            short_sales_whole_shares_only=False,
            floor_short_targets_to_whole_shares=False,
        ),
        "opening_short_integer": ExecutionScenario(
            key="opening_short_integer",
            label="Opening shorts whole-share only",
            opening_shorts_whole_shares_only=True,
            short_sales_whole_shares_only=False,
            floor_short_targets_to_whole_shares=False,
        ),
        "short_sale_integer": ExecutionScenario(
            key="short_sale_integer",
            label="All short-sale sells whole-share only",
            opening_shorts_whole_shares_only=True,
            short_sales_whole_shares_only=True,
            floor_short_targets_to_whole_shares=False,
        ),
        "baseline_floor_targets": ExecutionScenario(
            key="baseline_floor_targets",
            label="Baseline: floor target short shares",
            opening_shorts_whole_shares_only=True,
            short_sales_whole_shares_only=True,
            floor_short_targets_to_whole_shares=True,
        ),
    }
    token = str(scenario_text or "").strip()
    if not token:
        return [
            ExecutionScenario(
                key="configured",
                label="Configured execution flags",
                opening_shorts_whole_shares_only=bool(opening_shorts_whole_shares_only),
                short_sales_whole_shares_only=bool(short_sales_whole_shares_only),
                floor_short_targets_to_whole_shares=bool(floor_short_targets_to_whole_shares),
            )
        ]

    out: list[ExecutionScenario] = []
    seen: set[str] = set()
    for raw in token.split(","):
        key = raw.strip()
        if not key:
            continue
        if key not in defaults:
            raise ValueError(
                f"Unknown execution scenario {key!r}. Supported: {', '.join(sorted(defaults))}"
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(defaults[key])
    if not out:
        raise ValueError("execution-scenarios is empty after parsing")
    return out


def _resolve_initial_equities(*, initial_equities_text: str, fallback_initial_equity: float) -> list[float]:
    token = str(initial_equities_text or "").strip()
    if not token:
        if float(fallback_initial_equity) <= 0:
            raise ValueError("initial-equity must be positive")
        return [float(fallback_initial_equity)]
    values: list[float] = []
    for part in token.split(","):
        piece = part.strip()
        if not piece:
            continue
        value = float(piece)
        if value <= 0:
            raise ValueError("all initial-equities must be positive")
        values.append(float(value))
    if not values:
        raise ValueError("initial-equities is empty after parsing")
    seen: set[str] = set()
    unique: list[float] = []
    for value in values:
        key = _equity_tag(value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


@dataclass
class BacktestOrderInstruction:
    symbol: str
    side: str
    qty: float
    reference_price: float
    current_notional: float
    target_notional: float
    delta_notional: float
    opening_short: bool


def _resolve_price_from_rows(
    *,
    symbol: str,
    primary_prices: pd.Series,
    fallback_prices: pd.Series,
    last_known_prices: Mapping[str, float] | None = None,
) -> float | None:
    normalized = str(symbol).strip().upper()
    px = _safe_float(primary_prices.get(normalized, np.nan))
    if px is not None and px > 0:
        return float(px)
    px = _safe_float(fallback_prices.get(normalized, np.nan))
    if px is not None and px > 0:
        return float(px)
    if last_known_prices:
        px = _safe_float(last_known_prices.get(normalized))
        if px is not None and px > 0:
            return float(px)
    return None


def _portfolio_equity_and_notional(
    *,
    state: PortfolioState,
    primary_prices: pd.Series,
    fallback_prices: pd.Series,
) -> tuple[float, dict[str, float], int]:
    signed_notional: dict[str, float] = {}
    missing = 0
    mtm = 0.0
    to_drop: list[str] = []
    for symbol, qty_raw in state.shares.items():
        qty = float(qty_raw)
        if abs(qty) <= 1e-12:
            to_drop.append(symbol)
            continue
        px = _resolve_price_from_rows(
            symbol=symbol,
            primary_prices=primary_prices,
            fallback_prices=fallback_prices,
            last_known_prices=state.last_known_prices,
        )
        if px is None:
            missing += 1
            continue
        state.last_known_prices[str(symbol).upper()] = float(px)
        notional = float(qty * px)
        signed_notional[str(symbol).upper()] = notional
        mtm += notional
    for symbol in to_drop:
        state.shares.pop(symbol, None)
    equity = float(state.cash + mtm)
    return equity, signed_notional, int(missing)


def _build_reference_price_map_for_backtest(
    *,
    symbols: set[str],
    open_prices: pd.Series,
    close_prices: pd.Series,
    last_known_prices: Mapping[str, float],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol_raw in sorted(symbols):
        symbol = str(symbol_raw).strip().upper()
        if not symbol:
            continue
        px = _resolve_price_from_rows(
            symbol=symbol,
            primary_prices=open_prices,
            fallback_prices=close_prices,
            last_known_prices=last_known_prices,
        )
        if px is None or px <= 0:
            continue
        out[symbol] = float(px)
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
        if abs(weight) <= 1e-12:
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


def _build_backtest_order_instructions(
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
) -> tuple[list[BacktestOrderInstruction], list[dict[str, Any]], dict[str, float]]:
    eps = 1e-10
    symbols = sorted(set(target_signed_weights) | set(current_signed_notional))
    instructions: list[BacktestOrderInstruction] = []
    skipped: list[dict[str, Any]] = []
    opening_short_names = 0
    opening_short_zeroed = 0
    opening_short_lost_notional = 0.0
    short_sale_names = 0
    short_sale_zeroed = 0
    short_sale_lost_notional = 0.0
    for symbol in symbols:
        target_notional = float(account_equity) * float(target_signed_weights.get(symbol, 0.0))
        current_notional = float(current_signed_notional.get(symbol, 0.0))
        delta_notional = float(target_notional - current_notional)
        if abs(delta_notional) < float(min_trade_notional):
            continue
        px = _safe_float(reference_prices.get(symbol))
        if px is None or px <= 0:
            skipped.append({"symbol": symbol, "reason": "missing_reference_price", "delta_notional": delta_notional})
            continue
        side = "buy" if delta_notional > 0 else "sell"
        opening_short = side == "sell" and target_notional < 0 and current_notional <= eps
        increasing_short = side == "sell" and target_notional < current_notional and target_notional < -eps
        short_sale = bool(opening_short or increasing_short)
        if opening_short:
            opening_short_names += 1
            if not shorting_enabled:
                skipped.append({"symbol": symbol, "reason": "account_shorting_disabled", "delta_notional": delta_notional})
                continue
            asset = assets_by_symbol.get(symbol, {})
            if not bool(asset.get("shortable", False)):
                skipped.append({"symbol": symbol, "reason": "asset_not_shortable", "delta_notional": delta_notional})
                continue

        if short_sale:
            short_sale_names += 1
        should_force_whole_share = bool(whole_shares_only) or (
            bool(opening_shorts_whole_shares_only) and bool(opening_short)
        ) or (
            bool(short_sales_whole_shares_only) and bool(short_sale)
        )
        raw_qty = abs(delta_notional) / float(px)
        qty = _quantize_qty(
            raw_qty,
            whole_shares_only=should_force_whole_share,
            decimals=qty_decimals,
        )
        if opening_short:
            realized = float(qty * px)
            opening_short_lost_notional += max(0.0, abs(delta_notional) - realized)
        if short_sale:
            realized = float(qty * px)
            short_sale_lost_notional += max(0.0, abs(delta_notional) - realized)
        if qty <= 0:
            if opening_short:
                opening_short_zeroed += 1
            if short_sale:
                short_sale_zeroed += 1
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
            BacktestOrderInstruction(
                symbol=str(symbol).upper(),
                side=str(side),
                qty=float(qty),
                reference_price=float(px),
                current_notional=float(current_notional),
                target_notional=float(target_notional),
                delta_notional=float(delta_notional),
                opening_short=bool(opening_short),
            )
        )
    instructions.sort(key=lambda item: abs(item.delta_notional), reverse=True)
    return instructions, skipped, {
        "opening_short_names": float(opening_short_names),
        "opening_short_zeroed": float(opening_short_zeroed),
        "opening_short_lost_notional": float(opening_short_lost_notional),
        "short_sale_names": float(short_sale_names),
        "short_sale_zeroed": float(short_sale_zeroed),
        "short_sale_lost_notional": float(short_sale_lost_notional),
    }


def _apply_backtest_orders(
    *,
    state: PortfolioState,
    instructions: Sequence[BacktestOrderInstruction],
    cost_model: CostModel,
) -> dict[str, float]:
    execution_cost = 0.0
    sec_fee_cost = 0.0
    taf_cost = 0.0
    trade_notional = 0.0
    sell_notional = 0.0
    for item in instructions:
        symbol = str(item.symbol).upper()
        qty = max(0.0, float(item.qty))
        if qty <= 0:
            continue
        px = max(1e-9, float(item.reference_price))
        notional = qty * px
        trade_notional += abs(notional)
        execution_cost += abs(notional) * (float(cost_model.execution_bps) / 10_000.0)
        state.last_known_prices[symbol] = float(px)

        if str(item.side).lower() == "buy":
            state.cash -= notional
            state.shares[symbol] = float(state.shares.get(symbol, 0.0) + qty)
        else:
            state.cash += notional
            state.shares[symbol] = float(state.shares.get(symbol, 0.0) - qty)
            sell_notional += abs(notional)
            sec_fee_cost += abs(notional) * max(0.0, float(cost_model.sec_fee_rate))
            taf_cost += min(
                qty * max(0.0, float(cost_model.taf_per_share)),
                max(0.0, float(cost_model.taf_cap_per_trade)),
            )

    total_cost = execution_cost + sec_fee_cost + taf_cost
    state.cash -= total_cost
    for symbol in list(state.shares.keys()):
        if abs(float(state.shares.get(symbol, 0.0))) <= 1e-12:
            state.shares.pop(symbol, None)

    return {
        "execution_cost": float(execution_cost),
        "sec_fee_cost": float(sec_fee_cost),
        "taf_cost": float(taf_cost),
        "total_cost": float(total_cost),
        "trade_notional": float(trade_notional),
        "sell_notional": float(sell_notional),
    }


def _open_to_open_return(
    *,
    symbol: str,
    open_today: pd.Series,
    open_next: pd.Series,
    close_today: pd.Series,
    close_next: pd.Series,
) -> float:
    normalized = str(symbol).strip().upper()
    px0 = _resolve_price_from_rows(
        symbol=normalized,
        primary_prices=open_today,
        fallback_prices=close_today,
    )
    px1 = _resolve_price_from_rows(
        symbol=normalized,
        primary_prices=open_next,
        fallback_prices=close_next,
    )
    if px0 is None or px1 is None or px0 <= 0 or px1 <= 0:
        return 0.0
    return float(px1 / px0 - 1.0)


def _apply_short_floor_to_signed_weights(
    *,
    signed_weights: Mapping[str, float],
    session_close_row: pd.Series,
    equity: float,
) -> tuple[dict[str, float], dict[str, float]]:
    effective: dict[str, float] = {}
    short_names = 0
    short_zeroed = 0
    lost_notional = 0.0
    safe_equity = max(float(equity), 1e-9)
    for symbol, raw_weight in signed_weights.items():
        weight = float(raw_weight)
        if abs(weight) <= 1e-12:
            continue
        normalized_symbol = str(symbol).strip().upper()
        if weight >= 0:
            effective[normalized_symbol] = float(weight)
            continue

        short_names += 1
        px_raw = session_close_row.get(normalized_symbol, np.nan)
        px = _safe_float(px_raw)
        if px is None or px <= 0:
            effective[normalized_symbol] = float(weight)
            continue
        target_notional = abs(weight) * safe_equity
        raw_shares = target_notional / px
        floored_shares = float(np.floor(raw_shares + 1e-12))
        realized_notional = floored_shares * px
        lost_notional += max(0.0, target_notional - realized_notional)
        if floored_shares <= 0:
            short_zeroed += 1
            continue
        effective_weight = -(realized_notional / safe_equity)
        effective[normalized_symbol] = float(effective_weight)

    return effective, {
        "short_names": float(short_names),
        "short_zeroed": float(short_zeroed),
        "lost_notional": float(lost_notional),
    }


def _weighted_gross_return(
    *,
    signed_weights: Mapping[str, float],
    session_ret_row: pd.Series,
) -> tuple[float, int]:
    gross_return = 0.0
    missing_price_symbols = 0
    for symbol, weight in signed_weights.items():
        value = session_ret_row.get(symbol, np.nan)
        if pd.isna(value):
            missing_price_symbols += 1
            continue
        gross_return += float(weight) * float(value)
    return float(gross_return), int(missing_price_symbols)


def _weight_turnover(*, previous_weights: Mapping[str, float], next_weights: Mapping[str, float]) -> float:
    keys = set(previous_weights.keys()).union(next_weights.keys())
    if not keys:
        return 0.0
    total_abs_change = 0.0
    for symbol in keys:
        before = float(previous_weights.get(symbol, 0.0))
        after = float(next_weights.get(symbol, 0.0))
        total_abs_change += abs(after - before)
    return float(0.5 * total_abs_change)


def _changed_weight_names(
    *,
    previous_weights: Mapping[str, float],
    next_weights: Mapping[str, float],
    tolerance: float = 1e-10,
) -> int:
    keys = set(previous_weights.keys()).union(next_weights.keys())
    changed = 0
    for symbol in keys:
        before = float(previous_weights.get(symbol, 0.0))
        after = float(next_weights.get(symbol, 0.0))
        if abs(after - before) > float(tolerance):
            changed += 1
    return int(changed)


def _build_reporting_daily_frame(
    *,
    daily_rows: Sequence[Mapping[str, Any]],
    strategy_tags: Sequence[str],
    primary_strategy_tag: str,
) -> pd.DataFrame:
    daily_all = pd.DataFrame(list(daily_rows))
    if daily_all.empty:
        return daily_all
    if "in_performance_window" not in daily_all.columns:
        return pd.DataFrame()

    daily = daily_all[daily_all["in_performance_window"].astype(bool)].copy()
    daily = daily.reset_index(drop=True)
    if daily.empty:
        return daily

    for tag in strategy_tags:
        raw_col = f"strategy_equity_raw_{tag}"
        norm_col = f"strategy_equity_{tag}"
        drawdown_col = f"strategy_drawdown_{tag}"
        if raw_col not in daily.columns:
            continue
        first_value = float(pd.to_numeric(pd.Series([daily[raw_col].iloc[0]]), errors="coerce").iloc[0])
        if not np.isfinite(first_value) or abs(first_value) <= 1e-12:
            first_value = 1.0
        daily[norm_col] = pd.to_numeric(daily[raw_col], errors="coerce") / float(first_value)
        curve = pd.to_numeric(daily[norm_col], errors="coerce")
        peak = curve.cummax()
        daily[drawdown_col] = curve / peak - 1.0

    spy_base = float(pd.to_numeric(pd.Series([daily["spy_equity_raw"].iloc[0]]), errors="coerce").iloc[0])
    qqq_base = float(pd.to_numeric(pd.Series([daily["qqq_equity_raw"].iloc[0]]), errors="coerce").iloc[0])
    if not np.isfinite(spy_base) or abs(spy_base) <= 1e-12:
        spy_base = 1.0
    if not np.isfinite(qqq_base) or abs(qqq_base) <= 1e-12:
        qqq_base = 1.0
    daily["spy_equity"] = pd.to_numeric(daily["spy_equity_raw"], errors="coerce") / float(spy_base)
    daily["qqq_equity"] = pd.to_numeric(daily["qqq_equity_raw"], errors="coerce") / float(qqq_base)
    daily["spy_peak"] = daily["spy_equity"].cummax()
    daily["spy_drawdown"] = daily["spy_equity"] / daily["spy_peak"] - 1.0
    daily["qqq_peak"] = daily["qqq_equity"].cummax()
    daily["qqq_drawdown"] = daily["qqq_equity"] / daily["qqq_peak"] - 1.0

    primary_equity_col = f"strategy_equity_{primary_strategy_tag}"
    primary_drawdown_col = f"strategy_drawdown_{primary_strategy_tag}"
    if primary_equity_col not in daily.columns:
        raise ValueError(f"Missing primary strategy equity column: {primary_equity_col}")
    daily["equity"] = pd.to_numeric(daily[primary_equity_col], errors="coerce")
    daily["drawdown"] = pd.to_numeric(daily[primary_drawdown_col], errors="coerce")
    return daily


def _build_multi_tier_summary(*, daily: pd.DataFrame, strategy_tags: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if daily.empty:
        return out
    n_days = len(daily)
    for tag in strategy_tags:
        equity_col = f"strategy_equity_{tag}"
        dd_col = f"strategy_drawdown_{tag}"
        ret_col = f"strategy_net_return_{tag}"
        if equity_col not in daily.columns:
            continue
        final_norm = float(pd.to_numeric(pd.Series([daily[equity_col].iloc[-1]]), errors="coerce").iloc[0])
        total_return = final_norm - 1.0
        max_dd = float(pd.to_numeric(daily.get(dd_col, pd.Series([0.0])), errors="coerce").min())
        returns = pd.to_numeric(daily.get(ret_col, pd.Series(dtype=float)), errors="coerce")
        out[tag] = {
            "final_norm_equity": final_norm,
            "total_return": float(total_return),
            "annualized_return": _annualized_return(total_return, n_days),
            "max_drawdown": max_dd,
            "annualized_volatility": _annualized_volatility(returns),
            "sharpe_0rf": _sharpe_ratio(returns),
        }
    return out


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _collect_bars_parallel(
    *,
    client: AlpacaHttpClient,
    symbols: Sequence[str],
    start: str,
    end: str,
    chunk_size: int,
    workers: int,
    feed: str,
    adjustment: str,
) -> list[dict[str, Any]]:
    if not symbols:
        return []
    rows: list[dict[str, Any]] = []
    chunks = [list(chunk) for chunk in _chunks(list(symbols), int(chunk_size))]
    total_chunks = max(1, len(chunks))
    worker_count = max(1, min(int(workers), total_chunks))
    _print_progress(label="Alpaca prefetch bars", current=0, total=total_chunks)
    if worker_count == 1:
        for idx, chunk in enumerate(chunks, start=1):
            bars = client.get_stock_bars(
                symbols=chunk,
                start=start,
                end=end,
                timeframe="1Day",
                adjustment=adjustment,
                feed=feed,
                limit=10000,
            )
            rows.extend(bar for bar in bars if isinstance(bar, Mapping))
            _print_progress(label="Alpaca prefetch bars", current=idx, total=total_chunks)
        return rows

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    interrupted = False
    future_to_chunk: dict[concurrent.futures.Future[list[dict[str, Any]]], list[str]] = {}
    try:
        future_to_chunk = {
            executor.submit(
                client.get_stock_bars,
                symbols=chunk,
                start=start,
                end=end,
                timeframe="1Day",
                adjustment=adjustment,
                feed=feed,
                limit=10000,
            ): chunk
            for chunk in chunks
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_to_chunk):
            bars = future.result()
            rows.extend(bar for bar in bars if isinstance(bar, Mapping))
            completed += 1
            _print_progress(label="Alpaca prefetch bars", current=completed, total=total_chunks)
    except KeyboardInterrupt:
        interrupted = True
        for future in future_to_chunk:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        print("\n[Backtest] interrupted by Ctrl+C during Alpaca prefetch.", flush=True)
        raise
    finally:
        if not interrupted:
            executor.shutdown(wait=True)
    return rows


def _build_bars_index(bars: Sequence[Mapping[str, Any]]) -> dict[str, tuple[list[str], list[dict[str, Any]]]]:
    by_symbol_date: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in bars:
        symbol = str(raw.get("symbol") or "").strip().upper()
        ts = str(raw.get("t") or raw.get("timestamp") or "")
        if not symbol or len(ts) < 10:
            continue
        session_date = ts[:10]
        close = _safe_float(raw.get("c"))
        if close is None:
            close = _safe_float(raw.get("close"))
        if close is None or close <= 0:
            continue
        volume = _safe_float(raw.get("v"))
        if volume is None:
            volume = _safe_float(raw.get("volume"))
        vwap = _safe_float(raw.get("vw"))
        if vwap is None:
            vwap = _safe_float(raw.get("vwap"))
        by_symbol_date.setdefault(symbol, {})[session_date] = {
            "symbol": symbol,
            "t": ts,
            "c": float(close),
            "v": float(volume) if volume is not None else np.nan,
            "vw": float(vwap) if vwap is not None else np.nan,
        }

    out: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    for symbol, date_map in by_symbol_date.items():
        ordered_dates = sorted(date_map.keys())
        ordered_bars = [date_map[token] for token in ordered_dates]
        out[symbol] = (ordered_dates, ordered_bars)
    return out


def _bind_cached_bar_collector(alpha_core: AlphaCore, bars_index: Mapping[str, tuple[list[str], list[dict[str, Any]]]]):
    def _collect_cached(self: AlphaCore, *, symbols: Sequence[str], start: str, end: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start_token = str(start)[:10]
        end_token = str(end)[:10]
        for symbol in symbols:
            normalized = str(symbol).strip().upper()
            entry = bars_index.get(normalized)
            if entry is None:
                continue
            dates, bars = entry
            lo = bisect.bisect_left(dates, start_token)
            hi = bisect.bisect_right(dates, end_token)
            if hi > lo:
                rows.extend(bars[lo:hi])
        return rows

    return _collect_cached.__get__(alpha_core, AlphaCore)


def _bars_to_panel_for_backtest(bars: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for bar in bars:
        symbol = str(bar.get("symbol") or "").strip().upper()
        timestamp = str(bar.get("t") or bar.get("timestamp") or "")
        if not symbol or len(timestamp) < 10:
            continue
        session_date = pd.Timestamp(timestamp[:10])
        close = _safe_float(bar.get("c"))
        if close is None:
            close = _safe_float(bar.get("close"))
        open_px = _safe_float(bar.get("o"))
        if open_px is None:
            open_px = _safe_float(bar.get("open"))
        if open_px is None:
            open_px = close
        volume = _safe_float(bar.get("v"))
        if volume is None:
            volume = _safe_float(bar.get("volume"))
        vwap = _safe_float(bar.get("vw"))
        if vwap is None:
            vwap = _safe_float(bar.get("vwap"))
        if close is None or open_px is None or volume is None:
            continue
        reference_price = close if close is not None else vwap
        if reference_price is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "session_date": session_date,
                "open": float(open_px),
                "close": float(close),
                "dollar_volume": float(reference_price) * float(volume),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["symbol", "session_date", "open", "close", "dollar_volume"])
    frame = pd.DataFrame(rows)
    frame = frame.sort_values(["symbol", "session_date"]).drop_duplicates(
        ["symbol", "session_date"], keep="last"
    )
    return frame.reset_index(drop=True)


def _extract_trading_dates(
    *,
    panel: pd.DataFrame,
    symbol: str,
    start_date: date,
    end_date: date,
) -> list[str]:
    benchmark = panel[panel["symbol"].eq(str(symbol).strip().upper())].copy()
    if benchmark.empty:
        return []
    mask = benchmark["session_date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    ordered = benchmark.loc[mask, "session_date"].drop_duplicates().sort_values()
    return [ts.date().isoformat() for ts in ordered.tolist()]


def _prepare_liquidity_daily_table(
    *,
    panel: pd.DataFrame,
    symbols: Sequence[str],
    lookback_sessions: int,
    min_observations: int,
    price_floor: float,
    beta_full_observations: int,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    stock = panel[panel["symbol"].isin(set(str(s).strip().upper() for s in symbols))].copy()
    if stock.empty:
        return pd.DataFrame(
            columns=[
                "session_date",
                "symbol",
                "trailing_median_dollar_volume_20",
                "lagged_close",
                "prior_bar_count",
                "liquidity_eligible",
            ]
        )
    stock = stock.sort_values(["symbol", "session_date"]).reset_index(drop=True)
    grouped = stock.groupby("symbol", sort=False)
    lagged_close = grouped["close"].shift(1)
    shifted_dv = grouped["dollar_volume"].shift(1)
    stock["lagged_close"] = lagged_close
    stock["trailing_obs"] = (
        lagged_close.groupby(stock["symbol"])
        .rolling(
            int(lookback_sessions),
            min_periods=1,
        )
        .count()
        .reset_index(level=0, drop=True)
    )
    stock["trailing_median_dollar_volume_20"] = (
        shifted_dv.groupby(stock["symbol"])
        .rolling(
            int(lookback_sessions),
            min_periods=int(min_observations),
        )
        .median()
        .reset_index(level=0, drop=True)
    )
    stock["prior_bar_count"] = grouped.cumcount()
    stock["liquidity_eligible"] = (
        stock["trailing_obs"].ge(int(min_observations))
        & stock["prior_bar_count"].ge(int(beta_full_observations))
        & stock["lagged_close"].ge(float(price_floor))
        & stock["trailing_median_dollar_volume_20"].gt(0.0)
        & stock["trailing_median_dollar_volume_20"].notna()
    )
    mask = stock["session_date"].between(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return stock.loc[
        mask,
        [
            "session_date",
            "symbol",
            "trailing_median_dollar_volume_20",
            "lagged_close",
            "prior_bar_count",
            "liquidity_eligible",
        ],
    ].copy()


def _select_dynamic_pool_for_date(*, liquidity_daily: pd.DataFrame, session_date: str, pool_size: int) -> list[str]:
    if liquidity_daily.empty:
        return []
    day = liquidity_daily[liquidity_daily["session_date"].eq(pd.Timestamp(session_date))]
    if day.empty:
        return []
    eligible = day[day["liquidity_eligible"]].copy()
    if eligible.empty:
        return []
    ranked = eligible.sort_values(
        ["trailing_median_dollar_volume_20", "symbol"],
        ascending=[False, True],
    )
    return ranked.head(int(pool_size))["symbol"].astype(str).tolist()


def _filter_symbols_by_recent_price(
    *,
    symbols: Sequence[str],
    bars_index: Mapping[str, tuple[list[str], list[dict[str, Any]]]],
    cutoff_date: str,
    max_staleness_days: int,
) -> list[str]:
    cutoff = _normalize_date(cutoff_date)
    out: list[str] = []
    for symbol in symbols:
        normalized = str(symbol).strip().upper()
        entry = bars_index.get(normalized)
        if entry is None:
            continue
        dates, _ = entry
        idx = bisect.bisect_right(dates, cutoff.isoformat()) - 1
        if idx < 0:
            continue
        latest = _normalize_date(dates[idx])
        staleness = (cutoff - latest).days
        if staleness <= int(max_staleness_days):
            out.append(normalized)
    return out


def _signed_weights_from_lot(lot_manager: LotManager) -> dict[str, float]:
    weights = lot_manager.previous_weights()
    out: dict[str, float] = {}
    for symbol, value in weights["long"].items():
        out[str(symbol)] = float(out.get(str(symbol), 0.0) + float(value))
    for symbol, value in weights["short"].items():
        out[str(symbol)] = float(out.get(str(symbol), 0.0) - float(value))
    return out


def _estimate_cost_rate(
    *,
    cost_model: CostModel,
    equity: float,
    turnover: float,
    session_ret_row: pd.Series,
    session_close_row: pd.Series,
    estimated_trade_names: int,
) -> tuple[float, dict[str, float]]:
    safe_equity = max(float(equity), 1e-9)
    safe_turnover = max(0.0, float(turnover))
    trade_notional = safe_equity * safe_turnover
    execution_cost = trade_notional * (float(cost_model.execution_bps) / 10_000.0)

    sell_notional = trade_notional * float(np.clip(cost_model.sell_notional_fraction, 0.0, 1.0))
    sec_fee_cost = sell_notional * max(0.0, float(cost_model.sec_fee_rate))

    avg_px = 50.0
    if isinstance(session_close_row, pd.Series) and len(session_close_row) > 0:
        px = pd.to_numeric(session_close_row, errors="coerce")
        px = px[np.isfinite(px) & px.gt(0)]
        if len(px) > 0:
            avg_px = float(np.clip(np.nanmedian(px.to_numpy(dtype=float)), 5.0, 500.0))
    shares_sold = sell_notional / max(avg_px, 1e-6)
    taf_uncapped = shares_sold * max(0.0, float(cost_model.taf_per_share))
    taf_cap_total = max(1, int(estimated_trade_names)) * max(0.0, float(cost_model.taf_cap_per_trade))
    taf_cost = min(taf_uncapped, taf_cap_total)

    total_cost = execution_cost + sec_fee_cost + taf_cost
    return float(total_cost / safe_equity), {
        "execution_cost": float(execution_cost),
        "sec_fee_cost": float(sec_fee_cost),
        "taf_cost": float(taf_cost),
        "total_cost": float(total_cost),
    }


def _annualized_return(total_return: float, n_days: int) -> float:
    if n_days <= 0:
        return 0.0
    years = n_days / 252.0
    if years <= 0:
        return 0.0
    return float((1.0 + total_return) ** (1.0 / years) - 1.0)


def _annualized_volatility(daily_returns: pd.Series) -> float:
    values = pd.to_numeric(daily_returns, errors="coerce").dropna()
    if len(values) <= 1:
        return 0.0
    return float(values.std(ddof=1) * np.sqrt(252.0))


def _sharpe_ratio(daily_returns: pd.Series) -> float:
    vol = _annualized_volatility(daily_returns)
    if vol <= 1e-12:
        return 0.0
    ann = float(pd.to_numeric(daily_returns, errors="coerce").dropna().mean() * 252.0)
    return float(ann / vol)


def _write_live_checkpoint(
    *,
    daily: pd.DataFrame,
    output_root: Path,
    benchmark_symbol: str,
    compare_symbol: str,
    prefix: str,
    sessions_done: int,
    sessions_total: int,
    render_plots: bool,
) -> None:
    if daily.empty:
        return
    safe_prefix = str(prefix).strip() or "live"
    daily_path = output_root / f"{safe_prefix}_daily_backtest_results.csv"
    curve_path = output_root / f"{safe_prefix}_equity_curve_compare.csv"
    drawdown_path = output_root / f"{safe_prefix}_drawdown_curve_compare.csv"
    status_path = output_root / f"{safe_prefix}_status.json"
    curve_png_path = output_root / f"{safe_prefix}_equity_curve_compare.png"
    drawdown_png_path = output_root / f"{safe_prefix}_drawdown_curve_compare.png"

    daily.to_csv(daily_path, index=False)
    curve_columns = ["session_date"] + _strategy_equity_curve_columns(daily) + ["spy_equity", "qqq_equity"]
    drawdown_columns = ["session_date"] + _strategy_drawdown_curve_columns(daily) + ["spy_drawdown", "qqq_drawdown"]
    curve_existing = [column for column in curve_columns if column in daily.columns]
    drawdown_existing = [column for column in drawdown_columns if column in daily.columns]
    daily[curve_existing].to_csv(curve_path, index=False)
    daily[drawdown_existing].to_csv(drawdown_path, index=False)

    if render_plots:
        _plot_equity_curves(
            daily=daily,
            benchmark_symbol=benchmark_symbol,
            compare_symbol=compare_symbol,
            output_path=curve_png_path,
        )
        _plot_drawdown_curves(
            daily=daily,
            benchmark_symbol=benchmark_symbol,
            compare_symbol=compare_symbol,
            output_path=drawdown_png_path,
        )

    latest = daily.iloc[-1]
    progress = 100.0 * float(sessions_done) / float(max(1, sessions_total))
    status_payload = {
        "ok": True,
        "prefix": safe_prefix,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "sessions_done": int(sessions_done),
        "sessions_total": int(sessions_total),
        "progress_pct": float(progress),
        "session_date": str(latest.get("session_date", "")),
        "equity": float(pd.to_numeric(pd.Series([latest.get("equity")]), errors="coerce").iloc[0]),
        "drawdown": float(pd.to_numeric(pd.Series([latest.get("drawdown")]), errors="coerce").iloc[0]),
        "spy_equity": float(pd.to_numeric(pd.Series([latest.get("spy_equity")]), errors="coerce").iloc[0]),
        "spy_drawdown": float(pd.to_numeric(pd.Series([latest.get("spy_drawdown")]), errors="coerce").iloc[0]),
        "qqq_equity": float(pd.to_numeric(pd.Series([latest.get("qqq_equity")]), errors="coerce").iloc[0]),
        "qqq_drawdown": float(pd.to_numeric(pd.Series([latest.get("qqq_drawdown")]), errors="coerce").iloc[0]),
    }
    status_path.write_text(json.dumps(status_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_backtest_summary(
    *,
    daily: pd.DataFrame,
    initial_equity: float,
    cost_model: CostModel,
    benchmark_symbol: str,
    compare_symbol: str,
) -> dict[str, Any]:
    final_equity = float(daily["equity"].iloc[-1])
    final_spy_equity = float(daily["spy_equity"].iloc[-1])
    final_qqq_equity = float(daily["qqq_equity"].iloc[-1])
    total_return = final_equity / float(initial_equity) - 1.0
    spy_return = final_spy_equity / float(initial_equity) - 1.0
    qqq_return = final_qqq_equity / float(initial_equity) - 1.0

    max_dd = float(daily["drawdown"].min()) if len(daily) else 0.0
    spy_max_dd = float(daily["spy_drawdown"].min()) if len(daily) else 0.0
    qqq_max_dd = float(daily["qqq_drawdown"].min()) if len(daily) else 0.0

    return {
        "ok": True,
        "sessions": int(len(daily)),
        "initial_equity": float(initial_equity),
        "final_equity": final_equity,
        "final_benchmark_equity": {
            benchmark_symbol: final_spy_equity,
            compare_symbol: final_qqq_equity,
        },
        "returns": {
            "strategy_total_return": float(total_return),
            f"{benchmark_symbol}_total_return": float(spy_return),
            f"{compare_symbol}_total_return": float(qqq_return),
            "strategy_annualized_return": _annualized_return(total_return, len(daily)),
            f"{benchmark_symbol}_annualized_return": _annualized_return(spy_return, len(daily)),
            f"{compare_symbol}_annualized_return": _annualized_return(qqq_return, len(daily)),
        },
        "risk": {
            "strategy_max_drawdown": max_dd,
            f"{benchmark_symbol}_max_drawdown": spy_max_dd,
            f"{compare_symbol}_max_drawdown": qqq_max_dd,
            "strategy_annualized_volatility": _annualized_volatility(daily["net_return"]),
            f"{benchmark_symbol}_annualized_volatility": _annualized_volatility(daily["spy_return"]),
            f"{compare_symbol}_annualized_volatility": _annualized_volatility(daily["qqq_return"]),
            "strategy_sharpe_0rf": _sharpe_ratio(daily["net_return"]),
            f"{benchmark_symbol}_sharpe_0rf": _sharpe_ratio(daily["spy_return"]),
            f"{compare_symbol}_sharpe_0rf": _sharpe_ratio(daily["qqq_return"]),
        },
        "trading": {
            "avg_turnover": float(pd.to_numeric(daily["target_turnover"], errors="coerce").fillna(0.0).mean()),
            "avg_dynamic_symbols": float(pd.to_numeric(daily["dynamic_symbols"], errors="coerce").fillna(0.0).mean()),
            "total_cost": float(pd.to_numeric(daily["cost_total"], errors="coerce").fillna(0.0).sum()),
            "cost_model": {
                "execution_bps": float(cost_model.execution_bps),
                "sell_notional_fraction": float(cost_model.sell_notional_fraction),
                "sec_fee_rate": float(cost_model.sec_fee_rate),
                "taf_per_share": float(cost_model.taf_per_share),
                "taf_cap_per_trade": float(cost_model.taf_cap_per_trade),
            },
        },
        "dates": {
            "start": str(daily["session_date"].iloc[0]) if len(daily) else "",
            "end": str(daily["session_date"].iloc[-1]) if len(daily) else "",
        },
    }


def _strategy_column_sort_key(column: str, prefix: str) -> tuple[int, float | str]:
    tag = column[len(prefix) :]
    try:
        return (0, float(tag.replace("p", ".")))
    except ValueError:
        return (1, tag)


def _strategy_equity_curve_columns(daily: pd.DataFrame) -> list[str]:
    prefix = "strategy_equity_"
    columns = [
        column
        for column in daily.columns
        if column.startswith(prefix) and not column.startswith("strategy_equity_raw_")
    ]
    return sorted(columns, key=lambda name: _strategy_column_sort_key(name, prefix))


def _strategy_drawdown_curve_columns(daily: pd.DataFrame) -> list[str]:
    prefix = "strategy_drawdown_"
    columns = [column for column in daily.columns if column.startswith(prefix)]
    return sorted(columns, key=lambda name: _strategy_column_sort_key(name, prefix))


def _strategy_label_from_tag(tag: str) -> str:
    try:
        value = float(str(tag).replace("p", "."))
    except ValueError:
        return f"Strategy {tag}"
    if value >= 1_000_000:
        return f"Strategy ${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"Strategy ${value / 1_000:.0f}K"
    return f"Strategy ${value:.0f}"


def _plot_equity_curves(
    *,
    daily: pd.DataFrame,
    benchmark_symbol: str,
    compare_symbol: str,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    x = pd.to_datetime(daily["session_date"], errors="coerce")
    strategy_columns = _strategy_equity_curve_columns(daily)
    if strategy_columns:
        for column in strategy_columns:
            tag = column.replace("strategy_equity_", "", 1)
            ax.plot(x, daily[column], label=_strategy_label_from_tag(tag), linewidth=1.5)
    else:
        ax.plot(x, daily["equity"], label="Strategy", linewidth=1.8)
    ax.plot(x, daily["spy_equity"], label=benchmark_symbol, linewidth=1.4, alpha=0.9)
    ax.plot(x, daily["qqq_equity"], label=compare_symbol, linewidth=1.4, alpha=0.9)
    ax.set_title("Equity Curve Comparison")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _plot_drawdown_curves(
    *,
    daily: pd.DataFrame,
    benchmark_symbol: str,
    compare_symbol: str,
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 6))
    x = pd.to_datetime(daily["session_date"], errors="coerce")
    drawdown_columns = _strategy_drawdown_curve_columns(daily)
    if drawdown_columns:
        for column in drawdown_columns:
            tag = column.replace("strategy_drawdown_", "", 1)
            ax.plot(x, daily[column], label=_strategy_label_from_tag(tag), linewidth=1.5)
    else:
        ax.plot(x, daily["drawdown"], label="Strategy", linewidth=1.8)
    ax.plot(x, daily["spy_drawdown"], label=benchmark_symbol, linewidth=1.4, alpha=0.9)
    ax.plot(x, daily["qqq_drawdown"], label=compare_symbol, linewidth=1.4, alpha=0.9)
    ax.set_title("Drawdown Comparison")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _fetch_companyfacts_payloads_parallel(
    *,
    sec_client: SecApiClient,
    ciks: Sequence[str],
    max_workers: int,
) -> dict[str, tuple[Mapping[str, Any] | None, str, str]]:
    normalized_ciks = sorted(
        {
            normalized
            for normalized in (_normalize_cik(cik) for cik in ciks)
            if normalized and normalized != "0000000000"
        }
    )
    if not normalized_ciks:
        return {}

    total = len(normalized_ciks)
    worker_count = max(1, min(int(max_workers), total))
    results: dict[str, tuple[Mapping[str, Any] | None, str, str]] = {}
    _print_progress(label="SEC companyfacts timelines", current=0, total=total)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    interrupted = False
    future_to_cik: dict[concurrent.futures.Future[tuple[Mapping[str, Any] | None, str, str]], str] = {}
    try:
        future_to_cik = {executor.submit(sec_client.get_companyfacts_payload, cik): cik for cik in normalized_ciks}
        completed = 0
        for future in concurrent.futures.as_completed(future_to_cik):
            cik = future_to_cik[future]
            try:
                payload, source, error = future.result()
            except Exception as exc:  # defensive
                payload, source, error = None, "failed", f"{type(exc).__name__}: {exc}"
            if not isinstance(payload, Mapping):
                payload = None
            results[cik] = (payload, str(source or ""), str(error or ""))
            completed += 1
            _print_progress(label="SEC companyfacts timelines", current=completed, total=total)
    except KeyboardInterrupt:
        interrupted = True
        for future in future_to_cik:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        print("\n[Backtest] interrupted by Ctrl+C during SEC companyfacts timeline build.", flush=True)
        raise
    finally:
        if not interrupted:
            executor.shutdown(wait=True)
    return results


def _collect_payload_filed_dates(payload: Mapping[str, Any], *, max_cutoff_date: str) -> list[str]:
    facts = payload.get("facts", {})
    if not isinstance(facts, Mapping):
        return []
    dates: set[str] = set()
    for namespace_payload in facts.values():
        if not isinstance(namespace_payload, Mapping):
            continue
        for concept_payload in namespace_payload.values():
            if not isinstance(concept_payload, Mapping):
                continue
            units = concept_payload.get("units", {})
            if not isinstance(units, Mapping):
                continue
            for values in units.values():
                if not isinstance(values, list):
                    continue
                for fact in values:
                    if not isinstance(fact, Mapping):
                        continue
                    filed = str(fact.get("filed") or "")
                    if len(filed) == 10 and filed <= max_cutoff_date:
                        dates.add(filed)
    return sorted(dates)


def _build_sec_symbol_snapshots(
    *,
    sec_client: SecApiClient,
    symbols: Sequence[str],
    max_workers: int,
    max_cutoff_date: str,
) -> dict[str, dict[str, Any]]:
    ticker_to_cik = sec_client.load_ticker_to_cik_map()
    symbol_to_cik = {symbol: _normalize_cik(ticker_to_cik.get(symbol)) for symbol in symbols}
    unique_ciks = sorted({cik for cik in symbol_to_cik.values() if cik})
    payload_by_cik = _fetch_companyfacts_payloads_parallel(
        sec_client=sec_client,
        ciks=unique_ciks,
        max_workers=max_workers,
    )

    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        cik = symbol_to_cik.get(symbol, "")
        base_row = {
            "symbol": symbol,
            "cik": cik,
            "sec_status": "missing_cik_mapping" if not cik else "payload_unavailable",
            "sec_payload_source": "",
            "sec_error": "",
            "shares_outstanding": np.nan,
            "share_source": "",
            "share_source_tag": "",
            "share_source_priority": np.nan,
            "share_is_spot": False,
            "share_filed_date": "",
            "share_period_end": "",
            "share_form": "",
            "share_accession": "",
            "last_fundamental_filed_date": "",
            "last_fundamental_period_end": "",
        }
        for concept in RAW_FACT_COLUMNS:
            base_row[concept] = np.nan

        if not cik:
            out[symbol] = {"cik": "", "timeline_dates": [], "timeline_rows": [], "base_row": base_row}
            continue

        payload, payload_source, payload_error = payload_by_cik.get(cik, (None, "missing", "payload_not_fetched"))
        base_row["sec_payload_source"] = payload_source
        base_row["sec_error"] = payload_error
        if payload is None:
            out[symbol] = {"cik": cik, "timeline_dates": [], "timeline_rows": [], "base_row": base_row}
            continue

        filed_dates = _collect_payload_filed_dates(payload, max_cutoff_date=max_cutoff_date)
        if max_cutoff_date not in filed_dates:
            filed_dates.append(max_cutoff_date)
        filed_dates = sorted(set(filed_dates))

        timeline_dates: list[str] = []
        timeline_rows: list[dict[str, Any]] = []
        last_signature: tuple[Any, ...] | None = None

        for asof in filed_dates:
            row = dict(base_row)
            row["sec_status"] = "ok"
            share_snapshot = _extract_share_snapshot(payload, as_of_date=asof)
            fundamental_snapshot = _extract_fundamental_snapshot(payload, as_of_date=asof)
            row.update(share_snapshot)
            row.update(fundamental_snapshot)
            signature = (
                row.get("shares_outstanding"),
                row.get("share_filed_date"),
                row.get("last_fundamental_filed_date"),
                row.get("last_fundamental_period_end"),
                *[row.get(concept) for concept in RAW_FACT_COLUMNS],
            )
            if signature == last_signature and timeline_dates:
                continue
            timeline_dates.append(asof)
            timeline_rows.append(row)
            last_signature = signature

        out[symbol] = {
            "cik": cik,
            "timeline_dates": timeline_dates,
            "timeline_rows": timeline_rows,
            "base_row": base_row,
        }
    return out


def _bind_cached_sec_feature_builder(
    alpha_core: AlphaCore,
    symbol_snapshots: Mapping[str, Mapping[str, Any]],
):
    def _build_sec_features_cached(self: AlphaCore, symbols: Sequence[str], *, data_cutoff_date: date) -> pd.DataFrame:
        cutoff = data_cutoff_date.isoformat()
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            key = str(symbol).strip().upper()
            info = symbol_snapshots.get(key)
            if not info:
                row = {
                    "symbol": key,
                    "cik": "",
                    "sec_status": "missing_snapshot_cache",
                    "sec_payload_source": "",
                    "sec_error": "symbol_not_in_snapshot_cache",
                    "shares_outstanding": np.nan,
                    "share_source": "",
                    "share_source_tag": "",
                    "share_source_priority": np.nan,
                    "share_is_spot": False,
                    "share_filed_date": "",
                    "share_period_end": "",
                    "share_form": "",
                    "share_accession": "",
                    "last_fundamental_filed_date": "",
                    "last_fundamental_period_end": "",
                }
                for concept in RAW_FACT_COLUMNS:
                    row[concept] = np.nan
                rows.append(row)
                continue

            timeline_dates = list(info.get("timeline_dates") or [])
            timeline_rows = list(info.get("timeline_rows") or [])
            base_row = dict(info.get("base_row") or {})
            if not timeline_dates or not timeline_rows:
                row = dict(base_row)
                row["symbol"] = key
                rows.append(row)
                continue

            idx = bisect.bisect_right(timeline_dates, cutoff) - 1
            if idx < 0:
                row = dict(base_row)
                row["symbol"] = key
                rows.append(row)
                continue
            row = dict(timeline_rows[min(idx, len(timeline_rows) - 1)])
            row["symbol"] = key
            rows.append(row)

        frame = pd.DataFrame(rows)
        for column in ("shares_outstanding", *RAW_FACT_COLUMNS):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    return _build_sec_features_cached.__get__(alpha_core, AlphaCore)


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


if __name__ == "__main__":
    raise SystemExit(main())
