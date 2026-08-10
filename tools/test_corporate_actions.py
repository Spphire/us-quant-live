"""Regression tests for raw-price backtest corporate-action accounting."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from corporate_actions import apply_corporate_actions, index_corporate_actions  # noqa: E402


def test_split_updates_long_and_short_share_quantities() -> None:
    shares = {"AAA": 10.0, "BBB": -5.0}
    cash, diagnostics = apply_corporate_actions(
        shares=shares,
        cash=100.0,
        actions=[
            {
                "id": "split-1",
                "action_type": "stock_splits",
                "symbol": "AAA",
                "ex_date": "2026-01-02",
                "old_rate": "1.0",
                "new_rate": "2.0",
            },
            {
                "id": "split-2",
                "action_type": "stock_splits",
                "symbol": "BBB",
                "ex_date": "2026-01-02",
                "split_factor": "2:1",
            },
        ],
    )
    assert cash == 100.0
    assert shares == {"AAA": 20.0, "BBB": -10.0}
    assert diagnostics["split_event_count"] == 2
    assert diagnostics["status"] == "pass"


def test_dividend_cash_flow_has_opposite_long_and_short_signs() -> None:
    shares = {"LONG": 10.0, "SHORT": -5.0}
    cash, diagnostics = apply_corporate_actions(
        shares=shares,
        cash=100.0,
        actions=[
            {"id": "div-1", "action_type": "cash_dividends", "symbol": "LONG", "rate": "1.25"},
            {"id": "div-2", "action_type": "cash_dividends", "symbol": "SHORT", "rate": "2.00"},
        ],
    )
    assert cash == 100.0 + 12.5 - 10.0
    assert diagnostics["dividend_event_count"] == 2
    assert diagnostics["cash_delta"] == 2.5


def test_actions_preserve_open_to_next_open_economic_equity() -> None:
    split_shares = {"AAA": 10.0}
    split_cash, _ = apply_corporate_actions(
        shares=split_shares,
        cash=0.0,
        actions=[
            {
                "action_type": "stock_splits",
                "symbol": "AAA",
                "old_rate": 1,
                "new_rate": 2,
            }
        ],
    )
    assert split_cash + split_shares["AAA"] * 50.0 == 10.0 * 100.0

    dividend_shares = {"AAA": 10.0}
    dividend_cash, _ = apply_corporate_actions(
        shares=dividend_shares,
        cash=0.0,
        actions=[{"action_type": "cash_dividends", "symbol": "AAA", "rate": 1.0}],
    )
    assert dividend_cash + dividend_shares["AAA"] * 99.0 == 10.0 * 100.0

    short_shares = {"AAA": -10.0}
    short_cash, _ = apply_corporate_actions(
        shares=short_shares,
        cash=2000.0,
        actions=[{"action_type": "cash_dividends", "symbol": "AAA", "rate": 1.0}],
    )
    assert short_cash + short_shares["AAA"] * 99.0 == 2000.0 - 10.0 * 100.0


def test_corporate_action_index_deduplicates_broker_rows() -> None:
    indexed = index_corporate_actions(
        [
            {"id": "same", "action_type": "cash_dividends", "symbol": "aaa", "ex_date": "2026-01-02"},
            {"id": "same", "action_type": "cash_dividends", "symbol": "AAA", "ex_date": "2026-01-02"},
            {"action_type": "cash_dividends", "symbol": "BBB", "ex_date": "2026-01-03", "rate": "0.1"},
        ]
    )
    assert list(indexed) == ["2026-01-02", "2026-01-03"]
    assert len(indexed["2026-01-02"]) == 1
    assert indexed["2026-01-02"][0]["symbol"] == "AAA"

    symbol_change = index_corporate_actions(
        [{"id": "merge", "action_type": "mergers", "old_symbol": "OLD", "effective_date": "2026-01-04"}]
    )
    assert symbol_change["2026-01-04"][0]["symbol"] == "OLD"
    assert symbol_change["2026-01-04"][0]["effective_date_source"] == "effective_date"


def test_invalid_action_is_explicitly_reported() -> None:
    shares = {"AAA": 10.0}
    cash, diagnostics = apply_corporate_actions(
        shares=shares,
        cash=100.0,
        actions=[
            {
                "action_type": "stock_splits",
                "symbol": "AAA",
                "old_rate": "0",
                "new_rate": "2",
            }
        ],
    )
    assert cash == 100.0
    assert shares == {"AAA": 10.0}
    assert diagnostics["status"] == "error"
    assert diagnostics["errors"][0]["reason"] == "invalid_split_ratio"


def test_stock_dividend_is_not_misclassified_as_cash_dividend() -> None:
    shares = {"AAA": 10.0}
    cash, diagnostics = apply_corporate_actions(
        shares=shares,
        cash=100.0,
        actions=[
            {
                "action_type": "stock_dividends",
                "symbol": "AAA",
                "rate": "0.1",
            }
        ],
    )
    assert cash == 100.0
    assert shares == {"AAA": 10.0}
    assert diagnostics["dividend_event_count"] == 0
    assert diagnostics["unsupported_event_count"] == 1
    assert diagnostics["status"] == "error"
    assert diagnostics["errors"][0]["reason"] == "unsupported_corporate_action_for_held_position"


def test_unsupported_action_without_position_is_audited_but_not_fatal() -> None:
    cash, diagnostics = apply_corporate_actions(
        shares={},
        cash=100.0,
        actions=[{"action_type": "mergers", "symbol": "AAA"}],
    )
    assert cash == 100.0
    assert diagnostics["status"] == "attention"
    assert diagnostics["unsupported_event_count"] == 1
    assert diagnostics["errors"] == []


def main() -> int:
    tests = [
        test_split_updates_long_and_short_share_quantities,
        test_dividend_cash_flow_has_opposite_long_and_short_signs,
        test_actions_preserve_open_to_next_open_economic_equity,
        test_corporate_action_index_deduplicates_broker_rows,
        test_invalid_action_is_explicitly_reported,
        test_stock_dividend_is_not_misclassified_as_cash_dividend,
        test_unsupported_action_without_position_is_audited_but_not_fatal,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
