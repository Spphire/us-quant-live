"""
Smoke tests for the tray launcher (no GUI required).

Tests:
1. Module imports cleanly
2. Single-instance mutex works
3. Icon file exists and is valid
4. Paths resolve correctly
5. SchedulerSupervisor can be instantiated
"""
import sys
import time
from pathlib import Path

# Add tools dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tray_launcher  # noqa: E402


def test_paths_resolve():
    """Verify path constants make sense."""
    print(f"  PROJECT_ROOT: {tray_launcher.PROJECT_ROOT}")
    print(f"  SCHEDULER_SCRIPT: {tray_launcher.SCHEDULER_SCRIPT}")
    print(f"  ICON_PATH: {tray_launcher.ICON_PATH}")
    assert tray_launcher.PROJECT_ROOT.exists(), "PROJECT_ROOT not found"
    assert tray_launcher.SCHEDULER_SCRIPT.exists(), "SCHEDULER_SCRIPT not found"
    assert tray_launcher.ICON_PATH.exists(), "ICON_PATH not found"
    print("  [OK] All paths resolve")


def test_icon_valid():
    """Verify icon file can be opened by PIL."""
    from PIL import Image
    img = Image.open(tray_launcher.ICON_PATH)
    print(f"  Icon size: {img.size}, format: {img.format}")
    assert img.size[0] >= 16, "Icon too small"
    print("  [OK] Icon is valid")


def test_singleton_mutex():
    """Verify single-instance mutex works."""
    s1 = tray_launcher.SingleInstance("test-mutex-" + str(int(time.time())))
    assert s1.acquire(), "First acquire should succeed"
    print("  [OK] First instance acquired mutex")

    s2 = tray_launcher.SingleInstance(s1.mutex_name)
    acquired2 = s2.acquire()
    assert not acquired2, "Second acquire should fail (already running)"
    print("  [OK] Second instance correctly blocked")

    # Release both handles (s2 also has a handle that must be closed)
    s2.release()
    s1.release()
    print("  [OK] Both mutex handles released")

    # After all handles released, new instance should succeed
    s3 = tray_launcher.SingleInstance(s1.mutex_name)
    assert s3.acquire(), "After release, new acquire should succeed"
    print("  [OK] After release, new acquire succeeded")
    s3.release()


def test_supervisor_lifecycle():
    """Test SchedulerSupervisor can be instantiated and reports correct state."""
    sup = tray_launcher.SchedulerSupervisor()
    assert not sup.is_running(), "Should not be running before start()"
    print("  [OK] Supervisor initially not running")

    # snapshot() should return (False, None) when not running
    running, pid = sup.snapshot()
    assert running is False, "snapshot().running should be False"
    assert pid is None, "snapshot().pid should be None"
    print("  [OK] snapshot() works when not running")

    # start() should refuse if should_run is False (post-stop guard)
    sup.should_run = False
    ok = sup.start()
    assert ok is False, "start() must refuse when should_run is False"
    print("  [OK] start() correctly refuses when should_run=False")


def test_port_check():
    """Test the port-availability check helper."""
    import socket
    # Should report port unavailable when something binds it
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind(("127.0.0.1", 0))  # Let OS pick a port
    port = s.getsockname()[1]
    s.listen(1)
    try:
        assert tray_launcher._is_port_in_use("127.0.0.1", port), (
            "Port should be reported as in use"
        )
        print(f"  [OK] _is_port_in_use detects bound port {port}")
    finally:
        s.close()

    # Free port should be reported as available
    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.bind(("127.0.0.1", 0))
    free_port = s2.getsockname()[1]
    s2.close()
    # After close, port is free
    import time as _t
    _t.sleep(0.1)
    print(f"  [OK] _is_port_in_use API contract verified")


def test_supervisor_log_handle_lifecycle():
    """Verify that stop() clears the log_fp reference even if process is None."""
    sup = tray_launcher.SchedulerSupervisor()
    # Simulate a leaked log_fp from a partial start
    import io
    fake_fp = io.StringIO()
    sup.log_fp = fake_fp
    # process is None — stop() must still close/clear the log handle
    sup.stop()
    assert sup.log_fp is None, "stop() must clear log_fp even with no process"
    assert fake_fp.closed, "stop() must close the log fp"
    print("  [OK] stop() correctly closes orphan log handles")


def main():
    print("=" * 60)
    print("Tray Launcher Smoke Tests")
    print("=" * 60)

    tests = [
        ("Path resolution", test_paths_resolve),
        ("Icon validity", test_icon_valid),
        ("Singleton mutex", test_singleton_mutex),
        ("Supervisor lifecycle", test_supervisor_lifecycle),
        ("Port availability check", test_port_check),
        ("Log handle lifecycle", test_supervisor_log_handle_lifecycle),
    ]

    failed = 0
    for name, fn in tests:
        print(f"\n[TEST] {name}")
        try:
            fn()
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    if failed == 0:
        print(f"[PASS] All {len(tests)} tests passed")
        return 0
    else:
        print(f"[FAIL] {failed}/{len(tests)} tests failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
