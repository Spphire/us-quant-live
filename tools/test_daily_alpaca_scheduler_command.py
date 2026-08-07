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
        prepare_output_root=Path("prepare"),
        decision_output_root=Path("decision"),
        execute_output_root=Path("execute"),
        alpha_panel_path=Path("prepare/alpha_core_panel_20260728.csv"),
        decision_targets_path=Path("decision/decision_targets.csv"),
        prepare_stdout_log=Path("prepare.out.log"),
        prepare_stderr_log=Path("prepare.err.log"),
        decision_stdout_log=Path("decision.out.log"),
        decision_stderr_log=Path("decision.err.log"),
        execute_stdout_log=Path("execute.out.log"),
        execute_stderr_log=Path("execute.err.log"),
    )
    state: dict = {}
    commands = {
        task: [
            str(value)
            for value in _build_command(
                args,
                date(2026, 7, 28),
                task,
                paths,
                state,
            )
        ]
        for task in ("prepare", "decision", "execute")
    }
    command = commands["execute"]
    expected_values = {
        "--execution-quote-provider": "longbridge",
        "--longbridge-max-quote-age-seconds": "10",
        "--longbridge-snapshot-contexts": "4",
        "--staged-entry-repair-rounds": "1",
        "--staged-entry-repair-max-attempts": "1",
        "--staged-entry-repair-wait-seconds": "10",
    }
    for flag, expected in expected_values.items():
        actual = command[command.index(flag) + 1]
        assert actual == expected, (flag, actual, expected)

    prepare = commands["prepare"]
    assert "--alpha-panel-input-path" not in prepare, prepare
    assert prepare[prepare.index("--trigger-mode") + 1] == "plan_only", prepare
    assert "--no-submit" in prepare, prepare
    assert prepare[prepare.index("--output-root") + 1] == "prepare", prepare

    decision = commands["decision"]
    assert decision[decision.index("--alpha-panel-input-path") + 1].endswith(
        "prepare\\alpha_core_panel_20260728.csv"
    ), decision
    assert decision[decision.index("--position-continuity-reference-path") + 1].endswith(
        "prepare\\broker_positions_after_raw.json"
    ), decision
    assert decision[decision.index("--position-continuity-mode") + 1] == "rebalance", decision
    assert "--no-submit" in decision, decision

    assert command[command.index("--alpha-panel-input-path") + 1].endswith(
        "prepare\\alpha_core_panel_20260728.csv"
    ), command
    assert "--decision-targets-input-path" not in command, command
    assert command[command.index("--position-continuity-reference-path") + 1].endswith(
        "prepare\\broker_positions_after_raw.json"
    ), command
    assert command[command.index("--position-continuity-mode") + 1] == "rebalance", command
    assert "--no-submit" not in command, command

    print(
        "[PASS] scheduler commands carry prepared-Alpha dependencies, fresh-decision "
        "rebalance guards, Longbridge freshness, and entry-repair defaults"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
