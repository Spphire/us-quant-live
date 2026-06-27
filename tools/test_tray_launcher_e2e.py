"""
End-to-end test for tray launcher (verified working approach).

Test plan:
1. Start launcher subprocess
2. Wait for scheduler + dashboard to come up
3. Verify dashboard is reachable
4. Test single-instance protection
5. Kill the launcher tree
6. Verify cleanup (dashboard no longer reachable)
"""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT_ROOT / "tools" / "tray_launcher.py"
DASHBOARD_URL = "http://127.0.0.1:8766/api/overview"


def find_launcher_pids() -> list[int]:
    """Find PIDs of running tray_launcher.py processes via WMIC."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' and commandline like '%tray_launcher%'",
             "get", "ProcessId"],
            capture_output=True, text=True, timeout=10,
        )
        pids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception:
        return []


def kill_launcher_processes():
    """Kill only tray_launcher.py processes and their children, leaving test process alone."""
    pids = find_launcher_pids()
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    time.sleep(2)


def http_ok(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    print("=" * 60)
    print("End-to-End Launcher Test")
    print("=" * 60)

    # Clean slate
    kill_launcher_processes()
    venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"

    env = os.environ.copy()
    env["US_QUANT_LIVE_NO_NOTIFICATIONS"] = "1"

    results = {}

    # Test 1: Start launcher and verify it boots
    print("\n[1/4] Starting launcher and waiting 20s for scheduler boot...")
    launcher_log = PROJECT_ROOT / "artifacts" / "test_launcher.log"
    launcher_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(launcher_log, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [str(venv_python), str(LAUNCHER)],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
    )
    print(f"  Launcher PID: {proc.pid}")

    for _ in range(20):
        time.sleep(1)

    # Test 2: Dashboard reachable
    print(f"\n[2/4] Testing dashboard at {DASHBOARD_URL}...")
    dashboard_ok = http_ok(DASHBOARD_URL, timeout=5)
    results['dashboard_reachable'] = dashboard_ok
    print(f"  {'[OK]' if dashboard_ok else '[FAIL]'} Dashboard reachable: {dashboard_ok}")

    # Test 3: Single-instance protection
    print(f"\n[3/4] Testing single-instance protection (start second launcher)...")
    proc2 = subprocess.Popen(
        [str(venv_python), str(LAUNCHER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        rc = proc2.wait(timeout=15)
        single_ok = (rc == 1)  # Expected: second instance exits with code 1
        print(f"  {'[OK]' if single_ok else '[FAIL]'} Second instance exited with code {rc}")
    except subprocess.TimeoutExpired:
        proc2.kill()
        single_ok = False
        print(f"  [FAIL] Second instance did not exit within 15s")
    results['single_instance'] = single_ok

    # Test 4: Clean shutdown
    print(f"\n[4/4] Killing launcher tree and verifying cleanup...")
    # Find actual launcher PIDs (could be 1-2 depending on test history)
    pids = find_launcher_pids()
    print(f"  Found launcher PIDs: {pids}")
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
        )
    time.sleep(3)

    # Dashboard should now be unreachable
    cleanup_ok = not http_ok(DASHBOARD_URL, timeout=2)
    results['cleanup'] = cleanup_ok
    print(f"  {'[OK]' if cleanup_ok else '[FAIL]'} Dashboard correctly stopped: {cleanup_ok}")

    log_fp.close()

    # Final cleanup
    kill_launcher_processes()

    # Print log summary
    print(f"\n[Log] First 30 lines of launcher.log:")
    try:
        with open(launcher_log, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 30:
                    break
                print(f"  | {line.rstrip()}")
    except Exception:
        pass

    print("\n" + "=" * 60)
    all_passed = all(results.values())
    for name, ok in results.items():
        print(f"  {name:30s} {'PASS' if ok else 'FAIL'}")
    print("=" * 60)
    print(f"OVERALL: {'PASS' if all_passed else 'FAIL'}")

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
