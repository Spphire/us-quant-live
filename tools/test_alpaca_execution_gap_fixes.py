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
    _DecisionPhaseTimingRecorder,
    _PersistentRunEvents,
    _build_submission_capability_guard,
    _build_margin_reconciliation,
    _build_target_capability_drift,
    _build_target_capability_snapshot,
    OrderInstruction,
    _build_order_instructions,
    _client_order_id,
    _collect_intraday_bars_snapshot,
    _collect_portfolio_history_snapshot,
    _effective_min_trade_notional,
    _execution_attempt_outcome_summary,
    _execution_run_succeeded,
    _final_logical_execution_records,
    _is_insufficient_buying_power_error,
    _is_insufficient_qty_available_error,
    _marketable_offset_ladder,
    _mark_event,
    _submit_and_track_orders,
    _submit_order_with_collision_recovery,
    _submit_staged_regt_orders,
    _total_regt_buying_power_capacity,
    _write_json_file_if_absent,
)
from src.executable_target_projector import (  # noqa: E402
    project_executable_targets,
    resolve_initial_margin_requirement,
)
from vendors import AlpacaRequestError, LongbridgeQuoteError  # noqa: E402
from tools.daily_audit_report import (  # noqa: E402
    _build_execution_attempt_outcome_audit,
    _build_executable_target_projection_outputs,
    _build_execution_attribution_outputs,
    _build_order_attempt_rows,
    _build_order_trace,
    _build_position_capacity_summary,
    _build_quote_evidence,
)
from tools.execution_quality import _logical_records  # noqa: E402


def test_first_run_json_evidence_is_not_overwritten() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "broker_day_open_snapshot.json"
        assert _write_json_file_if_absent(path, {"capture": "first"}) is True
        assert _write_json_file_if_absent(path, {"capture": "restart"}) is False
        assert json.loads(path.read_text(encoding="utf-8")) == {"capture": "first"}


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


class _ManualClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


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
            str(symbol).upper(): {
                "bp": 90.0,
                "ap": 91.0,
                "bs": 20,
                "as": 30,
                "bx": "V",
                "ax": "V",
                "z": "A",
                "t": "2026-07-24T14:00:00Z",
                "feed": str(feed),
            }
            for symbol in symbols
        }


class _FractionalClosePrecisionClient(_ImmediateFillConcurrencyClient):
    def __init__(self, error_message: str = "fractional orders cannot be sold short") -> None:
        super().__init__(submit_delay_seconds=0.0)
        self.submitted_qty: list[float] = []
        self.error_message = str(error_message)

    def submit_order(self, **kwargs):
        qty = float(kwargs.get("qty") or 0.0)
        self.submitted_qty.append(qty)
        if len(self.submitted_qty) == 1:
            raise AlpacaRequestError(
                'Alpaca request failed with HTTP 422: {"code":42210000,'
                f'"message":"{self.error_message}"}}'
            )
        return super().submit_order(**kwargs)


class _AmbiguousSubmitClient:
    """Simulate a POST accepted by Alpaca before its response is lost."""

    def __init__(self, existing_status: str | None) -> None:
        self.existing_status = existing_status
        self.submit_requests: list[dict[str, object]] = []
        self.lookup_count = 0
        self.orders: dict[str, dict[str, object]] = {}

    @staticmethod
    def _order(kwargs: dict[str, object], *, status: str) -> dict[str, object]:
        qty = str(kwargs.get("qty") or "0")
        filled_qty = qty if status == "filled" else "0"
        return {
            "id": f"order-{kwargs.get('client_order_id')}",
            "client_order_id": str(kwargs.get("client_order_id") or ""),
            "symbol": str(kwargs.get("symbol") or ""),
            "side": str(kwargs.get("side") or ""),
            "type": str(kwargs.get("type") or ""),
            "time_in_force": str(kwargs.get("time_in_force") or ""),
            "qty": qty,
            "limit_price": kwargs.get("limit_price"),
            "status": status,
            "filled_qty": filled_qty,
            "filled_avg_price": kwargs.get("limit_price") if status == "filled" else None,
            "updated_at": "2026-08-10T14:00:00Z",
        }

    def submit_order(self, **kwargs):
        request = dict(kwargs)
        self.submit_requests.append(request)
        client_order_id = str(request.get("client_order_id") or "")
        if len(self.submit_requests) == 1:
            if self.existing_status is not None:
                self.orders[client_order_id] = self._order(
                    request,
                    status=self.existing_status,
                )
            raise AlpacaRequestError(
                'Alpaca request failed with HTTP 422: '
                '{"code":40010001,"message":"client_order_id must be unique"}'
            )
        order = self._order(request, status="filled")
        self.orders[client_order_id] = dict(order)
        return order

    def get_order_by_client_order_id(self, client_order_id):
        self.lookup_count += 1
        if str(client_order_id) not in self.orders:
            raise AlpacaRequestError("Alpaca request failed with HTTP 404: order not found")
        return dict(self.orders[str(client_order_id)])

    def get_order(self, order_id):
        for order in self.orders.values():
            if str(order.get("id")) == str(order_id):
                return dict(order)
        raise AlpacaRequestError("Alpaca request failed with HTTP 404: order not found")

    def cancel_order(self, order_id):
        return {}


class _StagedNeverFillClient(_QuoteNeverFillClient):
    def list_positions(self):
        return [
            {
                "symbol": "X",
                "side": "long",
                "qty": "10",
                "market_value": "1000",
                "current_price": "100",
            }
        ]

    def get_account(self):
        return {
            "equity": "10000",
            "buying_power": "19000",
            "regt_buying_power": "19000",
        }

    def get_latest_trades(self, *, symbols, feed):
        return {
            str(symbol).upper(): {"p": 100.0, "feed": str(feed)}
            for symbol in symbols
        }


class _StatefulStagedFillClient:
    def __init__(self, signed_qty: float, *, stale_position_reads_after_fill: int = 0) -> None:
        self.signed_qty = float(signed_qty)
        self.initial_signed_qty = float(signed_qty)
        self.stale_position_reads_after_fill = max(0, int(stale_position_reads_after_fill))
        self.orders: dict[str, dict[str, object]] = {}
        self.submissions: list[dict[str, object]] = []

    def submit_order(self, **kwargs):
        qty = float(kwargs.get("qty") or 0.0)
        side = str(kwargs.get("side") or "")
        client_order_id = str(kwargs.get("client_order_id") or "")
        before = float(self.signed_qty)
        is_release_sell = "_rsl_" in client_order_id
        is_release_cover = "_rbc_" in client_order_id
        is_unified_release = "_rel_" in client_order_id
        if is_unified_release and before > 0.0:
            is_release_sell = True
        if is_unified_release and before < 0.0:
            is_release_cover = True
        if is_release_sell:
            assert before > 0.0, (before, kwargs)
            assert side == "sell", kwargs
            assert qty <= before + 1e-9, (qty, before, kwargs)
        if is_release_cover:
            assert before < 0.0, (before, kwargs)
            assert side == "buy", kwargs
            assert qty <= abs(before) + 1e-9, (qty, before, kwargs)

        self.signed_qty += qty if side == "buy" else -qty
        if abs(self.signed_qty) <= 1e-9:
            self.signed_qty = 0.0
        order_id = client_order_id
        order = {
            "id": order_id,
            "client_order_id": client_order_id,
            "symbol": str(kwargs.get("symbol") or "X"),
            "side": side,
            "type": str(kwargs.get("type") or "market"),
            "time_in_force": str(kwargs.get("time_in_force") or "day"),
            "qty": str(qty),
            "status": "filled",
            "filled_qty": str(qty),
            "filled_avg_price": "100",
            "updated_at": "2026-07-27T14:00:00Z",
        }
        self.orders[order_id] = dict(order)
        self.submissions.append(
            {
                "client_order_id": client_order_id,
                "side": side,
                "qty": qty,
                "signed_qty_before": before,
                "signed_qty_after": float(self.signed_qty),
                "is_release": bool(is_release_sell or is_release_cover),
            }
        )
        return dict(order)

    def get_order(self, order_id):
        return dict(self.orders[order_id])

    def list_positions(self):
        reported_signed_qty = float(self.signed_qty)
        if self.submissions and self.stale_position_reads_after_fill > 0:
            reported_signed_qty = float(self.initial_signed_qty)
            self.stale_position_reads_after_fill -= 1
        if abs(reported_signed_qty) <= 1e-9:
            return []
        return [
            {
                "symbol": "X",
                "side": "long" if reported_signed_qty > 0 else "short",
                "qty": str(abs(reported_signed_qty)),
                "market_value": str(abs(reported_signed_qty) * 100.0),
                "current_price": "100",
            }
        ]

    def get_account(self):
        return {
            "equity": "10000",
            "buying_power": "19000",
            "regt_buying_power": "19000",
        }

    def get_latest_trades(self, *, symbols, feed):
        return {
            str(symbol).upper(): {"p": 100.0, "feed": str(feed)}
            for symbol in symbols
        }

    def get_latest_quotes(self, *, symbols, feed):
        return {
            str(symbol).upper(): {
                "bp": 100.0,
                "ap": 100.0,
                "feed": str(feed),
            }
            for symbol in symbols
        }


