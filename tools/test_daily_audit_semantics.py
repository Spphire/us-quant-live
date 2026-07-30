"""Regression tests for aligned daily-audit timing and evidence semantics."""

from __future__ import annotations

import json
from pathlib import Path

from daily_audit_report import (
    _build_account_state_bridge,
    _build_decision_intent_trace,
    _build_equity_pnl_bridge,
    _build_quote_evidence,
    _build_residual_diagnosis,
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
