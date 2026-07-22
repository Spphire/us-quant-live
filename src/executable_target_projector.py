from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


EPS = 1e-10


@dataclass(slots=True)
class _TargetSpec:
    symbol: str
    side: str
    raw_weight: float
    desired_notional: float
    reference_price: float
    current_signed_qty: float
    current_signed_notional: float
    current_same_side_qty: float
    short_position_residual_qty: float
    qty_upper_bound: float
    integral_target: bool
    buying_power_price: float
    constraints: list[str]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(out) if math.isfinite(out) else float(default)


def _adverse_price(*, side: str, reference_price: float, offset_bps: float) -> float:
    price = max(float(reference_price), 1e-9)
    offset = max(float(offset_bps), 0.0) / 10000.0
    if str(side).lower() == "buy":
        return float(price * (1.0 + offset))
    return float(max(price * (1.0 - offset), 1e-9))


def _projected_whole_qty(raw_qty: float, *, integer_tolerance: float = 0.20) -> float:
    if raw_qty <= EPS:
        return 0.0
    nearest = round(float(raw_qty))
    if nearest > 0 and abs(float(raw_qty) - float(nearest)) <= float(integer_tolerance):
        return float(nearest)
    return float(math.floor(float(raw_qty) + 1e-12))


def _quantize_down(value: float, decimals: int) -> float:
    scale = 10 ** max(0, int(decimals))
    return float(math.floor(max(0.0, float(value)) * scale + 1e-9) / scale)


def _build_target_specs(
    *,
    raw_target_signed_weights: Mapping[str, float],
    current_signed_qty: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    reference_prices: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    account_equity: float,
    shorting_enabled: bool,
    whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    sizing_adverse_offset_bps: float,
    short_buying_power_adverse_offset_bps: float,
) -> tuple[list[_TargetSpec], list[dict[str, Any]]]:
    specs: list[_TargetSpec] = []
    blocked: list[dict[str, Any]] = []
    equity = max(float(account_equity), 1e-9)

    for symbol_raw, weight_raw in sorted(raw_target_signed_weights.items()):
        symbol = str(symbol_raw).strip().upper()
        weight = _safe_float(weight_raw)
        if not symbol or abs(weight) <= EPS:
            continue

        price = _safe_float(reference_prices.get(symbol))
        current_qty = _safe_float(current_signed_qty.get(symbol))
        current_notional = _safe_float(current_signed_notional.get(symbol))
        if price <= EPS:
            blocked.append(
                {
                    "symbol": symbol,
                    "raw_target_signed_weight": float(weight),
                    "current_signed_qty": float(current_qty),
                    "current_signed_notional": float(current_notional),
                    "reason": "missing_reference_price",
                }
            )
            continue

        side = "long" if weight > 0 else "short"
        desired_notional = abs(float(weight)) * equity
        desired_qty = desired_notional / price
        asset = assets_by_symbol.get(symbol, {}) or {}
        constraints: list[str] = []
        residual = 0.0

        if side == "short":
            current_short_qty = max(0.0, -current_qty)
            current_short_anchor = _projected_whole_qty(current_short_qty)
            if current_short_qty > EPS:
                residual = float(current_short_qty - current_short_anchor)
            integral_target = bool(short_sales_whole_shares_only or whole_shares_only)
            qty_upper = float(math.ceil(max(0.0, desired_qty - residual) - 1e-12))
            qty_upper = max(qty_upper, current_short_anchor)
            shortable = bool(asset.get("shortable", False))
            if not shorting_enabled or not shortable:
                qty_upper = min(qty_upper, current_short_anchor)
                constraints.append("account_shorting_disabled" if not shorting_enabled else "asset_not_shortable")
            current_same_side = current_short_anchor
            bp_price = _adverse_price(
                side="buy",
                reference_price=price,
                offset_bps=short_buying_power_adverse_offset_bps,
            )
            constraints.append("short_target_integer" if integral_target else "short_target_fractional_allowed")
        else:
            current_same_side = max(0.0, current_qty)
            fractionable = bool(asset.get("fractionable", True))
            integral_target = bool(whole_shares_only or not fractionable)
            qty_upper = float(math.ceil(desired_qty - 1e-12)) if integral_target else float(desired_qty)
            qty_upper = max(qty_upper, current_same_side if integral_target else 0.0)
            bp_price = _adverse_price(
                side="buy",
                reference_price=price,
                offset_bps=sizing_adverse_offset_bps,
            )
            if not fractionable:
                constraints.append("asset_not_fractionable")

        specs.append(
            _TargetSpec(
                symbol=symbol,
                side=side,
                raw_weight=float(weight),
                desired_notional=float(desired_notional),
                reference_price=float(price),
                current_signed_qty=float(current_qty),
                current_signed_notional=float(current_notional),
                current_same_side_qty=float(current_same_side),
                short_position_residual_qty=float(residual),
                qty_upper_bound=max(0.0, float(qty_upper)),
                integral_target=bool(integral_target),
                buying_power_price=float(bp_price),
                constraints=constraints,
            )
        )
    return specs, blocked


