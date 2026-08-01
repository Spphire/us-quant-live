"""Regression checks for dashboard long/short daily return history."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_server import DataAggregator  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_positions(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "side", "qty", "signed_qty", "current_price", "market_value"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build_fixture(root: Path) -> DataAggregator:
    artifacts_root = root / "artifacts" / "daily_alpaca_scheduler"
    _write_json(
        root / "artifacts" / "alpaca_executor" / "account_state.json",
        {
            "lifecycle_epoch": 2,
            "account_reset_effective_session": "2026-07-23",
            "initial_equity": 100000.0,
        },
    )

    previous = artifacts_root / "20260728_execute"
    _write_json(
        previous / "execution_summary.json",
        {
            "decision_date": "2026-07-28",
            "ok": True,
            "submitted": True,
            "account_equity_post_trade": 100000.0,
            "account_equity_post_trade_captured_at_utc": "2026-07-28T14:00:00+00:00",
        },
    )
    _write_json(previous / "broker_account_after.json", {"equity": "100000"})
    _write_positions(
        previous / "broker_positions_after.csv",
        [
            {
                "symbol": "LONG",
                "side": "long",
                "qty": 10.0,
                "signed_qty": 10.0,
                "current_price": 100.0,
                "market_value": 1000.0,
            },
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": 5.0,
                "signed_qty": -5.0,
                "current_price": 100.0,
                "market_value": -500.0,
            },
        ],
    )

    complete = artifacts_root / "20260729_execute"
    _write_json(
        complete / "execution_summary.json",
        {
            "decision_date": "2026-07-29",
            "ok": True,
            "submitted": True,
            "account_equity_preflight": 100090.0,
            "account_equity_preflight_captured_at_utc": "2026-07-29T13:59:00+00:00",
            "account_equity_post_trade": 100090.0,
            "account_equity_post_trade_captured_at_utc": "2026-07-29T14:00:00+00:00",
        },
    )
    _write_json(
        complete / "broker_account_before.json",
        {"equity": "100090"},
    )
    _write_json(
        complete / "broker_account_after.json",
        {"equity": "100090", "last_equity": "100000", "balance_asof": "2026-07-28"},
    )
    _write_positions(
        complete / "broker_positions_before.csv",
        [
            {
                "symbol": "LONG",
                "side": "long",
                "qty": 10.0,
                "signed_qty": 10.0,
                "current_price": 115.0,
                "market_value": 1150.0,
            },
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": 5.0,
                "signed_qty": -5.0,
                "current_price": 110.0,
                "market_value": -550.0,
            },
        ],
    )
    _write_positions(
        complete / "broker_positions_after.csv",
        [
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": 5.0,
                "signed_qty": -5.0,
                "current_price": 110.0,
                "market_value": -550.0,
            }
        ],
    )
    _write_json(
        complete / "audit" / "51_account_activity_attribution_summary.json",
        {
            "known_non_trade_equity_impact_net_amount": 99990.0,
            "non_strategy_cashflow_net_amount": 100000.0,
            "fee_interest_dividend_net_amount": -10.0,
        },
    )
    _write_json(complete / "broker_day_open_snapshot.json", {"positions": []})
    _write_json(
        complete / "audit" / "07_risk_snapshot.json",
        {
            "gross_long_market_value_after": 10000.0,
            "gross_short_market_value_abs_after": 5000.0,
            "snapshot_intraday_pnl_by_side": {
                "long": 150.0,
                "short": -50.0,
            },
        },
    )

    zero_exposure = artifacts_root / "20260730_execute"
    _write_json(
        zero_exposure / "execution_summary.json",
        {
            "decision_date": "2026-07-30",
            "ok": True,
            "submitted": True,
            "account_equity_preflight": 100115.0,
            "account_equity_preflight_captured_at_utc": "2026-07-30T13:59:00+00:00",
            "account_equity_post_trade": 100115.0,
            "account_equity_post_trade_captured_at_utc": "2026-07-30T14:00:00+00:00",
        },
    )
    _write_json(
        zero_exposure / "broker_account_before.json",
        {"equity": "100115"},
    )
    _write_json(
        zero_exposure / "broker_account_after.json",
        {"equity": "100115", "last_equity": "100090", "balance_asof": "2026-07-29"},
    )
    _write_positions(
        zero_exposure / "broker_positions_before.csv",
        [
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": 5.0,
                "signed_qty": -5.0,
                "current_price": 105.0,
                "market_value": -525.0,
            }
        ],
    )
    _write_positions(
        zero_exposure / "broker_positions_after.csv",
        [
            {
                "symbol": "SHORT",
                "side": "short",
                "qty": 5.0,
                "signed_qty": -5.0,
                "current_price": 105.0,
                "market_value": -525.0,
            }
        ],
    )
    _write_json(
        zero_exposure / "audit" / "07_risk_snapshot.json",
        {
            "gross_long_market_value_after": 0.0,
            "gross_short_market_value_abs_after": 2500.0,
            "snapshot_intraday_pnl_by_side": {
                "long": 0.0,
                "short": 25.0,
            },
        },
    )
    return DataAggregator(artifacts_root, root)


def test_cycle_return_decomposes_holding_sides_and_execution_window() -> None:
    with TemporaryDirectory() as tmp:
        history = _build_fixture(Path(tmp)).get_history(limit=10)
        row = next(item for item in history if item.get("run_dir") == "20260729_execute")
        assert row["long_snapshot_intraday_pnl"] == 150.0, row
        assert row["short_snapshot_intraday_pnl"] == -50.0, row
        assert row["long_snapshot_intraday_return"] == 0.015, row
        assert row["short_snapshot_intraday_return"] == -0.01, row
        assert row["side_return_snapshot_source"] == "audit/07_risk_snapshot.json", row
        assert row["daily_account_pnl"] == 90.0, row
        assert row["daily_account_return"] == 0.0009, row
        assert row["daily_account_cycle_start_equity"] == 100000.0, row
        assert row["daily_account_pre_trade_equity"] == 100090.0, row
        assert row["daily_account_cycle_end_equity"] == 100090.0, row
        assert row["daily_account_cycle_start_run_dir"] == "20260728_execute", row
        assert row["daily_long_equity_contribution"] == 0.0015, row
        assert row["daily_short_equity_contribution"] == -0.0005, row
        assert row["daily_execution_window_pnl"] == 0.0, row
        assert row["daily_execution_window_equity_contribution"] == 0.0, row
        assert row["daily_holding_residual_pnl"] == -10.0, row
        assert row["daily_holding_residual_equity_contribution"] == -0.0001, row
        assert row["daily_component_sum_pnl"] == row["daily_account_pnl"], row
        assert row["daily_component_identity_error_pnl"] == 0.0, row
        assert row["daily_known_non_trade_equity_contribution"] == -0.0001, row
        assert row["daily_unattributed_residual_pnl"] == -10.0, row
        assert row["daily_position_continuity_status"] == "pass", row
        assert row["daily_matched_position_count"] == 2, row
        component_pnl = sum(
            row[key]
            for key in (
                "daily_long_pnl",
                "daily_short_pnl",
                "daily_execution_window_pnl",
                "daily_holding_residual_pnl",
            )
        )
        component_return = sum(
            row[key]
            for key in (
                "daily_long_equity_contribution",
                "daily_short_equity_contribution",
                "daily_execution_window_equity_contribution",
                "daily_holding_residual_equity_contribution",
            )
        )
        assert component_pnl == row["daily_account_pnl"], row
        assert component_return == row["daily_account_return"], row
        assert row["daily_side_pnl_attribution_status"] == "reconciled", row
        assert row["daily_side_opening_snapshot_available"] is True, row


def test_zero_exposure_does_not_emit_a_false_zero_return() -> None:
    with TemporaryDirectory() as tmp:
        history = _build_fixture(Path(tmp)).get_history(limit=10)
        row = next(item for item in history if item.get("run_dir") == "20260730_execute")
        assert row["long_snapshot_intraday_return"] is None, row
        assert row["short_snapshot_intraday_return"] == 0.01, row
        assert row["daily_account_pnl"] == 25.0, row
        assert row["daily_account_cycle_start_equity"] == 100090.0, row
        assert row["daily_short_pnl"] == 25.0, row
        assert row["daily_execution_window_pnl"] == 0.0, row
        assert row["daily_unattributed_residual_pnl"] == 0.0, row
        assert row["daily_side_pnl_attribution_status"] == "reconciled", row


def test_position_continuity_gap_is_partial_and_remains_explicit() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        aggregator = _build_fixture(root)
        run_dir = root / "artifacts" / "daily_alpaca_scheduler" / "20260731_execute"
        _write_json(
            run_dir / "execution_summary.json",
            {
                "decision_date": "2026-07-31",
                "account_equity_preflight": 100065.0,
                "account_equity_preflight_captured_at_utc": "2026-07-31T13:59:00+00:00",
                "account_equity_post_trade": 100500.0,
                "account_equity_post_trade_captured_at_utc": "2026-07-31T14:00:00+00:00",
            },
        )
        _write_json(
            run_dir / "broker_account_before.json",
            {"equity": "100065"},
        )
        _write_json(
            run_dir / "broker_account_after.json",
            {"equity": "100500", "last_equity": "100000"},
        )
        _write_positions(
            run_dir / "broker_positions_before.csv",
            [
                {
                    "symbol": "SHORT",
                    "side": "short",
                    "qty": 4.0,
                    "signed_qty": -4.0,
                    "current_price": 115.0,
                    "market_value": -460.0,
                }
            ],
        )
        _write_json(
            run_dir / "audit" / "07_risk_snapshot.json",
            {
                "gross_long_market_value_after": 10000.0,
                "gross_short_market_value_abs_after": 10000.0,
                "snapshot_intraday_pnl_by_side": {"long": -100.0, "short": 50.0},
            },
        )

        history = aggregator.get_history(limit=10)
        row = next(item for item in history if item.get("run_dir") == "20260731_execute")
        assert row["daily_account_pnl"] == 385.0, row
        assert row["daily_execution_window_pnl"] == 435.0, row
        assert row["daily_unattributed_residual_pnl"] == -50.0, row
        assert row["daily_unattributed_equity_contribution"] == -50.0 / 100115.0, row
        assert row["daily_position_continuity_status"] == "partial", row
        assert row["daily_matched_position_count"] == 0, row
        assert row["daily_side_pnl_attribution_status"] == "partial", row


def test_dashboard_markup_includes_side_return_chart() -> None:
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    for required in (
        "chartSideReturnTitle",
        "buildSideReturnSeries",
        "sideReturnChartPanel",
        "sideReturnChart",
        "daily_long_equity_contribution",
        "daily_short_equity_contribution",
        "daily_execution_window_equity_contribution",
        "daily_holding_residual_equity_contribution",
        "seriesExecutionWindowContribution",
        "seriesAccountReturn",
        "seriesResidualContribution",
    ):
        assert required in html, required


def main() -> int:
    tests = [
        ("Aligned holding and execution decomposition", test_cycle_return_decomposes_holding_sides_and_execution_window),
        ("Zero exposure remains missing", test_zero_exposure_does_not_emit_a_false_zero_return),
        ("Position continuity gap remains explicit", test_position_continuity_gap_is_partial_and_remains_explicit),
        ("Dashboard side-return markup", test_dashboard_markup_includes_side_return_chart),
    ]
    for name, test in tests:
        print(f"[TEST] {name}")
        test()
        print("  [OK]")
    print(f"[PASS] All {len(tests)} side-return dashboard tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