class _MultiSymbolUnifiedReleaseClient:
    def __init__(self, submit_delay_seconds: float = 0.08) -> None:
        self.signed_qty = {"LONG": 10.0, "SHORT": -10.0}
        self.submit_delay_seconds = float(submit_delay_seconds)
        self.lock = threading.Lock()
        self.active_submits = 0
        self.max_active_submits = 0
        self.orders: dict[str, dict[str, object]] = {}

    def submit_order(self, **kwargs):
        client_order_id = str(kwargs.get("client_order_id") or "")
        symbol = str(kwargs.get("symbol") or "")
        side = str(kwargs.get("side") or "")
        qty = float(kwargs.get("qty") or 0.0)
        assert "_rel_" in client_order_id, client_order_id
        with self.lock:
            before = float(self.signed_qty[symbol])
            if symbol == "LONG":
                assert side == "sell" and before > 0.0, kwargs
            else:
                assert side == "buy" and before < 0.0, kwargs
            self.active_submits += 1
            self.max_active_submits = max(
                self.max_active_submits, self.active_submits
            )
        try:
            time.sleep(self.submit_delay_seconds)
            with self.lock:
                self.signed_qty[symbol] += qty if side == "buy" else -qty
                if abs(self.signed_qty[symbol]) <= 1e-9:
                    self.signed_qty[symbol] = 0.0
                order = {
                    "id": client_order_id,
                    "client_order_id": client_order_id,
                    "symbol": symbol,
                    "side": side,
                    "type": str(kwargs.get("type") or "market"),
                    "time_in_force": str(kwargs.get("time_in_force") or "day"),
                    "qty": str(qty),
                    "status": "filled",
                    "filled_qty": str(qty),
                    "filled_avg_price": "100",
                    "updated_at": "2026-07-31T14:00:00Z",
                }
                self.orders[client_order_id] = dict(order)
            return dict(order)
        finally:
            with self.lock:
                self.active_submits -= 1

    def get_order(self, order_id):
        with self.lock:
            return dict(self.orders[order_id])

    def list_positions(self):
        with self.lock:
            signed_qty = dict(self.signed_qty)
        return [
            {
                "symbol": symbol,
                "side": "long" if qty > 0 else "short",
                "qty": str(abs(qty)),
                "market_value": str(abs(qty) * 100.0),
                "current_price": "100",
            }
            for symbol, qty in signed_qty.items()
            if abs(qty) > 1e-9
        ]

    def get_account(self):
        return {
            "equity": "10000",
            "buying_power": "20000",
            "regt_buying_power": "20000",
        }

    def get_latest_trades(self, *, symbols, feed):
        return {
            str(symbol).upper(): {"p": 100.0, "feed": str(feed)}
            for symbol in symbols
        }

    def get_latest_quotes(self, *, symbols, feed):
        return {
            str(symbol).upper(): {
                "bp": 100.0,
                "ap": 100.0,
                "feed": str(feed),
            }
            for symbol in symbols
        }


class _EntryRepairFillClient(_StatefulStagedFillClient):
    def submit_order(self, **kwargs):
        client_order_id = str(kwargs.get("client_order_id") or "")
        if "_ent_" not in client_order_id or "_erp_" in client_order_id:
            return super().submit_order(**kwargs)

        qty = float(kwargs.get("qty") or 0.0)
        side = str(kwargs.get("side") or "")
        order = {
            "id": client_order_id,
            "client_order_id": client_order_id,
            "symbol": str(kwargs.get("symbol") or "X"),
            "side": side,
            "type": str(kwargs.get("type") or "limit"),
            "time_in_force": str(kwargs.get("time_in_force") or "day"),
            "qty": str(qty),
            "limit_price": kwargs.get("limit_price"),
            "status": "canceled",
            "filled_qty": "0",
            "filled_avg_price": None,
            "updated_at": "2026-07-27T14:00:00Z",
        }
        self.orders[client_order_id] = dict(order)
        self.submissions.append(
            {
                "client_order_id": client_order_id,
                "side": side,
                "qty": qty,
                "signed_qty_before": float(self.signed_qty),
                "signed_qty_after": float(self.signed_qty),
                "is_release": False,
                "forced_unfilled_entry": True,
            }
        )
        return dict(order)


class _PortfolioHistoryClient:
    def __init__(self) -> None:
        self.kwargs = None

    def get_portfolio_history(self, **kwargs):
        self.kwargs = dict(kwargs)
        return {"timestamp": [], "equity": []}


class _IntradayFallbackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_stock_bars(self, **kwargs):
        self.calls.append(dict(kwargs))
        feed = str(kwargs.get("feed") or "")
        symbols = [str(symbol) for symbol in kwargs.get("symbols") or []]
        available = {"X"} if feed == "iex" else {"Y"}
        return [
            {
                "symbol": symbol,
                "timestamp": "2026-07-24T14:00:00Z",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            }
            for symbol in symbols
            if symbol in available
        ]


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
    assets=None,
    betas=None,
    equity=90000.0,
    buying_power=360000.0,
    buffer=0.90,
    total_capacity=None,
    gross_capacity_ratio=0.95,
    sizing_adverse_offset_bps=12.0,
    short_buying_power_adverse_offset_bps=300.0,
    min_trade_notional=0.0,
    executable_beta_band=0.01,
):
    default_assets = {
        symbol: {
            "shortable": True,
            "fractionable": True,
            "marginable": True,
            "maintenance_margin_requirement": 30,
            "margin_requirement_long": 30,
            "margin_requirement_short": 30,
        }
        for symbol in set(weights) | set(current_qty or {})
    }
    if assets:
        default_assets.update(assets)
    return project_executable_targets(
        raw_target_signed_weights=weights,
        current_signed_qty=current_qty or {},
        current_signed_notional=current_notional or {},
        reference_prices=prices,
        assets_by_symbol=default_assets,
        account_equity=equity,
        buying_power=buying_power,
        buying_power_buffer=buffer,
        min_trade_notional=min_trade_notional,
        qty_decimals=4,
        whole_shares_only=False,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        sizing_adverse_offset_bps=sizing_adverse_offset_bps,
        short_buying_power_adverse_offset_bps=short_buying_power_adverse_offset_bps,
        total_buying_power_capacity=total_capacity,
        gross_capacity_target_ratio=gross_capacity_ratio,
        target_beta_by_symbol=betas,
        executable_beta_band=executable_beta_band,
    )


def test_margin_requirement_resolution_is_side_specific_and_fail_closed():
    ordinary = {
        "marginable": True,
        "maintenance_margin_requirement": 30,
        "margin_requirement_long": 30,
        "margin_requirement_short": 30,
    }
    long_special = {
        "marginable": True,
        "maintenance_margin_requirement": 75,
        "margin_requirement_long": 75,
        "margin_requirement_short": 100,
    }
    ordinary_long = resolve_initial_margin_requirement(
        asset=ordinary,
        side="long",
        reference_price=100.0,
    )
    special_long = resolve_initial_margin_requirement(
        asset=long_special,
        side="long",
        reference_price=100.0,
    )
    special_short = resolve_initial_margin_requirement(
        asset=long_special,
        side="short",
        reference_price=100.0,
    )
    missing = resolve_initial_margin_requirement(
        asset={"marginable": True},
        side="long",
        reference_price=100.0,
    )
    non_marginable = resolve_initial_margin_requirement(
        asset={"marginable": False},
        side="long",
        reference_price=100.0,
    )
    low_price_short = resolve_initial_margin_requirement(
        asset=ordinary,
        side="short",
        reference_price=1.0,
    )

    assert ordinary_long["initial_margin_rate"] == 0.50, ordinary_long
    assert special_long["initial_margin_rate"] == 0.75, special_long
    assert special_short["initial_margin_rate"] == 1.00, special_short
    assert missing["initial_margin_rate"] == 1.00, missing
    assert missing["initial_margin_requirement_source"] == "missing_asset_margin_metadata_fail_closed"
    assert non_marginable["initial_margin_rate"] == 1.00, non_marginable
    assert low_price_short["initial_margin_rate"] == 2.50, low_price_short
    assert low_price_short["initial_margin_requirement_source"] == "regt_low_price_short_rule"
    print("  [OK] margin rates use side metadata, Reg T floors, and fail-closed fallbacks")


def test_projector_trims_high_margin_target_before_ordinary_target():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"ORD": 0.75, "SPECIAL": 0.75},
        prices={"ORD": 100.0, "SPECIAL": 100.0},
        assets={
            "SPECIAL": {
                "shortable": True,
                "fractionable": True,
                "marginable": True,
                "maintenance_margin_requirement": 100,
                "margin_requirement_long": 100,
                "margin_requirement_short": 100,
            }
        },
        equity=100000.0,
        buying_power=400000.0,
        buffer=0.95,
        total_capacity=200000.0,
        gross_capacity_ratio=0.95,
        sizing_adverse_offset_bps=0.0,
        short_buying_power_adverse_offset_bps=0.0,
    )
    assert diagnostics["hard_constraints_satisfied"], diagnostics
    assert abs(lattice_qty["ORD"] - 750.0) < 1e-6, lattice_qty
    assert abs(lattice_qty["SPECIAL"] - 575.0) < 1e-6, lattice_qty
    assert abs(diagnostics["projected_initial_margin"] - 95000.0) < 1e-5, diagnostics
    assert abs(diagnostics["projected_regt_buying_power"] - 10000.0) < 1e-5, diagnostics
    assert abs(diagnostics["projected_final_gross_notional"] - 132500.0) < 1e-5, diagnostics
    print("  [OK] high-margin target is reduced only as needed under the 95% margin cap")


def test_projector_beta_uses_nominal_signed_notional_without_margin_multiplier():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"LONG": 0.75, "SHORT": -0.75},
        prices={"LONG": 100.0, "SHORT": 100.0},
        assets={
            "SHORT": {
                "shortable": True,
                "fractionable": True,
                "marginable": True,
                "maintenance_margin_requirement": 100,
                "margin_requirement_long": 100,
                "margin_requirement_short": 100,
            }
        },
        betas={"LONG": 1.0, "SHORT": 1.0},
        equity=100000.0,
        buying_power=400000.0,
        buffer=0.95,
        total_capacity=200000.0,
        gross_capacity_ratio=0.95,
        sizing_adverse_offset_bps=0.0,
        short_buying_power_adverse_offset_bps=0.0,
    )
    rows = {row["symbol"]: row for row in diagnostics["symbols"]}
    assert diagnostics["hard_constraints_satisfied"], diagnostics
    assert diagnostics["beta_constraint_enforced"], diagnostics
    assert abs(diagnostics["projected_net_beta"] - 0.01) < 1e-8, diagnostics
    assert abs(rows["LONG"]["projected_beta_exposure"] - 0.64) < 1e-8, rows
    assert abs(rows["SHORT"]["projected_beta_exposure"] + 0.63) < 1e-8, rows
    assert lattice_qty == {"LONG": 640.0, "SHORT": -630.0}, lattice_qty
    assert abs(diagnostics["projected_final_gross_notional"] - 127000.0) < 1e-5
    assert abs(diagnostics["projected_initial_margin"] - 95000.0) < 1e-5
    print("  [OK] beta stays 1x nominal while margin uses per-symbol coefficients")


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
    assert diagnostics["solver"]["tertiary_optimization_used"]
    assert abs(
        diagnostics["solver"]["primary_weight_error_objective"]
        - diagnostics["optimizer_pre_min_trade_summary"]["tracking_error_l1_weight"]
    ) < 1e-7, diagnostics
    print("  [OK] equal weight-error tie uses higher exposure only in secondary solve")


