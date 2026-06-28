"""
Unit tests for trading-day / session-date resolution in daily_alpaca_scheduler.

These lock in the timezone contract:
1. Decision (12:00 BJ) and Execute (22:00 BJ) MUST resolve to the SAME session_date
   (so execute can find the decision-targets file decision wrote).
2. session_date must refer to the correct US-Eastern trading session in both
   summer (DST) and winter (standard time).
3. Early-morning manual runs (Beijing 00:00-11:59) must roll back to the US date,
   not be a day ahead.
"""
import sys
from datetime import datetime, time as dt_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_alpaca_scheduler import resolve_session_date, CN_TZ  # noqa: E402


def _bj(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ)


def test_decision_execute_consistency():
    """Decision and execute must give identical session_date (default 12:00/22:00)."""
    cutoff = dt_time(12, 0)
    for day, season in [("2026-06-27", "summer/DST"), ("2026-12-15", "winter/standard")]:
        dec = resolve_session_date(_bj(f"{day} 12:00"), cutoff)
        exe = resolve_session_date(_bj(f"{day} 22:00"), cutoff)
        assert dec == exe, f"{season}: decision={dec} != execute={exe} (file name mismatch!)"
        print(f"  [OK] {season}: decision==execute=={dec}")


def test_configurable_decision_time_consistency():
    """Invariant must hold for a configured SUB-NOON decision time (regression).

    If the operating-window boundary were hardcoded to noon, a 09:00 decision would
    roll back to the US date while the 22:00 execute used the Beijing date, breaking
    the decision==execute contract. Passing the configured decision time as the
    window start keeps them aligned.
    """
    for day, season in [("2026-06-27", "summer"), ("2026-12-15", "winter")]:
        for dec_str, exe_str in [("09:00", "22:00"), ("10:30", "21:00"), ("08:00", "20:00")]:
            cutoff = dt_time(*map(int, dec_str.split(":")))
            dec = resolve_session_date(_bj(f"{day} {dec_str}"), cutoff)
            exe = resolve_session_date(_bj(f"{day} {exe_str}"), cutoff)
            assert dec == exe, (
                f"{season} decision@{dec_str}/execute@{exe_str}: "
                f"decision={dec} != execute={exe}"
            )
            print(f"  [OK] {season} decision@{dec_str} execute@{exe_str} -> both {dec}")


def test_operating_window_uses_beijing_date():
    """During 12:00-23:59 Beijing, session_date equals the Beijing calendar date."""
    cases = [
        ("2026-06-27 12:00", "2026-06-27"),
        ("2026-06-27 22:00", "2026-06-27"),
        ("2026-12-15 12:00", "2026-12-15"),
        ("2026-12-15 22:00", "2026-12-15"),
        ("2026-12-15 23:30", "2026-12-15"),
    ]
    for bj_str, expected in cases:
        got = resolve_session_date(_bj(bj_str)).isoformat()
        assert got == expected, f"{bj_str}: expected {expected}, got {got}"
        print(f"  [OK] {bj_str} -> {got}")


def test_early_morning_rolls_back_to_us_date():
    """Beijing 00:00-11:59 must roll back to the US-Eastern date (prior session)."""
    cases = [
        # Beijing early morning -> US Eastern is the previous calendar day
        ("2026-12-15 03:00", "2026-12-14"),
        ("2026-06-27 03:00", "2026-06-26"),
        ("2026-06-27 11:00", "2026-06-26"),  # still pre-noon Beijing
    ]
    for bj_str, expected in cases:
        got = resolve_session_date(_bj(bj_str)).isoformat()
        assert got == expected, f"{bj_str}: expected {expected}, got {got}"
        print(f"  [OK] {bj_str} -> {got} (rolled back to US date)")


def test_noon_boundary():
    """Exactly 12:00 Beijing uses Beijing date; 11:59 rolls back."""
    at_noon = resolve_session_date(_bj("2026-12-15 12:00")).isoformat()
    before_noon = resolve_session_date(_bj("2026-12-15 11:59")).isoformat()
    assert at_noon == "2026-12-15", f"12:00 should be Beijing date, got {at_noon}"
    assert before_noon == "2026-12-14", f"11:59 should roll back, got {before_noon}"
    print(f"  [OK] 12:00 -> {at_noon}, 11:59 -> {before_noon}")


def main():
    print("=" * 60)
    print("Session Date Resolution Tests (timezone contract)")
    print("=" * 60)

    tests = [
        ("Decision/Execute consistency", test_decision_execute_consistency),
        ("Configurable decision-time consistency", test_configurable_decision_time_consistency),
        ("Operating window uses Beijing date", test_operating_window_uses_beijing_date),
        ("Early-morning rolls back to US date", test_early_morning_rolls_back_to_us_date),
        ("Noon boundary", test_noon_boundary),
    ]

    failed = 0
    for name, fn in tests:
        print(f"\n[TEST] {name}")
        try:
            fn()
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            failed += 1

    print("\n" + "=" * 60)
    if failed == 0:
        print(f"[PASS] All {len(tests)} tests passed")
        return 0
    print(f"[FAIL] {failed}/{len(tests)} tests failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
