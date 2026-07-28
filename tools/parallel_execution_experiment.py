from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dynamic_symbol_pool import _load_credentials_from_accounts_json  # noqa: E402
from vendors.alpaca import AlpacaHttpClient  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run repeated equal-weight long/short executions on an isolated Alpaca paper account."
    )
    parser.add_argument("--account-name", required=True)
    parser.add_argument("--production-account-name", default="ALPACA_US_FULL")
    parser.add_argument(
        "--accounts-json-path",
        default="configs/alpaca_acounts/alpaca_accounts.local.json",
    )
    parser.add_argument(
        "--longbridge-config-path",
        default="configs/longbridge.local.json",
    )
    parser.add_argument(
        "--source-universe-json",
        default="artifacts/daily_alpaca_scheduler/20260728_decision/symbol_universe_intersection.json",
    )
    parser.add_argument(
        "--source-universe-csv",
        default="artifacts/daily_alpaca_scheduler/20260728_decision/symbol_universe_intersection.csv",
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--long-count", type=int, default=35)
    parser.add_argument("--short-count", type=int, default=35)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--min-reference-price", type=float, default=10.0)
    parser.add_argument("--max-reference-price", type=float, default=300.0)
    parser.add_argument("--execution-workers", type=int, default=10)
    parser.add_argument("--max-quote-age-seconds", type=float, default=10.0)
    parser.add_argument("--between-round-seconds", type=float, default=5.0)
    parser.add_argument("--output-root", default=None)
    return parser


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists() or path.stat().st_size <= 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, float(percentile))) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def _stats(values: Iterable[float]) -> dict[str, float | None]:
    cleaned = [float(value) for value in values]
    return {
        "mean": mean(cleaned) if cleaned else None,
        "median": median(cleaned) if cleaned else None,
        "p95": _percentile(cleaned, 0.95),
        "min": min(cleaned) if cleaned else None,
        "max": max(cleaned) if cleaned else None,
    }


def _credentials(path: Path, account_name: str):
    return _load_credentials_from_accounts_json(
        path=path,
        account_name=account_name,
        data_base_url="https://data.alpaca.markets",
        request_timeout_seconds=60.0,
        max_retries=2,
    )


def _account_snapshot(path: Path, account_name: str) -> tuple[AlpacaHttpClient, dict[str, Any]]:
    credentials = _credentials(path, account_name)
    if "paper-api.alpaca.markets" not in credentials.trading_base_url:
        raise RuntimeError(f"Refusing non-paper Alpaca endpoint for account profile {account_name!r}.")
    client = AlpacaHttpClient(credentials)
    account = client.get_account()
    return client, account


def _eligible_symbols(
    *,
    universe_json_path: Path,
    universe_csv_path: Path,
    assets: Iterable[Mapping[str, Any]],
    min_price: float,
    max_price: float,
) -> list[str]:
    snapshot = _load_json(universe_json_path, {})
    final_symbols = {
        str(symbol).strip().upper()
        for symbol in snapshot.get("final_intersection_symbols", [])
        if str(symbol).strip()
    }
    prices: dict[str, float] = {}
    with universe_csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                prices[symbol] = _float(row.get("longbridge_last_price"), -1.0)
    assets_by_symbol = {
        str(asset.get("symbol") or "").strip().upper(): asset
        for asset in assets
        if str(asset.get("symbol") or "").strip()
    }
    eligible: list[str] = []
    for symbol in sorted(final_symbols):
        asset = assets_by_symbol.get(symbol, {})
        price = prices.get(symbol, -1.0)
        if not bool(asset.get("tradable")):
            continue
        if not bool(asset.get("shortable")) or not bool(asset.get("easy_to_borrow")):
            continue
        if price < float(min_price) or price > float(max_price):
            continue
        eligible.append(symbol)
    return eligible


def _write_targets(path: Path, longs: list[str], shorts: list[str]) -> None:
    rows = [
        {
            "symbol": symbol,
            "signed_weight": 1.0 / len(longs),
            "side": "long",
            "side_weight": 1.0 / len(longs),
        }
        for symbol in longs
    ] + [
        {
            "symbol": symbol,
            "signed_weight": -1.0 / len(shorts),
            "side": "short",
            "side_weight": 1.0 / len(shorts),
        }
        for symbol in shorts
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "signed_weight", "side", "side_weight"],
        )
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (str(row["side"]), str(row["symbol"]))))