def test_projector_reports_constraint_floor_and_min_trade_increment():
    _, _, diagnostics = project_executable_targets(
        raw_target_signed_weights={"X": 0.01},
        current_signed_qty={"X": 95.0},
        current_signed_notional={"X": 95.0},
        reference_prices={"X": 1.0},
        assets_by_symbol={"X": {"shortable": True, "fractionable": True}},
        account_equity=10000.0,
        buying_power=40000.0,
        buying_power_buffer=0.95,
        min_trade_notional=10.0,
        qty_decimals=4,
        whole_shares_only=False,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        sizing_adverse_offset_bps=12.0,
        short_buying_power_adverse_offset_bps=300.0,
    )
    assert diagnostics["projection_error_floor_proven_optimal"], diagnostics
    assert diagnostics["projection_error_floor_l1_weight"] < 1e-10, diagnostics
    assert abs(diagnostics["tracking_error_l1_weight"] - 0.0005) < 1e-10, diagnostics
    assert abs(
        diagnostics["min_trade_filter_incremental_error_l1_weight"] - 0.0005
    ) < 1e-10, diagnostics
    assert diagnostics["tracking_error_long_l1_weight"] == diagnostics[
        "tracking_error_l1_weight"
    ]
    assert diagnostics["tracking_error_short_l1_weight"] == 0.0
    print("  [OK] projector separates the constraint floor from min-trade filtering")


def test_min_trade_carry_cannot_breach_initial_margin_cap():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"X": 0.95},
        prices={"X": 100.0},
        current_qty={"X": 950.5},
        current_notional={"X": 95050.0},
        assets={
            "X": {
                "shortable": True,
                "fractionable": True,
                "marginable": True,
                "maintenance_margin_requirement": 100,
                "margin_requirement_long": 100,
                "margin_requirement_short": 100,
            }
        },
        equity=100000.0,
        buying_power=10000.0,
        buffer=0.95,
        total_capacity=200000.0,
        gross_capacity_ratio=0.95,
        sizing_adverse_offset_bps=0.0,
        short_buying_power_adverse_offset_bps=0.0,
        min_trade_notional=100.0,
    )
    row = diagnostics["symbols"][0]
    assert lattice_qty["X"] == 950.0, lattice_qty
    assert diagnostics["hard_constraints_satisfied"], diagnostics
    assert diagnostics["projected_initial_margin"] == 95000.0, diagnostics
    assert "min_trade_notional_waived_for_hard_constraint" in row["constraint_reasons"], row
    assert "min_trade_carry_rejected_initial_margin_cap" in row["constraint_reasons"], row
    print("  [OK] tiny reductions are retained when carrying would breach the margin cap")


def test_min_trade_carry_cannot_breach_beta_limit():
    _, lattice_qty, diagnostics = _project_targets(
        weights={"LONG": 0.50, "SHORT": -0.50},
        prices={"LONG": 100.0, "SHORT": 100.0},
        current_qty={"LONG": 500.5, "SHORT": -500.0},
        current_notional={"LONG": 50050.0, "SHORT": -50000.0},
        betas={"LONG": 1.0, "SHORT": 1.0},
        equity=100000.0,
        buying_power=10000.0,
        buffer=0.95,
        total_capacity=200000.0,
        gross_capacity_ratio=0.95,
        sizing_adverse_offset_bps=0.0,
        short_buying_power_adverse_offset_bps=0.0,
        min_trade_notional=100.0,
        executable_beta_band=0.0001,
    )
    rows = {row["symbol"]: row for row in diagnostics["symbols"]}
    assert lattice_qty == {"LONG": 500.0, "SHORT": -500.0}, lattice_qty
    assert diagnostics["hard_constraints_satisfied"], diagnostics
    assert abs(diagnostics["projected_net_beta"]) < 1e-12, diagnostics
    assert "min_trade_carry_rejected_beta_abs_limit" in rows["LONG"]["constraint_reasons"]
    print("  [OK] min-trade carrying cannot move executable beta outside its 1x band")


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


def test_submission_guard_blocks_missing_short_side():
    guard = _build_submission_capability_guard(
        raw_target_signed_weights={"LONG": 0.50, "SHORT": -0.50},
        capacity_adjusted_target_signed_weights={"LONG": 0.475, "SHORT": -0.475},
        executable_expected_signed_weights={"LONG": 0.475, "SHORT": 0.0},
        current_signed_notional={},
        account_equity=100000.0,
        shorting_enabled=False,
        material_notional_tolerance=10.0,
    )
    assert guard["status"] == "blocked", guard
    assert "account_shorting_disabled_for_required_short_increase" in guard["blocking_reasons"]
    assert "long_short_side_missing_after_projection" in guard["blocking_reasons"]
    assert guard["required_short_increase_symbols"] == ["SHORT"]
    print("  [OK] submission guard blocks a long-only projection of a long/short strategy")


def test_submission_guard_allows_complete_long_short_projection():
    guard = _build_submission_capability_guard(
        raw_target_signed_weights={"LONG": 0.50, "SHORT": -0.50},
        capacity_adjusted_target_signed_weights={"LONG": 0.475, "SHORT": -0.475},
        executable_expected_signed_weights={"LONG": 0.474, "SHORT": -0.473},
        current_signed_notional={},
        account_equity=100000.0,
        shorting_enabled=True,
        material_notional_tolerance=10.0,
    )
    assert guard["status"] == "pass", guard
    assert not guard["blocking_reasons"], guard
    print("  [OK] submission guard allows a complete executable long/short portfolio")


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


def test_total_regt_capacity_uses_stable_equity_baseline():
    total, gross, remaining, source = _total_regt_buying_power_capacity(
        account={
            "equity": "100000",
            "long_market_value": "95000",
            "short_market_value": "-85000",
            "initial_margin": "95000",
            "regt_buying_power": "10000",
        },
        signed_notional={},
    )
    assert total == 200000.0, total
    assert gross == 180000.0, gross
    assert remaining == 10000.0, remaining
    assert "equity" in source, source
    assert "identity_check=200000.000000" in source, source
    print("  [OK] total RegT capacity is the stable 2x-equity baseline")


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


def test_intraday_bar_capture_falls_back_for_primary_missing_symbols():
    client = _IntradayFallbackClient()
    snapshot = _collect_intraday_bars_snapshot(
        client=client,
        symbols=["X", "Y"],
        session_date=date(2026, 7, 24),
        feed="iex",
        fallback_feed="sip",
        label="test",
    )

    assert len(client.calls) == 2, client.calls
    assert client.calls[0]["feed"] == "iex", client.calls
    assert client.calls[1]["feed"] == "sip", client.calls
    assert client.calls[1]["symbols"] == ["Y"], client.calls
    assert snapshot["missing_bar_symbols"] == [], snapshot
    assert snapshot["primary_bar_symbols"] == ["X"], snapshot
    assert snapshot["fallback_bar_symbols"] == ["Y"], snapshot
    assert snapshot["source_by_symbol"] == {"X": "iex", "Y": "sip"}, snapshot
    assert {row["capture_source"] for row in snapshot["bars"]} == {
        "primary",
        "fallback_for_primary_missing",
    }
    print("  [OK] missing IEX minute bars are backfilled from SIP with per-symbol source evidence")


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
        "optimizer_pre_min_trade_summary": {"tracking_error_l1_weight": 0.008},
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
    assert summary["projection_error_floor_l1_weight"] == 0.008, summary
    assert summary["projection_error_floor_proven_optimal"] is False, summary
    assert abs(summary["min_trade_filter_incremental_error_l1_weight"] - 0.002) < 1e-12
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
    cancel_reasons = [attempt["cancel_reason"] for attempt in attempts]
    assert len(attempts) > 2, f"expected repeated requotes beyond one pass, got offsets={offsets}"
    assert max(offsets) <= 50.0, f"max offset cap violated: {offsets}"
    assert all(reason in {"requote_wait_elapsed", "global_order_timeout"} for reason in cancel_reasons)
    assert all(attempt["cancel_requested_at_utc"] for attempt in attempts)
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
    assert attempt["live_bid_price"] == 90.0, attempt
    assert attempt["live_ask_price"] == 91.0, attempt
    assert abs(attempt["live_mid_price"] - 90.5) < 1e-12, attempt
    assert abs(attempt["live_spread_bps"] - (1.0 / 90.5 * 10000.0)) < 1e-9, attempt
    assert attempt["marketable_reference_field"] == "bp", attempt
    assert attempt["cancel_reason"] == "requote_wait_elapsed", attempt
    assert attempt["cancel_requested_at_utc"], attempt
    assert records[0]["attempt_count"] == 1, records[0]
    audit_row = _build_order_attempt_rows(records, [])[0]
    assert audit_row["live_reference_price"] == 90.0, audit_row
    assert audit_row["reference_price_source"] == "latest_quote.bp", audit_row
    assert audit_row["marketable_limit_max_attempts"] == 1, audit_row
    assert audit_row["max_offset_bps"] == 150.0, audit_row
    assert audit_row["live_bid_price"] == 90.0, audit_row
    assert audit_row["live_ask_price"] == 91.0, audit_row
    assert audit_row["live_spread_bps"] == attempt["live_spread_bps"], audit_row
    assert audit_row["cancel_reason"] == "requote_wait_elapsed", audit_row
    assert audit_row["cancel_requested_at_utc"] == attempt["cancel_requested_at_utc"], audit_row
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


