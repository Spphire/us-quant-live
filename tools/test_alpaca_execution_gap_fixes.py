"""Regression tests for live Alpaca execution gap fixes.

These tests do not call Alpaca. They lock in local behaviors that directly
affect ideal-vs-actual gaps, including short-share sizing, bounded live-quote
requotes, stage-level concurrency, and audit propagation.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alpaca_executor import (  # noqa: E402
    OrderInstruction,
    _build_order_instructions,
    _collect_portfolio_history_snapshot,
    _effective_min_trade_notional,
    _is_insufficient_buying_power_error,
    _is_insufficient_qty_available_error,
    _marketable_offset_ladder,
    _submit_and_track_orders,
    _total_regt_buying_power_capacity,
)
from src.executable_target_projector import project_executable_targets  # noqa: E402
from tools.daily_audit_report import (  # noqa: E402
    _build_executable_target_projection_outputs,
    _build_execution_attribution_outputs,
    _build_order_attempt_rows,
    _build_order_trace,
    _build_position_capacity_summary,
)
from tools.execution_quality import _logical_records  # noqa: E402


class _NeverFillClient:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.submit_count = 0
        self.cancel_count = 0

    def submit_order(self, **kwargs):
        self.submit_count += 1
        order_id = f"order-{self.submit_count}"
        order = {
            "id": order_id,
            "client_order_id": kwargs.get("client_order_id"),
            "symbol": kwargs.get("symbol"),
            "side": kwargs.get("side"),
            "type": kwargs.get("type"),
            "time_in_force": kwargs.get("time_in_force"),
            "qty": kwargs.get("qty"),
            "limit_price": kwargs.get("limit_price"),
            "status": "new",
            "filled_qty": "0",
            "filled_avg_price": None,
        }
        self.orders[order_id] = dict(order)
        return dict(order)

    def get_order(self, order_id):
        return dict(self.orders[order_id])

    def cancel_order(self, order_id):
        self.cancel_count += 1
        self.orders[order_id]["status"] = "canceled"
        return {}


class _ImmediateFillConcurrencyClient:
    def __init__(self, submit_delay_seconds: float = 0.12) -> None:
        self.submit_delay_seconds = float(submit_delay_seconds)
        self.lock = threading.Lock()
        self.active_submits = 0
        self.max_active_submits = 0
        self.orders: dict[str, dict[str, object]] = {}

    def submit_order(self, **kwargs):
        with self.lock:
            self.active_submits += 1
            self.max_active_submits = max(self.max_active_submits, self.active_submits)
        try:
            time.sleep(self.submit_delay_seconds)
            order_id = str(kwargs.get("client_order_id"))
            order = {
                "id": order_id,
                "client_order_id": order_id,
                "symbol": kwargs.get("symbol"),
                "side": kwargs.get("side"),
                "type": kwargs.get("type"),
                "time_in_force": kwargs.get("time_in_force"),
                "qty": str(kwargs.get("qty")),
                "status": "filled",
                "filled_qty": str(kwargs.get("qty")),
                "filled_avg_price": "100",
            }
            with self.lock:
                self.orders[order_id] = dict(order)
            return dict(order)
        finally:
            with self.lock:
                self.active_submits -= 1

    def get_order(self, order_id):
        return dict(self.orders[order_id])


class _QuoteNeverFillClient(_NeverFillClient):
    def get_latest_quotes(self, *, symbols, feed):
        return {
            str(symbol).upper(): {"bp": 90.0, "ap": 91.0, "feed": str(feed)}
            for symbol in symbols
        }


class _PortfolioHistoryClient:
    def __init__(self) -> None:
        self.kwargs = None

    def get_portfolio_history(self, **kwargs):
        self.kwargs = dict(kwargs)
        return {"timestamp": [], "equity": []}


def _instructions_for_case(*, target_notional: float, current_notional: float, current_qty: float, price: float):
    return _build_order_instructions(
        target_signed_weights={"X": target_notional / 89945.44},
        current_signed_notional={"X": current_notional},
        current_signed_qty={"X": current_qty},
        account_equity=89945.44,
        reference_prices={"X": price},
        assets_by_symbol={"X": {"shortable": True}},
        min_trade_notional=200.0,
        sizing_adverse_offset_bps=12.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )


def test_whole_share_short_delta_uses_target_shares():
    cases = [
        ("increase_short_one_share", -1539.0509200000001, -1154.656314, -3.0, 385.225, 1.0),
        ("increase_short_ceg_like", -2887.770512, -2631.781831, -10.0, 262.84, 1.0),
        ("open_short_two_shares_tiny_residual", -2287.212048, -1.029582, -0.0009, 1144.98, 2.0),
        ("open_short_two_shares_anet_like", -366.479696, 0.0, 0.0, 183.46, 2.0),
    ]
    for name, target_notional, current_notional, current_qty, price, expected_qty in cases:
        instructions, skipped = _instructions_for_case(
            target_notional=target_notional,
            current_notional=current_notional,
            current_qty=current_qty,
            price=price,
        )
        assert not skipped, f"{name}: unexpected skipped={skipped}"
        assert len(instructions) == 1, f"{name}: expected one order, got {instructions}"
        assert instructions[0].side == "sell", f"{name}: expected sell, got {instructions[0].side}"
        assert instructions[0].qty == expected_qty, f"{name}: qty={instructions[0].qty}, expected={expected_qty}"
        print(f"  [OK] {name}: qty={instructions[0].qty}")


def test_fractional_short_residual_close_does_not_round_up():
    instructions, skipped = _instructions_for_case(
        target_notional=0.0,
        current_notional=-366.98,
        current_qty=-0.9998,
        price=366.98,
    )
    assert not skipped, f"unexpected skipped={skipped}"
    assert len(instructions) == 1, f"expected one order, got {instructions}"
    assert instructions[0].side == "buy", f"expected buy-to-cover, got {instructions[0].side}"
    assert instructions[0].qty == 0.9998, f"qty={instructions[0].qty}, expected 0.9998"
    print(f"  [OK] fractional short residual close qty={instructions[0].qty}")


def test_short_cover_to_remaining_short_stays_whole_share():
    instructions, skipped = _instructions_for_case(
        target_notional=-366.98,
        current_notional=-916.715826,
        current_qty=-2.4998,
        price=366.98,
    )
    assert not skipped, f"unexpected skipped={skipped}"
    assert len(instructions) == 1, f"expected one order, got {instructions}"
    assert instructions[0].side == "buy", f"expected buy-to-cover, got {instructions[0].side}"
    assert instructions[0].qty == 1.0, f"qty={instructions[0].qty}, expected integer cover qty 1.0"
    print(f"  [OK] short cover to remaining short stays whole-share qty={instructions[0].qty}")


def test_short_cover_near_integer_residual_does_not_round_to_zero():
    instructions, skipped = _instructions_for_case(
        target_notional=-2638.85457,
        current_notional=-3167.92098,
        current_qty=-5.991,
        price=528.405,
    )
    assert not skipped, f"unexpected skipped={skipped}"
    assert len(instructions) == 1, f"expected one order, got {instructions}"
    assert instructions[0].side == "buy", f"expected buy-to-cover, got {instructions[0].side}"
    assert instructions[0].qty == 1.0, f"qty={instructions[0].qty}, expected integer cover qty 1.0"
    print(f"  [OK] near-integer short residual cover qty={instructions[0].qty}")


def _project_targets(
    *,
    weights,
    prices,
    current_qty=None,
    current_notional=None,
    equity=90000.0,
    buying_power=360000.0,
    buffer=0.90,
    total_capacity=None,
    gross_capacity_ratio=0.95,
):
    assets = {
        symbol: {"shortable": True, "fractionable": True}
        for symbol in set(weights) | set(current_qty or {})
    }
    return project_executable_targets(
        raw_target_signed_weights=weights,
        current_signed_qty=current_qty or {},
        current_signed_notional=current_notional or {},
        reference_prices=prices,
        assets_by_symbol=assets,
        account_equity=equity,
        buying_power=buying_power,
        buying_power_buffer=buffer,
        min_trade_notional=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        sizing_adverse_offset_bps=12.0,
        short_buying_power_adverse_offset_bps=300.0,
        total_buying_power_capacity=total_capacity,
        gross_capacity_target_ratio=gross_capacity_ratio,
    )


def test_projector_uses_nearest_integer_short_target():
    order_weights, lattice_qty, diagnostics = _project_targets(
        weights={"AMD": -(1.0 / 30.0)},
        prices={"AMD": 526.25},
    )
    assert diagnostics["solver"]["success"], diagnostics["solver"]
    assert lattice_qty["AMD"] == -6.0, lattice_qty
    row = next(item for item in diagnostics["symbols"] if item["symbol"] == "AMD")
    nearest_gap = abs(row["projection_notional_gap"])
    floor_gap = abs(3000.0 - 5.0 * 526.25)
    assert nearest_gap < floor_gap, (nearest_gap, floor_gap)
    assert order_weights["AMD"] < 0.0
    print(f"  [OK] projector selects nearest short lattice qty=6, gap=${nearest_gap:.2f}")


def test_projector_enforces_buying_power_cap_proportionally():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"A": 0.50, "B": 0.50},
        prices={"A": 100.0, "B": 100.0},
        equity=100000.0,
        buying_power=10000.0,
        buffer=0.90,
    )
    assert diagnostics["solver"]["success"], diagnostics["solver"]
    assert diagnostics["estimated_entry_buying_power_used"] <= 9000.0 + 1e-6
    assert abs(lattice_qty["A"] - lattice_qty["B"]) <= 0.001, lattice_qty
    assert 44.0 <= lattice_qty["A"] <= 45.0, lattice_qty
    print(
        "  [OK] projector respects 90% cap and preserves proportional targets "
        f"used=${diagnostics['estimated_entry_buying_power_used']:.2f}"
    )


def test_projector_short_residual_produces_integer_order_delta():
    equity = 90000.0
    price = 500.0
    order_weights, lattice_qty, diagnostics = _project_targets(
        weights={"X": -(3500.0 / equity)},
        prices={"X": price},
        current_qty={"X": -5.991},
        current_notional={"X": -2995.5},
        equity=equity,
    )
    assert lattice_qty["X"] == -7.0, lattice_qty
    instructions, skipped = _build_order_instructions(
        target_signed_weights=order_weights,
        current_signed_notional={"X": -2995.5},
        current_signed_qty={"X": -5.991},
        account_equity=equity,
        reference_prices={"X": price},
        assets_by_symbol={"X": {"shortable": True, "fractionable": True}},
        min_trade_notional=0.0,
        sizing_adverse_offset_bps=12.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )
    assert not skipped, skipped
    assert len(instructions) == 1
    assert instructions[0].side == "sell"
    assert instructions[0].qty == 1.0
    row = next(item for item in diagnostics["symbols"] if item["symbol"] == "X")
    assert abs(row["expected_final_signed_qty"] + 6.991) < 1e-9, row
    print("  [OK] residual-aware short target emits one integer sell share")


def test_projector_logs_buffer_scenarios():
    _, _, diagnostics = _project_targets(
        weights={"A": 0.50, "B": 0.50},
        prices={"A": 100.0, "B": 100.0},
        equity=100000.0,
        buying_power=10000.0,
        buffer=0.90,
    )
    scenarios = {round(item["buffer"], 2): item for item in diagnostics["buying_power_buffer_scenarios"]}
    assert {0.85, 0.90, 0.95}.issubset(scenarios), scenarios
    assert scenarios[0.85]["buying_power_cap"] == 8500.0
    assert scenarios[0.95]["buying_power_cap"] == 9500.0
    print("  [OK] projector logs 85/90/95% buying-power scenarios")


def test_projector_uses_buying_power_only_as_secondary_objective():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"X": -(550.0 / 10000.0)},
        prices={"X": 100.0},
        equity=10000.0,
        buying_power=100000.0,
        buffer=0.90,
    )
    assert lattice_qty["X"] == -6.0, lattice_qty
    assert diagnostics["solver"]["objective_priority"][0] == "minimize_absolute_weight_error"
    assert diagnostics["solver"]["secondary_optimization_used"]
    print("  [OK] equal weight-error tie uses higher exposure only in secondary solve")


def test_projector_enforces_final_gross_capacity_target():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"A": 0.50, "B": 0.50, "C": -0.50, "D": -0.50},
        prices={"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0},
        equity=100000.0,
        buying_power=400000.0,
        buffer=0.95,
        total_capacity=200000.0,
        gross_capacity_ratio=0.95,
    )
    assert diagnostics["solver"]["success"], diagnostics["solver"]
    assert diagnostics["gross_capacity_constraint_enforced"] is True
    assert abs(diagnostics["gross_capacity_target_notional"] - 190000.0) < 1e-6
    assert diagnostics["projected_final_gross_notional"] <= 190000.0 + 1e-6
    assert abs(diagnostics["gross_capacity_target_scale"] - 0.95) < 1e-12
    adjusted = diagnostics["capacity_adjusted_target_signed_weights"]
    assert adjusted == {"A": 0.475, "B": 0.475, "C": -0.475, "D": -0.475}
    assert sum(abs(float(qty)) * 100.0 for qty in lattice_qty.values()) <= 190000.0 + 1e-6
    print("  [OK] final gross is capped at 95% of stable total RegT capacity")


def test_total_regt_capacity_reconstruction():
    total, gross, remaining, source = _total_regt_buying_power_capacity(
        account={
            "long_market_value": "89727.87",
            "short_market_value": "-86240.25",
            "regt_buying_power": "1811.90",
        },
        signed_notional={},
    )
    assert abs(gross - 175968.12) < 1e-6
    assert abs(remaining - 1811.90) < 1e-6
    assert abs(total - 177780.02) < 1e-6
    assert "regt_buying_power" in source
    print("  [OK] total RegT capacity uses gross position plus remaining RegT buying power")


def test_portfolio_history_uses_explicit_range_without_period():
    client = _PortfolioHistoryClient()
    result = _collect_portfolio_history_snapshot(
        client=client,
        session_date=date(2026, 7, 22),
        label="test",
    )
    assert result["ok"] is True, result
    assert client.kwargs is not None
    assert "period" not in client.kwargs, client.kwargs
    assert client.kwargs["start"] == "2026-07-22T00:00:00Z"
    assert client.kwargs["end"] == "2026-07-23T00:00:00Z"
    print("  [OK] portfolio history avoids conflicting period plus start/end parameters")


def test_projection_audit_prefers_staged_entry_snapshot():
    initial = {
        "solver": {"success": True},
        "buying_power": 1000.0,
        "buying_power_buffer": 0.90,
        "buying_power_cap": 900.0,
        "estimated_entry_buying_power_used": 100.0,
        "tracking_error_l1_weight": 0.02,
        "symbols": [{"symbol": "X", "constraint_reasons": ["short_target_integer"]}],
    }
    staged = {
        **initial,
        "estimated_entry_buying_power_used": 200.0,
        "tracking_error_l1_weight": 0.01,
    }
    with TemporaryDirectory() as temp_dir:
        run_dir = Path(temp_dir)
        (run_dir / "executable_target_projection.json").write_text(json.dumps(initial), encoding="utf-8")
        rows, summary = _build_executable_target_projection_outputs(
            run_dir=run_dir,
            staged_rebuild_snapshots={
                "snapshots": [
                    {
                        "snapshot_type": "entry_rebuild",
                        "entry_executable_target_projection": staged,
                    }
                ]
            },
        )
    assert len(rows) == 2, rows
    assert summary["final_projection_phase"] == "staged_entry", summary
    assert summary["tracking_error_l1_weight"] == 0.01, summary
    assert rows[-1]["constraint_reasons"] == "short_target_integer", rows[-1]
    print("  [OK] projection audit uses refreshed staged-entry optimization")


def test_min_trade_short_carry_cannot_emit_residual_order():
    equity = 100000.0
    current_notional = -1620.0
    order_weights, _, diagnostics = project_executable_targets(
        raw_target_signed_weights={"X": -(1520.0 / equity)},
        current_signed_qty={"X": -0.81},
        current_signed_notional={"X": current_notional},
        reference_prices={"X": 2000.0},
        assets_by_symbol={"X": {"shortable": True, "fractionable": True}},
        account_equity=equity,
        buying_power=400000.0,
        buying_power_buffer=0.90,
        min_trade_notional=200.0,
        qty_decimals=4,
        whole_shares_only=False,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        sizing_adverse_offset_bps=12.0,
        short_buying_power_adverse_offset_bps=300.0,
    )
    assert order_weights["X"] == current_notional / equity, order_weights
    instructions, skipped = _build_order_instructions(
        target_signed_weights=order_weights,
        current_signed_notional={"X": current_notional},
        current_signed_qty={"X": -0.81},
        account_equity=equity,
        reference_prices={"X": 2000.0},
        assets_by_symbol={"X": {"shortable": True, "fractionable": True}},
        min_trade_notional=200.0,
        sizing_adverse_offset_bps=12.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )
    assert not instructions and not skipped, (instructions, skipped)
    row = next(item for item in diagnostics["symbols"] if item["symbol"] == "X")
    assert "carried_by_min_trade_notional" in row["constraint_reasons"], row
    print("  [OK] high-price fractional short carry emits no residual order")


def test_min_trade_threshold_scales_with_weight_error_budget():
    assert _effective_min_trade_notional(
        account_equity=90000.0,
        absolute_floor=1.0,
        weight_bps=1.0,
    ) == 9.0
    assert _effective_min_trade_notional(
        account_equity=90000.0,
        absolute_floor=25.0,
        weight_bps=1.0,
    ) == 25.0
    print("  [OK] min-trade band scales to one account-equity basis point")


def test_insufficient_qty_error_is_not_buying_power_abort():
    exc = RuntimeError(
        'Alpaca request failed with HTTP 403: {"available":"0.9998","code":40310000,'
        '"existing_qty":"0.9998","held_for_orders":"0",'
        '"message":"insufficient qty available for order (requested: 1, available: 0.9998)",'
        '"symbol":"GOOGL"}'
    )
    assert _is_insufficient_qty_available_error(exc)
    assert not _is_insufficient_buying_power_error(exc)
    print("  [OK] insufficient-qty submit error is non-buying-power")


def test_marketable_limit_requotes_until_timeout():
    client = _NeverFillClient()
    records = _submit_and_track_orders(
        client=client,
        instructions=[
            OrderInstruction(
                symbol="X",
                side="buy",
                qty=1.0,
                reference_price=100.0,
                sizing_price=101.0,
                current_notional=0.0,
                target_notional=100.0,
                delta_notional=100.0,
                opening_short=False,
            )
        ],
        session_token="test",
        timeout_seconds=2.2,
        poll_seconds=0.1,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=10.0,
        marketable_limit_max_offset_bps=50.0,
        marketable_limit_requote_steps_bps=[0.0, 10.0],
        marketable_limit_requote_wait_seconds=0.1,
    )
    attempts = records[0]["attempts"]
    offsets = [attempt["offset_bps"] for attempt in attempts]
    assert len(attempts) > 2, f"expected repeated requotes beyond one pass, got offsets={offsets}"
    assert max(offsets) <= 50.0, f"max offset cap violated: {offsets}"
    assert records[0]["remaining_qty"] == 1.0
    print(f"  [OK] repeated requotes: attempts={len(attempts)}, offsets={offsets}")


def test_marketable_limit_ladder_is_bounded_and_unique():
    ladder = _marketable_offset_ladder(
        base_offset_bps=12.0,
        max_offset_bps=150.0,
        requote_steps_bps=[0.0, 25.0, 75.0, 150.0],
        max_attempts=4,
    )
    assert ladder == [12.0, 37.0, 87.0, 150.0], ladder
    assert len(ladder) == len(set(ladder)) == 4
    print(f"  [OK] bounded distinct quote ladder: {ladder}")


def test_marketable_limit_uses_live_quote_side():
    client = _QuoteNeverFillClient()
    records = _submit_and_track_orders(
        client=client,
        instructions=[
            OrderInstruction(
                symbol="X",
                side="sell",
                qty=1.0,
                reference_price=100.0,
                sizing_price=99.0,
                current_notional=100.0,
                target_notional=0.0,
                delta_notional=-100.0,
                opening_short=False,
            )
        ],
        session_token="quote",
        timeout_seconds=1.2,
        poll_seconds=0.1,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=1,
        execution_price_feed="iex",
    )
    attempt = records[0]["attempts"][0]
    assert attempt["reference_price_source"] == "latest_quote.bp", attempt
    assert attempt["live_reference_price"] == 90.0, attempt
    assert attempt["limit_price"] == 89.89, attempt
    assert attempt["max_offset_bps"] == 150.0, attempt
    assert records[0]["attempt_count"] == 1, records[0]
    audit_row = _build_order_attempt_rows(records, [])[0]
    assert audit_row["live_reference_price"] == 90.0, audit_row
    assert audit_row["reference_price_source"] == "latest_quote.bp", audit_row
    assert audit_row["marketable_limit_max_attempts"] == 1, audit_row
    assert audit_row["max_offset_bps"] == 150.0, audit_row
    print("  [OK] sell limit refreshes from live bid and obeys one-attempt cap")


def test_order_batch_runs_symbols_concurrently():
    client = _ImmediateFillConcurrencyClient(submit_delay_seconds=0.12)
    instructions = [
        OrderInstruction(
            symbol=f"X{index}",
            side="buy",
            qty=1.0,
            reference_price=100.0,
            sizing_price=101.0,
            current_notional=0.0,
            target_notional=100.0,
            delta_notional=100.0,
            opening_short=False,
        )
        for index in range(6)
    ]
    started = time.monotonic()
    records = _submit_and_track_orders(
        client=client,
        instructions=instructions,
        session_token="parallel",
        timeout_seconds=5.0,
        poll_seconds=0.1,
        execution_order_style="market",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=1,
        max_workers=3,
    )
    elapsed = time.monotonic() - started
    assert client.max_active_submits == 3, client.max_active_submits
    assert elapsed < 0.60, elapsed
    assert [row["symbol"] for row in records] == [f"X{index}" for index in range(6)]
    assert all(row["batch_effective_workers"] == 3 for row in records)
    assert max(float(row["queue_wait_ms"]) for row in records) >= 80.0
    print(
        f"  [OK] six symbols completed with max_concurrency={client.max_active_submits} "
        f"elapsed={elapsed:.3f}s"
    )


def test_audit_keeps_requote_fields():
    records = [
        {
            "symbol": "X",
            "side": "buy",
            "stage": "entry",
            "status_latest": "canceled",
            "qty": 1.0,
            "filled_qty": 0.0,
            "remaining_qty": 1.0,
            "reference_price": 100.0,
            "delta_notional": 100.0,
            "attempt_count": 2,
            "attempts": [
                {
                    "attempt_no": 1,
                    "client_order_id": "x-1",
                    "order_id": "order-1",
                    "qty_submitted": 1.0,
                    "limit_price": 100.1,
                    "offset_bps": 10.0,
                    "requote_step_index": 1,
                    "requote_cycle": 1,
                    "status_latest": "canceled",
                    "filled_qty": 0.0,
                    "poll_events": [{"event": "submitted", "max_offset_bps": 50.0}],
                },
                {
                    "attempt_no": 2,
                    "client_order_id": "x-2",
                    "order_id": "order-2",
                    "qty_submitted": 1.0,
                    "limit_price": 100.5,
                    "offset_bps": 50.0,
                    "requote_step_index": 2,
                    "requote_cycle": 3,
                    "max_offset_bps": 50.0,
                    "status_latest": "canceled",
                    "filled_qty": 0.0,
                },
            ],
        }
    ]
    attempt_rows = _build_order_attempt_rows(records, [])
    assert attempt_rows[1]["requote_step_index"] == 2
    assert attempt_rows[1]["requote_cycle"] == 3
    assert attempt_rows[1]["max_offset_bps"] == 50.0

    execution_rows, summary = _build_execution_attribution_outputs(records, [])
    assert execution_rows[1]["requote_step_index"] == 2
    assert execution_rows[1]["requote_cycle"] == 3
    assert execution_rows[1]["max_offset_bps"] == 50.0
    assert summary["multi_attempt_record_count"] == 1
    assert summary["records_hitting_max_offset_count"] == 1
    assert summary["unfilled_records_hitting_max_offset_count"] == 1
    assert summary["unfilled_records_hitting_max_offset_remaining_notional"] == 100.0
    print("  [OK] audit preserves requote fields and max-offset summary")


def test_audit_parses_submit_error_payload():
    records = [
        {
            "symbol": "GOOGL",
            "side": "buy",
            "stage": "release_buy_to_cover",
            "status_latest": "submit_error",
            "qty": 1.0,
            "filled_qty": 0.0,
            "remaining_qty": 1.0,
            "reference_price": 366.98,
            "delta_notional": 366.796626,
            "error_type": "AlpacaRequestError",
            "error": (
                'Alpaca request failed with HTTP 403: {"available":"0.9998","code":40310000,'
                '"existing_qty":"0.9998","held_for_orders":"0",'
                '"message":"insufficient qty available for order (requested: 1, available: 0.9998)",'
                '"symbol":"GOOGL"}'
            ),
        }
    ]
    attempt_rows = _build_order_attempt_rows(records, [])
    assert attempt_rows[0]["submit_error_class"] == "insufficient_qty_available"
    assert attempt_rows[0]["broker_available_qty"] == 0.9998
    assert attempt_rows[0]["broker_existing_qty"] == 0.9998
    assert attempt_rows[0]["broker_error_code"] == 40310000
    print("  [OK] audit parses submit-error payload")


def test_audit_marks_not_submitted_reason():
    plan = {
        "orders": [
            {"symbol": "GOOGL", "side": "buy", "qty": 1.0, "delta_notional": 366.8},
            {"symbol": "HLI", "side": "buy", "qty": 17.58, "delta_notional": 2473.84},
        ]
    }
    records = [
        {
            "symbol": "GOOGL",
            "side": "buy",
            "stage": "release_buy_to_cover",
            "status_latest": "submit_error",
            "qty": 1.0,
            "remaining_qty": 1.0,
            "delta_notional": 366.8,
            "submit_error_class": "insufficient_qty_available",
        }
    ]
    summary = {
        "staged_diagnostics": {
            "entry_abort_reason": "release_buy_to_cover_not_fully_filled_after_3_rounds",
            "release_unfilled_stage": "release_buy_to_cover",
            "release_unfilled_symbols": ["GOOGL", "ABBV"],
        }
    }
    rows = _build_order_trace(plan, records, {}, summary)
    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["GOOGL"]["not_submitted_reason"] == "submit_error:insufficient_qty_available"
    assert by_symbol["HLI"]["not_submitted_reason"] == (
        "entry_aborted:release_buy_to_cover_not_fully_filled_after_3_rounds"
    )
    print("  [OK] audit marks not-submitted reason")


def test_audit_merges_staged_rebuild_fill_after_cancel():
    plan = {
        "orders": [
            {
                "symbol": "ALAB",
                "side": "buy",
                "qty": 1.0023,
                "delta_notional": 303.39621,
                "current_notional": -303.39621,
                "target_notional": 0.0,
                "reference_price": 301.98,
            }
        ]
    }
    records = [
        {
            "symbol": "ALAB",
            "side": "buy",
            "stage": "release_buy_to_cover",
            "release_round": 1,
            "status_latest": "canceled",
            "qty": 1.0023,
            "filled_qty": 0.0,
            "remaining_qty": 1.0023,
            "delta_notional": 303.39621,
            "reference_price": 301.98,
            "attempt_count": 14,
        },
        {
            "symbol": "ALAB",
            "side": "buy",
            "stage": "release_buy_to_cover",
            "release_round": 2,
            "status_latest": "filled",
            "qty": 1.0023,
            "filled_qty": 1.0023,
            "remaining_qty": 0.0,
            "delta_notional": 307.981733,
            "reference_price": 307.93,
            "filled_avg_price": 308.88,
            "attempt_count": 1,
        },
    ]
    rows = _build_order_trace(plan, records, {}, {})
    assert len(rows) == 1
    assert rows[0]["status_latest"] == "filled"
    assert rows[0]["filled_qty"] == 1.0023
    assert rows[0]["remaining_qty"] == 0.0
    assert rows[0]["attempt_count"] == 15

    logical = _logical_records(records)
    assert len(logical) == 1
    assert logical[0]["status_latest"] == "filled"
    assert logical[0]["raw_record_count"] == 2
    assert logical[0]["filled_qty"] == 1.0023
    print("  [OK] staged rebuild fill supersedes earlier canceled audit record")


def test_position_capacity_uses_total_regt_capacity():
    with TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "broker_account_after.json").write_text(
            json.dumps(
                {
                    "long_market_value": "89727.87",
                    "short_market_value": "-86240.25",
                    "position_market_value": "175968.12",
                    "regt_buying_power": "1811.90",
                }
            ),
            encoding="utf-8",
        )
        summary = _build_position_capacity_summary(run_dir)

    assert summary["status"] == "pass"
    assert abs(summary["gross_position_notional"] - 175968.12) < 1e-6
    assert abs(summary["total_regt_buying_power_capacity"] - 177780.02) < 1e-6
    assert abs(summary["configured_gross_target_notional"] - 168891.019) < 1e-6
    assert abs(summary["gross_error_vs_target_notional"] - 7077.101) < 1e-6
    assert abs(summary["gross_utilization_of_total_bp"] - (175968.12 / 177780.02)) < 1e-12
    assert 3.9 < summary["gross_error_vs_target_pct_points"] < 4.1
    assert summary["gross_error_vs_total_pct_points"] < -1.0
    print("  [OK] gross position is benchmarked against reconstructed total RegT capacity")


def main() -> int:
    tests = [
        ("Whole-share short target sizing", test_whole_share_short_delta_uses_target_shares),
        ("Fractional short residual close sizing", test_fractional_short_residual_close_does_not_round_up),
        ("Short cover to remaining short stays whole-share", test_short_cover_to_remaining_short_stays_whole_share),
        ("Near-integer short residual cover sizing", test_short_cover_near_integer_residual_does_not_round_to_zero),
        ("Nearest integer executable short target", test_projector_uses_nearest_integer_short_target),
        ("Proportional buying-power projection", test_projector_enforces_buying_power_cap_proportionally),
        ("Residual-aware integer short delta", test_projector_short_residual_produces_integer_order_delta),
        ("Buying-power scenario diagnostics", test_projector_logs_buffer_scenarios),
        ("Lexicographic weight-error priority", test_projector_uses_buying_power_only_as_secondary_objective),
        ("Final gross capacity target", test_projector_enforces_final_gross_capacity_target),
        ("Total RegT capacity reconstruction", test_total_regt_capacity_reconstruction),
        ("Portfolio-history request parameters", test_portfolio_history_uses_explicit_range_without_period),
        ("Projection audit staged-entry selection", test_projection_audit_prefers_staged_entry_snapshot),
        ("Min-trade short carry safety", test_min_trade_short_carry_cannot_emit_residual_order),
        ("Weight-based min-trade threshold", test_min_trade_threshold_scales_with_weight_error_budget),
        ("Insufficient-qty error classification", test_insufficient_qty_error_is_not_buying_power_abort),
        ("Marketable-limit repeated requotes", test_marketable_limit_requotes_until_timeout),
        ("Bounded marketable-limit ladder", test_marketable_limit_ladder_is_bounded_and_unique),
        ("Live quote marketable-limit reference", test_marketable_limit_uses_live_quote_side),
        ("Concurrent symbol execution", test_order_batch_runs_symbols_concurrently),
        ("Audit requote field propagation", test_audit_keeps_requote_fields),
        ("Audit submit-error payload parsing", test_audit_parses_submit_error_payload),
        ("Audit not-submitted reason", test_audit_marks_not_submitted_reason),
        ("Audit staged rebuild fill merge", test_audit_merges_staged_rebuild_fill_after_cancel),
        ("Total RegT position-capacity audit", test_position_capacity_uses_total_regt_capacity),
    ]
    failed = 0
    for name, fn in tests:
        print(f"\n[TEST] {name}")
        try:
            fn()
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            failed += 1
    if failed:
        print(f"\n[FAIL] {failed}/{len(tests)} tests failed")
        return 1
    print(f"\n[PASS] All {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
