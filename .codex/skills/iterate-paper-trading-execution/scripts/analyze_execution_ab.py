#!/usr/bin/env python3
"""Compare canonical paper-trading execution runs and apply quality gates."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping


EPS = 1e-10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="LABEL=RUN_DIR",
        help="Canonical run directory. Repeat with baseline first, then candidates.",
    )
    parser.add_argument("--output", required=True, help="Output directory for JSON and Markdown reports.")
    parser.add_argument("--min-fill-rate", type=float, default=1.0)
    parser.add_argument("--max-submit-errors", type=int, default=0)
    parser.add_argument("--max-terminal-unfilled", type=int, default=0)
    parser.add_argument("--max-rate-limits", type=int, default=0)
    parser.add_argument("--max-server-errors", type=int, default=0)
    parser.add_argument("--max-executable-l1-regression", type=float, default=0.0005)
    parser.add_argument("--gross-target", type=float, default=0.95)
    parser.add_argument("--gross-tolerance", type=float, default=0.005)
    return parser


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.stat().st_size <= 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    low = int(math.floor(rank))
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def _stats(values: Iterable[float]) -> dict[str, float | None]:
    cleaned = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "mean": statistics.mean(cleaned) if cleaned else None,
        "median": statistics.median(cleaned) if cleaned else None,
        "p95": _percentile(cleaned, 0.95),
        "max": max(cleaned) if cleaned else None,
    }


def _logical_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    stage = str(record.get("stage") or "single_pass")
    if stage in {"entry", "entry_repair"}:
        stage = "entry"
    return (
        str(record.get("symbol") or "").upper(),
        str(record.get("side") or "").lower(),
        stage,
    )


def _logical_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []
    for record in records:
        key = _logical_key(record)
        if key not in latest:
            order.append(key)
        previous = latest.get(key)
        if previous is None:
            latest[key] = record
            continue
        previous_filled = _number(previous.get("filled_qty")) or 0.0
        current_filled = _number(record.get("filled_qty")) or 0.0
        if current_filled > previous_filled + EPS or str(record.get("status_latest")) == "filled":
            latest[key] = record
    return [latest[key] for key in order]


def _is_filled(record: Mapping[str, Any]) -> bool:
    status = str(record.get("status_latest") or "").lower()
    remaining = _number(record.get("remaining_qty"))
    requested = _number(record.get("qty")) or _number(record.get("requested_qty"))
    filled = _number(record.get("filled_qty")) or 0.0
    if status == "filled":
        return True
    if remaining is not None:
        return remaining <= EPS and filled > EPS
    return requested is not None and requested > EPS and filled >= requested - EPS


def _phase_seconds(timings: Mapping[str, Any], phase_name: str) -> float | None:
    phases = timings.get("phases") if isinstance(timings.get("phases"), list) else []
    for phase in phases:
        if isinstance(phase, dict) and phase.get("phase") == phase_name:
            return _number(phase.get("elapsed_seconds"))
    return None


def _position_values(rows: Any) -> tuple[dict[str, float], float]:
    signed_notional: dict[str, float] = {}
    gross = 0.0
    if not isinstance(rows, list):
        return signed_notional, gross
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        market_value = _number(row.get("market_value"))
        if market_value is None:
            qty = _number(row.get("qty")) or 0.0
            price = _number(row.get("current_price")) or 0.0
            market_value = abs(qty * price)
        side = str(row.get("side") or "").lower()
        signed = -abs(market_value) if side == "short" else abs(market_value)
        if symbol:
            signed_notional[symbol] = signed
        gross += abs(market_value)
    return signed_notional, gross


def _weight_l1(targets: Any, actual_notional: Mapping[str, float], equity: float | None) -> float | None:
    if not isinstance(targets, dict) or equity is None or equity <= EPS:
        return None
    symbols = set(str(symbol).upper() for symbol in targets) | set(actual_notional)
    return sum(
        abs(
            float(_number(targets.get(symbol)) or 0.0)
            - float(actual_notional.get(symbol, 0.0)) / equity
        )
        for symbol in symbols
    )


def _nested_number(payload: Mapping[str, Any], paths: list[tuple[str, ...]]) -> float | None:
    for path in paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        result = _number(value)
        if result is not None:
            return result
    return None


def _batch_totals(payload: Any) -> tuple[float, float]:
    elapsed = 0.0
    work = 0.0
    if isinstance(payload, dict):
        batch_elapsed = _number(payload.get("elapsed_seconds"))
        batch_work = _number(payload.get("aggregate_order_work_seconds"))
        if batch_elapsed is not None and batch_work is not None:
            return batch_elapsed, batch_work
        for value in payload.values():
            child_elapsed, child_work = _batch_totals(value)
            elapsed += child_elapsed
            work += child_work
    elif isinstance(payload, list):
        for value in payload:
            child_elapsed, child_work = _batch_totals(value)
            elapsed += child_elapsed
            work += child_work
    return elapsed, work


def _run_metrics(label: str, run_dir: Path) -> dict[str, Any]:
    records_value = _read_json(run_dir / "execution_records.json", [])
    records = [dict(row) for row in records_value if isinstance(row, dict)] if isinstance(records_value, list) else []
    logical = _logical_records(records)
    timings = _read_json(run_dir / "decision_phase_timings.json", {})
    summary = _read_json(run_dir / "execution_summary.json", {})
    account_after = _read_json(run_dir / "broker_account_after.json", {})
    positions_after = _read_json(run_dir / "broker_positions_after_raw.json", [])
    api_path = run_dir / "broker_api_audit.jsonl"
    if not api_path.exists():
        api_path = run_dir / "alpaca_api_audit.jsonl"
    api_rows = _read_jsonl(api_path)

    queue_seconds = [(value or 0.0) / 1000.0 for value in (_number(row.get("queue_wait_ms")) for row in records)]
    order_work = sum(_number(row.get("order_wall_time_seconds")) or 0.0 for row in records)
    parallel_order_wall, batch_work = _batch_totals(summary.get("staged_diagnostics"))
    if batch_work > EPS:
        order_work = batch_work
    execution_seconds = _phase_seconds(timings, "order_submission_and_tracking")
    if execution_seconds is None:
        execution_seconds = _nested_number(summary, [("order_submission_seconds",), ("execution_elapsed_seconds",)])

    requested_workers = max((int(_number(row.get("batch_requested_workers")) or 0) for row in records), default=0)
    effective_workers = max((int(_number(row.get("batch_effective_workers")) or 0) for row in records), default=0)
    worker_cap = max((int(_number(row.get("batch_worker_safety_cap")) or 0) for row in records), default=0)
    submit_errors = sum(str(row.get("status_latest") or "").lower() == "submit_error" for row in logical)
    terminal_unfilled = sum(not _is_filled(row) for row in logical)
    filled_count = len(logical) - terminal_unfilled
    fill_rate = filled_count / len(logical) if logical else 0.0

    status_codes = [int(code) for code in (_number(row.get("status_code")) for row in api_rows) if code is not None]
    rate_limits = sum(code == 429 for code in status_codes)
    server_errors = sum(500 <= code < 600 for code in status_codes)
    api_failures = sum(not bool(row.get("ok")) for row in api_rows)

    actual_notional, gross_notional = _position_values(positions_after)
    equity = _nested_number(account_after, [("equity",), ("portfolio_value",)])
    regt_remaining = _nested_number(account_after, [("regt_buying_power",), ("buying_power",)])
    gross_utilization = _nested_number(summary, [("gross_utilization",), ("actual_gross_utilization",)])
    stable_total_capacity = _nested_number(
        summary,
        [
            ("staged_diagnostics", "fresh_total_regt_capacity"),
            ("staged_diagnostics", "entry_projection", "total_buying_power_capacity"),
            ("executable_target_projection", "total_buying_power_capacity"),
        ],
    )
    if gross_utilization is None and stable_total_capacity is not None and stable_total_capacity > EPS:
        gross_utilization = gross_notional / stable_total_capacity
    if gross_utilization is None and regt_remaining is not None and gross_notional + regt_remaining > EPS:
        gross_utilization = gross_notional / (gross_notional + regt_remaining)

    executable_l1 = _nested_number(
        summary,
        [
            ("executable_to_actual_l1",),
            ("alignment_after_execution", "abs_weight_diff_sum"),
        ],
    )
    if executable_l1 is None:
        executable_l1 = _weight_l1(summary.get("executable_expected_signed_weights"), actual_notional, equity)
    strategy_l1 = _nested_number(summary, [("strategy_to_actual_l1",)])
    if strategy_l1 is None:
        strategy_l1 = _weight_l1(summary.get("raw_target_signed_weights"), actual_notional, equity)

    return {
        "label": label,
        "run_dir": run_dir.as_posix(),
        "evidence": {
            "execution_records": bool(records),
            "phase_timings": bool(timings),
            "execution_summary": bool(summary),
            "api_audit": bool(api_rows),
            "positions_after": bool(positions_after),
            "account_after": bool(account_after),
        },
        "execution_seconds": execution_seconds,
        "aggregate_order_work_seconds": order_work,
        "parallel_order_wall_seconds": parallel_order_wall or None,
        "parallel_speedup_ratio": order_work / parallel_order_wall if parallel_order_wall > EPS else None,
        "queue_wait_seconds": _stats(queue_seconds),
        "requested_workers": requested_workers,
        "worker_safety_cap": worker_cap or None,
        "effective_workers": effective_workers,
        "logical_orders": len(logical),
        "filled_logical_orders": filled_count,
        "logical_fill_rate": fill_rate,
        "attempt_count": sum(int(_number(row.get("attempt_count")) or len(row.get("attempts") or [])) for row in records),
        "requote_cancel_count": sum(
            str(attempt.get("cancel_reason") or "") == "requote_wait_elapsed"
            for row in records
            for attempt in (row.get("attempts") if isinstance(row.get("attempts"), list) else [])
            if isinstance(attempt, dict)
        ),
        "submit_errors": submit_errors,
        "terminal_unfilled": terminal_unfilled,
        "api_calls": len(api_rows),
        "api_failures": api_failures,
        "rate_limit_responses": rate_limits,
        "server_error_responses": server_errors,
        "api_latency_ms": _stats([_number(row.get("elapsed_ms")) or 0.0 for row in api_rows]),
        "strategy_to_actual_l1": strategy_l1,
        "executable_to_actual_l1": executable_l1,
        "gross_utilization": gross_utilization,
    }


def _gate_run(metrics: Mapping[str, Any], baseline: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, actual: Any, limit: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "actual": actual, "limit": limit})

    check("logical_fill_rate", float(metrics["logical_fill_rate"]) >= args.min_fill_rate - EPS, metrics["logical_fill_rate"], f">={args.min_fill_rate}")
    check("submit_errors", int(metrics["submit_errors"]) <= args.max_submit_errors, metrics["submit_errors"], f"<={args.max_submit_errors}")
    check("terminal_unfilled", int(metrics["terminal_unfilled"]) <= args.max_terminal_unfilled, metrics["terminal_unfilled"], f"<={args.max_terminal_unfilled}")
    check("rate_limits", int(metrics["rate_limit_responses"]) <= args.max_rate_limits, metrics["rate_limit_responses"], f"<={args.max_rate_limits}")
    check("server_errors", int(metrics["server_error_responses"]) <= args.max_server_errors, metrics["server_error_responses"], f"<={args.max_server_errors}")

    l1 = _number(metrics.get("executable_to_actual_l1"))
    baseline_l1 = _number(baseline.get("executable_to_actual_l1"))
    if l1 is not None and baseline_l1 is not None:
        check("executable_l1_regression", l1 <= baseline_l1 + args.max_executable_l1_regression + EPS, l1, f"<={baseline_l1 + args.max_executable_l1_regression:.8f}")

    gross = _number(metrics.get("gross_utilization"))
    if gross is not None:
        check("gross_utilization", abs(gross - args.gross_target) <= args.gross_tolerance + EPS, gross, f"{args.gross_target}+/-{args.gross_tolerance}")

    return {
        "status": "pass" if all(item["passed"] for item in checks) else "fail",
        "checks": checks,
        "failed_checks": [item["name"] for item in checks if not item["passed"]],
    }


def _comparison(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    base_seconds = _number(baseline.get("execution_seconds"))
    candidate_seconds = _number(candidate.get("execution_seconds"))
    speed_change = None
    speed_pct = None
    if base_seconds is not None and candidate_seconds is not None:
        speed_change = candidate_seconds - base_seconds
        speed_pct = 100.0 * (base_seconds - candidate_seconds) / base_seconds if base_seconds > EPS else None
    return {
        "baseline": baseline.get("label"),
        "candidate": candidate.get("label"),
        "execution_seconds_change": speed_change,
        "execution_improvement_pct": speed_pct,
        "fill_rate_change": float(candidate["logical_fill_rate"]) - float(baseline["logical_fill_rate"]),
        "executable_l1_change": (
            float(candidate["executable_to_actual_l1"]) - float(baseline["executable_to_actual_l1"])
            if candidate.get("executable_to_actual_l1") is not None and baseline.get("executable_to_actual_l1") is not None
            else None
        ),
        "gross_utilization_change": (
            float(candidate["gross_utilization"]) - float(baseline["gross_utilization"])
            if candidate.get("gross_utilization") is not None and baseline.get("gross_utilization") is not None
            else None
        ),
    }


def _format(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Execution A/B Report",
        "",
        "| Run | Gate | Exec s | Fill rate | Submit errors | Unfilled | 429 | Effective workers | Queue P95 s | Executable L1 | Gross util |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    gates = {str(item["label"]): item["gate"] for item in report["runs"]}
    for run in report["runs"]:
        queue = run.get("queue_wait_seconds") or {}
        lines.append(
            "| " + " | ".join(
                [
                    str(run["label"]),
                    str(gates[str(run["label"])]["status"]),
                    _format(run.get("execution_seconds"), 3),
                    _format(run.get("logical_fill_rate"), 6),
                    str(run.get("submit_errors")),
                    str(run.get("terminal_unfilled")),
                    str(run.get("rate_limit_responses")),
                    str(run.get("effective_workers")),
                    _format(queue.get("p95"), 3),
                    _format(run.get("executable_to_actual_l1"), 8),
                    _format(run.get("gross_utilization"), 6),
                ]
            ) + " |"
        )
    lines.extend(["", "## Comparisons", ""])
    for item in report["comparisons"]:
        lines.append(
            f"- **{item['candidate']} vs {item['baseline']}**: "
            f"execution improvement {_format(item.get('execution_improvement_pct'), 2)}%, "
            f"fill-rate change {_format(item.get('fill_rate_change'), 6)}, "
            f"executable L1 change {_format(item.get('executable_l1_change'), 8)}."
        )
    lines.extend(["", "## Failed Gates", ""])
    any_failed = False
    for run in report["runs"]:
        failed = run["gate"]["failed_checks"]
        if failed:
            any_failed = True
            lines.append(f"- **{run['label']}**: {', '.join(failed)}")
    if not any_failed:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parser().parse_args()
    parsed_runs: list[tuple[str, Path]] = []
    for spec in args.run:
        if "=" not in spec:
            raise SystemExit(f"Invalid --run {spec!r}; expected LABEL=RUN_DIR")
        label, raw_path = spec.split("=", 1)
        run_dir = Path(raw_path).expanduser().resolve()
        if not label.strip() or not run_dir.is_dir():
            raise SystemExit(f"Invalid --run {spec!r}; directory must exist")
        parsed_runs.append((label.strip(), run_dir))

    metrics = [_run_metrics(label, path) for label, path in parsed_runs]
    baseline = metrics[0]
    for run in metrics:
        run["gate"] = _gate_run(run, baseline, args)
    report = {
        "schema_version": "1.0",
        "artifact_type": "execution_ab_report",
        "baseline": baseline["label"],
        "gate_config": {
            "min_fill_rate": args.min_fill_rate,
            "max_submit_errors": args.max_submit_errors,
            "max_terminal_unfilled": args.max_terminal_unfilled,
            "max_rate_limits": args.max_rate_limits,
            "max_server_errors": args.max_server_errors,
            "max_executable_l1_regression": args.max_executable_l1_regression,
            "gross_target": args.gross_target,
            "gross_tolerance": args.gross_tolerance,
        },
        "runs": metrics,
        "comparisons": [_comparison(baseline, run) for run in metrics[1:]],
    }

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "execution_ab_report.json"
    markdown_path = output_dir / "execution_ab_report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": json_path.as_posix(),
        "markdown": markdown_path.as_posix(),
        "runs": [{"label": run["label"], "gate": run["gate"]["status"]} for run in metrics],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