def test_order_batch_caps_workers_at_empirical_rate_limit_boundary():
    client = _ImmediateFillConcurrencyClient(submit_delay_seconds=0.05)
    instructions = [
        OrderInstruction(
            symbol=f"C{index}",
            side="buy",
            qty=1.0,
            reference_price=100.0,
            sizing_price=101.0,
            current_notional=0.0,
            target_notional=100.0,
            delta_notional=100.0,
            opening_short=False,
        )
        for index in range(14)
    ]
    records = _submit_and_track_orders(
        client=client,
        instructions=instructions,
        session_token="worker-cap",
        timeout_seconds=5.0,
        poll_seconds=0.1,
        execution_order_style="market",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=1,
        max_workers=14,
    )
    assert client.max_active_submits == 10, client.max_active_submits
    assert all(row["batch_requested_workers"] == 14 for row in records), records
    assert all(row["batch_worker_safety_cap"] == 10 for row in records), records
    assert all(row["batch_effective_workers"] == 10 for row in records), records
    print("  [OK] requested concurrency above ten is capped before broker submission")


def test_duplicate_client_order_id_recovers_existing_fill():
    client = _AmbiguousSubmitClient(existing_status="filled")
    records = _submit_and_track_orders(
        client=client,
        instructions=[
            OrderInstruction(
                symbol="BUD",
                side="buy",
                qty=2.0,
                reference_price=82.7,
                sizing_price=82.8,
                current_notional=-2300.0,
                target_notional=-2134.6,
                delta_notional=165.4,
                opening_short=False,
                current_signed_qty=-28.0,
                target_signed_qty=-26.0,
            )
        ],
        session_token="ambiguous-fill",
        timeout_seconds=2.0,
        poll_seconds=0.05,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=1,
    )
    attempt = records[0]["attempts"][0]
    assert len(client.submit_requests) == 1, client.submit_requests
    assert client.lookup_count == 1, client.lookup_count
    assert records[0]["status_latest"] == "filled", records[0]
    assert records[0]["filled_qty"] == 2.0, records[0]
    assert records[0]["remaining_qty"] == 0.0, records[0]
    assert attempt["submit_recovery"]["outcome"] == "reconciled_existing_order", attempt
    assert attempt["submit_recovery"]["request_match"] is True, attempt
    print("  [OK] ambiguous duplicate-ID submit adopts the already-filled broker order")


def test_duplicate_client_order_id_requotes_existing_canceled_order():
    client = _AmbiguousSubmitClient(existing_status="canceled")
    records = _submit_and_track_orders(
        client=client,
        instructions=[
            OrderInstruction(
                symbol="SHOP",
                side="sell",
                qty=22.0,
                reference_price=153.73,
                sizing_price=153.6,
                current_notional=0.0,
                target_notional=-3382.06,
                delta_notional=-3382.06,
                opening_short=True,
                current_signed_qty=0.0,
                target_signed_qty=-22.0,
            )
        ],
        session_token="ambiguous-cancel",
        timeout_seconds=2.0,
        poll_seconds=0.05,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0, 25.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=2,
    )
    attempts = records[0]["attempts"]
    assert len(client.submit_requests) == 2, client.submit_requests
    assert records[0]["status_latest"] == "filled", records[0]
    assert records[0]["filled_qty"] == 22.0, records[0]
    assert len(attempts) == 2, attempts
    assert attempts[0]["status_latest"] == "canceled", attempts
    assert attempts[0]["submit_recovery"]["outcome"] == "reconciled_existing_order", attempts
    assert attempts[1]["submit_recovery"]["outcome"] == "normal", attempts
    assert attempts[0]["client_order_id"] != attempts[1]["client_order_id"], attempts
    print("  [OK] recovered canceled order proceeds through the bounded requote ladder")


def test_duplicate_client_order_id_without_order_uses_fresh_id():
    client = _AmbiguousSubmitClient(existing_status=None)
    original_id = "sm_original"
    order, recovery = _submit_order_with_collision_recovery(
        client=client,
        request={
            "symbol": "BUD",
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "qty": "2",
            "limit_price": "82.8",
            "client_order_id": original_id,
        },
        replacement_id_factory=lambda: "sm_replacement",
        lookup_attempts=2,
        lookup_wait_seconds=0.0,
    )
    assert client.lookup_count == 2, client.lookup_count
    assert len(client.submit_requests) == 2, client.submit_requests
    assert client.submit_requests[0]["client_order_id"] == original_id, client.submit_requests
    assert client.submit_requests[1]["client_order_id"] == "sm_replacement", client.submit_requests
    assert client.submit_requests[1]["qty"] == "2", client.submit_requests
    assert order["status"] == "filled", order
    assert recovery["outcome"] == "resubmitted_with_fresh_client_order_id", recovery
    assert recovery["replacement_client_order_id"] == "sm_replacement", recovery
    print("  [OK] unresolved duplicate-ID reservation resubmits unchanged intent with a fresh ID")


def test_client_order_id_preserves_uniqueness_under_length_limit():
    first = _client_order_id(
        "very_long_entry_repair_session_token_i999",
        idx=999,
        side="sell",
        symbol="LONGSYMBOL",
        attempt_no=12,
        nonce="abcdef",
    )
    second = _client_order_id(
        "very_long_entry_repair_session_token_i999",
        idx=999,
        side="sell",
        symbol="LONGSYMBOL",
        attempt_no=12,
        nonce="abcdeg",
    )
    assert len(first) <= 48, first
    assert len(second) <= 48, second
    assert first != second, (first, second)
    print("  [OK] long staged IDs remain distinct inside Alpaca's 48-character limit")


def test_fractional_long_close_retries_one_minimum_unit_lower():
    client = _FractionalClosePrecisionClient()
    records = _submit_and_track_orders(
        client=client,
        instructions=[
            OrderInstruction(
                symbol="HALO",
                side="sell",
                qty=30.7241,
                reference_price=84.265,
                sizing_price=84.265,
                current_notional=2588.966287,
                target_notional=0.0,
                delta_notional=-2588.966287,
                opening_short=False,
                current_signed_qty=30.7241,
                target_signed_qty=0.0,
            )
        ],
        session_token="fractional-close",
        timeout_seconds=5.0,
        poll_seconds=0.1,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=1.0,
        marketable_limit_max_attempts=1,
    )
    assert client.submitted_qty == [30.7241, 30.724], client.submitted_qty
    assert records[0]["status_latest"] == "filled", records[0]
    assert records[0]["fractional_close_retry_count"] == 1, records[0]
    assert abs(records[0]["fractional_close_residual_qty"] - 0.0001) < 1e-10, records[0]

    insufficient_client = _FractionalClosePrecisionClient(
        "insufficient qty available for order (requested: 30.7242, available: 30.7241)"
    )
    instruction = OrderInstruction(
        symbol="HALO",
        side="sell",
        qty=30.7242,
        reference_price=84.265,
        sizing_price=84.265,
        current_notional=2588.966287,
        target_notional=0.0,
        delta_notional=-2588.966287,
        opening_short=False,
        current_signed_qty=30.7241,
        target_signed_qty=0.0,
    )
    insufficient_records = _submit_and_track_orders(
        client=insufficient_client,
        instructions=[instruction],
        session_token="fractional-close-available",
        timeout_seconds=5.0,
        poll_seconds=0.1,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=1.0,
        marketable_limit_max_attempts=1,
    )
    assert insufficient_client.submitted_qty == [30.7242, 30.724], insufficient_client.submitted_qty
    assert insufficient_records[0]["status_latest"] == "filled", insufficient_records[0]
    assert abs(insufficient_records[0]["fractional_close_residual_qty"] - 0.0001) < 1e-10
    print("  [OK] rejected exact fractional close retries 0.0001 share lower")


def test_per_symbol_attempt_budget_bounds_requotes():
    client = _QuoteNeverFillClient()
    instructions = [
        OrderInstruction(
            symbol=symbol,
            side="buy",
            qty=1.0,
            reference_price=100.0,
            sizing_price=101.0,
            current_notional=0.0,
            target_notional=100.0,
            delta_notional=100.0,
            opening_short=False,
        )
        for symbol in ("X", "Y")
    ]
    records = _submit_and_track_orders(
        client=client,
        instructions=instructions,
        session_token="budget",
        timeout_seconds=2.0,
        poll_seconds=0.05,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0, 25.0, 75.0, 150.0],
        marketable_limit_requote_wait_seconds=0.1,
        marketable_limit_max_attempts=4,
        max_workers=1,
        execution_price_feed="iex",
        max_attempts_by_symbol={"X": 1, "Y": 2},
    )
    by_symbol = {str(record["symbol"]): record for record in records}
    assert by_symbol["X"]["attempt_count"] == 1, by_symbol["X"]
    assert by_symbol["X"]["marketable_limit_max_attempts"] == 1, by_symbol["X"]
    assert by_symbol["Y"]["attempt_count"] == 2, by_symbol["Y"]
    assert by_symbol["Y"]["marketable_limit_max_attempts"] == 2, by_symbol["Y"]
    print("  [OK] per-symbol remaining attempt budgets cap cross-round requotes")


def test_staged_release_attempt_budget_is_global_across_rounds():
    client = _StagedNeverFillClient()
    snapshots: list[dict[str, object]] = []
    records, diagnostics = _submit_staged_regt_orders(
        client=client,
        initial_instructions=[
            OrderInstruction(
                symbol="X",
                side="sell",
                qty=5.0,
                reference_price=100.0,
                sizing_price=99.0,
                current_notional=1000.0,
                target_notional=500.0,
                delta_notional=-500.0,
                opening_short=False,
            )
        ],
        target_signed_weights={"X": 0.05},
        raw_target_signed_weights={"X": 0.05},
        assets_by_symbol={
            "X": {
                "symbol": "X",
                "tradable": True,
                "fractionable": True,
                "shortable": True,
            }
        },
        fallback_prices={"X": 100.0},
        session_token="stage-budget",
        execution_price_feed="iex",
        account_equity=10000.0,
        min_trade_notional_floor=1.0,
        min_trade_weight_bps=0.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        buying_power_buffer=0.95,
        gross_capacity_target_ratio=0.95,
        short_buying_power_adverse_offset_bps=300.0,
        release_timeout_seconds=10.0,
        entry_timeout_seconds=1.0,
        poll_seconds=0.01,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=12.0,
        marketable_limit_max_offset_bps=150.0,
        marketable_limit_requote_steps_bps=[0.0, 25.0, 75.0, 150.0],
        marketable_limit_requote_wait_seconds=0.02,
        marketable_limit_max_attempts=4,
        execution_workers=2,
        release_max_rounds=3,
        release_round_extra_bps=10.0,
        release_round_sleep_seconds=0.0,
        stage_snapshots=snapshots,
    )
    assert client.submit_count == 4, client.submit_count
    assert len(records) == 1, records
    assert records[0]["attempt_count"] == 4, records[0]
    assert records[0]["stage_symbol_attempt_count_after"] == 4, records[0]
    assert records[0]["stage_symbol_attempts_remaining"] == 0, records[0]
    assert diagnostics["entry_aborted"] is True, diagnostics
    assert diagnostics["entry_abort_reason"] == "reduce_exposure_attempt_budget_exhausted", diagnostics
    assert diagnostics["release_unfilled_action_classes"] == ["release_sell_long"], diagnostics
    assert diagnostics["release_attempt_counts_by_symbol"] == {"X": 4}, diagnostics
    assert diagnostics["release_attempt_budget_exhausted_symbols"] == ["X"], diagnostics
    release_snapshots = [item for item in snapshots if item.get("snapshot_type") == "release_round"]
    assert len(release_snapshots) == 1, release_snapshots
    assert release_snapshots[0]["stage_symbol_attempt_counts"] == {"X": 4}
    print("  [OK] three release rounds share one four-attempt per-symbol budget")


