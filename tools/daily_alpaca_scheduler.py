from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Sequence
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
            "Keep an Alpaca daily workflow online: run DecisionEngine at 12:00 Beijing "
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

    parser.add_argument("--decision-time-cn", default="12:00")
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
    parser.add_argument("--sizing-adverse-offset-bps", type=float, default=None)
    parser.add_argument("--short-buying-power-adverse-offset-bps", type=float, default=300.0)
    parser.add_argument("--buying-power-buffer", type=float, default=0.88)
    parser.add_argument("--order-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--order-poll-seconds", type=float, default=2.0)
    parser.add_argument("--staged-release-timeout-seconds", type=float, default=None)
    parser.add_argument("--staged-entry-timeout-seconds", type=float, default=None)
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
    parser.add_argument("--dashboard-port", type=int, default=8766, help="Dashboard server port")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    _resolve_config_paths(args)
    state = _load_state(args.state_path)
    calendar_cache: dict[str, bool] = {}

    # Start dashboard server as a child process (best-effort, non-blocking)
    dashboard_proc = _start_dashboard_server(args)

    if args.run_once:
        now_cn = datetime.now(CN_TZ)
        ok = _run_once(args=args, state=state, calendar_cache=calendar_cache, now_cn=now_cn)
        if dashboard_proc is not None:
            dashboard_proc.terminate()
        return 0 if ok else 1

    print(
        "[Scheduler] online. "
        f"decision={args.decision_time_cn} CN, execute={args.execute_time_cn} CN, "
        f"state={args.state_path}",
        flush=True,
    )
    last_heartbeat_at: datetime | None = None
    try:
        while True:
            now_cn = datetime.now(CN_TZ)
            try:
                _run_due_tasks(args=args, state=state, calendar_cache=calendar_cache, now_cn=now_cn)
            except KeyboardInterrupt:
                print("[Scheduler] stopped by keyboard interrupt.", flush=True)
                if dashboard_proc is not None:
                    dashboard_proc.terminate()
                return 130
            except Exception as exc:  # Keep the daemon alive after transient API/process errors.
                print(f"[Scheduler] warning: loop error: {exc}", flush=True)

            # Restart dashboard if it died
            if dashboard_proc is not None and dashboard_proc.poll() is not None:
                print("[Scheduler] dashboard server exited, restarting...", flush=True)
                dashboard_proc = _start_dashboard_server(args)

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
                last_heartbeat_at = now_cn

            time.sleep(max(float(args.poll_seconds), 1.0))
    finally:
        if dashboard_proc is not None:
            try:
                dashboard_proc.terminate()
                dashboard_proc.wait(timeout=5)
            except Exception:
                pass


def _resolve_config_paths(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    args.project_root = project_root
    args.executor_path = _resolve_path(args.executor_path, project_root) if args.executor_path else project_root / "src" / "alpaca_executor.py"
    args.output_root = _resolve_path(args.output_root, project_root)
    args.state_path = _resolve_path(args.state_path, project_root)
    args.accounts_json_path = _resolve_path(args.accounts_json_path, project_root)


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
    if not _session_is_tradable(args, session_date, calendar_cache):
        return True

    decision_time = _parse_hhmm(args.decision_time_cn)
    execute_time = _parse_hhmm(args.execute_time_cn)
    paths = _day_paths(args, session_date)
    ran_ok = True

    if now_cn.time() >= decision_time:
        ran_ok = _run_task(args=args, state=state, session_date=session_date, task="decision", paths=paths, now_cn=now_cn) and ran_ok

    if now_cn.time() >= execute_time:
        target_path = _decision_targets_path_from_state(state, session_date, paths)
        if args.dry_run or target_path.exists():
            ran_ok = _run_task(args=args, state=state, session_date=session_date, task="execute", paths=paths, now_cn=now_cn) and ran_ok
        else:
            print(
                f"[Scheduler] execute waiting for decision targets: {target_path}",
                flush=True,
            )

    return ran_ok


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

    if args.dry_run:
        print(f"[Scheduler][dry-run][{task}] {command_text}", flush=True)
        return True

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    paths.decision_output_root.mkdir(parents=True, exist_ok=True)
    paths.execute_output_root.mkdir(parents=True, exist_ok=True)

    previous_attempts = int(task_state.get("attempts") or 0)
    task_state.clear()
    task_state.update(
        {
            "status": "started",
            "attempts": previous_attempts + 1,
            "started_at_cn": _iso_now_cn(),
            "command": command_text,
            "stdout_log": stdout_log.as_posix(),
            "stderr_log": stderr_log.as_posix(),
        }
    )
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
    else:
        task_state["status"] = "failed"

    _save_state(args.state_path, state)
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
        "--buying-power-buffer",
        _num(args.buying_power_buffer),
        "--order-timeout-seconds",
        _num(args.order_timeout_seconds),
        "--order-poll-seconds",
        _num(args.order_poll_seconds),
    ]
    _append_optional_float(command, "--marketable-limit-base-offset-bps", args.marketable_limit_base_offset_bps)
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
    if args.force:
        return True
    if str(task_state.get("status") or "") == "completed":
        return False
    attempts = int(task_state.get("attempts") or 0)
    max_attempts = int(args.max_execute_attempts) if str(task) == "execute" else int(args.max_attempts_per_task)
    if attempts >= max_attempts:
        return False
    last_raw = task_state.get("finished_at_cn") or task_state.get("started_at_cn")
    if last_raw:
        last_at = _parse_datetime(str(last_raw))
        retry_after_minutes = (
            float(args.execute_retry_failed_after_minutes)
            if str(task) == "execute"
            else float(args.retry_failed_after_minutes)
        )
        if last_at and (now_cn - last_at).total_seconds() < retry_after_minutes * 60:
            return False
    return True


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


if __name__ == "__main__":
    raise SystemExit(main())
