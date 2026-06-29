"""
Regression test for optimizer feasibility when a locked lot weight exceeds the
single-name cap.

Background: when a position carried from a prior session (or synced from broker
positions that drifted up on price) has a side weight ABOVE max_single_name_side_weight,
the locked weight becomes an optimizer lower bound that previously exceeded the
uniform upper bound (the cap) -> LP infeasible -> every decision fell back to
carry/repair instead of doing real optimization.

Fix: per-name upper bound = max(cap, locked_weight), so an over-cap locked lot is
held in place while the cap still constrains all other names. The LP stays feasible.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from decision_engine import DecisionEngine, DecisionConfig  # noqa: E402


def _make_side_frame(symbols, betas, scores, sectors):
    return pd.DataFrame(
        {
            "symbol": symbols,
            "beta": betas,
            "composite_score": scores,
            "sic2_sector": sectors,
        }
    )


def test_over_cap_locked_weight_is_feasible():
    """Optimizer must succeed (not raise) when a locked weight exceeds the cap."""
    engine = DecisionEngine(DecisionConfig())
    cap = 1.0 / 30.0  # 0.0333

    # 40 candidate names per side so there's plenty of capacity (40*cap >> 1.0)
    n = 40
    long_syms = [f"L{i}" for i in range(n)]
    short_syms = [f"S{i}" for i in range(n)]
    rng_beta_long = [1.0 + 0.01 * i for i in range(n)]
    rng_beta_short = [1.0 + 0.01 * i for i in range(n)]
    scores_long = [float(n - i) for i in range(n)]   # descending desirability
    scores_short = [float(i) for i in range(n)]
    sectors = [f"SIC{i % 5}" for i in range(n)]

    longs = _make_side_frame(long_syms, rng_beta_long, scores_long, sectors)
    shorts = _make_side_frame(short_syms, rng_beta_short, scores_short, sectors)

    # One locked long and one locked short ABOVE the cap (0.047 > 0.0333)
    locked_long = {"L0": 0.047}
    locked_short = {"S0": 0.047}

    long_w, short_w = engine._optimize_joint_weights_locked(
        longs=longs,
        shorts=shorts,
        max_weight=cap,
        score_weight=0.01,
        sector_penalty=25.0,
        turnover_penalty=0.005,
        previous_long_weights={},
        previous_short_weights={},
        locked_long_weights=locked_long,
        locked_short_weights=locked_short,
        turnover_budget=10.0,   # generous so turnover isn't the binding constraint
        deploy_gap=0.0,
    )

    # Side sums must be ~1.0
    assert abs(float(long_w.sum()) - 1.0) < 1e-6, f"long sum={long_w.sum()}"
    assert abs(float(short_w.sum()) - 1.0) < 1e-6, f"short sum={short_w.sum()}"
    print(f"  [OK] long sum={long_w.sum():.6f}, short sum={short_w.sum():.6f}")

    # The over-cap locked name must be held at >= its locked weight
    assert long_w[0] >= 0.047 - 1e-6, f"L0 weight={long_w[0]} should be >= 0.047"
    assert short_w[0] >= 0.047 - 1e-6, f"S0 weight={short_w[0]} should be >= 0.047"
    print(f"  [OK] locked over-cap held: L0={long_w[0]:.4f}, S0={short_w[0]:.4f}")

    # All OTHER names must respect the cap
    assert np.all(long_w[1:] <= cap + 1e-6), f"non-locked long exceeds cap: max={long_w[1:].max()}"
    assert np.all(short_w[1:] <= cap + 1e-6), f"non-locked short exceeds cap: max={short_w[1:].max()}"
    print(f"  [OK] non-locked names within cap {cap:.4f}")


def test_normal_locked_within_cap_still_works():
    """Sanity: when locked weights are within the cap, optimizer still works."""
    engine = DecisionEngine(DecisionConfig())
    cap = 1.0 / 30.0
    n = 40
    longs = _make_side_frame(
        [f"L{i}" for i in range(n)], [1.0] * n, [float(n - i) for i in range(n)],
        [f"SIC{i % 5}" for i in range(n)],
    )
    shorts = _make_side_frame(
        [f"S{i}" for i in range(n)], [1.0] * n, [float(i) for i in range(n)],
        [f"SIC{i % 5}" for i in range(n)],
    )
    long_w, short_w = engine._optimize_joint_weights_locked(
        longs=longs, shorts=shorts, max_weight=cap,
        score_weight=0.01, sector_penalty=25.0, turnover_penalty=0.005,
        previous_long_weights={}, previous_short_weights={},
        locked_long_weights={"L0": 0.02}, locked_short_weights={"S0": 0.02},
        turnover_budget=10.0, deploy_gap=0.0,
    )
    assert abs(float(long_w.sum()) - 1.0) < 1e-6
    assert abs(float(short_w.sum()) - 1.0) < 1e-6
    assert np.all(long_w <= cap + 1e-6) and np.all(short_w <= cap + 1e-6)
    assert long_w[0] >= 0.02 - 1e-6, "locked L0 should be honored"
    print(f"  [OK] within-cap locked honored, all names <= cap")


def main():
    print("=" * 60)
    print("Optimizer Over-Cap Locked Weight Feasibility Tests")
    print("=" * 60)
    tests = [
        ("Over-cap locked weight is feasible", test_over_cap_locked_weight_is_feasible),
        ("Normal within-cap locked still works", test_normal_locked_within_cap_still_works),
    ]
    failed = 0
    for name, fn in tests:
        print(f"\n[TEST] {name}")
        try:
            fn()
        except Exception as exc:
            import traceback
            print(f"  [FAIL] {exc}")
            traceback.print_exc()
            failed += 1
    print("\n" + "=" * 60)
    if failed == 0:
        print(f"[PASS] All {len(tests)} tests passed")
        return 0
    print(f"[FAIL] {failed}/{len(tests)} failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