def test_staged_release_sides_share_one_parallel_batch():
    client = _MultiSymbolUnifiedReleaseClient()
    weights = {"LONG": 0.0, "SHORT": 0.0}
    current_qty = {"LONG": 10.0, "SHORT": -10.0}
    current_notional = {"LONG": 1000.0, "SHORT": -1000.0}
    assets = {
        symbol: {
            "symbol": symbol,
            "tradable": True,
            "fractionable": True,
            "shortable": True,
        }
        for symbol in weights
    }
    instructions, skipped = _build_order_instructions(
        target_signed_weights=weights,
        current_signed_notional=current_notional,
        current_signed_qty=current_qty,
        account_equity=10000.0,
        reference_prices={"LONG": 100.0, "SHORT": 100.0},
        assets_by_symbol=assets,
        min_trade_notional=1.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )
    assert not skipped, skipped
    snapshots: list[dict[str, object]] = []
    records, diagnostics = _submit_staged_regt_orders(
        client=client,
        initial_instructions=instructions,
        target_signed_weights=weights,
        raw_target_signed_weights=weights,
        assets_by_symbol=assets,
        fallback_prices={"LONG": 100.0, "SHORT": 100.0},
        session_token="unified-release",
        execution_price_feed="iex",
        account_equity=10000.0,
        min_trade_notional_floor=1.0,
        min_trade_weight_bps=0.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        buying_power_buffer=0.95,
        gross_capacity_target_ratio=0.95,
        short_buying_power_adverse_offset_bps=300.0,
        release_timeout_seconds=2.0,
        entry_timeout_seconds=2.0,
        poll_seconds=0.01,
        execution_order_style="market",
        marketable_limit_base_offset_bps=0.0,
        marketable_limit_max_offset_bps=50.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.01,
        marketable_limit_max_attempts=2,
        execution_workers=2,
        release_max_rounds=2,
        release_round_extra_bps=5.0,
        release_round_sleep_seconds=0.0,
        stage_snapshots=snapshots,
        initial_current_signed_qty=current_qty,
    )
    assert client.max_active_submits == 2, client.max_active_submits
    assert len(records) == 2, records
    assert {record["stage"] for record in records} == {
        "release_sell_long",
        "release_buy_to_cover",
    }, records
    assert {record["macro_stage"] for record in records} == {
        "reduce_exposure"
    }, records
    assert len({record["batch_started_at_utc"] for record in records}) == 1, records
    assert diagnostics["release_execution_mode"] == "unified_reduce_exposure"
    assert diagnostics["release_fully_filled"] is True, diagnostics
    assert diagnostics["release_rounds"][0]["action_class_counts"] == {
        "release_buy_to_cover": 1,
        "release_sell_long": 1,
    }, diagnostics
    assert all(
        item.get("concurrent") is True
        for item in diagnostics["release_substages"]
    ), diagnostics
    print("  [OK] long reductions and short covers share one parallel worker pool")


