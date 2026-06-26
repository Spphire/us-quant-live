from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


def _resolve_project_root() -> Path:
    """Locate project root by walking upward until src/alpha_core.py exists."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "src" / "alpha_core.py").exists():
            return candidate
    # Fallback for unexpected layouts.
    return here.parent.parent


PROJECT_ROOT = _resolve_project_root()
DEFAULT_ALPHA_CORE_PATH = PROJECT_ROOT / "src" / "alpha_core.py"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "artifacts" / "alpha_core_backfill_until_invalid"


@dataclass
class RunRecord:
    step: int
    run_date: str
    status: str
    reason: str
    return_code: int
    rows_output: int
    composite_coverage: float
    output_path: str
    log_path: str


def _parse_start_date(raw: str) -> date:
    token = str(raw).strip()
    if not token:
        raise ValueError("start date is empty")

    # ISO date: YYYY-MM-DD
    if len(token) == 10 and token[4] == "-" and token[7] == "-":
        return date.fromisoformat(token)

    # Month-day shorthand: 5-11 or 05-11, resolved to current year.
    parts = token.split("-")
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        year = date.today().year
        month = int(parts[0])
        day = int(parts[1])
        return date(year, month, day)

    raise ValueError(f"Unsupported start date format: {raw!r}. Use YYYY-MM-DD or M-D.")


def _previous_weekday(current: date) -> date:
    candidate = current - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _extract_last_json_blob(text: str) -> dict[str, Any] | None:
    lines = text.splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].lstrip().startswith("{"):
            candidate = "\n".join(lines[idx:]).strip()
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def _is_valid_summary(summary: dict[str, Any] | None) -> tuple[bool, str, int, float, str]:
    if summary is None:
        return False, "summary_json_not_found", 0, 0.0, ""
    if not bool(summary.get("ok", False)):
        return False, str(summary.get("error") or "ok_false"), 0, 0.0, ""

    rows_output = int(summary.get("rows_output", 0) or 0)
    if rows_output <= 0:
        return False, "rows_output<=0", rows_output, 0.0, str(summary.get("output_path") or "")

    coverage = summary.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    composite = float(coverage.get("composite_score_non_null_rate", 0.0) or 0.0)
    if composite <= 0.0:
        return False, "composite_score_coverage<=0", rows_output, composite, str(summary.get("output_path") or "")

    return True, "ok", rows_output, composite, str(summary.get("output_path") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Run alpha_core serially backward from a start date over weekdays, "
            "and stop once API/data becomes invalid."
        ),
    )
    parser.add_argument(
        "--start-date",
        default="05-11",
        help="Start date (YYYY-MM-DD or M-D / MM-DD). Default: 05-11 of current year.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=400,
        help="Maximum number of backward runs before forced stop.",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Project root for running alpha_core.",
    )
    parser.add_argument(
        "--alpha-core-path",
        default=str(DEFAULT_ALPHA_CORE_PATH),
        help="Path to alpha_core.py.",
    )
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Python executable used to run alpha_core.",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Output root for logs and summary.",
    )

    # Any unknown args are forwarded to alpha_core.
    args, passthrough = parser.parse_known_args(argv)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    if int(args.max_steps) <= 0:
        raise ValueError("--max-steps must be > 0")

    project_root = Path(args.project_root).resolve()
    alpha_core_path = Path(args.alpha_core_path).resolve()
    python_executable = str(args.python_executable)
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    logs_root = output_root / "logs"
    logs_root.mkdir(parents=True, exist_ok=True)

    start = _parse_start_date(str(args.start_date))
    run_date = start

    print(
        f"[Runner] start={start.isoformat()} max_steps={int(args.max_steps)} "
        f"alpha_core={alpha_core_path.as_posix()}",
        flush=True,
    )
    if passthrough:
        print(f"[Runner] pass-through args: {passthrough}", flush=True)

    records: list[RunRecord] = []
    stop_reason = "max_steps_reached"
    stop_date = ""

    for step in range(1, int(args.max_steps) + 1):
        run_date_str = run_date.isoformat()
        cmd = [
            python_executable,
            str(alpha_core_path),
            "--date",
            run_date_str,
            *passthrough,
        ]
        print(f"[Runner] step={step} date={run_date_str} running ...", flush=True)

        completed = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_text = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")

        log_path = logs_root / f"alpha_core_{run_date.strftime('%Y%m%d')}.log"
        log_path.write_text(output_text, encoding="utf-8")

        summary = _extract_last_json_blob(output_text)
        valid, reason, rows_output, composite_coverage, output_path = _is_valid_summary(summary)

        if completed.returncode != 0 and reason == "ok":
            valid = False
            reason = f"alpha_core_exit_code_{completed.returncode}"
        elif completed.returncode != 0 and reason != "ok":
            reason = f"{reason};exit_code={completed.returncode}"

        status = "ok" if valid else "stop"
        record = RunRecord(
            step=step,
            run_date=run_date_str,
            status=status,
            reason=reason,
            return_code=int(completed.returncode),
            rows_output=int(rows_output),
            composite_coverage=float(composite_coverage),
            output_path=output_path,
            log_path=log_path.as_posix(),
        )
        records.append(record)

        if valid:
            print(
                f"[Runner] step={step} date={run_date_str} ok "
                f"rows={rows_output} composite_cov={composite_coverage:.4f}",
                flush=True,
            )
            run_date = _previous_weekday(run_date)
            continue

        stop_reason = reason
        stop_date = run_date_str
        print(
            f"[Runner] stop at date={run_date_str} reason={reason} "
            f"(return_code={completed.returncode})",
            flush=True,
        )
        break

    summary_payload = {
        "ok": True,
        "started_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "start_date": start.isoformat(),
        "stop_date": stop_date or None,
        "stop_reason": stop_reason,
        "steps_executed": len(records),
        "project_root": project_root.as_posix(),
        "alpha_core_path": alpha_core_path.as_posix(),
        "python_executable": python_executable,
        "pass_through_args": passthrough,
        "records": [record.__dict__ for record in records],
    }

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    summary_path = output_root / f"run_summary_{stamp}.json"
    latest_path = output_root / "latest_run_summary.json"
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary_payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
