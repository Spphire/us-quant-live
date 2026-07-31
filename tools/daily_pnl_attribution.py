"""Shared account-consistent daily PnL attribution semantics."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping


DEFAULT_RECONCILIATION_THRESHOLD_BPS = 10.0


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


def build_daily_side_pnl_attribution(
    *,
    account_after_capture: Any,
    account_start_capture: Any,
    risk_snapshot: Any,
    account_activity_summary: Any,
    opening_snapshot_available: bool,
    account_start_run_dir: str = "",
    account_end_run_dir: str = "",
    account_end_captured_at_utc: str = "",
    reconciliation_threshold_bps: float = DEFAULT_RECONCILIATION_THRESHOLD_BPS,
) -> dict[str, Any]:
    """Reconcile side snapshot PnL to the execution-cycle equity window.

    The long/short inputs remain snapshot proxies until a complete opening
    position and fill replay is available. The residual is intentionally kept
    explicit so the displayed components always conserve account PnL without
    inventing a side allocation.
    """

    account = _payload_mapping(account_after_capture)
    account_start = _payload_mapping(account_start_capture)
    risk = _payload_mapping(risk_snapshot)
    activity = _payload_mapping(account_activity_summary)
    by_side = risk.get("snapshot_intraday_pnl_by_side")
    by_side = by_side if isinstance(by_side, Mapping) else {}

    equity_after = _account_equity(account)
    cycle_start_equity = _account_equity(account_start)
    broker_last_equity = _finite_float(account.get("last_equity"))
    long_pnl = _finite_float(by_side.get("long"))
    short_pnl = _finite_float(by_side.get("short"))
    if "fee_interest_dividend_net_amount" in activity:
        non_trade_pnl = _finite_float(activity.get("fee_interest_dividend_net_amount"))
        non_trade_source = "fee_interest_dividend_net_amount"
    else:
        non_trade_pnl = _finite_float(activity.get("known_non_trade_equity_impact_net_amount"))
        non_trade_source = "known_non_trade_equity_impact_net_amount_legacy_fallback"
    if non_trade_pnl is None:
        non_trade_pnl = 0.0

    account_daily_pnl = (
        equity_after - cycle_start_equity
        if equity_after is not None and cycle_start_equity is not None
        else None
    )
    has_side_snapshot = long_pnl is not None and short_pnl is not None
    residual = (
        account_daily_pnl - long_pnl - short_pnl - non_trade_pnl
        if account_daily_pnl is not None and has_side_snapshot
        else None
    )
    residual_abs_bps = (
        abs(residual) / cycle_start_equity * 10_000.0
        if residual is not None and cycle_start_equity is not None and cycle_start_equity > 0
        else None
    )

    if account_daily_pnl is None or cycle_start_equity is None or cycle_start_equity <= 0:
        status = "unavailable"
    elif not has_side_snapshot:
        status = "unavailable"
    elif residual_abs_bps is not None and residual_abs_bps <= reconciliation_threshold_bps:
        status = "reconciled"
    else:
        status = "partial"

    component_sum = (
        long_pnl + short_pnl + non_trade_pnl + residual
        if has_side_snapshot and residual is not None
        else None
    )
    identity_error = (
        account_daily_pnl - component_sum
        if account_daily_pnl is not None and component_sum is not None
        else None
    )

    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "window_semantics": "previous_completed_execute_to_current_completed_execute",
        "denominator_semantics": "previous_completed_execute_post_trade_equity",
        "side_pnl_semantics": "final_position_intraday_snapshot_proxy",
        "account_cycle_start_equity": cycle_start_equity,
        "account_cycle_end_equity": equity_after,
        "account_cycle_start_captured_at_utc": account_start.get("captured_at_utc"),
        "account_cycle_end_captured_at_utc": account_end_captured_at_utc,
        "account_cycle_start_run_dir": account_start_run_dir,
        "account_cycle_end_run_dir": account_end_run_dir,
        "account_equity_after": equity_after,
        "account_last_equity": cycle_start_equity,
        "broker_last_equity": broker_last_equity,
        "account_balance_asof": account.get("balance_asof"),
        "account_daily_pnl": account_daily_pnl,
        "account_daily_return": _ratio(account_daily_pnl, cycle_start_equity),
        "long_pnl": long_pnl,
        "short_pnl": short_pnl,
        "known_non_trade_pnl": non_trade_pnl,
        "known_non_trade_pnl_source": non_trade_source,
        "unattributed_residual_pnl": residual,
        "long_equity_contribution": _ratio(long_pnl, cycle_start_equity),
        "short_equity_contribution": _ratio(short_pnl, cycle_start_equity),
        "known_non_trade_equity_contribution": _ratio(non_trade_pnl, cycle_start_equity),
        "unattributed_equity_contribution": _ratio(residual, cycle_start_equity),
        "component_sum_pnl": component_sum,
        "identity_error_pnl": identity_error,
        "residual_abs_bps_of_cycle_start_equity": residual_abs_bps,
        "residual_abs_bps_of_last_equity": residual_abs_bps,
        "reconciliation_threshold_bps": reconciliation_threshold_bps,
        "opening_snapshot_available": bool(opening_snapshot_available),
        "opening_snapshot_source": "broker_day_open_snapshot.json" if opening_snapshot_available else "",
        "strict_daily_side_attribution_ready": False,
        "note": (
            "Components conserve execution-cycle account PnL. Long and short remain final-position snapshot "
            "proxies; the explicit residual absorbs closed-position, restart, transfer-window, and timing "
            "coverage gaps. The broker last_equity field is retained as a diagnostic, not as the cycle start."
        ),
    }