def _run_stateful_staged_case(
    *,
    initial_signed_qty: float,
    target_signed_weight: float,
    stale_position_reads_after_fill: int = 0,
    execution_quote_client: object | None = None,
) -> tuple[_StatefulStagedFillClient, list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    client = _StatefulStagedFillClient(
        initial_signed_qty,
        stale_position_reads_after_fill=stale_position_reads_after_fill,
    )
    current_qty = {"X": float(initial_signed_qty)}
    current_notional = {"X": float(initial_signed_qty) * 100.0}
    weights = {"X": float(target_signed_weight)}
    assets = {
        "X": {
            "symbol": "X",
            "tradable": True,
            "fractionable": True,
            "shortable": True,
        }
    }
    instructions, skipped = _build_order_instructions(
        target_signed_weights=weights,
        current_signed_notional=current_notional,
        current_signed_qty=current_qty,
        account_equity=10000.0,
        reference_prices={"X": 100.0},
        assets_by_symbol=assets,
        min_trade_notional=1.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )
    assert not skipped, skipped
    assert len(instructions) == 1, instructions
    snapshots: list[dict[str, object]] = []
    records, diagnostics = _submit_staged_regt_orders(
        client=client,
        execution_quote_client=execution_quote_client,
        initial_instructions=instructions,
        target_signed_weights=weights,
        raw_target_signed_weights=weights,
        assets_by_symbol=assets,
        fallback_prices={"X": 100.0},
        session_token="stateful-stage",
        execution_price_feed="iex",
        account_equity=10000.0,
        min_trade_notional_floor=1.0,
        min_trade_weight_bps=0.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        buying_power_buffer=0.95,
        gross_capacity_target_ratio=0.95,
        short_buying_power_adverse_offset_bps=300.0,
        release_timeout_seconds=2.0,
        entry_timeout_seconds=2.0,
        poll_seconds=0.01,
        execution_order_style="market",
        marketable_limit_base_offset_bps=0.0,
        marketable_limit_max_offset_bps=50.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.01,
        marketable_limit_max_attempts=2,
        execution_workers=2,
        release_max_rounds=3,
        release_round_extra_bps=5.0,
        release_round_sleep_seconds=0.0,
        stage_snapshots=snapshots,
        initial_current_signed_qty=current_qty,
    )
    return client, records, diagnostics, snapshots


def test_staged_long_to_short_stops_at_zero_before_entry():
    client, records, diagnostics, snapshots = _run_stateful_staged_case(
        initial_signed_qty=10.0,
        target_signed_weight=-0.05,
    )
    assert diagnostics["release_target_signed_weights"] == {"X": 0.0}, diagnostics
    assert diagnostics["initial_deferred_entry_count"] == 1, diagnostics
    assert len(client.submissions) == 2, client.submissions
    release, entry = client.submissions
    assert release["is_release"] is True and release["side"] == "sell", release
    assert release["qty"] == 10.0 and release["signed_qty_after"] == 0.0, release
    assert entry["is_release"] is False and entry["side"] == "sell", entry
    assert entry["qty"] == 5.0 and entry["signed_qty_after"] == -5.0, entry
    release_rounds = [row for row in snapshots if row.get("snapshot_type") == "release_round"]
    assert len(release_rounds) == 1, release_rounds
    assert release_rounds[0]["fully_filled"] is True, release_rounds[0]
    assert all(record.get("status_latest") == "filled" for record in records), records
    print("  [OK] long-to-short closes exactly to zero once, then opens the short")


def test_staged_short_to_long_stops_at_zero_before_entry():
    client, records, diagnostics, snapshots = _run_stateful_staged_case(
        initial_signed_qty=-5.0,
        target_signed_weight=0.05,
    )
    assert diagnostics["release_target_signed_weights"] == {"X": 0.0}, diagnostics
    assert diagnostics["initial_deferred_entry_count"] == 1, diagnostics
    assert len(client.submissions) == 2, client.submissions
    release, entry = client.submissions
    assert release["is_release"] is True and release["side"] == "buy", release
    assert release["qty"] == 5.0 and release["signed_qty_after"] == 0.0, release
    assert entry["is_release"] is False and entry["side"] == "buy", entry
    assert entry["qty"] == 5.0 and entry["signed_qty_after"] == 5.0, entry
    release_rounds = [row for row in snapshots if row.get("snapshot_type") == "release_round"]
    assert len(release_rounds) == 1, release_rounds
    assert all(record.get("status_latest") == "filled" for record in records), records
    print("  [OK] short-to-long covers exactly to zero once, then opens the long")


def test_staged_same_side_reduction_has_no_entry_leg():
    client, records, diagnostics, snapshots = _run_stateful_staged_case(
        initial_signed_qty=10.0,
        target_signed_weight=0.05,
    )
    assert diagnostics["release_target_signed_weights"] == {"X": 0.05}, diagnostics
    assert diagnostics["initial_deferred_entry_count"] == 0, diagnostics
    assert len(client.submissions) == 1, client.submissions
    release = client.submissions[0]
    assert release["is_release"] is True and release["side"] == "sell", release
    assert release["qty"] == 5.0 and release["signed_qty_after"] == 5.0, release
    release_rounds = [row for row in snapshots if row.get("snapshot_type") == "release_round"]
    assert len(release_rounds) == 1, release_rounds
    assert all(record.get("status_latest") == "filled" for record in records), records
    print("  [OK] same-side reduction reaches its target without an entry order")


def test_entry_rebuild_executes_new_release_residual_before_entry():
    client = _StatefulStagedFillClient(-16.0)
    snapshots: list[dict[str, object]] = []
    records, diagnostics = _submit_staged_regt_orders(
        client=client,
        initial_instructions=[],
        target_signed_weights={"X": -0.16},
        raw_target_signed_weights={"X": -0.15},
        assets_by_symbol={
            "X": {
                "symbol": "X",
                "tradable": True,
                "fractionable": True,
                "shortable": True,
            }
        },
        fallback_prices={"X": 100.0},
        session_token="entry-residual-release",
        execution_price_feed="iex",
        account_equity=10000.0,
        min_trade_notional_floor=1.0,
        min_trade_weight_bps=0.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        buying_power_buffer=0.95,
        gross_capacity_target_ratio=0.95,
        short_buying_power_adverse_offset_bps=0.0,
        release_timeout_seconds=2.0,
        entry_timeout_seconds=2.0,
        poll_seconds=0.01,
        execution_order_style="market",
        marketable_limit_base_offset_bps=0.0,
        marketable_limit_max_offset_bps=50.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.01,
        marketable_limit_max_attempts=2,
        execution_workers=2,
        release_max_rounds=2,
        release_round_extra_bps=5.0,
        release_round_sleep_seconds=0.0,
        stage_snapshots=snapshots,
        initial_current_signed_qty={"X": -16.0},
    )

    assert client.signed_qty == -15.0, client.submissions
    assert len(client.submissions) == 1, client.submissions
    assert client.submissions[0]["is_release"] is True, client.submissions
    assert client.submissions[0]["side"] == "buy", client.submissions
    assert client.submissions[0]["qty"] == 1.0, client.submissions
    assert len(records) == 1 and records[0]["status_latest"] == "filled", records
    assert records[0]["stage"] == "release_buy_to_cover", records
    assert records[0]["release_origin"] == "entry_rebuild", records
    assert diagnostics["entry_aborted"] is False, diagnostics
    assert diagnostics["entry_rebuild_release_residual_count"] == 1, diagnostics
    assert diagnostics["entry_rebuild_release_residual_fully_filled"] is True
    residual_snapshots = [
        row
        for row in snapshots
        if row.get("snapshot_type") == "entry_rebuild_release_residual"
    ]
    assert len(residual_snapshots) == 1, residual_snapshots
    assert residual_snapshots[0]["fully_filled"] is True, residual_snapshots
    print("  [OK] entry rebuild executes and reconciles newly exposed release residuals")


def test_staged_filled_release_is_not_rebuilt_from_lagged_position():
    client, records, diagnostics, snapshots = _run_stateful_staged_case(
        initial_signed_qty=10.0,
        target_signed_weight=-0.05,
        stale_position_reads_after_fill=2,
    )
    release_submissions = [row for row in client.submissions if row["is_release"]]
    assert len(release_submissions) == 1, client.submissions
    assert len(client.submissions) == 2, client.submissions
    release_rounds = [row for row in snapshots if row.get("snapshot_type") == "release_round"]
    assert len(release_rounds) == 1, release_rounds
    assert release_rounds[0]["filled_instruction_suppressed_rebuild_symbols"] == ["X"]
    reconciliation = [
        row
        for row in snapshots
        if row.get("snapshot_type") == "release_position_reconciliation"
    ]
    assert len(reconciliation) == 1, reconciliation
    assert reconciliation[0]["status"] == "pass", reconciliation[0]
    assert reconciliation[0]["poll_count"] == 2, reconciliation[0]
    assert diagnostics["entry_aborted"] is False, diagnostics
    assert all(record.get("status_latest") == "filled" for record in records), records
    print("  [OK] filled release is not resubmitted when Alpaca positions briefly lag")


def test_staged_quote_failure_after_release_is_controlled_abort():
    class _FailingReferenceQuoteClient:
        def get_reference_prices(self, symbols):
            raise LongbridgeQuoteError("X: stale quote after release")

    client, records, diagnostics, snapshots = _run_stateful_staged_case(
        initial_signed_qty=10.0,
        target_signed_weight=-0.05,
        execution_quote_client=_FailingReferenceQuoteClient(),
    )
    assert len(client.submissions) == 1, client.submissions
    assert len(records) == 1 and records[0]["status_latest"] == "filled", records
    assert diagnostics["entry_aborted"] is True, diagnostics
    assert diagnostics["entry_abort_reason"] == (
        "reduce_exposure_rebuild_quote_validation_failed_after_broker_mutation"
    )
    assert diagnostics["quote_validation_failure_symbols"] == ["X"], diagnostics
    aborts = [row for row in snapshots if row.get("snapshot_type") == "entry_abort"]
    assert len(aborts) == 1 and aborts[0]["broker_mutation_record_count"] == 1, aborts
    print("  [OK] post-release quote failure records a controlled abort without retrying entry")


def test_staged_entry_residual_repair_fills_weight_priority_gap():
    client = _EntryRepairFillClient(0.0, stale_position_reads_after_fill=3)
    weights = {"X": 0.05}
    assets = {
        "X": {
            "symbol": "X",
            "tradable": True,
            "fractionable": True,
            "shortable": True,
        }
    }
    instructions, skipped = _build_order_instructions(
        target_signed_weights=weights,
        current_signed_notional={"X": 0.0},
        current_signed_qty={"X": 0.0},
        account_equity=10000.0,
        reference_prices={"X": 100.0},
        assets_by_symbol=assets,
        min_trade_notional=1.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
    )
    assert not skipped, skipped
    snapshots: list[dict[str, object]] = []
    records, diagnostics = _submit_staged_regt_orders(
        client=client,
        initial_instructions=instructions,
        target_signed_weights=weights,
        raw_target_signed_weights=weights,
        assets_by_symbol=assets,
        fallback_prices={"X": 100.0},
        session_token="entry-repair",
        execution_price_feed="iex",
        account_equity=10000.0,
        min_trade_notional_floor=1.0,
        min_trade_weight_bps=1.0,
        sizing_adverse_offset_bps=0.0,
        qty_decimals=4,
        whole_shares_only=False,
        opening_shorts_whole_shares_only=True,
        short_sales_whole_shares_only=True,
        shorting_enabled=True,
        buying_power_buffer=0.95,
        gross_capacity_target_ratio=0.95,
        short_buying_power_adverse_offset_bps=300.0,
        release_timeout_seconds=2.0,
        entry_timeout_seconds=2.0,
        entry_repair_rounds=1,
        entry_repair_max_attempts=1,
        entry_repair_wait_seconds=0.1,
        poll_seconds=0.01,
        execution_order_style="marketable_limit",
        marketable_limit_base_offset_bps=0.0,
        marketable_limit_max_offset_bps=50.0,
        marketable_limit_requote_steps_bps=[0.0],
        marketable_limit_requote_wait_seconds=0.01,
        marketable_limit_max_attempts=1,
        execution_workers=2,
        release_max_rounds=3,
        release_round_extra_bps=5.0,
        release_round_sleep_seconds=0.0,
        stage_snapshots=snapshots,
        initial_current_signed_qty={"X": 0.0},
    )

    assert len(client.submissions) == 2, client.submissions
    assert client.submissions[0].get("forced_unfilled_entry") is True
    assert "_erp_r01_" in str(client.submissions[1]["client_order_id"])
    assert client.signed_qty == 5.0, client.signed_qty
    assert [record["status_latest"] for record in records] == ["canceled", "filled"], records
    assert [record["stage"] for record in records] == ["entry", "entry_repair"], records
    assert diagnostics["entry_repair_rounds_completed"] == 1, diagnostics
    assert diagnostics["entry_repair_records"] == 1, diagnostics
    assert diagnostics["entry_repair_final_unfilled_symbols"] == [], diagnostics
    assert diagnostics["entry_final_position_reconciliation"]["status"] == "pass"
    assert diagnostics["entry_final_position_reconciliation"]["poll_count"] == 3
    repair_snapshots = [
        row for row in snapshots if row.get("snapshot_type") == "entry_repair"
    ]
    assert len(repair_snapshots) == 1, repair_snapshots
    assert repair_snapshots[0]["candidate_symbols"] == ["X"], repair_snapshots[0]
    assert repair_snapshots[0]["priority_rule"] == "absolute_weight_gap_bps_descending"
    assert repair_snapshots[0]["max_attempts_per_symbol"] == 1
    assert repair_snapshots[0]["limit_offset_bps"] == 50.0
    final_reconciliation = [
        row
        for row in snapshots
        if row.get("snapshot_type") == "entry_final_position_reconciliation"
    ]
    assert len(final_reconciliation) == 1, final_reconciliation
    assert final_reconciliation[0]["status"] == "pass", final_reconciliation[0]
    print("  [OK] canceled entry is repaired once in descending weight-gap priority")


def test_attempt_outcome_summary_separates_requotes_from_terminal_misses():
    records = [
        {
            "symbol": "X",
            "stage": "entry",
            "status_latest": "canceled",
            "qty": 5.0,
            "filled_qty": 0.0,
            "remaining_qty": 5.0,
            "attempts": [
                {
                    "status_latest": "canceled",
                    "cancel_reason": "requote_wait_elapsed",
                }
            ],
        },
        {
            "symbol": "X",
            "stage": "entry_repair",
            "status_latest": "filled",
            "qty": 5.0,
            "filled_qty": 5.0,
            "remaining_qty": 0.0,
            "attempts": [{"status_latest": "filled"}],
        },
        {
            "symbol": "Y",
            "stage": "entry",
            "status_latest": "canceled",
            "qty": 1.0,
            "filled_qty": 0.0,
            "remaining_qty": 1.0,
            "attempts": [
                {
                    "status_latest": "canceled",
                    "cancel_reason": "global_order_timeout",
                }
            ],
        },
    ]
    summary = _execution_attempt_outcome_summary(records)
    final_records = _final_logical_execution_records(records)
    assert summary["broker_attempt_count"] == 3, summary
    assert summary["canceled_attempt_count"] == 2, summary
    assert summary["superseded_requote_canceled_attempt_count"] == 1, summary
    assert summary["terminal_canceled_attempt_count"] == 1, summary
    assert summary["canceled_attempt_reason_counts"] == {
        "global_order_timeout": 1,
        "requote_wait_elapsed": 1,
    }, summary
    assert [attempt["outcome"] for attempt in summary["canceled_attempts"]] == [
        "superseded_requote",
        "terminal_unfilled",
    ], summary
    assert summary["terminal_unfilled_record_count"] == 1, summary
    assert summary["terminal_unfilled_symbols"] == ["Y"], summary
    assert summary["repaired_entry_symbols"] == ["X"], summary
    assert [record["symbol"] for record in final_records] == ["X", "Y"], final_records
    assert [record["status_latest"] for record in final_records] == ["filled", "canceled"]
    print("  [OK] audit separates superseded requotes from final unfilled instructions")


def test_terminal_unfilled_orders_force_an_execution_retry_status():
    assert _execution_run_succeeded(
        submit_error_count=0,
        staged_abort_reason="",
        attempt_outcome_summary={"terminal_unfilled_record_count": 1},
    ) is False
    assert _execution_run_succeeded(
        submit_error_count=0,
        staged_abort_reason="",
        attempt_outcome_summary={"terminal_unfilled_record_count": 0},
    ) is True
    assert _execution_run_succeeded(
        submit_error_count=0,
        staged_abort_reason="entry_unfilled",
        attempt_outcome_summary={"terminal_unfilled_record_count": 0},
    ) is False
    print("  [OK] terminal unfilled instructions cannot be reported as a successful execution")


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
            "stage_symbol_attempt_cap": 4,
            "stage_symbol_attempt_count_before": 2,
            "stage_symbol_attempt_count_after": 4,
            "stage_symbol_attempts_remaining": 0,
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
                    "submit_recovery": {
                        "outcome": "reconciled_existing_order",
                        "recovered_order_id": "broker-1",
                    },
                    "cancel_reason": "requote_wait_elapsed",
                    "cancel_requested_at_utc": "2026-07-27T14:00:01Z",
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
    assert attempt_rows[0]["cancel_reason"] == "requote_wait_elapsed"
    assert attempt_rows[0]["cancel_requested_at_utc"] == "2026-07-27T14:00:01Z"
    assert attempt_rows[0]["submit_recovery_outcome"] == "reconciled_existing_order"
    assert '"recovered_order_id": "broker-1"' in attempt_rows[0]["submit_recovery"]

    execution_rows, summary = _build_execution_attribution_outputs(records, [])
    assert execution_rows[1]["requote_step_index"] == 2
    assert execution_rows[1]["requote_cycle"] == 3
    assert execution_rows[1]["max_offset_bps"] == 50.0
    assert summary["multi_attempt_record_count"] == 1
    assert summary["records_hitting_max_offset_count"] == 1
    assert summary["unfilled_records_hitting_max_offset_count"] == 1
    assert summary["unfilled_records_hitting_max_offset_remaining_notional"] == 100.0
    assert summary["max_stage_symbol_attempt_cap"] == 4
    assert summary["max_stage_symbol_attempt_count_after"] == 4
    assert summary["attempt_budget_exhausted_record_count"] == 1
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


def test_execution_quality_merges_entry_repair_with_initial_entry():
    records = [
        {
            "symbol": "SEZL",
            "side": "sell",
            "stage": "entry",
            "status_latest": "canceled",
            "qty": 3.0,
            "filled_qty": 0.0,
            "remaining_qty": 3.0,
            "reference_price": 154.55,
            "attempt_count": 1,
        },
        {
            "symbol": "SEZL",
            "side": "sell",
            "stage": "entry_repair",
            "status_latest": "filled",
            "qty": 3.0,
            "filled_qty": 3.0,
            "remaining_qty": 0.0,
            "reference_price": 154.55,
            "filled_avg_price": 154.01,
            "attempt_count": 1,
        },
    ]

    logical = _logical_records(records)

    assert len(logical) == 1, logical
    assert logical[0]["stage"] == "entry", logical
    assert logical[0]["status_latest"] == "filled", logical
    assert logical[0]["raw_record_count"] == 2, logical
    assert logical[0]["raw_status_counts"] == {"canceled": 1, "filled": 1}, logical
    assert logical[0]["filled_qty"] == 3.0, logical
    assert logical[0]["remaining_qty"] == 0.0, logical
    print("  [OK] entry repair is one filled logical order, not a separate cancel")


def test_audit_exposes_attempt_outcomes_without_treating_requotes_as_misses():
    with TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "execution_attempt_outcome_summary.json").write_text(
            json.dumps(
                {
                    "broker_attempt_count": 2,
                    "canceled_attempt_count": 1,
                    "superseded_requote_canceled_attempt_count": 1,
                    "terminal_canceled_attempt_count": 0,
                    "terminal_unfilled_record_count": 0,
                    "terminal_unfilled_symbols": [],
                    "repaired_entry_symbol_count": 1,
                    "repaired_entry_symbols": ["SEZL"],
                }
            ),
            encoding="utf-8",
        )

        outcome = _build_execution_attempt_outcome_audit(run_dir, {})

    assert outcome["available"] is True, outcome
    assert outcome["status"] == "pass", outcome
    assert outcome["canceled_attempt_count"] == 1, outcome
    assert outcome["superseded_requote_canceled_attempt_count"] == 1, outcome
    assert outcome["repaired_entry_symbol_count"] == 1, outcome
    assert outcome["terminal_unfilled_record_count"] == 0, outcome
    print("  [OK] audit separates repaired requote cancels from terminal misses")


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


