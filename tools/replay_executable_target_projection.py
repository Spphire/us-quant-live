from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.alpaca_executor import (  # noqa: E402
    _build_order_instructions,
    _buying_power,
    _scale_entry_instructions_to_buying_power,
    _split_release_entry_instructions,
)
from src.executable_target_projector import project_executable_targets  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else {}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline replay of executable target projection from a live run.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--buying-power-buffer", type=float, default=0.90)
    parser.add_argument("--min-trade-notional", type=float, default=None)
    parser.add_argument("--min-trade-weight-bps", type=float, default=1.0)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--output-csv", type=Path, default=None)
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    target_snapshot = _read_json(run_dir / "target_weights_snapshot.json")
    portfolio_snapshot = _read_json(run_dir / "portfolio_weights_snapshot.json")
    price_snapshot = _read_json(run_dir / "execution_price_snapshot.json")
    asset_snapshot = _read_json(run_dir / "broker_assets_relevant.json")
    account_snapshot = _read_json(run_dir / "broker_account_for_sizing.json")
    order_plan = _read_json(run_dir / "order_plan.json")

    raw_weights = _mapping(target_snapshot, "raw_target_signed_weights") or _mapping(
        portfolio_snapshot, "raw_target_signed_weights"
    )
    current_qty = _mapping(portfolio_snapshot, "broker_signed_qty_before")
    current_notional = _mapping(portfolio_snapshot, "broker_signed_notional_before")
    reference_prices = _mapping(price_snapshot, "reference_prices")
    assets_by_symbol = _mapping(asset_snapshot, "assets_by_symbol")
    equity = float(
        target_snapshot.get("account_equity_for_sizing")
        or portfolio_snapshot.get("sizing_equity")
        or account_snapshot.get("portfolio_value")
        or account_snapshot.get("equity")
    )
    buying_power, buying_power_source = _buying_power(account_snapshot)
    min_trade_notional_floor = float(order_plan.get("min_trade_notional_absolute_floor") or 1.0)
    min_trade_notional = (
        float(args.min_trade_notional)
        if args.min_trade_notional is not None
        else max(
            min_trade_notional_floor,
            equity * max(0.0, float(args.min_trade_weight_bps)) / 10000.0,
        )
    )

    order_weights, lattice_qty, diagnostics = project_executable_targets(
        raw_target_signed_weights=raw_weights,
        current_signed_qty=current_qty,
        current_signed_notional=current_notional,
        reference_prices=reference_prices,
        assets_by_symbol=assets_by_symbol,
        account_equity=equity,
        buying_power=buying_power,
        buying_power_buffer=float(args.buying_power_buffer),
        min_trade_notional=min_trade_notional,
        qty_decimals=int(order_plan.get("qty_decimals") or 4),
        whole_shares_only=bool(order_plan.get("whole_shares_only", False)),
        short_sales_whole_shares_only=bool(order_plan.get("short_sales_whole_shares_only", True)),
        shorting_enabled=bool(account_snapshot.get("shorting_enabled", True)),
        sizing_adverse_offset_bps=float(order_plan.get("sizing_adverse_offset_bps") or 12.0),
        short_buying_power_adverse_offset_bps=float(
            order_plan.get("short_buying_power_adverse_offset_bps") or 300.0
        ),
    )
    instructions, skipped_orders = _build_order_instructions(
        target_signed_weights=order_weights,
        current_signed_notional=current_notional,
        current_signed_qty=current_qty,
        account_equity=equity,
        reference_prices=reference_prices,
        assets_by_symbol=assets_by_symbol,
        min_trade_notional=min_trade_notional,
        sizing_adverse_offset_bps=float(order_plan.get("sizing_adverse_offset_bps") or 12.0),
        qty_decimals=int(order_plan.get("qty_decimals") or 4),
        whole_shares_only=bool(order_plan.get("whole_shares_only", False)),
        opening_shorts_whole_shares_only=bool(order_plan.get("opening_shorts_whole_shares_only", True)),
        short_sales_whole_shares_only=bool(order_plan.get("short_sales_whole_shares_only", True)),
        shorting_enabled=bool(account_snapshot.get("shorting_enabled", True)),
    )
    release_instructions, entry_instructions = _split_release_entry_instructions(instructions)
    guarded_entry_instructions, terminal_cap = _scale_entry_instructions_to_buying_power(
        entry_instructions,
        buying_power=buying_power,
        buffer=float(args.buying_power_buffer),
        min_trade_notional=min_trade_notional,
        qty_decimals=int(order_plan.get("qty_decimals") or 4),
        whole_shares_only=bool(order_plan.get("whole_shares_only", False)),
        short_sales_whole_shares_only=bool(order_plan.get("short_sales_whole_shares_only", True)),
        short_buying_power_adverse_offset_bps=float(
            order_plan.get("short_buying_power_adverse_offset_bps") or 300.0
        ),
    )
    terminal_scaler_changed = [
        (item.symbol, item.side, item.qty) for item in guarded_entry_instructions
    ] != [(item.symbol, item.side, item.qty) for item in entry_instructions]
    fractional_short_sell_orders = [
        {"symbol": item.symbol, "qty": item.qty}
        for item in instructions
        if item.side == "sell"
        and item.target_notional < 0.0
        and abs(float(item.qty) - round(float(item.qty))) > 1e-9
    ]
    old_floor = _mapping(target_snapshot, "target_short_floor_diagnostics")
    old_projected_weights = _mapping(target_snapshot, "projected_target_signed_weights")
    old_projection_weight_error_l1 = sum(
        abs(float(raw_weights.get(symbol, 0.0)) - float(old_projected_weights.get(symbol, 0.0)))
        for symbol in set(raw_weights) | set(old_projected_weights)
    )
    pre_min_summary = diagnostics.get("optimizer_pre_min_trade_summary") or {}
    report = {
        "schema_version": "1.0",
        "replay_source_run_dir": run_dir.as_posix(),
        "primary_weight_error": {
            "final_tracking_error_l1_weight": diagnostics.get("tracking_error_l1_weight"),
            "final_tracking_error_l1_weight_pct": diagnostics.get("tracking_error_l1_weight_pct"),
            "final_mean_abs_symbol_weight_error": diagnostics.get("mean_abs_symbol_weight_error"),
            "final_mean_abs_symbol_weight_error_pct": diagnostics.get(
                "mean_abs_symbol_weight_error_pct"
            ),
            "final_max_abs_symbol_weight_error": diagnostics.get("max_abs_symbol_weight_error"),
            "final_max_abs_symbol_weight_error_pct": diagnostics.get("max_abs_symbol_weight_error_pct"),
            "optimizer_pre_min_trade_l1_weight": pre_min_summary.get("tracking_error_l1_weight"),
            "optimizer_pre_min_trade_l1_weight_pct": pre_min_summary.get("tracking_error_l1_weight_pct"),
            "legacy_short_floor_projection_l1_weight": float(old_projection_weight_error_l1),
            "legacy_short_floor_projection_l1_weight_pct": float(old_projection_weight_error_l1 * 100.0),
        },
        "constraint_utilization": {
            "buying_power_source": buying_power_source,
            "buying_power": buying_power,
            "buying_power_buffer": diagnostics.get("buying_power_buffer"),
            "buying_power_cap": diagnostics.get("buying_power_cap"),
            "estimated_entry_buying_power_used": diagnostics.get("estimated_entry_buying_power_used"),
            "buying_power_cap_utilization": diagnostics.get("buying_power_cap_utilization"),
            "effective_min_trade_notional": float(min_trade_notional),
            "min_trade_weight_bps": float(args.min_trade_weight_bps),
        },
        "auxiliary_notional_translation": {
            "legacy_short_floor_gap": old_floor.get("lost_notional"),
            "new_final_integer_short_gap": diagnostics.get("integer_short_absolute_notional_gap"),
            "new_optimizer_pre_min_trade_integer_short_gap": pre_min_summary.get(
                "integer_short_absolute_notional_gap"
            ),
        },
        "order_target_signed_weights": order_weights,
        "target_lattice_signed_qty": lattice_qty,
        "simulated_order_plan": {
            "order_count": len(instructions),
            "release_order_count": len(release_instructions),
            "entry_order_count_before_terminal_cap": len(entry_instructions),
            "entry_order_count_after_terminal_cap": len(guarded_entry_instructions),
            "skipped_orders": skipped_orders,
            "terminal_buying_power_cap": terminal_cap,
            "terminal_scaler_changed_optimizer_result": terminal_scaler_changed,
            "fractional_short_sell_orders": fractional_short_sell_orders,
        },
        "projection": diagnostics,
    }

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(diagnostics.get("symbols") or []).to_csv(args.output_csv, index=False)

    concise = {
        "solver_success": diagnostics.get("solver", {}).get("success"),
        "primary_objective": diagnostics.get("solver", {}).get("objective_priority", [None])[0],
        "tracking_error_l1_weight_pct": diagnostics.get("tracking_error_l1_weight_pct"),
        "mean_abs_symbol_weight_error_pct": diagnostics.get("mean_abs_symbol_weight_error_pct"),
        "max_abs_symbol_weight_error_pct": diagnostics.get("max_abs_symbol_weight_error_pct"),
        "optimizer_pre_min_trade_l1_weight_pct": pre_min_summary.get("tracking_error_l1_weight_pct"),
        "legacy_short_floor_projection_l1_weight_pct": float(old_projection_weight_error_l1 * 100.0),
        "raw_long_gross_weight": diagnostics.get("raw_long_gross_weight"),
        "raw_short_gross_weight": diagnostics.get("raw_short_gross_weight"),
        "executable_long_gross_weight": diagnostics.get("executable_long_gross_weight"),
        "executable_short_gross_weight": diagnostics.get("executable_short_gross_weight"),
        "buying_power": buying_power,
        "buying_power_buffer": diagnostics.get("buying_power_buffer"),
        "buying_power_cap": diagnostics.get("buying_power_cap"),
        "estimated_entry_buying_power_used": diagnostics.get("estimated_entry_buying_power_used"),
        "buying_power_cap_utilization": diagnostics.get("buying_power_cap_utilization"),
        "blocked_target_count": diagnostics.get("blocked_target_count"),
        "simulated_order_count": len(instructions),
        "simulated_release_order_count": len(release_instructions),
        "simulated_entry_order_count": len(entry_instructions),
        "simulated_skipped_order_count": len(skipped_orders),
        "terminal_scaler_changed_optimizer_result": terminal_scaler_changed,
        "fractional_short_sell_order_count": len(fractional_short_sell_orders),
    }
    print(json.dumps(concise, indent=2, ensure_ascii=False))
    return 0 if bool(diagnostics.get("solver", {}).get("success")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
