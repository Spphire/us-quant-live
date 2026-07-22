from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")


def resolve_session_date(now_cn: datetime, operating_window_start: dt_time | None = None) -> date:
    """Resolve the US trading session that a Beijing wall-clock moment operates on.

    Design contract
    ---------------
    The session_date MUST be identical for the decision run and the execute run of
    the same operating day, because execute reads the decision-targets file that
    decision wrote, and both are keyed by session_date. If they diverged, execute
    would look for a file decision never created.

    Operating window
    ----------------
    `operating_window_start` is the Beijing time at/after which a run is considered
    part of "today's" operating cycle (decision then execute). It MUST be <= the
    decision trigger time so that BOTH decision and execute fall on or after it and
    therefore resolve to the same Beijing date. The scheduler passes the configured
    decision time here, so the boundary tracks the actual trigger instead of a
    hardcoded noon. When omitted, defaults to 12:00 (the default decision time).

    Within the operating window the served US session falls on the same calendar
    number as the Beijing date (verified for both summer DST and winter standard
    time), so we anchor session_date to the Beijing date there.

    Early-morning correction
    ------------------------
    Before `operating_window_start` (e.g. a manual --run-once at 03:00 Beijing), the
    Beijing date is one day AHEAD of the most recent US session. We roll back to the
    US-Eastern date, which is the session that just closed — the correct target for
    reconciliation.

    Summary (with default 12:00 window start):
    - Beijing >= 12:00  -> session_date = Beijing date (matches the served session)
    - Beijing <  12:00  -> session_date = US-Eastern date (prior/most-recent session)
    """
    cutoff = operating_window_start if operating_window_start is not None else dt_time(12, 0)
    if now_cn.timetz().replace(tzinfo=None) >= cutoff:
        # Operating window: Beijing date == served US session date.
        return now_cn.date()
    # Before the operating window: roll back to the US-Eastern session date to avoid
    # being a day ahead of the actual US trading session.
    return now_cn.astimezone(US_TZ).date()




