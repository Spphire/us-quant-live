"""Regression checks for cached-Alpha decisions and position continuity."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca_executor import (  # noqa: E402
    _build_position_continuity_guard,
    _latest_stability_payload,
    _load_cached_alpha_panel,
)
from decision_engine import DecisionConfig, DecisionEngine  # noqa: E402
import tools.daily_alpaca_scheduler as scheduler  # noqa: E402
from tools.daily_alpaca_scheduler import (  # noqa: E402
    CN_TZ,
    _day_paths,
    _prepared_alpha_dependency_error,
    _run_task,
    _sha256_path,
    parse_args,
)


SESSION_DATE = date(2026, 8, 5)


def _position(symbol: str, qty: float, *, side: str = "long", price: float = 100.0) -> dict:
    return {
        "symbol": symbol,
        "qty": str(abs(qty)),
        "side": side,
        "current_price": str(price),
        "market_value": str(abs(qty) * price),
    }


def _stability(samples: list[list[dict]]) -> dict:
    return {
        "sample_count": len(samples),
        "samples": [
            {
                "sample_index": index,
                "positions_ok": True,
                "positions_payload": rows,
                "collected_at_utc": f"2026-08-05T13:00:0{index}+00:00",
            }
            for index, rows in enumerate(samples, start=1)
        ],
    }


def test_position_continuity_semantics() -> None:
    with TemporaryDirectory() as tmp:
        reference_path = Path(tmp) / "broker_positions_after_raw.json"
        reference_path.write_text(
            json.dumps(
                [
                    _position("AAA", 10, price=100),
                    _position("BBB", 7, side="short", price=50),
                ]
            ),
            encoding="utf-8",
        )
        current = [
            _position("AAA", 10, price=107),
            _position("BBB", 7, side="short", price=44),
        ]
        stable = _stability([current, current, current])
        passed = _build_position_continuity_guard(
            reference_path=reference_path,
            current_positions=current,
            current_stability=stable,
            mode="strict",
            qty_decimals=6,
        )
        assert passed["status"] == "pass", passed
        assert passed["drift_symbol_count"] == 0, passed

        changed = [
            _position("AAA", 9, price=107),
            _position("CCC", 2, price=20),
        ]
        changed_stability = _stability([changed, changed, changed])
        blocked = _build_position_continuity_guard(
            reference_path=reference_path,
            current_positions=changed,
            current_stability=changed_stability,
            mode="strict",
            qty_decimals=6,
        )
        assert blocked["status"] == "blocked", blocked
        assert set(blocked["drift_symbols"]) == {"AAA", "BBB", "CCC"}, blocked
        assert "cross_snapshot_position_quantity_drift" in blocked["blocking_reasons"], blocked

        audited = _build_position_continuity_guard(
            reference_path=reference_path,
            current_positions=changed,
            current_stability=changed_stability,
            mode="audit",
            qty_decimals=6,
        )
        assert audited["status"] == "attention", audited
        assert not audited["blocking_reasons"], audited

        unstable = _stability([current, changed, current])
        unstable_guard = _build_position_continuity_guard(
            reference_path=reference_path,
            current_positions=current,
            current_stability=unstable,
            mode="strict",
            qty_decimals=6,
        )
        assert unstable_guard["status"] == "blocked", unstable_guard
        assert "current_position_quantities_unstable" in unstable_guard["blocking_reasons"]

        missing = _build_position_continuity_guard(
            reference_path=Path(tmp) / "missing.json",
            current_positions=current,
            current_stability=stable,
            mode="strict",
            qty_decimals=6,
        )
        assert missing["status"] == "blocked", missing
        assert missing["reference_sha256"] is None, missing


def test_latest_successful_stability_payload() -> None:
    stability = {
        "samples": [
            {"positions_ok": True, "positions_payload": [{"symbol": "OLD"}]},
            {"positions_ok": False, "positions_payload": []},
            {"positions_ok": True, "positions_payload": [{"symbol": "NEW"}]},
        ]
    }
    latest = _latest_stability_payload(
        stability,
        payload_key="positions_payload",
        fallback=[],
    )
    assert latest == [{"symbol": "NEW"}], latest


def _alpha_rows() -> list[dict]:
    rows = []
    for index in range(8):
        rows.append(
            {
                "symbol": f"S{index:02d}",
                "session_date": SESSION_DATE.isoformat(),
                "beta": 0.7 + index * 0.05,
                "sic2_sector": f"SEC{index % 3}",
                "sic4_industry": f"IND{index % 4}",
                "reversal_score": float(index),
                "momentum_score": float(7 - index),
                "small_size_score": float(index % 5),
                "low_beta_score": float(8 - index),
                "cash_quality_score": float(index % 4),
            }
        )
    return rows


def test_cached_alpha_validation_and_decision_equivalence() -> None:
    with TemporaryDirectory() as tmp:
        alpha_path = Path(tmp) / "alpha.csv"
        original = pd.DataFrame(_alpha_rows())
        original.to_csv(alpha_path, index=False)
        loaded = _load_cached_alpha_panel(alpha_path, SESSION_DATE)

        config = DecisionConfig(
            candidate_pool_per_side=4,
            max_single_name_side_weight=0.6,
            min_nonzero_names=1,
            turnover_budget=2.0,
        )
        engine = DecisionEngine(config)
        direct = engine.decide(
            alpha_frame=original,
            previous_weights={"long": {}, "short": {}},
            session_idx=8,
            session_date=SESSION_DATE.isoformat(),
        )
        cached = engine.decide(
            alpha_frame=loaded,
            previous_weights={"long": {}, "short": {}},
            session_idx=8,
            session_date=SESSION_DATE.isoformat(),
        )
        assert direct.status == cached.status
        pd.testing.assert_frame_equal(
            direct.targets.sort_values(["side", "symbol"]).reset_index(drop=True),
            cached.targets.sort_values(["side", "symbol"]).reset_index(drop=True),
        )

        wrong_date = original.copy()
        wrong_date["session_date"] = "2026-08-04"
        wrong_date.to_csv(alpha_path, index=False)
        try:
            _load_cached_alpha_panel(alpha_path, SESSION_DATE)
        except ValueError as exc:
            assert "session_date does not match" in str(exc)
        else:
            raise AssertionError("wrong-session Alpha cache was accepted")

        invalid_symbols = original.copy()
        invalid_symbols.loc[1, "symbol"] = invalid_symbols.loc[0, "symbol"]
        invalid_symbols.to_csv(alpha_path, index=False)
        try:
            _load_cached_alpha_panel(alpha_path, SESSION_DATE)
        except ValueError as exc:
            assert "blank or duplicate" in str(exc)
        else:
            raise AssertionError("duplicate-symbol Alpha cache was accepted")

        blank_symbol = original.copy()
        blank_symbol.loc[0, "symbol"] = None
        blank_symbol.to_csv(alpha_path, index=False)
        try:
            _load_cached_alpha_panel(alpha_path, SESSION_DATE)
        except ValueError as exc:
            assert "blank or duplicate" in str(exc)
        else:
            raise AssertionError("blank-symbol Alpha cache was accepted")


def test_scheduler_rejects_modified_alpha_cache() -> None:
    with TemporaryDirectory() as tmp:
        args = parse_args([])
        args.output_root = Path(tmp)
        paths = _day_paths(args, SESSION_DATE)
        paths.alpha_panel_path.parent.mkdir(parents=True, exist_ok=True)
        paths.alpha_panel_path.write_text("symbol,session_date\nAAA,2026-08-05\n", encoding="utf-8")
        state = {
            "sessions": {
                SESSION_DATE.isoformat(): {
                    "prepare": {
                        "status": "completed",
                        "alpha_panel_path": paths.alpha_panel_path.as_posix(),
                        "alpha_panel_sha256": _sha256_path(paths.alpha_panel_path),
                    }
                }
            }
        }
        assert _prepared_alpha_dependency_error(
            state=state,
            session_date=SESSION_DATE,
            paths=paths,
        ) is None
        paths.alpha_panel_path.write_text("symbol,session_date\nBBB,2026-08-05\n", encoding="utf-8")
        error = _prepared_alpha_dependency_error(
            state=state,
            session_date=SESSION_DATE,
            paths=paths,
        )
        assert error and "hash changed" in error, error


def test_scheduler_waits_for_stage_dependencies() -> None:
    with TemporaryDirectory() as tmp:
        args = parse_args([])
        args.output_root = Path(tmp)
        args.dry_run = False
        paths = _day_paths(args, SESSION_DATE)
        state: dict = {"sessions": {}}
        now_cn = datetime(2026, 8, 5, 22, 0, tzinfo=CN_TZ)
        decision_ok = _run_task(
            args=args,
            state=state,
            session_date=SESSION_DATE,
            task="decision",
            paths=paths,
            now_cn=now_cn,
        )
        execute_ok = _run_task(
            args=args,
            state=state,
            session_date=SESSION_DATE,
            task="execute",
            paths=paths,
            now_cn=now_cn,
        )
        assert decision_ok is False
        assert execute_ok is False
        assert not paths.decision_output_root.exists()
        assert not paths.execute_output_root.exists()


def _command_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_scheduler_three_stage_lifecycle_and_retry_reference() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        args = parse_args([])
        args.project_root = root
        args.output_root = root / "scheduler"
        args.state_path = root / "scheduler" / "state.json"
        args.python_executable = Path("python.exe")
        args.executor_path = Path("executor.py")
        args.accounts_json_path = Path("accounts.json")
        args.dry_run = False
        args.force = False
        paths = _day_paths(args, SESSION_DATE)
        state: dict = {"version": 1, "sessions": {}}
        now_cn = datetime(2026, 8, 5, 22, 0, tzinfo=CN_TZ)
        calls: list[dict[str, str]] = []

        original_run = scheduler.subprocess.run
        original_quality = scheduler._generate_execution_quality
        original_audit = scheduler._generate_daily_audit
        original_finalize = scheduler._finalize_scheduler_run_evidence

        def fake_run(command, **_kwargs):
            argv = [str(value) for value in command]
            output_root = Path(_command_value(argv, "--output-root")).resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            if "--alpha-panel-input-path" in argv:
                task = "decision"
            elif "--decision-targets-input-path" in argv:
                task = "execute"
            else:
                task = "prepare"

            call = {"task": task, "output_root": output_root.as_posix()}
            if "--position-continuity-reference-path" in argv:
                call["position_reference"] = Path(
                    _command_value(argv, "--position-continuity-reference-path")
                ).resolve().as_posix()
                assert _command_value(argv, "--position-continuity-mode") == "strict"
            calls.append(call)

            outputs: dict[str, str] = {}
            if task == "prepare":
                alpha_path = output_root / f"alpha_core_panel_{paths.session_key}.csv"
                pd.DataFrame(_alpha_rows()).to_csv(alpha_path, index=False)
                outputs["alpha_panel_csv"] = alpha_path.as_posix()
            elif task == "decision":
                alpha_path = Path(_command_value(argv, "--alpha-panel-input-path")).resolve()
                assert alpha_path == paths.alpha_panel_path.resolve()
                assert alpha_path.exists()
                target_path = output_root / "decision_targets.csv"
                target_path.write_text("symbol,target_signed_weight\nAAA,0.1\n", encoding="utf-8")
                outputs["decision_targets_csv"] = target_path.as_posix()
            else:
                target_path = Path(_command_value(argv, "--decision-targets-input-path")).resolve()
                assert target_path == paths.decision_targets_path.resolve()
                assert target_path.exists()

            (output_root / "broker_positions_after_raw.json").write_text(
                json.dumps([_position("AAA", 1)]),
                encoding="utf-8",
            )
            (output_root / "execution_summary.json").write_text(
                json.dumps({"ok": True, "outputs": outputs}),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0)

        scheduler.subprocess.run = fake_run
        scheduler._generate_execution_quality = lambda _path: None
        scheduler._generate_daily_audit = lambda _execute, _decision=None: None
        scheduler._finalize_scheduler_run_evidence = lambda _path: None
        try:
            for task in ("prepare", "decision", "execute"):
                assert scheduler._run_task(
                    args=args,
                    state=state,
                    session_date=SESSION_DATE,
                    task=task,
                    paths=paths,
                    now_cn=now_cn,
                ) is True

            session_state = state["sessions"][SESSION_DATE.isoformat()]
            assert [call["task"] for call in calls] == ["prepare", "decision", "execute"]
            assert all(
                session_state[task]["status"] == "completed"
                for task in ("prepare", "decision", "execute")
            )
            assert session_state["prepare"]["alpha_panel_path"] == paths.alpha_panel_path.resolve().as_posix()
            assert session_state["prepare"]["alpha_panel_sha256"] == _sha256_path(paths.alpha_panel_path)
            assert session_state["decision"]["decision_targets_path"] == paths.decision_targets_path.as_posix()
            assert calls[1]["position_reference"] == (
                paths.prepare_output_root / "broker_positions_after_raw.json"
            ).resolve().as_posix()
            assert calls[2]["position_reference"] == (
                paths.decision_output_root / "broker_positions_after_raw.json"
            ).resolve().as_posix()

            session_state["execute"]["status"] = "failed"
            args.force = True
            assert scheduler._run_task(
                args=args,
                state=state,
                session_date=SESSION_DATE,
                task="execute",
                paths=paths,
                now_cn=now_cn,
            ) is True
            assert calls[-1]["position_reference"] == (
                paths.execute_output_root / "broker_positions_after_raw.json"
            ).resolve().as_posix()
        finally:
            scheduler.subprocess.run = original_run
            scheduler._generate_execution_quality = original_quality
            scheduler._generate_daily_audit = original_audit
            scheduler._finalize_scheduler_run_evidence = original_finalize


def main() -> int:
    tests = [
        test_position_continuity_semantics,
        test_latest_successful_stability_payload,
        test_cached_alpha_validation_and_decision_equivalence,
        test_scheduler_rejects_modified_alpha_cache,
        test_scheduler_waits_for_stage_dependencies,
        test_scheduler_three_stage_lifecycle_and_retry_reference,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
