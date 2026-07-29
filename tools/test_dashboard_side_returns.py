"""Regression checks for dashboard long/short daily return history."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_server import DataAggregator  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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

    complete = artifacts_root / "20260729_execute"
    _write_json(
        complete / "execution_summary.json",
        {
            "decision_date": "2026-07-29",
            "ok": True,
            "submitted": True,
            "account_equity_post_trade": 101000.0,
        },
    )
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
            "account_equity_post_trade": 101050.0,
        },
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


def test_side_returns_are_normalized_by_side_exposure() -> None:
    with TemporaryDirectory() as tmp:
        history = _build_fixture(Path(tmp)).get_history(limit=10)
        row = next(item for item in history if item.get("run_dir") == "20260729_execute")
        assert row["long_snapshot_intraday_pnl"] == 150.0, row
        assert row["short_snapshot_intraday_pnl"] == -50.0, row
        assert row["long_snapshot_intraday_return"] == 0.015, row
        assert row["short_snapshot_intraday_return"] == -0.01, row
        assert row["side_return_snapshot_source"] == "audit/07_risk_snapshot.json", row


def test_zero_exposure_does_not_emit_a_false_zero_return() -> None:
    with TemporaryDirectory() as tmp:
        history = _build_fixture(Path(tmp)).get_history(limit=10)
        row = next(item for item in history if item.get("run_dir") == "20260730_execute")
        assert row["long_snapshot_intraday_return"] is None, row
        assert row["short_snapshot_intraday_return"] == 0.01, row


def test_dashboard_markup_includes_side_return_chart() -> None:
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    for required in (
        "chartSideReturnTitle",
        "buildSideReturnSeries",
        "sideReturnChartPanel",
        "sideReturnChart",
        "long_snapshot_intraday_return",
        "short_snapshot_intraday_return",
    ):
        assert required in html, required


def main() -> int:
    tests = [
        ("Side returns normalized by exposure", test_side_returns_are_normalized_by_side_exposure),
        ("Zero exposure remains missing", test_zero_exposure_does_not_emit_a_false_zero_return),
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