def _walk_batch_summaries(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, Mapping):
        if {
            "elapsed_seconds",
            "aggregate_order_work_seconds",
            "parallel_speedup_ratio",
        }.issubset(value):
            yield dict(value)
        for child in value.values():
            yield from _walk_batch_summaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_batch_summaries(child)


def _l1_error(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> float:
    symbols = set(expected) | set(actual)
    return float(
        sum(abs(_float(expected.get(symbol)) - _float(actual.get(symbol))) for symbol in symbols)
    )


def _summarize_round(round_no: int, run_dir: Path, return_code: int, wall_seconds: float) -> dict[str, Any]:
    summary = _load_json(run_dir / "execution_summary.json", {})
    outcome = _load_json(run_dir / "execution_attempt_outcome_summary.json", {})
    weights_after = _load_json(run_dir / "portfolio_weights_after_snapshot.json", {})
    account_after = _load_json(run_dir / "broker_account_after.json", {})
    quote_health = _load_json(run_dir / "execution_quote_provider_health_after.json", {})
    timings = _load_json(run_dir / "decision_phase_timings.json", {})
    actual_weights = dict(weights_after.get("broker_weights_after") or {})
    raw_weights = dict(summary.get("raw_target_signed_weights") or {})
    executable_weights = dict(summary.get("executable_expected_signed_weights") or {})
    logical_records = int(outcome.get("logical_record_count") or 0)
    terminal_unfilled = int(outcome.get("terminal_unfilled_record_count") or 0)
    batch_summaries = [
        row
        for row in _walk_batch_summaries(summary.get("staged_diagnostics") or {})
        if int(row.get("effective_workers") or 0) > 1
    ]
    batch_elapsed = sum(_float(row.get("elapsed_seconds")) for row in batch_summaries)
    batch_work = sum(
        _float(row.get("aggregate_order_work_seconds")) for row in batch_summaries
    )
    gross_position = abs(_float(account_after.get("long_market_value"))) + abs(
        _float(account_after.get("short_market_value"))
    )
    regt_remaining = _float(account_after.get("regt_buying_power"))
    final_snapshot_total_capacity = gross_position + regt_remaining
    projection = dict(
        summary.get("executable_target_projection")
        or summary.get("initial_executable_target_projection")
        or {}
    )
    projection_capacity = _float(projection.get("total_buying_power_capacity"))
    projection_target_ratio = _float(
        projection.get("gross_capacity_target_ratio"),
        _float(summary.get("gross_capacity_target_ratio"), 0.95),
    )
    projected_final_gross = _float(projection.get("projected_final_gross_notional"))
    submit_errors = int(summary.get("submit_error_count") or 0)
    quote_failures = int(quote_health.get("snapshot_refresh_failure_count") or 0)
    result = {
        "round": int(round_no),
        "return_code": int(return_code),
        "ok": bool(
            return_code == 0
            and summary.get("ok") is True
            and terminal_unfilled == 0
            and submit_errors == 0
            and not str((summary.get("staged_diagnostics") or {}).get("entry_abort_reason") or "")
        ),
        "wall_seconds": float(wall_seconds),
        "executor_elapsed_seconds": _float(timings.get("elapsed_seconds")),
        "order_submission_seconds": next(
            (
                _float(phase.get("elapsed_seconds"))
                for phase in timings.get("phases", [])
                if phase.get("phase") == "order_submission_and_tracking"
            ),
            0.0,
        ),
        "logical_orders": logical_records,
        "filled_logical_orders": max(0, logical_records - terminal_unfilled),
        "broker_attempts": int(outcome.get("broker_attempt_count") or 0),
        "requote_cancels": int(
            outcome.get("superseded_requote_canceled_attempt_count") or 0
        ),
        "terminal_cancels": int(outcome.get("terminal_canceled_attempt_count") or 0),
        "terminal_unfilled": terminal_unfilled,
        "submit_errors": submit_errors,
        "entry_abort_reason": str(
            (summary.get("staged_diagnostics") or {}).get("entry_abort_reason") or ""
        ),
        "strategy_to_actual_l1": _l1_error(raw_weights, actual_weights),
        "executable_to_actual_l1": _l1_error(executable_weights, actual_weights),
        "gross_utilization": (
            gross_position / projection_capacity if projection_capacity > 0 else None
        ),
        "gross_target_error_notional": (
            gross_position - projection_target_ratio * projection_capacity
            if projection_capacity > 0
            else None
        ),
        "gross_projection_capacity": projection_capacity or None,
        "gross_projection_target_ratio": projection_target_ratio,
        "projected_final_gross_notional": projected_final_gross or None,
        "actual_vs_projected_gross_notional": (
            gross_position - projected_final_gross if projected_final_gross > 0 else None
        ),
        "final_snapshot_gross_utilization": (
            gross_position / final_snapshot_total_capacity
            if final_snapshot_total_capacity > 0
            else None
        ),
        "multi_worker_batch_count": len(batch_summaries),
        "order_parallel_elapsed_seconds": float(batch_elapsed),
        "order_parallel_work_seconds": float(batch_work),
        "order_parallel_speedup_ratio": batch_work / batch_elapsed if batch_elapsed > 0 else None,
        "quote_refresh_attempts": int(quote_health.get("snapshot_refresh_attempt_count") or 0),
        "quote_refresh_symbols": int(quote_health.get("snapshot_refresh_symbol_count") or 0),
        "quote_refresh_failures": quote_failures,
        "quote_multi_refresh_count": int(
            quote_health.get("snapshot_refresh_multi_symbol_count") or 0
        ),
        "quote_parallel_elapsed_seconds": _float(
            quote_health.get("snapshot_refresh_multi_symbol_depth_elapsed_seconds")
        ),
        "quote_parallel_work_seconds": _float(
            quote_health.get("snapshot_refresh_multi_symbol_depth_work_seconds")
        ),
        "quote_parallel_speedup_ratio": quote_health.get(
            "snapshot_refresh_multi_symbol_parallel_speedup_ratio"
        ),
        "quote_max_batch_symbols": int(
            quote_health.get("snapshot_refresh_max_requested_symbols") or 0
        ),
        "quote_max_workers": int(quote_health.get("snapshot_refresh_max_depth_workers") or 0),
        "max_depth_age_ms_after": quote_health.get("max_depth_local_age_ms"),
        "run_dir": run_dir.as_posix(),
    }
    _write_json(run_dir.parent / "round_result.json", result)
    return result


def _aggregate(results: list[dict[str, Any]], metadata: Mapping[str, Any]) -> dict[str, Any]:
    quote_elapsed = sum(_float(row.get("quote_parallel_elapsed_seconds")) for row in results)
    quote_work = sum(_float(row.get("quote_parallel_work_seconds")) for row in results)
    order_elapsed = sum(_float(row.get("order_parallel_elapsed_seconds")) for row in results)
    order_work = sum(_float(row.get("order_parallel_work_seconds")) for row in results)
    logical_orders = sum(int(row.get("logical_orders") or 0) for row in results)
    filled_orders = sum(int(row.get("filled_logical_orders") or 0) for row in results)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now().astimezone().isoformat(),
        **dict(metadata),
        "completed_rounds": len(results),
        "successful_rounds": sum(bool(row.get("ok")) for row in results),
        "logical_orders": logical_orders,
        "filled_logical_orders": filled_orders,
        "logical_fill_rate": filled_orders / logical_orders if logical_orders else None,
        "broker_attempts": sum(int(row.get("broker_attempts") or 0) for row in results),
        "requote_cancels": sum(int(row.get("requote_cancels") or 0) for row in results),
        "terminal_cancels": sum(int(row.get("terminal_cancels") or 0) for row in results),
        "terminal_unfilled": sum(int(row.get("terminal_unfilled") or 0) for row in results),
        "submit_errors": sum(int(row.get("submit_errors") or 0) for row in results),
        "quote_refresh_failures": sum(int(row.get("quote_refresh_failures") or 0) for row in results),
        "quote_parallel_elapsed_seconds": quote_elapsed,
        "quote_serial_equivalent_work_seconds": quote_work,
        "quote_parallel_speedup_ratio": quote_work / quote_elapsed if quote_elapsed > 0 else None,
        "quote_estimated_time_saved_seconds": max(0.0, quote_work - quote_elapsed),
        "order_parallel_elapsed_seconds": order_elapsed,
        "order_serial_equivalent_work_seconds": order_work,
        "order_parallel_speedup_ratio": order_work / order_elapsed if order_elapsed > 0 else None,
        "order_estimated_time_saved_seconds": max(0.0, order_work - order_elapsed),
        "wall_seconds": _stats(_float(row.get("wall_seconds")) for row in results),
        "order_submission_seconds": _stats(
            _float(row.get("order_submission_seconds")) for row in results
        ),
        "strategy_to_actual_l1": _stats(
            _float(row.get("strategy_to_actual_l1")) for row in results
        ),
        "executable_to_actual_l1": _stats(
            _float(row.get("executable_to_actual_l1")) for row in results
        ),
        "gross_utilization": _stats(
            _float(row.get("gross_utilization"))
            for row in results
            if row.get("gross_utilization") is not None
        ),
        "rounds": results,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.rounds < 1 or args.long_count < 1 or args.short_count < 1:
        raise ValueError("rounds, long-count, and short-count must all be positive.")
    if args.account_name == args.production_account_name:
        raise RuntimeError("Test and production account profiles must be different.")

    accounts_path = (PROJECT_ROOT / args.accounts_json_path).resolve()
    longbridge_path = (PROJECT_ROOT / args.longbridge_config_path).resolve()
    universe_json_path = (PROJECT_ROOT / args.source_universe_json).resolve()
    universe_csv_path = (PROJECT_ROOT / args.source_universe_csv).resolve()
    if not longbridge_path.exists():
        raise FileNotFoundError(longbridge_path)
    if not universe_json_path.exists() or not universe_csv_path.exists():
        raise FileNotFoundError("Source symbol-universe artifacts are required.")

    test_client, test_account = _account_snapshot(accounts_path, args.account_name)
    _, production_account = _account_snapshot(accounts_path, args.production_account_name)
    test_account_id = str(test_account.get("id") or "")
    production_account_id = str(production_account.get("id") or "")
    if not test_account_id or test_account_id == production_account_id:
        raise RuntimeError("Test account identity is missing or matches the production account.")
    if str(test_account.get("status") or "").upper() != "ACTIVE":
        raise RuntimeError("Test account is not ACTIVE.")
    if not bool(test_account.get("shorting_enabled")):
        raise RuntimeError("Test account does not permit shorting.")

    assets = test_client.list_assets(status="active", asset_class="us_equity")
    eligible = _eligible_symbols(
        universe_json_path=universe_json_path,
        universe_csv_path=universe_csv_path,
        assets=assets,
        min_price=float(args.min_reference_price),
        max_price=float(args.max_reference_price),
    )
    required_count = int(args.long_count) + int(args.short_count)
    if len(eligible) < required_count:
        raise RuntimeError(
            f"Only {len(eligible)} eligible symbols remain; {required_count} are required."
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (
        Path(args.output_root).resolve()
        if args.output_root
        else (PROJECT_ROOT / "artifacts" / "parallel_execution_experiment" / stamp).resolve()
    )
    output_root.mkdir(parents=True, exist_ok=True)
    ledger_path = output_root / "test_account_lot_ledger.json"
    rng = random.Random(int(args.seed))
    metadata = {
        "account_profile": str(args.account_name),
        "account_id_tail": test_account_id[-6:],
        "production_account_id_tail": production_account_id[-6:],
        "paper_endpoint_verified": True,
        "shorting_enabled": True,
        "decision_date": str(args.date),
        "requested_rounds": int(args.rounds),
        "long_count": int(args.long_count),
        "short_count": int(args.short_count),
        "side_weight": 1.0 / float(args.long_count),
        "raw_gross_weight": 2.0,
        "eligible_symbol_count": len(eligible),
        "seed": int(args.seed),
        "execution_workers": int(args.execution_workers),
        "max_quote_age_seconds": float(args.max_quote_age_seconds),
        "source_universe_json": universe_json_path.as_posix(),
        "output_root": output_root.as_posix(),
    }
    _write_json(output_root / "experiment_context.json", metadata)
    print(
        f"[experiment] profile={args.account_name} account=*{test_account_id[-6:]} "
        f"eligible={len(eligible)} rounds={args.rounds}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    for round_no in range(1, int(args.rounds) + 1):
        selected = rng.sample(eligible, required_count)
        rng.shuffle(selected)
        longs = sorted(selected[: int(args.long_count)])
        shorts = sorted(selected[int(args.long_count) :])
        round_root = output_root / f"round_{round_no:02d}"
        run_dir = round_root / "execution"
        target_path = round_root / "decision_targets.csv"
        _write_targets(target_path, longs, shorts)
        shutil.copy2(universe_json_path, round_root / "symbol_universe_intersection.json")
        shutil.copy2(universe_csv_path, round_root / "symbol_universe_intersection.csv")
        _write_json(
            round_root / "random_decision.json",
            {
                "round": round_no,
                "seed": int(args.seed),
                "longs": longs,
                "shorts": shorts,
                "side_weight": 1.0 / float(args.long_count),
            },
        )
        command = [
            sys.executable,
            str(SRC_ROOT / "alpaca_executor.py"),
            "--date",
            str(args.date),
            "--accounts-json-path",
            str(accounts_path),
            "--account-name",
            str(args.account_name),
            "--longbridge-config-path",
            str(longbridge_path),
            "--decision-targets-input-path",
            str(target_path),
            "--trigger-mode",
            "immediate",
            "--cancel-open-orders-before-submit",
            "--execution-mode",
            "staged_regt",
            "--execution-order-style",
            "marketable_limit",
            "--execution-quote-provider",
            "longbridge",
            "--execution-price-feed",
            "iex",
            "--longbridge-max-quote-age-seconds",
            str(float(args.max_quote_age_seconds)),
            "--longbridge-max-spread-bps",
            "150",
            "--execution-workers",
            str(int(args.execution_workers)),
            "--marketable-limit-requote-wait-seconds",
            "6",
            "--marketable-limit-requote-steps-bps",
            "0,25,75,150",
            "--marketable-limit-max-attempts",
            "4",
            "--order-timeout-seconds",
            "180",
            "--staged-release-timeout-seconds",
            "180",
            "--staged-entry-timeout-seconds",
            "180",
            "--staged-entry-repair-rounds",
            "1",
            "--staged-entry-repair-max-attempts",
            "1",
            "--gross-capacity-target-ratio",
            "0.95",
            "--entry-buying-power-buffer",
            "0.95",
            "--ledger-path",
            str(ledger_path),
            "--output-root",
            str(run_dir),
        ]
        print(
            f"[round {round_no:02d}/{args.rounds}] start long={len(longs)} short={len(shorts)}",
            flush=True,
        )
        started = time.monotonic()
        with (round_root / "executor.log").open("w", encoding="utf-8") as log_handle:
            completed = subprocess.run(
                command,
                cwd=PROJECT_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        wall_seconds = time.monotonic() - started
        result = _summarize_round(round_no, run_dir, completed.returncode, wall_seconds)
        results.append(result)
        aggregate = _aggregate(results, metadata)
        _write_json(output_root / "experiment_summary.json", aggregate)
        print(
            f"[round {round_no:02d}/{args.rounds}] ok={result['ok']} "
            f"orders={result['filled_logical_orders']}/{result['logical_orders']} "
            f"quote_speedup={_float(result.get('quote_parallel_speedup_ratio')):.2f}x "
            f"order_speedup={_float(result.get('order_parallel_speedup_ratio')):.2f}x "
            f"wall={wall_seconds:.1f}s",
            flush=True,
        )
        if round_no < int(args.rounds) and float(args.between_round_seconds) > 0:
            time.sleep(float(args.between_round_seconds))

    summary = _aggregate(results, metadata)
    _write_json(output_root / "experiment_summary.json", summary)
    csv_rows = [{key: value for key, value in row.items() if not isinstance(value, (dict, list))} for row in results]
    if csv_rows:
        with (output_root / "round_results.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
            writer.writeheader()
            writer.writerows(csv_rows)
    print(
        f"[experiment] complete successful={summary['successful_rounds']}/{summary['completed_rounds']} "
        f"fills={summary['filled_logical_orders']}/{summary['logical_orders']} "
        f"quote_speedup={_float(summary.get('quote_parallel_speedup_ratio')):.2f}x "
        f"order_speedup={_float(summary.get('order_parallel_speedup_ratio')):.2f}x",
        flush=True,
    )
    print(f"[experiment] summary={output_root / 'experiment_summary.json'}", flush=True)
    return 0 if int(summary["successful_rounds"]) == int(summary["completed_rounds"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
