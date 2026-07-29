"""Regression checks for broker-weight turnover control in DecisionEngine."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from decision_engine import DecisionConfig, DecisionEngine, FACTOR_COLUMNS  # noqa: E402


def _alpha_frame() -> pd.DataFrame:
    rows = []
    for prefix, score_base in (("L", 2.0), ("S", -2.0)):
        for index in range(40):
            score = score_base - index * 0.01
            row = {
                "symbol": f"{prefix}{index:02d}",
                "composite_score": score,
                "beta": 1.0,
                "sic2_sector": str(index % 4),
                "sic4_industry": str(index % 8),
            }
            row.update({factor: score for factor in FACTOR_COLUMNS})
            rows.append(row)
    return pd.DataFrame(rows)


def test_fully_deployed_book_respects_turnover_budget() -> None:
    previous = {
        "long": {f"S{index:02d}": 1.0 / 30.0 for index in range(30)},
        "short": {f"L{index:02d}": 1.0 / 30.0 for index in range(30)},
    }
    engine = DecisionEngine(
        DecisionConfig(
            candidate_pool_per_side=40,
            min_nonzero_names=20,
            turnover_budget=0.15,
        )
    )

    result = engine.decide(
        alpha_frame=_alpha_frame(),
        previous_weights=previous,
        session_idx=7,
        session_date="2026-07-30",
    )

    assert result.status == "ok", result.diagnostics
    assert result.diagnostics["deploy_gap"] == 0.0, result.diagnostics
    assert result.diagnostics["effective_turnover_budget"] == 0.15, result.diagnostics
    assert result.diagnostics["target_turnover_raw"] <= 0.150001, result.diagnostics
    assert not result.targets.empty
    assert "signed_weight" in result.targets.columns
    assert set(result.targets.columns) == {
        "session_date",
        "session_idx",
        "side",
        "symbol",
        "side_weight",
        "signed_weight",
        "beta",
        "sic2_sector",
        "sic4_industry",
        "composite_score",
        *FACTOR_COLUMNS,
    }


def test_empty_book_deploy_gap_allows_initial_investment() -> None:
    engine = DecisionEngine(
        DecisionConfig(
            candidate_pool_per_side=40,
            min_nonzero_names=20,
            turnover_budget=0.15,
        )
    )
    result = engine.decide(
        alpha_frame=_alpha_frame(),
        previous_weights={"long": {}, "short": {}},
        session_idx=0,
        session_date="2026-07-30",
    )

    assert result.status == "ok", result.diagnostics
    assert result.diagnostics["deploy_gap"] == 2.0, result.diagnostics
    assert result.diagnostics["target_turnover_raw"] <= 2.000001, result.diagnostics
    assert abs(result.targets.loc[result.targets["side"] == "long", "side_weight"].sum() - 1.0) < 1e-8
    assert abs(result.targets.loc[result.targets["side"] == "short", "side_weight"].sum() - 1.0) < 1e-8


def test_unavailable_previous_position_counts_toward_turnover() -> None:
    previous = {
        "long": {"MISSING": 1.0 / 30.0} | {f"S{index:02d}": 1.0 / 30.0 for index in range(29)},
        "short": {f"L{index:02d}": 1.0 / 30.0 for index in range(30)},
    }
    engine = DecisionEngine(
        DecisionConfig(
            candidate_pool_per_side=40,
            min_nonzero_names=20,
            turnover_budget=0.05,
        )
    )
    result = engine.decide(
        alpha_frame=_alpha_frame(),
        previous_weights=previous,
        session_idx=7,
        session_date="2026-07-30",
    )

    assert result.status == "skip", result.diagnostics
    assert result.targets.empty
    assert result.diagnostics["unavailable_previous_symbols"]["long"] == ["MISSING"]


def main() -> int:
    tests = [
        ("Fully deployed turnover cap", test_fully_deployed_book_respects_turnover_budget),
        ("Cold-start deploy gap", test_empty_book_deploy_gap_allows_initial_investment),
        ("Unavailable previous weight", test_unavailable_previous_position_counts_toward_turnover),
    ]
    for name, test in tests:
        print(f"[TEST] {name}")
        test()
        print("  [OK]")
    print(f"[PASS] All {len(tests)} decision turnover tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
