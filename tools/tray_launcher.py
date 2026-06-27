"""
US Quant Live - System Tray Launcher

A Windows system tray application that:
1. Launches and supervises the daily_alpaca_scheduler.py daemon
2. Auto-starts the dashboard HTTP server (via scheduler integration)
3. Provides a tray icon with K-line chart style
4. Right-click menu: Open Dashboard, View Logs, Restart, Exit
5. Single-instance protection via Windows named mutex
6. Auto-restarts scheduler if it dies unexpectedly

Usage:
    python tools/tray_launcher.py
    # OR compile to .exe with PyInstaller (see build_exe.py)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image


# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
# When frozen by PyInstaller, sys.executable is the .exe (not python).
# The project structure must live alongside the .exe (tools/, src/, venv/, etc.)
if getattr(sys, 'frozen', False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = SCRIPT_DIR.parent
ICON_PATH = SCRIPT_DIR / "tray_icon.ico"
SCHEDULER_SCRIPT = PROJECT_ROOT / "tools" / "daily_alpaca_scheduler.py"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts" / "daily_alpaca_scheduler"
DASHBOARD_LOG = ARTIFACTS_ROOT / "daemon" / "scheduler.out.log"

DASHBOARD_URL = "http://127.0.0.1:8766"
DASHBOARD_PORT = 8766
MUTEX_NAME = "us-quant-live-tray-launcher-singleton"
# Threshold: scheduler dying before this uptime is considered a hard fail (don't auto-restart)
EARLY_DEATH_THRESHOLD_SECONDS = 30.0


# === Single-instance protection (Windows named mutex) ===
class SingleInstance:
    """Windows named mutex to prevent multiple launcher instances."""

    def __init__(self, mutex_name: str):
        self.mutex_name = mutex_name
        self.mutex = None
        self.is_already_running = False

    def acquire(self) -> bool:
        """Try to acquire the mutex. Returns True if this is the first instance."""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            ERROR_ALREADY_EXISTS = 183

            # CreateMutex returns a handle. If mutex already exists, GetLastError == ERROR_ALREADY_EXISTS
            self.mutex = kernel32.CreateMutexW(None, False, self.mutex_name)
            last_error = kernel32.GetLastError()

            if last_error == ERROR_ALREADY_EXISTS:
                self.is_already_running = True
                return False
            return True
        except Exception as exc:
            print(f"[Launcher] WARNING: mutex check failed: {exc}", flush=True)
            return True  # Fail open: allow startup if check fails

    def release(self) -> None:
        """Release the mutex. Always closes the handle. ReleaseMutex only if we own it."""
        if self.mutex is not None:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # Only call ReleaseMutex if we actually own the mutex (first instance)
                if not self.is_already_running:
                    kernel32.ReleaseMutex(self.mutex)
                kernel32.CloseHandle(self.mutex)
            except Exception:
                pass
            finally:
                self.mutex = None


# === Notification helper ===
# Set to True to disable blocking MessageBox dialogs (useful for tests / headless mode)
NOTIFICATIONS_DISABLED = os.environ.get("US_QUANT_LIVE_NO_NOTIFICATIONS") == "1"


def show_notification(title: str, message: str) -> None:
    """Show a Windows notification balloon (skipped if disabled via env var)."""
    if NOTIFICATIONS_DISABLED:
        print(f"[Notification] {title}: {message}", flush=True)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_ICONINFORMATION
    except Exception as exc:
        print(f"[Launcher] notification failed: {exc}", flush=True)


def show_notification_async(title: str, message: str) -> None:
    """Show a notification in a daemon thread so the caller never blocks.

    Use this from background threads (monitor_loop, etc.) where blocking on
    a MessageBox would prevent restart logic from running.
    """
    threading.Thread(
        target=show_notification, args=(title, message), daemon=True
    ).start()


def _is_port_in_use(host: str, port: int) -> bool:
    """Return True if a TCP listener already owns (host, port)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.bind((host, port))
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass
    return False


