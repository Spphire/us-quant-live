"""Regression tests for raw/adjusted price-basis separation."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = ROOT / "src"
for path in (SRC_ROOT, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from alpha_core import AlphaCore, _extract_share_snapshot  # noqa: E402
from alpaca_executor import _build_fallback_price_map  # noqa: E402
from price_basis import (  # noqa: E402
    build_dual_price_panel,
    normalize_alpha_price_adjustment,
    summarize_alpha_price_basis_panel,
)


def _bar(symbol: str, session_date: str, close: float, *, volume: float = 1000.0) -> dict:
    return {
        "symbol": symbol,
        "t": f"{session_date}T00:00:00Z",
        "o": close,
        "h": close,
        "l": close,
        "c": close,
        "v": volume,
        "vw": close,
    }


def test_alpha_returns_use_adjusted_closes_but_keep_raw_absolute_prices() -> None:
    dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
    raw = []
    adjusted = []
    raw_aaa = [100.0, 101.0, 102.0, 103.0, 104.0, 50.0]
    adjusted_aaa = [50.0, 50.5, 51.0, 51.5, 52.0, 50.0]
    for session_date, raw_close, adjusted_close in zip(dates, raw_aaa, adjusted_aaa):
        raw.append(_bar("AAA", session_date, raw_close))
        adjusted.append(_bar("AAA", session_date, adjusted_close))
        raw.append(_bar("SPY", session_date, 400.0 + len(raw)))
        adjusted.append(_bar("SPY", session_date, 200.0 + len(adjusted)))

    panel, diagnostics = build_dual_price_panel(
        raw_bars=raw,
        adjusted_bars=adjusted,
        adjusted_price_adjustment="all",
    )
    assert diagnostics["status"] == "pass", diagnostics
    assert set(panel["price_basis_status"]) == {"matched"}

    core = AlphaCore(
        alpaca_client=SimpleNamespace(),
        sec_client=SimpleNamespace(),
        industry_map=pd.DataFrame(columns=["symbol", "sic2_sector", "sic4_industry"]),
        bars_window_calendar_days=20,
        benchmark_symbol="SPY",
        beta_lookback_sessions=5,
        beta_min_observations=2,
    )

    def collect(*, symbols, start, end, adjustment=None):
        requested = list(symbols)
        source = raw if adjustment == "raw" else adjusted
        return [row for row in source if row["symbol"] in requested]

    core._collect_bars_for_symbols = collect  # type: ignore[method-assign]
    features = core._build_price_features(  # type: ignore[attr-defined]
        ["AAA"],
        target_date=date(2026, 1, 7),
        data_cutoff_date=date(2026, 1, 6),
    )
    row = features.loc[features["symbol"].eq("AAA")].iloc[0]

    assert row["raw_close"] == 50.0
    assert row["adjusted_close"] == 50.0
    assert row["lagged_raw_close"] == 104.0
    assert abs(float(row["return_5d"])) < 1e-12
    assert row["alpha_return_price_source"] == "adjusted_close"
    assert row["absolute_price_source"] == "raw_close"


def test_price_basis_diagnostics_identify_partial_raw_adjusted_coverage() -> None:
    panel, diagnostics = build_dual_price_panel(
        raw_bars=[_bar("AAA", "2026-01-01", 100.0)],
        adjusted_bars=[_bar("AAA", "2026-01-02", 100.0)],
        adjusted_price_adjustment="all",
    )
    assert diagnostics["status"] == "partial", diagnostics
    assert diagnostics["raw_only_row_count"] == 1
    assert diagnostics["adjusted_only_row_count"] == 1
    evidence = summarize_alpha_price_basis_panel(panel, configured_adjustment="all")
    assert evidence["status"] == "partial", evidence


def test_alpha_price_basis_evidence_rejects_old_or_mismatched_panels() -> None:
    old_panel = pd.DataFrame([{"symbol": "AAA", "close": 100.0}])
    old_evidence = summarize_alpha_price_basis_panel(old_panel, configured_adjustment="all")
    assert old_evidence["status"] == "partial", old_evidence
    assert old_evidence["missing_required_columns"]

    panel, _ = build_dual_price_panel(
        raw_bars=[_bar("AAA", "2026-01-01", 100.0)],
        adjusted_bars=[_bar("AAA", "2026-01-01", 50.0)],
        adjusted_price_adjustment="all",
    )
    mismatch = summarize_alpha_price_basis_panel(panel, configured_adjustment="split")
    assert mismatch["status"] == "partial", mismatch
    assert mismatch["alpha_price_adjustment_matches_config"] is False

    for unsupported in ("raw", "dividend"):
        try:
            normalize_alpha_price_adjustment(unsupported)
        except ValueError as exc:
            assert "return-based alpha requires" in str(exc)
        else:
            raise AssertionError(f"unsupported Alpha return adjustment was accepted: {unsupported}")


def test_execution_fallback_uses_raw_only_and_rejects_adjusted_only_history() -> None:
    raw_panel = pd.DataFrame(
        [{"symbol": "AAA", "raw_close": 100.0, "adjusted_close": 50.0, "lagged_raw_close": 99.0}]
    )
    assert _build_fallback_price_map(alpha_panel=raw_panel, broker_positions=pd.DataFrame()) == {"AAA": 100.0}

    adjusted_only_panel = pd.DataFrame([{"symbol": "AAA", "adjusted_close": 50.0}])
    assert _build_fallback_price_map(
        alpha_panel=adjusted_only_panel,
        broker_positions=pd.DataFrame(),
    ) == {}


def test_market_cap_shares_are_forward_adjusted_to_raw_price_date() -> None:
    core = AlphaCore(
        alpaca_client=SimpleNamespace(),
        sec_client=SimpleNamespace(),
        industry_map=pd.DataFrame(columns=["symbol", "sic2_sector", "sic4_industry"]),
    )
    core._collect_corporate_actions_for_symbols = lambda **_kwargs: [  # type: ignore[method-assign]
        {
            "id": "nvda-split",
            "action_type": "forward_splits",
            "symbol": "NVDA",
            "ex_date": "2024-06-10",
            "old_rate": 1,
            "new_rate": 10,
        }
    ]
    frame = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "shares_outstanding": 2_460_000_000.0,
                "share_is_spot": True,
                "share_period_end": "2024-05-24",
                "share_filed_date": "2024-05-29",
                "market_cap_price_asof_session_date": "2024-06-10",
            }
        ]
    )
    aligned = core._align_shares_to_market_cap_price_basis(frame)  # type: ignore[attr-defined]
    row = aligned.iloc[0]
    assert row["shares_outstanding_reported"] == 2_460_000_000.0
    assert row["shares_split_adjustment_factor"] == 10.0
    assert row["shares_outstanding"] == 24_600_000_000.0
    assert row["shares_price_basis_status"] == "adjusted_for_splits"
    assert row["shares_split_adjustment_dates"] == "2024-06-10"

    reverse = core._align_shares_to_market_cap_price_basis(  # type: ignore[attr-defined]
        pd.DataFrame(
            [
                {
                    "symbol": "NVDA",
                    "shares_outstanding": 24_600_000_000.0,
                    "share_is_spot": True,
                    "share_period_end": "2024-06-11",
                    "share_filed_date": "2024-08-01",
                    "market_cap_price_asof_session_date": "2024-06-07",
                }
            ]
        )
    )
    reverse_row = reverse.iloc[0]
    assert reverse_row["shares_split_adjustment_factor"] == 0.1
    assert reverse_row["shares_outstanding"] == 2_460_000_000.0


def test_share_snapshot_rejects_future_measurement_dates() -> None:
    payload = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2027-07-17",
                                "val": 661_969_951,
                                "accn": "future",
                                "form": "10-Q",
                                "filed": "2026-07-23",
                            },
                            {
                                "end": "2026-05-01",
                                "val": 650_000_000,
                                "accn": "valid",
                                "form": "10-Q",
                                "filed": "2026-05-03",
                            },
                        ]
                    }
                }
            }
        }
    }
    snapshot = _extract_share_snapshot(payload, as_of_date="2026-08-06")
    assert snapshot["shares_outstanding"] == 650_000_000.0
    assert snapshot["share_accession"] == "valid"


def test_share_alignment_rejects_future_measurement_dates() -> None:
    core = AlphaCore(
        alpaca_client=SimpleNamespace(),
        sec_client=SimpleNamespace(),
        industry_map=pd.DataFrame(columns=["symbol", "sic2_sector", "sic4_industry"]),
    )
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAL",
                "session_date": "2026-08-07",
                "shares_outstanding": 661_969_951.0,
                "share_is_spot": True,
                "share_period_end": "2027-07-17",
                "share_filed_date": "2026-07-23",
                "market_cap_price_asof_session_date": "2026-08-06",
            }
        ]
    )
    try:
        core._align_shares_to_market_cap_price_basis(frame)  # type: ignore[attr-defined]
    except ValueError as exc:
        assert "share_period_end_after_decision_date" in str(exc)
    else:
        raise AssertionError("future share measurement date must fail closed")


def main() -> int:
    tests = [
        test_alpha_returns_use_adjusted_closes_but_keep_raw_absolute_prices,
        test_price_basis_diagnostics_identify_partial_raw_adjusted_coverage,
        test_alpha_price_basis_evidence_rejects_old_or_mismatched_panels,
        test_execution_fallback_uses_raw_only_and_rejects_adjusted_only_history,
        test_market_cap_shares_are_forward_adjusted_to_raw_price_date,
        test_share_snapshot_rejects_future_measurement_dates,
        test_share_alignment_rejects_future_measurement_dates,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
