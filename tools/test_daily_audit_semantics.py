"""Regression tests for aligned daily-audit timing and evidence semantics."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpaca_executor import _write_artifact_completeness_snapshot

from daily_audit_report import (
    _build_account_activity_attribution,
    _build_account_state_bridge,
    _build_attribution_dossier,
    _build_decision_intent_trace,
    _build_equity_pnl_bridge,
    _build_evidence_completeness,
    _build_ideal_vs_actual_gap,
    _build_intraday_bar_evidence,
    _build_market_price_evidence,
    _build_quote_evidence,
    _build_residual_diagnosis,
    _build_run_evidence_digest_audit,
    _build_strict_attribution_checklist,
)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_account_bridge_uses_sizing_to_post_trade_window(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "broker_account_before.json",
        {"portfolio_value": "100", "cash": "40", "long_market_value": "60", "short_market_value": "0"},
    )
    _write_json(
        tmp_path / "broker_account_for_sizing.json",
        {"portfolio_value": "110", "cash": "50", "long_market_value": "60", "short_market_value": "0"},
    )
    _write_json(
        tmp_path / "broker_account_after.json",
        {"portfolio_value": "105", "cash": "45", "long_market_value": "60", "short_market_value": "0"},
    )
    summary = {"account_equity": 110.0, "account_equity_post_trade": 105.0}

    equity_bridge = _build_equity_pnl_bridge(
        run_dir=tmp_path,
        summary=summary,
        risk={},
        position_rows=[],
        realized_summary={},
        execution_attribution_summary={},
        broker_activity_summary={},
    )
    rows, account_bridge = _build_account_state_bridge(
        run_dir=tmp_path,
        summary=summary,
        account_field_rows=[],
        equity_pnl_bridge=equity_bridge,
    )

    assert rows
    assert equity_bridge["bridge_semantics"] == "execution_window_account_identity"
    assert equity_bridge["account_equity_change"] == -5.0
    assert equity_bridge["preflight_to_sizing_equity_change"] == 10.0
    assert equity_bridge["component_amounts"]["unexplained_execution_window_equity_residual"] == 0.0
    assert account_bridge["source_before"] == "broker_account_for_sizing.json"
    assert account_bridge["window_semantics"] == "sizing_to_post_trade"
    assert account_bridge["preflight_to_sizing_equity_delta"] == 10.0
    assert account_bridge["equity_delta"] == -5.0
    assert account_bridge["equity_delta_vs_summary_delta"] == 0.0
    assert account_bridge["status"] == "pass"

    residual_rows, residual_summary = _build_residual_diagnosis(
        reconciliation_rows=[],
        equity_pnl_bridge=equity_bridge,
        account_field_summary={"exists_before": True, "exists_after": True},
        account_state_bridge_summary=account_bridge,
        position_snapshot_summary={"status": "pass"},
    )
    assert residual_rows == []
    assert residual_summary["status"] == "pass"


def test_quote_capture_uses_valid_fallback_and_execution_universe(tmp_path: Path) -> None:
    valid_before = {"bp": 99.9, "ap": 100.1, "t": "before"}
    stale_post = {
        "bp": 99.8,
        "ap": 100.2,
        "t": "post",
        "validation_error": "stale_quote_age_ms=12000",
    }
    valid_after = {"bp": 100.0, "ap": 100.2, "t": "after"}
    for filename, quote in [
        ("execution_latest_quotes_snapshot.json", valid_before),
        ("execution_latest_quotes_snapshot_post_submission.json", stale_post),
        ("execution_latest_quotes_snapshot_after.json", valid_after),
    ]:
        _write_json(
            tmp_path / filename,
            {"ok": True, "requested_symbols": ["AAA"], "payload": {"AAA": quote}, "errors": []},
        )

    market_rows = [
        {
            "symbol": "AAA",
            "in_execute_target_symbols": True,
            "in_execute_broker_position_before": False,
            "execute_reference_price_used": 100.0,
        },
        {"symbol": "CTX1", "in_execute_target_symbols": False, "in_execute_broker_position_before": False},
        {"symbol": "CTX2", "in_execute_target_symbols": False, "in_execute_broker_position_before": False},
    ]
    rows, summary = _build_quote_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=market_rows,
        fill_rows=[{"symbol": "AAA", "qty": 1, "price": 100.1, "side": "buy"}],
    )
    by_symbol = {row["symbol"]: row for row in rows}

    assert by_symbol["AAA"]["required_for_execution"] is True
    assert by_symbol["AAA"]["source_used"] == "after_execution"
    assert by_symbol["AAA"]["status"] == "pass"
    assert summary["status"] == "pass"
    assert summary["execution_relevant_symbol_count"] == 1
    assert summary["missing_quote_symbol_count"] == 0
    assert summary["invalid_quote_symbol_count"] == 0
    assert summary["context_missing_quote_symbol_count"] == 2
    assert summary["source_invalid_quote_observation_count"] == 1


def test_quote_spread_uses_execution_run_threshold(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "run_context.json",
        {"parsed_args": {"longbridge_max_spread_bps": 150.0}},
    )
    _write_json(
        tmp_path / "execution_latest_quotes_snapshot.json",
        {
            "ok": True,
            "requested_symbols": ["AAA"],
            "payload": {"AAA": {"bp": 99.4, "ap": 100.6}},
            "errors": [],
        },
    )
    rows, summary = _build_quote_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=[
            {
                "symbol": "AAA",
                "in_execute_target_symbols": True,
                "in_execute_broker_position_before": False,
            }
        ],
        fill_rows=[],
    )

    assert 100.0 < rows[0]["spread_bps"] < 150.0
    assert rows[0]["status"] == "pass"
    assert summary["status"] == "pass"
    assert summary["wide_spread_threshold_bps"] == 150.0
    assert summary["wide_spread_symbol_count"] == 0


def test_intraday_bars_exclude_context_only_symbols_from_status(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "execution_intraday_bars_1min.json",
        {
            "ok": True,
            "requested_symbols": ["AAA"],
            "bars": [
                {
                    "symbol": "AAA",
                    "t": "2026-07-29T14:00:00Z",
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.5,
                    "v": 1000,
                    "vw": 100.2,
                }
            ],
            "errors": [],
        },
    )
    market_rows = [
        {
            "symbol": "AAA",
            "in_execute_target_symbols": True,
            "in_execute_broker_position_before": False,
            "execute_reference_price_used": 100.0,
        },
        {"symbol": "CTX1", "in_execute_target_symbols": False, "in_execute_broker_position_before": False},
        {"symbol": "CTX2", "in_execute_target_symbols": False, "in_execute_broker_position_before": False},
    ]

    rows, summary = _build_intraday_bar_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=market_rows,
        fill_rows=[{"symbol": "AAA", "qty": 1, "price": 100.1, "side": "buy"}],
    )
    by_symbol = {row["symbol"]: row for row in rows}

    assert by_symbol["AAA"]["required_for_execution"] is True
    assert by_symbol["CTX1"]["required_for_execution"] is False
    assert summary["status"] == "pass"
    assert summary["execution_relevant_symbol_count"] == 1
    assert summary["missing_bar_symbol_count"] == 0
    assert summary["context_missing_bar_symbol_count"] == 2


def test_market_prices_exclude_context_only_symbols_from_status(tmp_path: Path) -> None:
    decision_dir = tmp_path / "decision"
    execute_dir = tmp_path / "execute"
    decision_dir.mkdir()
    execute_dir.mkdir()
    _write_json(
        decision_dir / "execution_price_snapshot.json",
        {
            "feed": "iex",
            "target_symbols": ["AAA"],
            "broker_position_symbols_before": [],
            "reference_prices": {"AAA": 100.0, "CTX": 200.0},
            "fallback_prices": {},
            "missing_reference_price_symbols": [],
        },
    )
    _write_json(
        execute_dir / "execution_price_snapshot.json",
        {
            "feed": "us_lv1_nbbo",
            "target_symbols": ["AAA"],
            "broker_position_symbols_before": [],
            "reference_prices": {"AAA": 101.0},
            "fallback_prices": {},
            "missing_reference_price_symbols": [],
        },
    )

    rows, summary = _build_market_price_evidence(
        run_dir=execute_dir,
        decision_dir=decision_dir,
        decision_plan={},
        execute_plan={},
        decision_rows=[{"symbol": "AAA"}],
        fill_rows=[],
    )
    by_symbol = {row["symbol"]: row for row in rows}

    assert by_symbol["AAA"]["required_for_execution"] is True
    assert by_symbol["CTX"]["required_for_execution"] is False
    assert summary["status"] == "pass"
    assert summary["execution_relevant_symbol_count"] == 1
    assert summary["execute_missing_reference_symbol_count"] == 0
    assert summary["context_missing_reference_symbol_count"] == 1


def test_market_price_fallback_only_is_usable_partial_evidence(tmp_path: Path) -> None:
    execute_dir = tmp_path / "execute"
    execute_dir.mkdir()
    _write_json(
        execute_dir / "execution_price_snapshot.json",
        {
            "target_symbols": ["AAA"],
            "reference_prices": {"AAA": 100.0},
            "fallback_prices": {"AAA": 100.0},
            "missing_reference_price_symbols": [],
        },
    )

    rows, summary = _build_market_price_evidence(
        run_dir=execute_dir,
        decision_dir=None,
        decision_plan={},
        execute_plan={},
        decision_rows=[],
        fill_rows=[],
    )

    assert rows[0]["status"] == "fallback_only"
    assert summary["status"] == "partial"
    assert summary["execute_missing_reference_symbol_count"] == 0
    assert summary["fallback_only_symbol_count"] == 1


def test_context_only_bar_and_quote_errors_do_not_downgrade_status(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "execution_intraday_bars_1min.json",
        {
            "requested_symbols": ["AAA", "CTX"],
            "bars": [
                {
                    "symbol": "AAA",
                    "t": "2026-07-29T14:00:00Z",
                    "o": 100.0,
                    "h": 101.0,
                    "l": 99.0,
                    "c": 100.5,
                    "v": 1000,
                }
            ],
            "errors": [{"symbols": ["CTX"], "error": "context unavailable"}],
        },
    )
    _write_json(
        tmp_path / "execution_latest_quotes_snapshot.json",
        {
            "ok": True,
            "requested_symbols": ["AAA", "CTX"],
            "payload": {"AAA": {"bp": 99.9, "ap": 100.1}},
            "errors": [{"symbols": ["CTX"], "error": "context unavailable"}],
        },
    )
    market_rows = [
        {
            "symbol": "AAA",
            "in_execute_target_symbols": True,
            "in_execute_broker_position_before": False,
            "execute_reference_price_used": 100.0,
        },
        {
            "symbol": "CTX",
            "in_execute_target_symbols": False,
            "in_execute_broker_position_before": False,
        },
    ]

    _, bar_summary = _build_intraday_bar_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=market_rows,
        fill_rows=[],
    )
    _, quote_summary = _build_quote_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=market_rows,
        fill_rows=[],
    )

    assert bar_summary["status"] == "pass"
    assert bar_summary["error_count"] == 0
    assert bar_summary["context_error_count"] == 1
    assert quote_summary["status"] == "pass"
    assert quote_summary["error_count"] == 0
    assert quote_summary["context_error_count"] == 1


def test_failed_quote_capture_is_execution_error(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "execution_latest_quotes_snapshot.json",
        {
            "ok": False,
            "requested_symbols": ["AAA"],
            "error_type": "QuoteError",
            "error": "capture failed",
        },
    )
    _, summary = _build_quote_evidence(
        run_dir=tmp_path,
        market_price_evidence_rows=[
            {
                "symbol": "AAA",
                "in_execute_target_symbols": True,
                "in_execute_broker_position_before": False,
            }
        ],
        fill_rows=[],
    )

    assert summary["status"] == "attention"
    assert summary["error_count"] == 1
    assert summary["missing_quote_symbol_count"] == 1


def test_attribution_dossier_uses_execution_symbols_and_symbol_gap_count() -> None:
    rows, summary = _build_attribution_dossier(
        context={"session_date": "2026-07-29", "run_dir": "execute"},
        summary={"decision_date": "2026-07-29"},
        equity_pnl_bridge={"component_amounts": {}},
        symbol_attribution_rows=[{"symbol": "AAA"}],
        target_transition_rows=[],
        decision_intent_rows=[],
        order_constraint_rows=[],
        decision_execute_drift_rows=[],
        market_price_evidence_rows=[
            {"symbol": "AAA", "required_for_execution": True, "status": "pass"},
            {
                "symbol": "CTX",
                "required_for_execution": False,
                "status": "missing_reference_price",
                "missing_reference_flag": True,
            },
        ],
        intraday_bar_rows=[
            {"symbol": "AAA", "required_for_execution": True, "status": "pass"},
            {"symbol": "CTX", "required_for_execution": False, "status": "missing_bars"},
        ],
        quote_rows=[
            {"symbol": "AAA", "required_for_execution": True, "status": "pass"},
            {"symbol": "CTX", "required_for_execution": False, "status": "missing_quote"},
        ],
        market_context_rows=[{"row_type": "symbol", "symbol": "AAA"}],
        market_context_summary={},
        residual_diagnosis_summary={"status": "pass"},
        evidence_completeness_summary={
            "strict_account_position_replay_ready": True,
            "lowest_coverage_areas": [],
        },
        strict_attribution_checklist_summary={
            "status": "ready",
            "strict_attribution_ready": True,
            "blocking_item_count": 0,
            "top_blockers": [],
        },
    )

    assert [row["symbol"] for row in rows] == ["AAA"]
    assert summary["universe_semantics"] == "execution_relevant_symbols_only"
    assert summary["focus_symbol_count"] == 1
    assert summary["symbol_evidence_gap_count"] == 0
    assert summary["coverage_area_gap_count"] == 0
    assert summary["status"] == "pass"


def test_evidence_digest_resolves_decision_stage_alpha(tmp_path: Path) -> None:
    execute_dir = tmp_path / "20260729_execute"
    decision_dir = tmp_path / "20260729_decision"
    execute_dir.mkdir()
    decision_dir.mkdir()
    _write_json(execute_dir / "run_evidence_digest.json", {"status": "pass"})
    _write_json(
        execute_dir / "artifact_completeness_snapshot.json",
        {
            "status": "partial",
            "categories": {
                "portfolio_intent": {
                    "status": "partial",
                    "missing": ["alpha_core_panel_20260729.csv"],
                }
            },
        },
    )
    (decision_dir / "alpha_core_panel_20260729.csv").write_text("symbol\nAAA\n", encoding="utf-8")

    _, summary = _build_run_evidence_digest_audit(execute_dir, decision_dir)

    assert summary["artifact_completeness_raw_status"] == "partial"
    assert summary["artifact_completeness_status"] == "pass"
    assert summary["artifact_completeness_partial_category_count"] == 0
    assert summary["artifact_completeness_missing_file_count"] == 0
    assert summary["artifact_completeness_resolved_from_paired_decision_count"] == 1


def test_executor_completeness_records_paired_decision_source(tmp_path: Path) -> None:
    execute_dir = tmp_path / "20260729_execute"
    decision_dir = tmp_path / "20260729_decision"
    execute_dir.mkdir()
    decision_dir.mkdir()
    alpha_path = decision_dir / "alpha_core_panel_20260729.csv"
    alpha_path.write_text("symbol\nAAA\n", encoding="utf-8")
    _write_json(
        execute_dir / "run_context.json",
        {
            "parsed_args": {
                "decision_targets_input_path": str(decision_dir / "decision_targets.csv")
            }
        },
    )

    snapshot_path = _write_artifact_completeness_snapshot(execute_dir)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    alpha_row = next(
        row
        for row in snapshot["categories"]["portfolio_intent"]["artifacts"]
        if row["artifact"] == "alpha_core_panel_20260729.csv"
    )

    assert alpha_row["exists"] is True
    assert alpha_row["source_scope"] == "paired_decision"
    assert Path(alpha_row["path"]) == alpha_path


def test_decision_intent_treats_minimum_trade_edge_as_explained() -> None:
    rows, summary = _build_decision_intent_trace(
        plan={
            "account_equity": 1000.0,
            "min_trade_notional": 10.0,
            "raw_target_signed_weights": {"BE": 0.0101},
            "capacity_adjusted_target_signed_weights": {"BE": 0.0101},
            "executable_expected_signed_weights": {"BE": 0.0101},
        },
        decision_rows=[{"symbol": "BE", "before_market_value": 0.0}],
        order_rows=[],
        summary={},
    )

    assert rows[0]["desired_delta_notional_estimate"] == 10.1
    assert rows[0]["order_intent_status"] == "within_min_trade_boundary_tolerance"
    assert summary["min_trade_boundary_symbol_count"] == 1
    assert summary["unexplained_no_order_symbol_count"] == 0
    assert summary["status"] == "pass"


def test_decision_intent_uses_final_order_target_for_minimum_trade_check() -> None:
    rows, summary = _build_decision_intent_trace(
        plan={
            "account_equity": 1000.0,
            "min_trade_notional": 10.0,
            "raw_target_signed_weights": {"AAA": 0.52},
            "capacity_adjusted_target_signed_weights": {"AAA": 0.52},
            "executable_expected_signed_weights": {"AAA": 0.5125},
            "target_signed_weights": {"AAA": 0.509},
        },
        decision_rows=[{"symbol": "AAA", "before_market_value": 500.0}],
        order_rows=[],
        summary={},
    )

    assert rows[0]["projected_target_delta_notional_estimate"] == 12.5
    assert rows[0]["order_builder_delta_notional_estimate"] == 9.0
    assert rows[0]["desired_delta_notional_estimate"] == 9.0
    assert rows[0]["order_intent_status"] == "below_min_trade_notional"
    assert summary["order_target_source"] == "target_signed_weights"
    assert summary["below_min_trade_symbol_count"] == 1
    assert summary["unexplained_no_order_symbol_count"] == 0
    assert summary["status"] == "pass"


def test_ideal_actual_weight_errors_use_signed_weights_and_post_trade_equity() -> None:
    rows, summary = _build_ideal_vs_actual_gap(
        context={"session_date": "2026-07-29"},
        summary={"account_equity": 190.0, "account_equity_post_trade": 200.0},
        symbol_attribution_rows=[{"symbol": "AAA", "after_market_value": 60.0}],
        target_transition_rows=[],
        decision_intent_rows=[
            {
                "symbol": "AAA",
                "raw_target_signed_weight": 0.5,
                "strategy_target_signed_weight": 0.5,
                "projected_target_signed_weight": 0.4,
                "raw_target_notional_estimate": 95.0,
                "strategy_target_notional_estimate": 95.0,
                "projected_target_notional_estimate": 76.0,
            }
        ],
        order_constraint_rows=[],
        decision_execute_drift_rows=[
            {
                "symbol": "AAA",
                "execute_projected_target_signed_weight": 0.4,
                "execute_target_notional_estimate": 76.0,
            }
        ],
        market_context_rows=[],
        replay_focus_rows=[],
    )

    assert len(rows) == 1
    assert rows[0]["actual_signed_weight"] == 0.3
    assert rows[0]["strategy_actual_weight_error_abs"] == 0.2
    assert abs(rows[0]["strategy_executable_weight_error_abs"] - 0.1) < 1e-12
    assert abs(rows[0]["executable_actual_weight_error_abs"] - 0.1) < 1e-12
    assert summary["weight_error_semantics"] == "sum_abs_signed_weight_difference"
    assert summary["weight_error_actual_denominator"] == "post_trade_account_equity"
    assert summary["weight_error_actual_denominator_amount"] == 200.0
    assert summary["strategy_to_actual_weight_error_l1"] == 0.2


def _strict_context_with_all_artifacts(tmp_path: Path) -> dict[str, object]:
    artifact_keys = [
        "broker_account_before",
        "broker_account_after",
        "broker_positions_before_raw",
        "broker_positions_after_raw",
        "broker_position_account_stability_before",
        "broker_position_account_stability_after",
        "broker_fill_activities",
        "broker_account_activities",
        "broker_order_snapshots",
        "broker_orders_all_after",
        "order_poll_timeline",
        "execution_price_snapshot",
        "execution_latest_trades_snapshot",
        "execution_latest_quotes_snapshot",
        "execution_latest_quotes_snapshot_after",
        "execution_intraday_bars_1min",
        "broker_portfolio_history_before",
        "broker_portfolio_history_after",
        "broker_calendar_window",
        "broker_corporate_actions",
        "run_context",
        "run_evidence_digest",
        "source_code_manifest",
        "source_git_snapshot",
        "python_environment",
        "scheduler_task_context",
        "scheduler_task_result",
    ]
    artifacts: dict[str, str] = {}
    for key in artifact_keys:
        path = tmp_path / f"{key}.json"
        path.write_text("{}", encoding="utf-8")
        artifacts[key] = str(path)
    return {"artifacts": artifacts, "artifact_counts": {}}


def _strict_passing_summaries() -> dict[str, dict[str, object]]:
    return {
        "position_snapshot_integrity": {"status": "pass"},
        "residual_diagnosis": {"status": "pass"},
        "evidence_completeness": {"strict_account_position_replay_ready": True},
        "market_price_evidence": {
            "status": "pass",
            "execute_missing_reference_symbol_count": 0,
        },
        "account_activity_attribution": {
            "status": "pass",
            "capture_ok": True,
            "unknown_activity_net_amount": 0.0,
        },
        "corporate_action_trace": {"status": "pass"},
        "portfolio_history_trace": {"status": "pass"},
        "calendar_trace": {"status": "pass"},
        "account_state_bridge": {"status": "pass"},
        "intraday_bar_evidence": {
            "status": "pass",
            "missing_bar_symbol_count": 0,
            "error_count": 0,
        },
        "quote_evidence": {
            "status": "pass",
            "missing_quote_symbol_count": 0,
            "invalid_quote_symbol_count": 0,
            "error_count": 0,
        },
        "decision_intent": {
            "status": "pass",
            "unexplained_no_order_symbol_count": 0,
        },
        "decision_execute_drift": {"symbol_count": 1},
        "run_evidence_digest": {
            "status": "pass",
            "digest_exists": True,
            "strict_missing_file_count": 0,
        },
        "startup_binding": {
            "status": "attention",
            "issue_count": 1,
            "autostart_registered": False,
        },
        "run_failure_diagnosis": {"status": "pass", "failure_class": "none"},
    }


def test_current_startup_state_does_not_block_historical_attribution(tmp_path: Path) -> None:
    context = _strict_context_with_all_artifacts(tmp_path)
    summaries = _strict_passing_summaries()

    rows, summary = _build_strict_attribution_checklist(context=context, summaries=summaries)
    startup_row = next(row for row in rows if row["item"] == "startup_binding_observable")

    assert startup_row["status"] == "attention"
    assert startup_row["blocking_strict_attribution"] is False
    assert summary["status"] == "ready"
    assert summary["blocking_item_count"] == 0

    _, evidence = _build_evidence_completeness(context=context, summaries=summaries)
    assert evidence["operational_context_gap_area_count"] == 1
    assert all(row["area"] != "operational_startup" for row in evidence["coverage_gap_areas"])


def test_modern_historical_limited_capture_statuses_block_strict_attribution(tmp_path: Path) -> None:
    context = _strict_context_with_all_artifacts(tmp_path)
    cases = [
        ("market_price_evidence", "reference_prices_available"),
        ("portfolio_history_trace", "portfolio_history_capture_ok"),
        ("calendar_trace", "calendar_capture_ok"),
        ("account_state_bridge", "account_state_bridge_ok"),
        ("account_activity_attribution", "account_activity_known_or_empty"),
        ("corporate_action_trace", "corporate_action_capture_ok"),
    ]
    for summary_key, item_name in cases:
        summaries = deepcopy(_strict_passing_summaries())
        summaries[summary_key]["status"] = "historical_limited"
        rows, summary = _build_strict_attribution_checklist(
            context=context,
            summaries=summaries,
        )
        item = next(row for row in rows if row["item"] == item_name)
        assert item["status"] == "attention"
        assert item["blocking_strict_attribution"] is True
        assert summary["status"] == "blocked"


def test_unexplained_decision_intent_blocks_strict_attribution(tmp_path: Path) -> None:
    context = _strict_context_with_all_artifacts(tmp_path)
    summaries = deepcopy(_strict_passing_summaries())
    summaries["decision_intent"] = {
        "status": "attention",
        "unexplained_no_order_symbol_count": 1,
    }

    rows, summary = _build_strict_attribution_checklist(
        context=context,
        summaries=summaries,
    )
    item = next(row for row in rows if row["item"] == "decision_intent_explained")

    assert item["status"] == "attention"
    assert item["blocking_strict_attribution"] is True
    assert summary["status"] == "blocked"


def test_account_activity_capture_failure_is_attention() -> None:
    _, summary = _build_account_activity_attribution(
        broker_activity_rows=[],
        broker_account_activities_capture={
            "ok": False,
            "error_type": "BrokerError",
            "error": "activity request failed",
        },
    )

    assert summary["status"] == "attention"
    assert summary["capture_ok"] is False
    assert summary["unknown_activity_net_amount"] == 0.0
