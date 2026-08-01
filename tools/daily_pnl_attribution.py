"""Shared execution-cycle PnL attribution semantics."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping


DEFAULT_RECONCILIATION_THRESHOLD_BPS = 10.0
ALIGNED_SIDE_PNL_SEMANTICS = (
    "static_positions_previous_post_trade_to_current_pre_trade_mark_to_market"
)


def _payload_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload = value.get("payload")
    return payload if isinstance(payload, Mapping) else value


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(amount: float | None, denominator: float | None) -> float | None:
    if amount is None or denominator is None or denominator <= 0:
        return None
    return amount / denominator


def _account_equity(account: Mapping[str, Any]) -> float | None:
    return _finite_float(account.get("equity") or account.get("portfolio_value"))


def _position_rows(value: Any) -> tuple[list[Mapping[str, Any]], bool]:
    if value is None:
        return [], False
    if isinstance(value, Mapping):
        for key in ("positions", "payload"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, Mapping)], True
        rows = [row for row in value.values() if isinstance(row, Mapping)]
        return rows, True
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, Mapping)], True
    return [], False


def _signed_qty(row: Mapping[str, Any]) -> float | None:
    signed = _finite_float(row.get("signed_qty"))
    if signed is not None:
        return signed
    qty = _finite_float(row.get("qty"))
    if qty is None:
        return None
    side = str(row.get("side") or "").strip().lower()
    return -abs(qty) if side == "short" else abs(qty)


def _position_price(row: Mapping[str, Any], signed_qty: float) -> float | None:
    price = _finite_float(row.get("current_price"))
    if price is not None and price > 0:
        return price
    market_value = _finite_float(row.get("market_value"))
    if market_value is None or abs(signed_qty) <= 1e-12:
        return None
    derived = market_value / signed_qty
    return derived if derived > 0 else None


def _position_map(value: Any) -> tuple[dict[str, dict[str, float]], bool]:
    rows, available = _position_rows(value)
    positions: dict[str, dict[str, float]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        qty = _signed_qty(row)
        if not symbol or qty is None or abs(qty) <= 1e-12:
            continue
        price = _position_price(row, qty)
        positions[symbol] = {
            "signed_qty": float(qty),
            "current_price": float(price) if price is not None else math.nan,
        }
    return positions, available


def _static_holding_side_pnl(
    previous_positions_after: Any,
    current_positions_before: Any,
) -> dict[str, Any]:
    previous, previous_available = _position_map(previous_positions_after)
    current, current_available = _position_map(current_positions_before)
    long_pnl = 0.0
    short_pnl = 0.0
    matched_symbols: list[str] = []
    quantity_mismatch_symbols: list[str] = []
    missing_previous_symbols: list[str] = []
    missing_current_symbols: list[str] = []
    missing_price_symbols: list[str] = []

    for symbol in sorted(set(previous) | set(current)):
        previous_row = previous.get(symbol)
        current_row = current.get(symbol)
        if previous_row is None:
            missing_previous_symbols.append(symbol)
            continue
        if current_row is None:
            missing_current_symbols.append(symbol)
            continue
        previous_qty = float(previous_row["signed_qty"])
        current_qty = float(current_row["signed_qty"])
        qty_tolerance = max(1e-6, abs(previous_qty) * 1e-8)
        if abs(previous_qty - current_qty) > qty_tolerance:
            quantity_mismatch_symbols.append(symbol)
            continue
        previous_price = float(previous_row["current_price"])
        current_price = float(current_row["current_price"])
        if not math.isfinite(previous_price) or not math.isfinite(current_price):
            missing_price_symbols.append(symbol)
            continue
        pnl = previous_qty * (current_price - previous_price)
        if previous_qty > 0:
            long_pnl += pnl
        else:
            short_pnl += pnl
        matched_symbols.append(symbol)

    mismatch_symbols = sorted(
        set(quantity_mismatch_symbols)
        | set(missing_previous_symbols)
        | set(missing_current_symbols)
    )
    evidence_available = previous_available and current_available
    return {
        "evidence_available": evidence_available,
        "long_pnl": long_pnl if evidence_available else None,
        "short_pnl": short_pnl if evidence_available else None,
        "previous_position_count": len(previous),
        "current_position_count": len(current),
        "matched_position_count": len(matched_symbols),
        "matched_symbols": matched_symbols,
        "quantity_mismatch_symbols": quantity_mismatch_symbols,
        "missing_previous_symbols": missing_previous_symbols,
        "missing_current_symbols": missing_current_symbols,
        "missing_price_symbols": missing_price_symbols,
        "mismatch_symbols": mismatch_symbols,
        "position_continuity_status": (
            "unavailable"
            if not evidence_available
            else "pass"
            if not mismatch_symbols and not missing_price_symbols
            else "partial"
        ),
    }


def build_daily_side_pnl_attribution(
    *,
    account_after_capture: Any,
    account_before_capture: Any,
    account_start_capture: Any,
    previous_positions_after: Any,
    current_positions_before: Any,
    risk_snapshot: Any,
    account_activity_summary: Any,
    opening_snapshot_available: bool,
    account_start_run_dir: str = "",
    account_end_run_dir: str = "",
    account_start_captured_at_utc: str = "",
    account_before_captured_at_utc: str = "",
    account_end_captured_at_utc: str = "",
    reconciliation_threshold_bps: float = DEFAULT_RECONCILIATION_THRESHOLD_BPS,
) -> dict[str, Any]:
    """Decompose one completed-execution cycle into additive contributions.

    Account return spans previous post-trade equity to current post-trade equity.
    Long and short PnL use unchanged quantities from the previous post-trade
    position snapshot marked at current pre-trade prices. The execution-window
    component is the current post-trade equity less current pre-trade equity.
    Every contribution uses previous post-trade equity as its denominator.
    """

    account_after = _payload_mapping(account_after_capture)
    account_before = _payload_mapping(account_before_capture)
    account_start = _payload_mapping(account_start_capture)
    activity = _payload_mapping(account_activity_summary)
    static_side = _static_holding_side_pnl(
        previous_positions_after,
        current_positions_before,
    )

    cycle_start_equity = _account_equity(account_start)
    pre_trade_equity = _account_equity(account_before)
    cycle_end_equity = _account_equity(account_after)
    broker_last_equity = _finite_float(account_after.get("last_equity"))
    long_pnl = _finite_float(static_side.get("long_pnl"))
    short_pnl = _finite_float(static_side.get("short_pnl"))

    account_cycle_pnl = (
        cycle_end_equity - cycle_start_equity
        if cycle_end_equity is not None and cycle_start_equity is not None
        else None
    )
    holding_window_pnl = (
        pre_trade_equity - cycle_start_equity
        if pre_trade_equity is not None and cycle_start_equity is not None
        else None
    )
    execution_window_pnl = (
        cycle_end_equity - pre_trade_equity
        if cycle_end_equity is not None and pre_trade_equity is not None
        else None
    )
    holding_residual_pnl = (
        holding_window_pnl - long_pnl - short_pnl
        if holding_window_pnl is not None
        and long_pnl is not None
        and short_pnl is not None
        else None
    )

    if "fee_interest_dividend_net_amount" in activity:
        known_non_trade_pnl = _finite_float(
            activity.get("fee_interest_dividend_net_amount")
        )
        known_non_trade_source = "fee_interest_dividend_net_amount"
    else:
        known_non_trade_pnl = _finite_float(
            activity.get("known_non_trade_equity_impact_net_amount")
        )
        known_non_trade_source = (
            "known_non_trade_equity_impact_net_amount_legacy_fallback"
        )
    if known_non_trade_pnl is None:
        known_non_trade_pnl = 0.0
    unexplained_holding_residual_pnl = (
        holding_residual_pnl - known_non_trade_pnl
        if holding_residual_pnl is not None
        else None
    )
    residual_abs_bps = (
        abs(unexplained_holding_residual_pnl) / cycle_start_equity * 10_000.0
        if unexplained_holding_residual_pnl is not None
        and cycle_start_equity is not None
        and cycle_start_equity > 0
        else None
    )

    component_sum_pnl = (
        long_pnl + short_pnl + execution_window_pnl + holding_residual_pnl
        if long_pnl is not None
        and short_pnl is not None
        and execution_window_pnl is not None
        and holding_residual_pnl is not None
        else None
    )
    identity_error_pnl = (
        account_cycle_pnl - component_sum_pnl
        if account_cycle_pnl is not None and component_sum_pnl is not None
        else None
    )

    required_equity_available = (
        cycle_start_equity is not None
        and cycle_start_equity > 0
        and pre_trade_equity is not None
        and cycle_end_equity is not None
    )
    if not required_equity_available or not static_side["evidence_available"]:
        status = "unavailable"
    elif static_side["position_continuity_status"] != "pass":
        status = "partial"
    elif residual_abs_bps is not None and residual_abs_bps <= reconciliation_threshold_bps:
        status = "reconciled"
    else:
        status = "partial"

    return {
        "schema_version": "2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "window_semantics": "previous_completed_execute_to_current_completed_execute",
        "holding_window_semantics": (
            "previous_completed_execute_post_trade_to_current_execute_pre_trade"
        ),
        "execution_window_semantics": "current_execute_pre_trade_to_post_trade",
        "denominator_semantics": "previous_completed_execute_post_trade_equity",
        "side_pnl_semantics": ALIGNED_SIDE_PNL_SEMANTICS,
        "component_semantics": (
            "long_holding_plus_short_holding_plus_execution_window_plus_holding_residual"
        ),
        "account_cycle_start_equity": cycle_start_equity,
        "account_pre_trade_equity": pre_trade_equity,
        "account_cycle_end_equity": cycle_end_equity,
        "account_cycle_start_captured_at_utc": account_start_captured_at_utc,
        "account_pre_trade_captured_at_utc": account_before_captured_at_utc,
        "account_cycle_end_captured_at_utc": account_end_captured_at_utc,
        "account_cycle_start_run_dir": account_start_run_dir,
        "account_cycle_end_run_dir": account_end_run_dir,
        "account_equity_after": cycle_end_equity,
        "account_last_equity": cycle_start_equity,
        "broker_last_equity": broker_last_equity,
        "account_balance_asof": account_after.get("balance_asof"),
        "account_daily_pnl": account_cycle_pnl,
        "account_daily_return": _ratio(account_cycle_pnl, cycle_start_equity),
        "holding_window_pnl": holding_window_pnl,
        "holding_window_return": _ratio(holding_window_pnl, cycle_start_equity),
        "execution_window_pnl": execution_window_pnl,
        "execution_window_local_return": _ratio(
            execution_window_pnl,
            pre_trade_equity,
        ),
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "holding_residual_pnl": holding_residual_pnl,
        "known_non_trade_pnl": known_non_trade_pnl,
        "known_non_trade_pnl_source": known_non_trade_source,
        "unexplained_holding_residual_pnl": unexplained_holding_residual_pnl,
        "unattributed_residual_pnl": holding_residual_pnl,
        "long_equity_contribution": _ratio(long_pnl, cycle_start_equity),
        "short_equity_contribution": _ratio(short_pnl, cycle_start_equity),
        "execution_window_equity_contribution": _ratio(
            execution_window_pnl,
            cycle_start_equity,
        ),
        "holding_residual_equity_contribution": _ratio(
            holding_residual_pnl,
            cycle_start_equity,
        ),
        "known_non_trade_equity_contribution": _ratio(
            known_non_trade_pnl,
            cycle_start_equity,
        ),
        "unattributed_equity_contribution": _ratio(
            holding_residual_pnl,
            cycle_start_equity,
        ),
        "component_sum_pnl": component_sum_pnl,
        "identity_error_pnl": identity_error_pnl,
        "residual_abs_bps_of_cycle_start_equity": residual_abs_bps,
        "residual_abs_bps_of_last_equity": residual_abs_bps,
        "reconciliation_threshold_bps": reconciliation_threshold_bps,
        "position_continuity_status": static_side["position_continuity_status"],
        "previous_position_count": static_side["previous_position_count"],
        "current_pre_trade_position_count": static_side["current_position_count"],
        "matched_position_count": static_side["matched_position_count"],
        "position_quantity_mismatch_symbols": static_side[
            "quantity_mismatch_symbols"
        ],
        "position_missing_previous_symbols": static_side[
            "missing_previous_symbols"
        ],
        "position_missing_current_symbols": static_side[
            "missing_current_symbols"
        ],
        "position_missing_price_symbols": static_side["missing_price_symbols"],
        "opening_snapshot_available": bool(opening_snapshot_available),
        "opening_snapshot_source": (
            "broker_day_open_snapshot.json" if opening_snapshot_available else ""
        ),
        "strict_daily_side_attribution_ready": status == "reconciled",
        "note": (
            "Long and short use unchanged previous post-trade quantities marked from previous post-trade "
            "prices to current pre-trade prices. Execution-window PnL is current post-trade equity less "
            "current pre-trade equity. All chart contributions use previous post-trade equity, so they are "
            "additive; holding residual remains explicit for cash activity, snapshot timing, or continuity gaps."
        ),
    }