def test_position_capacity_uses_stable_equity_and_separate_margin_metrics():
    with TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "broker_account_after.json").write_text(
            json.dumps(
                {
                    "equity": "100000",
                    "long_market_value": "95000",
                    "short_market_value": "-85000",
                    "position_market_value": "180000",
                    "initial_margin": "95000",
                    "maintenance_margin": "60000",
                    "regt_buying_power": "10000",
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "execution_summary.json").write_text(
            json.dumps(
                {
                    "executable_target_projection": {
                        "gross_capacity_target_ratio": 0.95,
                        "gross_capacity_constraint_enforced": True,
                    }
                }
            ),
            encoding="utf-8",
        )
        summary = _build_position_capacity_summary(run_dir)

    assert summary["total_regt_buying_power_capacity"] == 200000.0, summary
    assert summary["gross_utilization_of_total_bp"] == 0.90, summary
    assert summary["initial_margin_utilization_of_capacity"] == 0.95, summary
    assert summary["regt_buying_power_reserve_ratio"] == 0.05, summary
    assert summary["initial_margin_error_vs_target_notional"] == 0.0, summary
    print("  [OK] audit separates nominal gross, initial margin, and RegT BP reserve")


def test_margin_reconciliation_matches_broker_initial_margin():
    ordinary = {
        "marginable": True,
        "margin_requirement_long": 30,
        "margin_requirement_short": 30,
        "maintenance_margin_requirement": 30,
    }
    special = {
        "marginable": True,
        "margin_requirement_long": 100,
        "margin_requirement_short": 100,
        "maintenance_margin_requirement": 100,
    }
    reconciliation = _build_margin_reconciliation(
        positions=[
            {
                "symbol": "LONG",
                "side": "long",
                "qty": "10",
                "current_price": "100",
                "market_value": "1000",
            },
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": "-5",
                "current_price": "100",
                "market_value": "-500",
            },
        ],
        account={
            "equity": "2000",
            "initial_margin": "1000",
            "regt_buying_power": "2000",
        },
        assets_by_symbol={"LONG": ordinary, "SHORT": special},
        executable_projection={
            "projected_initial_margin": 950.0,
            "initial_margin_cap": 1900.0,
            "projected_net_beta": 0.0,
            "beta_abs_limit": 0.01,
            "hard_constraints_satisfied": True,
        },
    )
    assert reconciliation["status"] == "pass", reconciliation
    assert reconciliation["predicted_initial_margin"] == 1000.0, reconciliation
    assert reconciliation["initial_margin_prediction_error"] == 0.0, reconciliation
    assert reconciliation["predicted_regt_buying_power"] == 2000.0, reconciliation
    assert reconciliation["high_margin_symbol_count"] == 1, reconciliation
    assert reconciliation["extra_initial_margin_vs_regt_floor"] == 250.0, reconciliation
    print("  [OK] post-trade margin model reconciles to broker account fields")


def test_decision_phase_timings_persist_progress_and_failure():
    clock = _ManualClock(100.0)
    with TemporaryDirectory() as tmp:
        recorder = _DecisionPhaseTimingRecorder(
            output_root=Path(tmp),
            run_started_at_utc="2026-07-24T00:00:00+00:00",
            run_started_monotonic=clock(),
            clock=clock,
            utc_now=lambda: "2026-07-24T00:00:00+00:00",
        )
        recorder.start("sec_industry_map", {"symbol_count": 917, "cache_mode": "network"})
        running = json.loads(recorder.path.read_text(encoding="utf-8"))
        assert running["status"] == "running"
        assert running["current_phase"] == "sec_industry_map"
        assert running["phases"][0]["status"] == "running"

        clock.advance(12.5)
        recorder.finish("sec_industry_map", {"industry_record_count": 917})
        clock.advance(0.25)
        recorder.start("alpha_core_build", {"symbol_count": 917})
        clock.advance(4.75)
        failed = recorder.fail(RuntimeError("synthetic alpha failure"))
        persisted = json.loads(recorder.path.read_text(encoding="utf-8"))

    assert failed == persisted
    assert persisted["status"] == "failed"
    assert persisted["current_phase"] is None
    assert persisted["elapsed_seconds"] == 17.5
    assert persisted["decision_compute_elapsed_seconds"] == 17.25
    assert persisted["slowest_phase"]["phase"] == "sec_industry_map"
    assert persisted["phases"][0]["elapsed_seconds"] == 12.5
    assert persisted["phases"][1]["status"] == "failed"
    assert persisted["phases"][1]["context"]["error_type"] == "RuntimeError"
    print("  [OK] decision phase timings persist both live progress and failed-stage evidence")


def test_run_events_are_persisted_immediately_with_sequence_and_elapsed_time():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "run_events.jsonl"
        started = time.monotonic()
        events = _PersistentRunEvents(path=path, run_started_monotonic=started)
        _mark_event(events, "first", {"value": 1})
        first_disk = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        _mark_event(events, "second", {"value": 2})
        second_disk = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert len(first_disk) == 1, first_disk
    assert first_disk[0]["seq"] == 1, first_disk
    assert first_disk[0]["name"] == "first", first_disk
    assert first_disk[0]["run_elapsed_seconds"] is not None, first_disk
    assert [row["seq"] for row in second_disk] == [1, 2], second_disk
    assert second_disk[1]["payload"] == {"value": 2}, second_disk
    print("  [OK] run events survive immediately on disk with sequence and relative timing")


def test_target_capability_drift_explains_new_nonshortable_target():
    raw_targets = {"LONG": 0.5, "TER": -0.5}
    prior = _build_target_capability_snapshot(
        raw_target_signed_weights=raw_targets,
        projection={
            "symbols": [
                {
                    "symbol": "LONG",
                    "capacity_adjusted_target_signed_weight": 0.475,
                    "executable_expected_signed_weight": 0.475,
                },
                {
                    "symbol": "TER",
                    "capacity_adjusted_target_signed_weight": -0.475,
                    "executable_expected_signed_weight": -0.45,
                    "target_lattice_signed_qty": -8,
                    "constraint_reasons": ["short_target_integer"],
                },
            ]
        },
        assets_by_symbol={
            "LONG": {"tradable": True, "fractionable": True},
            "TER": {
                "tradable": True,
                "shortable": True,
                "easy_to_borrow": True,
                "borrow_status": "easy_to_borrow",
            },
        },
        account_shorting_enabled=True,
        run_role="decision",
        input_target_path=None,
    )
    current = _build_target_capability_snapshot(
        raw_target_signed_weights=raw_targets,
        projection={
            "symbols": [
                {
                    "symbol": "LONG",
                    "capacity_adjusted_target_signed_weight": 0.475,
                    "executable_expected_signed_weight": 0.475,
                },
                {
                    "symbol": "TER",
                    "capacity_adjusted_target_signed_weight": -0.475,
                    "executable_expected_signed_weight": 0.0,
                    "target_lattice_signed_qty": 0,
                    "constraint_reasons": ["asset_not_shortable", "short_target_integer"],
                },
            ]
        },
        assets_by_symbol={
            "LONG": {"tradable": True, "fractionable": True},
            "TER": {
                "tradable": True,
                "shortable": False,
                "easy_to_borrow": False,
                "borrow_status": "hard_to_borrow",
            },
        },
        account_shorting_enabled=True,
        run_role="execute",
        input_target_path="decision_targets.csv",
    )
    drift = _build_target_capability_drift(
        current_snapshot=current,
        prior_snapshot=prior,
        prior_snapshot_path=Path("target_capability_snapshot.json"),
    )

    assert current["blocked_target_symbols"] == ["TER"], current
    assert current["nonshortable_short_target_symbols"] == ["TER"], current
    assert drift["status"] == "attention", drift
    assert drift["became_nonshortable_symbols"] == ["TER"], drift
    assert drift["projected_to_zero_now_symbols"] == ["TER"], drift
    assert drift["execution_blocking_change_symbols"] == ["TER"], drift
    print("  [OK] target capability drift explains a decision target becoming nonshortable")


def test_quote_audit_prefers_immediate_post_submission_snapshot():
    with TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "execution_latest_quotes_snapshot.json").write_text(
            json.dumps({"ok": True, "payload": {"X": {"bp": 99.0, "ap": 101.0}}}),
            encoding="utf-8",
        )
        (run_dir / "execution_latest_quotes_snapshot_post_submission.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "feed": "iex",
                    "requested_symbols": ["X"],
                    "payload": {"X": {"bp": 109.0, "ap": 111.0, "t": "2026-07-24T14:01:00Z"}},
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "execution_latest_quotes_snapshot_after.json").write_text(
            json.dumps({"ok": True, "payload": {"X": {"bp": 119.0, "ap": 121.0}}}),
            encoding="utf-8",
        )
        rows, summary = _build_quote_evidence(
            run_dir=run_dir,
            market_price_evidence_rows=[{"symbol": "X", "execute_reference_price_used": 100.0}],
            fill_rows=[],
        )

    assert len(rows) == 1, rows
    assert rows[0]["source_used"] == "post_submission", rows[0]
    assert rows[0]["mid_price"] == 110.0, rows[0]
    assert rows[0]["post_submission_mid_price"] == 110.0, rows[0]
    assert summary["sources"]["post_submission"]["requested_symbol_count"] == 1, summary
    assert summary["sources"]["post_submission"]["quote_symbol_count"] == 1, summary
    print("  [OK] quote audit uses the immediate post-submission snapshot for fill context")


