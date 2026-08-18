from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


EPS = 1e-10
REGT_INITIAL_MARGIN_FLOOR_RATE = 0.50
MISSING_MARGIN_REQUIREMENT_RATE = 1.00


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
    initial_margin_rate: float
    initial_margin_price: float
    initial_margin_requirement_source: str
    broker_margin_requirement_rate: float | None
    marginable: bool | None
    beta: float | None
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


def _optional_rate(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate < 0.0:
        return None
    return float(rate / 100.0 if rate > 2.0 else rate)


def resolve_initial_margin_requirement(
    *,
    asset: Mapping[str, Any] | None,
    side: str,
    reference_price: float,
    regt_floor_rate: float = REGT_INITIAL_MARGIN_FLOOR_RATE,
) -> dict[str, Any]:
    """Resolve a conservative side-specific initial-margin coefficient.

    Alpaca exposes side-specific maintenance requirements on the asset record.
    Reg T initial margin still has a 50% floor for ordinary marginable equities,
    while non-marginable, broker-special, and low-priced short positions can be
    more expensive.  Missing metadata is intentionally fail-closed at 100%.
    """

    normalized_side = "short" if str(side).lower() == "short" else "long"
    floor_rate = max(0.0, float(regt_floor_rate))
    payload = dict(asset or {})
    asset_available = bool(payload)
    marginable_raw = payload.get("marginable")
    marginable = bool(marginable_raw) if marginable_raw is not None else None
    side_key = f"margin_requirement_{normalized_side}"
    side_rate = _optional_rate(payload.get(side_key))
    maintenance_rate = _optional_rate(payload.get("maintenance_margin_requirement"))
    broker_rate = side_rate if side_rate is not None else maintenance_rate

    candidates: list[tuple[float, str]] = [(floor_rate, "regt_initial_margin_floor")]
    if broker_rate is not None:
        candidates.append(
            (
                float(broker_rate),
                f"alpaca_asset.{side_key}"
                if side_rate is not None
                else "alpaca_asset.maintenance_margin_requirement",
            )
        )
    if marginable is False:
        candidates.append((1.0, "alpaca_asset.non_marginable"))
    if not asset_available or broker_rate is None:
        candidates.append(
            (MISSING_MARGIN_REQUIREMENT_RATE, "missing_asset_margin_metadata_fail_closed")
        )

    price = max(0.0, _safe_float(reference_price))
    if normalized_side == "short" and 0.0 < price < 5.0:
        low_price_rate = max(1.0, 2.50 / price)
        candidates.append((float(low_price_rate), "regt_low_price_short_rule"))

    effective_rate, source = max(candidates, key=lambda item: item[0])
    return {
        "side": normalized_side,
        "initial_margin_rate": float(effective_rate),
        "initial_margin_requirement_pct": float(effective_rate * 100.0),
        "initial_margin_requirement_source": str(source),
        "broker_margin_requirement_rate": broker_rate,
        "broker_margin_requirement_pct": (
            float(broker_rate * 100.0) if broker_rate is not None else None
        ),
        "marginable": marginable,
        "asset_margin_metadata_available": bool(asset_available),
        "regt_initial_margin_floor_rate": float(floor_rate),
    }


def _projected_whole_qty(raw_qty: float, *, integer_tolerance: float = 0.20) -> float:
    if raw_qty <= EPS:
        return 0.0
    nearest = round(float(raw_qty))
    if nearest > 0 and abs(float(raw_qty) - float(nearest)) <= float(integer_tolerance):
        return float(nearest)
    return float(math.floor(float(raw_qty) + 1e-12))


def _quantize_down(value: float, decimals: int) -> float:
    scale = 10 ** max(0, int(decimals))
    scaled = max(0.0, float(value)) * scale
    nearest = round(scaled)
    if abs(scaled - nearest) <= 1e-2:
        scaled = float(nearest)
    return float(math.floor(scaled + 1e-9) / scale)


def _build_target_specs(
    *,
    raw_target_signed_weights: Mapping[str, float],
    current_signed_qty: Mapping[str, float],
    current_signed_notional: Mapping[str, float],
    reference_prices: Mapping[str, float],
    assets_by_symbol: Mapping[str, Mapping[str, Any]],
    target_beta_by_symbol: Mapping[str, float] | None,
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
        side = "long" if weight > 0 else "short"
        asset = assets_by_symbol.get(symbol, {}) or {}
        margin_reference_price = price
        if margin_reference_price <= EPS and abs(current_qty) > EPS:
            margin_reference_price = abs(current_notional / current_qty)
        current_side = "short" if current_notional < -EPS or current_qty < -EPS else "long"
        margin_side = current_side if price <= EPS and abs(current_notional) > EPS else side
        margin_requirement = resolve_initial_margin_requirement(
            asset=asset,
            side=margin_side,
            reference_price=margin_reference_price,
        )
        beta_value = (
            _safe_float(target_beta_by_symbol.get(symbol), default=float("nan"))
            if target_beta_by_symbol is not None
            else float("nan")
        )
        beta = float(beta_value) if math.isfinite(beta_value) else None
        if price <= EPS:
            blocked.append(
                {
                    "symbol": symbol,
                    "raw_target_signed_weight": float(weight),
                    "current_signed_qty": float(current_qty),
                    "current_signed_notional": float(current_notional),
                    "carried_side": current_side,
                    "initial_margin_rate": margin_requirement["initial_margin_rate"],
                    "initial_margin_requirement_pct": margin_requirement[
                        "initial_margin_requirement_pct"
                    ],
                    "initial_margin_requirement_source": margin_requirement[
                        "initial_margin_requirement_source"
                    ],
                    "broker_margin_requirement_rate": margin_requirement[
                        "broker_margin_requirement_rate"
                    ],
                    "marginable": margin_requirement["marginable"],
                    "projected_initial_margin": float(
                        abs(current_notional)
                        * float(margin_requirement["initial_margin_rate"])
                    ),
                    "beta": beta,
                    "reason": "missing_reference_price",
                }
            )
            continue

        desired_notional = abs(float(weight)) * equity
        desired_qty = desired_notional / price
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

        margin_requirement = resolve_initial_margin_requirement(
            asset=asset,
            side=side,
            reference_price=price,
        )
        initial_margin_rate = float(margin_requirement["initial_margin_rate"])
        initial_margin_source = str(
            margin_requirement["initial_margin_requirement_source"]
        )
        if initial_margin_rate > REGT_INITIAL_MARGIN_FLOOR_RATE + EPS:
            constraints.append("elevated_initial_margin_requirement")
        if initial_margin_source == "missing_asset_margin_metadata_fail_closed":
            constraints.append("margin_metadata_missing_fail_closed")

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
                initial_margin_rate=initial_margin_rate,
                initial_margin_price=float(bp_price),
                initial_margin_requirement_source=initial_margin_source,
                broker_margin_requirement_rate=margin_requirement[
                    "broker_margin_requirement_rate"
                ],
                marginable=margin_requirement["marginable"],
                beta=beta,
                constraints=constraints,
            )
        )
    return specs, blocked


