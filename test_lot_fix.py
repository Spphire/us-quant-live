"""
Test script to verify the lot history persistence fix.

This test does NOT require Alpaca API access or market data.
It directly tests the lot_manager persistence logic to confirm:
1. Factor lots are created with correct min_hold periods
2. Ledger persists to disk correctly
3. session_idx advances by trading day (not by process invocation)
"""

import sys
from pathlib import Path
import pandas as pd

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from lot_manager import LotManager, DEFAULT_FACTOR_MIN_HOLDS

def test_lot_persistence():
    print("=" * 60)
    print("TEST: Lot Factor Persistence Fix Verification")
    print("=" * 60)

    test_ledger_path = Path("artifacts/test_lot_ledger.json")
    test_ledger_path.parent.mkdir(parents=True, exist_ok=True)

    # Clean start
    if test_ledger_path.exists():
        test_ledger_path.unlink()

    print("\n[1/5] Creating new LotManager...")
    lm = LotManager()

    print("[2/5] Adding positions with factor support (simulating DecisionEngine.decide())...")

    # Create a minimal base DataFrame with factor scores
    base_data = {
        'symbol': ['AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA'],
        'reversal_score': [0.5, 0.1, 1.0, 0.8, 0.2],
        'momentum_score': [0.2, 0.3, 0.0, 0.2, 0.1],
        'small_size_score': [0.3, 0.2, 0.0, 0.0, 1.0],
        'low_beta_score': [0.0, 0.6, 0.0, 0.0, 0.0],
        'cash_quality_score': [0.0, 0.4, 0.0, 0.0, 0.0],
    }
    base = pd.DataFrame(base_data)

    # Target weights (long and short)
    target_weights = {
        'long': {'AAPL': 0.05, 'MSFT': 0.03, 'GOOGL': 0.02},
        'short': {'TSLA': 0.04, 'NVDA': 0.03}
    }

    # Factor weights (same as production)
    factor_weights = {
        'reversal_score': 0.25,
        'momentum_score': 0.10,
        'small_size_score': 0.30,
        'low_beta_score': 0.20,
        'cash_quality_score': 0.15
    }

    lm.update_for_targets(
        target_weights=target_weights,
        base=base,
        session_idx=1,
        session_date='2026-06-27',
        factor_weights=factor_weights,
        factor_min_holds=DEFAULT_FACTOR_MIN_HOLDS
    )

    # Update meta to simulate what alpaca_executor does
    lm.meta.update({
        'last_session_idx': 1,
        'last_session_date': '2026-06-27'
    })

    print(f"   Long lots created: {len(lm.ledger['long'])}")
    print(f"   Short lots created: {len(lm.ledger['short'])}")

    print("\n[3/5] Persisting to disk (simulating decision phase write)...")
    lm.to_json(test_ledger_path)
    print(f"   [OK] Written to {test_ledger_path}")

    print("\n[4/5] Reloading from disk (simulating execute phase load)...")
    lm2 = LotManager.from_json(test_ledger_path)
    print(f"   [OK] Loaded {len(lm2.ledger['long'])} long + {len(lm2.ledger['short'])} short lots")

    print("\n[5/5] Verifying lot structure...")
    print("\n" + "=" * 60)
    print("LONG LOTS (all):")
    print("=" * 60)

    success = True
    factor_lot_count = 0
    broker_sync_count = 0

    for i, lot in enumerate(lm2.ledger['long']):
        symbol = lot['symbol']
        factor = lot['factor']
        min_hold = lot['min_hold']
        weight = lot['weight']
        birth_idx = lot['birth_idx']

        print(f"  {i+1}. {symbol:6s} factor={factor:20s} min_hold={min_hold:2d} weight={weight:.4f} birth={birth_idx}")

        # Count factor lots
        if factor == 'broker_sync' and min_hold == 0:
            broker_sync_count += 1
            print(f"     [WARN]  WARNING: This is a broker_sync lot")
        elif min_hold in [5, 10, 20]:
            factor_lot_count += 1
            print(f"     [OK] OK: Factor lot with proper min_hold")
        else:
            print(f"     [FAIL] FAIL: Invalid min_hold={min_hold}")
            success = False

    print("\n" + "=" * 60)
    print("SHORT LOTS (all):")
    print("=" * 60)

    for i, lot in enumerate(lm2.ledger['short']):
        symbol = lot['symbol']
        factor = lot['factor']
        min_hold = lot['min_hold']
        weight = lot['weight']
        birth_idx = lot['birth_idx']

        print(f"  {i+1}. {symbol:6s} factor={factor:20s} min_hold={min_hold:2d} weight={weight:.4f} birth={birth_idx}")

        if factor == 'broker_sync' and min_hold == 0:
            broker_sync_count += 1
            print(f"     [WARN]  WARNING: This is a broker_sync lot")
        elif min_hold in [5, 10, 20]:
            factor_lot_count += 1
            print(f"     [OK] OK: Factor lot with proper min_hold")
        else:
            print(f"     [FAIL] FAIL: Invalid min_hold={min_hold}")
            success = False

    print("\n" + "=" * 60)
    print("LOT COMPOSITION SUMMARY:")
    print("=" * 60)
    print(f"  Factor lots (reversal/momentum/size/beta/cash): {factor_lot_count}")
    print(f"  Broker sync lots (min_hold=0): {broker_sync_count}")

    if factor_lot_count > 0 and broker_sync_count == 0:
        print("  [OK] OK: All lots are factor lots (FIX IS WORKING)")
    elif factor_lot_count == 0:
        print("  [FAIL] FAIL: No factor lots found (bug not fixed)")
        success = False
    else:
        print("  [WARN]  MIXED: Both factor and broker_sync lots present")

    print("\n" + "=" * 60)
    print("SESSION_IDX HANDLING TEST:")
    print("=" * 60)

    # Test that same trading day doesn't increment session_idx
    print("\n  Simulating second run on SAME trading day (should reuse session_idx)...")

    # Manually import the fixed _resolve_session_idx logic
    sys.path.insert(0, str(Path(__file__).parent / "src"))
    from alpaca_executor import _resolve_session_idx

    idx1 = _resolve_session_idx(lm2, None, session_date='2026-06-27')
    print(f"    First call with session_date='2026-06-27': idx={idx1}")

    idx2 = _resolve_session_idx(lm2, None, session_date='2026-06-27')
    print(f"    Second call with session_date='2026-06-27': idx={idx2}")

    if idx1 == idx2:
        print(f"    [OK] OK: Same trading day reuses idx={idx1}")
    else:
        print(f"    [FAIL] FAIL: idx incremented from {idx1} to {idx2} on same day")
        success = False

    # Test that new trading day increments
    idx3 = _resolve_session_idx(lm2, None, session_date='2026-06-30')
    print(f"    Call with session_date='2026-06-30' (new day): idx={idx3}")

    if idx3 == idx1 + 1:
        print(f"    [OK] OK: New trading day increments idx from {idx1} to {idx3}")
    else:
        print(f"    [FAIL] FAIL: Expected idx={idx1+1}, got {idx3}")
        success = False

    print("\n" + "=" * 60)
    print("FINAL RESULT:")
    print("=" * 60)

    if success and factor_lot_count > 0:
        print("[PASS] ALL TESTS PASSED - Lot history fix is working correctly!")
        print("\nWhat this means:")
        print("  - Factor lots (reversal/momentum/size/beta/cash) are created")
        print("  - Each factor lot has correct min_hold period (5/10/20 sessions)")
        print("  - Ledger persists to disk and reloads correctly")
        print("  - session_idx counts trading days (not process invocations)")
        print("\nIn production, this will:")
        print("  - Reduce turnover (locked lots prevent premature selling)")
        print("  - Match backtest behavior (Phase7K lot mechanism)")
        print("  - Preserve factor attribution across decision/execute phases")
        return 0
    else:
        print("[FAIL] SOME TESTS FAILED - Please review output above")
        return 1

if __name__ == '__main__':
    sys.exit(test_lot_persistence())
