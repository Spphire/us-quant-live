"""Regression tests for retry-aware execution-cycle attribution."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = ROOT / "src"
for path in (ROOT, SRC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from daily_audit_report import (  # noqa: E402
    _build_cross_day_continuity,
    _build_execution_cycle_attempt_metadata,
)
from daily_pnl_attribution import (  # noqa: E402
    build_daily_side_pnl_attribution,
    select_execution_cycle_boundary,
)


def _position(symbol: str, qty: float, price: float, side: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "qty": qty,
        "signed_qty": qty if side == "long" else -abs(qty),
        "side": side,
        "current_price": price,
        "market_value": (qty if side == "long" else -abs(qty)) * price,
    }


def _write_positions(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "qty", "signed_qty", "side", "current_price", "market_value"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_retry_boundary_uses_first_submit_snapshot() -> None:
    fallback_positions = [_position("LONG", 5.0, 120.0, "long")]
    snapshot_positions = [
        _position("LONG", 10.0, 110.0, "long"),
        _position("SHORT", 5.0, 90.0, "short"),
    ]
    boundary = select_execution_cycle_boundary(
        fallback_account_capture={"equity": "1100"},
        fallback_positions=fallback_positions,
        opening_snapshot={
            "capture_semantics": "first_submit_enabled_executor_preflight_for_session",
            "captured_at_utc": "2026-08-18T14:00:00+00:00",
            "account": {"equity": "1150"},
            "positions": snapshot_positions,
        },
        attempt_count=2,
    )

    assert boundary["available"] is True
    assert boundary["retry_occurred"] is True
    assert boundary["source"] == "broker_day_open_snapshot.json"
    assert boundary["positions"] == snapshot_positions

    attribution = build_daily_side_pnl_attribution(
        account_after_capture={"equity": "1140"},
        account_before_capture=boundary["account_capture"],
        account_start_capture={"payload": {"equity": "1000"}},
        previous_positions_after=[
            _position("LONG", 10.0, 100.0, "long"),
            _position("SHORT", 5.0, 100.0, "short"),
        ],
        current_positions_before=boundary["positions"],
        risk_snapshot={},
        account_activity_summary={"fee_interest_dividend_net_amount": 0.0},
        opening_snapshot_available=True,
        execution_cycle_boundary=boundary,
    )

    assert attribution["status"] == "reconciled"
    assert attribution["long_pnl"] == 100.0
    assert attribution["short_pnl"] == 50.0
    assert attribution["holding_residual_pnl"] == 0.0
    assert attribution["execution_window_pnl"] == -10.0
    assert attribution["execution_attempt_count"] == 2
    assert attribution["retry_occurred"] is True
    assert attribution["identity_error_pnl"] == 0.0


def test_retry_without_boundary_is_explicitly_partial() -> None:
    boundary = select_execution_cycle_boundary(
        fallback_account_capture={"equity": "1100"},
        fallback_positions=[_position("LONG", 5.0, 120.0, "long")],
        opening_snapshot={},
        attempt_count=2,
    )
    attribution = build_daily_side_pnl_attribution(
        account_after_capture={"equity": "1140"},
        account_before_capture=boundary["account_capture"],
        account_start_capture={"payload": {"equity": "1000"}},
        previous_positions_after=[_position("LONG", 10.0, 100.0, "long")],
        current_positions_before=boundary["positions"],
        risk_snapshot={},
        account_activity_summary={"fee_interest_dividend_net_amount": 0.0},
        opening_snapshot_available=False,
        execution_cycle_boundary=boundary,
    )

    assert attribution["status"] == "partial"
    assert attribution["execution_cycle_boundary_status"] == "missing"
    assert attribution["retry_occurred"] is True
    assert attribution["opening_snapshot_available"] is False


def test_historical_retry_metadata_can_be_recovered_from_log() -> None:
    with TemporaryDirectory() as raw_directory:
        tmp_path = Path(raw_directory)
        run_dir = tmp_path / "20260818_execute"
        logs_dir = tmp_path / "logs"
        run_dir.mkdir()
        logs_dir.mkdir()
        (run_dir / "scheduler_task_result.json").write_text(
            json.dumps({"attempt": 2, "paths": {"stdout_log": str(logs_dir / "execute.log")}}),
            encoding="utf-8",
        )
        (logs_dir / "execute.log").write_text(
            "\n".join(
                [
                    "=== execute 2026-08-18 start 2026-08-18T22:00:10+08:00 ===",
                    "[Executor] warning: submission completed with 1 error(s): ARW: spread_bps=154.373_exceeds_150.000",
                    "[DecisionTiming] status=completed_with_errors elapsed=196.625s",
                    "=== execute 2026-08-18 start 2026-08-18T22:08:36+08:00 ===",
                    "[DecisionTiming] status=completed elapsed=202.743s",
                ]
            ),
            encoding="utf-8",
        )

        metadata = _build_execution_cycle_attempt_metadata(run_dir, {})

        assert metadata["attempt_count"] == 2
        assert metadata["retry_occurred"] is True
        assert metadata["retry_after_partial_execution"] is True
        assert metadata["first_attempt_executor_status"] == "completed_with_errors"
        assert metadata["first_attempt_returncode"] == 1
        assert "ARW" in metadata["first_attempt_error"]
        assert metadata["retry_reason"] == "executor_nonzero_returncode_after_submit_error"


def test_cross_day_continuity_excludes_first_attempt_trades() -> None:
    with TemporaryDirectory() as raw_directory:
        root = Path(raw_directory)
        previous = root / "20260817_execute"
        current = root / "20260818_execute"
        previous.mkdir()
        current.mkdir()
        previous_positions = [
            _position("LONG", 10.0, 100.0, "long"),
            _position("SHORT", 5.0, 100.0, "short"),
        ]
        _write_positions(previous / "broker_positions_after.csv", previous_positions)
        _write_positions(
            current / "broker_positions_before.csv",
            [_position("LONG", 5.0, 120.0, "long")],
        )
        (current / "broker_account_before.json").write_text(
            json.dumps({"equity": "1200"}), encoding="utf-8"
        )
        (current / "scheduler_task_result.json").write_text(
            json.dumps({"attempt": 2}), encoding="utf-8"
        )
        (current / "broker_day_open_snapshot.json").write_text(
            json.dumps(
                {
                    "capture_semantics": "first_submit_enabled_executor_preflight_for_session",
                    "captured_at_utc": "2026-08-18T14:00:00+00:00",
                    "account": {"equity": "1050"},
                    "positions": previous_positions,
                }
            ),
            encoding="utf-8",
        )

        rows, summary = _build_cross_day_continuity(
            root,
            [
                {
                    "run_dir": previous.as_posix(),
                    "session_date": "2026-08-17",
                    "equity_after": 1000.0,
                },
                {
                    "run_dir": current.as_posix(),
                    "session_date": "2026-08-18",
                    "equity_before": 1200.0,
                },
            ],
        )

        assert summary["issue_pair_count"] == 0
        assert rows[0]["status"] == "pass"
        assert rows[0]["symbols_with_qty_gap"] == 0
        assert rows[0]["next_before_equity"] == 1050.0
        assert rows[0]["next_boundary_source"] == "broker_day_open_snapshot.json"
        assert rows[0]["next_execution_attempt_count"] == 2


def main() -> int:
    tests = (
        test_retry_boundary_uses_first_submit_snapshot,
        test_retry_without_boundary_is_explicitly_partial,
        test_historical_retry_metadata_can_be_recovered_from_log,
        test_cross_day_continuity_excludes_first_attempt_trades,
    )
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