# === Scheduler subprocess management ===
class SchedulerSupervisor:
    """Manages the scheduler subprocess and its lifecycle.

    Threading model:
    - All state mutations (process, log_fp, should_run, restart_in_progress)
      occur under self.lock.
    - Long-running operations (terminate, taskkill, wait) execute OUTSIDE the
      lock to avoid blocking other threads for many seconds.
    - The monitor_thread is permanent: it never exits on its own. It pauses
      (via should_run=False) when explicitly told to.
    - start() refuses to spawn a process when should_run is False — this
      prevents orphaned subprocesses if stop() races with monitor restart.
    """

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.log_fp = None  # Open log file handle; must be closed on stop
        self.lock = threading.Lock()
        self.should_run = True
        self.monitor_thread: threading.Thread | None = None
        self.start_time: float = 0.0
        self.restart_in_progress = False  # Prevents monitor + manual restart racing

    def _resolve_python_exe(self) -> str | None:
        """Find the Python interpreter to launch the scheduler with."""
        if getattr(sys, 'frozen', False):
            # When running as PyInstaller .exe, find venv python alongside the exe
            venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
            if venv_python.exists():
                return str(venv_python)
            # Fallback: try system python
            for candidate in ("python.exe", "python3.exe"):
                from shutil import which
                p = which(candidate)
                if p:
                    return p
            return None
        return sys.executable

    def snapshot(self) -> tuple[bool, int | None]:
        """Thread-safe snapshot of running state and PID. Returns (running, pid_or_None)."""
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return True, self.process.pid
            return False, None

    def start(self) -> bool:
        """Start the scheduler subprocess. Returns True on success.

        Refuses to start if should_run is False (i.e. stop() was called).
        This prevents the monitor thread from spawning an orphan subprocess
        after a manual cleanup has been initiated.
        """
        with self.lock:
            if not self.should_run:
                print("[Launcher] start() refused: should_run is False", flush=True)
                return False

            if self.process is not None and self.process.poll() is None:
                print("[Launcher] scheduler already running", flush=True)
                return True

            python_exe = self._resolve_python_exe()
            if python_exe is None:
                show_notification(
                    "US Quant Live - Error",
                    f"Cannot find Python interpreter.\n\n"
                    f"Expected venv at: {PROJECT_ROOT}/venv/Scripts/python.exe\n"
                    f"Please ensure venv is set up.",
                )
                return False

            if not SCHEDULER_SCRIPT.exists():
                show_notification(
                    "US Quant Live - Error",
                    f"Scheduler script not found:\n{SCHEDULER_SCRIPT}\n\n"
                    f"The .exe must be placed next to the 'tools/' directory.",
                )
                return False

            # Check if dashboard port is already in use (often = stale scheduler from prior run)
            if _is_port_in_use("127.0.0.1", DASHBOARD_PORT):
                show_notification(
                    "US Quant Live - Port In Use",
                    f"Port {DASHBOARD_PORT} is already in use.\n\n"
                    f"Another instance of the scheduler/dashboard may still be running.\n"
                    f"Check Task Manager for leftover python.exe processes, or wait 30s.",
                )
                return False

            # Ensure log directory exists
            DASHBOARD_LOG.parent.mkdir(parents=True, exist_ok=True)

            cmd = [
                python_exe,
                str(SCHEDULER_SCRIPT),
                "--project-root",
                str(PROJECT_ROOT),
                "--dashboard-port",
                str(DASHBOARD_PORT),
            ]

            # Open log file (keep handle so we can close it)
            try:
                self.log_fp = open(DASHBOARD_LOG, "a", encoding="utf-8")
                self.log_fp.write(
                    f"\n=== [Launcher] Starting scheduler at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                self.log_fp.flush()
            except Exception as exc:
                show_notification("US Quant Live - Error", f"Cannot open log file:\n{exc}")
                return False

            try:
                # CREATE_NO_WINDOW: hide console window on Windows
                # CREATE_NEW_PROCESS_GROUP: allows clean kill of process tree later via taskkill /T
                creationflags = 0
                if os.name == 'nt':
                    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

                self.process = subprocess.Popen(
                    cmd,
                    stdout=self.log_fp,
                    stderr=subprocess.STDOUT,
                    cwd=str(PROJECT_ROOT),
                    creationflags=creationflags,
                )
                self.start_time = time.time()
                print(f"[Launcher] scheduler started (PID {self.process.pid})", flush=True)
                return True
            except Exception as exc:
                # Clean up log handle on failure
                self._close_log_fp()
                show_notification("US Quant Live - Error", f"Failed to start scheduler:\n{exc}")
                return False

    def _close_log_fp(self) -> None:
        """Close the log file handle if open."""
        if self.log_fp is not None:
            try:
                self.log_fp.close()
            except Exception:
                pass
            self.log_fp = None

    def _kill_process_tree(self, pid: int, timeout: float) -> None:
        """Kill a process and all its descendants. Windows uses taskkill /T /F."""
        if os.name == 'nt':
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as exc:
                print(f"[Launcher] taskkill failed: {exc}", flush=True)
        else:
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except Exception as exc:
                print(f"[Launcher] killpg failed: {exc}", flush=True)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the scheduler subprocess gracefully, killing the whole tree.
        Disables auto-restart by the monitor (set should_run=True to re-enable).

        Implementation notes:
        - Acquires lock only briefly to snapshot+clear state, then performs
          terminate/taskkill/wait OUTSIDE the lock. This prevents other
          threads (is_running, restart, monitor) from blocking on us for
          up to `timeout` seconds.
        """
        # Snapshot under lock; clear state immediately so other callers
        # (monitor, restart) see process=None and don't operate on it.
        with self.lock:
            self.should_run = False
            proc = self.process
            log_fp = self.log_fp
            self.process = None
            self.log_fp = None

        if proc is None:
            # Nothing to terminate, but still close any orphan log handle
            if log_fp is not None:
                try:
                    log_fp.close()
                except Exception:
                    pass
            return

        if proc.poll() is not None:
            # Already exited; just clean up handles
            if log_fp is not None:
                try:
                    log_fp.close()
                except Exception:
                    pass
            return

        pid = proc.pid
        print(f"[Launcher] stopping scheduler tree (PID {pid})...", flush=True)
        # Graceful first
        try:
            proc.terminate()
            proc.wait(timeout=timeout / 2)
            print("[Launcher] scheduler stopped gracefully", flush=True)
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            print(f"[Launcher] terminate error: {exc}", flush=True)

        # Always kill the process tree to clean up grandchildren
        # (executor subprocess, dashboard server etc.)
        self._kill_process_tree(pid, timeout=timeout / 2)
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

        # Close log handle last
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass

    def restart(self) -> bool:
        """Restart the scheduler safely (won't race with monitor)."""
        # Coordinate with monitor: pause auto-restart during the manual restart
        with self.lock:
            if self.restart_in_progress:
                print("[Launcher] restart already in progress, ignoring", flush=True)
                return False
            self.restart_in_progress = True
        try:
            self.stop()  # also sets should_run = False
            time.sleep(2)  # let port release
            with self.lock:
                self.should_run = True
            return self.start()
        finally:
            with self.lock:
                self.restart_in_progress = False

    def is_running(self) -> bool:
        """Check if scheduler is currently running."""
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def monitor_loop(self) -> None:
        """Background thread: restart scheduler if it dies unexpectedly.

        Lifecycle: this thread runs FOREVER until the process is killed.
        It pauses (no-ops) when should_run is False, and resumes auto-restart
        when should_run is True (set by start() / restart()).

        We catch all exceptions to avoid the thread dying silently — once it
        dies, no future crash detection works.
        """
        suppressed_until_manual_restart = False  # set after early-death

        while True:
            try:
                time.sleep(5)

                # Snapshot state under the lock; do any restart spawn OUTSIDE the lock
                with self.lock:
                    if not self.should_run or self.restart_in_progress:
                        continue
                    if suppressed_until_manual_restart:
                        # Wait for manual restart to clear the suppression flag
                        # (restart() sets should_run back to True; that's our signal)
                        continue
                    if self.process is None:
                        continue
                    exit_code = self.process.poll()
                    if exit_code is None:
                        continue  # Still running

                    # Process died — capture diagnostics under lock
                    uptime = time.time() - self.start_time
                    print(
                        f"[Launcher] scheduler died (code={exit_code}, uptime={uptime:.1f}s)",
                        flush=True,
                    )

                    # Close log handle and clear process slot
                    log_fp_to_close = self.log_fp
                    self.log_fp = None
                    self.process = None

                    # Decide whether to auto-restart
                    if uptime < EARLY_DEATH_THRESHOLD_SECONDS:
                        print(
                            f"[Launcher] scheduler exited too quickly ({uptime:.1f}s < "
                            f"{EARLY_DEATH_THRESHOLD_SECONDS}s), suspending auto-restart",
                            flush=True,
                        )
                        suppressed_until_manual_restart = True
                        # Atomic flip: don't auto-restart, but stay alive to react
                        # to a future manual restart (which sets should_run=True
                        # and we'll clear the suppression flag below).

                # Operations OUTSIDE the lock
                if log_fp_to_close is not None:
                    try:
                        log_fp_to_close.close()
                    except Exception:
                        pass

                if suppressed_until_manual_restart:
                    # Notify user, do not block the monitor (use async dialog)
                    show_notification_async(
                        "US Quant Live - Scheduler Crashed",
                        f"Scheduler exited unexpectedly within {uptime:.1f}s.\n\n"
                        f"This usually means a config error. Check the log:\n{DASHBOARD_LOG}\n\n"
                        f"Use the tray menu 'Restart Scheduler' to retry after fixing.",
                    )
                    # Wait for restart() to flip should_run back to True (manual signal).
                    # Poll the flag and clear suppression when user retries.
                    while True:
                        time.sleep(2)
                        with self.lock:
                            if self.should_run and self.process is not None:
                                # restart() succeeded; reset suppression
                                suppressed_until_manual_restart = False
                                break
                    continue  # Resume normal monitoring

                # Normal auto-restart path
                print("[Launcher] auto-restarting scheduler...", flush=True)
                self.start()
            except Exception as exc:
                # Never let the monitor die — log and continue. Otherwise future
                # crashes go undetected.
                import traceback
                print(f"[Launcher] monitor_loop exception: {exc}", flush=True)
                traceback.print_exc()
                time.sleep(5)

    def start_monitor(self) -> None:
        """Start the monitor thread (only if not already running)."""
        if self.monitor_thread is not None and self.monitor_thread.is_alive():
            return
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()


# === Tray menu actions ===
def make_menu(supervisor: SchedulerSupervisor, on_exit_callback) -> pystray.Menu:
    """Build the tray icon right-click menu."""

    def open_dashboard(icon, item):
        webbrowser.open(DASHBOARD_URL)

    def open_log_folder(icon, item):
        log_dir = ARTIFACTS_ROOT / "daemon"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(log_dir))

    def open_log_file(icon, item):
        if DASHBOARD_LOG.exists():
            os.startfile(str(DASHBOARD_LOG))
        else:
            show_notification("US Quant Live", "Log file not yet created. Wait a few seconds.")

    def restart_scheduler(icon, item):
        def _do_restart():
            ok = supervisor.restart()
            show_notification(
                "US Quant Live",
                "Scheduler restarted successfully" if ok else
                "Restart failed — check the log file via 'Open Latest Log'",
            )
        threading.Thread(target=_do_restart, daemon=True).start()

    def show_status(icon, item):
        # Atomic snapshot to avoid TOCTOU race with monitor_loop
        running, pid = supervisor.snapshot()
        msg = (
            f"Scheduler: {'Running' if running else 'Stopped'}\n"
            f"PID: {pid if pid is not None else 'N/A'}\n"
            f"Dashboard: {DASHBOARD_URL}"
        )
        show_notification("US Quant Live - Status", msg)

    def quit_app(icon, item):
        print("[Launcher] exit requested by user", flush=True)
        on_exit_callback()
        icon.stop()

    return pystray.Menu(
        pystray.MenuItem(
            "📊 Open Dashboard",
            open_dashboard,
            default=True,  # Double-click action
        ),
        pystray.MenuItem("📁 Open Log Folder", open_log_folder),
        pystray.MenuItem("📄 Open Latest Log", open_log_file),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("ℹ️  Status", show_status),
        pystray.MenuItem("🔄 Restart Scheduler", restart_scheduler),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ Exit", quit_app),
    )


# === Main ===
def main():
    print(f"[Launcher] US Quant Live Tray Launcher starting...", flush=True)
    print(f"[Launcher] PROJECT_ROOT: {PROJECT_ROOT}", flush=True)
    print(f"[Launcher] ICON: {ICON_PATH}", flush=True)

    # Single instance check
    singleton = SingleInstance(MUTEX_NAME)
    if not singleton.acquire():
        show_notification(
            "US Quant Live",
            "Launcher is already running.\nCheck system tray for the icon (bottom-right).",
        )
        return 1

    supervisor: SchedulerSupervisor | None = None
    try:
        # Load icon (try common locations: bundled with exe, then dev location)
        icon_search = [ICON_PATH]
        if getattr(sys, 'frozen', False):
            # PyInstaller extracts data files to sys._MEIPASS
            meipass = Path(getattr(sys, '_MEIPASS', SCRIPT_DIR))
            icon_search.insert(0, meipass / "tray_icon.ico")
        actual_icon = next((p for p in icon_search if p.exists()), None)

        if actual_icon is None:
            # In frozen mode the generator script isn't bundled — show clear error.
            if getattr(sys, 'frozen', False):
                show_notification(
                    "US Quant Live - Error",
                    f"Tray icon not found in bundled .exe.\n\n"
                    f"This is a build issue. Rebuild with:\n"
                    f"python tools/build_exe.py",
                )
                return 1
            # Dev mode: try regenerating
            print(f"[Launcher] icon not found, attempting to generate...", flush=True)
            try:
                from generate_tray_icon import main as gen_icon
                gen_icon()
                actual_icon = ICON_PATH
            except Exception as exc:
                show_notification("US Quant Live - Error", f"Failed to generate icon: {exc}")
                return 1

        # Load icon into memory and close the file handle immediately so that:
        # (1) the .ico file isn't locked while the tray runs, and
        # (2) Windows can replace the file (e.g., for icon updates).
        with Image.open(actual_icon) as src:
            icon_image = src.copy()

        # Start scheduler
        supervisor = SchedulerSupervisor()
        if not supervisor.start():
            return 1

        supervisor.start_monitor()

        # Wait briefly for dashboard to come up
        time.sleep(2)

        # Cleanup callback for tray exit
        def cleanup():
            print("[Launcher] cleanup: stopping scheduler...", flush=True)
            if supervisor is not None:
                supervisor.stop()
            print("[Launcher] cleanup complete", flush=True)

        # Create tray icon
        icon = pystray.Icon(
            'us-quant-live',
            icon_image,
            'US Quant Live\nDashboard: ' + DASHBOARD_URL,
            menu=make_menu(supervisor, cleanup),
        )

        print(f"[Launcher] tray icon created, dashboard at {DASHBOARD_URL}", flush=True)
        print(f"[Launcher] right-click the tray icon to access menu", flush=True)

        # Show startup notification (delayed, non-blocking)
        threading.Thread(
            target=lambda: (
                time.sleep(1),
                show_notification(
                    "US Quant Live - Started",
                    f"Trading daemon is running.\n\n"
                    f"Dashboard: {DASHBOARD_URL}\n\n"
                    f"Right-click the tray icon (bottom-right) for options.",
                ),
            ),
            daemon=True,
        ).start()

        # Run the tray icon (blocks until icon.stop() called)
        icon.run()

    except KeyboardInterrupt:
        print("[Launcher] interrupted by keyboard", flush=True)
    except Exception as exc:
        print(f"[Launcher] fatal error: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        show_notification("US Quant Live - Fatal Error", str(exc))
        return 1
    finally:
        # Ensure scheduler stopped and mutex released on every exit path
        if supervisor is not None:
            try:
                supervisor.stop()
            except Exception:
                pass
        singleton.release()
        print("[Launcher] exited", flush=True)

    return 0


if __name__ == '__main__':
    sys.exit(main())