def _solve_projection(
    specs: Sequence[_TargetSpec],
    *,
    buying_power_cap: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    count = len(specs)
    if count == 0:
        return np.zeros(0, dtype=float), {
            "success": True,
            "status": 0,
            "message": "no_optimizable_targets",
            "solver": "scipy.optimize.milp/highs",
        }

    # Variables are target qty, absolute signed-weight deviation, entry qty,
    # and maximum single-name weight deviation.
    q0 = 0
    d0 = count
    e0 = count * 2
    z_idx = count * 3
    variable_count = z_idx + 1
    primary_objective = np.zeros(variable_count, dtype=float)
    primary_objective[d0 : d0 + count] = 1.0
    primary_objective[z_idx] = 0.25

    lower = np.zeros(variable_count, dtype=float)
    upper = np.full(variable_count, np.inf, dtype=float)
    integrality = np.zeros(variable_count, dtype=int)
    for idx, spec in enumerate(specs):
        upper[q0 + idx] = float(spec.qty_upper_bound)
        upper[e0 + idx] = float(spec.qty_upper_bound)
        if spec.integral_target:
            integrality[q0 + idx] = 1

    rows: list[np.ndarray] = []
    row_lower: list[float] = []
    row_upper: list[float] = []
    for idx, spec in enumerate(specs):
        target_weight = abs(float(spec.raw_weight))
        equity = max(float(spec.desired_notional) / max(target_weight, 1e-12), 1e-9)
        weight_per_qty = float(spec.reference_price) / equity
        residual_weight = weight_per_qty * float(spec.short_position_residual_qty)

        row = np.zeros(variable_count, dtype=float)
        row[q0 + idx] = weight_per_qty
        row[d0 + idx] = -1.0
        rows.append(row)
        row_lower.append(-np.inf)
        row_upper.append(target_weight - residual_weight)

        row = np.zeros(variable_count, dtype=float)
        row[q0 + idx] = -weight_per_qty
        row[d0 + idx] = -1.0
        rows.append(row)
        row_lower.append(-np.inf)
        row_upper.append(-target_weight + residual_weight)

        row = np.zeros(variable_count, dtype=float)
        row[d0 + idx] = 1.0
        row[z_idx] = -1.0
        rows.append(row)
        row_lower.append(-np.inf)
        row_upper.append(0.0)

        row = np.zeros(variable_count, dtype=float)
        row[q0 + idx] = 1.0
        row[e0 + idx] = -1.0
        rows.append(row)
        row_lower.append(-np.inf)
        row_upper.append(float(spec.current_same_side_qty))

    bp_row = np.zeros(variable_count, dtype=float)
    safe_cap = max(0.0, float(buying_power_cap))
    normalizer = max(safe_cap, 1.0)
    for idx, spec in enumerate(specs):
        bp_row[e0 + idx] = float(spec.buying_power_price) / normalizer
    rows.append(bp_row)
    row_lower.append(-np.inf)
    row_upper.append(safe_cap / normalizer)

    base_constraint = LinearConstraint(np.vstack(rows), np.asarray(row_lower), np.asarray(row_upper))
    primary_result = milp(
        c=primary_objective,
        integrality=integrality,
        bounds=Bounds(lower, upper),
        constraints=base_constraint,
        options={"time_limit": 10.0, "mip_rel_gap": 1e-9},
    )
    result = primary_result
    primary_value = _safe_float(primary_result.fun, default=float("nan"))
    secondary_used = False
    if primary_result.success and primary_result.x is not None and math.isfinite(primary_value):
        # Lock the best weight error, then maximize executable target gross.
        # This makes buying-power utilization a true secondary objective.
        secondary_objective = np.zeros(variable_count, dtype=float)
        for idx, spec in enumerate(specs):
            equity = max(
                float(spec.desired_notional) / max(abs(float(spec.raw_weight)), 1e-12),
                1e-9,
            )
            secondary_objective[q0 + idx] = -float(spec.reference_price) / equity
            secondary_objective[e0 + idx] = 1e-10
        tolerance = max(1e-9, abs(primary_value) * 1e-7)
        primary_lock = LinearConstraint(
            primary_objective.reshape(1, -1),
            np.asarray([-np.inf]),
            np.asarray([primary_value + tolerance]),
        )
        secondary_result = milp(
            c=secondary_objective,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=(base_constraint, primary_lock),
            options={"time_limit": 10.0, "mip_rel_gap": 1e-9},
        )
        if secondary_result.success and secondary_result.x is not None:
            result = secondary_result
            secondary_used = True
    solver_diag = {
        "success": bool(result.success),
        "status": int(result.status),
        "message": str(result.message),
        "solver": "scipy.optimize.milp/highs",
        "objective_priority": [
            "minimize_absolute_weight_error",
            "maximize_executable_target_gross_without_worsening_weight_error",
        ],
        "primary_weight_error_objective": float(primary_value),
        "secondary_optimization_used": bool(secondary_used),
        "secondary_objective_value": _safe_float(result.fun, default=float("nan"))
        if secondary_used
        else None,
        "mip_gap": _safe_float(getattr(result, "mip_gap", None), default=float("nan")),
        "mip_node_count": int(_safe_float(getattr(result, "mip_node_count", 0))),
    }
    if result.success and result.x is not None:
        return np.asarray(result.x[q0 : q0 + count], dtype=float), solver_diag

    desired = np.asarray(
        [
            max(0.0, (spec.desired_notional / spec.reference_price) - spec.short_position_residual_qty)
            for spec in specs
        ],
        dtype=float,
    )
    for idx, spec in enumerate(specs):
        desired[idx] = min(desired[idx], spec.qty_upper_bound)
        if spec.integral_target:
            desired[idx] = float(math.floor(desired[idx] + 1e-12))
    required = sum(
        max(0.0, float(desired[idx]) - float(spec.current_same_side_qty)) * float(spec.buying_power_price)
        for idx, spec in enumerate(specs)
    )
    scale = min(1.0, safe_cap / required) if required > EPS else 1.0
    fallback = np.asarray(
        [
            min(
                spec.qty_upper_bound,
                spec.current_same_side_qty
                + max(0.0, desired[idx] - spec.current_same_side_qty) * scale,
            )
            if desired[idx] > spec.current_same_side_qty
            else desired[idx]
            for idx, spec in enumerate(specs)
        ],
        dtype=float,
    )
    for idx, spec in enumerate(specs):
        if spec.integral_target:
            fallback[idx] = float(math.floor(fallback[idx] + 1e-12))
    solver_diag["fallback_used"] = True
    solver_diag["fallback_entry_scale"] = float(scale)
    return fallback, solver_diag


def _summarize_solution(
    specs: Sequence[_TargetSpec],
    target_qty: Sequence[float],
    *,
    account_equity: float,
    buying_power_cap: float,
) -> dict[str, Any]:
    equity = max(float(account_equity), 1e-9)
    used = 0.0
    l1 = 0.0
    l2_sq = 0.0
    max_abs_weight_gap = 0.0
    max_relative = 0.0
    integer_rounding_loss = 0.0
    for spec, qty_raw in zip(specs, target_qty):
        qty = max(0.0, float(qty_raw))
        expected_qty = 0.0 if qty <= EPS else max(0.0, qty + spec.short_position_residual_qty)
        actual_notional = expected_qty * spec.reference_price
        gap = actual_notional - spec.desired_notional
        weight_gap = gap / equity
        l1 += abs(weight_gap)
        l2_sq += weight_gap * weight_gap
        max_abs_weight_gap = max(max_abs_weight_gap, abs(weight_gap))
        max_relative = max(max_relative, abs(gap) / max(spec.desired_notional, 1e-9))
        entry_qty = max(0.0, qty - spec.current_same_side_qty)
        used += entry_qty * spec.buying_power_price
        if spec.side == "short" and spec.integral_target:
            integer_rounding_loss += abs(gap)
    cap = max(0.0, float(buying_power_cap))
    return {
        "estimated_entry_buying_power_used": float(used),
        "buying_power_cap": float(cap),
        "buying_power_cap_utilization": float(used / cap) if cap > EPS else 0.0,
        "tracking_error_l1_weight": float(l1),
        "tracking_error_l2_weight": float(math.sqrt(l2_sq)),
        "tracking_error_l1_weight_pct": float(l1 * 100.0),
        "mean_abs_symbol_weight_error": float(l1 / len(specs)) if specs else 0.0,
        "mean_abs_symbol_weight_error_pct": float((l1 / len(specs)) * 100.0) if specs else 0.0,
        "max_abs_symbol_weight_error": float(max_abs_weight_gap),
        "max_abs_symbol_weight_error_pct": float(max_abs_weight_gap * 100.0),
        "max_symbol_relative_target_error": float(max_relative),
        "integer_short_absolute_notional_gap": float(integer_rounding_loss),
    }


def _summarize_projection_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    buying_power_cap: float,
) -> dict[str, Any]:
    used = sum(_safe_float(row.get("estimated_entry_buying_power")) for row in rows)
    weight_gaps = [_safe_float(row.get("projection_weight_gap")) for row in rows]
    max_relative = 0.0
    integer_gap = 0.0
    for row in rows:
        raw_notional = abs(_safe_float(row.get("raw_target_notional")))
        gap = abs(_safe_float(row.get("projection_notional_gap")))
        if raw_notional > EPS:
            max_relative = max(max_relative, gap / raw_notional)
        if str(row.get("target_side")) == "short" and bool(row.get("integer_target_required")):
            integer_gap += gap
    cap = max(0.0, float(buying_power_cap))
    l1 = float(sum(abs(value) for value in weight_gaps))
    mean_abs = float(l1 / len(weight_gaps)) if weight_gaps else 0.0
    max_abs = float(max((abs(value) for value in weight_gaps), default=0.0))
    return {
        "estimated_entry_buying_power_used": float(used),
        "buying_power_cap": float(cap),
        "buying_power_cap_utilization": float(used / cap) if cap > EPS else 0.0,
        "tracking_error_l1_weight": l1,
        "tracking_error_l2_weight": float(math.sqrt(sum(value * value for value in weight_gaps))),
        "tracking_error_l1_weight_pct": float(l1 * 100.0),
        "mean_abs_symbol_weight_error": mean_abs,
        "mean_abs_symbol_weight_error_pct": float(mean_abs * 100.0),
        "max_abs_symbol_weight_error": max_abs,
        "max_abs_symbol_weight_error_pct": float(max_abs * 100.0),
        "max_symbol_relative_target_error": float(max_relative),
        "integer_short_absolute_notional_gap": float(integer_gap),
    }


