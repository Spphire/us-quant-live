"""Regression checks for the execution-only dashboard timeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dashboard_server import DataAggregator  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _timings(started_at: str, elapsed: float = 10.0) -> dict:
    return {
        "schema_version": "1.0",
        "run_started_at_utc": started_at,
        "status": "succeeded",
        "elapsed_seconds": elapsed,
        "phases": [
            {
                "phase": "startup_evidence",
                "status": "completed",
                "run_elapsed_start_seconds": 0.0,
                "run_elapsed_end_seconds": 1.0,
                "elapsed_seconds": 1.0,
                "share_of_run_pct": 10.0,
            },
            {
                "phase": "order_submission_and_tracking",
                "status": "completed",
                "run_elapsed_start_seconds": 1.0,
                "run_elapsed_end_seconds": 7.0,
                "elapsed_seconds": 6.0,
                "share_of_run_pct": 60.0,
            },
            {
                "phase": "post_run_audit_and_finalize",
                "status": "completed",
                "run_elapsed_start_seconds": 7.0,
                "run_elapsed_end_seconds": elapsed,
                "elapsed_seconds": elapsed - 7.0,
                "share_of_run_pct": 30.0,
            },
        ],
    }


def _execution_records() -> list[dict]:
    return [
        {
            "symbol": "AAA",
            "side": "buy",
            "qty": 10.5,
            "stage": "entry",
            "status_latest": "filled",
            "batch_started_at_utc": "2026-07-29T14:00:02+00:00",
            "queue_wait_ms": 0.0,
            "order_wall_time_seconds": 3.0,
            "batch_effective_workers": 2,
            "batch_wave_index": 1,
            "attempts": [
                {
                    "attempt_no": 1,
                    "quote_observed_at_utc": "2026-07-29T14:00:02.100+00:00",
                    "quote_age_ms": 120.0,
                    "offset_bps": 12.0,
                    "status_latest": "filled",
                    "reference_price_source": "test.quote",
                    "poll_events": [
                        {"at_utc": "2026-07-29T14:00:02.500+00:00", "event": "submitted"},
                        {"at_utc": "2026-07-29T14:00:04.500+00:00", "event": "poll"},
                    ],
                }
            ],
        },
        {
            "symbol": "BBB",
            "side": "sell",
            "qty": 4,
            "stage": "entry",
            "status_latest": "filled",
            "batch_started_at_utc": "2026-07-29T14:00:02+00:00",
            "queue_wait_ms": 1000.0,
            "order_wall_time_seconds": 3.0,
            "batch_effective_workers": 2,
            "batch_wave_index": 1,
            "attempts": [
                {
                    "attempt_no": 1,
                    "quote_observed_at_utc": "2026-07-29T14:00:03.100+00:00",
                    "status_latest": "filled",
                    "poll_events": [],
                }
            ],
        },
        {
            "symbol": "CCC",
            "side": "buy",
            "qty": 2,
            "stage": "entry_repair",
            "status_latest": "filled",
            "batch_started_at_utc": "2026-07-29T14:00:06+00:00",
            "queue_wait_ms": 0.0,
            "order_wall_time_seconds": 0.5,
            "batch_effective_workers": 1,
            "batch_wave_index": 2,
            "attempts": [],
        },
    ]


def _build_fixture(root: Path) -> tuple[Path, DataAggregator]:
    artifacts_root = root / "artifacts" / "daily_alpaca_scheduler"
    old_run = artifacts_root / "20260728_execute"
    _write_json(old_run / "decision_phase_timings.json", _timings("2026-07-28T14:00:00+00:00"))

    decision_run = artifacts_root / "20260729_decision"
    _write_json(decision_run / "decision_phase_timings.json", _timings("2026-07-29T04:30:00+00:00"))

    run = artifacts_root / "20260729_execute"
    _write_json(run / "decision_phase_timings.json", _timings("2026-07-29T14:00:00+00:00"))
    _write_json(run / "execution_records.json", _execution_records())
    _write_jsonl(
        run / "run_events.jsonl",
        [
            {"seq": 1, "name": "executor_started", "run_elapsed_seconds": 0.0},
            {"seq": 2, "name": "order_submission_finished", "run_elapsed_seconds": 7.0},
            {"seq": 3, "name": "execution_summary_ready", "run_elapsed_seconds": 9.5},
        ],
    )
    _write_jsonl(
        run / "alpaca_api_audit.jsonl",
        [
            {
                "seq": 1,
                "started_at_utc": "2026-07-29T13:59:00+00:00",
                "method": "GET",
                "url": "https://paper-api.example/v2/account?token=old-secret",
                "elapsed_ms": 100.0,
                "ok": True,
                "status_code": 200,
            },
            {
                "seq": 2,
                "started_at_utc": "2026-07-29T14:00:01+00:00",
                "method": "GET",
                "url": "https://paper-api.example/v2/account",
                "elapsed_ms": 200.0,
                "ok": True,
                "status_code": 200,
                "response_body": {"account_number": "SECRET-ACCOUNT"},
            },
            {
                "seq": 3,
                "started_at_utc": "2026-07-29T14:00:02+00:00",
                "method": "POST",
                "url": "https://paper-api.example/v2/orders",
                "elapsed_ms": 500.0,
                "ok": True,
                "status_code": 200,
                "request_body": {"client_order_id": "secret-client-id"},
            },
            {
                "seq": 4,
                "started_at_utc": "2026-07-29T14:00:02.100+00:00",
                "method": "GET",
                "url": "https://paper-api.example/v2/orders/secret-order-id",
                "elapsed_ms": 600.0,
                "ok": False,
                "status_code": 503,
            },
            {
                "seq": 5,
                "started_at_utc": "2026-07-29T14:01:00+00:00",
                "method": "GET",
                "url": "https://paper-api.example/v2/positions",
                "elapsed_ms": 100.0,
                "ok": True,
                "status_code": 200,
            },
        ],
    )
    return artifacts_root, DataAggregator(artifacts_root, root)


def test_latest_run_and_execution_only_scope() -> None:
    with TemporaryDirectory() as tmp:
        _, aggregator = _build_fixture(Path(tmp))
        payload = aggregator.get_execution_timeline()
        assert payload["status"] == "pass", payload
        assert payload["selected_run"] == "20260729_execute", payload
        names = [row["run_name"] for row in payload["available_runs"]]
        assert names == ["20260729_execute", "20260728_execute"], names
        assert all(name.endswith("_execute") for name in names), names
        assert payload["run"]["elapsed_seconds"] == 6.0, payload["run"]
        assert payload["run"]["started_at_utc"] == "2026-07-29T14:00:01+00:00", payload["run"]
        assert any(track["id"] == "pipeline" for track in payload["tracks"]), payload["tracks"]
        pipeline = next(track for track in payload["tracks"] if track["id"] == "pipeline")
        assert [item["detail"]["phase"] for item in pipeline["items"]] == [
            "order_submission_and_tracking"
        ], pipeline


def test_order_attempts_concurrency_and_summary() -> None:
    with TemporaryDirectory() as tmp:
        _, aggregator = _build_fixture(Path(tmp))
        payload = aggregator.get_execution_timeline("20260729_execute")
        order_tracks = [track for track in payload["tracks"] if track["kind"] == "order"]
        assert len(order_tracks) == 2, order_tracks
        order_items = [item for track in order_tracks for item in track["items"]]
        assert len(order_items) == 3, order_items
        assert sum(len(item.get("children", [])) for item in order_items) == 2, order_items
        summary = payload["summary"]
        assert summary["order_count"] == 3, summary
        assert summary["attempt_count"] == 2, summary
        assert summary["max_order_concurrency"] == 2, summary
        assert summary["total_process_seconds"] == 6.0, summary
        assert summary["trading_complete_seconds"] == 6.0, summary


def test_api_window_classification_and_redaction() -> None:
    with TemporaryDirectory() as tmp:
        _, aggregator = _build_fixture(Path(tmp))
        payload = aggregator.get_execution_timeline("20260729_execute")
        api_items = [
            item
            for track in payload["tracks"]
            if track["kind"] == "api"
            for item in track["items"]
        ]
        assert len(api_items) == 3, api_items
        assert payload["summary"]["api_call_count"] == 3, payload["summary"]
        assert payload["summary"]["max_api_concurrency"] == 2, payload["summary"]
        assert {item["label"] for item in api_items} == {"Account", "Submit order", "Poll order"}
        milestones = [
            item
            for track in payload["tracks"]
            if track["kind"] == "milestone"
            for item in track["items"]
        ]
        assert [item["detail"]["event"] for item in milestones] == [
            "order_submission_finished"
        ], milestones
        serialized = json.dumps(payload).lower()
        for forbidden in (
            "paper-api.example",
            "secret-order-id",
            "secret-client-id",
            "secret-account",
            "request_body",
            "response_body",
            "client_order_id",
            "order_id",
        ):
            assert forbidden not in serialized, forbidden


def test_selected_run_and_invalid_name_are_bounded() -> None:
    with TemporaryDirectory() as tmp:
        _, aggregator = _build_fixture(Path(tmp))
        selected = aggregator.get_execution_timeline("20260728_execute")
        assert selected["status"] == "pass", selected
        assert selected["selected_run"] == "20260728_execute", selected
        invalid = aggregator.get_execution_timeline("../20260729_execute")
        assert invalid["status"] == "not_found", invalid
        assert invalid["tracks"] == [], invalid


def main() -> int:
    tests = [
        ("Latest execution-only run", test_latest_run_and_execution_only_scope),
        ("Order attempts and concurrency", test_order_attempts_concurrency_and_summary),
        ("API window and redaction", test_api_window_classification_and_redaction),
        ("Bounded run selection", test_selected_run_and_invalid_name_are_bounded),
    ]
    for name, test in tests:
        print(f"[TEST] {name}")
        test()
        print("  [OK]")
    print(f"[PASS] All {len(tests)} execution timeline tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
