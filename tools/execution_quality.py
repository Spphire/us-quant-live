"""
Execution quality analytics for post-trade review.

Reads an execute run's execution_records.json (+ execution_summary.json) and
computes detailed per-order and aggregate quality metrics for later analysis:

  - Fill rate (count and notional weighted)
  - Slippage in bps (filled_avg_price vs reference_price, signed by side so that
    positive slippage always means "we paid worse than reference")
  - Tracking error: unfilled notional vs planned notional
  - Cancellation attribution (how many attempts before cancel, per stage)
  - Per-stage breakdown (staged Reg T: release_sell_long / release_buy_to_cover /
    entry)

This is a READ-ONLY analyzer. It does not place, modify, or cancel any orders.
It writes an execution_quality.json next to the records so the dashboard and
future backtests can consume a consistent quality record per session.

Usage:
    # Analyze one run:
    python tools/execution_quality.py --run-dir artifacts/daily_alpaca_scheduler/20260630_execute

    # Analyze all execute runs and print a summary table:
    python tools/execution_quality.py --all
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHED_ROOT = PROJECT_ROOT / "artifacts" / "daily_alpaca_scheduler"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _signed_slippage_bps(side: str, reference_price: float, filled_avg_price: float) -> float | None:
    """Slippage in bps, signed so POSITIVE = worse than reference (a cost).

    - Buy: paying above reference is a cost  -> (fill - ref)/ref
    - Sell: selling below reference is a cost -> (ref - fill)/ref
    """
    if reference_price <= 0 or filled_avg_price <= 0:
        return None
    if str(side).lower() == "buy":
        raw = (filled_avg_price - reference_price) / reference_price
    else:  # sell / short
        raw = (reference_price - filled_avg_price) / reference_price
    return raw * 10_000.0


def _logical_order_key(rec: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(rec.get("symbol", "?")).upper(),
        str(rec.get("side", "?")).lower(),
        str(rec.get("stage", "single_pass")),
    )


def _logical_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rec in records:
        if isinstance(rec, dict):
            grouped.setdefault(_logical_order_key(rec), []).append(rec)

    logical: list[dict[str, Any]] = []
    for key, items in grouped.items():
        items_sorted = sorted(
            items,
            key=lambda item: (
                int(_safe_float(item.get("release_round"))),
                int(_safe_float(item.get("attempt_count"))),
                str(item.get("updated_at") or item.get("submitted_at_utc") or ""),
            ),
        )
        symbol, side, stage = key
        req_qty = max(abs(_safe_float(item.get("qty"))) for item in items_sorted)
        fill_qty = sum(abs(_safe_float(item.get("filled_qty"))) for item in items_sorted)
        latest = items_sorted[-1]
        filled_items = [item for item in items_sorted if abs(_safe_float(item.get("filled_qty"))) > 0]
        chosen = filled_items[-1] if filled_items else latest

        order_planned_notional = max(
            (
                abs(_safe_float(item.get("qty")))
                * _safe_float(item.get("reference_price"))
                if _safe_float(item.get("reference_price")) > 0
                else abs(_safe_float(item.get("delta_notional")))
            )
            for item in items_sorted
        )
        filled_notional = sum(
            abs(_safe_float(item.get("filled_qty")))
            * (
                _safe_float(item.get("filled_avg_price"))
                if _safe_float(item.get("filled_avg_price")) > 0
                else _safe_float(item.get("reference_price"))
            )
            for item in items_sorted
        )
        reference_notional = sum(
            abs(_safe_float(item.get("filled_qty"))) * _safe_float(item.get("reference_price"))
            for item in items_sorted
        )
        weighted_slippage_sum = 0.0
        weighted_slippage_notional = 0.0
        for item in items_sorted:
            item_fill_qty = abs(_safe_float(item.get("filled_qty")))
            item_fill_px = _safe_float(item.get("filled_avg_price"))
            item_ref = _safe_float(item.get("reference_price"))
            item_notional = item_fill_qty * (item_fill_px if item_fill_px > 0 else item_ref)
            slip = _signed_slippage_bps(side, item_ref, item_fill_px)
            if slip is not None and item_notional > 0:
                weighted_slippage_sum += slip * item_notional
                weighted_slippage_notional += item_notional

        remaining_qty = max(0.0, req_qty - fill_qty)
        status = str(latest.get("status_latest", "unknown")).lower()
        if req_qty > 0 and remaining_qty <= max(1e-6, req_qty * 1e-6):
            status = "filled"
            remaining_qty = 0.0
        elif fill_qty > 0:
            status = "partial_fill"

        raw_status_counts: dict[str, int] = {}
        for item in items_sorted:
            raw_status = str(item.get("status_latest") or item.get("status") or "unknown").lower()
            raw_status_counts[raw_status] = raw_status_counts.get(raw_status, 0) + 1

        logical.append(
            {
                "symbol": symbol,
                "side": side,
                "stage": stage,
                "status_latest": status,
                "qty": req_qty,
                "filled_qty": fill_qty,
                "remaining_qty": remaining_qty,
                "filled_avg_price": filled_notional / fill_qty if fill_qty > 0 else 0.0,
                "reference_price": _safe_float(chosen.get("reference_price") or latest.get("reference_price")),
                "planned_notional": order_planned_notional,
                "filled_notional": filled_notional,
                "reference_filled_notional": reference_notional,
                "slippage_bps": (
                    weighted_slippage_sum / weighted_slippage_notional
                    if weighted_slippage_notional > 0
                    else None
                ),
                "attempt_count": sum(int(_safe_float(item.get("attempt_count"))) for item in items_sorted),
                "raw_record_count": len(items_sorted),
                "latest_status_raw": str(latest.get("status_latest", "")),
                "raw_status_counts": raw_status_counts,
            }
        )
    return logical


def analyze_run(run_dir: Path) -> dict[str, Any]:
    """Compute execution-quality metrics for a single execute run directory."""
    records_path = run_dir / "execution_records.json"
    summary_path = run_dir / "execution_summary.json"

    if not records_path.exists():
        return {"run_dir": run_dir.name, "error": "execution_records.json not found"}

    try:
        records = json.loads(records_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"run_dir": run_dir.name, "error": f"cannot parse records: {exc}"}

    if not isinstance(records, list):
        return {"run_dir": run_dir.name, "error": "records is not a list", "records_type": str(type(records))}

    summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {}

    raw_record_count = len(records)
    logical_records = _logical_records(records)
    total_orders = len(logical_records)
    filled_orders = 0
    canceled_orders = 0
    other_orders = 0

    planned_notional = 0.0
    filled_notional = 0.0

    slippage_samples: list[float] = []          # per-order signed slippage bps
    slippage_notional_weighted_sum = 0.0
    slippage_notional_weight = 0.0

    per_stage: dict[str, dict[str, Any]] = {}
    cancel_attribution: dict[str, int] = {}     # attempts-count -> how many cancels
    per_order: list[dict[str, Any]] = []

    superseded_canceled_records = 0
    for rec in logical_records:
        symbol = str(rec.get("symbol", "?"))
        side = str(rec.get("side", "?"))
        status = str(rec.get("status_latest", "unknown")).lower()
        stage = str(rec.get("stage", "single_pass"))
        ref = _safe_float(rec.get("reference_price"))
        req_qty = abs(_safe_float(rec.get("qty")))
        fill_qty = abs(_safe_float(rec.get("filled_qty")))
        fill_px = _safe_float(rec.get("filled_avg_price"))
        attempts = int(rec.get("attempt_count") or len(rec.get("attempts", []) or []))

        order_planned_notional = _safe_float(rec.get("planned_notional"))
        order_filled_notional = _safe_float(rec.get("filled_notional"))

        planned_notional += order_planned_notional
        filled_notional += order_filled_notional

        stage_rec = per_stage.setdefault(stage, {
            "orders": 0, "filled": 0, "canceled": 0,
            "planned_notional": 0.0, "filled_notional": 0.0,
        })
        stage_rec["orders"] += 1
        stage_rec["planned_notional"] += order_planned_notional
        stage_rec["filled_notional"] += order_filled_notional

        slip = None
        if status == "filled":
            filled_orders += 1
            stage_rec["filled"] += 1
            slip = rec.get("slippage_bps")
            if slip is not None:
                slippage_samples.append(slip)
                slippage_notional_weighted_sum += slip * order_filled_notional
                slippage_notional_weight += order_filled_notional
        elif status == "canceled":
            canceled_orders += 1
            stage_rec["canceled"] += 1
            cancel_attribution[str(attempts)] = cancel_attribution.get(str(attempts), 0) + 1
        else:
            other_orders += 1

        per_order.append({
            "symbol": symbol,
            "side": side,
            "stage": stage,
            "status": status,
            "attempts": attempts,
            "raw_record_count": rec.get("raw_record_count", 1),
            "latest_status_raw": rec.get("latest_status_raw", status),
            "raw_status_counts": rec.get("raw_status_counts", {}),
            "planned_notional": round(order_planned_notional, 2),
            "filled_notional": round(order_filled_notional, 2),
            "reference_price": ref,
            "filled_avg_price": fill_px,
            "slippage_bps": round(slip, 2) if slip is not None else None,
        })
        if status in {"filled", "partial_fill"} and int(rec.get("raw_record_count", 1)) > 1:
            raw_status_counts = rec.get("raw_status_counts", {})
            if isinstance(raw_status_counts, dict):
                superseded_canceled_records += int(raw_status_counts.get("canceled", 0))

    fill_rate_count = (filled_orders / total_orders) if total_orders else 0.0
    fill_rate_notional = (filled_notional / planned_notional) if planned_notional > 0 else 0.0
    unfilled_notional = max(0.0, planned_notional - filled_notional)

    avg_slippage_bps = (sum(slippage_samples) / len(slippage_samples)) if slippage_samples else None
    notional_wtd_slippage_bps = (
        slippage_notional_weighted_sum / slippage_notional_weight
        if slippage_notional_weight > 0 else None
    )
    worst_slippage = max(slippage_samples) if slippage_samples else None  # most costly
    best_slippage = min(slippage_samples) if slippage_samples else None   # most favorable

    # Round per-stage notionals
    for s in per_stage.values():
        s["planned_notional"] = round(s["planned_notional"], 2)
        s["filled_notional"] = round(s["filled_notional"], 2)

    return {
        "run_dir": run_dir.name,
        "session_date": summary.get("decision_date") or summary.get("session_date"),
        "equity_before": summary.get("account_equity"),
        "equity_after": summary.get("account_equity_post_trade"),
        "submit_error_count": summary.get("submit_error_count", 0),
        "entry_aborted": summary.get("staged_diagnostics", {}).get("entry_aborted"),
        "counts": {
            "total_orders": total_orders,
            "filled": filled_orders,
            "canceled": canceled_orders,
            "other": other_orders,
            "raw_record_count": raw_record_count,
            "superseded_record_count": max(0, raw_record_count - total_orders),
            "superseded_canceled_record_count": superseded_canceled_records,
        },
        "fill_rate_count": round(fill_rate_count, 4),
        "fill_rate_notional": round(fill_rate_notional, 4),
        "planned_notional": round(planned_notional, 2),
        "filled_notional": round(filled_notional, 2),
        "unfilled_notional": round(unfilled_notional, 2),
        "slippage_bps": {
            "avg_equal_weight": round(avg_slippage_bps, 2) if avg_slippage_bps is not None else None,
            "avg_notional_weighted": round(notional_wtd_slippage_bps, 2) if notional_wtd_slippage_bps is not None else None,
            "worst": round(worst_slippage, 2) if worst_slippage is not None else None,
            "best": round(best_slippage, 2) if best_slippage is not None else None,
            "sample_count": len(slippage_samples),
        },
        "cancel_attribution_by_attempts": cancel_attribution,
        "per_stage": per_stage,
        "per_order": per_order,
    }


def write_quality_file(run_dir: Path) -> dict[str, Any]:
    result = analyze_run(run_dir)
    out_path = run_dir / "execution_quality.json"
    try:
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        result["_written_to"] = str(out_path)
    except Exception as exc:
        result["_write_error"] = str(exc)
    return result


def _print_summary_row(r: dict[str, Any]) -> None:
    if r.get("error"):
        print(f"  {r['run_dir']:24} ERROR: {r['error']}")
        return
    c = r["counts"]
    slip = r["slippage_bps"]
    print(
        f"  {r['run_dir']:22} "
        f"orders={c['total_orders']:3d} "
        f"fill={r['fill_rate_count']*100:5.1f}%(cnt)/{r['fill_rate_notional']*100:5.1f}%(notl) "
        f"slip_nw={_fmt(slip['avg_notional_weighted'])}bps "
        f"worst={_fmt(slip['worst'])}bps "
        f"unfilled=${r['unfilled_notional']:,.0f}"
    )


def _fmt(v: Any) -> str:
    return f"{v:+.1f}" if isinstance(v, (int, float)) else " n/a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execution quality analytics (read-only)")
    parser.add_argument("--run-dir", default=None, help="A single execute run directory to analyze")
    parser.add_argument("--all", action="store_true", help="Analyze all *_execute run directories")
    parser.add_argument("--no-write", action="store_true", help="Do not write execution_quality.json, just print")
    args = parser.parse_args(argv)

    run_dirs: list[Path] = []
    if args.run_dir:
        run_dirs = [Path(args.run_dir).resolve()]
    elif args.all:
        run_dirs = sorted(SCHED_ROOT.glob("*_execute"))
    else:
        parser.error("provide --run-dir <dir> or --all")

    if not run_dirs:
        print("No execute run directories found.")
        return 1

    print("=" * 100)
    print("Execution Quality Report  (slippage: positive = paid worse than reference)")
    print("=" * 100)
    for run_dir in run_dirs:
        if args.no_write:
            result = analyze_run(run_dir)
        else:
            result = write_quality_file(run_dir)
        _print_summary_row(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