def main() -> int:
    tests = [
        ("Whole-share short target sizing", test_whole_share_short_delta_uses_target_shares),
        ("Fractional short residual close sizing", test_fractional_short_residual_close_does_not_round_up),
        ("Short cover to remaining short stays whole-share", test_short_cover_to_remaining_short_stays_whole_share),
        ("Near-integer short residual cover sizing", test_short_cover_near_integer_residual_does_not_round_to_zero),
        ("Side-specific margin requirement resolution", test_margin_requirement_resolution_is_side_specific_and_fail_closed),
        ("High-margin target projection", test_projector_trims_high_margin_target_before_ordinary_target),
        ("Nominal 1x beta projection", test_projector_beta_uses_nominal_signed_notional_without_margin_multiplier),
        ("Nearest integer executable short target", test_projector_uses_nearest_integer_short_target),
        ("Proportional buying-power projection", test_projector_enforces_buying_power_cap_proportionally),
        ("Residual-aware integer short delta", test_projector_short_residual_produces_integer_order_delta),
        ("Buying-power scenario diagnostics", test_projector_logs_buffer_scenarios),
        ("Lexicographic weight-error priority", test_projector_uses_buying_power_only_as_secondary_objective),
        ("Projection constraint floor diagnostics", test_projector_reports_constraint_floor_and_min_trade_increment),
        ("Min-trade margin hard constraint", test_min_trade_carry_cannot_breach_initial_margin_cap),
        ("Min-trade beta hard constraint", test_min_trade_carry_cannot_breach_beta_limit),
        ("Final gross capacity target", test_projector_enforces_final_gross_capacity_target),
        ("Block missing short side", test_submission_guard_blocks_missing_short_side),
        ("Allow complete long/short portfolio", test_submission_guard_allows_complete_long_short_projection),
        ("Total RegT capacity reconstruction", test_total_regt_capacity_reconstruction),
        ("Stable equity RegT capacity", test_total_regt_capacity_uses_stable_equity_baseline),
        ("Portfolio-history request parameters", test_portfolio_history_uses_explicit_range_without_period),
        ("Intraday SIP fallback evidence", test_intraday_bar_capture_falls_back_for_primary_missing_symbols),
        ("Projection audit staged-entry selection", test_projection_audit_prefers_staged_entry_snapshot),
        ("Min-trade short carry safety", test_min_trade_short_carry_cannot_emit_residual_order),
        ("Weight-based min-trade threshold", test_min_trade_threshold_scales_with_weight_error_budget),
        ("Insufficient-qty error classification", test_insufficient_qty_error_is_not_buying_power_abort),
        ("Marketable-limit repeated requotes", test_marketable_limit_requotes_until_timeout),
        ("Bounded marketable-limit ladder", test_marketable_limit_ladder_is_bounded_and_unique),
        ("Live quote marketable-limit reference", test_marketable_limit_uses_live_quote_side),
        ("Concurrent symbol execution", test_order_batch_runs_symbols_concurrently),
        ("Empirical execution worker safety cap", test_order_batch_caps_workers_at_empirical_rate_limit_boundary),
        ("Ambiguous submit existing-fill recovery", test_duplicate_client_order_id_recovers_existing_fill),
        ("Ambiguous submit canceled-order requote", test_duplicate_client_order_id_requotes_existing_canceled_order),
        ("Ambiguous submit fresh-ID fallback", test_duplicate_client_order_id_without_order_uses_fresh_id),
        ("Client-order ID length uniqueness", test_client_order_id_preserves_uniqueness_under_length_limit),
        ("Fractional long-close precision fallback", test_fractional_long_close_retries_one_minimum_unit_lower),
        ("Per-symbol attempt budget", test_per_symbol_attempt_budget_bounds_requotes),
        ("Cross-round staged attempt budget", test_staged_release_attempt_budget_is_global_across_rounds),
        ("Unified parallel release batch", test_staged_release_sides_share_one_parallel_batch),
        ("Staged long-to-short zero boundary", test_staged_long_to_short_stops_at_zero_before_entry),
        ("Staged short-to-long zero boundary", test_staged_short_to_long_stops_at_zero_before_entry),
        ("Staged same-side reduction", test_staged_same_side_reduction_has_no_entry_leg),
        ("Entry rebuild residual release", test_entry_rebuild_executes_new_release_residual_before_entry),
        ("Staged filled release position lag", test_staged_filled_release_is_not_rebuilt_from_lagged_position),
        ("Controlled post-release quote abort", test_staged_quote_failure_after_release_is_controlled_abort),
        ("Staged entry residual repair", test_staged_entry_residual_repair_fills_weight_priority_gap),
        ("Attempt outcome classification", test_attempt_outcome_summary_separates_requotes_from_terminal_misses),
        ("Terminal unfilled retry status", test_terminal_unfilled_orders_force_an_execution_retry_status),
        ("Audit requote field propagation", test_audit_keeps_requote_fields),
        ("Audit submit-error payload parsing", test_audit_parses_submit_error_payload),
        ("Audit not-submitted reason", test_audit_marks_not_submitted_reason),
        ("Audit staged rebuild fill merge", test_audit_merges_staged_rebuild_fill_after_cancel),
        ("Execution-quality entry repair merge", test_execution_quality_merges_entry_repair_with_initial_entry),
        ("Audit attempt-outcome propagation", test_audit_exposes_attempt_outcomes_without_treating_requotes_as_misses),
        ("Total RegT position-capacity audit", test_position_capacity_uses_total_regt_capacity),
        ("Stable RegT margin-capacity audit", test_position_capacity_uses_stable_equity_and_separate_margin_metrics),
        ("Post-trade margin reconciliation", test_margin_reconciliation_matches_broker_initial_margin),
        ("Decision phase timing persistence", test_decision_phase_timings_persist_progress_and_failure),
        ("Persistent run event logging", test_run_events_are_persisted_immediately_with_sequence_and_elapsed_time),
        ("Target capability drift evidence", test_target_capability_drift_explains_new_nonshortable_target),
        ("Immutable first-run day-open evidence", test_first_run_json_evidence_is_not_overwritten),
        ("Post-submission quote audit", test_quote_audit_prefers_immediate_post_submission_snapshot),
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
