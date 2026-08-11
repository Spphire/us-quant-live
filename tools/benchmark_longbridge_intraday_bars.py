from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alpaca_executor import _collect_intraday_bars_snapshot  # noqa: E402
from vendors.longbridge import LongbridgeCredentials, LongbridgeQuoteClient  # noqa: E402


def _parse_workers(raw: str) -> list[int]:
    values = sorted({max(1, int(item.strip())) for item in str(raw).split(",") if item.strip()})
    if not values:
        raise ValueError("At least one worker count is required.")
    return values


def _load_symbols(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
    if not symbols:
        raise RuntimeError(f"No symbols were found in {path}.")
    return sorted(symbols)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    position = (len(ordered) - 1) * min(1.0, max(0.0, float(quantile)))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _gate_snapshot(payload: Mapping[str, Any], expected_symbols: Sequence[str]) -> dict[str, Any]:
    bars = [dict(row) for row in payload.get("bars", []) if isinstance(row, Mapping)]
    api_calls = [dict(row) for row in payload.get("api_calls", []) if isinstance(row, Mapping)]
    errors = [dict(row) for row in payload.get("errors", []) if isinstance(row, Mapping)]
    expected = set(expected_symbols)
    observed = {
        str(row.get("symbol") or "").strip().upper()
        for row in bars
        if str(row.get("symbol") or "").strip()
    }
    positive_volume_bars = [row for row in bars if float(row.get("v") or 0.0) > 0.0]
    positive_volume_symbols = {
        str(row.get("symbol") or "").strip().upper()
        for row in positive_volume_bars
        if str(row.get("symbol") or "").strip()
    }
    invalid_bars = [
        row
        for row in bars
        if not str(row.get("t") or "").strip()
        or any(float(row.get(key) or 0.0) <= 0.0 for key in ("o", "h", "l", "c"))
    ]
    metrics = dict(payload.get("metrics") or {}) if isinstance(payload.get("metrics"), Mapping) else {}
    checks = {
        "provider_is_longbridge": str(payload.get("provider") or "") == "longbridge",
        "adjustment_is_raw": str(payload.get("adjustment") or "") == "raw",
        "requested_symbol_count_matches": int(payload.get("requested_symbol_count") or 0) == len(expected),
        "all_symbols_have_bars": observed == expected,
        "all_symbols_have_positive_volume_bars": positive_volume_symbols == expected,
        "no_capture_errors": not errors,
        "no_rate_limit_errors": int(metrics.get("rate_limit_error_count") or 0) == 0,
        "one_api_call_per_symbol": len(api_calls) == len(expected),
        "all_api_calls_succeeded": bool(api_calls) and all(bool(row.get("ok")) for row in api_calls),
        "bars_have_timestamp_and_ohlc": not invalid_bars,
    }
    return {
        "schema_version": "1.0",
        "artifact_type": "longbridge_intraday_bar_gate",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "expected_symbol_count": len(expected),
        "bar_symbol_count": len(observed),
        "bar_count": len(bars),
        "positive_volume_bar_count": len(positive_volume_bars),
        "zero_volume_bar_count": len(bars) - len(positive_volume_bars),
        "zero_volume_bar_ratio": (
            (len(bars) - len(positive_volume_bars)) / len(bars) if bars else 0.0
        ),
        "positive_volume_bar_symbol_count": len(positive_volume_symbols),
        "missing_positive_volume_bar_symbols": sorted(expected - positive_volume_symbols),
        "missing_symbols": sorted(expected - observed),
        "unexpected_symbols": sorted(observed - expected),
        "error_count": len(errors),
        "invalid_bar_count": len(invalid_bars),
        "rate_limit_error_count": int(metrics.get("rate_limit_error_count") or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Longbridge one-minute evidence benchmark using a production target universe."
    )
    parser.add_argument(
        "--targets-path",
        default=r"W:\Quat\us-quant-live\artifacts\daily_alpaca_scheduler\20260811_prepare\decision_targets.csv",
    )
    parser.add_argument(
        "--longbridge-config-path",
        default=r"W:\Quat\us-quant-live\configs\longbridge.local.json",
    )
    parser.add_argument("--session-date", default="2026-08-11")
    parser.add_argument("--workers", default="1,4,8")
    parser.add_argument(
        "--output-root",
        default=str(PROJECT_ROOT / "artifacts" / "research" / "longbridge_intraday_target_20260811"),
    )
    args = parser.parse_args()

    targets_path = Path(args.targets_path).resolve()
    output_root = Path(args.output_root).resolve()
    session_date = date.fromisoformat(str(args.session_date)[:10])
    workers = _parse_workers(str(args.workers))
    symbols = _load_symbols(targets_path)
    credentials = LongbridgeCredentials.from_sources(args.longbridge_config_path)
    summaries: list[dict[str, Any]] = []

    for worker_count in workers:
        run_dir = output_root / f"workers_{worker_count}"
        run_dir.mkdir(parents=True, exist_ok=True)
        client = LongbridgeQuoteClient(
            credentials,
            snapshot_context_count=worker_count,
        )
        try:
            payload = _collect_intraday_bars_snapshot(
                client=client,
                symbols=symbols,
                session_date=session_date,
                feed=client.intraday_bar_feed_name,
                label=f"production_target_workers_{worker_count}",
                fallback_feed=None,
            )
        finally:
            client.close()

        artifact_path = run_dir / "execution_intraday_bars_1min.json"
        _write_json(artifact_path, payload)
        gate = _gate_snapshot(payload, symbols)
        gate_path = run_dir / "artifact_completeness_snapshot.json"
        _write_json(gate_path, gate)

        api_calls = [dict(row) for row in payload.get("api_calls", []) if isinstance(row, Mapping)]
        elapsed_ms = [float(row.get("elapsed_ms") or 0.0) for row in api_calls]
        rows_per_symbol = Counter(
            str(row.get("symbol") or "").strip().upper()
            for row in payload.get("bars", [])
            if isinstance(row, Mapping) and str(row.get("symbol") or "").strip()
        )
        positive_volume_rows = [
            row
            for row in payload.get("bars", [])
            if isinstance(row, Mapping) and float(row.get("v") or 0.0) > 0.0
        ]
        positive_volume_symbols = {
            str(row.get("symbol") or "").strip().upper()
            for row in positive_volume_rows
            if str(row.get("symbol") or "").strip()
        }
        metrics = dict(payload.get("metrics") or {}) if isinstance(payload.get("metrics"), Mapping) else {}
        summary = {
            "schema_version": "1.0",
            "artifact_type": "longbridge_intraday_bar_benchmark",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "read_only": True,
            "orders_submitted": False,
            "session_date": session_date.isoformat(),
            "targets_path": targets_path.as_posix(),
            "target_symbol_count": len(symbols),
            "configured_worker_count": worker_count,
            "effective_worker_count": int(metrics.get("worker_count") or 0),
            "context_count": int(metrics.get("context_count") or 0),
            "elapsed_seconds": metrics.get("elapsed_seconds"),
            "aggregate_request_work_seconds": metrics.get("aggregate_request_work_seconds"),
            "parallel_speedup_ratio": metrics.get("parallel_speedup_ratio"),
            "api_call_count": len(api_calls),
            "api_latency_ms": {
                "min": min(elapsed_ms) if elapsed_ms else None,
                "mean": sum(elapsed_ms) / len(elapsed_ms) if elapsed_ms else None,
                "p50": _percentile(elapsed_ms, 0.50),
                "p95": _percentile(elapsed_ms, 0.95),
                "max": max(elapsed_ms) if elapsed_ms else None,
            },
            "bar_count": int(payload.get("bar_count") or 0),
            "bar_symbol_count": int(payload.get("bar_symbol_count") or 0),
            "positive_volume_bar_count": len(positive_volume_rows),
            "zero_volume_bar_count": int(payload.get("bar_count") or 0) - len(positive_volume_rows),
            "zero_volume_bar_ratio": (
                (int(payload.get("bar_count") or 0) - len(positive_volume_rows))
                / int(payload.get("bar_count") or 0)
                if int(payload.get("bar_count") or 0) > 0
                else 0.0
            ),
            "positive_volume_bar_symbol_count": len(positive_volume_symbols),
            "missing_positive_volume_bar_symbols": sorted(set(symbols) - positive_volume_symbols),
            "trade_session_counts": metrics.get("trade_session_counts") or {},
            "rows_per_symbol": dict(sorted(rows_per_symbol.items())),
            "missing_bar_symbols": list(payload.get("missing_bar_symbols") or []),
            "error_count": len(payload.get("errors") or []),
            "rate_limit_error_count": int(metrics.get("rate_limit_error_count") or 0),
            "gate_status": gate["status"],
            "artifact_path": artifact_path.as_posix(),
            "artifact_sha256": _sha256(artifact_path),
            "completeness_path": gate_path.as_posix(),
            "completeness_sha256": _sha256(gate_path),
        }
        _write_json(run_dir / "benchmark_summary.json", summary)
        summaries.append(summary)

    comparison = {
        "schema_version": "1.0",
        "artifact_type": "longbridge_intraday_bar_benchmark_comparison",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "read_only": True,
        "orders_submitted": False,
        "target_symbol_count": len(symbols),
        "session_date": session_date.isoformat(),
        "runs": summaries,
        "passing_worker_counts": [
            row["configured_worker_count"] for row in summaries if row["gate_status"] == "pass"
        ],
    }
    _write_json(output_root / "comparison.json", comparison)
    print(json.dumps(comparison, indent=2, ensure_ascii=False))
    return 0 if all(row["gate_status"] == "pass" for row in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
