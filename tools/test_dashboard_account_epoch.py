"""Regression checks for dashboard account-reset lifecycle boundaries."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_server import DataAggregator  # noqa: E402


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_positions(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol",
        "side",
        "qty",
        "signed_qty",
        "current_price",
        "market_value",
        "avg_entry_price",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _build_fixture(project_root: Path) -> tuple[Path, DataAggregator]:
    artifacts_root = project_root / "artifacts" / "daily_alpaca_scheduler"
    _write_json(
        project_root / "artifacts" / "alpaca_executor" / "lot_ledger.json",
        {
            "meta": {
                "lifecycle_epoch": 2,
                "account_reset_at_utc": "2026-07-23T03:05:04Z",
                "account_reset_effective_session": "2026-07-23",
                "broker_account_id": "new-account-id",
                "broker_account_number": "NEW-PAPER",
                "initial_equity": 100000.0,
                "initial_cash": 100000.0,
            },
            "ledger": {"long": [], "short": []},
        },
    )
    _write_json(
        artifacts_root / "state.json",
        {
            "sessions": {
                "2026-07-22": {
                    "decision": {"status": "completed"},
                    "execute": {"status": "completed"},
                }
            }
        },
    )
    old_run = artifacts_root / "20260722_execute"
    _write_json(
        old_run / "execution_summary.json",
        {
            "decision_date": "2026-07-22",
            "ok": True,
            "submitted": True,
            "account_equity_post_trade": 87935.69,
        },
    )
    _write_positions(
        old_run / "broker_positions_after.csv",
        [
            {
                "symbol": "OLD",
                "side": "long",
                "qty": 1,
                "signed_qty": 1,
                "current_price": 100,
                "market_value": 100,
                "avg_entry_price": 90,
            }
        ],
    )
    return artifacts_root, DataAggregator(artifacts_root, project_root)


def test_reset_boundary_before_new_run() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _, aggregator = _build_fixture(root)
        overview = aggregator.get_overview()
        assert overview["equity"] == 100000.0, overview
        assert overview["positions_count"]["total"] == 0, overview
        assert overview["session_date"] == "2026-07-23", overview
        assert overview["account_epoch"]["capital_epoch"] == 2, overview
        assert overview["account_epoch"]["reset_pending"] is True, overview
        assert aggregator.get_positions() == []
        history = aggregator.get_history(limit=10)
        assert history[0]["run_type"] == "account_reset", history
        assert history[0]["capital_epoch"] == 2, history[0]
        old_rows = [row for row in history if row.get("run_dir") == "20260722_execute"]
        assert old_rows and old_rows[0]["capital_epoch"] == 1, history


def test_first_new_account_run_supersedes_reset_baseline() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifacts_root, aggregator = _build_fixture(root)
        new_run = artifacts_root / "20260723_execute"
        _write_json(
            new_run / "execution_summary.json",
            {
                "decision_date": "2026-07-23",
                "ok": True,
                "submitted": True,
                "account_equity_post_trade": 99950.0,
            },
        )
        _write_positions(
            new_run / "broker_positions_after.csv",
            [
                {
                    "symbol": "NEW",
                    "side": "long",
                    "qty": 2,
                    "signed_qty": 2,
                    "current_price": 50,
                    "market_value": 100,
                    "avg_entry_price": 49,
                }
            ],
        )
        overview = aggregator.get_overview()
        assert overview["equity"] == 99950.0, overview
        assert overview["positions_count"]["total"] == 1, overview
        assert overview["account_epoch"]["reset_pending"] is False, overview
        history = aggregator.get_history(limit=10)
        new_rows = [row for row in history if row.get("run_dir") == "20260723_execute"]
        assert new_rows and new_rows[0]["capital_epoch"] == 2, history


def main() -> int:
    tests = [
        ("Reset boundary before first run", test_reset_boundary_before_new_run),
        ("First new-account run", test_first_new_account_run_supersedes_reset_baseline),
    ]
    for name, test in tests:
        print(f"[TEST] {name}")
        test()
        print("  [OK]")
    print(f"[PASS] All {len(tests)} dashboard account-epoch tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
