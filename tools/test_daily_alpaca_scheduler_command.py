"""Regression checks for production scheduler-to-executor arguments."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.daily_alpaca_scheduler import DayPaths, _build_command, parse_args  # noqa: E402


def main() -> int:
    args = parse_args([])
    args.python_executable = Path("python.exe")
    args.executor_path = Path("src/alpaca_executor.py")
    args.accounts_json_path = Path(
        "configs/alpaca_acounts/alpaca_accounts.local.json"
    )
    paths = DayPaths(
        session_key="20260728",
        decision_output_root=Path("decision"),
        execute_output_root=Path("execute"),
        decision_targets_path=Path("decision/decision_targets.csv"),
        decision_stdout_log=Path("decision.out.log"),
        decision_stderr_log=Path("decision.err.log"),
        execute_stdout_log=Path("execute.out.log"),
        execute_stderr_log=Path("execute.err.log"),
    )
    command = [
        str(value)
        for value in _build_command(
            args,
            date(2026, 7, 28),
            "execute",
            paths,
            {},
        )
    ]
    expected_values = {
        "--execution-quote-provider": "longbridge",
        "--longbridge-max-quote-age-seconds": "5",
        "--staged-entry-repair-rounds": "1",
        "--staged-entry-repair-max-attempts": "1",
        "--staged-entry-repair-wait-seconds": "10",
    }
    for flag, expected in expected_values.items():
        actual = command[command.index(flag) + 1]
        assert actual == expected, (flag, actual, expected)

    print(
        "[PASS] scheduler command carries Longbridge freshness and "
        "entry-repair defaults"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