def project_executable_targets(
    *,
    raw_target_signed_weights: Mapping[str, float],
    current_signed_qty: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    reference_prices: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    account_equity: float,
    buying_power: float,
    buying_power_buffer: float,
    min_trade_notional: float,
    qty_decimals: int,
    whole_shares_only: bool,
    short_sales_whole_shares_only: bool,
    shorting_enabled: bool,
    sizing_adverse_offset_bps: float,
    short_buying_power_adverse_offset_bps: float,
    scenario_buffers: Sequence[float] = (0.85, 0.90, 0.95),
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    equity = max(float(account_equity), 1e-9)
    buffer = min(max(float(buying_power_buffer), 0.0), 1.0)
    cap = max(0.0, float(buying_power)) * buffer
    specs, blocked = _build_target_specs(
        raw_target_signed_weights=raw_target_signed_weights,
        current_signed_qty=current_signed_qty,
        current_signed_notional=current_signed_notional,
        reference_prices=reference_prices,
        assets_by_symbol=assets_by_symbol,
        account_equity=equity,
        shorting_enabled=shorting_enabled,
        whole_shares_only=whole_shares_only,
        short_sales_whole_shares_only=short_sales_whole_shares_only,
        sizing_adverse_offset_bps=sizing_adverse_offset_bps,
        short_buying_power_adverse_offset_bps=short_buying_power_adverse_offset_bps,
    )
    solved_qty, solver_diag = _solve_projection(specs, buying_power_cap=cap)

    order_target_weights: dict[str, float] = {}
    target_lattice_signed_qty: dict[str, float] = {}
    executable_expected_weights: dict[str, float] = {}
    symbol_rows: list[dict[str, Any]] = []

    for spec, qty_raw in zip(specs, solved_qty):
        qty = max(0.0, min(float(qty_raw), float(spec.qty_upper_bound)))
        if spec.integral_target:
            qty = float(round(qty))
        else:
            qty = _quantize_down(qty, qty_decimals)
        expected_abs_qty = 0.0 if qty <= EPS else max(0.0, qty + spec.short_position_residual_qty)
        expected_signed_qty = expected_abs_qty if spec.side == "long" else -expected_abs_qty
        expected_notional = expected_signed_qty * spec.reference_price
        raw_target_notional = spec.raw_weight * equity
        estimated_delta_notional = expected_notional - spec.current_signed_notional
        reasons = list(spec.constraints)
        carried_by_min_trade = False

        if abs(estimated_delta_notional) < float(min_trade_notional):
            reasons.append("carried_by_min_trade_notional")
            carried_by_min_trade = True
            if spec.side == "short" and spec.current_signed_qty < -EPS:
                qty = float(spec.current_same_side_qty)
            elif spec.side == "long" and spec.current_signed_qty > EPS:
                qty = float(spec.current_signed_qty)
            else:
                qty = 0.0
            expected_signed_qty = float(spec.current_signed_qty)
            expected_notional = float(spec.current_signed_notional)
            estimated_delta_notional = 0.0

        if carried_by_min_trade:
            target_lattice_signed_qty[spec.symbol] = float(-qty) if spec.side == "short" else float(qty)
            if abs(spec.current_signed_notional) > EPS:
                order_target_weights[spec.symbol] = float(spec.current_signed_notional / equity)
        elif spec.side == "short":
            target_lattice_signed_qty[spec.symbol] = float(-qty) if qty > EPS else 0.0
            if qty > EPS:
                current_anchor = float(spec.current_same_side_qty)
                side = "sell" if qty > current_anchor + EPS else "buy"
                sizing_price = _adverse_price(
                    side=side,
                    reference_price=spec.reference_price,
                    offset_bps=sizing_adverse_offset_bps,
                )
                order_target_weights[spec.symbol] = float(-(qty * sizing_price) / equity)
        else:
            target_lattice_signed_qty[spec.symbol] = float(qty)
            if qty > EPS:
                delta_qty = float(qty - spec.current_signed_qty)
                side = "buy" if delta_qty >= 0 else "sell"
                sizing_price = _adverse_price(
                    side=side,
                    reference_price=spec.reference_price,
                    offset_bps=sizing_adverse_offset_bps,
                )
                order_target_notional = spec.current_signed_notional + delta_qty * sizing_price
                order_target_weights[spec.symbol] = float(order_target_notional / equity)

        if abs(expected_notional) > EPS:
            executable_expected_weights[spec.symbol] = float(expected_notional / equity)
        entry_qty = max(0.0, qty - spec.current_same_side_qty)
        entry_bp = entry_qty * spec.buying_power_price
        symbol_rows.append(
            {
                "symbol": spec.symbol,
                "target_side": spec.side,
                "raw_target_signed_weight": float(spec.raw_weight),
                "raw_target_notional": float(raw_target_notional),
                "reference_price": float(spec.reference_price),
                "current_signed_qty": float(spec.current_signed_qty),
                "current_signed_notional": float(spec.current_signed_notional),
                "raw_target_abs_qty": float(spec.desired_notional / spec.reference_price),
                "target_lattice_abs_qty": float(qty),
                "target_lattice_signed_qty": float(target_lattice_signed_qty[spec.symbol]),
                "short_position_residual_qty": float(spec.short_position_residual_qty),
                "expected_final_signed_qty": float(expected_signed_qty),
                "executable_expected_signed_weight": float(expected_notional / equity),
                "projection_weight_gap": float((expected_notional - raw_target_notional) / equity),
                "projection_notional_gap": float(expected_notional - raw_target_notional),
                "estimated_entry_qty": float(entry_qty),
                "estimated_entry_buying_power": float(entry_bp),
                "buying_power_price": float(spec.buying_power_price),
                "integer_target_required": bool(spec.integral_target),
                "constraint_reasons": reasons,
            }
        )

    for item in blocked:
        symbol = str(item["symbol"])
        current_qty = _safe_float(item.get("current_signed_qty"))
        current_notional = _safe_float(item.get("current_signed_notional"))
        if abs(current_notional) > EPS:
            executable_expected_weights[symbol] = float(current_notional / equity)
            order_target_weights[symbol] = float(current_notional / equity)
        target_lattice_signed_qty[symbol] = float(current_qty)
        raw_weight = _safe_float(item.get("raw_target_signed_weight"))
        symbol_rows.append(
            {
                "symbol": symbol,
                "target_side": "long" if raw_weight >= 0 else "short",
                "raw_target_signed_weight": float(raw_weight),
                "raw_target_notional": float(raw_weight * equity),
                "reference_price": None,
                "current_signed_qty": float(current_qty),
                "current_signed_notional": float(current_notional),
                "raw_target_abs_qty": None,
                "target_lattice_abs_qty": abs(float(current_qty)),
                "target_lattice_signed_qty": float(current_qty),
                "short_position_residual_qty": 0.0,
                "expected_final_signed_qty": float(current_qty),
                "executable_expected_signed_weight": float(current_notional / equity),
                "projection_weight_gap": float((current_notional / equity) - raw_weight),
                "projection_notional_gap": float(current_notional - raw_weight * equity),
                "estimated_entry_qty": 0.0,
                "estimated_entry_buying_power": 0.0,
                "buying_power_price": None,
                "integer_target_required": bool(raw_weight < 0 and short_sales_whole_shares_only),
                "constraint_reasons": [str(item.get("reason") or "blocked")],
            }
        )

    targeted = {spec.symbol for spec in specs} | {str(item["symbol"]) for item in blocked}
    for symbol in sorted(set(current_signed_qty) - targeted):
        current_qty = _safe_float(current_signed_qty.get(symbol))
        current_notional = _safe_float(current_signed_notional.get(symbol))
        if abs(current_qty) <= EPS and abs(current_notional) <= EPS:
            continue
        target_lattice_signed_qty[str(symbol).upper()] = 0.0
        symbol_rows.append(
            {
                "symbol": str(symbol).upper(),
                "target_side": "flat",
                "raw_target_signed_weight": 0.0,
                "raw_target_notional": 0.0,
                "reference_price": _safe_float(reference_prices.get(symbol))
                if _safe_float(reference_prices.get(symbol)) > EPS
                else None,
                "current_signed_qty": float(current_qty),
                "current_signed_notional": float(current_notional),
                "raw_target_abs_qty": 0.0,
                "target_lattice_abs_qty": 0.0,
                "target_lattice_signed_qty": 0.0,
                "short_position_residual_qty": 0.0,
                "expected_final_signed_qty": 0.0,
                "executable_expected_signed_weight": 0.0,
                "projection_weight_gap": 0.0,
                "projection_notional_gap": 0.0,
                "estimated_entry_qty": 0.0,
                "estimated_entry_buying_power": 0.0,
                "buying_power_price": None,
                "integer_target_required": False,
                "constraint_reasons": ["raw_target_zero_release"],
            }
        )

    optimizer_pre_filter_summary = _summarize_solution(
        specs,
        solved_qty,
        account_equity=equity,
        buying_power_cap=cap,
    )
    actual_summary = _summarize_projection_rows(symbol_rows, buying_power_cap=cap)
    scenario_rows: list[dict[str, Any]] = []
    scenario_values = sorted({min(max(float(value), 0.0), 1.0) for value in scenario_buffers} | {buffer})
    for scenario_buffer in scenario_values:
        scenario_cap = max(0.0, float(buying_power)) * scenario_buffer
        scenario_qty, scenario_solver = _solve_projection(specs, buying_power_cap=scenario_cap)
        scenario_rows.append(
            {
                "buffer": float(scenario_buffer),
                **_summarize_solution(specs, scenario_qty, account_equity=equity, buying_power_cap=scenario_cap),
                "solver_success": bool(scenario_solver.get("success")),
                "solver_status": scenario_solver.get("status"),
            }
        )

    raw_long_gross = sum(max(0.0, _safe_float(value)) for value in raw_target_signed_weights.values())
    raw_short_gross = sum(max(0.0, -_safe_float(value)) for value in raw_target_signed_weights.values())
    executable_long_gross = sum(max(0.0, value) for value in executable_expected_weights.values())
    executable_short_gross = sum(max(0.0, -value) for value in executable_expected_weights.values())
    diagnostics = {
        "schema_version": "1.0",
        "optimizer": "executable_target_projector",
        "account_equity": float(equity),
        "buying_power": float(buying_power),
        "buying_power_buffer": float(buffer),
        "buying_power_cap": float(cap),
        "min_trade_notional": float(min_trade_notional),
        "qty_decimals": int(qty_decimals),
        "whole_shares_only": bool(whole_shares_only),
        "short_sales_whole_shares_only": bool(short_sales_whole_shares_only),
        "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
        "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
        "raw_long_gross_weight": float(raw_long_gross),
        "raw_short_gross_weight": float(raw_short_gross),
        "executable_long_gross_weight": float(executable_long_gross),
        "executable_short_gross_weight": float(executable_short_gross),
        "solver": solver_diag,
        "optimizer_pre_min_trade_summary": optimizer_pre_filter_summary,
        **actual_summary,
        "blocked_target_count": int(len(blocked)),
        "integer_short_target_count": int(sum(spec.side == "short" and spec.integral_target for spec in specs)),
        "symbol_count": int(len(symbol_rows)),
        "symbols": sorted(symbol_rows, key=lambda row: str(row["symbol"])),
        "buying_power_buffer_scenarios": scenario_rows,
        "executable_expected_signed_weights": dict(sorted(executable_expected_weights.items())),
        "target_lattice_signed_qty": dict(sorted(target_lattice_signed_qty.items())),
    }
    return dict(sorted(order_target_weights.items())), dict(sorted(target_lattice_signed_qty.items())), diagnostics