@dataclass(frozen=True)
class DayPaths:
    session_key: str
    decision_output_root: Path
    execute_output_root: Path
    decision_targets_path: Path
    decision_stdout_log: Path
    decision_stderr_log: Path
    execute_stdout_log: Path
    execute_stderr_log: Path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Keep an Alpaca daily workflow online: run DecisionEngine at 12:30 Beijing "
            "and execute the same day's decision_targets.csv at 22:00 Beijing."
        )
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument("--executor-path", default=None)
    parser.add_argument("--output-root", default="artifacts/daily_alpaca_scheduler")
    parser.add_argument("--state-path", default="artifacts/daily_alpaca_scheduler/state.json")

    parser.add_argument("--accounts-json-path", default="configs/alpaca_acounts/alpaca_accounts.local.json")
    parser.add_argument("--account-name", default="ALPACA_US_FULL")
    parser.add_argument("--data-base-url", default="https://data.alpaca.markets")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument(
        "--feed",
        default="sip",
        help="Feed for AlphaCore daily bars (historical, ends at session_date - 1). "
             "Default 'sip' gives consolidated full-market OHLC for cleaner factor signals. "
             "Free-tier SIP works for these historical bars because they never include the "
             "recent 15-minute window. Use 'iex' to force single-exchange coverage if needed.",
    )
    parser.add_argument(
        "--dynamic-feed",
        default="sip",
        help="Feed for DynamicSymbolPool bars (historical liquidity ranking, ends at "
             "session_date - 1). Default 'sip' for full-market liquidity. Free-tier compatible.",
    )
    parser.add_argument(
        "--execution-price-feed",
        default="iex",
        help="Feed for the latest-trade price refresh during execution (requires RECENT data). "
             "Default 'iex' — free-tier SIP rejects recent-data queries with HTTP 403, so SIP "
             "is NOT usable for execution pricing without a paid market-data subscription. "
             "IEX gives single-exchange last-trade, which is acceptable for top-1000 liquid "
             "names (all present on IEX). Set 'sip' only if you have an entitled subscription.",
    )

    # NOTE: decision fires at 12:30 BJ (not 12:00). BJ 12:00 == US Eastern 00:00 which
    # is exactly midnight ET — the free-tier Alpaca SIP endpoint marks the previous
    # trading day's daily bar as "recent" for ~15-30 minutes past midnight and rejects
    # queries against it with HTTP 403 "subscription does not permit querying recent
    # SIP data". Empirically at 00:00:26 ET the request 403's; by 00:17 ET it succeeds.
    # BJ 12:30 == 00:30 ET stays comfortably past the rollover, so decision runs
    # succeed on the first attempt instead of relying on the 30-minute retry.
    parser.add_argument("--decision-time-cn", default="12:30")
    parser.add_argument("--execute-time-cn", default="22:00")
    parser.add_argument("--target-ny-time", default="10:00")
    parser.add_argument(
        "--executor-trigger-mode",
        choices=("wait_target_time", "immediate", "wait_open"),
        default="wait_target_time",
        help="Mode passed to alpaca_executor for the 22:00 execution phase.",
    )

    parser.add_argument("--execution-mode", choices=("single_pass", "staged_regt"), default="staged_regt")
    parser.add_argument("--execution-order-style", choices=("marketable_limit", "market"), default="marketable_limit")
    parser.add_argument("--adverse-price-offset-bps", type=float, default=12.0)
    parser.add_argument("--marketable-limit-base-offset-bps", type=float, default=None)
    parser.add_argument("--marketable-limit-max-offset-bps", type=float, default=150.0)
    parser.add_argument("--marketable-limit-requote-steps-bps", default="0,25,75,150")
    parser.add_argument("--marketable-limit-requote-wait-seconds", type=float, default=6.0)
    parser.add_argument("--marketable-limit-max-attempts", type=int, default=4)
    parser.add_argument("--execution-workers", type=int, default=6)
    parser.add_argument("--sizing-adverse-offset-bps", type=float, default=None)
    parser.add_argument("--short-buying-power-adverse-offset-bps", type=float, default=300.0)
    parser.add_argument(
        "--entry-buying-power-buffer",
        "--buying-power-buffer",
        dest="buying_power_buffer",
        type=float,
        default=0.95,
    )
    parser.add_argument("--gross-capacity-target-ratio", type=float, default=0.95)
    parser.add_argument("--min-trade-notional", type=float, default=1.0)
    parser.add_argument("--min-trade-weight-bps", type=float, default=1.0)
    parser.add_argument("--order-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--order-poll-seconds", type=float, default=2.0)
    parser.add_argument("--staged-release-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--staged-entry-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--cancel-open-orders-before-submit", action="store_true")
    parser.add_argument("--no-submit", action="store_true", help="Pass --no-submit to the execution phase.")

    parser.add_argument(
        "--trading-day-source",
        choices=("alpaca_calendar", "weekday", "always"),
        default="alpaca_calendar",
        help=(
            "How to decide whether the resolved US session_date is a tradable US equity "
            "session. session_date is derived from the Beijing wall-clock via "
            "resolve_session_date() so it always refers to the correct US-Eastern session."
        ),
    )
    parser.add_argument("--allow-non-trading-day", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--heartbeat-minutes", type=float, default=30.0)
    parser.add_argument("--enable-dashboard", action="store_true", default=True, help="Launch dashboard HTTP server (default: enabled)")
    parser.add_argument("--no-dashboard", dest="enable_dashboard", action="store_false", help="Disable dashboard HTTP server")
    parser.add_argument("--dashboard-host", default="127.0.0.1", help="Dashboard server bind host")
    parser.add_argument("--dashboard-port", type=int, default=18076, help="Dashboard server port")
    parser.add_argument("--max-attempts-per-task", type=int, default=3)
    parser.add_argument("--max-execute-attempts", type=int, default=8)
    parser.add_argument("--retry-failed-after-minutes", type=float, default=30.0)
    parser.add_argument("--execute-retry-failed-after-minutes", type=float, default=5.0)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD session date for --run-once modes only.")
    parser.add_argument("--run-once", choices=("decision", "execute", "both", "due"), default=None)
    parser.add_argument("--force", action="store_true", help="Run even if the task is already completed in state.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without writing state or running them.")
    return parser.parse_args(argv)


def _start_dashboard_server(args: argparse.Namespace) -> subprocess.Popen | None:
    """Launch dashboard HTTP server as a child process."""
    if not bool(args.enable_dashboard):
        return None
    if _is_port_in_use(str(args.dashboard_host), int(args.dashboard_port)):
        print(
            f"[Scheduler] dashboard already listening at http://{args.dashboard_host}:{args.dashboard_port}; not starting another",
            flush=True,
        )
        return None
    dashboard_script = Path(args.project_root) / "tools" / "dashboard_server.py"
    if not dashboard_script.exists():
        print(f"[Scheduler] warning: dashboard_server.py not found at {dashboard_script}", flush=True)
        return None

    cmd = [
        str(args.python_executable),
        str(dashboard_script),
        "--artifacts-root",
        str(args.output_root),
        "--project-root",
        str(args.project_root),
        "--host",
        str(args.dashboard_host),
        "--port",
        str(int(args.dashboard_port)),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(args.project_root),
        )
        print(
            f"[Scheduler] dashboard server started at http://{args.dashboard_host}:{args.dashboard_port} (PID {proc.pid})",
            flush=True,
        )
        return proc
    except Exception as exc:
        print(f"[Scheduler] warning: failed to start dashboard server: {exc}", flush=True)
        return None


def _is_port_in_use(host: str, port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _resolve_config_paths(args)
    state = _load_state(args.state_path)
    calendar_cache: dict[str, bool] = {}
    daemon_identity_written = False
    _write_runtime_event(
        args,
        "scheduler_process_started",
        {
            "pid": os.getpid(),
            "argv": list(sys.argv),
            "project_root": Path(args.project_root).as_posix(),
            "state_path": Path(args.state_path).as_posix(),
            "run_once": args.run_once,
            "dry_run": bool(args.dry_run),
        },
    )

    # Start dashboard server as a child process (best-effort, non-blocking)
    if not args.run_once:
        _write_daemon_identity(args)
        daemon_identity_written = True
    dashboard_proc = _start_dashboard_server(args)
    _write_runtime_event(
        args,
        "dashboard_start_checked",
        {
            "enabled": bool(args.enable_dashboard),
            "dashboard_pid": dashboard_proc.pid if dashboard_proc is not None else None,
            "host": str(args.dashboard_host),
            "port": int(args.dashboard_port),
        },
    )

    try:
        if args.run_once:
            now_cn = datetime.now(CN_TZ)
            ok = _run_once(args=args, state=state, calendar_cache=calendar_cache, now_cn=now_cn)
            _write_runtime_event(args, "run_once_finished", {"run_once": args.run_once, "ok": bool(ok)})
            return 0 if ok else 1

        print(
            "[Scheduler] online. "
            f"decision={args.decision_time_cn} CN, execute={args.execute_time_cn} CN, "
            f"state={args.state_path}",
            flush=True,
        )
        last_heartbeat_at: datetime | None = None
        while True:
            now_cn = datetime.now(CN_TZ)
            try:
                _run_due_tasks(args=args, state=state, calendar_cache=calendar_cache, now_cn=now_cn)
            except KeyboardInterrupt:
                print("[Scheduler] stopped by keyboard interrupt.", flush=True)
                _write_runtime_event(args, "keyboard_interrupt", {})
                return 130
            except Exception as exc:  # Keep the daemon alive after transient API/process errors.
                print(f"[Scheduler] warning: loop error: {exc}", flush=True)
                _write_runtime_event(
                    args,
                    "loop_error",
                    {"error_type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc()},
                )

            # Restart dashboard if it died
            if dashboard_proc is not None and dashboard_proc.poll() is not None:
                print("[Scheduler] dashboard server exited, restarting...", flush=True)
                _write_runtime_event(
                    args,
                    "dashboard_exited",
                    {"dashboard_pid": dashboard_proc.pid, "returncode": dashboard_proc.returncode},
                )
                dashboard_proc = _start_dashboard_server(args)
                _write_runtime_event(
                    args,
                    "dashboard_restarted",
                    {"dashboard_pid": dashboard_proc.pid if dashboard_proc is not None else None},
                )

            heartbeat_due = (
                last_heartbeat_at is None
                or (now_cn - last_heartbeat_at).total_seconds() >= max(args.heartbeat_minutes, 0.1) * 60
            )
            if heartbeat_due:
                session_date = resolve_session_date(now_cn, _parse_hhmm(args.decision_time_cn))
                now_us = now_cn.astimezone(US_TZ)
                print(
                    f"[Scheduler] heartbeat {now_cn.isoformat(timespec='seconds')} "
                    f"(US {now_us.strftime('%Y-%m-%d %H:%M %Z')}) session={session_date.isoformat()}",
                    flush=True,
                )
                _write_runtime_event(
                    args,
                    "heartbeat",
                    {
                        "now_cn": now_cn.isoformat(timespec="seconds"),
                        "now_us": now_us.isoformat(timespec="seconds"),
                        "session_date": session_date.isoformat(),
                        "dashboard_pid": dashboard_proc.pid if dashboard_proc is not None else None,
                    },
                )
                last_heartbeat_at = now_cn

            time.sleep(max(float(args.poll_seconds), 1.0))
    finally:
        _write_runtime_event(args, "scheduler_process_stopping", {"pid": os.getpid()})
        if dashboard_proc is not None:
            try:
                dashboard_proc.terminate()
                dashboard_proc.wait(timeout=5)
            except Exception:
                pass
        if daemon_identity_written:
            _cleanup_daemon_identity(args)
        _write_runtime_event(args, "scheduler_process_stopped", {"pid": os.getpid()})


def _resolve_config_paths(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    args.project_root = project_root
    args.executor_path = _resolve_path(args.executor_path, project_root) if args.executor_path else project_root / "src" / "alpaca_executor.py"
    args.output_root = _resolve_path(args.output_root, project_root)
    args.state_path = _resolve_path(args.state_path, project_root)
    args.accounts_json_path = _resolve_path(args.accounts_json_path, project_root)


def _write_daemon_identity(args: argparse.Namespace) -> None:
    daemon_dir = Path(args.output_root) / "daemon"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "scheduler.pid").write_text(str(os.getpid()), encoding="ascii")
    command = " ".join(_quote_command_piece(piece) for piece in [sys.executable, *sys.argv])
    (daemon_dir / "scheduler.command.txt").write_text(command, encoding="utf-8")


def _cleanup_daemon_identity(args: argparse.Namespace) -> None:
    daemon_dir = Path(args.output_root) / "daemon"
    pid_path = daemon_dir / "scheduler.pid"
    try:
        if pid_path.exists() and pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
            pid_path.unlink(missing_ok=True)
    except Exception:
        pass


def _quote_command_piece(piece: Any) -> str:
    text = str(piece)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or any(ch in text for ch in ['"', "'"]):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _resolve_path(raw: str | Path, project_root: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (project_root / path).resolve()


def _run_once(
    *,
    args: argparse.Namespace,
    state: dict[str, Any],
    calendar_cache: dict[str, bool],
    now_cn: datetime,
) -> bool:
    session_date = date.fromisoformat(args.date) if args.date else resolve_session_date(now_cn, _parse_hhmm(args.decision_time_cn))
    if not _session_is_tradable(args, session_date, calendar_cache):
        print(f"[Scheduler] skip {session_date}: not a US trading day.", flush=True)
        return True

    paths = _day_paths(args, session_date)
    if args.run_once == "decision":
        return _run_task(args=args, state=state, session_date=session_date, task="decision", paths=paths, now_cn=now_cn)
    if args.run_once == "execute":
        return _run_task(args=args, state=state, session_date=session_date, task="execute", paths=paths, now_cn=now_cn)
    if args.run_once == "both":
        decision_ok = _run_task(args=args, state=state, session_date=session_date, task="decision", paths=paths, now_cn=now_cn)
        execute_ok = _run_task(args=args, state=state, session_date=session_date, task="execute", paths=paths, now_cn=now_cn)
        return bool(decision_ok and execute_ok)
    return _run_due_tasks(args=args, state=state, calendar_cache=calendar_cache, now_cn=now_cn, session_date=session_date)


def _run_due_tasks(
    *,
    args: argparse.Namespace,
    state: dict[str, Any],
    calendar_cache: dict[str, bool],
    now_cn: datetime,
    session_date: date | None = None,
) -> bool:
    session_date = session_date or resolve_session_date(now_cn, _parse_hhmm(args.decision_time_cn))
    trace: dict[str, Any] = _build_due_trace_base(args=args, state=state, now_cn=now_cn, session_date=session_date)
    try:
        tradable = _session_is_tradable(args, session_date, calendar_cache)
        trace["tradable"] = bool(tradable)
        trace["calendar_cache_keys"] = sorted(calendar_cache.keys())
    except Exception as exc:
        trace["tradable"] = False
        trace["calendar_error"] = {"type": type(exc).__name__, "message": str(exc)}
        _write_due_trace(args, trace)
        raise
    if not tradable:
        trace["decision"] = {"due": False, "action": "skip", "reason": "not_trading_day"}
        trace["execute"] = {"due": False, "action": "skip", "reason": "not_trading_day"}
        _write_due_trace(args, trace)
        return True

    decision_time = _parse_hhmm(args.decision_time_cn)
    execute_time = _parse_hhmm(args.execute_time_cn)
    paths = _day_paths(args, session_date)
    ran_ok = True
    decision_task_state = _task_state(state, session_date, "decision")
    execute_task_state = _task_state(state, session_date, "execute")
    trace["paths"] = {
        "decision_output_root": paths.decision_output_root.as_posix(),
        "execute_output_root": paths.execute_output_root.as_posix(),
        "decision_targets_path_default": paths.decision_targets_path.as_posix(),
        "decision_targets_path_resolved": _decision_targets_path_from_state(state, session_date, paths).as_posix(),
    }
    trace["decision"] = _task_due_trace(
        task="decision",
        task_state=decision_task_state,
        args=args,
        now_cn=now_cn,
        due_time=decision_time,
    )
    trace["execute"] = _task_due_trace(
        task="execute",
        task_state=execute_task_state,
        args=args,
        now_cn=now_cn,
        due_time=execute_time,
    )

    if now_cn.time() >= decision_time:
        trace["decision"]["due"] = True
        ran_ok = _run_task(args=args, state=state, session_date=session_date, task="decision", paths=paths, now_cn=now_cn) and ran_ok
        trace["decision"]["action"] = "attempted" if trace["decision"].get("can_attempt") else "skipped"
        trace["decision"]["status_after"] = str(decision_task_state.get("status") or "")
        trace["decision"]["attempts_after"] = int(decision_task_state.get("attempts") or 0)
    else:
        trace["decision"]["action"] = "wait"
        trace["decision"]["reason"] = "before_decision_time"

    if now_cn.time() >= execute_time:
        trace["execute"]["due"] = True
        target_path = _decision_targets_path_from_state(state, session_date, paths)
        trace["execute"]["decision_targets_exists"] = bool(target_path.exists())
        if args.dry_run or target_path.exists():
            ran_ok = _run_task(args=args, state=state, session_date=session_date, task="execute", paths=paths, now_cn=now_cn) and ran_ok
            trace["execute"]["action"] = "attempted" if trace["execute"].get("can_attempt") else "skipped"
            trace["execute"]["status_after"] = str(execute_task_state.get("status") or "")
            trace["execute"]["attempts_after"] = int(execute_task_state.get("attempts") or 0)
        else:
            trace["execute"]["action"] = "wait"
            trace["execute"]["reason"] = "missing_decision_targets"
            print(
                f"[Scheduler] execute waiting for decision targets: {target_path}",
                flush=True,
            )
    else:
        trace["execute"]["action"] = "wait"
        trace["execute"]["reason"] = "before_execute_time"

    trace["ran_ok"] = bool(ran_ok)
    _write_due_trace(args, trace)
    return ran_ok


def _build_due_trace_base(
    *,
    args: argparse.Namespace,
    state: dict[str, Any],
    now_cn: datetime,
    session_date: date,
) -> dict[str, Any]:
    sessions = state.get("sessions", {}) if isinstance(state.get("sessions"), dict) else {}
    session_state = sessions.get(session_date.isoformat(), {}) if isinstance(sessions, dict) else {}
    now_us = now_cn.astimezone(US_TZ)
    return {
        "schema_version": "1.0",
        "record_type": "scheduler_due_check",
        "generated_at_cn": now_cn.isoformat(timespec="seconds"),
        "generated_at_utc": now_cn.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_us": now_us.isoformat(timespec="seconds"),
        "session_date": session_date.isoformat(),
        "decision_time_cn": str(args.decision_time_cn),
        "execute_time_cn": str(args.execute_time_cn),
        "target_ny_time": str(args.target_ny_time),
        "trading_day_source": str(args.trading_day_source),
        "allow_non_trading_day": bool(args.allow_non_trading_day),
        "dry_run": bool(args.dry_run),
        "force": bool(args.force),
        "state_path": Path(args.state_path).as_posix(),
        "session_state_before": _jsonable(session_state),
    }


def _task_due_trace(
    *,
    task: str,
    task_state: dict[str, Any],
    args: argparse.Namespace,
    now_cn: datetime,
    due_time: dt_time,
) -> dict[str, Any]:
    can_attempt, reason = _task_can_attempt_reason(task_state, args, now_cn, task=task)
    last_raw = task_state.get("finished_at_cn") or task_state.get("started_at_cn")
    last_at = _parse_datetime(str(last_raw)) if last_raw else None
    retry_after_minutes = (
        float(args.execute_retry_failed_after_minutes)
        if str(task) == "execute"
        else float(args.retry_failed_after_minutes)
    )
    return {
        "due": now_cn.time() >= due_time,
        "due_time_cn": str(due_time),
        "status_before": str(task_state.get("status") or ""),
        "attempts_before": int(task_state.get("attempts") or 0),
        "max_attempts": int(args.max_execute_attempts) if str(task) == "execute" else int(args.max_attempts_per_task),
        "last_event_at_cn": last_at.isoformat(timespec="seconds") if last_at else "",
        "retry_after_minutes": retry_after_minutes,
        "can_attempt": bool(can_attempt),
        "can_attempt_reason": reason,
        "command_before": str(task_state.get("command") or ""),
        "output_root_before": str(task_state.get("output_root") or ""),
    }


def _write_due_trace(args: argparse.Namespace, payload: Mapping[str, Any]) -> None:
    try:
        daemon_dir = Path(args.output_root) / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        json_line = json.dumps(payload, ensure_ascii=False, default=_json_default)
        trace_path = daemon_dir / "scheduler_due_trace.jsonl"
        with trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json_line + "\n")
        latest_path = daemon_dir / "scheduler_due_latest.json"
        latest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
    except Exception:
        return


def _write_runtime_event(args: argparse.Namespace, event_type: str, payload: Mapping[str, Any]) -> None:
    try:
        daemon_dir = Path(args.output_root) / "daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        now_cn = datetime.now(CN_TZ)
        event = {
            "schema_version": "1.0",
            "record_type": "scheduler_runtime_event",
            "event_type": str(event_type),
            "generated_at_cn": now_cn.isoformat(timespec="seconds"),
            "generated_at_utc": now_cn.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            **dict(payload),
        }
        json_line = json.dumps(event, ensure_ascii=False, default=_json_default)
        with (daemon_dir / "scheduler_runtime_events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json_line + "\n")
        (daemon_dir / "scheduler_runtime_latest.json").write_text(
            json.dumps(event, indent=2, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
    except Exception:
        return


def _session_is_tradable(args: argparse.Namespace, session_date: date, calendar_cache: dict[str, bool]) -> bool:
    if args.allow_non_trading_day or args.trading_day_source == "always":
        return True
    if args.trading_day_source == "weekday":
        return session_date.weekday() < 5

    key = session_date.isoformat()
    if key in calendar_cache:
        return bool(calendar_cache[key])

    client = _calendar_client(args)
    payload = client._get_trading("/v2/calendar", {"start": key, "end": key})
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Alpaca calendar payload shape.")
    is_open = any(isinstance(item, dict) and str(item.get("date")) == key for item in payload)
    calendar_cache[key] = bool(is_open)
    return bool(is_open)


def _calendar_client(args: argparse.Namespace) -> Any:
    src_root = args.project_root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    from dynamic_symbol_pool import _resolve_alpaca_credentials  # noqa: WPS433
    from vendors import AlpacaHttpClient  # noqa: WPS433

    credentials = _resolve_alpaca_credentials(
        accounts_json_path=str(args.accounts_json_path),
        account_name=str(args.account_name),
        data_base_url=str(args.data_base_url),
        request_timeout_seconds=float(args.request_timeout_seconds),
        max_retries=int(args.max_retries),
    )
    return AlpacaHttpClient(credentials)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat()
    return str(value)


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    except Exception:
        return {"repr": repr(value)}


def _redact_value(key: str, value: Any) -> Any:
    key_l = str(key).lower()
    if any(token in key_l for token in ("secret", "password", "token", "api_key", "key_id")):
        if value in (None, ""):
            return value
        return f"<redacted:{len(str(value))} chars>"
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _scheduler_args_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return {str(key): _redact_value(str(key), value) for key, value in sorted(vars(args).items())}


def _selected_environment_snapshot() -> dict[str, Any]:
    env_keys = [
        "COMPUTERNAME",
        "CONDA_PREFIX",
        "PROCESSOR_ARCHITECTURE",
        "PYTHONPATH",
        "USERNAME",
        "USERPROFILE",
        "VIRTUAL_ENV",
    ]
    path_text = os.environ.get("PATH", "")
    return {
        "selected": {key: os.environ.get(key) for key in env_keys if os.environ.get(key) is not None},
        "path_entry_count": len([part for part in path_text.split(os.pathsep) if part]),
        "path_sha256": hashlib.sha256(path_text.encode("utf-8", errors="ignore")).hexdigest()
        if path_text
        else None,
    }


def _run_git(project_root: Path, command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *command],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _scheduler_git_snapshot(project_root: Path) -> dict[str, Any]:
    return {
        "commit": _run_git(project_root, ["rev-parse", "HEAD"]),
        "branch": _run_git(project_root, ["rev-parse", "--abbrev-ref", "HEAD"]),
        "status_short": _run_git(project_root, ["status", "--short"]),
        "diff_name_status": _run_git(project_root, ["diff", "--name-status"]),
    }


def _tail_file(path: Path, max_bytes: int = 20000) -> dict[str, Any]:
    if not path.exists():
        return {"path": path.as_posix(), "exists": False, "bytes": 0, "truncated": False, "text": ""}
    try:
        stat = path.stat()
        with path.open("rb") as fh:
            if stat.st_size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            data = fh.read()
        return {
            "path": path.as_posix(),
            "exists": True,
            "bytes": int(stat.st_size),
            "truncated": bool(stat.st_size > max_bytes),
            "text": data.decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {
            "path": path.as_posix(),
            "exists": path.exists(),
            "bytes": None,
            "truncated": None,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "text": "",
        }


def _path_status(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except Exception:
        return {"path": path.as_posix(), "exists": False, "bytes": None}
    return {"path": path.as_posix(), "exists": path.exists(), "bytes": int(stat.st_size)}


def _write_scheduler_task_json(path: Path, payload: Mapping[str, Any]) -> str | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _finalize_scheduler_run_evidence(output_root: Path) -> str | None:
    """Refresh executor evidence indexes after scheduler context/result files exist.

    The executor writes run_evidence_digest.json before the scheduler writes
    scheduler_task_result.json. Refreshing here prevents a false evidence gap for
    the scheduler result, file hash manifest, and artifact completeness snapshot.
    """
    try:
        src_root = PROJECT_ROOT / "src"
        if str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
        from alpaca_executor import _finalize_run_evidence  # noqa: WPS433

        _finalize_run_evidence(Path(output_root), refresh_runtime_environment=False)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _build_scheduler_task_context(
    *,
    args: argparse.Namespace,
    state: dict[str, Any],
    previous_task_state: Any,
    session_date: date,
    task: str,
    paths: DayPaths,
    now_cn: datetime,
    command: Sequence[Any],
    command_text: str,
    stdout_log: Path,
    stderr_log: Path,
    attempt_number: int,
    context_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    output_root = _task_output_root(task, paths)
    now_utc = now_cn.astimezone(timezone.utc)
    return {
        "schema_version": "1.0",
        "record_type": "scheduler_task_context",
        "generated_at_cn": _iso_now_cn(),
        "generated_at_utc": _utc_now(),
        "trigger_now_cn": now_cn.isoformat(timespec="seconds"),
        "trigger_now_utc": now_utc.isoformat(timespec="seconds"),
        "trigger_now_us": now_cn.astimezone(US_TZ).isoformat(timespec="seconds"),
        "session_date": session_date.isoformat(),
        "session_key": paths.session_key,
        "task": str(task),
        "attempt": int(attempt_number),
        "timing": {
            "decision_time_cn": str(args.decision_time_cn),
            "execute_time_cn": str(args.execute_time_cn),
            "target_ny_time": str(args.target_ny_time),
            "executor_trigger_mode": str(args.executor_trigger_mode),
            "retry_failed_after_minutes": float(args.retry_failed_after_minutes),
            "execute_retry_failed_after_minutes": float(args.execute_retry_failed_after_minutes),
            "max_attempts_per_task": int(args.max_attempts_per_task),
            "max_execute_attempts": int(args.max_execute_attempts),
        },
        "paths": {
            "project_root": Path(args.project_root).as_posix(),
            "state_path": Path(args.state_path).as_posix(),
            "output_root": output_root.as_posix(),
            "decision_output_root": paths.decision_output_root.as_posix(),
            "execute_output_root": paths.execute_output_root.as_posix(),
            "decision_targets_path_default": paths.decision_targets_path.as_posix(),
            "decision_targets_path_resolved": _decision_targets_path_from_state(state, session_date, paths).as_posix(),
            "stdout_log": stdout_log.as_posix(),
            "stderr_log": stderr_log.as_posix(),
            "scheduler_task_context": context_path.as_posix(),
            "scheduler_task_result": result_path.as_posix(),
        },
        "command": {
            "argv": [str(item) for item in command],
            "command_text": command_text,
            "cwd": Path(args.project_root).as_posix(),
        },
        "scheduler_args": _scheduler_args_snapshot(args),
        "previous_task_state": previous_task_state,
        "state_version": state.get("version"),
        "state_session_keys": sorted((state.get("sessions") or {}).keys())
        if isinstance(state.get("sessions"), dict)
        else [],
        "process": {
            "pid": os.getpid(),
            "cwd": Path.cwd().as_posix(),
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "argv": list(sys.argv),
        },
        "environment": _selected_environment_snapshot(),
        "code": {
            "script_path": Path(__file__).resolve().as_posix(),
            "git": _scheduler_git_snapshot(Path(args.project_root)),
        },
    }


def _build_scheduler_task_result(
    *,
    args: argparse.Namespace,
    session_date: date,
    task: str,
    paths: DayPaths,
    stdout_log: Path,
    stderr_log: Path,
    command_text: str,
    returncode: int,
    elapsed_seconds: float,
    task_state: Mapping[str, Any],
    context_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    output_root = _task_output_root(task, paths)
    return {
        "schema_version": "1.0",
        "record_type": "scheduler_task_result",
        "generated_at_cn": _iso_now_cn(),
        "generated_at_utc": _utc_now(),
        "session_date": session_date.isoformat(),
        "session_key": paths.session_key,
        "task": str(task),
        "attempt": int(task_state.get("attempts") or 0),
        "status": str(task_state.get("status") or ""),
        "returncode": int(returncode),
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "command_text": command_text,
        "paths": {
            "project_root": Path(args.project_root).as_posix(),
            "state_path": Path(args.state_path).as_posix(),
            "output_root": output_root.as_posix(),
            "stdout_log": stdout_log.as_posix(),
            "stderr_log": stderr_log.as_posix(),
            "scheduler_task_context": context_path.as_posix(),
            "scheduler_task_result": result_path.as_posix(),
        },
        "artifacts": {
            "execution_summary": _path_status(output_root / "execution_summary.json"),
            "execution_quality": _path_status(output_root / "execution_quality.json"),
            "daily_audit_dir": _path_status(output_root / "audit"),
            "decision_targets": _path_status(output_root / "decision_targets.csv"),
            "order_plan": _path_status(output_root / "order_plan.json"),
            "execution_records": _path_status(output_root / "execution_records.json"),
            "order_poll_timeline": _path_status(output_root / "order_poll_timeline.json"),
            "run_context": _path_status(output_root / "run_context.json"),
            "alpaca_api_audit": _path_status(output_root / "alpaca_api_audit.jsonl"),
            "scheduler_due_trace": _path_status(Path(args.output_root) / "daemon" / "scheduler_due_trace.jsonl"),
            "scheduler_due_latest": _path_status(Path(args.output_root) / "daemon" / "scheduler_due_latest.json"),
            "scheduler_runtime_events": _path_status(
                Path(args.output_root) / "daemon" / "scheduler_runtime_events.jsonl"
            ),
            "run_evidence_digest": _path_status(output_root / "run_evidence_digest.json"),
            "run_artifact_manifest": _path_status(output_root / "run_artifact_manifest.json"),
            "staged_rebuild_snapshots": _path_status(output_root / "staged_rebuild_snapshots.json"),
            "execution_price_snapshot": _path_status(output_root / "execution_price_snapshot.json"),
            "executable_target_projection": _path_status(
                output_root / "executable_target_projection.json"
            ),
            "execution_latest_trades_snapshot": _path_status(output_root / "execution_latest_trades_snapshot.json"),
            "execution_latest_quotes_snapshot": _path_status(output_root / "execution_latest_quotes_snapshot.json"),
            "execution_latest_quotes_snapshot_after": _path_status(
                output_root / "execution_latest_quotes_snapshot_after.json"
            ),
            "execution_intraday_bars_1min": _path_status(output_root / "execution_intraday_bars_1min.json"),
            "execution_intraday_bars_1min_after": _path_status(
                output_root / "execution_intraday_bars_1min_after.json"
            ),
            "broker_calendar_window": _path_status(output_root / "broker_calendar_window.json"),
            "broker_account_configurations_before": _path_status(
                output_root / "broker_account_configurations_before.json"
            ),
            "broker_account_configurations_after": _path_status(
                output_root / "broker_account_configurations_after.json"
            ),
            "broker_corporate_actions": _path_status(output_root / "broker_corporate_actions.json"),
            "broker_portfolio_history_before": _path_status(output_root / "broker_portfolio_history_before.json"),
            "broker_portfolio_history_after": _path_status(output_root / "broker_portfolio_history_after.json"),
            "position_account_stability_before": _path_status(
                output_root / "broker_position_account_stability_before.json"
            ),
            "position_account_stability_after": _path_status(
                output_root / "broker_position_account_stability_after.json"
            ),
            "equity_pnl_bridge": _path_status(output_root / "audit" / "30_equity_pnl_bridge.json"),
            "execution_attribution_summary": _path_status(
                output_root / "audit" / "28_execution_attribution_summary.json"
            ),
            "position_snapshot_integrity": _path_status(
                output_root / "audit" / "37_position_snapshot_integrity.json"
            ),
            "residual_diagnosis": _path_status(output_root / "audit" / "38_residual_diagnosis.json"),
            "evidence_completeness": _path_status(output_root / "audit" / "39_evidence_completeness.json"),
            "target_transition_trace": _path_status(output_root / "audit" / "40_target_transition_trace.csv"),
            "target_transition_summary": _path_status(output_root / "audit" / "41_target_transition_summary.json"),
            "decision_intent_trace": _path_status(output_root / "audit" / "42_decision_intent_trace.csv"),
            "decision_intent_summary": _path_status(output_root / "audit" / "43_decision_intent_summary.json"),
            "order_constraint_trace": _path_status(output_root / "audit" / "44_order_constraint_trace.csv"),
            "order_constraint_summary": _path_status(output_root / "audit" / "45_order_constraint_summary.json"),
            "decision_execute_drift": _path_status(output_root / "audit" / "46_decision_execute_drift.csv"),
            "decision_execute_drift_summary": _path_status(
                output_root / "audit" / "47_decision_execute_drift_summary.json"
            ),
            "market_price_evidence": _path_status(output_root / "audit" / "48_market_price_evidence.csv"),
            "market_price_evidence_summary": _path_status(
                output_root / "audit" / "49_market_price_evidence_summary.json"
            ),
            "account_activity_attribution": _path_status(
                output_root / "audit" / "50_account_activity_attribution.csv"
            ),
            "account_activity_attribution_summary": _path_status(
                output_root / "audit" / "51_account_activity_attribution_summary.json"
            ),
            "strict_attribution_checklist": _path_status(
                output_root / "audit" / "52_strict_attribution_checklist.csv"
            ),
            "strict_attribution_checklist_summary": _path_status(
                output_root / "audit" / "53_strict_attribution_checklist_summary.json"
            ),
            "corporate_action_trace": _path_status(output_root / "audit" / "54_corporate_action_trace.csv"),
            "corporate_action_summary": _path_status(output_root / "audit" / "55_corporate_action_summary.json"),
            "portfolio_history_trace": _path_status(output_root / "audit" / "56_portfolio_history_trace.csv"),
            "portfolio_history_summary": _path_status(output_root / "audit" / "57_portfolio_history_summary.json"),
            "intraday_bar_evidence": _path_status(output_root / "audit" / "58_intraday_bar_evidence.csv"),
            "intraday_bar_summary": _path_status(output_root / "audit" / "59_intraday_bar_summary.json"),
            "quote_evidence": _path_status(output_root / "audit" / "60_quote_evidence.csv"),
            "quote_summary": _path_status(output_root / "audit" / "61_quote_summary.json"),
            "calendar_trace": _path_status(output_root / "audit" / "62_calendar_trace.csv"),
            "calendar_summary": _path_status(output_root / "audit" / "63_calendar_summary.json"),
            "account_state_bridge": _path_status(output_root / "audit" / "64_account_state_bridge.csv"),
            "account_state_bridge_summary": _path_status(
                output_root / "audit" / "65_account_state_bridge_summary.json"
            ),
            "market_context_attribution": _path_status(
                output_root / "audit" / "66_market_context_attribution.csv"
            ),
            "market_context_summary": _path_status(output_root / "audit" / "67_market_context_summary.json"),
            "replay_focus_trace": _path_status(output_root / "audit" / "68_replay_focus_trace.csv"),
            "attribution_dossier": _path_status(output_root / "audit" / "69_attribution_dossier.json"),
            "run_evidence_digest_summary": _path_status(
                output_root / "audit" / "72_run_evidence_digest_summary.json"
            ),
            "run_evidence_digest_checks": _path_status(
                output_root / "audit" / "73_run_evidence_digest_checks.csv"
            ),
            "executable_target_projection_trace": _path_status(
                output_root / "audit" / "80_executable_target_projection.csv"
            ),
            "executable_target_projection_summary": _path_status(
                output_root / "audit" / "81_executable_target_projection_summary.json"
            ),
        },
        "task_state": _jsonable(dict(task_state)),
        "logs": {
            "stdout_tail": _tail_file(stdout_log),
            "stderr_tail": _tail_file(stderr_log),
        },
    }


def _run_task(
    *,
    args: argparse.Namespace,
    state: dict[str, Any],
    session_date: date,
    task: str,
    paths: DayPaths,
    now_cn: datetime,
) -> bool:
    task_state = _task_state(state, session_date, task)
    if not _task_can_attempt(task_state, args, now_cn, task=task):
        return True

    if task == "execute":
        target_path = _decision_targets_path_from_state(state, session_date, paths)
        if not args.dry_run and not target_path.exists():
            print(f"[Scheduler] execute skipped; missing decision targets: {target_path}", flush=True)
            return False

    command = _build_command(args, session_date, task, paths, state)
    stdout_log, stderr_log = _task_logs(task, paths)
    command_text = subprocess.list2cmdline([str(item) for item in command])
    output_root = _task_output_root(task, paths)
    scheduler_task_context_path = output_root / "scheduler_task_context.json"
    scheduler_task_result_path = output_root / "scheduler_task_result.json"

    if args.dry_run:
        print(f"[Scheduler][dry-run][{task}] {command_text}", flush=True)
        return True

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    paths.decision_output_root.mkdir(parents=True, exist_ok=True)
    paths.execute_output_root.mkdir(parents=True, exist_ok=True)

    previous_attempts = int(task_state.get("attempts") or 0)
    previous_task_state = _jsonable(dict(task_state))
    attempt_number = previous_attempts + 1
    try:
        context_payload = _build_scheduler_task_context(
            args=args,
            state=state,
            previous_task_state=previous_task_state,
            session_date=session_date,
            task=task,
            paths=paths,
            now_cn=now_cn,
            command=command,
            command_text=command_text,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            attempt_number=attempt_number,
            context_path=scheduler_task_context_path,
            result_path=scheduler_task_result_path,
        )
        context_error = _write_scheduler_task_json(scheduler_task_context_path, context_payload)
    except Exception as exc:
        context_error = f"{type(exc).__name__}: {exc}"
    task_state.clear()
    task_state.update(
        {
            "status": "started",
            "attempts": attempt_number,
            "started_at_cn": _iso_now_cn(),
            "command": command_text,
            "stdout_log": stdout_log.as_posix(),
            "stderr_log": stderr_log.as_posix(),
            "scheduler_task_context_path": scheduler_task_context_path.as_posix(),
            "scheduler_task_result_path": scheduler_task_result_path.as_posix(),
        }
    )
    if context_error:
        task_state["scheduler_task_context_error"] = context_error
    _save_state(args.state_path, state)

    print(f"[Scheduler] starting {task} for {session_date}: {command_text}", flush=True)
    started_monotonic = time.monotonic()
    try:
        with stdout_log.open("a", encoding="utf-8") as stdout_handle, stderr_log.open("a", encoding="utf-8") as stderr_handle:
            stdout_handle.write(f"\n=== {task} {session_date} start {datetime.now(CN_TZ).isoformat(timespec='seconds')} ===\n")
            stdout_handle.write(f"command: {command_text}\n")
            stdout_handle.flush()
            result = subprocess.run(
                [str(item) for item in command],
                cwd=str(args.project_root),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                check=False,
            )
            returncode = int(result.returncode)
    except Exception as exc:
        returncode = 1
        task_state["error"] = str(exc)

    elapsed_seconds = time.monotonic() - started_monotonic
    task_state.update(
        {
            "finished_at_cn": _iso_now_cn(),
            "elapsed_seconds": round(float(elapsed_seconds), 3),
            "returncode": int(returncode),
            "output_root": _task_output_root(task, paths).as_posix(),
        }
    )

    if returncode == 0:
        _capture_task_outputs(task_state, task, paths)
        if task == "decision":
            target_path = Path(str(task_state.get("decision_targets_path") or paths.decision_targets_path))
            if not target_path.exists():
                task_state["status"] = "failed"
                task_state["error"] = f"decision completed but targets file was not found: {target_path}"
            else:
                task_state["status"] = "completed"
        else:
            task_state["status"] = "completed"
            # After a successful execute, generate the execution-quality report
            # (fill rate, slippage bps, tracking error, cancel attribution) so
            # every session leaves a consistent post-trade record for review.
            _generate_execution_quality(paths.execute_output_root)
    else:
        task_state["status"] = "failed"

    try:
        result_payload = _build_scheduler_task_result(
            args=args,
            session_date=session_date,
            task=task,
            paths=paths,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            command_text=command_text,
            returncode=returncode,
            elapsed_seconds=elapsed_seconds,
            task_state=task_state,
            context_path=scheduler_task_context_path,
            result_path=scheduler_task_result_path,
        )
        result_error = _write_scheduler_task_json(scheduler_task_result_path, result_payload)
    except Exception as exc:
        result_error = f"{type(exc).__name__}: {exc}"
    if result_error:
        task_state["scheduler_task_result_error"] = result_error
    evidence_finalize_error = _finalize_scheduler_run_evidence(output_root)
    if evidence_finalize_error:
        task_state["run_evidence_finalize_error"] = evidence_finalize_error
    _save_state(args.state_path, state)
    if returncode == 0 and task == "execute" and str(task_state.get("status")) == "completed":
        _generate_daily_audit(paths.execute_output_root, paths.decision_output_root)
    print(
        f"[Scheduler] finished {task} for {session_date}: status={task_state['status']} "
        f"returncode={returncode} elapsed={elapsed_seconds:.1f}s",
        flush=True,
    )
    return str(task_state.get("status")) == "completed"


def _build_command(
    args: argparse.Namespace,
    session_date: date,
    task: str,
    paths: DayPaths,
    state: dict[str, Any],
) -> list[str | Path]:
    command: list[str | Path] = [
        args.python_executable,
        args.executor_path,
        "--date",
        session_date.isoformat(),
        "--accounts-json-path",
        args.accounts_json_path,
        "--account-name",
        args.account_name,
        "--data-base-url",
        args.data_base_url,
        "--request-timeout-seconds",
        _num(args.request_timeout_seconds),
        "--max-retries",
        str(int(args.max_retries)),
        "--feed",
        args.feed,
        "--dynamic-feed",
        args.dynamic_feed,
        "--execution-price-feed",
        args.execution_price_feed,
        "--execution-mode",
        args.execution_mode,
        "--execution-order-style",
        args.execution_order_style,
        "--adverse-price-offset-bps",
        _num(args.adverse_price_offset_bps),
        "--short-buying-power-adverse-offset-bps",
        _num(args.short_buying_power_adverse_offset_bps),
        "--entry-buying-power-buffer",
        _num(args.buying_power_buffer),
        "--gross-capacity-target-ratio",
        _num(args.gross_capacity_target_ratio),
        "--min-trade-notional",
        _num(args.min_trade_notional),
        "--min-trade-weight-bps",
        _num(args.min_trade_weight_bps),
        "--order-timeout-seconds",
        _num(args.order_timeout_seconds),
        "--order-poll-seconds",
        _num(args.order_poll_seconds),
    ]
    _append_optional_float(command, "--marketable-limit-base-offset-bps", args.marketable_limit_base_offset_bps)
    _append_optional_float(command, "--marketable-limit-max-offset-bps", args.marketable_limit_max_offset_bps)
    command.extend(
        [
            "--marketable-limit-requote-steps-bps",
            str(args.marketable_limit_requote_steps_bps),
            "--marketable-limit-requote-wait-seconds",
            _num(args.marketable_limit_requote_wait_seconds),
            "--marketable-limit-max-attempts",
            str(int(args.marketable_limit_max_attempts)),
            "--execution-workers",
            str(int(args.execution_workers)),
        ]
    )
    _append_optional_float(command, "--sizing-adverse-offset-bps", args.sizing_adverse_offset_bps)
    _append_optional_float(command, "--staged-release-timeout-seconds", args.staged_release_timeout_seconds)
    _append_optional_float(command, "--staged-entry-timeout-seconds", args.staged_entry_timeout_seconds)

    if task == "decision":
        command.extend(
            [
                "--trigger-mode",
                "plan_only",
                "--no-submit",
                "--output-root",
                paths.decision_output_root,
            ]
        )
        return command

    command.extend(
        [
            "--decision-targets-input-path",
            _decision_targets_path_from_state(state, session_date, paths),
            "--trigger-mode",
            args.executor_trigger_mode,
            "--target-ny-time",
            args.target_ny_time,
            "--output-root",
            paths.execute_output_root,
        ]
    )
    if args.cancel_open_orders_before_submit:
        command.append("--cancel-open-orders-before-submit")
    if args.no_submit:
        command.append("--no-submit")
    return command


def _append_optional_float(command: list[str | Path], flag: str, value: float | None) -> None:
    if value is not None:
        command.extend([flag, _num(value)])


def _num(value: float | int) -> str:
    return f"{float(value):g}"


def _task_can_attempt(task_state: dict[str, Any], args: argparse.Namespace, now_cn: datetime, *, task: str) -> bool:
    can_attempt, _reason = _task_can_attempt_reason(task_state, args, now_cn, task=task)
    return can_attempt


def _task_can_attempt_reason(
    task_state: dict[str, Any],
    args: argparse.Namespace,
    now_cn: datetime,
    *,
    task: str,
) -> tuple[bool, str]:
    if args.force:
        return True, "force"
    if str(task_state.get("status") or "") == "completed":
        return False, "already_completed"
    attempts = int(task_state.get("attempts") or 0)
    max_attempts = int(args.max_execute_attempts) if str(task) == "execute" else int(args.max_attempts_per_task)
    if attempts >= max_attempts:
        return False, "max_attempts_reached"
    last_raw = task_state.get("finished_at_cn") or task_state.get("started_at_cn")
    if last_raw:
        last_at = _parse_datetime(str(last_raw))
        retry_after_minutes = (
            float(args.execute_retry_failed_after_minutes)
            if str(task) == "execute"
            else float(args.retry_failed_after_minutes)
        )
        if last_at and (now_cn - last_at).total_seconds() < retry_after_minutes * 60:
            return False, "retry_window_not_elapsed"
    return True, "eligible"


def _generate_execution_quality(execute_output_root: Path) -> None:
    """Generate execution_quality.json for a completed execute run (best-effort).

    Imports the analyzer lazily so a problem here never blocks the trading loop.
    """
    try:
        tools_dir = Path(__file__).resolve().parent
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from execution_quality import write_quality_file  # noqa: WPS433

        result = write_quality_file(Path(execute_output_root))
        counts = result.get("counts", {}) if isinstance(result, dict) else {}
        slip = result.get("slippage_bps", {}) if isinstance(result, dict) else {}
        print(
            f"[Scheduler] execution quality: "
            f"fill={result.get('fill_rate_count', 0) * 100:.1f}% "
            f"({counts.get('filled', 0)}/{counts.get('total_orders', 0)}), "
            f"slippage_nw={slip.get('avg_notional_weighted')}bps, "
            f"unfilled=${result.get('unfilled_notional', 0):,.0f}",
            flush=True,
        )
    except Exception as exc:  # never let analytics break the daemon
        print(f"[Scheduler] warning: execution-quality generation failed: {exc}", flush=True)


def _generate_daily_audit(execute_output_root: Path, decision_output_root: Path | None = None) -> None:
    """Generate the post-trade audit package for a completed execute run (best-effort).

    This is intentionally downstream analytics only.  It must never block the
    live scheduler from recording a successful trading run.
    """
    try:
        tools_dir = Path(__file__).resolve().parent
        if str(tools_dir) not in sys.path:
            sys.path.insert(0, str(tools_dir))
        from daily_audit_report import generate_audit, generate_rollup  # noqa: WPS433

        result = generate_audit(Path(execute_output_root), Path(decision_output_root) if decision_output_root else None)
        rollup = generate_rollup(Path(execute_output_root).parent)
        print(
            f"[Scheduler] daily audit: {result.get('audit_dir')} "
            f"(decision_rows={result.get('decision_rows')}, "
            f"orders={result.get('order_rows')}, lots={result.get('lot_rows')}, "
            f"realized_rows={result.get('realized_pnl_rows')}, "
            f"manifest_files={result.get('audit_manifest_files')}, "
            f"rollup_days={rollup.get('trading_day_count')})",
            flush=True,
        )
    except Exception as exc:  # never let analytics break the daemon
        print(f"[Scheduler] warning: daily-audit generation failed: {exc}", flush=True)


def _capture_task_outputs(task_state: dict[str, Any], task: str, paths: DayPaths) -> None:
    output_root = _task_output_root(task, paths)
    summary_path = output_root / "execution_summary.json"
    task_state["execution_summary_path"] = summary_path.as_posix()
    if not summary_path.exists():
        return
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    outputs = summary.get("outputs", {}) if isinstance(summary, dict) else {}
    if isinstance(outputs, dict) and outputs.get("decision_targets_csv"):
        task_state["decision_targets_path"] = str(outputs["decision_targets_csv"])


def _task_state(state: dict[str, Any], session_date: date, task: str) -> dict[str, Any]:
    sessions = state.setdefault("sessions", {})
    session = sessions.setdefault(session_date.isoformat(), {})
    return session.setdefault(task, {})


def _decision_targets_path_from_state(state: dict[str, Any], session_date: date, paths: DayPaths) -> Path:
    sessions = state.get("sessions", {})
    session = sessions.get(session_date.isoformat(), {}) if isinstance(sessions, dict) else {}
    decision = session.get("decision", {}) if isinstance(session, dict) else {}
    raw = decision.get("decision_targets_path") if isinstance(decision, dict) else None
    return Path(str(raw)).resolve() if raw else paths.decision_targets_path


def _task_logs(task: str, paths: DayPaths) -> tuple[Path, Path]:
    if task == "decision":
        return paths.decision_stdout_log, paths.decision_stderr_log
    return paths.execute_stdout_log, paths.execute_stderr_log


def _task_output_root(task: str, paths: DayPaths) -> Path:
    return paths.decision_output_root if task == "decision" else paths.execute_output_root


def _day_paths(args: argparse.Namespace, session_date: date) -> DayPaths:
    session_key = session_date.strftime("%Y%m%d")
    output_root = Path(args.output_root)
    logs_root = output_root / "logs"
    decision_output_root = output_root / f"{session_key}_decision"
    execute_output_root = output_root / f"{session_key}_execute"
    return DayPaths(
        session_key=session_key,
        decision_output_root=decision_output_root,
        execute_output_root=execute_output_root,
        decision_targets_path=decision_output_root / "decision_targets.csv",
        decision_stdout_log=logs_root / f"{session_key}_decision.out.log",
        decision_stderr_log=logs_root / f"{session_key}_decision.err.log",
        execute_stdout_log=logs_root / f"{session_key}_execute.out.log",
        execute_stderr_log=logs_root / f"{session_key}_execute.err.log",
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sessions": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"state file must be a JSON object: {path}")
    payload.setdefault("version", 1)
    payload.setdefault("sessions", {})
    return payload


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _parse_hhmm(raw: str) -> dt_time:
    parts = str(raw).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM time: {raw}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid HH:MM time: {raw}")
    return dt_time(hour, minute)


def _parse_datetime(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _iso_now_cn() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