def _solve_projection(
    specs: Sequence[_TargetSpec],
    *,
    buying_power_cap: float,
    gross_notional_cap: float | None = None,
    fixed_gross_notional: float = 0.0,
    initial_margin_cap: float | None = None,
    fixed_initial_margin: float = 0.0,
    beta_abs_limit: float | None = None,
    fixed_beta_exposure: float = 0.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    count = len(specs)
    safe_gross_cap = (
        max(0.0, float(gross_notional_cap))
        if gross_notional_cap is not None
        else None
    )
    safe_fixed_gross = max(0.0, float(fixed_gross_notional))
    safe_margin_cap = (
        max(0.0, float(initial_margin_cap))
        if initial_margin_cap is not None
        else None
    )
    safe_fixed_margin = max(0.0, float(fixed_initial_margin))
    safe_beta_limit = (
        max(0.0, float(beta_abs_limit))
        if beta_abs_limit is not None
        else None
    )
    if count == 0:
        gross_feasible = safe_gross_cap is None or safe_fixed_gross <= safe_gross_cap + 1e-6
        margin_feasible = (
            safe_margin_cap is None or safe_fixed_margin <= safe_margin_cap + 1e-6
        )
        beta_feasible = (
            safe_beta_limit is None
            or abs(float(fixed_beta_exposure)) <= safe_beta_limit + 1e-10
        )
        feasible = bool(gross_feasible and margin_feasible and beta_feasible)
        return np.zeros(0, dtype=float), {
            "success": bool(feasible),
            "status": 0 if feasible else 2,
            "message": "no_optimizable_targets" if feasible else "fixed_gross_exceeds_capacity_cap",
            "solver": "scipy.optimize.milp/highs",
            "gross_notional_cap": safe_gross_cap,
            "fixed_gross_notional": safe_fixed_gross,
            "initial_margin_cap": safe_margin_cap,
            "fixed_initial_margin": safe_fixed_margin,
            "beta_abs_limit": safe_beta_limit,
            "fixed_beta_exposure": float(fixed_beta_exposure),
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
        margin_bp_multiplier = max(
            1.0,
            float(spec.initial_margin_rate) / REGT_INITIAL_MARGIN_FLOOR_RATE,
        )
        bp_row[e0 + idx] = (
            float(spec.buying_power_price) * margin_bp_multiplier / normalizer
        )
    rows.append(bp_row)
    row_lower.append(-np.inf)
    row_upper.append(safe_cap / normalizer)

    conservative_residual_gross = sum(
        max(0.0, float(spec.short_position_residual_qty)) * float(spec.reference_price)
        for spec in specs
    )
    if safe_gross_cap is not None:
        gross_row = np.zeros(variable_count, dtype=float)
        gross_normalizer = max(safe_gross_cap, 1.0)
        for idx, spec in enumerate(specs):
            gross_row[q0 + idx] = float(spec.reference_price) / gross_normalizer
        rows.append(gross_row)
        row_lower.append(-np.inf)
        row_upper.append(
            (safe_gross_cap - safe_fixed_gross - conservative_residual_gross)
            / gross_normalizer
        )

    conservative_residual_initial_margin = sum(
        max(0.0, float(spec.short_position_residual_qty))
        * float(spec.initial_margin_price)
        * float(spec.initial_margin_rate)
        for spec in specs
    )
    if safe_margin_cap is not None:
        margin_row = np.zeros(variable_count, dtype=float)
        margin_normalizer = max(safe_margin_cap, 1.0)
        for idx, spec in enumerate(specs):
            margin_row[q0 + idx] = (
                float(spec.initial_margin_price)
                * float(spec.initial_margin_rate)
                / margin_normalizer
            )
        rows.append(margin_row)
        row_lower.append(-np.inf)
        row_upper.append(
            (
                safe_margin_cap
                - safe_fixed_margin
                - conservative_residual_initial_margin
            )
            / margin_normalizer
        )

    beta_constant = float(fixed_beta_exposure)
    beta_constraint_enforced = bool(
        safe_beta_limit is not None and all(spec.beta is not None for spec in specs)
    )
    if beta_constraint_enforced:
        beta_row = np.zeros(variable_count, dtype=float)
        for idx, spec in enumerate(specs):
            target_weight = abs(float(spec.raw_weight))
            equity = max(
                float(spec.desired_notional) / max(target_weight, 1e-12),
                1e-9,
            )
            signed_beta_per_qty = (
                float(spec.beta)
                * float(spec.reference_price)
                / equity
                * (1.0 if spec.side == "long" else -1.0)
            )
            beta_row[q0 + idx] = signed_beta_per_qty
            if spec.side == "short":
                beta_constant -= (
                    float(spec.beta)
                    * float(spec.reference_price)
                    * float(spec.short_position_residual_qty)
                    / equity
                )
        rows.append(beta_row)
        row_lower.append(-safe_beta_limit - beta_constant)
        row_upper.append(safe_beta_limit - beta_constant)

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
    secondary_value: float | None = None
    secondary_gross_weight: float | None = None
    tertiary_used = False
    tertiary_value: float | None = None
    if primary_result.success and primary_result.x is not None and math.isfinite(primary_value):
        # Lock the best weight error, then maximize executable target gross.
        # This makes buying-power utilization a true secondary objective.
        secondary_objective = np.zeros(variable_count, dtype=float)
        gross_weight_row = np.zeros(variable_count, dtype=float)
        for idx, spec in enumerate(specs):
            equity = max(
                float(spec.desired_notional) / max(abs(float(spec.raw_weight)), 1e-12),
                1e-9,
            )
            gross_weight_row[q0 + idx] = float(spec.reference_price) / equity
            secondary_objective[q0 + idx] = -gross_weight_row[q0 + idx]
            secondary_objective[e0 + idx] = 1e-10
        tolerance = max(1e-12, abs(primary_value) * 1e-10)
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
            secondary_value = _safe_float(secondary_result.fun, default=float("nan"))
            secondary_gross_weight = float(np.dot(gross_weight_row, secondary_result.x))

            # Resolve exact L1/gross ties by reducing the worst single-name gap.
            # Tight locks and quantity-grid snapping keep this tie-break from
            # perturbing executable fractional quantities.
            gross_tolerance = max(1e-12, abs(secondary_gross_weight) * 1e-10)
            gross_lock = LinearConstraint(
                gross_weight_row.reshape(1, -1),
                np.asarray([secondary_gross_weight - gross_tolerance]),
                np.asarray([np.inf]),
            )
            tertiary_objective = np.zeros(variable_count, dtype=float)
            tertiary_objective[z_idx] = 1.0
            tertiary_result = milp(
                c=tertiary_objective,
                integrality=integrality,
                bounds=Bounds(lower, upper),
                constraints=(base_constraint, primary_lock, gross_lock),
                options={"time_limit": 10.0, "mip_rel_gap": 1e-9},
            )
            if tertiary_result.success and tertiary_result.x is not None:
                result = tertiary_result
                tertiary_used = True
                tertiary_value = _safe_float(
                    tertiary_result.fun,
                    default=float("nan"),
                )
    solver_diag = {
        "success": bool(result.success),
        "status": int(result.status),
        "message": str(result.message),
        "solver": "scipy.optimize.milp/highs",
        "objective_priority": [
            "minimize_absolute_weight_error",
            "maximize_executable_target_gross_without_worsening_weight_error",
            "minimize_max_single_name_weight_error_only_within_exact_higher_priority_ties",
        ],
        "primary_weight_error_objective": float(primary_value),
        "secondary_optimization_used": bool(secondary_used),
        "secondary_objective_value": secondary_value,
        "secondary_gross_weight": secondary_gross_weight,
        "tertiary_optimization_used": bool(tertiary_used),
        "tertiary_max_single_name_weight_error": tertiary_value,
        "mip_gap": _safe_float(getattr(result, "mip_gap", None), default=float("nan")),
        "mip_node_count": int(_safe_float(getattr(result, "mip_node_count", 0))),
        "gross_notional_cap": safe_gross_cap,
        "fixed_gross_notional": float(safe_fixed_gross),
        "conservative_short_residual_gross_notional": float(conservative_residual_gross),
        "initial_margin_cap": safe_margin_cap,
        "fixed_initial_margin": float(safe_fixed_margin),
        "conservative_short_residual_initial_margin": float(
            conservative_residual_initial_margin
        ),
        "beta_constraint_enforced": beta_constraint_enforced,
        "beta_abs_limit": safe_beta_limit,
        "fixed_beta_exposure": float(fixed_beta_exposure),
        "beta_constraint_constant": float(beta_constant),
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
        max(0.0, float(desired[idx]) - float(spec.current_same_side_qty))
        * float(spec.buying_power_price)
        * max(1.0, float(spec.initial_margin_rate) / REGT_INITIAL_MARGIN_FLOOR_RATE)
        for idx, spec in enumerate(specs)
    )
    entry_scale = min(1.0, safe_cap / required) if required > EPS else 1.0
    fallback = np.asarray(
        [
            min(
                spec.qty_upper_bound,
                spec.current_same_side_qty
                + max(0.0, desired[idx] - spec.current_same_side_qty) * entry_scale,
            )
            if desired[idx] > spec.current_same_side_qty
            else desired[idx]
            for idx, spec in enumerate(specs)
        ],
        dtype=float,
    )
    gross_scale = 1.0
    if safe_gross_cap is not None:
        active_residual_gross = sum(
            float(spec.short_position_residual_qty) * float(spec.reference_price)
            for idx, spec in enumerate(specs)
            if fallback[idx] > EPS
        )
        variable_gross = sum(
            max(0.0, float(fallback[idx])) * float(spec.reference_price)
            for idx, spec in enumerate(specs)
        )
        available_for_variable = max(
            0.0,
            safe_gross_cap - safe_fixed_gross - active_residual_gross,
        )
        if variable_gross > available_for_variable + 1e-6:
            gross_scale = available_for_variable / max(variable_gross, 1e-9)
            fallback *= gross_scale
    margin_scale = 1.0
    if safe_margin_cap is not None:
        active_residual_margin = sum(
            float(spec.short_position_residual_qty)
            * float(spec.initial_margin_price)
            * float(spec.initial_margin_rate)
            for idx, spec in enumerate(specs)
            if fallback[idx] > EPS
        )
        variable_margin = sum(
            max(0.0, float(fallback[idx]))
            * float(spec.initial_margin_price)
            * float(spec.initial_margin_rate)
            for idx, spec in enumerate(specs)
        )
        available_margin = max(
            0.0,
            safe_margin_cap - safe_fixed_margin - active_residual_margin,
        )
        if variable_margin > available_margin + 1e-6:
            margin_scale = available_margin / max(variable_margin, 1e-9)
            fallback *= margin_scale
    for idx, spec in enumerate(specs):
        if spec.integral_target:
            fallback[idx] = float(math.floor(fallback[idx] + 1e-12))
    solver_diag["fallback_used"] = True
    solver_diag["fallback_entry_scale"] = float(entry_scale)
    solver_diag["fallback_gross_scale"] = float(gross_scale)
    solver_diag["fallback_margin_scale"] = float(margin_scale)
    return fallback, solver_diag


def _summarize_solution(
    specs: Sequence[_TargetSpec],
    target_qty: Sequence[float],
    *,
    account_equity: float,
    buying_power_cap: float,
    gross_notional_cap: float | None = None,
    fixed_gross_notional: float = 0.0,
    initial_margin_cap: float | None = None,
    fixed_initial_margin: float = 0.0,
    total_buying_power_capacity: float | None = None,
    fixed_beta_exposure: float = 0.0,
    beta_abs_limit: float | None = None,
) -> dict[str, Any]:
    equity = max(float(account_equity), 1e-9)
    used = 0.0
    l1 = 0.0
    l2_sq = 0.0
    long_l1 = 0.0
    short_l1 = 0.0
    max_abs_weight_gap = 0.0
    max_relative = 0.0
    integer_rounding_loss = 0.0
    projected_gross = max(0.0, float(fixed_gross_notional))
    projected_initial_margin = max(0.0, float(fixed_initial_margin))
    projected_net_beta = float(fixed_beta_exposure)
    beta_coverage_complete = True
    for spec, qty_raw in zip(specs, target_qty):
        qty = max(0.0, float(qty_raw))
        expected_qty = 0.0 if qty <= EPS else max(0.0, qty + spec.short_position_residual_qty)
        actual_notional = expected_qty * spec.reference_price
        projected_gross += abs(actual_notional)
        gap = actual_notional - spec.desired_notional
        weight_gap = gap / equity
        l1 += abs(weight_gap)
        if spec.side == "short":
            short_l1 += abs(weight_gap)
        else:
            long_l1 += abs(weight_gap)
        l2_sq += weight_gap * weight_gap
        max_abs_weight_gap = max(max_abs_weight_gap, abs(weight_gap))
        max_relative = max(max_relative, abs(gap) / max(spec.desired_notional, 1e-9))
        entry_qty = max(0.0, qty - spec.current_same_side_qty)
        margin_bp_multiplier = max(
            1.0,
            float(spec.initial_margin_rate) / REGT_INITIAL_MARGIN_FLOOR_RATE,
        )
        used += entry_qty * spec.buying_power_price * margin_bp_multiplier
        projected_initial_margin += (
            expected_qty
            * float(spec.initial_margin_price)
            * float(spec.initial_margin_rate)
        )
        if spec.beta is None:
            beta_coverage_complete = False
        else:
            projected_net_beta += (
                float(spec.beta)
                * actual_notional
                / equity
                * (1.0 if spec.side == "long" else -1.0)
            )
        if spec.side == "short" and spec.integral_target:
            integer_rounding_loss += abs(gap)
    cap = max(0.0, float(buying_power_cap))
    gross_cap = (
        max(0.0, float(gross_notional_cap))
        if gross_notional_cap is not None
        else None
    )
    margin_cap = (
        max(0.0, float(initial_margin_cap))
        if initial_margin_cap is not None
        else None
    )
    total_capacity = (
        max(0.0, float(total_buying_power_capacity))
        if total_buying_power_capacity is not None
        else None
    )
    projected_regt_buying_power = (
        float(
            total_capacity
            - projected_initial_margin / REGT_INITIAL_MARGIN_FLOOR_RATE
        )
        if total_capacity is not None
        else None
    )
    return {
        "estimated_entry_buying_power_used": float(used),
        "buying_power_cap": float(cap),
        "buying_power_cap_utilization": float(used / cap) if cap > EPS else 0.0,
        "tracking_error_l1_weight": float(l1),
        "tracking_error_long_l1_weight": float(long_l1),
        "tracking_error_short_l1_weight": float(short_l1),
        "tracking_error_l2_weight": float(math.sqrt(l2_sq)),
        "tracking_error_l1_weight_pct": float(l1 * 100.0),
        "tracking_error_long_l1_weight_pct": float(long_l1 * 100.0),
        "tracking_error_short_l1_weight_pct": float(short_l1 * 100.0),
        "mean_abs_symbol_weight_error": float(l1 / len(specs)) if specs else 0.0,
        "mean_abs_symbol_weight_error_pct": float((l1 / len(specs)) * 100.0) if specs else 0.0,
        "max_abs_symbol_weight_error": float(max_abs_weight_gap),
        "max_abs_symbol_weight_error_pct": float(max_abs_weight_gap * 100.0),
        "max_symbol_relative_target_error": float(max_relative),
        "integer_short_absolute_notional_gap": float(integer_rounding_loss),
        "projected_final_gross_notional": float(projected_gross),
        "gross_notional_cap": gross_cap,
        "gross_notional_cap_utilization": (
            float(projected_gross / gross_cap) if gross_cap is not None and gross_cap > EPS else None
        ),
        "gross_notional_cap_slack": (
            float(gross_cap - projected_gross) if gross_cap is not None else None
        ),
        "projected_initial_margin": float(projected_initial_margin),
        "initial_margin_cap": margin_cap,
        "initial_margin_cap_utilization": (
            float(projected_initial_margin / margin_cap)
            if margin_cap is not None and margin_cap > EPS
            else None
        ),
        "initial_margin_cap_slack": (
            float(margin_cap - projected_initial_margin)
            if margin_cap is not None
            else None
        ),
        "projected_regt_buying_power": projected_regt_buying_power,
        "projected_regt_buying_power_ratio": (
            float(projected_regt_buying_power / total_capacity)
            if projected_regt_buying_power is not None and total_capacity and total_capacity > EPS
            else None
        ),
        "projected_net_beta": (
            float(projected_net_beta) if beta_coverage_complete else None
        ),
        "beta_coverage_complete": bool(beta_coverage_complete),
        "beta_abs_limit": beta_abs_limit,
        "beta_constraint_satisfied": (
            abs(float(projected_net_beta)) <= float(beta_abs_limit) + 1e-9
            if beta_coverage_complete and beta_abs_limit is not None
            else None
        ),
    }


def _summarize_projection_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    buying_power_cap: float,
    gross_notional_cap: float | None = None,
    initial_margin_cap: float | None = None,
    total_buying_power_capacity: float | None = None,
    beta_abs_limit: float | None = None,
) -> dict[str, Any]:
    used = sum(_safe_float(row.get("estimated_entry_buying_power")) for row in rows)
    weight_gaps = [_safe_float(row.get("projection_weight_gap")) for row in rows]
    long_l1 = sum(
        abs(_safe_float(row.get("projection_weight_gap")))
        for row in rows
        if str(row.get("target_side") or "").lower() == "long"
    )
    short_l1 = sum(
        abs(_safe_float(row.get("projection_weight_gap")))
        for row in rows
        if str(row.get("target_side") or "").lower() == "short"
    )
    max_relative = 0.0
    integer_gap = 0.0
    projected_gross = 0.0
    projected_initial_margin = 0.0
    projected_net_beta = 0.0
    beta_coverage_complete = True
    for row in rows:
        raw_notional = abs(
            _safe_float(
                row.get("capacity_adjusted_target_notional")
                if row.get("capacity_adjusted_target_notional") not in (None, "")
                else row.get("raw_target_notional")
            )
        )
        gap = abs(_safe_float(row.get("projection_notional_gap")))
        projected_gross += abs(_safe_float(row.get("expected_final_notional")))
        projected_initial_margin += max(
            0.0, _safe_float(row.get("projected_initial_margin"))
        )
        beta_exposure = row.get("projected_beta_exposure")
        if beta_exposure is None:
            if abs(_safe_float(row.get("expected_final_notional"))) > EPS:
                beta_coverage_complete = False
        else:
            projected_net_beta += _safe_float(beta_exposure)
        if raw_notional > EPS:
            max_relative = max(max_relative, gap / raw_notional)
        if str(row.get("target_side")) == "short" and bool(row.get("integer_target_required")):
            integer_gap += gap
    cap = max(0.0, float(buying_power_cap))
    l1 = float(sum(abs(value) for value in weight_gaps))
    mean_abs = float(l1 / len(weight_gaps)) if weight_gaps else 0.0
    max_abs = float(max((abs(value) for value in weight_gaps), default=0.0))
    gross_cap = (
        max(0.0, float(gross_notional_cap))
        if gross_notional_cap is not None
        else None
    )
    margin_cap = (
        max(0.0, float(initial_margin_cap))
        if initial_margin_cap is not None
        else None
    )
    total_capacity = (
        max(0.0, float(total_buying_power_capacity))
        if total_buying_power_capacity is not None
        else None
    )
    projected_regt_buying_power = (
        float(
            total_capacity
            - projected_initial_margin / REGT_INITIAL_MARGIN_FLOOR_RATE
        )
        if total_capacity is not None
        else None
    )
    return {
        "estimated_entry_buying_power_used": float(used),
        "buying_power_cap": float(cap),
        "buying_power_cap_utilization": float(used / cap) if cap > EPS else 0.0,
        "tracking_error_l1_weight": l1,
        "tracking_error_long_l1_weight": float(long_l1),
        "tracking_error_short_l1_weight": float(short_l1),
        "tracking_error_l2_weight": float(math.sqrt(sum(value * value for value in weight_gaps))),
        "tracking_error_l1_weight_pct": float(l1 * 100.0),
        "tracking_error_long_l1_weight_pct": float(long_l1 * 100.0),
        "tracking_error_short_l1_weight_pct": float(short_l1 * 100.0),
        "mean_abs_symbol_weight_error": mean_abs,
        "mean_abs_symbol_weight_error_pct": float(mean_abs * 100.0),
        "max_abs_symbol_weight_error": max_abs,
        "max_abs_symbol_weight_error_pct": float(max_abs * 100.0),
        "max_symbol_relative_target_error": float(max_relative),
        "integer_short_absolute_notional_gap": float(integer_gap),
        "projected_final_gross_notional": float(projected_gross),
        "gross_notional_cap": gross_cap,
        "gross_notional_cap_utilization": (
            float(projected_gross / gross_cap) if gross_cap is not None and gross_cap > EPS else None
        ),
        "gross_notional_cap_slack": (
            float(gross_cap - projected_gross) if gross_cap is not None else None
        ),
        "projected_initial_margin": float(projected_initial_margin),
        "initial_margin_cap": margin_cap,
        "initial_margin_cap_utilization": (
            float(projected_initial_margin / margin_cap)
            if margin_cap is not None and margin_cap > EPS
            else None
        ),
        "initial_margin_cap_slack": (
            float(margin_cap - projected_initial_margin)
            if margin_cap is not None
            else None
        ),
        "projected_regt_buying_power": projected_regt_buying_power,
        "projected_regt_buying_power_ratio": (
            float(projected_regt_buying_power / total_capacity)
            if projected_regt_buying_power is not None and total_capacity and total_capacity > EPS
            else None
        ),
        "projected_net_beta": (
            float(projected_net_beta) if beta_coverage_complete else None
        ),
        "beta_coverage_complete": bool(beta_coverage_complete),
        "beta_abs_limit": beta_abs_limit,
        "beta_constraint_satisfied": (
            abs(float(projected_net_beta)) <= float(beta_abs_limit) + 1e-9
            if beta_coverage_complete and beta_abs_limit is not None
            else None
        ),
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
    total_buying_power_capacity: float | None = None,
    gross_capacity_target_ratio: float = 0.95,
    target_beta_by_symbol: Mapping[str, float] | None = None,
    executable_beta_band: float = 0.01,
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    equity = max(float(account_equity), 1e-9)
    buffer = min(max(float(buying_power_buffer), 0.0), 1.0)
    cap = max(0.0, float(buying_power)) * buffer
    gross_capacity_ratio = min(max(float(gross_capacity_target_ratio), 0.0), 1.0)
    total_capacity = (
        max(0.0, float(total_buying_power_capacity))
        if total_buying_power_capacity is not None
        else None
    )
    raw_weights = {
        str(symbol).strip().upper(): _safe_float(weight)
        for symbol, weight in raw_target_signed_weights.items()
        if str(symbol).strip() and abs(_safe_float(weight)) > EPS
    }
    raw_gross_weight = sum(abs(weight) for weight in raw_weights.values())
    raw_gross_notional = raw_gross_weight * equity
    gross_target_notional = (
        total_capacity * gross_capacity_ratio
        if total_capacity is not None
        else raw_gross_notional
    )
    capacity_scale = (
        min(1.0, gross_target_notional / raw_gross_notional)
        if raw_gross_notional > EPS
        else 1.0
    )
    capacity_adjusted_weights = {
        symbol: float(weight * capacity_scale)
        for symbol, weight in raw_weights.items()
    }
    normalized_betas: dict[str, float] = {}
    if target_beta_by_symbol is not None:
        for symbol, value in target_beta_by_symbol.items():
            beta_value = _safe_float(value, default=float("nan"))
            if math.isfinite(beta_value):
                normalized_betas[str(symbol).strip().upper()] = float(beta_value)
    missing_beta_symbols = sorted(set(raw_weights) - set(normalized_betas))
    beta_coverage_complete = bool(raw_weights) and not missing_beta_symbols
    raw_target_net_beta = (
        float(sum(weight * normalized_betas[symbol] for symbol, weight in raw_weights.items()))
        if beta_coverage_complete
        else None
    )
    capacity_adjusted_target_net_beta = (
        float(
            sum(
                weight * normalized_betas[symbol]
                for symbol, weight in capacity_adjusted_weights.items()
            )
        )
        if beta_coverage_complete
        else None
    )
    beta_abs_limit = (
        max(
            max(0.0, float(executable_beta_band)),
            abs(float(capacity_adjusted_target_net_beta)) + 1e-9,
        )
        if capacity_adjusted_target_net_beta is not None
        else None
    )
    initial_margin_cap = (
        total_capacity
        * REGT_INITIAL_MARGIN_FLOOR_RATE
        * gross_capacity_ratio
        if total_capacity is not None
        else None
    )
    regt_buying_power_reserve_floor = (
        total_capacity * (1.0 - gross_capacity_ratio)
        if total_capacity is not None
        else None
    )
    specs, blocked = _build_target_specs(
        raw_target_signed_weights=capacity_adjusted_weights,
        current_signed_qty=current_signed_qty,
        current_signed_notional=current_signed_notional,
        reference_prices=reference_prices,
        assets_by_symbol=assets_by_symbol,
        target_beta_by_symbol=normalized_betas if normalized_betas else None,
        account_equity=equity,
        shorting_enabled=shorting_enabled,
        whole_shares_only=whole_shares_only,
        short_sales_whole_shares_only=short_sales_whole_shares_only,
        sizing_adverse_offset_bps=sizing_adverse_offset_bps,
        short_buying_power_adverse_offset_bps=short_buying_power_adverse_offset_bps,
    )
    fixed_gross_notional = sum(
        abs(_safe_float(item.get("current_signed_notional"))) for item in blocked
    )
    fixed_initial_margin = sum(
        max(0.0, _safe_float(item.get("projected_initial_margin")))
        for item in blocked
    )
    fixed_beta_exposure = sum(
        _safe_float(item.get("beta"))
        * _safe_float(item.get("current_signed_notional"))
        / equity
        for item in blocked
        if item.get("beta") is not None
    )
    if any(
        abs(_safe_float(item.get("current_signed_notional"))) > EPS
        and item.get("beta") is None
        for item in blocked
    ):
        beta_abs_limit = None
    solved_qty, solver_diag = _solve_projection(
        specs,
        buying_power_cap=cap,
        gross_notional_cap=gross_target_notional if total_capacity is not None else None,
        fixed_gross_notional=fixed_gross_notional,
        initial_margin_cap=initial_margin_cap,
        fixed_initial_margin=fixed_initial_margin,
        beta_abs_limit=beta_abs_limit,
        fixed_beta_exposure=fixed_beta_exposure,
    )
    optimizer_pre_filter_summary = _summarize_solution(
        specs,
        solved_qty,
        account_equity=equity,
        buying_power_cap=cap,
        gross_notional_cap=gross_target_notional if total_capacity is not None else None,
        fixed_gross_notional=fixed_gross_notional,
        initial_margin_cap=initial_margin_cap,
        fixed_initial_margin=fixed_initial_margin,
        total_buying_power_capacity=total_capacity,
        fixed_beta_exposure=fixed_beta_exposure,
        beta_abs_limit=beta_abs_limit,
    )
    running_filtered_gross = _safe_float(
        optimizer_pre_filter_summary.get("projected_final_gross_notional")
    )
    running_filtered_margin = _safe_float(
        optimizer_pre_filter_summary.get("projected_initial_margin")
    )
    running_filtered_beta = optimizer_pre_filter_summary.get("projected_net_beta")
    if running_filtered_beta is not None:
        running_filtered_beta = _safe_float(running_filtered_beta)

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
        raw_weight = _safe_float(raw_weights.get(spec.symbol))
        capacity_adjusted_weight = float(spec.raw_weight)
        raw_target_notional = raw_weight * equity
        capacity_adjusted_target_notional = capacity_adjusted_weight * equity
        estimated_delta_notional = expected_notional - spec.current_signed_notional
        reasons = list(spec.constraints)
        carried_by_min_trade = False

        if abs(estimated_delta_notional) < float(min_trade_notional):
            solver_gross = abs(float(expected_notional))
            solver_margin = (
                expected_abs_qty
                * float(spec.initial_margin_price)
                * float(spec.initial_margin_rate)
            )
            solver_beta = (
                float(spec.beta) * float(expected_notional) / equity
                if spec.beta is not None
                else None
            )
            current_gross = abs(float(spec.current_signed_notional))
            current_abs_qty = abs(float(spec.current_signed_qty))
            current_margin = (
                current_abs_qty
                * float(spec.initial_margin_price)
                * float(spec.initial_margin_rate)
            )
            current_beta = (
                float(spec.beta) * float(spec.current_signed_notional) / equity
                if spec.beta is not None
                else None
            )
            same_side_or_flat = bool(
                abs(float(spec.current_signed_notional)) <= EPS
                or float(spec.current_signed_notional) * float(expected_notional) >= -EPS
            )
            candidate_gross = running_filtered_gross - solver_gross + current_gross
            candidate_margin = running_filtered_margin - solver_margin + current_margin
            candidate_beta = (
                float(running_filtered_beta) - float(solver_beta) + float(current_beta)
                if running_filtered_beta is not None
                and solver_beta is not None
                and current_beta is not None
                else running_filtered_beta
            )
            carry_rejections: list[str] = []
            if not same_side_or_flat:
                carry_rejections.append("side_transition")
            if (
                total_capacity is not None
                and candidate_gross > gross_target_notional + 1e-6
            ):
                carry_rejections.append("gross_notional_cap")
            if (
                initial_margin_cap is not None
                and candidate_margin > initial_margin_cap + 1e-6
            ):
                carry_rejections.append("initial_margin_cap")
            if (
                beta_abs_limit is not None
                and candidate_beta is not None
                and abs(float(candidate_beta)) > float(beta_abs_limit) + 1e-9
            ):
                carry_rejections.append("beta_abs_limit")

            if carry_rejections:
                reasons.append("min_trade_notional_waived_for_hard_constraint")
                reasons.extend(
                    f"min_trade_carry_rejected_{reason}"
                    for reason in carry_rejections
                )
            else:
                reasons.append("carried_by_min_trade_notional")
                carried_by_min_trade = True
                if spec.side == "short" and spec.current_signed_qty < -EPS:
                    qty = float(spec.current_same_side_qty)
                elif spec.side == "long" and spec.current_signed_qty > EPS:
                    qty = float(spec.current_signed_qty)
                else:
                    qty = 0.0
                expected_signed_qty = float(spec.current_signed_qty)
                expected_abs_qty = abs(expected_signed_qty)
                expected_notional = float(spec.current_signed_notional)
                estimated_delta_notional = 0.0
                running_filtered_gross = float(candidate_gross)
                running_filtered_margin = float(candidate_margin)
                running_filtered_beta = candidate_beta

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
        margin_bp_multiplier = max(
            1.0,
            float(spec.initial_margin_rate) / REGT_INITIAL_MARGIN_FLOOR_RATE,
        )
        entry_bp = entry_qty * spec.buying_power_price * margin_bp_multiplier
        projected_initial_margin = (
            expected_abs_qty
            * float(spec.initial_margin_price)
            * float(spec.initial_margin_rate)
        )
        projected_beta_exposure = (
            float(spec.beta) * float(expected_notional) / equity
            if spec.beta is not None
            else None
        )
        symbol_rows.append(
            {
                "symbol": spec.symbol,
                "target_side": spec.side,
                "raw_target_signed_weight": float(raw_weight),
                "capacity_adjusted_target_signed_weight": float(capacity_adjusted_weight),
                "raw_target_notional": float(raw_target_notional),
                "capacity_adjusted_target_notional": float(capacity_adjusted_target_notional),
                "reference_price": float(spec.reference_price),
                "current_signed_qty": float(spec.current_signed_qty),
                "current_signed_notional": float(spec.current_signed_notional),
                "raw_target_abs_qty": float(abs(raw_target_notional) / spec.reference_price),
                "capacity_adjusted_target_abs_qty": float(
                    spec.desired_notional / spec.reference_price
                ),
                "target_lattice_abs_qty": float(qty),
                "target_lattice_signed_qty": float(target_lattice_signed_qty[spec.symbol]),
                "short_position_residual_qty": float(spec.short_position_residual_qty),
                "expected_final_signed_qty": float(expected_signed_qty),
                "expected_final_notional": float(expected_notional),
                "executable_expected_signed_weight": float(expected_notional / equity),
                "projection_weight_gap": float(
                    (expected_notional - capacity_adjusted_target_notional) / equity
                ),
                "projection_notional_gap": float(
                    expected_notional - capacity_adjusted_target_notional
                ),
                "raw_strategy_weight_gap": float((expected_notional - raw_target_notional) / equity),
                "raw_strategy_notional_gap": float(expected_notional - raw_target_notional),
                "estimated_entry_qty": float(entry_qty),
                "estimated_entry_buying_power": float(entry_bp),
                "buying_power_price": float(spec.buying_power_price),
                "buying_power_margin_multiplier": float(margin_bp_multiplier),
                "initial_margin_rate": float(spec.initial_margin_rate),
                "initial_margin_requirement_pct": float(
                    spec.initial_margin_rate * 100.0
                ),
                "initial_margin_requirement_source": str(
                    spec.initial_margin_requirement_source
                ),
                "broker_margin_requirement_rate": spec.broker_margin_requirement_rate,
                "broker_margin_requirement_pct": (
                    float(spec.broker_margin_requirement_rate * 100.0)
                    if spec.broker_margin_requirement_rate is not None
                    else None
                ),
                "marginable": spec.marginable,
                "initial_margin_price": float(spec.initial_margin_price),
                "projected_initial_margin": float(projected_initial_margin),
                "beta": spec.beta,
                "projected_beta_exposure": projected_beta_exposure,
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
        capacity_adjusted_weight = _safe_float(item.get("raw_target_signed_weight"))
        raw_weight = _safe_float(raw_weights.get(symbol))
        capacity_adjusted_notional = capacity_adjusted_weight * equity
        symbol_rows.append(
            {
                "symbol": symbol,
                "target_side": "long" if raw_weight >= 0 else "short",
                "raw_target_signed_weight": float(raw_weight),
                "capacity_adjusted_target_signed_weight": float(capacity_adjusted_weight),
                "raw_target_notional": float(raw_weight * equity),
                "capacity_adjusted_target_notional": float(capacity_adjusted_notional),
                "reference_price": None,
                "current_signed_qty": float(current_qty),
                "current_signed_notional": float(current_notional),
                "raw_target_abs_qty": None,
                "capacity_adjusted_target_abs_qty": None,
                "target_lattice_abs_qty": abs(float(current_qty)),
                "target_lattice_signed_qty": float(current_qty),
                "short_position_residual_qty": 0.0,
                "expected_final_signed_qty": float(current_qty),
                "expected_final_notional": float(current_notional),
                "executable_expected_signed_weight": float(current_notional / equity),
                "projection_weight_gap": float(
                    (current_notional / equity) - capacity_adjusted_weight
                ),
                "projection_notional_gap": float(
                    current_notional - capacity_adjusted_notional
                ),
                "raw_strategy_weight_gap": float((current_notional / equity) - raw_weight),
                "raw_strategy_notional_gap": float(current_notional - raw_weight * equity),
                "estimated_entry_qty": 0.0,
                "estimated_entry_buying_power": 0.0,
                "buying_power_price": None,
                "buying_power_margin_multiplier": 0.0,
                "initial_margin_rate": item.get("initial_margin_rate"),
                "initial_margin_requirement_pct": item.get(
                    "initial_margin_requirement_pct"
                ),
                "initial_margin_requirement_source": item.get(
                    "initial_margin_requirement_source"
                ),
                "broker_margin_requirement_rate": item.get(
                    "broker_margin_requirement_rate"
                ),
                "broker_margin_requirement_pct": (
                    _safe_float(item.get("broker_margin_requirement_rate")) * 100.0
                    if item.get("broker_margin_requirement_rate") is not None
                    else None
                ),
                "marginable": item.get("marginable"),
                "initial_margin_price": (
                    abs(current_notional / current_qty)
                    if abs(current_qty) > EPS
                    else None
                ),
                "projected_initial_margin": item.get("projected_initial_margin"),
                "beta": item.get("beta"),
                "projected_beta_exposure": (
                    _safe_float(item.get("beta")) * current_notional / equity
                    if item.get("beta") is not None
                    else None
                ),
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
        flat_price = _safe_float(reference_prices.get(symbol))
        current_side = "short" if current_notional < -EPS or current_qty < -EPS else "long"
        flat_margin_requirement = resolve_initial_margin_requirement(
            asset=assets_by_symbol.get(str(symbol).upper(), {}) or {},
            side=current_side,
            reference_price=(
                flat_price
                if flat_price > EPS
                else abs(current_notional / current_qty)
                if abs(current_qty) > EPS
                else 0.0
            ),
        )
        symbol_rows.append(
            {
                "symbol": str(symbol).upper(),
                "target_side": "flat",
                "raw_target_signed_weight": 0.0,
                "capacity_adjusted_target_signed_weight": 0.0,
                "raw_target_notional": 0.0,
                "capacity_adjusted_target_notional": 0.0,
                "reference_price": _safe_float(reference_prices.get(symbol))
                if _safe_float(reference_prices.get(symbol)) > EPS
                else None,
                "current_signed_qty": float(current_qty),
                "current_signed_notional": float(current_notional),
                "raw_target_abs_qty": 0.0,
                "capacity_adjusted_target_abs_qty": 0.0,
                "target_lattice_abs_qty": 0.0,
                "target_lattice_signed_qty": 0.0,
                "short_position_residual_qty": 0.0,
                "expected_final_signed_qty": 0.0,
                "expected_final_notional": 0.0,
                "executable_expected_signed_weight": 0.0,
                "projection_weight_gap": 0.0,
                "projection_notional_gap": 0.0,
                "raw_strategy_weight_gap": 0.0,
                "raw_strategy_notional_gap": 0.0,
                "estimated_entry_qty": 0.0,
                "estimated_entry_buying_power": 0.0,
                "buying_power_price": None,
                "buying_power_margin_multiplier": 0.0,
                "initial_margin_rate": flat_margin_requirement["initial_margin_rate"],
                "initial_margin_requirement_pct": flat_margin_requirement[
                    "initial_margin_requirement_pct"
                ],
                "initial_margin_requirement_source": flat_margin_requirement[
                    "initial_margin_requirement_source"
                ],
                "broker_margin_requirement_rate": flat_margin_requirement[
                    "broker_margin_requirement_rate"
                ],
                "broker_margin_requirement_pct": flat_margin_requirement[
                    "broker_margin_requirement_pct"
                ],
                "marginable": flat_margin_requirement["marginable"],
                "initial_margin_price": None,
                "projected_initial_margin": 0.0,
                "beta": normalized_betas.get(str(symbol).upper()),
                "projected_beta_exposure": 0.0,
                "integer_target_required": False,
                "constraint_reasons": ["raw_target_zero_release"],
            }
        )

    actual_summary = _summarize_projection_rows(
        symbol_rows,
        buying_power_cap=cap,
        gross_notional_cap=gross_target_notional if total_capacity is not None else None,
        initial_margin_cap=initial_margin_cap,
        total_buying_power_capacity=total_capacity,
        beta_abs_limit=beta_abs_limit,
    )
    projection_floor_l1 = _safe_float(
        optimizer_pre_filter_summary.get("tracking_error_l1_weight")
    )
    final_projection_l1 = _safe_float(actual_summary.get("tracking_error_l1_weight"))
    solver_mip_gap = _safe_float(solver_diag.get("mip_gap"), default=float("nan"))
    projection_floor_proven_optimal = bool(
        solver_diag.get("success")
        and int(_safe_float(solver_diag.get("status"), default=-1)) == 0
        and not solver_diag.get("fallback_used")
        and (not math.isfinite(solver_mip_gap) or solver_mip_gap <= 1e-8)
    )
    scenario_rows: list[dict[str, Any]] = []
    scenario_values = sorted({min(max(float(value), 0.0), 1.0) for value in scenario_buffers} | {buffer})
    for scenario_buffer in scenario_values:
        scenario_cap = max(0.0, float(buying_power)) * scenario_buffer
        scenario_qty, scenario_solver = _solve_projection(
            specs,
            buying_power_cap=scenario_cap,
            gross_notional_cap=gross_target_notional if total_capacity is not None else None,
            fixed_gross_notional=fixed_gross_notional,
            initial_margin_cap=initial_margin_cap,
            fixed_initial_margin=fixed_initial_margin,
            beta_abs_limit=beta_abs_limit,
            fixed_beta_exposure=fixed_beta_exposure,
        )
        scenario_rows.append(
            {
                "buffer": float(scenario_buffer),
                **_summarize_solution(
                    specs,
                    scenario_qty,
                    account_equity=equity,
                    buying_power_cap=scenario_cap,
                    gross_notional_cap=gross_target_notional
                    if total_capacity is not None
                    else None,
                    fixed_gross_notional=fixed_gross_notional,
                    initial_margin_cap=initial_margin_cap,
                    fixed_initial_margin=fixed_initial_margin,
                    total_buying_power_capacity=total_capacity,
                    fixed_beta_exposure=fixed_beta_exposure,
                    beta_abs_limit=beta_abs_limit,
                ),
                "solver_success": bool(scenario_solver.get("success")),
                "solver_status": scenario_solver.get("status"),
            }
        )

    raw_long_gross = sum(max(0.0, value) for value in raw_weights.values())
    raw_short_gross = sum(max(0.0, -value) for value in raw_weights.values())
    capacity_adjusted_long_gross = sum(
        max(0.0, value) for value in capacity_adjusted_weights.values()
    )
    capacity_adjusted_short_gross = sum(
        max(0.0, -value) for value in capacity_adjusted_weights.values()
    )
    executable_long_gross = sum(max(0.0, value) for value in executable_expected_weights.values())
    executable_short_gross = sum(max(0.0, -value) for value in executable_expected_weights.values())
    projected_final_gross_notional = float(
        actual_summary.get("projected_final_gross_notional") or 0.0
    )
    projected_initial_margin = _safe_float(
        actual_summary.get("projected_initial_margin")
    )
    projected_regt_buying_power = actual_summary.get("projected_regt_buying_power")
    gross_constraint_satisfied = bool(
        total_capacity is None
        or projected_final_gross_notional <= gross_target_notional + 1e-6
    )
    margin_constraint_satisfied = bool(
        initial_margin_cap is None
        or projected_initial_margin <= initial_margin_cap + 1e-6
    )
    beta_constraint_satisfied = actual_summary.get("beta_constraint_satisfied")
    hard_constraint_violations: list[str] = []
    if not bool(solver_diag.get("success")):
        hard_constraint_violations.append("projection_solver_failed")
    if not gross_constraint_satisfied:
        hard_constraint_violations.append("gross_notional_cap_exceeded")
    if not margin_constraint_satisfied:
        hard_constraint_violations.append("initial_margin_cap_exceeded")
    if beta_abs_limit is not None and beta_constraint_satisfied is not True:
        hard_constraint_violations.append("beta_abs_limit_exceeded_or_unverifiable")
    margin_source_counts: dict[str, int] = {}
    for row in symbol_rows:
        source = str(row.get("initial_margin_requirement_source") or "unknown")
        margin_source_counts[source] = margin_source_counts.get(source, 0) + 1
    high_margin_rows = sorted(
        (
            {
                "symbol": str(row.get("symbol") or ""),
                "target_side": str(row.get("target_side") or ""),
                "initial_margin_rate": _safe_float(row.get("initial_margin_rate")),
                "initial_margin_requirement_pct": _safe_float(
                    row.get("initial_margin_requirement_pct")
                ),
                "initial_margin_requirement_source": str(
                    row.get("initial_margin_requirement_source") or ""
                ),
                "expected_final_notional": _safe_float(
                    row.get("expected_final_notional")
                ),
                "projected_initial_margin": _safe_float(
                    row.get("projected_initial_margin")
                ),
                "extra_initial_margin_vs_regt_floor": max(
                    0.0,
                    _safe_float(row.get("projected_initial_margin"))
                    - abs(_safe_float(row.get("expected_final_notional")))
                    * REGT_INITIAL_MARGIN_FLOOR_RATE,
                ),
            }
            for row in symbol_rows
            if _safe_float(row.get("initial_margin_rate"))
            > REGT_INITIAL_MARGIN_FLOOR_RATE + EPS
            and abs(_safe_float(row.get("expected_final_notional"))) > EPS
        ),
        key=lambda row: float(row["extra_initial_margin_vs_regt_floor"]),
        reverse=True,
    )
    diagnostics = {
        "schema_version": "1.1",
        "optimizer": "executable_target_projector",
        "account_equity": float(equity),
        "buying_power": float(buying_power),
        "entry_buying_power_buffer": float(buffer),
        "buying_power_buffer": float(buffer),
        "entry_buying_power_cap": float(cap),
        "buying_power_cap": float(cap),
        "entry_buying_power_source_semantics": "remaining_broker_buying_power",
        "total_buying_power_capacity": total_capacity,
        "gross_capacity_target_ratio": float(gross_capacity_ratio),
        "gross_capacity_target_notional": float(gross_target_notional),
        "gross_capacity_target_scale": float(capacity_scale),
        "raw_target_gross_notional": float(raw_gross_notional),
        "capacity_adjusted_target_gross_notional": float(
            sum(abs(value) for value in capacity_adjusted_weights.values()) * equity
        ),
        "projected_final_gross_notional": projected_final_gross_notional,
        "projected_final_gross_utilization_of_total_capacity": (
            float(projected_final_gross_notional / total_capacity)
            if total_capacity is not None and total_capacity > EPS
            else None
        ),
        "gross_capacity_target_gap_notional": float(
            projected_final_gross_notional - gross_target_notional
        ),
        "gross_capacity_constraint_enforced": bool(total_capacity is not None),
        "regt_initial_margin_floor_rate": float(REGT_INITIAL_MARGIN_FLOOR_RATE),
        "initial_margin_constraint_enforced": bool(initial_margin_cap is not None),
        "initial_margin_capacity": (
            float(total_capacity * REGT_INITIAL_MARGIN_FLOOR_RATE)
            if total_capacity is not None
            else None
        ),
        "initial_margin_target_ratio": float(gross_capacity_ratio),
        "initial_margin_cap": initial_margin_cap,
        "projected_initial_margin": float(projected_initial_margin),
        "projected_initial_margin_utilization_of_capacity": (
            float(
                projected_initial_margin
                / (total_capacity * REGT_INITIAL_MARGIN_FLOOR_RATE)
            )
            if total_capacity is not None and total_capacity > EPS
            else None
        ),
        "initial_margin_cap_slack": (
            float(initial_margin_cap - projected_initial_margin)
            if initial_margin_cap is not None
            else None
        ),
        "regt_buying_power_reserve_floor": regt_buying_power_reserve_floor,
        "projected_regt_buying_power": projected_regt_buying_power,
        "projected_regt_buying_power_ratio": actual_summary.get(
            "projected_regt_buying_power_ratio"
        ),
        "margin_requirement_source_counts": dict(sorted(margin_source_counts.items())),
        "high_margin_symbol_count": len(high_margin_rows),
        "high_margin_symbols": high_margin_rows,
        "raw_target_net_beta": raw_target_net_beta,
        "capacity_adjusted_target_net_beta": capacity_adjusted_target_net_beta,
        "projected_net_beta": actual_summary.get("projected_net_beta"),
        "target_beta_coverage_complete": bool(beta_coverage_complete),
        "target_beta_missing_symbols": missing_beta_symbols,
        "executable_beta_band_floor": float(max(0.0, executable_beta_band)),
        "beta_abs_limit": beta_abs_limit,
        "beta_constraint_enforced": bool(beta_abs_limit is not None),
        "beta_constraint_satisfied": beta_constraint_satisfied,
        "gross_constraint_satisfied": gross_constraint_satisfied,
        "initial_margin_constraint_satisfied": margin_constraint_satisfied,
        "hard_constraints_satisfied": not hard_constraint_violations,
        "hard_constraint_violations": hard_constraint_violations,
        "strategy_capacity_scaling_error_l1_weight": float(
            sum(
                abs(raw_weights.get(symbol, 0.0) - capacity_adjusted_weights.get(symbol, 0.0))
                for symbol in set(raw_weights) | set(capacity_adjusted_weights)
            )
        ),
        "min_trade_notional": float(min_trade_notional),
        "qty_decimals": int(qty_decimals),
        "whole_shares_only": bool(whole_shares_only),
        "short_sales_whole_shares_only": bool(short_sales_whole_shares_only),
        "sizing_adverse_offset_bps": float(sizing_adverse_offset_bps),
        "short_buying_power_adverse_offset_bps": float(short_buying_power_adverse_offset_bps),
        "raw_long_gross_weight": float(raw_long_gross),
        "raw_short_gross_weight": float(raw_short_gross),
        "capacity_adjusted_long_gross_weight": float(capacity_adjusted_long_gross),
        "capacity_adjusted_short_gross_weight": float(capacity_adjusted_short_gross),
        "executable_long_gross_weight": float(executable_long_gross),
        "executable_short_gross_weight": float(executable_short_gross),
        "solver": solver_diag,
        "optimizer_pre_min_trade_summary": optimizer_pre_filter_summary,
        "projection_error_floor_semantics": (
            "global_milp_l1_optimum_before_min_trade_filter_under_integer_share_"
            "buying_power_gross_capacity_initial_margin_and_beta_constraints"
        ),
        "projection_error_floor_proven_optimal": projection_floor_proven_optimal,
        "projection_error_floor_l1_weight": float(projection_floor_l1),
        "projection_error_floor_l1_weight_pct": float(projection_floor_l1 * 100.0),
        "projection_error_floor_long_l1_weight": _safe_float(
            optimizer_pre_filter_summary.get("tracking_error_long_l1_weight")
        ),
        "projection_error_floor_long_l1_weight_pct": _safe_float(
            optimizer_pre_filter_summary.get("tracking_error_long_l1_weight_pct")
        ),
        "projection_error_floor_short_l1_weight": _safe_float(
            optimizer_pre_filter_summary.get("tracking_error_short_l1_weight")
        ),
        "projection_error_floor_short_l1_weight_pct": _safe_float(
            optimizer_pre_filter_summary.get("tracking_error_short_l1_weight_pct")
        ),
        "min_trade_filter_incremental_error_l1_weight": float(
            max(0.0, final_projection_l1 - projection_floor_l1)
        ),
        "min_trade_filter_incremental_error_l1_weight_pct": float(
            max(0.0, final_projection_l1 - projection_floor_l1) * 100.0
        ),
        **actual_summary,
        "blocked_target_count": int(len(blocked)),
        "integer_short_target_count": int(sum(spec.side == "short" and spec.integral_target for spec in specs)),
        "symbol_count": int(len(symbol_rows)),
        "symbols": sorted(symbol_rows, key=lambda row: str(row["symbol"])),
        "buying_power_buffer_scenarios": scenario_rows,
        "raw_target_signed_weights": dict(sorted(raw_weights.items())),
        "capacity_adjusted_target_signed_weights": dict(
            sorted(capacity_adjusted_weights.items())
        ),
        "executable_expected_signed_weights": dict(sorted(executable_expected_weights.items())),
        "target_lattice_signed_qty": dict(sorted(target_lattice_signed_qty.items())),
    }
    return dict(sorted(order_target_weights.items())), dict(sorted(target_lattice_signed_qty.items())), diagnostics
