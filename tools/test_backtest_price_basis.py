"""Focused backtest tests that do not require the plotting dependency."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = ROOT / "src"
for path in (SRC_ROOT, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import matplotlib.pyplot  # noqa: F401
except ModuleNotFoundError:
    matplotlib_module = ModuleType("matplotlib")
    pyplot_module = ModuleType("matplotlib.pyplot")
    matplotlib_module.pyplot = pyplot_module  # type: ignore[attr-defined]
    sys.modules["matplotlib"] = matplotlib_module
    sys.modules["matplotlib.pyplot"] = pyplot_module

from backtest.phase7k_backtest import (  # noqa: E402
    _bind_cached_bar_collector,
    _bind_cached_corporate_action_collector,
    _collect_corporate_actions_parallel,
    _summarize_backtest_bar_basis_coverage,
)


def _bar(close: float) -> dict:
    return {"symbol": "AAA", "t": "2026-01-01T00:00:00Z", "c": close}


def test_cached_collector_keeps_raw_and_adjusted_indexes_separate() -> None:
    indexes = {
        "raw": {"AAA": (["2026-01-01"], [_bar(100.0)])},
        "adjusted": {"AAA": (["2026-01-01"], [_bar(50.0)])},
    }
    collector = _bind_cached_bar_collector(SimpleNamespace(_price_adjustment="all"), indexes)

    raw = collector(symbols=["AAA"], start="2026-01-01", end="2026-01-01", adjustment="raw")
    adjusted = collector(symbols=["AAA"], start="2026-01-01", end="2026-01-01", adjustment="all")
    assert raw[0]["c"] == 100.0
    assert adjusted[0]["c"] == 50.0

    try:
        collector(symbols=["AAA"], start="2026-01-01", end="2026-01-01", adjustment="split")
    except ValueError as exc:
        assert "do not contain requested adjustment" in str(exc)
    else:
        raise AssertionError("cached collector silently reused a different adjusted basis")


class _CorporateActionClient:
    def __init__(self) -> None:
        self.chunks: list[list[str]] = []

    def get_corporate_actions(self, *, symbols, start, end, limit):
        self.chunks.append(list(symbols))
        return [
            {
                "id": f"action-{symbols[0]}",
                "action_type": "cash_dividends",
                "symbol": symbols[0],
                "ex_date": start,
                "rate": 0.1,
            }
        ]


def test_corporate_action_prefetch_caps_requests_at_100_symbols() -> None:
    client = _CorporateActionClient()
    rows = _collect_corporate_actions_parallel(
        client=client,  # type: ignore[arg-type]
        symbols=[f"S{index:03d}" for index in range(205)],
        start="2026-01-01",
        end="2026-01-31",
        chunk_size=500,
        workers=3,
    )
    assert sorted(len(chunk) for chunk in client.chunks) == [5, 100, 100]
    assert len(rows) == 3


def test_cached_corporate_action_collector_enforces_coverage_and_filters_symbols() -> None:
    collector = _bind_cached_corporate_action_collector(
        SimpleNamespace(),
        [
            {
                "id": "a1",
                "action_type": "forward_splits",
                "symbol": "AAA",
                "ex_date": "2026-01-05",
                "old_rate": 1,
                "new_rate": 2,
            },
            {
                "id": "b1",
                "action_type": "cash_dividends",
                "symbol": "BBB",
                "ex_date": "2026-01-05",
                "rate": 1,
            },
        ],
        coverage_start="2026-01-01",
        coverage_end="2026-01-31",
    )
    rows = collector(symbols=["AAA"], start="2026-01-01", end="2026-01-10")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAA"

    try:
        collector(symbols=["AAA"], start="2025-12-01", end="2026-01-10")
    except ValueError as exc:
        assert "coverage is insufficient" in str(exc)
    else:
        raise AssertionError("cached action collector accepted an uncovered range")


def test_backtest_price_basis_coverage_detects_missing_rows() -> None:
    raw = [
        {"symbol": "AAA", "t": "2026-01-01T00:00:00Z"},
        {"symbol": "BBB", "t": "2026-01-01T00:00:00Z"},
    ]
    adjusted = [{"symbol": "AAA", "t": "2026-01-01T00:00:00Z"}]
    partial = _summarize_backtest_bar_basis_coverage(
        raw_bars=raw,
        adjusted_bars=adjusted,
        alpha_price_adjustment="all",
    )
    assert partial["status"] == "partial"
    assert partial["raw_only_row_count"] == 1
    assert partial["raw_only_sample"] == [{"symbol": "BBB", "session_date": "2026-01-01"}]

    passed = _summarize_backtest_bar_basis_coverage(
        raw_bars=raw,
        adjusted_bars=list(raw),
        alpha_price_adjustment="all",
    )
    assert passed["status"] == "pass"


def main() -> int:
    tests = [
        test_cached_collector_keeps_raw_and_adjusted_indexes_separate,
        test_corporate_action_prefetch_caps_requests_at_100_symbols,
        test_cached_corporate_action_collector_enforces_coverage_and_filters_symbols,
        test_backtest_price_basis_coverage_detects_missing_rows,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
