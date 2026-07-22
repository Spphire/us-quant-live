"""Daily audit-trail package generator for live Alpaca sessions.

This read-only tool consolidates the artifacts emitted by the daily decision and
execute runs into a stable, review-friendly audit directory.  It intentionally
uses only files already written by the trading workflow; failures here must never
place, modify, or cancel orders.

Usage:
    python tools/daily_audit_report.py --run-dir artifacts/daily_alpaca_scheduler/20260706_execute
    python tools/daily_audit_report.py --all
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import platform
import subprocess
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHED_ROOT = PROJECT_ROOT / "artifacts" / "daily_alpaca_scheduler"
FACTOR_COLUMNS = [
    "reversal_score",
    "momentum_score",
    "small_size_score",
    "low_beta_score",
    "cash_quality_score",
]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric):
        return None
    return numeric


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _embedded_json_object(text: Any) -> dict[str, Any]:
    raw = str(text or "")
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _broker_error_payload(*items: Mapping[str, Any]) -> dict[str, Any]:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("broker_error_payload")
        if isinstance(raw, dict):
            return dict(raw)
        parsed = _embedded_json_object(item.get("error"))
        if parsed:
            return parsed
    return {}


def _submit_error_class_from_payload(payload: Mapping[str, Any], error: Any = "") -> str:
    message = str(payload.get("message") or "") if isinstance(payload, Mapping) else ""
    error_text = "" if _is_missing(error) else str(error)
    text = f"{message} {error_text}".lower()
    if "insufficient qty available" in text:
        return "insufficient_qty_available"
    if "insufficient buying power" in text or "insufficient day trading buying power" in text:
        return "insufficient_buying_power"
    if payload or text.strip():
        return "alpaca_submit_error"
    return ""


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    rows = list(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        rel = path.relative_to(root)
    except Exception:
        stat = None
        rel = path
    return {
        "path": path.as_posix(),
        "relative_path": rel.as_posix(),
        "exists": path.exists(),
        "bytes": int(stat.st_size) if stat else None,
        "modified_at_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
        if stat
        else None,
        "sha256": _sha256_file(path) if path.exists() and path.is_file() else None,
    }


def _inventory_files(root: Path, *, exclude_dirs: set[str] | None = None) -> list[dict[str, Any]]:
    exclude_dirs = set(exclude_dirs or set())
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except Exception:
            rel_parts = path.parts
        if rel_parts and rel_parts[0] in exclude_dirs:
            continue
        out.append(_file_entry(path, root))
    return out


def _write_audit_manifest(
    audit_dir: Path,
    run_dir: Path,
    context: dict[str, Any],
    decision_dir: Path | None = None,
) -> dict[str, Any]:
    manifest_path = audit_dir / "12_audit_manifest.json"
    audit_files = [
        entry
        for entry in _inventory_files(audit_dir)
        if Path(entry["path"]).resolve() != manifest_path.resolve()
    ]
    run_files = _inventory_files(run_dir, exclude_dirs={"audit"})
    decision_files = _inventory_files(decision_dir, exclude_dirs={"audit"}) if decision_dir else []
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_dir": run_dir.as_posix(),
        "decision_dir": decision_dir.as_posix() if decision_dir else None,
        "audit_dir": audit_dir.as_posix(),
        "session_date": context.get("session_date"),
        "run_file_count": len(run_files),
        "decision_file_count": len(decision_files),
        "audit_file_count": len(audit_files),
        "run_files": run_files,
        "decision_files": decision_files,
        "audit_files": audit_files,
    }
    _write_json(manifest_path, payload)
    return payload


def _path_exists(raw: Any) -> bool:
    if raw in (None, "", "null"):
        return False
    try:
        return Path(str(raw)).exists()
    except Exception:
        return False


def _record_order_ids(records: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for record in records if isinstance(records, list) else []:
        candidates = [record.get("order_id")]
        attempts = record.get("attempts")
        if isinstance(attempts, list):
            candidates.extend(attempt.get("order_id") for attempt in attempts if isinstance(attempt, dict))
        for raw in candidates:
            order_id = str(raw or "").strip()
            if order_id and order_id not in seen:
                seen.add(order_id)
                out.append(order_id)
    return out


def _build_order_poll_rows(order_poll_timeline: dict[str, Any]) -> list[dict[str, Any]]:
    events = order_poll_timeline.get("events", []) if isinstance(order_poll_timeline, dict) else []
    rows: list[dict[str, Any]] = []
    for idx, event in enumerate(events if isinstance(events, list) else [], start=1):
        if not isinstance(event, dict):
            continue
        row = dict(event)
        row.setdefault("timeline_row", idx)
        rows.append(row)
    return rows


def _fill_stats_by_order(fill_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"fill_count": 0, "fill_qty": 0.0, "fill_notional": 0.0})
    for fill in fill_rows:
        order_id = str(fill.get("order_id") or "").strip()
        if not order_id:
            continue
        qty = _safe_float(fill.get("qty"))
        price = _safe_float(fill.get("price"))
        bucket = stats[order_id]
        bucket["fill_count"] += 1
        bucket["fill_qty"] += qty
        bucket["fill_notional"] += qty * price
    for bucket in stats.values():
        qty = _safe_float(bucket.get("fill_qty"))
        bucket["fill_vwap"] = _safe_float(bucket.get("fill_notional")) / qty if qty > 0 else None
    return stats


def _build_order_attempt_rows(records: list[dict[str, Any]], fill_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fill_stats = _fill_stats_by_order(fill_rows)
    rows: list[dict[str, Any]] = []
    for record_index, record in enumerate(records if isinstance(records, list) else [], start=1):
        base = {
            "record_index": int(record_index),
            "symbol": str(record.get("symbol") or ""),
            "side": str(record.get("side") or ""),
            "stage": str(record.get("stage") or ""),
            "release_round": record.get("release_round", ""),
            "execution_order_style": str(record.get("execution_order_style") or ""),
            "record_client_order_id": str(record.get("client_order_id") or ""),
            "record_order_id": str(record.get("order_id") or ""),
            "record_status_latest": str(record.get("status_latest") or ""),
            "record_qty": _safe_float(record.get("qty")),
            "record_filled_qty": _safe_float(record.get("filled_qty")),
            "record_remaining_qty": _safe_float(record.get("remaining_qty")),
            "record_reference_price": _safe_float(record.get("reference_price")),
            "record_delta_notional": _safe_float(record.get("delta_notional")),
            "record_submitted_at_utc": record.get("submitted_at_utc", ""),
            "record_updated_at": record.get("updated_at", ""),
            "record_attempt_count": _safe_int(record.get("attempt_count")),
        }
        attempts = record.get("attempts")
        if isinstance(attempts, list) and attempts:
            for attempt_index, attempt in enumerate(attempts, start=1):
                if not isinstance(attempt, dict):
                    continue
                order_id = str(attempt.get("order_id") or "")
                stats = fill_stats.get(order_id, {})
                error_payload = _broker_error_payload(attempt, record)
                rows.append(
                    {
                        **base,
                        "attempt_index": int(attempt_index),
                        "attempt_no": _safe_int(attempt.get("attempt_no"), default=attempt_index),
                        "attempt_client_order_id": str(attempt.get("client_order_id") or ""),
                        "attempt_order_id": order_id,
                        "qty_submitted": _safe_float(attempt.get("qty_submitted")),
                        "limit_price": _safe_float(attempt.get("limit_price")),
                        "offset_bps": _safe_float(attempt.get("offset_bps")),
                        "requote_step_index": _safe_int(attempt.get("requote_step_index"), default="")
                        if not _is_missing(attempt.get("requote_step_index"))
                        else "",
                        "requote_cycle": _safe_int(attempt.get("requote_cycle"), default="")
                        if not _is_missing(attempt.get("requote_cycle"))
                        else "",
                        "max_offset_bps": _optional_float(attempt.get("max_offset_bps")),
                        "status_latest": str(attempt.get("status_latest") or ""),
                        "filled_qty": _safe_float(attempt.get("filled_qty")),
                        "remaining_qty_estimate": max(
                            0.0,
                            _safe_float(attempt.get("qty_submitted")) - _safe_float(attempt.get("filled_qty")),
                        ),
                        "filled_avg_price": _safe_float(attempt.get("filled_avg_price")),
                        "updated_at": attempt.get("updated_at", ""),
                        "broker_fill_count": _safe_int(stats.get("fill_count")),
                        "broker_fill_qty": _safe_float(stats.get("fill_qty")),
                        "broker_fill_vwap": stats.get("fill_vwap"),
                        "broker_fill_notional": _safe_float(stats.get("fill_notional")),
                        "poll_event_count": _safe_int(attempt.get("poll_event_count")),
                        "requested_qty": _safe_float(attempt.get("requested_qty") or attempt.get("qty_submitted")),
                        "submit_error_class": attempt.get("submit_error_class")
                        or record.get("submit_error_class")
                        or _submit_error_class_from_payload(error_payload, attempt.get("error") or record.get("error")),
                        "broker_error_code": attempt.get("broker_error_code") or record.get("broker_error_code") or error_payload.get("code") or "",
                        "broker_error_message": attempt.get("broker_error_message") or record.get("broker_error_message") or error_payload.get("message") or "",
                        "broker_available_qty": _optional_float(
                            attempt.get("broker_available_qty") or record.get("broker_available_qty") or error_payload.get("available")
                        ),
                        "broker_existing_qty": _optional_float(
                            attempt.get("broker_existing_qty") or record.get("broker_existing_qty") or error_payload.get("existing_qty")
                        ),
                        "broker_held_for_orders_qty": _optional_float(
                            attempt.get("broker_held_for_orders_qty")
                            or record.get("broker_held_for_orders_qty")
                            or error_payload.get("held_for_orders")
                        ),
                        "abort_remaining_orders": bool(attempt.get("abort_remaining_orders") or record.get("abort_remaining_orders")),
                        "error_type": attempt.get("error_type") or record.get("error_type") or "",
                        "error": attempt.get("error") or record.get("error") or "",
                    }
                )
        else:
            order_id = str(record.get("order_id") or "")
            stats = fill_stats.get(order_id, {})
            error_payload = _broker_error_payload(record)
            rows.append(
                {
                    **base,
                    "attempt_index": 1,
                    "attempt_no": 1,
                    "attempt_client_order_id": str(record.get("client_order_id") or ""),
                    "attempt_order_id": order_id,
                    "qty_submitted": _safe_float(record.get("qty")),
                    "limit_price": "",
                    "offset_bps": "",
                    "requote_step_index": "",
                    "requote_cycle": "",
                    "max_offset_bps": _optional_float(record.get("marketable_limit_max_offset_bps")),
                    "status_latest": str(record.get("status_latest") or ""),
                    "filled_qty": _safe_float(record.get("filled_qty")),
                    "remaining_qty_estimate": _safe_float(record.get("remaining_qty")),
                    "filled_avg_price": _safe_float(record.get("filled_avg_price")),
                    "updated_at": record.get("updated_at", ""),
                    "broker_fill_count": _safe_int(stats.get("fill_count")),
                    "broker_fill_qty": _safe_float(stats.get("fill_qty")),
                    "broker_fill_vwap": stats.get("fill_vwap"),
                    "broker_fill_notional": _safe_float(stats.get("fill_notional")),
                    "poll_event_count": _safe_int(record.get("poll_event_count")),
                    "requested_qty": _safe_float(record.get("requested_qty") or record.get("qty")),
                    "submit_error_class": record.get("submit_error_class")
                    or _submit_error_class_from_payload(error_payload, record.get("error")),
                    "broker_error_code": record.get("broker_error_code") or error_payload.get("code") or "",
                    "broker_error_message": record.get("broker_error_message") or error_payload.get("message") or "",
                    "broker_available_qty": _optional_float(record.get("broker_available_qty") or error_payload.get("available")),
                    "broker_existing_qty": _optional_float(record.get("broker_existing_qty") or error_payload.get("existing_qty")),
                    "broker_held_for_orders_qty": _optional_float(
                        record.get("broker_held_for_orders_qty") or error_payload.get("held_for_orders")
                    ),
                    "abort_remaining_orders": bool(record.get("abort_remaining_orders")),
                    "error_type": record.get("error_type", ""),
                    "error": record.get("error", ""),
                }
            )
    return rows


def _activity_list_from_payload(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get(key)
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _build_broker_activity_trace(
    *,
    broker_fills: dict[str, Any],
    broker_account_activities: dict[str, Any],
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    order_ids = set(_record_order_ids(records))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    matched_activity_ids = {
        str(item.get("id") or "")
        for item in _activity_list_from_payload(broker_fills, "matched_activities")
        if str(item.get("id") or "")
    }
    unmatched_activity_ids = {
        str(item.get("id") or "")
        for item in _activity_list_from_payload(broker_fills, "unmatched_same_day_symbol_activities")
        if str(item.get("id") or "")
    }

    def add_rows(source: str, activities: list[dict[str, Any]], matched: str = "") -> None:
        for activity in activities:
            activity_id = str(activity.get("id") or "")
            key = activity_id or f"{source}:{json.dumps(activity, sort_keys=True, default=str)}"
            if key in seen:
                continue
            seen.add(key)
            order_id = str(activity.get("order_id") or "")
            matched_scope = matched
            if activity_id in matched_activity_ids:
                matched_scope = "matched_order_id"
            elif activity_id in unmatched_activity_ids:
                matched_scope = "unmatched_same_day_symbol"
            rows.append(
                {
                    "source": source,
                    "matched_scope": matched_scope,
                    "activity_id": activity_id,
                    "activity_type": activity.get("activity_type", ""),
                    "type": activity.get("type", ""),
                    "transaction_time": activity.get("transaction_time") or activity.get("date") or "",
                    "symbol": str(activity.get("symbol") or "").upper(),
                    "side": str(activity.get("side") or "").lower(),
                    "qty": _safe_float(activity.get("qty")),
                    "price": _safe_float(activity.get("price")),
                    "order_id": order_id,
                    "order_status": activity.get("order_status", ""),
                    "leaves_qty": _safe_float(activity.get("leaves_qty")),
                    "cum_qty": _safe_float(activity.get("cum_qty")),
                    "net_amount": _safe_float(activity.get("net_amount")),
                    "gross_amount": _safe_float(activity.get("gross_amount")),
                    "in_execution_records": bool(order_id and order_id in order_ids),
                }
            )

    add_rows("broker_fill_activities.activities", _activity_list_from_payload(broker_fills, "activities"), "all_fill")
    add_rows("broker_fill_activities.matched_activities", _activity_list_from_payload(broker_fills, "matched_activities"), "matched_order_id")
    add_rows(
        "broker_fill_activities.unmatched_same_day_symbol_activities",
        _activity_list_from_payload(broker_fills, "unmatched_same_day_symbol_activities"),
        "unmatched_same_day_symbol",
    )
    payload = broker_account_activities.get("payload") if isinstance(broker_account_activities, dict) else None
    if isinstance(payload, list):
        add_rows("broker_account_activities.payload", [dict(item) for item in payload if isinstance(item, dict)], "all_activity")

    by_source = Counter(str(row.get("source") or "") for row in rows)
    by_activity_type = Counter(str(row.get("activity_type") or "__missing__") for row in rows)
    by_side = Counter(str(row.get("side") or "__missing__") for row in rows)
    by_symbol = Counter(str(row.get("symbol") or "__missing__") for row in rows)
    net_amount_by_activity_type: dict[str, float] = defaultdict(float)
    gross_amount_by_activity_type: dict[str, float] = defaultdict(float)
    for row in rows:
        activity_type = str(row.get("activity_type") or "__missing__")
        net_amount_by_activity_type[activity_type] += _safe_float(row.get("net_amount"))
        gross_amount_by_activity_type[activity_type] += _safe_float(row.get("gross_amount"))
    matched_order_ids = {str(row.get("order_id") or "") for row in rows if row.get("in_execution_records")}
    fill_rows = [row for row in rows if str(row.get("activity_type") or "").upper() == "FILL" or "fill" in str(row.get("source") or "")]
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_count": len(rows),
        "sources": dict(sorted(by_source.items())),
        "activity_type_counts": dict(sorted(by_activity_type.items())),
        "net_amount_by_activity_type": dict(sorted(net_amount_by_activity_type.items())),
        "gross_amount_by_activity_type": dict(sorted(gross_amount_by_activity_type.items())),
        "side_counts": dict(sorted(by_side.items())),
        "top_symbols": dict(by_symbol.most_common(30)),
        "execution_order_id_count": len(order_ids),
        "activity_order_ids_in_execution_records": len(matched_order_ids),
        "fill_activity_rows": len(fill_rows),
        "fill_qty_abs": sum(abs(_safe_float(row.get("qty"))) for row in fill_rows),
        "fill_notional_abs": sum(abs(_safe_float(row.get("qty")) * _safe_float(row.get("price"))) for row in fill_rows),
    }
    return rows, summary


def _safe_payload_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("payload")
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, dict)]
        if isinstance(raw, dict):
            orders = raw.get("orders")
            if isinstance(orders, list):
                return [dict(item) for item in orders if isinstance(item, dict)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


def _safe_order_page_meta(payload: Any) -> dict[str, Any]:
    raw = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        "page_count": _safe_int(raw.get("page_count")),
        "order_count": _safe_int(raw.get("order_count")),
        "truncated": bool(raw.get("truncated")),
        "page_limit": _safe_int(raw.get("page_limit")),
        "max_pages": _safe_int(raw.get("max_pages")),
    }


def _order_snapshot_rows_from_orders(source: str, orders: list[dict[str, Any]], order_ids: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, order in enumerate(orders, start=1):
        order_id = str(order.get("id") or "").strip()
        rows.append(
            {
                "source": source,
                "source_index": int(idx),
                "order_id": order_id,
                "client_order_id": str(order.get("client_order_id") or ""),
                "symbol": str(order.get("symbol") or "").upper(),
                "side": str(order.get("side") or "").lower(),
                "order_type": str(order.get("type") or ""),
                "time_in_force": str(order.get("time_in_force") or ""),
                "status": str(order.get("status") or ""),
                "qty": _safe_float(order.get("qty")),
                "filled_qty": _safe_float(order.get("filled_qty")),
                "filled_avg_price": _safe_float(order.get("filled_avg_price")),
                "limit_price": _safe_float(order.get("limit_price")),
                "notional": _safe_float(order.get("notional")),
                "created_at": order.get("created_at", ""),
                "submitted_at": order.get("submitted_at", ""),
                "updated_at": order.get("updated_at", ""),
                "filled_at": order.get("filled_at", ""),
                "canceled_at": order.get("canceled_at", ""),
                "expired_at": order.get("expired_at", ""),
                "failed_at": order.get("failed_at", ""),
                "in_execution_records": bool(order_id and order_id in order_ids),
            }
        )
    return rows


def _build_broker_order_universe(
    *,
    run_dir: Path,
    broker_order_snapshots: dict[str, Any],
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    order_ids = set(_record_order_ids(records))
    source_payloads = {
        "broker_order_snapshots.snapshots": broker_order_snapshots.get("snapshots", [])
        if isinstance(broker_order_snapshots, dict)
        else [],
        "broker_orders_all_before.payload": _safe_payload_list(_read_json(run_dir / "broker_orders_all_before.json", {})),
        "broker_orders_all_before_submit.payload": _safe_payload_list(
            _read_json(run_dir / "broker_orders_all_before_submit.json", {})
        ),
        "broker_orders_all_after_cancel.payload": _safe_payload_list(
            _read_json(run_dir / "broker_orders_all_after_cancel.json", {})
        ),
        "broker_orders_all_after.payload": _safe_payload_list(_read_json(run_dir / "broker_orders_all_after.json", {})),
    }
    page_metas = {
        key: meta
        for key, meta in {
            "broker_orders_all_before": _safe_order_page_meta(_read_json(run_dir / "broker_orders_all_before.json", {})),
            "broker_orders_all_before_submit": _safe_order_page_meta(
                _read_json(run_dir / "broker_orders_all_before_submit.json", {})
            ),
            "broker_orders_all_after_cancel": _safe_order_page_meta(
                _read_json(run_dir / "broker_orders_all_after_cancel.json", {})
            ),
            "broker_orders_all_after": _safe_order_page_meta(_read_json(run_dir / "broker_orders_all_after.json", {})),
        }.items()
        if meta
    }
    rows: list[dict[str, Any]] = []
    for source, orders in source_payloads.items():
        rows.extend(_order_snapshot_rows_from_orders(source, orders if isinstance(orders, list) else [], order_ids))

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        unique[order_id] = row
    status_counts = Counter(str(row.get("status") or "__missing__") for row in unique.values())
    side_counts = Counter(str(row.get("side") or "__missing__") for row in unique.values())
    source_counts = Counter(str(row.get("source") or "__missing__") for row in rows)
    missing_order_ids = sorted(order_id for order_id in order_ids if order_id not in unique)
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_row_count": len(rows),
        "unique_order_id_count": len(unique),
        "execution_order_id_count": len(order_ids),
        "execution_order_ids_missing_from_universe": missing_order_ids[:100],
        "execution_order_ids_missing_from_universe_count": len(missing_order_ids),
        "source_counts": dict(sorted(source_counts.items())),
        "paged_order_capture_meta": page_metas,
        "paged_order_capture_truncated_sources": sorted(
            key for key, meta in page_metas.items() if bool(meta.get("truncated"))
        ),
        "status_counts_unique_orders": dict(sorted(status_counts.items())),
        "side_counts_unique_orders": dict(sorted(side_counts.items())),
        "submitted_order_unique_count": sum(1 for row in unique.values() if row.get("in_execution_records")),
        "filled_qty_total_abs": sum(abs(_safe_float(row.get("filled_qty"))) for row in unique.values()),
        "filled_notional_total_abs": sum(
            abs(_safe_float(row.get("filled_qty")) * _safe_float(row.get("filled_avg_price")))
            for row in unique.values()
        ),
    }
    return rows, summary


def _json_cell(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _artifact_summary(path: Path, root: Path) -> dict[str, Any]:
    entry = _file_entry(path, root)
    parsed = _read_json(path, None)
    payload = parsed.get("payload") if isinstance(parsed, dict) and "payload" in parsed else parsed
    out = {
        "exists": bool(path.exists()),
        "path": path.as_posix(),
        "relative_path": entry.get("relative_path"),
        "bytes": entry.get("bytes"),
        "sha256": entry.get("sha256"),
        "json_type": type(parsed).__name__ if parsed is not None else "",
        "payload_type": type(payload).__name__ if payload is not None else "",
        "payload_count": len(payload) if isinstance(payload, (list, dict)) else 0,
    }
    if isinstance(parsed, dict) and "ok" in parsed:
        out["ok"] = bool(parsed.get("ok"))
        out["error_type"] = parsed.get("error_type")
        out["error"] = parsed.get("error")
    return out


def _jsonl_line_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "line_count": 0, "parse_error_count": 0}
    line_count = 0
    parse_error_count = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                line_count += 1
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    parse_error_count += 1
    except Exception:
        parse_error_count += 1
    return {"exists": True, "line_count": line_count, "parse_error_count": parse_error_count}


def _build_run_evidence_digest_audit(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    digest_path = run_dir / "run_evidence_digest.json"
    raw_digest = _read_json(digest_path, {})
    digest = raw_digest if isinstance(raw_digest, dict) else {}
    expected_files = [
        "scheduler_task_context.json",
        "scheduler_task_result.json",
        "execution_summary.json",
        "run_context.json",
        "run_events.jsonl",
        "runtime_environment_snapshot.json",
        "order_plan.json",
        "execution_records.json",
        "broker_account_before.json",
        "broker_account_after.json",
        "broker_positions_before_raw.json",
        "broker_positions_after_raw.json",
        "broker_position_account_stability_before.json",
        "broker_position_account_stability_after.json",
        "broker_fill_activities.json",
        "broker_account_activities.json",
        "broker_order_snapshots.json",
        "order_poll_timeline.json",
        "alpaca_api_audit.jsonl",
        "execution_price_snapshot.json",
        "execution_intraday_bars_1min.json",
        "execution_intraday_bars_1min_after.json",
        "execution_latest_quotes_snapshot.json",
        "execution_latest_quotes_snapshot_after.json",
        "broker_portfolio_history_before.json",
        "broker_portfolio_history_after.json",
        "broker_calendar_window.json",
        "broker_corporate_actions.json",
        "source_code_manifest.json",
        "source_git_snapshot.json",
        "source_git_diff.patch",
        "source_code_snapshot.zip",
        "python_environment.json",
        "file_hash_manifest.json",
        "artifact_completeness_snapshot.json",
    ]
    new_replay_files = {
        "run_events.jsonl",
        "runtime_environment_snapshot.json",
        "file_hash_manifest.json",
        "artifact_completeness_snapshot.json",
    }
    new_replay_enabled = any((run_dir / name).exists() for name in new_replay_files)
    scheduler_evidence_files = {
        "scheduler_task_context.json",
        "scheduler_task_result.json",
    }
    scheduler_evidence_enabled = any((run_dir / name).exists() for name in scheduler_evidence_files)
    strict_files = {
        "run_context.json",
        "order_plan.json",
        "execution_records.json",
        "broker_account_before.json",
        "broker_account_after.json",
        "broker_positions_before_raw.json",
        "broker_positions_after_raw.json",
        "broker_position_account_stability_before.json",
        "broker_position_account_stability_after.json",
        "broker_fill_activities.json",
        "broker_account_activities.json",
        "broker_order_snapshots.json",
        "order_poll_timeline.json",
        "alpaca_api_audit.jsonl",
        "execution_price_snapshot.json",
        "execution_intraday_bars_1min.json",
        "execution_intraday_bars_1min_after.json",
        "execution_latest_quotes_snapshot.json",
        "execution_latest_quotes_snapshot_after.json",
    }
    if new_replay_enabled:
        strict_files.update(new_replay_files)
    if scheduler_evidence_enabled:
        strict_files.update(scheduler_evidence_files)
    file_statuses = digest.get("file_statuses") if isinstance(digest.get("file_statuses"), dict) else {}
    rows: list[dict[str, Any]] = []
    for name in expected_files:
        status = file_statuses.get(name) if isinstance(file_statuses.get(name), dict) else {}
        path = run_dir / name
        if not status:
            status = _file_entry(path, run_dir)
            status["exists"] = path.exists()
        line_summary = _jsonl_line_summary(path) if path.suffix.lower() == ".jsonl" else {}
        exists = bool(status.get("exists") or path.exists())
        rows.append(
            {
                "artifact": name,
                "exists": exists,
                "strict_replay_input": name in strict_files,
                "bytes": status.get("bytes"),
                "sha256": status.get("sha256"),
                "payload_count": status.get("payload_count", ""),
                "line_count": line_summary.get("line_count", ""),
                "parse_error_count": line_summary.get("parse_error_count", ""),
                "status": "pass" if exists else "missing",
                "path": status.get("path") or path.as_posix(),
            }
        )

    present_count = sum(1 for row in rows if row.get("exists"))
    missing_rows = [row for row in rows if not row.get("exists")]
    strict_missing_rows = [row for row in missing_rows if row.get("strict_replay_input")]
    broker = digest.get("broker_state") if isinstance(digest.get("broker_state"), dict) else {}
    execution = digest.get("execution") if isinstance(digest.get("execution"), dict) else {}
    api_audit = execution.get("alpaca_api_audit") if isinstance(execution.get("alpaca_api_audit"), dict) else {}
    if not api_audit:
        api_audit = _jsonl_line_summary(run_dir / "alpaca_api_audit.jsonl")
    account_deltas = broker.get("account_field_deltas") if isinstance(broker.get("account_field_deltas"), dict) else {}
    execution_records = (
        execution.get("execution_records")
        if isinstance(execution.get("execution_records"), dict)
        else _artifact_summary(run_dir / "execution_records.json", run_dir)
    )
    runtime = digest.get("runtime") if isinstance(digest.get("runtime"), dict) else {}
    run_events = runtime.get("run_events") if isinstance(runtime.get("run_events"), dict) else {}
    if not run_events:
        run_events = _jsonl_line_summary(run_dir / "run_events.jsonl")
    hash_manifest = _read_json(run_dir / "file_hash_manifest.json", {})
    if not isinstance(hash_manifest, dict):
        hash_manifest = {}
    completeness = _read_json(run_dir / "artifact_completeness_snapshot.json", {})
    if not isinstance(completeness, dict):
        completeness = {}
    completeness_categories = (
        completeness.get("categories") if isinstance(completeness.get("categories"), dict) else {}
    )
    completeness_partial_categories = [
        str(name)
        for name, item in sorted(completeness_categories.items())
        if isinstance(item, dict) and item.get("status") != "pass"
    ]
    completeness_missing_files = [
        str(name)
        for item in completeness_categories.values()
        if isinstance(item, dict)
        for name in (item.get("missing") if isinstance(item.get("missing"), list) else [])
    ]
    coverage_ratio = present_count / len(rows) if rows else 1.0
    digest_exists = digest_path.exists()
    completeness_status = str(completeness.get("status") or "")
    has_completeness_gap = bool(
        completeness_status
        and completeness_status not in {"pass", "not_applicable"}
        and completeness_partial_categories
    )
    status = (
        "pass"
        if digest_exists and not strict_missing_rows and not has_completeness_gap
        else "partial"
        if digest_exists
        else "historical_limited"
    )
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "digest_exists": digest_exists,
        "digest_path": digest_path.as_posix(),
        "digest_status": digest.get("status", "missing"),
        "expected_file_count": len(rows),
        "present_file_count": present_count,
        "missing_file_count": len(missing_rows),
        "coverage_ratio": coverage_ratio,
        "strict_missing_file_count": len(strict_missing_rows),
        "strict_missing_files": [str(row.get("artifact")) for row in strict_missing_rows],
        "missing_files": [str(row.get("artifact")) for row in missing_rows],
        "api_audit_line_count": _safe_int(api_audit.get("line_count")),
        "api_audit_parse_error_count": _safe_int(api_audit.get("parse_error_count")),
        "run_event_count": _safe_int(run_events.get("line_count")),
        "run_event_parse_error_count": _safe_int(run_events.get("parse_error_count")),
        "file_hash_manifest_exists": bool(hash_manifest),
        "file_hash_manifest_file_count": _safe_int(hash_manifest.get("file_count")),
        "file_hash_manifest_total_bytes": _safe_int(hash_manifest.get("total_bytes")),
        "artifact_completeness_status": completeness_status or ("missing" if not completeness else ""),
        "artifact_completeness_partial_category_count": len(completeness_partial_categories),
        "artifact_completeness_partial_categories": completeness_partial_categories,
        "artifact_completeness_missing_file_count": len(completeness_missing_files),
        "artifact_completeness_missing_files": completeness_missing_files,
        "execution_record_count": _safe_int(execution_records.get("record_count") or execution_records.get("payload_count")),
        "filled_record_count": _safe_int(execution_records.get("filled_record_count")),
        "account_field_deltas": account_deltas,
        "account_equity_delta_digest": account_deltas.get("equity"),
        "account_cash_delta_digest": account_deltas.get("cash"),
        "position_symbol_union_count_digest": broker.get("position_symbol_union_count"),
        "position_symbol_added_count_digest": len(broker.get("position_symbol_added", []))
        if isinstance(broker.get("position_symbol_added"), list)
        else 0,
        "position_symbol_removed_count_digest": len(broker.get("position_symbol_removed", []))
        if isinstance(broker.get("position_symbol_removed"), list)
        else 0,
        "position_gross_market_value_abs_delta_digest": broker.get("position_gross_market_value_abs_delta"),
        "status_counts": dict(sorted(Counter(str(row.get("status") or "") for row in rows).items())),
        "note": (
            "Executor-written run_evidence_digest.json is the preferred semantic index. "
            "Historical runs without it are audited from raw file presence only."
        ),
    }
    return rows, summary


def _list_payload(raw: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _file_tail_info(path: Path, *, max_lines: int = 80, max_bytes: int = 20000) -> dict[str, Any]:
    try:
        stat = path.stat()
        data = path.read_bytes()[-max_bytes:]
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        return {
            "path": path.as_posix(),
            "exists": True,
            "bytes": int(stat.st_size),
            "modified_at_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "tail": "\n".join(lines),
            "tail_line_count": len(lines),
            "truncated": bool(stat.st_size > len(data)),
        }
    except Exception as exc:
        return {
            "path": path.as_posix(),
            "exists": path.exists(),
            "bytes": None,
            "modified_at_utc": None,
            "tail": "",
            "tail_line_count": 0,
            "truncated": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _autostart_task_status(project_root: Path) -> dict[str, Any]:
    if platform.system().lower() != "windows":
        return {"available": False, "reason": "not_windows"}
    script = project_root / "tools" / "install_autostart_task.ps1"
    if not script.exists():
        return {"available": False, "reason": "install_autostart_task_script_missing", "script": script.as_posix()}
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ProjectRoot",
        str(project_root),
        "-Status",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception as exc:
        return {
            "available": False,
            "script": script.as_posix(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return {
        "available": True,
        "script": script.as_posix(),
        "ok": result.returncode == 0,
        "returncode": int(result.returncode),
        "registered": "task registered" in stdout.lower(),
        "not_registered": "task not registered" in stdout.lower(),
        "stdout_tail": "\n".join(stdout.splitlines()[-40:]),
        "stderr_tail": "\n".join(stderr.splitlines()[-20:]),
    }


def _flatten_daemon_event(prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ["record_type", "generated_at_cn", "generated_at_utc", "session_date", "tradable", "ran_ok"]:
        if key in payload:
            out[f"{prefix}_{key}"] = payload.get(key)
    if isinstance(payload.get("decision"), dict):
        decision = payload["decision"]
        out.update(
            {
                f"{prefix}_decision_status_before": decision.get("status_before"),
                f"{prefix}_decision_action": decision.get("action"),
                f"{prefix}_decision_reason": decision.get("reason"),
                f"{prefix}_decision_due": decision.get("due"),
                f"{prefix}_decision_can_attempt": decision.get("can_attempt"),
                f"{prefix}_decision_can_attempt_reason": decision.get("can_attempt_reason"),
            }
        )
    if isinstance(payload.get("execute"), dict):
        execute = payload["execute"]
        out.update(
            {
                f"{prefix}_execute_status_before": execute.get("status_before"),
                f"{prefix}_execute_action": execute.get("action"),
                f"{prefix}_execute_reason": execute.get("reason"),
                f"{prefix}_execute_due": execute.get("due"),
                f"{prefix}_execute_can_attempt": execute.get("can_attempt"),
                f"{prefix}_execute_can_attempt_reason": execute.get("can_attempt_reason"),
            }
        )
    for key in ["event_type", "pid", "dashboard_pid", "now_cn", "now_us"]:
        if key in payload:
            out[f"{prefix}_{key}"] = payload.get(key)
    return out


def _capture_process_health(root: Path) -> dict[str, Any]:
    path = root / "process_health_latest.json"
    url = "http://127.0.0.1:18076/api/process-health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            raw = resp.read(2_000_000)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return payload
    except Exception as exc:
        payload = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "unavailable",
            "source_url": url,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return payload
    return _read_json(path, {})


def _build_startup_binding_audit(root: Path = SCHED_ROOT) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = root.resolve()
    project_root = root.parent.parent
    daemon_dir = root / "daemon"
    startup_log = daemon_dir / "startup.bat.log"
    due_latest_path = daemon_dir / "scheduler_due_latest.json"
    runtime_latest_path = daemon_dir / "scheduler_runtime_latest.json"
    process_health_path = root / "process_health_latest.json"
    start_bat = project_root / "Start.bat"
    due_latest = _read_json(due_latest_path, {})
    runtime_latest = _read_json(runtime_latest_path, {})
    process_health = _capture_process_health(root)
    autostart = _autostart_task_status(project_root)
    required_pid_paths = {
        "tray_launcher_pid": daemon_dir / "tray_launcher.pid",
        "scheduler_pid": daemon_dir / "scheduler.pid",
    }
    optional_pid_paths = {
        "watchdog_pid": root / "watchdog" / "watchdog.pid",
    }
    pid_paths = {**required_pid_paths, **optional_pid_paths}
    file_infos = {
        "start_bat": _file_tail_info(start_bat, max_lines=20, max_bytes=10000),
        "startup_log": _file_tail_info(startup_log, max_lines=80, max_bytes=20000),
        "scheduler_due_latest": _file_tail_info(due_latest_path, max_lines=80, max_bytes=20000),
        "scheduler_runtime_latest": _file_tail_info(runtime_latest_path, max_lines=80, max_bytes=20000),
        "process_health_latest": _file_tail_info(process_health_path, max_lines=80, max_bytes=20000),
    }
    for key, path in pid_paths.items():
        file_infos[key] = _file_tail_info(path, max_lines=1, max_bytes=2000)

    rows: list[dict[str, Any]] = []

    def add_row(
        area: str,
        item: str,
        status: str,
        *,
        severity: str,
        observed: Any,
        expected: Any,
        evidence_path: Path | str | None,
        detail: str,
    ) -> None:
        rows.append(
            {
                "area": area,
                "item": item,
                "status": status,
                "severity": severity,
                "observed": _json_cell(observed) if isinstance(observed, (dict, list)) else observed,
                "expected": _json_cell(expected) if isinstance(expected, (dict, list)) else expected,
                "evidence_path": str(evidence_path or ""),
                "detail": detail,
            }
        )

    add_row(
        "startup",
        "start_bat_exists",
        "pass" if start_bat.exists() else "fail",
        severity="error",
        observed=start_bat.exists(),
        expected=True,
        evidence_path=start_bat,
        detail="Visible tray-bound launcher entry point should exist.",
    )
    add_row(
        "startup",
        "startup_log_present",
        "pass" if startup_log.exists() else "warning",
        severity="warning",
        observed=file_infos["startup_log"],
        expected="startup.bat.log exists and is updated on launch",
        evidence_path=startup_log,
        detail="Start.bat appends restart/start evidence here.",
    )
    add_row(
        "autostart",
        "windows_logon_task_registered",
        "pass" if autostart.get("registered") else "warning" if autostart.get("available") else "not_applicable",
        severity="warning" if autostart.get("available") else "info",
        observed=autostart,
        expected="registered",
        evidence_path=autostart.get("script"),
        detail="Windows logon task should invoke Start.bat so the visible tray process owns the workflow.",
    )
    add_row(
        "daemon",
        "scheduler_due_latest_present",
        "pass" if due_latest_path.exists() and isinstance(due_latest, dict) and due_latest else "warning",
        severity="warning",
        observed=_flatten_daemon_event("due", due_latest if isinstance(due_latest, dict) else {}),
        expected="scheduler_due_latest.json present",
        evidence_path=due_latest_path,
        detail="Latest due check explains whether decision/execute should run, wait, or retry.",
    )
    add_row(
        "daemon",
        "scheduler_runtime_latest_present",
        "pass" if runtime_latest_path.exists() and isinstance(runtime_latest, dict) and runtime_latest else "warning",
        severity="warning",
        observed=_flatten_daemon_event("runtime", runtime_latest if isinstance(runtime_latest, dict) else {}),
        expected="scheduler_runtime_latest.json present",
        evidence_path=runtime_latest_path,
        detail="Runtime heartbeat proves the scheduler loop is alive independently of trade execution.",
    )
    for item, path in pid_paths.items():
        info = file_infos[item]
        is_optional = item in optional_pid_paths
        present = bool(info.get("exists") and str(info.get("tail") or "").strip())
        add_row(
            "pid_files",
            item,
            "pass" if present else "not_applicable" if is_optional else "warning",
            severity="info" if is_optional else "warning",
            observed=info,
            expected="pid file exists and contains a pid" if not is_optional else "optional watchdog pid when watchdog is used",
            evidence_path=path,
            detail=(
                "PID file links the tray/scheduler process tree to the daemon artifacts."
                if not is_optional
                else "Watchdog is optional in the tray-bound startup model; missing watchdog pid is not a startup failure."
            ),
        )
    if process_health_path.exists() or process_health:
        add_row(
            "process_binding",
            "process_health_latest",
            "pass" if process_health.get("status") == "pass" else "warning",
            severity="warning",
            observed=process_health,
            expected="status=pass",
            evidence_path=process_health_path,
            detail="External health snapshot records tray/scheduler/dashboard parent-child binding.",
        )

    issue_rows = [row for row in rows if row.get("status") not in {"pass", "not_applicable"}]
    severity_counts = Counter(str(row.get("severity") or "info") for row in issue_rows)
    due_flat = _flatten_daemon_event("due", due_latest if isinstance(due_latest, dict) else {})
    runtime_flat = _flatten_daemon_event("runtime", runtime_latest if isinstance(runtime_latest, dict) else {})
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": root.as_posix(),
        "project_root": project_root.as_posix(),
        "status": "fail" if severity_counts.get("error") else "attention" if issue_rows else "pass",
        "issue_count": len(issue_rows),
        "issue_count_by_severity": dict(sorted(severity_counts.items())),
        "start_bat_exists": start_bat.exists(),
        "startup_log_exists": startup_log.exists(),
        "startup_log_modified_at_utc": file_infos["startup_log"].get("modified_at_utc"),
        "autostart_available": autostart.get("available"),
        "autostart_registered": bool(autostart.get("registered")),
        "autostart_ok": autostart.get("ok"),
        "autostart_returncode": autostart.get("returncode"),
        "pid_files_present": {
            key: bool(info.get("exists") and str(info.get("tail") or "").strip())
            for key, info in file_infos.items()
            if key.endswith("_pid")
        },
        "scheduler_due_latest_exists": due_latest_path.exists(),
        "scheduler_runtime_latest_exists": runtime_latest_path.exists(),
        "process_health_status": process_health.get("status") if isinstance(process_health, dict) else None,
        "due_latest": due_flat,
        "runtime_latest": runtime_flat,
        "rows": rows,
        "file_tails": file_infos,
        "autostart_task": autostart,
        "process_health": process_health,
        "note": (
            "Startup binding is operational evidence: it proves whether Windows logon, Start.bat, tray pid, "
            "scheduler pid, due checks, runtime heartbeat, and process binding were observable."
        ),
    }
    return rows, summary


def _task_payload_status(payload: Any) -> str:
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status:
            return status
        if "ok" in payload:
            return "completed" if bool(payload.get("ok")) else "failed"
    return ""


def _scheduler_result_for_dir(run_dir: Path, decision_dir: Path | None = None) -> dict[str, Any]:
    candidates = [
        run_dir / "scheduler_task_result.json",
        decision_dir / "scheduler_task_result.json" if decision_dir else None,
    ]
    for path in candidates:
        if not path:
            continue
        payload = _read_json(path, {})
        if isinstance(payload, dict) and payload:
            return payload
    return {}


def _classify_failure(error_type: str, error: str, task_status: str, missing_core: list[str]) -> str:
    error_text = f"{error_type} {error}".lower()
    if task_status in {"completed", "success", "ok"}:
        return "none"
    if error_type in {"NameError", "ImportError", "ModuleNotFoundError", "SyntaxError", "AttributeError", "TypeError"}:
        return "code_error"
    if any(token in error_text for token in ["timeout", "connection", "rate limit", "429", "503", "504"]):
        return "network_or_api"
    if any(token in error_text for token in ["alpaca", "broker", "api"]):
        return "broker_api"
    if any(token in error_text for token in ["file not found", "no such file", "missing"]):
        return "missing_input"
    if missing_core and task_status not in {"completed", "success", "ok"}:
        return "incomplete_run"
    if task_status in {"failed", "error"}:
        return "runtime_error"
    if task_status in {"started", "running"}:
        return "running_or_stale"
    return "unknown"


def _build_run_failure_diagnosis(
    *,
    run_dir: Path,
    decision_dir: Path | None,
    context: dict[str, Any],
    summary: dict[str, Any],
    plan: dict[str, Any],
    records: list[dict[str, Any]],
    startup_binding_summary: dict[str, Any],
    run_evidence_digest_summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    artifacts = context.get("artifacts", {}) if isinstance(context.get("artifacts"), dict) else {}
    scheduler_result = _scheduler_result_for_dir(run_dir, decision_dir)
    scheduler_context = _read_json(run_dir / "scheduler_task_context.json", {})
    if not scheduler_context and decision_dir:
        scheduler_context = _read_json(decision_dir / "scheduler_task_context.json", {})
    due_latest_path = run_dir.parent / "daemon" / "scheduler_due_latest.json"
    runtime_latest_path = run_dir.parent / "daemon" / "scheduler_runtime_latest.json"
    due_latest = _read_json(due_latest_path, {})
    runtime_latest = _read_json(runtime_latest_path, {})

    summary_status = _task_payload_status(summary)
    scheduler_status = _task_payload_status(scheduler_result)
    task_status = scheduler_status or summary_status or ("completed" if records else "unknown")
    ok = bool(summary.get("ok")) if isinstance(summary, dict) and "ok" in summary else task_status in {"completed", "success", "ok"}
    error_type = str(summary.get("error_type") or scheduler_result.get("error_type") or "").strip()
    error = str(summary.get("error") or scheduler_result.get("error") or "").strip()
    traceback_text = str(summary.get("traceback") or scheduler_result.get("traceback") or "").strip()

    core_artifacts = ["execution_summary", "order_plan", "execution_records", "decision_targets"]
    missing_core = [key for key in core_artifacts if not _path_exists(artifacts.get(key))]
    if records:
        missing_core = [key for key in missing_core if key != "execution_records"]
    failure_class = _classify_failure(error_type, error or traceback_text, task_status, missing_core)

    def add_row(
        area: str,
        item: str,
        status: str,
        *,
        severity: str,
        observed: Any,
        expected: Any,
        evidence_path: Path | str | None,
        detail: str,
        next_action: str,
    ) -> None:
        rows.append(
            {
                "area": area,
                "item": item,
                "status": status,
                "severity": severity,
                "observed": _json_cell(observed) if isinstance(observed, (dict, list)) else observed,
                "expected": _json_cell(expected) if isinstance(expected, (dict, list)) else expected,
                "evidence_path": str(evidence_path or ""),
                "detail": detail,
                "next_action": next_action,
            }
        )

    add_row(
        "task",
        "scheduler_task_result",
        "pass"
        if scheduler_status in {"completed", "success", "ok"}
        else "fail"
        if scheduler_status in {"failed", "error"}
        else "warning"
        if scheduler_status in {"started", "running"}
        else "not_applicable",
        severity="error"
        if scheduler_status in {"failed", "error"}
        else "warning"
        if scheduler_status in {"started", "running"}
        else "info",
        observed={
            "status": scheduler_status,
            "returncode": scheduler_result.get("returncode"),
            "attempt": scheduler_result.get("attempt"),
            "elapsed_seconds": scheduler_result.get("elapsed_seconds"),
            "task": scheduler_result.get("task"),
        },
        expected="completed/returncode=0 for a finished task",
        evidence_path=(decision_dir or run_dir) / "scheduler_task_result.json",
        detail="Scheduler task result is the authoritative missed-run or failed-run record when present.",
        next_action="Inspect command_text, stdout_tail, stderr_tail, and retry policy in scheduler_task_result.json.",
    )
    add_row(
        "task",
        "execution_summary_status",
        "pass" if ok else "fail" if error_type or error or task_status in {"failed", "error"} else "warning",
        severity="error" if error_type or error or task_status in {"failed", "error"} else "warning" if not ok else "info",
        observed={
            "ok": summary.get("ok"),
            "status": summary_status,
            "error_type": error_type,
            "error": error,
        },
        expected="ok=true or no error payload",
        evidence_path=run_dir / "execution_summary.json",
        detail="execution_summary.json records executor success or the caught exception payload.",
        next_action="Fix the recorded error before expecting the scheduler retry to complete.",
    )
    if error_type or error:
        add_row(
            "failure",
            "exception_signature",
            "fail",
            severity="error",
            observed={"error_type": error_type, "error": error, "traceback_tail": traceback_text[-3000:]},
            expected="no exception",
            evidence_path=run_dir / "execution_summary.json",
            detail="Executor caught an exception and persisted the failure signature.",
            next_action="Prioritize the exact error_type/error pair; traceback line numbers identify the broken code path.",
        )
    for key in core_artifacts:
        present_value = key not in missing_core
        add_row(
            "artifacts",
            f"core_artifact:{key}",
            "pass" if present_value else "warning",
            severity="warning",
            observed=artifacts.get(key),
            expected="present",
            evidence_path=artifacts.get(key),
            detail="Core artifacts distinguish a real execute run from a partial, failed, or audit-only directory.",
            next_action="Ignore partial directories in rollup until executor produces the missing core artifact.",
        )

    due_flat = _flatten_daemon_event("due", due_latest if isinstance(due_latest, dict) else {})
    runtime_flat = _flatten_daemon_event("runtime", runtime_latest if isinstance(runtime_latest, dict) else {})
    add_row(
        "scheduler",
        "latest_due_state",
        "pass" if due_latest else "warning",
        severity="warning",
        observed=due_flat,
        expected="due trace present",
        evidence_path=due_latest_path,
        detail="Latest due state explains whether decision/execute was due, blocked by retry window, or waiting for time.",
        next_action="Use due_decision_can_attempt_reason and due_execute_can_attempt_reason to explain missed schedules.",
    )
    add_row(
        "scheduler",
        "latest_runtime_heartbeat",
        "pass" if runtime_latest else "warning",
        severity="warning",
        observed=runtime_flat,
        expected="runtime heartbeat present",
        evidence_path=runtime_latest_path,
        detail="Runtime heartbeat proves whether the scheduler loop was alive around the failure.",
        next_action="If missing or stale, restart via Start.bat and verify process health.",
    )
    add_row(
        "startup",
        "startup_binding_status",
        "pass" if startup_binding_summary.get("status") == "pass" else "warning",
        severity="warning",
        observed={
            "status": startup_binding_summary.get("status"),
            "issue_count": startup_binding_summary.get("issue_count"),
            "process_health_status": startup_binding_summary.get("process_health_status"),
            "autostart_registered": startup_binding_summary.get("autostart_registered"),
        },
        expected="pass",
        evidence_path=run_dir / "audit" / "75_startup_binding_summary.json",
        detail="Startup binding tells whether the tray-bound scheduler/dashboard process chain was observable.",
        next_action="If not pass, inspect Start.bat log, pid files, and /api/process-health output.",
    )
    add_row(
        "evidence",
        "run_evidence_digest_status",
        "pass" if run_evidence_digest_summary.get("status") == "pass" else "warning",
        severity="warning",
        observed={
            "status": run_evidence_digest_summary.get("status"),
            "missing_file_count": run_evidence_digest_summary.get("missing_file_count"),
            "strict_missing_file_count": run_evidence_digest_summary.get("strict_missing_file_count"),
        },
        expected="pass",
        evidence_path=run_dir / "audit" / "72_run_evidence_digest_summary.json",
        detail="Evidence digest coverage tells whether enough files exist for replay and attribution.",
        next_action="Treat attribution as incomplete when strict replay inputs are missing.",
    )

    warning_or_fail_rows = [row for row in rows if row.get("status") not in {"pass", "not_applicable"}]
    severity_counts = Counter(str(row.get("severity") or "info") for row in warning_or_fail_rows)
    run_health_status = "pass" if ok and failure_class == "none" else "fail" if failure_class != "unknown" else "attention"
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_dir": run_dir.as_posix(),
        "decision_dir": decision_dir.as_posix() if decision_dir else None,
        "session_date": context.get("session_date") or scheduler_result.get("session_date"),
        "task": scheduler_result.get("task") or ("execute" if run_dir.name.endswith("_execute") else "decision"),
        "status": run_health_status,
        "task_status": task_status,
        "ok": ok,
        "failure_class": failure_class,
        "error_type": error_type,
        "error": error,
        "returncode": scheduler_result.get("returncode"),
        "attempt": scheduler_result.get("attempt"),
        "elapsed_seconds": scheduler_result.get("elapsed_seconds"),
        "missing_core_artifacts": missing_core,
        "issue_count": len(warning_or_fail_rows),
        "issue_count_by_severity": dict(sorted(severity_counts.items())),
        "scheduler_result": scheduler_result,
        "scheduler_context": scheduler_context,
        "due_latest": due_flat,
        "runtime_latest": runtime_flat,
        "stdout_tail": (
            (scheduler_result.get("logs") or {}).get("stdout_tail")
            if isinstance(scheduler_result.get("logs"), dict)
            else None
        ),
        "stderr_tail": (
            (scheduler_result.get("logs") or {}).get("stderr_tail")
            if isinstance(scheduler_result.get("logs"), dict)
            else None
        ),
        "rows": rows,
        "note": "This diagnosis is read-only. It classifies failed, partial, and missed runs from persisted scheduler/executor evidence.",
    }
    return rows, summary_payload


def _symbols_from_instruction_payloads(raw: Any) -> list[str]:
    return sorted(
        {
            str(item.get("symbol") or "").upper().strip()
            for item in _list_payload(raw)
            if str(item.get("symbol") or "").strip()
        }
    )


def _record_status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return dict(
        sorted(
            Counter(str(record.get("status_latest") or record.get("status") or "__missing__") for record in records).items()
        )
    )


def _build_staged_rebuild_outputs(run_dir: Path, staged_raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = run_dir / "staged_rebuild_snapshots.json"
    snapshots = staged_raw.get("snapshots", []) if isinstance(staged_raw, dict) else []
    snapshots = [dict(item) for item in snapshots if isinstance(item, dict)] if isinstance(snapshots, list) else []
    rows: list[dict[str, Any]] = []
    for idx, snapshot in enumerate(snapshots, start=1):
        submitted_records = _list_payload(snapshot.get("submitted_records"))
        input_instructions = _list_payload(snapshot.get("input_instructions"))
        rebuilt_skipped = _list_payload(snapshot.get("rebuilt_skipped_orders")) or _list_payload(
            snapshot.get("entry_skipped_orders")
        )
        cap_diag = snapshot.get("entry_buying_power_cap") if isinstance(snapshot.get("entry_buying_power_cap"), dict) else {}
        status_counts = _record_status_counts(submitted_records)
        filled_qty = sum(_safe_float(record.get("filled_qty")) for record in submitted_records)
        filled_notional = sum(
            _safe_float(record.get("filled_qty")) * _safe_float(record.get("filled_avg_price"))
            for record in submitted_records
        )
        rows.append(
            {
                "snapshot_index": int(idx),
                "snapshot_type": snapshot.get("snapshot_type", ""),
                "captured_at_utc": snapshot.get("captured_at_utc", ""),
                "stage": snapshot.get("stage", ""),
                "round": _safe_int(snapshot.get("round")) if snapshot.get("round") not in (None, "") else "",
                "session_token": snapshot.get("session_token", ""),
                "limit_base_offset_bps": _safe_float(snapshot.get("limit_base_offset_bps")),
                "input_order_count": len(input_instructions),
                "input_symbols": _json_cell(_symbols_from_instruction_payloads(input_instructions)),
                "submitted_record_count": len(submitted_records),
                "submitted_status_counts": _json_cell(status_counts),
                "submitted_filled_record_count": sum(1 for record in submitted_records if _safe_float(record.get("filled_qty")) > 0),
                "submitted_filled_qty": filled_qty,
                "submitted_filled_notional": filled_notional,
                "refreshed_position_count": len(_list_payload(snapshot.get("refreshed_positions_raw"))),
                "refreshed_signed_notional_symbols": len(snapshot.get("refreshed_signed_notional", {}))
                if isinstance(snapshot.get("refreshed_signed_notional"), dict)
                else 0,
                "refreshed_price_count": len(snapshot.get("reference_prices", {}))
                if isinstance(snapshot.get("reference_prices"), dict)
                else 0,
                "buying_power": _safe_float(
                    snapshot.get("buying_power_after_stage")
                    if snapshot.get("buying_power_after_stage") not in (None, "")
                    else snapshot.get("fresh_buying_power")
                ),
                "buying_power_source": snapshot.get("buying_power_source") or snapshot.get("fresh_buying_power_source") or "",
                "account_equity": _safe_float(
                    snapshot.get("account_equity_after_stage")
                    if snapshot.get("account_equity_after_stage") not in (None, "")
                    else snapshot.get("fresh_account_equity")
                ),
                "account_equity_source": snapshot.get("account_equity_source")
                or snapshot.get("fresh_account_equity_source")
                or "",
                "rebuilt_all_order_count": len(_list_payload(snapshot.get("rebuilt_all_instructions"))),
                "rebuilt_release_order_count": len(_list_payload(snapshot.get("rebuilt_release_instructions"))),
                "rebuilt_stage_order_count": len(_list_payload(snapshot.get("rebuilt_stage_instructions"))),
                "rebuilt_release_residual_count": len(_list_payload(snapshot.get("rebuilt_release_residual_instructions"))),
                "entry_order_count_before_cap": len(_list_payload(snapshot.get("entry_instructions_before_buying_power_cap"))),
                "final_entry_order_count": len(_list_payload(snapshot.get("final_entry_instructions"))),
                "rebuilt_skipped_count": len(rebuilt_skipped),
                "cap_scaled_count": len(_list_payload(cap_diag.get("scaled") if isinstance(cap_diag, dict) else [])),
                "cap_skipped_count": len(_list_payload(cap_diag.get("skipped") if isinstance(cap_diag, dict) else [])),
                "cap_estimated_used": _safe_float(cap_diag.get("estimated_used")) if isinstance(cap_diag, dict) else 0.0,
                "cap": _safe_float(cap_diag.get("cap")) if isinstance(cap_diag, dict) else 0.0,
                "remaining_order_count": _safe_int(snapshot.get("remaining_order_count")),
                "remaining_symbols": _json_cell(snapshot.get("remaining_symbols")),
                "fully_filled": snapshot.get("fully_filled", ""),
                "entry_abort_reason": snapshot.get("entry_abort_reason", ""),
                "entry_submission_skipped_reason": snapshot.get("entry_submission_skipped_reason", ""),
            }
        )

    diagnostics = staged_raw.get("diagnostics", {}) if isinstance(staged_raw, dict) else {}
    stage_counts = Counter(str(row.get("stage") or "__missing__") for row in rows)
    snapshot_type_counts = Counter(str(row.get("snapshot_type") or "__missing__") for row in rows)
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": path.as_posix(),
        "exists": path.exists(),
        "snapshot_count": len(snapshots),
        "row_count": len(rows),
        "snapshot_type_counts": dict(sorted(snapshot_type_counts.items())),
        "stage_counts": dict(sorted(stage_counts.items())),
        "release_round_rows": sum(1 for row in rows if row.get("snapshot_type") == "release_round"),
        "entry_rebuild_rows": sum(1 for row in rows if row.get("snapshot_type") == "entry_rebuild"),
        "total_submitted_records": sum(_safe_int(row.get("submitted_record_count")) for row in rows),
        "total_submitted_filled_qty": sum(_safe_float(row.get("submitted_filled_qty")) for row in rows),
        "total_submitted_filled_notional": sum(_safe_float(row.get("submitted_filled_notional")) for row in rows),
        "total_rebuilt_skipped_orders": sum(_safe_int(row.get("rebuilt_skipped_count")) for row in rows),
        "total_cap_scaled_orders": sum(_safe_int(row.get("cap_scaled_count")) for row in rows),
        "total_cap_skipped_orders": sum(_safe_int(row.get("cap_skipped_count")) for row in rows),
        "latest_final_entry_order_count": next(
            (
                _safe_int(row.get("final_entry_order_count"))
                for row in reversed(rows)
                if str(row.get("snapshot_type") or "") == "entry_rebuild"
            ),
            0,
        ),
        "entry_aborted": bool(diagnostics.get("entry_aborted")) if isinstance(diagnostics, dict) else False,
        "entry_abort_reason": diagnostics.get("entry_abort_reason") if isinstance(diagnostics, dict) else None,
        "release_fully_filled": diagnostics.get("release_fully_filled") if isinstance(diagnostics, dict) else None,
        "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
    }
    return rows, summary


def _fill_aggregates_by_order_id(fill_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for fill in fill_rows:
        order_id = str(fill.get("order_id") or "").strip()
        if not order_id:
            continue
        bucket = out.setdefault(order_id, {"qty": 0.0, "notional": 0.0, "fill_count": 0})
        qty = _safe_float(fill.get("qty"))
        price = _safe_float(fill.get("price"))
        bucket["qty"] += abs(qty)
        bucket["notional"] += abs(qty * price)
        bucket["fill_count"] += 1
    for bucket in out.values():
        qty = _safe_float(bucket.get("qty"))
        bucket["vwap"] = _safe_float(bucket.get("notional")) / qty if qty > 0 else 0.0
    return out


def _execution_shortfall_bps(*, side: str, fill_price: float, reference_price: float) -> float:
    if reference_price <= 0 or fill_price <= 0:
        return 0.0
    direction = 1.0 if str(side).lower() == "buy" else -1.0
    return direction * ((float(fill_price) / float(reference_price)) - 1.0) * 10000.0


def _build_execution_attribution_outputs(
    records: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fills_by_order_id = _fill_aggregates_by_order_id(fill_rows)
    has_broker_fill_trace = any(str(row.get("source") or "").startswith("broker_fill_activity") for row in fill_rows)
    rows: list[dict[str, Any]] = []
    record_attempt_summaries: list[dict[str, Any]] = []
    for record_index, record in enumerate(records if isinstance(records, list) else [], start=1):
        attempts = _list_payload(record.get("attempts"))
        if not attempts:
            attempts = [record]
        attempt_offsets = [
            _safe_float(attempt.get("offset_bps"))
            for attempt in attempts
            if not _is_missing(attempt.get("offset_bps"))
        ]
        attempt_cycles = [
            _safe_int(attempt.get("requote_cycle"))
            for attempt in attempts
            if not _is_missing(attempt.get("requote_cycle"))
        ]
        configured_max_offsets = [
            _safe_float(attempt.get("max_offset_bps"))
            for attempt in attempts
            if not _is_missing(attempt.get("max_offset_bps"))
        ]
        max_attempt_offset = max(attempt_offsets, default=0.0)
        max_configured_offset = max(configured_max_offsets, default=0.0)
        hit_max_offset = bool(max_configured_offset > 0 and max_attempt_offset + 1e-9 >= max_configured_offset)
        record_remaining_qty = _safe_float(record.get("remaining_qty"))
        record_filled_qty = _safe_float(record.get("filled_qty"))
        record_attempt_summaries.append(
            {
                "record_index": int(record_index),
                "symbol": str(record.get("symbol") or "").upper(),
                "side": str(record.get("side") or "").lower(),
                "stage": record.get("stage", ""),
                "release_round": record.get("release_round", ""),
                "status_latest": str(record.get("status_latest") or ""),
                "attempt_count": len(attempts),
                "max_attempt_offset_bps": max_attempt_offset,
                "max_configured_offset_bps": max_configured_offset,
                "hit_max_offset": hit_max_offset,
                "max_requote_cycle": max(attempt_cycles, default=0),
                "filled_qty": record_filled_qty,
                "remaining_qty": record_remaining_qty,
                "reference_price": _safe_float(record.get("reference_price")),
                "remaining_notional_at_reference": record_remaining_qty * _safe_float(record.get("reference_price")),
                "record_delta_notional": _safe_float(record.get("delta_notional")),
            }
        )
        for attempt_index, attempt in enumerate(attempts, start=1):
            order_id = str(attempt.get("order_id") or record.get("order_id") or "").strip()
            fill_agg = fills_by_order_id.get(order_id, {})
            if fill_agg:
                fill_qty = _safe_float(fill_agg.get("qty"))
                fill_price = _safe_float(fill_agg.get("vwap"))
            elif has_broker_fill_trace:
                fill_qty = 0.0
                fill_price = 0.0
            else:
                fill_qty = _safe_float(attempt.get("filled_qty") or record.get("filled_qty"))
                fill_price = _safe_float(attempt.get("filled_avg_price") or record.get("filled_avg_price"))
            reference_price = _safe_float(record.get("reference_price"))
            limit_price = _safe_float(attempt.get("limit_price"))
            side = str(record.get("side") or "").lower()
            signed_shortfall_bps = _execution_shortfall_bps(
                side=side,
                fill_price=fill_price,
                reference_price=reference_price,
            )
            shortfall_notional = fill_qty * reference_price * signed_shortfall_bps / 10000.0
            limit_aggressiveness_bps = _execution_shortfall_bps(
                side=side,
                fill_price=limit_price,
                reference_price=reference_price,
            )
            status = str(attempt.get("status_latest") or record.get("status_latest") or "").lower()
            submitted_qty = _safe_float(attempt.get("qty_submitted") or record.get("qty"))
            if fill_qty <= 0:
                outcome = "unfilled"
            elif submitted_qty > 0 and fill_qty + 1e-9 < submitted_qty:
                outcome = "partial_fill"
            else:
                outcome = "filled"
            rows.append(
                {
                    "record_index": int(record_index),
                    "attempt_index": int(attempt_index),
                    "symbol": str(record.get("symbol") or "").upper(),
                    "side": side,
                    "stage": record.get("stage", ""),
                    "release_round": record.get("release_round", ""),
                    "client_order_id": attempt.get("client_order_id") or record.get("client_order_id") or "",
                    "order_id": order_id,
                    "status_latest": status,
                    "outcome": outcome,
                    "reference_price": reference_price,
                    "sizing_price": _safe_float(record.get("sizing_price")),
                    "limit_price": limit_price,
                    "limit_aggressiveness_bps": limit_aggressiveness_bps,
                    "attempt_offset_bps": _safe_float(attempt.get("offset_bps")),
                    "requote_step_index": _safe_int(attempt.get("requote_step_index"), default="")
                    if not _is_missing(attempt.get("requote_step_index"))
                    else "",
                    "requote_cycle": _safe_int(attempt.get("requote_cycle"), default="")
                    if not _is_missing(attempt.get("requote_cycle"))
                    else "",
                    "max_offset_bps": _optional_float(attempt.get("max_offset_bps")),
                    "submitted_qty": submitted_qty,
                    "filled_qty": fill_qty,
                    "filled_avg_price": fill_price,
                    "broker_fill_count": _safe_int(fill_agg.get("fill_count")) if fill_agg else 0,
                    "filled_notional_at_reference": fill_qty * reference_price,
                    "filled_notional_actual": fill_qty * fill_price,
                    "implementation_shortfall_bps": signed_shortfall_bps,
                    "implementation_shortfall_notional": shortfall_notional,
                    "poll_event_count": len(attempt.get("poll_events", [])) if isinstance(attempt.get("poll_events"), list) else 0,
                    "updated_at": attempt.get("updated_at") or record.get("updated_at") or "",
                }
            )

    filled_rows = [row for row in rows if _safe_float(row.get("filled_qty")) > 0]
    ref_notional = sum(_safe_float(row.get("filled_notional_at_reference")) for row in filled_rows)
    shortfall_total = sum(_safe_float(row.get("implementation_shortfall_notional")) for row in filled_rows)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row.get('stage') or '__missing__'}|{row.get('side') or '__missing__'}"
        bucket = grouped.setdefault(
            key,
            {
                "stage": row.get("stage") or "",
                "side": row.get("side") or "",
                "attempt_rows": 0,
                "filled_attempt_rows": 0,
                "filled_qty": 0.0,
                "filled_notional_at_reference": 0.0,
                "implementation_shortfall_notional": 0.0,
                "outcome_counts": Counter(),
                "status_counts": Counter(),
            },
        )
        bucket["attempt_rows"] += 1
        bucket["outcome_counts"][str(row.get("outcome") or "__missing__")] += 1
        bucket["status_counts"][str(row.get("status_latest") or "__missing__")] += 1
        if _safe_float(row.get("filled_qty")) > 0:
            bucket["filled_attempt_rows"] += 1
            bucket["filled_qty"] += _safe_float(row.get("filled_qty"))
            bucket["filled_notional_at_reference"] += _safe_float(row.get("filled_notional_at_reference"))
            bucket["implementation_shortfall_notional"] += _safe_float(row.get("implementation_shortfall_notional"))
    group_rows = []
    for bucket in grouped.values():
        group_ref = _safe_float(bucket.get("filled_notional_at_reference"))
        group_shortfall = _safe_float(bucket.get("implementation_shortfall_notional"))
        group_rows.append(
            {
                "stage": bucket.get("stage"),
                "side": bucket.get("side"),
                "attempt_rows": bucket.get("attempt_rows"),
                "filled_attempt_rows": bucket.get("filled_attempt_rows"),
                "filled_qty": bucket.get("filled_qty"),
                "filled_notional_at_reference": group_ref,
                "implementation_shortfall_notional": group_shortfall,
                "implementation_shortfall_bps_weighted": (group_shortfall / group_ref * 10000.0) if group_ref > 0 else 0.0,
                "outcome_counts": dict(sorted(bucket["outcome_counts"].items())),
                "status_counts": dict(sorted(bucket["status_counts"].items())),
            }
        )

    attempt_offsets_all = [
        _safe_float(row.get("attempt_offset_bps"))
        for row in rows
        if not _is_missing(row.get("attempt_offset_bps"))
    ]
    configured_offsets_all = [
        _safe_float(row.get("max_offset_bps"))
        for row in rows
        if not _is_missing(row.get("max_offset_bps"))
    ]
    requote_cycles_all = [
        _safe_int(row.get("requote_cycle"))
        for row in rows
        if not _is_missing(row.get("requote_cycle"))
    ]
    rows_at_max_offset = [
        row
        for row in rows
        if _safe_float(row.get("max_offset_bps")) > 0
        and _safe_float(row.get("attempt_offset_bps")) + 1e-9 >= _safe_float(row.get("max_offset_bps"))
    ]
    unfilled_rows_at_max_offset = [
        row for row in rows_at_max_offset if str(row.get("outcome") or "").lower() == "unfilled"
    ]
    records_hitting_max_offset = [row for row in record_attempt_summaries if bool(row.get("hit_max_offset"))]
    unfilled_records_hitting_max_offset = [
        row
        for row in records_hitting_max_offset
        if _safe_float(row.get("remaining_qty")) > 0 and str(row.get("status_latest") or "").lower() != "filled"
    ]
    summary = {
        "schema_version": "1.1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempt_row_count": len(rows),
        "filled_attempt_row_count": len(filled_rows),
        "record_count": len(records if isinstance(records, list) else []),
        "multi_attempt_record_count": sum(1 for row in record_attempt_summaries if _safe_int(row.get("attempt_count")) > 1),
        "max_attempt_count": max((_safe_int(row.get("attempt_count")) for row in record_attempt_summaries), default=0),
        "max_attempt_offset_bps": max(attempt_offsets_all, default=0.0),
        "max_configured_offset_bps": max(configured_offsets_all, default=0.0),
        "max_requote_cycle": max(requote_cycles_all, default=0),
        "attempt_rows_at_max_offset_count": len(rows_at_max_offset),
        "unfilled_attempt_rows_at_max_offset_count": len(unfilled_rows_at_max_offset),
        "records_hitting_max_offset_count": len(records_hitting_max_offset),
        "unfilled_records_hitting_max_offset_count": len(unfilled_records_hitting_max_offset),
        "unfilled_records_hitting_max_offset_remaining_notional": sum(
            _safe_float(row.get("remaining_notional_at_reference")) for row in unfilled_records_hitting_max_offset
        ),
        "outcome_counts": dict(sorted(Counter(str(row.get("outcome") or "__missing__") for row in rows).items())),
        "status_counts": dict(sorted(Counter(str(row.get("status_latest") or "__missing__") for row in rows).items())),
        "filled_notional_at_reference": ref_notional,
        "implementation_shortfall_notional": shortfall_total,
        "implementation_shortfall_bps_weighted": (shortfall_total / ref_notional * 10000.0) if ref_notional > 0 else 0.0,
        "fill_quantity_source": "broker_fill_trace_by_order_id" if has_broker_fill_trace else "execution_record_attempt_fallback",
        "by_stage_side": sorted(group_rows, key=lambda row: (str(row.get("stage")), str(row.get("side")))),
        "worst_shortfall_rows": sorted(
            filled_rows,
            key=lambda row: _safe_float(row.get("implementation_shortfall_notional")),
            reverse=True,
        )[:25],
        "top_requote_records": sorted(
            [row for row in record_attempt_summaries if _safe_int(row.get("attempt_count")) > 1],
            key=lambda row: (
                _safe_int(row.get("attempt_count")),
                _safe_float(row.get("max_attempt_offset_bps")),
                _safe_float(row.get("remaining_notional_at_reference")),
            ),
            reverse=True,
        )[:25],
        "top_unfilled_records_hitting_max_offset": sorted(
            unfilled_records_hitting_max_offset,
            key=lambda row: _safe_float(row.get("remaining_notional_at_reference")),
            reverse=True,
        )[:25],
        "note": "Positive implementation_shortfall means worse than reference price: buys above reference or sells below reference.",
    }
    return rows, summary


def _build_equity_pnl_bridge(
    *,
    summary: dict[str, Any],
    risk: dict[str, Any],
    position_rows: list[dict[str, Any]],
    realized_summary: dict[str, Any],
    execution_attribution_summary: dict[str, Any],
    broker_activity_summary: dict[str, Any],
    account_activity_attribution_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    equity_before = _safe_float(summary.get("account_equity"))
    equity_after = _safe_float(summary.get("account_equity_post_trade"))
    equity_change = equity_after - equity_before
    snapshot_intraday_pnl = sum(_safe_float(row.get("unrealized_intraday_pl_snapshot")) for row in position_rows)
    snapshot_position_delta_mv = sum(_safe_float(row.get("delta_market_value")) for row in position_rows)
    realized_pnl = _safe_float(realized_summary.get("realized_pnl_total")) if isinstance(realized_summary, dict) else 0.0
    execution_shortfall = (
        _safe_float(execution_attribution_summary.get("implementation_shortfall_notional"))
        if isinstance(execution_attribution_summary, dict)
        else 0.0
    )
    activity_net_by_type = (
        broker_activity_summary.get("net_amount_by_activity_type", {})
        if isinstance(broker_activity_summary, dict)
        else {}
    )
    account_activity_attr = account_activity_attribution_summary or {}
    non_trade_account_activity_net = _safe_float(account_activity_attr.get("known_non_trade_equity_impact_net_amount"))
    if not account_activity_attr:
        non_trade_account_activity_net = sum(
            _safe_float(value)
            for key, value in (activity_net_by_type.items() if isinstance(activity_net_by_type, dict) else [])
            if str(key).upper() != "FILL"
        )
    trade_fill_cashflow_net = _safe_float(account_activity_attr.get("trade_fill_cashflow_net_amount"))
    unknown_activity_net = _safe_float(account_activity_attr.get("unknown_activity_net_amount"))
    explained_components = {
        "snapshot_unrealized_intraday_pnl": snapshot_intraday_pnl,
        "realized_pnl_estimate": realized_pnl,
        "non_trade_account_activity_net_amount": non_trade_account_activity_net,
    }
    explained_sum = sum(_safe_float(value) for value in explained_components.values())
    rows = [
        {
            "component": "broker_equity_change",
            "amount": equity_change,
            "method": "broker_account_after.portfolio_value - broker_account_before_or_sizing.portfolio_value",
            "strictness": "broker_snapshot",
        },
        {
            "component": "snapshot_unrealized_intraday_pnl",
            "amount": snapshot_intraday_pnl,
            "method": "sum broker position unrealized_intraday_pl after execution",
            "strictness": "broker_position_snapshot",
        },
        {
            "component": "snapshot_position_delta_market_value",
            "amount": snapshot_position_delta_mv,
            "method": "sum after market_value - before market_value by symbol",
            "strictness": "signed_position_snapshot_not_pnl",
        },
        {
            "component": "realized_pnl_estimate",
            "amount": realized_pnl,
            "method": "fill-level close ledger using broker avg_entry_price before run",
            "strictness": (realized_summary or {}).get("strictness", ""),
        },
        {
            "component": "execution_shortfall_cost_estimate",
            "amount": execution_shortfall,
            "method": "broker fills versus executor reference price; positive is worse execution",
            "strictness": "implementation_shortfall_notional",
        },
        {
            "component": "non_trade_account_activity_net_amount",
            "amount": non_trade_account_activity_net,
            "method": "classified non-trade broker account activity net_amount when available",
            "strictness": "raw_broker_account_activity_classified",
        },
        {
            "component": "trade_fill_cashflow_net_amount_not_equity_pnl",
            "amount": trade_fill_cashflow_net,
            "method": "classified broker FILL account activity net_amount; tracked separately from equity PnL",
            "strictness": "raw_broker_fill_cashflow_excluded_from_equity_bridge",
        },
        {
            "component": "unknown_account_activity_net_amount_not_used",
            "amount": unknown_activity_net,
            "method": "unclassified broker activity net_amount; not used as explained equity until classified",
            "strictness": "raw_broker_account_activity_unknown",
        },
        {
            "component": "unexplained_after_snapshot_intraday_realized_activity",
            "amount": equity_change - explained_sum,
            "method": "broker_equity_change - snapshot_unrealized_intraday_pnl - realized_pnl_estimate - non_trade_account_activity_net_amount",
            "strictness": "residual_bridge_gap",
        },
    ]
    by_side = risk.get("snapshot_intraday_pnl_by_side", {}) if isinstance(risk.get("snapshot_intraday_pnl_by_side"), dict) else {}
    by_sector = risk.get("snapshot_intraday_pnl_by_sector", {}) if isinstance(risk.get("snapshot_intraday_pnl_by_sector"), dict) else {}
    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_equity_before": equity_before,
        "account_equity_after": equity_after,
        "account_equity_change": equity_change,
        "account_equity_source_before": summary.get("account_equity_source", ""),
        "account_equity_source_after": summary.get("account_equity_post_trade_source", ""),
        "components": rows,
        "component_amounts": {str(row["component"]): _safe_float(row["amount"]) for row in rows},
        "snapshot_intraday_pnl_by_side": by_side,
        "snapshot_intraday_pnl_by_sector": by_sector,
        "realized_pnl_summary": realized_summary,
        "execution_attribution_summary_subset": {
            "filled_notional_at_reference": execution_attribution_summary.get("filled_notional_at_reference")
            if isinstance(execution_attribution_summary, dict)
            else None,
            "implementation_shortfall_notional": execution_attribution_summary.get("implementation_shortfall_notional")
            if isinstance(execution_attribution_summary, dict)
            else None,
            "implementation_shortfall_bps_weighted": execution_attribution_summary.get(
                "implementation_shortfall_bps_weighted"
            )
            if isinstance(execution_attribution_summary, dict)
            else None,
            "fill_quantity_source": execution_attribution_summary.get("fill_quantity_source")
            if isinstance(execution_attribution_summary, dict)
            else None,
        },
        "account_activity_net_by_type": activity_net_by_type if isinstance(activity_net_by_type, dict) else {},
        "account_activity_attribution_summary_subset": {
            "known_non_trade_equity_impact_net_amount": account_activity_attr.get(
                "known_non_trade_equity_impact_net_amount"
            ),
            "trade_fill_cashflow_net_amount": account_activity_attr.get("trade_fill_cashflow_net_amount"),
            "unknown_activity_net_amount": account_activity_attr.get("unknown_activity_net_amount"),
            "activity_class_counts": account_activity_attr.get("activity_class_counts"),
        }
        if account_activity_attr
        else {},
        "notes": [
            "This bridge is diagnostic, not accounting-grade tax PnL.",
            "Broker equity snapshots can move with market marks while the executor is running.",
            "Realized PnL uses broker average entry price before execution, not broker tax lots.",
            "Broker FILL cash flow is tracked separately and excluded from explained equity PnL because cash and position value offset at trade time.",
            "Position delta market value is included for sanity only; it is not itself PnL.",
        ],
    }


def _equity_pnl_bridge_rows(bridge: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in bridge.get("components", []) if isinstance(item, dict)]


ACCOUNT_DIFF_FIELDS = [
    "portfolio_value",
    "equity",
    "last_equity",
    "cash",
    "buying_power",
    "regt_buying_power",
    "daytrading_buying_power",
    "non_marginable_buying_power",
    "long_market_value",
    "short_market_value",
    "initial_margin",
    "maintenance_margin",
    "last_maintenance_margin",
    "sma",
    "accrued_fees",
    "pending_transfer_in",
    "pending_transfer_out",
]

ACCOUNT_STATE_BRIDGE_FIELDS = [
    "portfolio_value",
    "equity",
    "last_equity",
    "cash",
    "accrued_fees",
    "pending_transfer_in",
    "pending_transfer_out",
    "long_market_value",
    "short_market_value",
    "buying_power",
    "regt_buying_power",
    "daytrading_buying_power",
    "non_marginable_buying_power",
    "sma",
    "initial_margin",
    "maintenance_margin",
    "last_maintenance_margin",
]

ACCOUNT_STATE_GROUPS = {
    "portfolio_value": "equity",
    "equity": "equity",
    "last_equity": "equity",
    "cash": "cash",
    "accrued_fees": "cash",
    "pending_transfer_in": "cash",
    "pending_transfer_out": "cash",
    "long_market_value": "exposure",
    "short_market_value": "exposure",
    "buying_power": "buying_power",
    "regt_buying_power": "buying_power",
    "daytrading_buying_power": "buying_power",
    "non_marginable_buying_power": "buying_power",
    "sma": "buying_power",
    "initial_margin": "margin",
    "maintenance_margin": "margin",
    "last_maintenance_margin": "margin",
}


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {})
    return dict(raw) if isinstance(raw, dict) else {}


def _load_account_snapshots(run_dir: Path) -> dict[str, Any]:
    before_raw = _read_json(run_dir / "broker_account_before.json", {})
    sizing_raw = _read_json(run_dir / "broker_account_for_sizing.json", {})
    after_raw = _read_json(run_dir / "broker_account_after.json", {})
    before_payload = _payload_dict(before_raw)
    sizing_payload = _payload_dict(sizing_raw)
    after_payload = _payload_dict(after_raw)
    before_source = "broker_account_before.json" if before_payload else "broker_account_for_sizing.json" if sizing_payload else ""
    return {
        "before": before_payload or sizing_payload,
        "before_source": before_source,
        "before_raw_exists": (run_dir / "broker_account_before.json").exists(),
        "sizing": sizing_payload,
        "sizing_source": "broker_account_for_sizing.json" if sizing_payload else "",
        "sizing_raw_exists": (run_dir / "broker_account_for_sizing.json").exists(),
        "after": after_payload,
        "after_source": "broker_account_after.json" if after_payload else "",
        "after_raw_exists": (run_dir / "broker_account_after.json").exists(),
    }


def _build_account_field_diff(run_dir: Path, summary: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots = _load_account_snapshots(run_dir)
    before = snapshots["before"]
    after = snapshots["after"]
    rows: list[dict[str, Any]] = []
    fields = sorted(set(ACCOUNT_DIFF_FIELDS) | set(before) | set(after))
    for field in fields:
        before_value = before.get(field, "")
        after_value = after.get(field, "")
        before_num = _safe_float(before_value)
        after_num = _safe_float(after_value)
        numeric = not _is_missing(before_value) and not _is_missing(after_value)
        rows.append(
            {
                "field": field,
                "before": before_value,
                "after": after_value,
                "before_num": before_num if numeric else "",
                "after_num": after_num if numeric else "",
                "delta": after_num - before_num if numeric else "",
                "source_before": snapshots["before_source"],
                "source_after": snapshots["after_source"],
                "tracked_field": field in ACCOUNT_DIFF_FIELDS,
            }
        )

    tracked_rows = [row for row in rows if row.get("tracked_field")]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exists_before": bool(before),
        "exists_after": bool(after),
        "raw_before_exists": bool(snapshots["before_raw_exists"]),
        "sizing_exists": bool(snapshots["sizing"]),
        "raw_after_exists": bool(snapshots["after_raw_exists"]),
        "source_before": snapshots["before_source"],
        "source_after": snapshots["after_source"],
        "row_count": len(rows),
        "tracked_field_count": len(tracked_rows),
        "missing_reason": ""
        if before and after
        else "historical_missing_raw_account_snapshots_or_future_run_not_yet_executed",
        "key_deltas": {
            str(row.get("field")): row.get("delta")
            for row in tracked_rows
            if row.get("delta") not in ("", None)
        },
        "summary_equity_before": summary.get("account_equity") if isinstance(summary, dict) else None,
        "summary_equity_after": summary.get("account_equity_post_trade") if isinstance(summary, dict) else None,
    }
    return rows, summary_payload


def _load_account_config_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    raw = _read_json(path, {})
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {}), "safe_broker_call.payload"
    if isinstance(raw, dict):
        return dict(raw), "raw"
    return {}, ""


def _build_account_config_diff(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_path = run_dir / "broker_account_configurations_before.json"
    after_path = run_dir / "broker_account_configurations_after.json"
    before, before_source = _load_account_config_snapshot(before_path)
    after, after_source = _load_account_config_snapshot(after_path)
    fields = sorted(set(before) | set(after))
    rows: list[dict[str, Any]] = []
    changed_count = 0
    for field in fields:
        before_value = before.get(field, "")
        after_value = after.get(field, "")
        changed = json.dumps(before_value, sort_keys=True, default=str) != json.dumps(after_value, sort_keys=True, default=str)
        if changed:
            changed_count += 1
        rows.append(
            {
                "field": field,
                "before": before_value,
                "after": after_value,
                "changed": bool(changed),
                "source_before": before_source,
                "source_after": after_source,
            }
        )
    if before_path.exists() or after_path.exists():
        status = "attention" if changed_count else "pass"
    else:
        status = "historical_limited"
    return rows, {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "before_exists": before_path.exists(),
        "after_exists": after_path.exists(),
        "source_before": before_source,
        "source_after": after_source,
        "field_count": len(fields),
        "changed_field_count": changed_count,
        "changed_fields": [str(row.get("field")) for row in rows if row.get("changed")],
        "note": "Captures Alpaca account trading configuration changes that can affect constraints, buying power checks, or fractional trading behavior.",
    }


def _account_bridge_interpretation(field: str, delta: float) -> str:
    if abs(delta) <= 1e-9:
        return "unchanged"
    direction = "increased" if delta > 0 else "decreased"
    if field in {"portfolio_value", "equity"}:
        return f"broker account equity {direction}; compare to equity PnL bridge residual"
    if field == "last_equity":
        return f"broker previous equity reference {direction}; useful for overnight/context checks"
    if field in {"cash", "accrued_fees", "pending_transfer_in", "pending_transfer_out"}:
        return f"cash-side account state {direction}; compare to fills, fees, dividends, transfers, and pending movements"
    if field in {"long_market_value", "short_market_value"}:
        return f"position market-value exposure {direction}; compare to fills and mark-to-market movement"
    if field in {"buying_power", "regt_buying_power", "daytrading_buying_power", "non_marginable_buying_power", "sma"}:
        return f"buying power {direction}; compare to exposure and margin deltas"
    if field in {"initial_margin", "maintenance_margin", "last_maintenance_margin"}:
        return f"margin requirement {direction}; compare to gross exposure and short exposure"
    return f"account field {direction}"


def _build_account_state_bridge(
    *,
    run_dir: Path,
    summary: dict[str, Any],
    account_field_rows: list[dict[str, Any]],
    equity_pnl_bridge: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshots = _load_account_snapshots(run_dir)
    before = snapshots["before"]
    after = snapshots["after"]
    rows: list[dict[str, Any]] = []
    fields = sorted(set(ACCOUNT_STATE_BRIDGE_FIELDS) | {str(row.get("field")) for row in account_field_rows if row.get("field")})
    for field in fields:
        before_value = before.get(field, "")
        after_value = after.get(field, "")
        numeric = not _is_missing(before_value) and not _is_missing(after_value)
        before_num = _safe_float(before_value)
        after_num = _safe_float(after_value)
        delta = after_num - before_num if numeric else None
        rows.append(
            {
                "group": ACCOUNT_STATE_GROUPS.get(field, "other"),
                "field": field,
                "before": before_value,
                "after": after_value,
                "before_num": before_num if numeric else "",
                "after_num": after_num if numeric else "",
                "delta": delta if delta is not None else "",
                "abs_delta": abs(delta) if delta is not None else "",
                "direction": "up" if delta is not None and delta > 0 else "down" if delta is not None and delta < 0 else "flat" if delta is not None else "non_numeric",
                "source_before": snapshots["before_source"],
                "source_after": snapshots["after_source"],
                "used_in_equity_bridge": field in {"portfolio_value", "equity"},
                "interpretation": _account_bridge_interpretation(field, delta) if delta is not None else "non-numeric or missing value; retained for raw account-state replay",
            }
        )

    def delta_for(field: str) -> float | None:
        for row in rows:
            if row.get("field") == field and row.get("delta") not in ("", None):
                return _safe_float(row.get("delta"))
        return None

    def value_after(field: str) -> float | None:
        for row in rows:
            if row.get("field") == field and row.get("after_num") not in ("", None):
                return _safe_float(row.get("after_num"))
        return None

    def value_before(field: str) -> float | None:
        for row in rows:
            if row.get("field") == field and row.get("before_num") not in ("", None):
                return _safe_float(row.get("before_num"))
        return None

    group_delta_totals: dict[str, float] = defaultdict(float)
    group_abs_delta_totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.get("delta") in ("", None):
            continue
        group = str(row.get("group") or "other")
        group_delta_totals[group] += _safe_float(row.get("delta"))
        group_abs_delta_totals[group] += abs(_safe_float(row.get("delta")))

    equity_delta = delta_for("portfolio_value")
    if equity_delta is None:
        equity_delta = delta_for("equity")
    summary_equity_before = _safe_float(summary.get("account_equity"))
    summary_equity_after = _safe_float(summary.get("account_equity_post_trade"))
    summary_equity_delta = summary_equity_after - summary_equity_before
    component_amounts = equity_pnl_bridge.get("component_amounts", {}) if isinstance(equity_pnl_bridge, dict) else {}
    equity_bridge_change = _safe_float(component_amounts.get("broker_equity_change"))
    cash_delta = delta_for("cash")
    long_mv_delta = delta_for("long_market_value")
    short_mv_delta = delta_for("short_market_value")
    long_mv_before = value_before("long_market_value")
    short_mv_before = value_before("short_market_value")
    long_mv_after = value_after("long_market_value")
    short_mv_after = value_after("short_market_value")
    gross_exposure_delta = (
        (abs(long_mv_after) + abs(short_mv_after)) - (abs(long_mv_before) + abs(short_mv_before))
        if None not in (long_mv_before, short_mv_before, long_mv_after, short_mv_after)
        else None
    )
    net_exposure_delta = (
        long_mv_delta + short_mv_delta
        if long_mv_delta is not None and short_mv_delta is not None
        else None
    )
    margin_delta = delta_for("maintenance_margin")
    if margin_delta is None:
        margin_delta = delta_for("initial_margin")
    buying_power_delta = delta_for("buying_power")
    equity_delta_vs_summary = (equity_delta - summary_equity_delta) if equity_delta is not None else None
    equity_delta_vs_bridge = (equity_delta - equity_bridge_change) if equity_delta is not None else None
    key_residuals = [
        abs(equity_delta_vs_summary or 0.0),
        abs(equity_delta_vs_bridge or 0.0),
    ]
    status = (
        "pass"
        if before and after and equity_delta is not None and max(key_residuals or [0.0]) <= 1.0
        else "attention"
        if before and after
        else "historical_limited"
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "raw_before_exists": bool(snapshots["before_raw_exists"]),
        "sizing_exists": bool(snapshots["sizing"]),
        "raw_after_exists": bool(snapshots["after_raw_exists"]),
        "exists_before": bool(before),
        "exists_after": bool(after),
        "source_before": snapshots["before_source"],
        "source_after": snapshots["after_source"],
        "row_count": len(rows),
        "numeric_row_count": sum(1 for row in rows if row.get("delta") not in ("", None)),
        "group_delta_totals": dict(sorted(group_delta_totals.items())),
        "group_abs_delta_totals": dict(sorted(group_abs_delta_totals.items())),
        "equity_delta": equity_delta,
        "summary_equity_delta": summary_equity_delta,
        "equity_bridge_change": equity_bridge_change,
        "equity_delta_vs_summary_delta": equity_delta_vs_summary,
        "equity_delta_vs_equity_bridge_change": equity_delta_vs_bridge,
        "cash_delta": cash_delta,
        "accrued_fees_delta": delta_for("accrued_fees"),
        "pending_transfer_in_delta": delta_for("pending_transfer_in"),
        "pending_transfer_out_delta": delta_for("pending_transfer_out"),
        "long_market_value_delta": long_mv_delta,
        "short_market_value_delta": short_mv_delta,
        "gross_exposure_delta": gross_exposure_delta,
        "net_exposure_delta": net_exposure_delta,
        "buying_power_delta": buying_power_delta,
        "regt_buying_power_delta": delta_for("regt_buying_power"),
        "daytrading_buying_power_delta": delta_for("daytrading_buying_power"),
        "maintenance_margin_delta": delta_for("maintenance_margin"),
        "initial_margin_delta": delta_for("initial_margin"),
        "largest_account_state_deltas": sorted(
            [row for row in rows if row.get("delta") not in ("", None)],
            key=lambda row: abs(_safe_float(row.get("delta"))),
            reverse=True,
        )[:20],
        "note": (
            "Uses already-persisted broker account snapshots only. "
            "It explains account-state movement around the equity bridge without making extra broker calls."
        ),
    }
    return rows, summary_payload


def _build_event_timeline(
    *,
    run_dir: Path,
    records: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    order_attempt_rows: list[dict[str, Any]],
    order_poll_rows: list[dict[str, Any]],
    api_audit_rows: list[dict[str, Any]],
    staged_rebuild_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add_event(
        *,
        at_utc: Any,
        source: str,
        event_type: str,
        symbol: str = "",
        order_id: str = "",
        client_order_id: str = "",
        stage: str = "",
        severity: str = "info",
        detail: str = "",
        payload: Any = None,
    ) -> None:
        at_text = str(at_utc or "")
        rows.append(
            {
                "timeline_seq": 0,
                "at_utc": at_text,
                "source": source,
                "event_type": event_type,
                "symbol": str(symbol or "").upper(),
                "order_id": str(order_id or ""),
                "client_order_id": str(client_order_id or ""),
                "stage": str(stage or ""),
                "severity": severity,
                "detail": detail,
                "payload": _json_cell(payload),
            }
        )

    scheduler_context = _read_json(run_dir / "scheduler_task_context.json", {})
    scheduler_result = _read_json(run_dir / "scheduler_task_result.json", {})
    run_context = _read_json(run_dir / "run_context.json", {})
    if isinstance(scheduler_context, dict) and scheduler_context:
        add_event(
            at_utc=scheduler_context.get("trigger_now_utc") or scheduler_context.get("generated_at_utc"),
            source="scheduler_task_context",
            event_type="scheduler_trigger",
            detail=str(scheduler_context.get("task") or ""),
            payload={
                "attempt": scheduler_context.get("attempt"),
                "command": (scheduler_context.get("command") or {}).get("command_text")
                if isinstance(scheduler_context.get("command"), dict)
                else "",
            },
        )
    if isinstance(run_context, dict):
        for event in run_context.get("events", []) if isinstance(run_context.get("events"), list) else []:
            if not isinstance(event, dict):
                continue
            add_event(
                at_utc=event.get("at_utc"),
                source="executor_run_context",
                event_type=str(event.get("name") or ""),
                detail=str(event.get("name") or ""),
                payload=event.get("payload"),
            )
    for record_index, record in enumerate(records if isinstance(records, list) else [], start=1):
        add_event(
            at_utc=record.get("submitted_at_utc"),
            source="execution_records",
            event_type="record_submitted",
            symbol=record.get("symbol", ""),
            order_id=record.get("order_id", ""),
            client_order_id=record.get("client_order_id", ""),
            stage=record.get("stage", ""),
            detail=str(record.get("status_latest") or ""),
            payload={"record_index": record_index, "side": record.get("side"), "qty": record.get("qty")},
        )
        attempts = record.get("attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                add_event(
                    at_utc=attempt.get("updated_at") or record.get("updated_at"),
                    source="execution_records.attempts",
                    event_type=f"attempt_{attempt.get('status_latest') or 'updated'}",
                    symbol=record.get("symbol", ""),
                    order_id=attempt.get("order_id", ""),
                    client_order_id=attempt.get("client_order_id", ""),
                    stage=record.get("stage", ""),
                    severity="warning" if str(attempt.get("status_latest") or "").lower() in {"canceled", "rejected"} else "info",
                    detail=f"attempt={attempt.get('attempt_no')} status={attempt.get('status_latest')}",
                    payload=attempt,
                )
    for row in fill_rows:
        add_event(
            at_utc=row.get("transaction_time"),
            source=str(row.get("source") or "fill_trace"),
            event_type="fill",
            symbol=row.get("symbol", ""),
            order_id=row.get("order_id", ""),
            client_order_id=row.get("client_order_id", ""),
            detail=f"{row.get('side')} qty={row.get('qty')} price={row.get('price')}",
            payload=row,
        )
    for row in order_attempt_rows:
        if _safe_int(row.get("poll_event_count")) > 0:
            add_event(
                at_utc=row.get("updated_at"),
                source="order_attempt_trace",
                event_type="attempt_poll_summary",
                symbol=row.get("symbol", ""),
                order_id=row.get("attempt_order_id", ""),
                client_order_id=row.get("attempt_client_order_id", ""),
                stage=row.get("stage", ""),
                detail=f"poll_events={row.get('poll_event_count')}",
                payload=row,
            )
    for row in order_poll_rows:
        add_event(
            at_utc=row.get("at_utc") or row.get("updated_at"),
            source="order_poll_timeline",
            event_type=str(row.get("event") or "order_poll"),
            symbol=row.get("symbol", ""),
            order_id=row.get("order_id", ""),
            client_order_id=row.get("client_order_id", ""),
            stage=row.get("record_stage", ""),
            severity="warning" if row.get("error") else "info",
            detail=str(row.get("status") or row.get("error") or ""),
            payload=row,
        )
    for row in api_audit_rows:
        add_event(
            at_utc=row.get("started_at_utc"),
            source="alpaca_api_audit",
            event_type="api_request",
            severity="warning" if not row.get("ok") else "info",
            detail=f"{row.get('method')} {row.get('status_code')} {row.get('url')}",
            payload=row,
        )
    for row in staged_rebuild_rows:
        add_event(
            at_utc=row.get("captured_at_utc"),
            source="staged_rebuild_trace",
            event_type=str(row.get("snapshot_type") or "staged_snapshot"),
            stage=row.get("stage", ""),
            detail=f"stage={row.get('stage')} round={row.get('round')} remaining={row.get('remaining_order_count')}",
            payload=row,
        )
    if isinstance(scheduler_result, dict) and scheduler_result:
        add_event(
            at_utc=scheduler_result.get("generated_at_utc"),
            source="scheduler_task_result",
            event_type="scheduler_result",
            severity="warning" if str(scheduler_result.get("status") or "") != "completed" else "info",
            detail=f"status={scheduler_result.get('status')} returncode={scheduler_result.get('returncode')}",
            payload={
                "elapsed_seconds": scheduler_result.get("elapsed_seconds"),
                "status": scheduler_result.get("status"),
                "returncode": scheduler_result.get("returncode"),
            },
        )

    rows.sort(key=lambda row: (str(row.get("at_utc") or ""), str(row.get("source") or ""), str(row.get("event_type") or "")))
    for idx, row in enumerate(rows, start=1):
        row["timeline_seq"] = idx
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_count": len(rows),
        "events_with_timestamp": sum(1 for row in rows if str(row.get("at_utc") or "")),
        "source_counts": dict(sorted(Counter(str(row.get("source") or "__missing__") for row in rows).items())),
        "event_type_counts": dict(sorted(Counter(str(row.get("event_type") or "__missing__") for row in rows).items())),
        "severity_counts": dict(sorted(Counter(str(row.get("severity") or "info") for row in rows).items())),
        "first_event_at_utc": next((row.get("at_utc") for row in rows if row.get("at_utc")), None),
        "last_event_at_utc": next((row.get("at_utc") for row in reversed(rows) if row.get("at_utc")), None),
    }
    return rows, summary


def _build_symbol_attribution_bridge(
    *,
    decision_rows: list[dict[str, Any]],
    realized_rows: list[dict[str, Any]],
    execution_attribution_rows: list[dict[str, Any]],
    reconciliation_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    symbols = {
        str(row.get("symbol") or "").upper().strip()
        for row in [*decision_rows, *realized_rows, *execution_attribution_rows, *reconciliation_rows, *fill_rows, *order_rows]
        if str(row.get("symbol") or "").strip()
    }
    decision_by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_rows}
    realized_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in realized_rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        realized_by_symbol[sym]["realized_pnl"] += _safe_float(row.get("realized_pnl"))
        realized_by_symbol[sym]["closed_qty"] += _safe_float(row.get("closed_qty"))
        realized_by_symbol[sym]["opening_qty"] += _safe_float(row.get("opening_qty"))
        realized_by_symbol[sym]["ledger_rows"] += 1
    exec_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in execution_attribution_rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        exec_by_symbol[sym]["attempt_rows"] += 1
        exec_by_symbol[sym]["filled_qty"] += _safe_float(row.get("filled_qty"))
        exec_by_symbol[sym]["filled_notional_at_reference"] += _safe_float(row.get("filled_notional_at_reference"))
        exec_by_symbol[sym]["implementation_shortfall_notional"] += _safe_float(row.get("implementation_shortfall_notional"))
    recon_by_symbol = {str(row.get("symbol") or "").upper(): row for row in reconciliation_rows}
    fill_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in fill_rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        fill_by_symbol[sym]["fill_count"] += 1
        fill_by_symbol[sym]["fill_abs_qty"] += abs(_safe_float(row.get("qty")))
        fill_by_symbol[sym]["fill_notional_abs"] += abs(_safe_float(row.get("qty")) * _safe_float(row.get("price")))
    order_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in order_rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        order_by_symbol[sym]["planned_abs_notional"] += abs(_safe_float(row.get("planned_delta_notional")))
        order_by_symbol[sym]["order_rows"] += 1

    rows: list[dict[str, Any]] = []
    for sym in sorted(symbols):
        decision = decision_by_symbol.get(sym, {})
        realized = realized_by_symbol.get(sym, {})
        exec_attr = exec_by_symbol.get(sym, {})
        recon = recon_by_symbol.get(sym, {})
        fills = fill_by_symbol.get(sym, {})
        orders = order_by_symbol.get(sym, {})
        ref_notional = _safe_float(exec_attr.get("filled_notional_at_reference"))
        shortfall = _safe_float(exec_attr.get("implementation_shortfall_notional"))
        rows.append(
            {
                "symbol": sym,
                "target_signed_weight": _safe_float(decision.get("target_signed_weight")),
                "target_side": decision.get("target_side", ""),
                "before_side": decision.get("before_side", ""),
                "after_side": decision.get("after_side", ""),
                "before_market_value": _safe_float(decision.get("before_market_value")),
                "after_market_value": _safe_float(decision.get("after_market_value")),
                "delta_market_value": _safe_float(decision.get("delta_market_value")),
                "snapshot_intraday_pnl": _safe_float(decision.get("unrealized_intraday_pl_snapshot")),
                "realized_pnl_estimate": _safe_float(realized.get("realized_pnl")),
                "closed_qty": _safe_float(realized.get("closed_qty")),
                "opening_qty": _safe_float(realized.get("opening_qty")),
                "realized_ledger_rows": _safe_int(realized.get("ledger_rows")),
                "implementation_shortfall_notional": shortfall,
                "implementation_shortfall_bps_weighted": shortfall / ref_notional * 10000.0 if ref_notional > 0 else 0.0,
                "filled_notional_at_reference": ref_notional,
                "execution_attempt_rows": _safe_int(exec_attr.get("attempt_rows")),
                "fill_count": _safe_int(fills.get("fill_count")),
                "fill_abs_qty": _safe_float(fills.get("fill_abs_qty")),
                "fill_notional_abs": _safe_float(fills.get("fill_notional_abs")),
                "planned_abs_notional": _safe_float(orders.get("planned_abs_notional")),
                "order_rows": _safe_int(orders.get("order_rows")),
                "position_unexplained_qty": _safe_float(recon.get("unexplained_qty")),
                "position_unexplained_abs_qty": _safe_float(recon.get("unexplained_abs_qty")),
                "position_unexplained_notional": _safe_float(recon.get("unexplained_notional_at_snapshot_price")),
                "position_residual_reason_hint": recon.get("residual_reason_hint", ""),
            }
        )
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol_count": len(rows),
        "symbols_with_fills": sum(1 for row in rows if _safe_float(row.get("fill_abs_qty")) > 0),
        "symbols_with_realized_pnl_rows": sum(1 for row in rows if _safe_int(row.get("realized_ledger_rows")) > 0),
        "symbols_with_position_residual": sum(1 for row in rows if _safe_float(row.get("position_unexplained_abs_qty")) > 0),
        "totals": {
            "snapshot_intraday_pnl": sum(_safe_float(row.get("snapshot_intraday_pnl")) for row in rows),
            "realized_pnl_estimate": sum(_safe_float(row.get("realized_pnl_estimate")) for row in rows),
            "implementation_shortfall_notional": sum(_safe_float(row.get("implementation_shortfall_notional")) for row in rows),
            "position_unexplained_notional": sum(_safe_float(row.get("position_unexplained_notional")) for row in rows),
            "filled_notional_at_reference": sum(_safe_float(row.get("filled_notional_at_reference")) for row in rows),
        },
        "worst_snapshot_intraday_pnl": sorted(rows, key=lambda row: _safe_float(row.get("snapshot_intraday_pnl")))[:20],
        "worst_realized_pnl": sorted(rows, key=lambda row: _safe_float(row.get("realized_pnl_estimate")))[:20],
        "worst_execution_shortfall": sorted(
            rows,
            key=lambda row: _safe_float(row.get("implementation_shortfall_notional")),
            reverse=True,
        )[:20],
        "largest_position_residuals": sorted(
            rows,
            key=lambda row: _safe_float(row.get("position_unexplained_notional")),
            reverse=True,
        )[:20],
    }
    return rows, summary


def _transition_intent(before_mv: float, target_mv: float, tolerance: float) -> str:
    if abs(before_mv) <= tolerance and abs(target_mv) <= tolerance:
        return "stay_flat"
    if abs(before_mv) <= tolerance:
        return "open_long" if target_mv > 0 else "open_short"
    if abs(target_mv) <= tolerance:
        return "close_long" if before_mv > 0 else "close_short"
    if before_mv * target_mv < 0:
        return "flip_long_to_short" if before_mv > 0 else "flip_short_to_long"
    delta = target_mv - before_mv
    if before_mv > 0:
        return "increase_long" if delta > tolerance else "reduce_long" if delta < -tolerance else "hold_long"
    return "increase_short" if delta < -tolerance else "reduce_short" if delta > tolerance else "hold_short"


def _transition_outcome(
    *,
    before_mv: float,
    target_mv: float,
    after_mv: float,
    tolerance: float,
    material_residual: bool,
) -> str:
    if material_residual:
        return "unverified_position_residual"
    desired_delta = target_mv - before_mv
    observed_delta = after_mv - before_mv
    target_error = after_mv - target_mv
    if abs(desired_delta) <= tolerance:
        return "no_trade_target_met" if abs(target_error) <= tolerance else "moved_without_target"
    if before_mv * target_mv < -tolerance and after_mv * target_mv > tolerance:
        return "flip_achieved"
    if abs(target_mv) <= tolerance and abs(after_mv) <= tolerance:
        return "closed_to_flat"
    if observed_delta * desired_delta < -tolerance:
        return "moved_opposite_target"
    if abs(target_error) <= max(tolerance, 0.10 * abs(desired_delta)):
        return "near_target"
    if abs(observed_delta) < abs(desired_delta):
        return "underfilled_or_partial"
    if abs(observed_delta) > abs(desired_delta) and (after_mv - target_mv) * desired_delta > tolerance:
        return "overshot_target"
    return "target_gap_remaining"


def _build_target_transition_trace(
    *,
    decision_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    reconciliation_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in decision_rows}
    order_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in order_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        order_by_symbol[symbol]["planned_order_count"] += 1
        order_by_symbol[symbol]["planned_delta_notional"] += _safe_float(row.get("planned_delta_notional"))
        order_by_symbol[symbol]["planned_abs_notional"] += abs(_safe_float(row.get("planned_delta_notional")))
        order_by_symbol[symbol]["filled_qty"] += _safe_float(row.get("filled_qty"))
        order_by_symbol[symbol]["filled_notional"] += _safe_float(row.get("filled_notional"))
    fill_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in fill_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        signed_qty = _signed_trade_qty(str(row.get("side") or ""), _safe_float(row.get("qty")))
        fill_by_symbol[symbol]["fill_count"] += 1
        fill_by_symbol[symbol]["fill_net_signed_qty"] += signed_qty
        fill_by_symbol[symbol]["fill_abs_qty"] += abs(_safe_float(row.get("qty")))
        fill_by_symbol[symbol]["fill_signed_notional"] += signed_qty * _safe_float(row.get("price"))
        fill_by_symbol[symbol]["fill_abs_notional"] += abs(_safe_float(row.get("qty")) * _safe_float(row.get("price")))
    recon_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in reconciliation_rows}
    equity = _safe_float(summary.get("account_equity") or summary.get("account_equity_post_trade"))
    symbols = sorted(set(decision_by_symbol) | set(order_by_symbol) | set(fill_by_symbol) | set(recon_by_symbol))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        decision = decision_by_symbol.get(symbol, {})
        orders = order_by_symbol.get(symbol, {})
        fills = fill_by_symbol.get(symbol, {})
        recon = recon_by_symbol.get(symbol, {})
        before_mv = _safe_float(decision.get("before_market_value"))
        after_mv = _safe_float(decision.get("after_market_value"))
        target_weight = _safe_float(decision.get("target_signed_weight"))
        target_mv = target_weight * equity if equity else 0.0
        planned_delta = _safe_float(orders.get("planned_delta_notional"))
        desired_delta = target_mv - before_mv
        observed_delta = after_mv - before_mv
        target_error = after_mv - target_mv
        tolerance = max(50.0, 0.0025 * max(abs(before_mv), abs(target_mv), abs(after_mv), abs(equity), 1.0))
        material_residual = str(recon.get("material_unexplained_qty")).lower() == "true"
        intent = _transition_intent(before_mv, target_mv, tolerance)
        outcome = _transition_outcome(
            before_mv=before_mv,
            target_mv=target_mv,
            after_mv=after_mv,
            tolerance=tolerance,
            material_residual=material_residual,
        )
        if material_residual:
            confidence = "blocked_by_position_residual"
        elif _safe_float(orders.get("planned_order_count")) > 0 and _safe_float(fills.get("fill_count")) <= 0:
            confidence = "order_without_captured_fill"
        elif outcome in {"near_target", "closed_to_flat", "flip_achieved", "no_trade_target_met"}:
            confidence = "high"
        else:
            confidence = "medium"
        rows.append(
            {
                "symbol": symbol,
                "intent": intent,
                "outcome": outcome,
                "confidence": confidence,
                "target_side": decision.get("target_side", ""),
                "before_side": decision.get("before_side", ""),
                "after_side": decision.get("after_side", ""),
                "before_market_value": before_mv,
                "target_market_value_estimate": target_mv,
                "after_market_value": after_mv,
                "desired_delta_market_value": desired_delta,
                "planned_delta_notional": planned_delta,
                "observed_delta_market_value": observed_delta,
                "target_error_market_value": target_error,
                "target_error_abs": abs(target_error),
                "target_error_bps_of_equity": (target_error / equity * 10000.0) if equity else 0.0,
                "planned_order_count": _safe_int(orders.get("planned_order_count")),
                "fill_count": _safe_int(fills.get("fill_count")),
                "fill_net_signed_qty": _safe_float(fills.get("fill_net_signed_qty")),
                "fill_abs_qty": _safe_float(fills.get("fill_abs_qty")),
                "fill_abs_notional": _safe_float(fills.get("fill_abs_notional")),
                "position_residual_reason_hint": recon.get("residual_reason_hint", ""),
                "material_position_residual": bool(material_residual),
                "position_unexplained_qty": _safe_float(recon.get("unexplained_qty")),
                "position_unexplained_notional": _safe_float(recon.get("unexplained_notional_at_snapshot_price")),
            }
        )
    material_rows = [row for row in rows if row.get("material_position_residual")]
    target_gap_rows = [
        row
        for row in rows
        if not row.get("material_position_residual")
        and _safe_float(row.get("target_error_abs")) > max(100.0, 0.0025 * abs(equity))
    ]
    attention_rows = material_rows
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol_count": len(rows),
        "status": "attention" if attention_rows else "pass",
        "intent_counts": dict(sorted(Counter(str(row.get("intent") or "") for row in rows).items())),
        "outcome_counts": dict(sorted(Counter(str(row.get("outcome") or "") for row in rows).items())),
        "confidence_counts": dict(sorted(Counter(str(row.get("confidence") or "") for row in rows).items())),
        "attention_symbol_count": len(attention_rows),
        "material_position_residual_symbols": len(material_rows),
        "target_gap_symbol_count_without_position_residual": len(target_gap_rows),
        "gross_target_error_abs_without_position_residual": sum(_safe_float(row.get("target_error_abs")) for row in target_gap_rows),
        "largest_target_errors": sorted(rows, key=lambda row: _safe_float(row.get("target_error_abs")), reverse=True)[:25],
        "largest_unverified_transitions": sorted(
            material_rows,
            key=lambda row: _safe_float(row.get("position_unexplained_notional")),
            reverse=True,
        )[:25],
        "note": (
            "Compares intended before->target market-value transition against after snapshot. "
            "Rows with material_position_residual cannot be treated as verified target misses until position snapshots/fills reconcile."
        ),
    }
    return rows, summary_payload


def _side_from_weight(weight: float) -> str:
    if weight > 0:
        return "long"
    if weight < 0:
        return "short"
    return "flat"


def _target_projection_reason(raw_weight: float, projected_weight: float, tolerance: float = 1e-10) -> str:
    if abs(raw_weight) <= tolerance and abs(projected_weight) <= tolerance:
        return "flat_or_absent"
    if abs(raw_weight) <= tolerance:
        return "projected_only"
    if abs(projected_weight) <= tolerance:
        return "short_floor_zeroed" if raw_weight < 0 else "projected_to_zero"
    if abs(raw_weight - projected_weight) <= tolerance:
        return "unchanged"
    if raw_weight < 0 and projected_weight < 0 and abs(projected_weight) < abs(raw_weight):
        return "short_floor_reduced"
    return "projected_changed"


def _build_decision_intent_trace(
    *,
    plan: dict[str, Any],
    decision_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_weights = plan.get("raw_target_signed_weights", {}) if isinstance(plan.get("raw_target_signed_weights"), dict) else {}
    projected_weights = (
        plan.get("executable_expected_signed_weights", {})
        if isinstance(plan.get("executable_expected_signed_weights"), dict)
        else plan.get("target_signed_weights", {})
        if isinstance(plan.get("target_signed_weights"), dict)
        else {}
    )
    decision_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in decision_rows}
    order_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in order_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        order_by_symbol[symbol]["planned_order_count"] += 1
        order_by_symbol[symbol]["planned_delta_notional"] += _safe_float(row.get("planned_delta_notional"))
        order_by_symbol[symbol]["planned_abs_notional"] += abs(_safe_float(row.get("planned_delta_notional")))
        order_by_symbol[symbol]["filled_qty"] += _safe_float(row.get("filled_qty"))
        order_by_symbol[symbol]["remaining_qty"] += _safe_float(row.get("remaining_qty"))
    skipped_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in plan.get("skipped_orders", []) if isinstance(plan.get("skipped_orders"), list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if symbol:
            skipped_by_symbol[symbol].append(row)

    equity = _safe_float(plan.get("account_equity") or summary.get("account_equity") or summary.get("account_equity_post_trade"))
    min_trade_notional = _safe_float(plan.get("min_trade_notional"), default=200.0)
    symbols = sorted(set(raw_weights) | set(projected_weights) | set(decision_by_symbol) | set(order_by_symbol) | set(skipped_by_symbol))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        decision = decision_by_symbol.get(symbol, {})
        orders = order_by_symbol.get(symbol, {})
        skipped = skipped_by_symbol.get(symbol, [])
        raw_weight = _safe_float(raw_weights.get(symbol))
        projected_weight = _safe_float(projected_weights.get(symbol))
        projection_delta_weight = projected_weight - raw_weight
        before_mv = _safe_float(decision.get("before_market_value"))
        projected_target_notional = projected_weight * equity if equity else 0.0
        raw_target_notional = raw_weight * equity if equity else 0.0
        desired_delta = projected_target_notional - before_mv
        planned_order_count = _safe_int(orders.get("planned_order_count"))
        skip_reasons = [str(item.get("reason") or "") for item in skipped if str(item.get("reason") or "")]
        if planned_order_count > 0:
            order_intent_status = "planned_order"
        elif skipped:
            order_intent_status = "skipped_by_order_builder"
        elif abs(desired_delta) < min_trade_notional:
            order_intent_status = "below_min_trade_notional"
        else:
            order_intent_status = "no_order_unexplained_from_plan"
        rows.append(
            {
                "symbol": symbol,
                "raw_target_signed_weight": raw_weight,
                "projected_target_signed_weight": projected_weight,
                "projection_delta_weight": projection_delta_weight,
                "raw_target_notional_estimate": raw_target_notional,
                "projected_target_notional_estimate": projected_target_notional,
                "projection_delta_notional_estimate": projection_delta_weight * equity if equity else 0.0,
                "projection_reason": _target_projection_reason(raw_weight, projected_weight),
                "raw_target_side": _side_from_weight(raw_weight),
                "projected_target_side": _side_from_weight(projected_weight),
                "before_side": decision.get("before_side", ""),
                "after_side": decision.get("after_side", ""),
                "before_market_value": before_mv,
                "after_market_value": _safe_float(decision.get("after_market_value")),
                "desired_delta_notional_estimate": desired_delta,
                "planned_delta_notional": _safe_float(orders.get("planned_delta_notional")),
                "planned_abs_notional": _safe_float(orders.get("planned_abs_notional")),
                "planned_order_count": planned_order_count,
                "filled_qty_from_order_trace": _safe_float(orders.get("filled_qty")),
                "remaining_qty_from_order_trace": _safe_float(orders.get("remaining_qty")),
                "order_intent_status": order_intent_status,
                "skip_reason": ";".join(skip_reasons),
                "skip_count": len(skipped),
                "min_trade_notional": min_trade_notional,
                "composite_score": _safe_float(decision.get("composite_score")),
                "reversal_score": _safe_float(decision.get("reversal_score")),
                "momentum_score": _safe_float(decision.get("momentum_score")),
                "small_size_score": _safe_float(decision.get("small_size_score")),
                "low_beta_score": _safe_float(decision.get("low_beta_score")),
                "cash_quality_score": _safe_float(decision.get("cash_quality_score")),
                "beta": _safe_float(decision.get("beta")),
                "sic2_sector": decision.get("sic2_sector", ""),
                "lot_total_weight": _safe_float(decision.get("lot_total_weight")),
                "lot_weight_reversal_score": _safe_float(decision.get("lot_weight_reversal_score")),
                "lot_weight_momentum_score": _safe_float(decision.get("lot_weight_momentum_score")),
                "lot_weight_small_size_score": _safe_float(decision.get("lot_weight_small_size_score")),
                "lot_weight_low_beta_score": _safe_float(decision.get("lot_weight_low_beta_score")),
                "lot_weight_cash_quality_score": _safe_float(decision.get("lot_weight_cash_quality_score")),
            }
        )

    projection_changed = [row for row in rows if str(row.get("projection_reason")) not in {"unchanged", "flat_or_absent"}]
    skipped_rows = [row for row in rows if _safe_int(row.get("skip_count")) > 0]
    unexplained_no_order = [row for row in rows if str(row.get("order_intent_status")) == "no_order_unexplained_from_plan"]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "attention" if unexplained_no_order else "pass",
        "symbol_count": len(rows),
        "raw_target_symbol_count": len([symbol for symbol, weight in raw_weights.items() if abs(_safe_float(weight)) > 0]),
        "projected_target_symbol_count": len([symbol for symbol, weight in projected_weights.items() if abs(_safe_float(weight)) > 0]),
        "projection_changed_symbol_count": len(projection_changed),
        "short_floor_zeroed_symbol_count": sum(1 for row in rows if row.get("projection_reason") == "short_floor_zeroed"),
        "short_floor_reduced_symbol_count": sum(1 for row in rows if row.get("projection_reason") == "short_floor_reduced"),
        "order_intent_status_counts": dict(sorted(Counter(str(row.get("order_intent_status") or "") for row in rows).items())),
        "projection_reason_counts": dict(sorted(Counter(str(row.get("projection_reason") or "") for row in rows).items())),
        "skip_reason_counts": dict(sorted(Counter(str(reason) for row in skipped_rows for reason in str(row.get("skip_reason") or "").split(";") if reason).items())),
        "gross_projection_delta_notional_abs": sum(abs(_safe_float(row.get("projection_delta_notional_estimate"))) for row in rows),
        "gross_desired_delta_notional_abs": sum(abs(_safe_float(row.get("desired_delta_notional_estimate"))) for row in rows),
        "gross_planned_abs_notional": sum(_safe_float(row.get("planned_abs_notional")) for row in rows),
        "skipped_symbol_count": len(skipped_rows),
        "unexplained_no_order_symbol_count": len(unexplained_no_order),
        "target_short_floor_diagnostics": plan.get("target_short_floor_diagnostics") if isinstance(plan, dict) else {},
        "executable_target_projection": plan.get("executable_target_projection")
        if isinstance(plan.get("executable_target_projection"), dict)
        else {},
        "largest_projection_changes": sorted(
            projection_changed,
            key=lambda row: abs(_safe_float(row.get("projection_delta_notional_estimate"))),
            reverse=True,
        )[:25],
        "largest_desired_deltas_without_order": sorted(
            [row for row in rows if _safe_int(row.get("planned_order_count")) <= 0],
            key=lambda row: abs(_safe_float(row.get("desired_delta_notional_estimate"))),
            reverse=True,
        )[:25],
        "note": (
            "Explains raw DecisionEngine target weights versus executable projected target weights. "
            "Projection changes are commonly caused by whole-share short constraints."
        ),
    }
    return rows, summary_payload


def _action_class_for_order(side: str, current_notional: float, target_notional: float) -> str:
    side_l = str(side or "").lower()
    if side_l == "sell" and current_notional > 0 and target_notional >= 0:
        return "release_sell_long"
    if side_l == "buy" and current_notional < 0 and target_notional <= 0:
        return "release_buy_to_cover"
    if current_notional * target_notional < 0:
        return "flip"
    if abs(current_notional) <= 1e-9:
        return "entry"
    return "increase_or_reduce"


def _whole_share_reason(plan: dict[str, Any], order: dict[str, Any], side: str, current_notional: float, target_notional: float) -> str:
    if bool(plan.get("whole_shares_only")):
        return "global_whole_shares_only"
    opening_short = bool(order.get("opening_short"))
    short_sale = str(side or "").lower() == "sell" and target_notional < current_notional and target_notional < -1e-9
    if opening_short and bool(plan.get("opening_shorts_whole_shares_only")):
        return "opening_short_whole_shares_only"
    if short_sale and bool(plan.get("short_sales_whole_shares_only")):
        return "short_sale_whole_shares_only"
    return ""


def _sizing_offset_bps(side: str, reference_price: float, sizing_price: float) -> float:
    if reference_price <= 0 or sizing_price <= 0:
        return 0.0
    if str(side or "").lower() == "buy":
        return (sizing_price / reference_price - 1.0) * 10000.0
    return (1.0 - sizing_price / reference_price) * 10000.0


def _build_order_constraint_trace(
    *,
    plan: dict[str, Any],
    order_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    decision_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in decision_rows}
    order_trace_by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in order_rows:
        key = (str(row.get("symbol") or "").upper().strip(), str(row.get("side") or "").lower().strip())
        if key[0]:
            order_trace_by_symbol_side[key].append(row)
    fill_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in fill_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        fill_by_symbol[symbol]["fill_count"] += 1
        fill_by_symbol[symbol]["fill_abs_qty"] += abs(_safe_float(row.get("qty")))
        fill_by_symbol[symbol]["fill_abs_notional"] += abs(_safe_float(row.get("qty")) * _safe_float(row.get("price")))
    min_trade_notional = _safe_float(plan.get("min_trade_notional"), default=200.0)
    qty_decimals = _safe_int(plan.get("qty_decimals"), default=4)
    rows: list[dict[str, Any]] = []

    for order_index, order in enumerate(plan.get("orders", []) if isinstance(plan.get("orders"), list) else [], start=1):
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("symbol") or "").upper().strip()
        side = str(order.get("side") or "").lower().strip()
        current_notional = _safe_float(order.get("current_notional"))
        target_notional = _safe_float(order.get("target_notional"))
        delta_notional = _safe_float(order.get("delta_notional"))
        reference_price = _safe_float(order.get("reference_price"))
        sizing_price = _safe_float(order.get("sizing_price"))
        planned_qty = _safe_float(order.get("qty"))
        raw_qty = abs(delta_notional) / sizing_price if sizing_price > 0 else 0.0
        whole_reason = _whole_share_reason(plan, order, side, current_notional, target_notional)
        trace_rows = order_trace_by_symbol_side.get((symbol, side), [])
        filled_qty = sum(_safe_float(row.get("filled_qty")) for row in trace_rows)
        remaining_qty = sum(_safe_float(row.get("remaining_qty")) for row in trace_rows)
        planned_abs_notional = abs(delta_notional)
        filled_notional = sum(_safe_float(row.get("filled_notional")) for row in trace_rows)
        if filled_notional <= 0 and reference_price > 0:
            filled_notional = filled_qty * reference_price
        unfilled_notional = max(0.0, planned_abs_notional - abs(filled_notional))
        statuses = sorted({str(row.get("status_latest") or "") for row in trace_rows if str(row.get("status_latest") or "")})
        rows.append(
            {
                "row_type": "planned_order",
                "plan_order_index": order_index,
                "symbol": symbol,
                "side": side,
                "action_class": _action_class_for_order(side, current_notional, target_notional),
                "current_notional": current_notional,
                "target_notional": target_notional,
                "delta_notional": delta_notional,
                "planned_abs_notional": planned_abs_notional,
                "reference_price": reference_price,
                "sizing_price": sizing_price,
                "sizing_offset_bps_estimate": _sizing_offset_bps(side, reference_price, sizing_price),
                "raw_qty_estimate": raw_qty,
                "planned_qty": planned_qty,
                "qty_rounding_loss": max(0.0, raw_qty - planned_qty),
                "qty_decimals": qty_decimals,
                "min_trade_notional": min_trade_notional,
                "estimated_notional_at_reference": planned_qty * reference_price,
                "estimated_notional_at_sizing": planned_qty * sizing_price,
                "whole_share_required": bool(whole_reason),
                "whole_share_reason": whole_reason,
                "opening_short": bool(order.get("opening_short")),
                "short_sale_estimate": side == "sell" and target_notional < current_notional and target_notional < -1e-9,
                "skipped": False,
                "skip_reason": "",
                "execution_trace_rows": len(trace_rows),
                "execution_stages": ";".join(sorted({str(row.get("stage") or "") for row in trace_rows if str(row.get("stage") or "")})),
                "status_latest_set": ";".join(statuses),
                "filled_qty": filled_qty,
                "remaining_qty": remaining_qty,
                "filled_notional_estimate": filled_notional,
                "unfilled_notional_estimate": unfilled_notional,
                "fill_count": _safe_int(fill_by_symbol.get(symbol, {}).get("fill_count")),
                "fill_abs_qty": _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_qty")),
                "fill_abs_notional": _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_notional")),
                "composite_score": _safe_float(decision_by_symbol.get(symbol, {}).get("composite_score")),
                "constraint_notes": "",
            }
        )

    for skipped_index, skipped in enumerate(plan.get("skipped_orders", []) if isinstance(plan.get("skipped_orders"), list) else [], start=1):
        if not isinstance(skipped, dict):
            continue
        symbol = str(skipped.get("symbol") or "").upper().strip()
        delta_notional = _safe_float(skipped.get("delta_notional"))
        side = "buy" if delta_notional > 0 else "sell" if delta_notional < 0 else ""
        decision = decision_by_symbol.get(symbol, {})
        rows.append(
            {
                "row_type": "skipped_order",
                "plan_order_index": skipped_index,
                "symbol": symbol,
                "side": side,
                "action_class": "skipped",
                "current_notional": _safe_float(decision.get("before_market_value")),
                "target_notional": _safe_float(decision.get("before_market_value")) + delta_notional,
                "delta_notional": delta_notional,
                "planned_abs_notional": abs(delta_notional),
                "reference_price": _safe_float(skipped.get("price")),
                "sizing_price": 0.0,
                "sizing_offset_bps_estimate": 0.0,
                "raw_qty_estimate": 0.0,
                "planned_qty": 0.0,
                "qty_rounding_loss": 0.0,
                "qty_decimals": qty_decimals,
                "min_trade_notional": min_trade_notional,
                "estimated_notional_at_reference": _safe_float(skipped.get("estimated_notional")),
                "estimated_notional_at_sizing": 0.0,
                "whole_share_required": False,
                "whole_share_reason": "",
                "opening_short": False,
                "short_sale_estimate": side == "sell" and delta_notional < 0,
                "skipped": True,
                "skip_reason": skipped.get("reason", ""),
                "execution_trace_rows": 0,
                "execution_stages": "",
                "status_latest_set": "",
                "filled_qty": 0.0,
                "remaining_qty": 0.0,
                "filled_notional_estimate": 0.0,
                "unfilled_notional_estimate": abs(delta_notional),
                "fill_count": _safe_int(fill_by_symbol.get(symbol, {}).get("fill_count")),
                "fill_abs_qty": _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_qty")),
                "fill_abs_notional": _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_notional")),
                "composite_score": _safe_float(decision.get("composite_score")),
                "constraint_notes": _json_cell(skipped),
            }
        )

    skipped_rows = [row for row in rows if bool(row.get("skipped"))]
    planned_rows = [row for row in rows if str(row.get("row_type")) == "planned_order"]
    unfilled_rows = [
        row
        for row in planned_rows
        if _safe_float(row.get("unfilled_notional_estimate")) > max(1.0, 0.01 * _safe_float(row.get("planned_abs_notional")))
    ]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pass",
        "row_count": len(rows),
        "planned_order_count": len(planned_rows),
        "skipped_order_count": len(skipped_rows),
        "whole_share_required_count": sum(1 for row in planned_rows if bool(row.get("whole_share_required"))),
        "opening_short_count": sum(1 for row in planned_rows if bool(row.get("opening_short"))),
        "short_sale_estimate_count": sum(1 for row in planned_rows if bool(row.get("short_sale_estimate"))),
        "action_class_counts": dict(sorted(Counter(str(row.get("action_class") or "") for row in rows).items())),
        "skip_reason_counts": dict(sorted(Counter(str(row.get("skip_reason") or "") for row in skipped_rows).items())),
        "status_latest_counts": dict(
            sorted(Counter(status for row in planned_rows for status in str(row.get("status_latest_set") or "").split(";") if status).items())
        ),
        "gross_planned_abs_notional": sum(_safe_float(row.get("planned_abs_notional")) for row in planned_rows),
        "gross_filled_notional_estimate": sum(_safe_float(row.get("filled_notional_estimate")) for row in planned_rows),
        "gross_unfilled_notional_estimate": sum(_safe_float(row.get("unfilled_notional_estimate")) for row in planned_rows),
        "gross_skipped_abs_notional": sum(_safe_float(row.get("planned_abs_notional")) for row in skipped_rows),
        "largest_unfilled_orders": sorted(
            unfilled_rows,
            key=lambda row: _safe_float(row.get("unfilled_notional_estimate")),
            reverse=True,
        )[:25],
        "skipped_orders": skipped_rows[:50],
        "note": (
            "Explains how order-builder constraints translated target deltas into planned quantities, skipped rows, "
            "whole-share requirements, and observed fill coverage."
        ),
    }
    return rows, summary_payload


def _planned_order_maps(plan: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    orders_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in plan.get("orders", []) if isinstance(plan.get("orders"), list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if symbol:
            orders_by_symbol[symbol].append(row)
    for row in plan.get("skipped_orders", []) if isinstance(plan.get("skipped_orders"), list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if symbol:
            skipped_by_symbol[symbol].append(row)
    return orders_by_symbol, skipped_by_symbol


def _plan_order_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for row in rows:
        out["order_count"] += 1
        out["delta_notional"] += _safe_float(row.get("delta_notional"))
        out["abs_delta_notional"] += abs(_safe_float(row.get("delta_notional")))
        out["qty"] += _safe_float(row.get("qty"))
        out["target_notional"] += _safe_float(row.get("target_notional"))
        out["current_notional"] += _safe_float(row.get("current_notional"))
    return out


def _first_numeric(rows: list[dict[str, Any]], key: str) -> float:
    for row in rows:
        value = _safe_float(row.get(key))
        if value:
            return value
    return 0.0


def _build_decision_execute_drift(
    *,
    decision_plan: dict[str, Any],
    execute_plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_orders, decision_skipped = _planned_order_maps(decision_plan if isinstance(decision_plan, dict) else {})
    execute_orders, execute_skipped = _planned_order_maps(execute_plan if isinstance(execute_plan, dict) else {})
    decision_raw = (
        decision_plan.get("raw_target_signed_weights", {})
        if isinstance(decision_plan.get("raw_target_signed_weights"), dict)
        else {}
    )
    decision_projected = (
        decision_plan.get("executable_expected_signed_weights", {})
        if isinstance(decision_plan.get("executable_expected_signed_weights"), dict)
        else decision_plan.get("target_signed_weights", {})
        if isinstance(decision_plan.get("target_signed_weights"), dict)
        else {}
    )
    execute_raw = (
        execute_plan.get("raw_target_signed_weights", {})
        if isinstance(execute_plan.get("raw_target_signed_weights"), dict)
        else {}
    )
    execute_projected = (
        execute_plan.get("executable_expected_signed_weights", {})
        if isinstance(execute_plan.get("executable_expected_signed_weights"), dict)
        else execute_plan.get("target_signed_weights", {})
        if isinstance(execute_plan.get("target_signed_weights"), dict)
        else {}
    )
    symbols = sorted(
        set(decision_raw)
        | set(decision_projected)
        | set(execute_raw)
        | set(execute_projected)
        | set(decision_orders)
        | set(execute_orders)
        | set(decision_skipped)
        | set(execute_skipped)
    )
    rows: list[dict[str, Any]] = []
    decision_equity = _safe_float(decision_plan.get("account_equity"))
    execute_equity = _safe_float(execute_plan.get("account_equity"))
    for symbol in symbols:
        d_orders = decision_orders.get(symbol, [])
        e_orders = execute_orders.get(symbol, [])
        d_skipped = decision_skipped.get(symbol, [])
        e_skipped = execute_skipped.get(symbol, [])
        d_totals = _plan_order_totals(d_orders)
        e_totals = _plan_order_totals(e_orders)
        d_weight = _safe_float(decision_projected.get(symbol))
        e_weight = _safe_float(execute_projected.get(symbol))
        d_raw_weight = _safe_float(decision_raw.get(symbol))
        e_raw_weight = _safe_float(execute_raw.get(symbol))
        order_presence = (
            "both_planned"
            if d_orders and e_orders
            else "execute_only"
            if e_orders
            else "decision_only"
            if d_orders
            else "no_planned_order"
        )
        skip_presence = (
            "both_skipped"
            if d_skipped and e_skipped
            else "execute_only_skipped"
            if e_skipped
            else "decision_only_skipped"
            if d_skipped
            else ""
        )
        drift_reason_parts: list[str] = []
        if abs(d_weight - e_weight) > 1e-10:
            drift_reason_parts.append("target_weight_changed")
        if order_presence != "both_planned" and order_presence != "no_planned_order":
            drift_reason_parts.append(order_presence)
        if skip_presence:
            drift_reason_parts.append(skip_presence)
        if abs(_first_numeric(d_orders, "reference_price") - _first_numeric(e_orders, "reference_price")) > 1e-9:
            drift_reason_parts.append("reference_price_changed")
        if abs(_safe_float(d_totals.get("current_notional")) - _safe_float(e_totals.get("current_notional"))) > 1.0:
            drift_reason_parts.append("current_notional_changed")
        if abs(_safe_float(d_totals.get("delta_notional")) - _safe_float(e_totals.get("delta_notional"))) > 1.0:
            drift_reason_parts.append("planned_delta_changed")
        rows.append(
            {
                "symbol": symbol,
                "order_presence": order_presence,
                "skip_presence": skip_presence,
                "drift_reasons": ";".join(drift_reason_parts),
                "decision_raw_target_signed_weight": d_raw_weight,
                "execute_raw_target_signed_weight": e_raw_weight,
                "raw_target_weight_delta": e_raw_weight - d_raw_weight,
                "decision_projected_target_signed_weight": d_weight,
                "execute_projected_target_signed_weight": e_weight,
                "projected_target_weight_delta": e_weight - d_weight,
                "decision_target_notional_estimate": d_weight * decision_equity if decision_equity else 0.0,
                "execute_target_notional_estimate": e_weight * execute_equity if execute_equity else 0.0,
                "target_notional_delta_estimate": (e_weight * execute_equity if execute_equity else 0.0)
                - (d_weight * decision_equity if decision_equity else 0.0),
                "decision_order_count": _safe_int(d_totals.get("order_count")),
                "execute_order_count": _safe_int(e_totals.get("order_count")),
                "decision_planned_delta_notional": _safe_float(d_totals.get("delta_notional")),
                "execute_planned_delta_notional": _safe_float(e_totals.get("delta_notional")),
                "planned_delta_notional_change": _safe_float(e_totals.get("delta_notional"))
                - _safe_float(d_totals.get("delta_notional")),
                "decision_planned_abs_notional": _safe_float(d_totals.get("abs_delta_notional")),
                "execute_planned_abs_notional": _safe_float(e_totals.get("abs_delta_notional")),
                "decision_current_notional": _safe_float(d_totals.get("current_notional")),
                "execute_current_notional": _safe_float(e_totals.get("current_notional")),
                "current_notional_change": _safe_float(e_totals.get("current_notional"))
                - _safe_float(d_totals.get("current_notional")),
                "decision_reference_price": _first_numeric(d_orders, "reference_price"),
                "execute_reference_price": _first_numeric(e_orders, "reference_price"),
                "reference_price_change": _first_numeric(e_orders, "reference_price") - _first_numeric(d_orders, "reference_price"),
                "reference_price_change_bps": (
                    (_first_numeric(e_orders, "reference_price") / _first_numeric(d_orders, "reference_price") - 1.0)
                    * 10000.0
                    if _first_numeric(d_orders, "reference_price") > 0 and _first_numeric(e_orders, "reference_price") > 0
                    else 0.0
                ),
                "decision_qty": _safe_float(d_totals.get("qty")),
                "execute_qty": _safe_float(e_totals.get("qty")),
                "qty_change": _safe_float(e_totals.get("qty")) - _safe_float(d_totals.get("qty")),
                "decision_skip_count": len(d_skipped),
                "execute_skip_count": len(e_skipped),
                "decision_skip_reasons": ";".join(sorted({str(item.get("reason") or "") for item in d_skipped if str(item.get("reason") or "")})),
                "execute_skip_reasons": ";".join(sorted({str(item.get("reason") or "") for item in e_skipped if str(item.get("reason") or "")})),
            }
        )

    changed_rows = [row for row in rows if str(row.get("drift_reasons") or "")]
    material_rows = [
        row
        for row in changed_rows
        if abs(_safe_float(row.get("planned_delta_notional_change"))) > 100.0
        or abs(_safe_float(row.get("target_notional_delta_estimate"))) > 100.0
        or str(row.get("order_presence")) in {"decision_only", "execute_only"}
        or str(row.get("skip_presence") or "")
    ]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pass",
        "symbol_count": len(rows),
        "changed_symbol_count": len(changed_rows),
        "material_changed_symbol_count": len(material_rows),
        "decision_order_count": sum(_safe_int(row.get("decision_order_count")) for row in rows),
        "execute_order_count": sum(_safe_int(row.get("execute_order_count")) for row in rows),
        "decision_skipped_symbol_count": sum(1 for row in rows if _safe_int(row.get("decision_skip_count")) > 0),
        "execute_skipped_symbol_count": sum(1 for row in rows if _safe_int(row.get("execute_skip_count")) > 0),
        "order_presence_counts": dict(sorted(Counter(str(row.get("order_presence") or "") for row in rows).items())),
        "skip_presence_counts": dict(sorted(Counter(str(row.get("skip_presence") or "") for row in rows if str(row.get("skip_presence") or "")).items())),
        "drift_reason_counts": dict(
            sorted(Counter(reason for row in rows for reason in str(row.get("drift_reasons") or "").split(";") if reason).items())
        ),
        "gross_abs_target_notional_delta_estimate": sum(abs(_safe_float(row.get("target_notional_delta_estimate"))) for row in rows),
        "gross_abs_planned_delta_notional_change": sum(abs(_safe_float(row.get("planned_delta_notional_change"))) for row in rows),
        "largest_planned_delta_changes": sorted(
            material_rows,
            key=lambda row: abs(_safe_float(row.get("planned_delta_notional_change"))),
            reverse=True,
        )[:25],
        "largest_target_notional_changes": sorted(
            material_rows,
            key=lambda row: abs(_safe_float(row.get("target_notional_delta_estimate"))),
            reverse=True,
        )[:25],
        "note": (
            "Compares the decision-time order plan with the execute-time rebuilt order plan. "
            "Drift can come from fresh broker positions/equity/prices, target projection, or order-builder skips."
        ),
    }
    return rows, summary_payload


def _snapshot_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {})
    return dict(raw) if isinstance(raw, dict) else {}


def _latest_trade_payload(raw: Any) -> dict[str, dict[str, Any]]:
    payload = _snapshot_payload(raw)
    return {
        str(symbol).upper().strip(): dict(trade)
        for symbol, trade in payload.items()
        if str(symbol).strip() and isinstance(trade, dict)
    }


def _latest_quote_payload(raw: Any) -> dict[str, dict[str, Any]]:
    payload = _snapshot_payload(raw)
    return {
        str(symbol).upper().strip(): dict(quote)
        for symbol, quote in payload.items()
        if str(symbol).strip() and isinstance(quote, dict)
    }


def _plan_price_by_symbol(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    for order in plan.get("orders", []) if isinstance(plan.get("orders"), list) else []:
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        bucket = out[symbol]
        weight = abs(_safe_float(order.get("delta_notional"))) or abs(_safe_float(order.get("qty"))) or 1.0
        reference_price = _safe_float(order.get("reference_price"))
        sizing_price = _safe_float(order.get("sizing_price"))
        bucket["order_count"] += 1
        bucket["planned_delta_notional"] += _safe_float(order.get("delta_notional"))
        bucket["planned_abs_notional"] += abs(_safe_float(order.get("delta_notional")))
        bucket["qty"] += _safe_float(order.get("qty"))
        if reference_price > 0:
            bucket["reference_price_weighted_sum"] += reference_price * weight
            bucket["reference_price_weight"] += weight
            bucket.setdefault("first_reference_price", reference_price)
        if sizing_price > 0:
            bucket["sizing_price_weighted_sum"] += sizing_price * weight
            bucket["sizing_price_weight"] += weight
            bucket.setdefault("first_sizing_price", sizing_price)
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, bucket in out.items():
        ref_weight = _safe_float(bucket.get("reference_price_weight"))
        sizing_weight = _safe_float(bucket.get("sizing_price_weight"))
        normalized[symbol] = {
            "order_count": _safe_int(bucket.get("order_count")),
            "planned_delta_notional": _safe_float(bucket.get("planned_delta_notional")),
            "planned_abs_notional": _safe_float(bucket.get("planned_abs_notional")),
            "qty": _safe_float(bucket.get("qty")),
            "reference_price": _safe_float(bucket.get("reference_price_weighted_sum")) / ref_weight
            if ref_weight > 0
            else _safe_float(bucket.get("first_reference_price")),
            "sizing_price": _safe_float(bucket.get("sizing_price_weighted_sum")) / sizing_weight
            if sizing_weight > 0
            else _safe_float(bucket.get("first_sizing_price")),
        }
    return normalized


def _snapshot_price_maps(raw: dict[str, Any]) -> dict[str, Any]:
    payload = _snapshot_payload(raw)
    reference = payload.get("reference_prices", {}) if isinstance(payload.get("reference_prices"), dict) else {}
    fallback = payload.get("fallback_prices", {}) if isinstance(payload.get("fallback_prices"), dict) else {}
    missing = payload.get("missing_reference_price_symbols", [])
    return {
        "exists": bool(raw),
        "ok": raw.get("ok") if isinstance(raw, dict) and "ok" in raw else None,
        "feed": payload.get("feed", ""),
        "collected_at_utc": payload.get("collected_at_utc") or raw.get("collected_at_utc", ""),
        "reference_prices": {
            str(symbol).upper().strip(): _safe_float(price)
            for symbol, price in reference.items()
            if str(symbol).strip()
        },
        "fallback_prices": {
            str(symbol).upper().strip(): _safe_float(price)
            for symbol, price in fallback.items()
            if str(symbol).strip()
        },
        "missing_symbols": sorted(str(symbol).upper().strip() for symbol in missing if str(symbol).strip())
        if isinstance(missing, list)
        else [],
        "target_symbols": sorted(
            str(symbol).upper().strip()
            for symbol in (payload.get("target_symbols", []) if isinstance(payload.get("target_symbols"), list) else [])
            if str(symbol).strip()
        ),
        "broker_position_symbols_before": sorted(
            str(symbol).upper().strip()
            for symbol in (
                payload.get("broker_position_symbols_before", [])
                if isinstance(payload.get("broker_position_symbols_before"), list)
                else []
            )
            if str(symbol).strip()
        ),
        "audit_benchmark_symbols": sorted(
            str(symbol).upper().strip()
            for symbol in (
                payload.get("audit_benchmark_symbols", [])
                if isinstance(payload.get("audit_benchmark_symbols"), list)
                else []
            )
            if str(symbol).strip()
        ),
        "audit_price_symbols": sorted(
            str(symbol).upper().strip()
            for symbol in (
                payload.get("audit_price_symbols", [])
                if isinstance(payload.get("audit_price_symbols"), list)
                else []
            )
            if str(symbol).strip()
        ),
    }


def _bps_change(new_value: float, old_value: float) -> float:
    if old_value <= 0 or new_value <= 0:
        return 0.0
    return (new_value / old_value - 1.0) * 10000.0


def _build_market_price_evidence(
    *,
    run_dir: Path,
    decision_dir: Path | None,
    decision_plan: dict[str, Any],
    execute_plan: dict[str, Any],
    decision_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_price = _snapshot_price_maps(
        _read_json((decision_dir / "execution_price_snapshot.json") if decision_dir else Path("__missing__"), {})
    )
    execute_price = _snapshot_price_maps(_read_json(run_dir / "execution_price_snapshot.json", {}))
    decision_trades = _latest_trade_payload(
        _read_json((decision_dir / "execution_latest_trades_snapshot.json") if decision_dir else Path("__missing__"), {})
    )
    execute_trades = _latest_trade_payload(_read_json(run_dir / "execution_latest_trades_snapshot.json", {}))
    decision_plan_prices = _plan_price_by_symbol(decision_plan if isinstance(decision_plan, dict) else {})
    execute_plan_prices = _plan_price_by_symbol(execute_plan if isinstance(execute_plan, dict) else {})
    decision_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in decision_rows}
    fill_by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for fill in fill_rows:
        symbol = str(fill.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        qty = abs(_safe_float(fill.get("qty")))
        price = _safe_float(fill.get("price"))
        fill_by_symbol[symbol]["fill_count"] += 1
        fill_by_symbol[symbol]["fill_abs_qty"] += qty
        fill_by_symbol[symbol]["fill_abs_notional"] += qty * price

    symbols = sorted(
        set(decision_by_symbol)
        | set(decision_price["reference_prices"])
        | set(execute_price["reference_prices"])
        | set(decision_price["fallback_prices"])
        | set(execute_price["fallback_prices"])
        | set(decision_price["target_symbols"])
        | set(execute_price["target_symbols"])
        | set(decision_price["broker_position_symbols_before"])
        | set(execute_price["broker_position_symbols_before"])
        | set(decision_plan_prices)
        | set(execute_plan_prices)
        | set(decision_trades)
        | set(execute_trades)
        | set(fill_by_symbol)
    )
    missing_execute = set(execute_price.get("missing_symbols", []))
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        decision_plan_price = _safe_float(decision_plan_prices.get(symbol, {}).get("reference_price"))
        execute_plan_price = _safe_float(execute_plan_prices.get(symbol, {}).get("reference_price"))
        decision_snapshot_ref = _safe_float(decision_price["reference_prices"].get(symbol))
        execute_snapshot_ref = _safe_float(execute_price["reference_prices"].get(symbol))
        decision_reference = decision_plan_price or decision_snapshot_ref
        execute_reference = execute_plan_price or execute_snapshot_ref
        decision_fallback = _safe_float(decision_price["fallback_prices"].get(symbol))
        execute_fallback = _safe_float(execute_price["fallback_prices"].get(symbol))
        decision_trade = decision_trades.get(symbol, {})
        execute_trade = execute_trades.get(symbol, {})
        decision_trade_price = _safe_float(decision_trade.get("p"))
        execute_trade_price = _safe_float(execute_trade.get("p"))
        fill_notional = _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_notional"))
        fill_qty = _safe_float(fill_by_symbol.get(symbol, {}).get("fill_abs_qty"))
        fill_vwap = fill_notional / fill_qty if fill_qty > 0 else 0.0
        latest_vs_reference_bps = _bps_change(execute_trade_price, execute_reference)
        reference_vs_fallback_bps = _bps_change(execute_reference, execute_fallback)
        decision_execute_reference_change_bps = _bps_change(execute_reference, decision_reference)
        if not execute_price["exists"]:
            status = "historical_plan_only" if execute_reference > 0 else "historical_no_price_evidence"
        elif execute_reference <= 0:
            status = "missing_reference_price"
        elif symbol in missing_execute:
            status = "missing_reference_symbol"
        elif execute_price["exists"] and execute_trade_price <= 0 and execute_fallback > 0:
            status = "fallback_only"
        elif abs(decision_execute_reference_change_bps) > 200.0:
            status = "large_decision_execute_reference_move"
        else:
            status = "pass"
        if execute_trade_price > 0 and abs(execute_trade_price - execute_reference) <= max(0.01, execute_reference * 0.0001):
            inferred_source = "latest_trade"
        elif execute_fallback > 0 and abs(execute_fallback - execute_reference) <= max(0.01, execute_reference * 0.0001):
            inferred_source = "fallback_price"
        elif execute_reference > 0 and execute_plan_price > 0:
            inferred_source = "order_plan_reference"
        else:
            inferred_source = "missing"
        rows.append(
            {
                "symbol": symbol,
                "status": status,
                "in_decision_target_or_position": symbol in decision_by_symbol,
                "in_execute_target_symbols": symbol in set(execute_price["target_symbols"]),
                "in_execute_broker_position_before": symbol in set(execute_price["broker_position_symbols_before"]),
                "decision_feed": decision_price.get("feed", ""),
                "execute_feed": execute_price.get("feed", ""),
                "decision_price_snapshot_exists": bool(decision_price["exists"]),
                "execute_price_snapshot_exists": bool(execute_price["exists"]),
                "decision_snapshot_collected_at_utc": decision_price.get("collected_at_utc", ""),
                "execute_snapshot_collected_at_utc": execute_price.get("collected_at_utc", ""),
                "decision_plan_reference_price": decision_plan_price,
                "execute_plan_reference_price": execute_plan_price,
                "decision_snapshot_reference_price": decision_snapshot_ref,
                "execute_snapshot_reference_price": execute_snapshot_ref,
                "decision_reference_price_used": decision_reference,
                "execute_reference_price_used": execute_reference,
                "decision_fallback_price": decision_fallback,
                "execute_fallback_price": execute_fallback,
                "execute_reference_source_inferred": inferred_source,
                "decision_latest_trade_price": decision_trade_price,
                "execute_latest_trade_price": execute_trade_price,
                "decision_latest_trade_time": decision_trade.get("t", ""),
                "execute_latest_trade_time": execute_trade.get("t", ""),
                "execute_latest_trade_exchange": execute_trade.get("x", ""),
                "execute_latest_trade_size": _safe_float(execute_trade.get("s")),
                "execute_latest_trade_conditions": _json_cell(execute_trade.get("c", "")),
                "reference_vs_fallback_bps": reference_vs_fallback_bps,
                "latest_trade_vs_reference_bps": latest_vs_reference_bps,
                "decision_execute_reference_change_bps": decision_execute_reference_change_bps,
                "execute_planned_delta_notional": _safe_float(execute_plan_prices.get(symbol, {}).get("planned_delta_notional")),
                "execute_planned_abs_notional": _safe_float(execute_plan_prices.get(symbol, {}).get("planned_abs_notional")),
                "fill_count": _safe_int(fill_by_symbol.get(symbol, {}).get("fill_count")),
                "fill_abs_notional": fill_notional,
                "fill_vwap": fill_vwap,
                "fill_vwap_vs_reference_bps": _bps_change(fill_vwap, execute_reference),
                "missing_reference_flag": symbol in missing_execute,
            }
        )

    status_counts = Counter(str(row.get("status") or "") for row in rows)
    large_reference_moves = [row for row in rows if row.get("status") == "large_decision_execute_reference_move"]
    missing_rows = [row for row in rows if str(row.get("status") or "").startswith("missing")]
    fallback_rows = [row for row in rows if row.get("status") == "fallback_only"]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "historical_limited" if not execute_price["exists"] else "attention" if missing_rows else "pass",
        "symbol_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "decision_price_snapshot_exists": bool(decision_price["exists"]),
        "execute_price_snapshot_exists": bool(execute_price["exists"]),
        "decision_latest_trade_count": len(decision_trades),
        "execute_latest_trade_count": len(execute_trades),
        "execute_missing_reference_symbol_count": len(missing_execute),
        "fallback_only_symbol_count": len(fallback_rows),
        "large_decision_execute_reference_move_count": len(large_reference_moves),
        "max_abs_decision_execute_reference_change_bps": max(
            (abs(_safe_float(row.get("decision_execute_reference_change_bps"))) for row in rows),
            default=0.0,
        ),
        "largest_decision_execute_reference_moves": sorted(
            rows,
            key=lambda row: abs(_safe_float(row.get("decision_execute_reference_change_bps"))),
            reverse=True,
        )[:25],
        "missing_reference_rows": missing_rows[:50],
        "fallback_only_rows": fallback_rows[:50],
        "note": (
            "Expands execution_price_snapshot and latest trade evidence by symbol. "
            "Historical runs can fall back to order_plan reference prices when explicit price snapshots are absent."
        ),
    }
    return rows, summary_payload


def _intraday_bar_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {})
    return dict(raw) if isinstance(raw, dict) else {}


def _intraday_bar_rows(raw: Any) -> list[dict[str, Any]]:
    payload = _intraday_bar_payload(raw)
    bars = payload.get("bars") if isinstance(payload.get("bars"), list) else []
    return [dict(row) for row in bars if isinstance(row, dict)]


def _intraday_bar_requested_symbols(raw: Any) -> set[str]:
    payload = _intraday_bar_payload(raw)
    requested = payload.get("requested_symbols") if isinstance(payload.get("requested_symbols"), list) else []
    return {str(symbol or "").upper().strip() for symbol in requested if str(symbol or "").strip()}


def _intraday_bar_errors(raw: Any) -> list[dict[str, Any]]:
    payload = _intraday_bar_payload(raw)
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    return [dict(item) for item in errors if isinstance(item, dict)]


def _bar_time(row: dict[str, Any]) -> str:
    return str(row.get("t") or row.get("timestamp") or row.get("time") or "")


def _range_position_pct(value: float, low: float, high: float) -> float | None:
    if value <= 0 or low <= 0 or high <= low:
        return None
    return (float(value) - float(low)) / (float(high) - float(low)) * 100.0


def _signed_adverse_bps(side: str, observed: float, benchmark: float) -> float:
    if observed <= 0 or benchmark <= 0:
        return 0.0
    token = str(side or "").lower()
    if token.startswith("buy"):
        return _bps_change(observed, benchmark)
    if token.startswith("sell"):
        return _bps_change(benchmark, observed)
    return _bps_change(observed, benchmark)


def _bar_stats(source: str, symbol: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    sorted_bars = sorted(bars, key=_bar_time)
    if not sorted_bars:
        return {
            "source": source,
            "symbol": symbol,
            "bar_count": 0,
        }
    open_price = _safe_float(sorted_bars[0].get("o") or sorted_bars[0].get("open"))
    close_price = _safe_float(sorted_bars[-1].get("c") or sorted_bars[-1].get("close"))
    highs = [_safe_float(row.get("h") or row.get("high")) for row in sorted_bars]
    lows = [_safe_float(row.get("l") or row.get("low")) for row in sorted_bars]
    closes = [_safe_float(row.get("c") or row.get("close")) for row in sorted_bars]
    volumes = [_safe_float(row.get("v") or row.get("volume")) for row in sorted_bars]
    vwaps = [_safe_float(row.get("vw") or row.get("vwap")) for row in sorted_bars]
    total_volume = sum(volumes)
    vwap_numer = sum(vwap * volume for vwap, volume in zip(vwaps, volumes) if vwap > 0 and volume > 0)
    if total_volume > 0 and vwap_numer > 0:
        vwap = vwap_numer / total_volume
    elif total_volume > 0:
        typical_numer = 0.0
        for row, volume in zip(sorted_bars, volumes):
            high = _safe_float(row.get("h") or row.get("high"))
            low = _safe_float(row.get("l") or row.get("low"))
            close = _safe_float(row.get("c") or row.get("close"))
            typical = (high + low + close) / 3.0 if high > 0 and low > 0 and close > 0 else close
            typical_numer += typical * volume
        vwap = typical_numer / total_volume if typical_numer > 0 else 0.0
    else:
        nonzero_closes = [value for value in closes if value > 0]
        vwap = sum(nonzero_closes) / len(nonzero_closes) if nonzero_closes else 0.0
    high_price = max([value for value in highs if value > 0], default=0.0)
    low_price = min([value for value in lows if value > 0], default=0.0)
    return {
        "source": source,
        "symbol": symbol,
        "bar_count": len(sorted_bars),
        "first_bar_time": _bar_time(sorted_bars[0]),
        "last_bar_time": _bar_time(sorted_bars[-1]),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "vwap": vwap,
        "volume": total_volume,
        "trade_count": sum(_safe_float(row.get("n") or row.get("trade_count")) for row in sorted_bars),
        "range_bps": _bps_change(high_price, low_price),
        "close_vs_open_bps": _bps_change(close_price, open_price),
    }


BENCHMARK_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA"}


def _beta_bucket(beta: float) -> str:
    if beta <= 0:
        return "missing_or_zero"
    if beta < 0.8:
        return "low_beta_lt_0_8"
    if beta <= 1.2:
        return "market_beta_0_8_1_2"
    return "high_beta_gt_1_2"


def _side_sign(side: Any) -> int:
    token = str(side or "").lower()
    if "short" in token:
        return -1
    if "long" in token:
        return 1
    return 0


def _context_bucket_rows(*, rows: list[dict[str, Any]], value_getter: Any) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "symbol_count": 0,
            "long_symbol_count": 0,
            "short_symbol_count": 0,
            "gross_after_market_value": 0.0,
            "net_after_market_value": 0.0,
            "snapshot_intraday_pnl": 0.0,
            "realized_pnl_estimate": 0.0,
            "implementation_shortfall_notional": 0.0,
            "position_unexplained_notional": 0.0,
            "signed_beta_exposure": 0.0,
            "filled_notional_at_reference": 0.0,
            "largest_losses": [],
        }
    )
    for row in rows:
        key = str(value_getter(row) or "unknown")
        bucket = buckets[key]
        after_mv = _safe_float(row.get("after_market_value"))
        side_sign = _side_sign(row.get("after_side") or row.get("target_side"))
        total_pnl = _safe_float(row.get("snapshot_intraday_pnl")) + _safe_float(row.get("realized_pnl_estimate"))
        bucket["symbol_count"] += 1
        bucket["long_symbol_count"] += 1 if side_sign > 0 else 0
        bucket["short_symbol_count"] += 1 if side_sign < 0 else 0
        bucket["gross_after_market_value"] += abs(after_mv)
        bucket["net_after_market_value"] += after_mv
        bucket["snapshot_intraday_pnl"] += _safe_float(row.get("snapshot_intraday_pnl"))
        bucket["realized_pnl_estimate"] += _safe_float(row.get("realized_pnl_estimate"))
        bucket["implementation_shortfall_notional"] += _safe_float(row.get("implementation_shortfall_notional"))
        bucket["position_unexplained_notional"] += _safe_float(row.get("position_unexplained_notional"))
        bucket["signed_beta_exposure"] += after_mv * _safe_float(row.get("beta"))
        bucket["filled_notional_at_reference"] += _safe_float(row.get("filled_notional_at_reference"))
        bucket["largest_losses"].append(
            {
                "symbol": row.get("symbol"),
                "after_side": row.get("after_side"),
                "after_market_value": after_mv,
                "snapshot_intraday_pnl": row.get("snapshot_intraday_pnl"),
                "realized_pnl_estimate": row.get("realized_pnl_estimate"),
                "snapshot_plus_realized_pnl": total_pnl,
            }
        )
    out: list[dict[str, Any]] = []
    for key, bucket in buckets.items():
        gross = _safe_float(bucket.get("gross_after_market_value"))
        pnl = _safe_float(bucket.get("snapshot_intraday_pnl")) + _safe_float(bucket.get("realized_pnl_estimate"))
        out.append(
            {
                "bucket": key,
                "symbol_count": _safe_int(bucket.get("symbol_count")),
                "long_symbol_count": _safe_int(bucket.get("long_symbol_count")),
                "short_symbol_count": _safe_int(bucket.get("short_symbol_count")),
                "gross_after_market_value": gross,
                "net_after_market_value": _safe_float(bucket.get("net_after_market_value")),
                "snapshot_intraday_pnl": _safe_float(bucket.get("snapshot_intraday_pnl")),
                "realized_pnl_estimate": _safe_float(bucket.get("realized_pnl_estimate")),
                "snapshot_plus_realized_pnl": pnl,
                "pnl_bps_of_gross_after_market_value": pnl / gross * 10000.0 if gross > 0 else 0.0,
                "implementation_shortfall_notional": _safe_float(bucket.get("implementation_shortfall_notional")),
                "position_unexplained_notional": _safe_float(bucket.get("position_unexplained_notional")),
                "signed_beta_exposure": _safe_float(bucket.get("signed_beta_exposure")),
                "filled_notional_at_reference": _safe_float(bucket.get("filled_notional_at_reference")),
                "largest_losses": _json_cell(
                    sorted(
                        bucket["largest_losses"],
                        key=lambda item: _safe_float(item.get("snapshot_plus_realized_pnl")),
                    )[:10]
                ),
            }
        )
    return sorted(out, key=lambda item: _safe_float(item.get("snapshot_plus_realized_pnl")))


def _factor_owners_for_symbol(lot_rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in lot_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        factor = str(row.get("factor") or "unknown")
        if symbol:
            out[symbol][factor] += _safe_float(row.get("weight"))
    return {symbol: dict(factors) for symbol, factors in out.items()}


def _build_market_context_attribution(
    *,
    run_dir: Path,
    decision_rows: list[dict[str, Any]],
    lot_rows: list[dict[str, Any]],
    symbol_attribution_rows: list[dict[str, Any]],
    market_price_evidence_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in decision_rows}
    price_by_symbol = {str(row.get("symbol") or "").upper().strip(): row for row in market_price_evidence_rows}
    factor_by_symbol = _factor_owners_for_symbol(lot_rows)
    enriched_rows: list[dict[str, Any]] = []
    for row in symbol_attribution_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        decision = decision_by_symbol.get(symbol, {})
        price = price_by_symbol.get(symbol, {})
        after_mv = _safe_float(row.get("after_market_value"))
        snapshot_pnl = _safe_float(row.get("snapshot_intraday_pnl"))
        realized_pnl = _safe_float(row.get("realized_pnl_estimate"))
        total_pnl = snapshot_pnl + realized_pnl
        beta = _safe_float(decision.get("beta"))
        factors = factor_by_symbol.get(symbol, {})
        primary_factor = max(factors.items(), key=lambda item: abs(_safe_float(item[1])))[0] if factors else "unattributed"
        gross_mv = abs(after_mv)
        enriched_rows.append(
            {
                "row_type": "symbol",
                "bucket": "symbol",
                "symbol": symbol,
                "target_side": row.get("target_side", ""),
                "after_side": row.get("after_side", ""),
                "sic2_sector": decision.get("sic2_sector", "") or "unknown",
                "primary_factor": primary_factor,
                "factor_weights": _json_cell(factors),
                "beta": beta,
                "beta_bucket": _beta_bucket(beta),
                "after_market_value": after_mv,
                "gross_after_market_value": gross_mv,
                "signed_beta_exposure": after_mv * beta,
                "change_today": _safe_float(decision.get("change_today")),
                "snapshot_intraday_pnl": snapshot_pnl,
                "realized_pnl_estimate": realized_pnl,
                "snapshot_plus_realized_pnl": total_pnl,
                "pnl_bps_of_gross_after_market_value": total_pnl / gross_mv * 10000.0 if gross_mv > 0 else 0.0,
                "implementation_shortfall_notional": _safe_float(row.get("implementation_shortfall_notional")),
                "position_unexplained_notional": _safe_float(row.get("position_unexplained_notional")),
                "filled_notional_at_reference": _safe_float(row.get("filled_notional_at_reference")),
                "execute_reference_price": _safe_float(price.get("execute_reference_price_used")),
                "decision_execute_reference_change_bps": _safe_float(price.get("decision_execute_reference_change_bps")),
                "context_note": "symbol-level strategy PnL context from local decision, position, fill, and price evidence",
            }
        )

    sector_rows = _context_bucket_rows(
        rows=enriched_rows,
        value_getter=lambda row: f"sector:{row.get('sic2_sector') or 'unknown'}",
    )
    factor_rows = _context_bucket_rows(
        rows=enriched_rows,
        value_getter=lambda row: f"factor:{row.get('primary_factor') or 'unattributed'}",
    )
    beta_rows = _context_bucket_rows(
        rows=enriched_rows,
        value_getter=lambda row: f"beta:{row.get('beta_bucket') or 'unknown'}",
    )
    side_rows = _context_bucket_rows(
        rows=enriched_rows,
        value_getter=lambda row: f"side:{row.get('after_side') or row.get('target_side') or 'unknown'}",
    )
    aggregate_rows: list[dict[str, Any]] = []
    for source, source_rows in [
        ("sector", sector_rows),
        ("factor", factor_rows),
        ("beta", beta_rows),
        ("side", side_rows),
    ]:
        for row in source_rows:
            aggregate_rows.append({"row_type": source, "symbol": "", **row, "context_note": f"{source} aggregate"})

    benchmark_symbols = set(BENCHMARK_SYMBOLS)
    price_snapshot = _snapshot_price_maps(_read_json(run_dir / "execution_price_snapshot.json", {}))
    benchmark_symbols |= set(price_snapshot.get("audit_benchmark_symbols", []))
    before_bars = _intraday_bar_rows(_read_json(run_dir / "execution_intraday_bars_1min.json", {}))
    after_bars = _intraday_bar_rows(_read_json(run_dir / "execution_intraday_bars_1min_after.json", {}))
    bars_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bar in [*before_bars, *after_bars]:
        symbol = str(bar.get("symbol") or "").upper().strip()
        if symbol in benchmark_symbols:
            bars_by_symbol[symbol].append(bar)
    benchmark_rows: list[dict[str, Any]] = []
    for symbol in sorted(benchmark_symbols):
        stats = _bar_stats("benchmark_bars", symbol, bars_by_symbol.get(symbol, []))
        benchmark_rows.append(
            {
                "row_type": "benchmark",
                "bucket": f"benchmark:{symbol}",
                "symbol": symbol,
                "bar_count": stats.get("bar_count"),
                "first_bar_time": stats.get("first_bar_time"),
                "last_bar_time": stats.get("last_bar_time"),
                "open": stats.get("open"),
                "high": stats.get("high"),
                "low": stats.get("low"),
                "close": stats.get("close"),
                "vwap": stats.get("vwap"),
                "range_bps": stats.get("range_bps"),
                "close_vs_open_bps": stats.get("close_vs_open_bps"),
                "context_note": "benchmark ETF intraday bars captured with execution price evidence when available",
            }
        )

    all_rows = [*aggregate_rows, *benchmark_rows, *enriched_rows]
    total_snapshot = sum(_safe_float(row.get("snapshot_intraday_pnl")) for row in enriched_rows)
    total_realized = sum(_safe_float(row.get("realized_pnl_estimate")) for row in enriched_rows)
    gross_after = sum(_safe_float(row.get("gross_after_market_value")) for row in enriched_rows)
    net_after = sum(_safe_float(row.get("after_market_value")) for row in enriched_rows)
    signed_beta_exposure = sum(_safe_float(row.get("signed_beta_exposure")) for row in enriched_rows)
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pass" if enriched_rows else "historical_limited",
        "symbol_count": len(enriched_rows),
        "row_count": len(all_rows),
        "benchmark_symbol_count": len(benchmark_symbols),
        "benchmark_symbols": sorted(benchmark_symbols),
        "benchmark_symbols_with_bars": sorted(symbol for symbol, bars in bars_by_symbol.items() if bars),
        "snapshot_intraday_pnl": total_snapshot,
        "realized_pnl_estimate": total_realized,
        "snapshot_plus_realized_pnl": total_snapshot + total_realized,
        "gross_after_market_value": gross_after,
        "net_after_market_value": net_after,
        "net_to_gross_after": net_after / gross_after if gross_after > 0 else None,
        "signed_beta_exposure": signed_beta_exposure,
        "net_beta_exposure_to_gross": signed_beta_exposure / gross_after if gross_after > 0 else None,
        "worst_symbols": sorted(enriched_rows, key=lambda row: _safe_float(row.get("snapshot_plus_realized_pnl")))[:20],
        "worst_sectors": sector_rows[:10],
        "worst_factors": factor_rows[:10],
        "beta_buckets": beta_rows,
        "side_buckets": side_rows,
        "benchmark_rows": benchmark_rows,
        "note": (
            "This is local market/factor context, not a factor model. "
            "It helps separate side/sector/beta/factor concentration from execution and account-state effects."
        ),
    }
    return all_rows, summary_payload


def _rows_by_symbol(rows: list[dict[str, Any]], *, row_type: str | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row_type is not None and str(row.get("row_type") or "") != row_type:
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        if symbol:
            out[symbol] = row
    return out


def _aggregate_order_constraints_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "row_count": 0,
            "planned_order_count": 0,
            "skipped_order_count": 0,
            "whole_share_required_count": 0,
            "planned_abs_notional": 0.0,
            "filled_notional_estimate": 0.0,
            "unfilled_notional_estimate": 0.0,
            "fill_abs_notional": 0.0,
            "action_classes": set(),
            "skip_reasons": set(),
            "status_latest_set": set(),
            "whole_share_reasons": set(),
        }
    )
    for row in rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        bucket = buckets[symbol]
        bucket["row_count"] += 1
        if str(row.get("row_type") or "") == "planned_order":
            bucket["planned_order_count"] += 1
        if bool(row.get("skipped")) or str(row.get("row_type") or "") == "skipped_order":
            bucket["skipped_order_count"] += 1
        if bool(row.get("whole_share_required")):
            bucket["whole_share_required_count"] += 1
        bucket["planned_abs_notional"] += _safe_float(row.get("planned_abs_notional"))
        bucket["filled_notional_estimate"] += _safe_float(row.get("filled_notional_estimate"))
        bucket["unfilled_notional_estimate"] += _safe_float(row.get("unfilled_notional_estimate"))
        bucket["fill_abs_notional"] += _safe_float(row.get("fill_abs_notional"))
        for key, target in [
            ("action_class", "action_classes"),
            ("skip_reason", "skip_reasons"),
            ("status_latest_set", "status_latest_set"),
            ("whole_share_reason", "whole_share_reasons"),
        ]:
            for item in str(row.get(key) or "").split(";"):
                text = item.strip()
                if text:
                    bucket[target].add(text)
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, bucket in buckets.items():
        normalized[symbol] = {
            key: (";".join(sorted(value)) if isinstance(value, set) else value)
            for key, value in bucket.items()
        }
    return normalized


def _focus_primary_bucket(
    *,
    snapshot_plus_realized_pnl: float,
    realized_pnl: float,
    snapshot_pnl: float,
    implementation_shortfall: float,
    position_residual: float,
    material_position_residual: bool,
    target_error_abs: float,
    planned_delta_change: float,
    evidence_gap_count: int,
) -> str:
    if material_position_residual or abs(position_residual) > 100.0:
        return "position_snapshot_residual"
    if evidence_gap_count > 0 and abs(snapshot_plus_realized_pnl) <= 10.0:
        return "evidence_gap"
    if target_error_abs > 100.0:
        return "target_transition_gap"
    if abs(planned_delta_change) > 100.0:
        return "decision_execute_drift"
    if implementation_shortfall > 5.0:
        return "execution_shortfall"
    if realized_pnl < -10.0:
        return "realized_pnl_loss"
    if snapshot_pnl < -10.0:
        return "mark_to_market_loss"
    if snapshot_plus_realized_pnl < -10.0:
        return "strategy_symbol_loss"
    return "context"


def _focus_next_action(bucket: str) -> str:
    return {
        "position_snapshot_residual": (
            "Inspect raw/stability position snapshots, broker activity, and corporate-action evidence before using this row as strategy PnL."
        ),
        "evidence_gap": "Wait for a future upgraded execute run or inspect raw broker/API artifacts before drawing causality.",
        "target_transition_gap": "Compare target transition, order constraints, fills, and after position snapshot for underfill or sizing drift.",
        "decision_execute_drift": "Compare 46_decision_execute_drift.csv with decision and execute order_plan.json.",
        "execution_shortfall": "Inspect order attempts, limit offsets, fills, quotes, and intraday VWAP context.",
        "realized_pnl_loss": "Inspect 08_realized_pnl_ledger.csv and pre-trade lot ownership/cost basis.",
        "mark_to_market_loss": "Inspect market/factor context, beta/sector bucket, and same-day price path.",
        "strategy_symbol_loss": "Inspect symbol-level market/factor context and realized/snapshot split.",
        "context": "Use this row as supporting context after higher-priority focus rows.",
    }.get(bucket, "Inspect linked audit artifacts.")


def _build_attribution_dossier(
    *,
    context: dict[str, Any],
    summary: dict[str, Any],
    equity_pnl_bridge: dict[str, Any],
    symbol_attribution_rows: list[dict[str, Any]],
    target_transition_rows: list[dict[str, Any]],
    decision_intent_rows: list[dict[str, Any]],
    order_constraint_rows: list[dict[str, Any]],
    decision_execute_drift_rows: list[dict[str, Any]],
    market_price_evidence_rows: list[dict[str, Any]],
    intraday_bar_rows: list[dict[str, Any]],
    quote_rows: list[dict[str, Any]],
    market_context_rows: list[dict[str, Any]],
    market_context_summary: dict[str, Any],
    residual_diagnosis_summary: dict[str, Any],
    evidence_completeness_summary: dict[str, Any],
    strict_attribution_checklist_summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    symbol_attr_by_symbol = _rows_by_symbol(symbol_attribution_rows)
    transition_by_symbol = _rows_by_symbol(target_transition_rows)
    decision_intent_by_symbol = _rows_by_symbol(decision_intent_rows)
    order_constraints_by_symbol = _aggregate_order_constraints_by_symbol(order_constraint_rows)
    drift_by_symbol = _rows_by_symbol(decision_execute_drift_rows)
    market_price_by_symbol = _rows_by_symbol(market_price_evidence_rows)
    intraday_by_symbol = _rows_by_symbol(intraday_bar_rows)
    quote_by_symbol = _rows_by_symbol(quote_rows)
    market_context_by_symbol = _rows_by_symbol(market_context_rows, row_type="symbol")
    symbols = sorted(
        set(symbol_attr_by_symbol)
        | set(transition_by_symbol)
        | set(decision_intent_by_symbol)
        | set(order_constraints_by_symbol)
        | set(drift_by_symbol)
        | set(market_price_by_symbol)
        | set(intraday_by_symbol)
        | set(quote_by_symbol)
        | set(market_context_by_symbol)
    )

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_attr = symbol_attr_by_symbol.get(symbol, {})
        transition = transition_by_symbol.get(symbol, {})
        intent = decision_intent_by_symbol.get(symbol, {})
        constraints = order_constraints_by_symbol.get(symbol, {})
        drift = drift_by_symbol.get(symbol, {})
        price = market_price_by_symbol.get(symbol, {})
        bars = intraday_by_symbol.get(symbol, {})
        quotes = quote_by_symbol.get(symbol, {})
        market_ctx = market_context_by_symbol.get(symbol, {})

        snapshot_pnl = _safe_float(symbol_attr.get("snapshot_intraday_pnl"))
        realized_pnl = _safe_float(symbol_attr.get("realized_pnl_estimate"))
        total_pnl = snapshot_pnl + realized_pnl
        implementation_shortfall = _safe_float(symbol_attr.get("implementation_shortfall_notional"))
        position_residual = _safe_float(symbol_attr.get("position_unexplained_notional"))
        target_error_abs = _safe_float(transition.get("target_error_abs"))
        planned_delta_change = _safe_float(drift.get("planned_delta_notional_change"))
        unfilled_notional = _safe_float(constraints.get("unfilled_notional_estimate"))
        skipped_order_count = _safe_int(constraints.get("skipped_order_count"))
        material_residual = bool(transition.get("material_position_residual")) or abs(position_residual) > 100.0

        evidence_tags: list[str] = []
        price_status = str(price.get("status") or "")
        bar_status = str(bars.get("status") or "")
        quote_status = str(quotes.get("status") or "")
        if bool(price.get("missing_reference_flag")) or "missing" in price_status:
            evidence_tags.append("price_reference_gap")
        if bar_status in {"historical_limited", "not_available", "attention"}:
            evidence_tags.append("intraday_bar_limited")
        if quote_status in {"historical_limited", "not_available", "attention"}:
            evidence_tags.append("quote_limited")
        if _safe_float(quotes.get("spread_bps")) > 50.0:
            evidence_tags.append("wide_spread")

        focus_tags: list[str] = []
        if total_pnl < -10.0:
            focus_tags.append("loss")
        if realized_pnl < -10.0:
            focus_tags.append("realized_loss")
        if snapshot_pnl < -10.0:
            focus_tags.append("mark_loss")
        if material_residual:
            focus_tags.append("position_residual")
        if target_error_abs > 100.0:
            focus_tags.append("target_gap")
        if abs(planned_delta_change) > 100.0:
            focus_tags.append("decision_execute_drift")
        if implementation_shortfall > 5.0:
            focus_tags.append("execution_shortfall")
        if unfilled_notional > 50.0:
            focus_tags.append("unfilled_order")
        if skipped_order_count > 0:
            focus_tags.append("skipped_order")
        if str(intent.get("projection_reason") or "").startswith("short_floor"):
            focus_tags.append("short_floor_projection")
        focus_tags.extend(evidence_tags)

        evidence_gap_count = len(evidence_tags)
        primary_bucket = _focus_primary_bucket(
            snapshot_plus_realized_pnl=total_pnl,
            realized_pnl=realized_pnl,
            snapshot_pnl=snapshot_pnl,
            implementation_shortfall=implementation_shortfall,
            position_residual=position_residual,
            material_position_residual=material_residual,
            target_error_abs=target_error_abs,
            planned_delta_change=planned_delta_change,
            evidence_gap_count=evidence_gap_count,
        )
        focus_score = (
            max(0.0, -total_pnl) * 4.0
            + max(0.0, implementation_shortfall) * 2.0
            + abs(position_residual) * 0.75
            + target_error_abs * 0.35
            + abs(planned_delta_change) * 0.25
            + unfilled_notional * 0.25
            + evidence_gap_count * 25.0
        )
        rows.append(
            {
                "focus_rank": 0,
                "symbol": symbol,
                "focus_score": focus_score,
                "focus_tags": ";".join(dict.fromkeys(focus_tags)),
                "primary_attribution_bucket": primary_bucket,
                "next_review_action": _focus_next_action(primary_bucket),
                "snapshot_plus_realized_pnl": total_pnl,
                "snapshot_intraday_pnl": snapshot_pnl,
                "realized_pnl_estimate": realized_pnl,
                "implementation_shortfall_notional": implementation_shortfall,
                "position_unexplained_notional": position_residual,
                "position_residual_reason_hint": symbol_attr.get("position_residual_reason_hint", ""),
                "material_position_residual": bool(material_residual),
                "target_intent": transition.get("intent", ""),
                "target_outcome": transition.get("outcome", ""),
                "target_confidence": transition.get("confidence", ""),
                "target_error_market_value": _safe_float(transition.get("target_error_market_value")),
                "target_error_abs": target_error_abs,
                "planned_order_count": _safe_int(transition.get("planned_order_count") or constraints.get("planned_order_count")),
                "fill_count": _safe_int(symbol_attr.get("fill_count") or transition.get("fill_count")),
                "planned_abs_notional": _safe_float(symbol_attr.get("planned_abs_notional") or constraints.get("planned_abs_notional")),
                "unfilled_notional_estimate": unfilled_notional,
                "skipped_order_count": skipped_order_count,
                "whole_share_required_count": _safe_int(constraints.get("whole_share_required_count")),
                "order_status_latest_set": constraints.get("status_latest_set", ""),
                "skip_reasons": constraints.get("skip_reasons", ""),
                "action_classes": constraints.get("action_classes", ""),
                "projection_reason": intent.get("projection_reason", ""),
                "raw_target_signed_weight": _safe_float(intent.get("raw_target_signed_weight")),
                "projected_target_signed_weight": _safe_float(intent.get("projected_target_signed_weight")),
                "projection_delta_notional_estimate": _safe_float(intent.get("projection_delta_notional_estimate")),
                "decision_execute_order_presence": drift.get("order_presence", ""),
                "decision_execute_drift_reasons": drift.get("drift_reasons", ""),
                "decision_execute_planned_delta_change": planned_delta_change,
                "decision_execute_target_notional_delta": _safe_float(drift.get("target_notional_delta_estimate")),
                "market_price_status": price_status,
                "decision_execute_reference_change_bps": _safe_float(price.get("decision_execute_reference_change_bps")),
                "fill_vwap_vs_reference_bps": _safe_float(price.get("fill_vwap_vs_reference_bps")),
                "missing_reference_flag": bool(price.get("missing_reference_flag")),
                "intraday_bar_status": bar_status,
                "intraday_range_bps": _safe_float(bars.get("range_bps")),
                "fill_vwap_vs_vwap_adverse_bps": _safe_float(bars.get("fill_vwap_vs_vwap_adverse_bps")),
                "quote_status": quote_status,
                "spread_bps": _safe_float(quotes.get("spread_bps")),
                "fill_vwap_vs_mid_adverse_bps": _safe_float(quotes.get("fill_vwap_vs_mid_adverse_bps")),
                "after_side": market_ctx.get("after_side") or symbol_attr.get("after_side", ""),
                "target_side": market_ctx.get("target_side") or symbol_attr.get("target_side", ""),
                "sic2_sector": market_ctx.get("sic2_sector", ""),
                "primary_factor": market_ctx.get("primary_factor", ""),
                "beta": _safe_float(market_ctx.get("beta")),
                "beta_bucket": market_ctx.get("beta_bucket", ""),
                "change_today": _safe_float(market_ctx.get("change_today")),
                "after_market_value": _safe_float(market_ctx.get("after_market_value") or symbol_attr.get("after_market_value")),
                "evidence_gap_count": evidence_gap_count,
                "evidence_gap_tags": ";".join(evidence_tags),
                "supporting_artifacts": (
                    "35_symbol_attribution_bridge.csv; 40_target_transition_trace.csv; "
                    "42_decision_intent_trace.csv; 44_order_constraint_trace.csv; "
                    "46_decision_execute_drift.csv; 48_market_price_evidence.csv; "
                    "58_intraday_bar_evidence.csv; 60_quote_evidence.csv; 66_market_context_attribution.csv"
                ),
            }
        )

    rows = sorted(rows, key=lambda row: _safe_float(row.get("focus_score")), reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["focus_rank"] = rank

    component_amounts = (
        equity_pnl_bridge.get("component_amounts", {})
        if isinstance(equity_pnl_bridge, dict) and isinstance(equity_pnl_bridge.get("component_amounts"), dict)
        else {}
    )
    strict_blockers = strict_attribution_checklist_summary.get("top_blockers", [])
    strict_blockers = strict_blockers if isinstance(strict_blockers, list) else []
    coverage_gaps = evidence_completeness_summary.get("lowest_coverage_areas", [])
    coverage_gaps = coverage_gaps if isinstance(coverage_gaps, list) else []
    top_loss_symbols = sorted(rows, key=lambda row: _safe_float(row.get("snapshot_plus_realized_pnl")))[:20]
    residual_rows = sorted(
        [row for row in rows if bool(row.get("material_position_residual")) or abs(_safe_float(row.get("position_unexplained_notional"))) > 0],
        key=lambda row: abs(_safe_float(row.get("position_unexplained_notional"))),
        reverse=True,
    )[:20]
    target_gap_rows = sorted(rows, key=lambda row: _safe_float(row.get("target_error_abs")), reverse=True)[:20]
    shortfall_rows = sorted(rows, key=lambda row: _safe_float(row.get("implementation_shortfall_notional")), reverse=True)[:20]
    drift_rows = sorted(rows, key=lambda row: abs(_safe_float(row.get("decision_execute_planned_delta_change"))), reverse=True)[:20]
    evidence_gap_rows = [row for row in rows if _safe_int(row.get("evidence_gap_count")) > 0][:20]
    primary_bucket_counts = Counter(str(row.get("primary_attribution_bucket") or "") for row in rows)
    focus_tag_counts = Counter(
        tag
        for row in rows
        for tag in str(row.get("focus_tags") or "").split(";")
        if tag
    )
    status = (
        "attention"
        if strict_attribution_checklist_summary.get("status") in {"blocked", "attention"}
        or residual_diagnosis_summary.get("status") == "attention"
        else "partial"
        if not evidence_completeness_summary.get("strict_account_position_replay_ready")
        else "pass"
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "session_date": context.get("session_date") or summary.get("decision_date"),
        "run_dir": context.get("run_dir"),
        "focus_symbol_count": len(rows),
        "focus_row_count": len(rows),
        "primary_bucket_counts": dict(sorted(primary_bucket_counts.items())),
        "focus_tag_counts": dict(sorted(focus_tag_counts.items())),
        "equity_change": _safe_float(component_amounts.get("broker_equity_change")),
        "equity_bridge_residual": _safe_float(component_amounts.get("unexplained_after_snapshot_intraday_realized_activity")),
        "snapshot_intraday_pnl": _safe_float(component_amounts.get("snapshot_unrealized_intraday_pnl")),
        "realized_pnl_estimate": _safe_float(component_amounts.get("realized_pnl_estimate")),
        "execution_shortfall_cost_estimate": _safe_float(component_amounts.get("execution_shortfall_cost_estimate")),
        "market_context_snapshot_plus_realized_pnl": market_context_summary.get("snapshot_plus_realized_pnl"),
        "market_context_net_beta_exposure_to_gross": market_context_summary.get("net_beta_exposure_to_gross"),
        "strict_attribution_status": strict_attribution_checklist_summary.get("status"),
        "strict_attribution_ready": strict_attribution_checklist_summary.get("strict_attribution_ready"),
        "strict_attribution_blocking_items": strict_attribution_checklist_summary.get("blocking_item_count"),
        "strict_account_position_replay_ready": evidence_completeness_summary.get("strict_account_position_replay_ready"),
        "evidence_gap_count": len(coverage_gaps),
        "coverage_gaps": coverage_gaps[:12],
        "strict_blockers": strict_blockers[:12],
        "top_focus_symbols": rows[:25],
        "top_loss_symbols": top_loss_symbols,
        "top_position_residual_symbols": residual_rows,
        "top_target_gap_symbols": target_gap_rows,
        "top_execution_shortfall_symbols": shortfall_rows,
        "top_decision_execute_drift_symbols": drift_rows,
        "top_evidence_gap_symbols": evidence_gap_rows,
        "worst_sectors": market_context_summary.get("worst_sectors", []),
        "worst_factors": market_context_summary.get("worst_factors", []),
        "side_buckets": market_context_summary.get("side_buckets", []),
        "beta_buckets": market_context_summary.get("beta_buckets", []),
        "interpretation": (
            "This dossier is a review index. It ranks where to inspect first and links each row to the "
            "audit artifacts needed for strict attribution. It does not override strict replay blockers."
        ),
    }
    return rows, summary_payload


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _split_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in str(value or "").replace(",", ";").split(";"):
            text = token.strip()
            if text:
                tokens.add(text)
    return tokens


def _aggregate_order_gap_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "row_count": 0,
            "submitted_order_count": 0,
            "skipped_order_count": 0,
            "whole_share_required_count": 0,
            "submitted_planned_abs_notional": 0.0,
            "submitted_filled_notional": 0.0,
            "submitted_unfilled_notional": 0.0,
            "skipped_notional": 0.0,
            "gross_order_gap_notional": 0.0,
            "fill_count": 0,
            "action_classes": set(),
            "skip_reasons": set(),
            "status_latest_set": set(),
            "whole_share_reasons": set(),
        }
    )
    for row in rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        bucket = buckets[symbol]
        row_type = str(row.get("row_type") or "")
        planned_abs = _safe_float(row.get("planned_abs_notional"))
        filled = _safe_float(row.get("filled_notional_estimate"))
        unfilled = _safe_float(row.get("unfilled_notional_estimate"))
        skipped = _truthy(row.get("skipped")) or row_type == "skipped_order"
        submitted = row_type == "planned_order"
        bucket["row_count"] += 1
        if submitted:
            bucket["submitted_order_count"] += 1
            bucket["submitted_planned_abs_notional"] += planned_abs
            bucket["submitted_filled_notional"] += filled
            bucket["submitted_unfilled_notional"] += unfilled
        if skipped:
            bucket["skipped_order_count"] += 1
            bucket["skipped_notional"] += planned_abs or unfilled
        if _truthy(row.get("whole_share_required")):
            bucket["whole_share_required_count"] += 1
        bucket["fill_count"] += _safe_int(row.get("fill_count"))
        for key, target in [
            ("action_class", "action_classes"),
            ("skip_reason", "skip_reasons"),
            ("status_latest_set", "status_latest_set"),
            ("whole_share_reason", "whole_share_reasons"),
        ]:
            bucket[target].update(_split_tokens(row.get(key)))
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, bucket in buckets.items():
        submitted_planned = _safe_float(bucket.get("submitted_planned_abs_notional"))
        submitted_filled = _safe_float(bucket.get("submitted_filled_notional"))
        skipped_notional = _safe_float(bucket.get("skipped_notional"))
        submitted_unfilled = _safe_float(bucket.get("submitted_unfilled_notional"))
        bucket["gross_order_gap_notional"] = skipped_notional + submitted_unfilled
        bucket["submitted_fill_rate_notional"] = submitted_filled / submitted_planned if submitted_planned > 0 else None
        normalized[symbol] = {
            key: (";".join(sorted(value)) if isinstance(value, set) else value)
            for key, value in bucket.items()
        }
    return normalized


def _primary_gap_bucket(
    *,
    material_position_residual: bool,
    equity: float,
    projection_gap_abs: float,
    decision_execute_target_drift_abs: float,
    decision_execute_order_drift_abs: float,
    skipped_notional: float,
    submitted_unfilled_notional: float,
    after_projected_target_gap_abs: float,
    ideal_actual_gap_abs: float,
) -> str:
    if material_position_residual:
        return "position_snapshot_residual"
    threshold = max(100.0, 0.0025 * abs(equity)) if equity else 100.0
    components = {
        "projection_constraints": projection_gap_abs,
        "decision_execute_target_drift": decision_execute_target_drift_abs,
        "decision_execute_order_drift": decision_execute_order_drift_abs,
        "skipped_orders": skipped_notional,
        "submitted_unfilled_orders": submitted_unfilled_notional,
        "after_position_target_gap": after_projected_target_gap_abs,
    }
    bucket, amount = max(components.items(), key=lambda item: item[1])
    if amount > threshold:
        return bucket
    if ideal_actual_gap_abs > threshold:
        return "ideal_actual_gap_unexplained"
    return "near_target"


def _performance_drag_bucket(
    *,
    snapshot_plus_realized_pnl: float,
    implementation_shortfall: float,
    primary_gap_bucket: str,
    gross_order_gap_notional: float,
    after_projected_target_gap_abs: float,
) -> str:
    loss_abs = max(0.0, -snapshot_plus_realized_pnl)
    shortfall_cost = max(0.0, implementation_shortfall)
    execution_gap = gross_order_gap_notional + after_projected_target_gap_abs
    if loss_abs > 10.0:
        if shortfall_cost > max(5.0, 0.20 * loss_abs):
            return "execution_shortfall_loss"
        if primary_gap_bucket not in {"near_target", "ideal_actual_gap_unexplained"} and execution_gap > 100.0:
            return "strategy_loss_with_execution_gap"
        return "market_or_signal_loss"
    if shortfall_cost > 5.0:
        return "execution_shortfall_without_symbol_loss"
    if primary_gap_bucket != "near_target":
        return "exposure_gap_without_symbol_loss"
    return "no_material_drag"


def _bucket_gap_rows(rows: list[dict[str, Any]], bucket_key: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "bucket": "",
            "symbol_count": 0,
            "snapshot_plus_realized_pnl": 0.0,
            "pnl_loss_abs": 0.0,
            "ideal_actual_gap_abs": 0.0,
            "projection_gap_abs": 0.0,
            "decision_execute_target_drift_abs": 0.0,
            "decision_execute_order_drift_abs": 0.0,
            "gross_order_gap_notional": 0.0,
            "implementation_shortfall_notional": 0.0,
            "top_symbols": [],
        }
    )
    for row in rows:
        bucket_name = str(row.get(bucket_key) or "unknown")
        bucket = buckets[bucket_name]
        bucket["bucket"] = bucket_name
        bucket["symbol_count"] += 1
        bucket["snapshot_plus_realized_pnl"] += _safe_float(row.get("snapshot_plus_realized_pnl"))
        bucket["pnl_loss_abs"] += _safe_float(row.get("pnl_loss_abs"))
        bucket["ideal_actual_gap_abs"] += _safe_float(row.get("ideal_actual_gap_abs"))
        bucket["projection_gap_abs"] += _safe_float(row.get("projection_gap_abs"))
        bucket["decision_execute_target_drift_abs"] += _safe_float(row.get("decision_execute_target_drift_abs"))
        bucket["decision_execute_order_drift_abs"] += _safe_float(row.get("decision_execute_order_drift_abs"))
        bucket["gross_order_gap_notional"] += _safe_float(row.get("gross_order_gap_notional"))
        bucket["implementation_shortfall_notional"] += _safe_float(row.get("implementation_shortfall_notional"))
        bucket["top_symbols"].append(str(row.get("symbol") or ""))
    out = []
    for bucket in buckets.values():
        bucket["top_symbols"] = ";".join([symbol for symbol in bucket["top_symbols"][:12] if symbol])
        out.append(bucket)
    return sorted(
        out,
        key=lambda row: (
            _safe_float(row.get("pnl_loss_abs")),
            _safe_float(row.get("ideal_actual_gap_abs")),
            _safe_float(row.get("gross_order_gap_notional")),
        ),
        reverse=True,
    )


def _build_ideal_vs_actual_gap(
    *,
    context: dict[str, Any],
    summary: dict[str, Any],
    symbol_attribution_rows: list[dict[str, Any]],
    target_transition_rows: list[dict[str, Any]],
    decision_intent_rows: list[dict[str, Any]],
    order_constraint_rows: list[dict[str, Any]],
    decision_execute_drift_rows: list[dict[str, Any]],
    market_context_rows: list[dict[str, Any]],
    replay_focus_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    symbol_attr_by_symbol = _rows_by_symbol(symbol_attribution_rows)
    transition_by_symbol = _rows_by_symbol(target_transition_rows)
    intent_by_symbol = _rows_by_symbol(decision_intent_rows)
    order_gap_by_symbol = _aggregate_order_gap_by_symbol(order_constraint_rows)
    drift_by_symbol = _rows_by_symbol(decision_execute_drift_rows)
    market_context_by_symbol = _rows_by_symbol(market_context_rows, row_type="symbol")
    focus_by_symbol = _rows_by_symbol(replay_focus_rows)
    equity = _safe_float(
        summary.get("account_equity_post_trade")
        or summary.get("account_equity")
        or context.get("account_equity_after")
        or context.get("account_equity_before")
    )
    symbols = sorted(
        set(symbol_attr_by_symbol)
        | set(transition_by_symbol)
        | set(intent_by_symbol)
        | set(order_gap_by_symbol)
        | set(drift_by_symbol)
        | set(market_context_by_symbol)
    )

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        symbol_attr = symbol_attr_by_symbol.get(symbol, {})
        transition = transition_by_symbol.get(symbol, {})
        intent = intent_by_symbol.get(symbol, {})
        order_gap = order_gap_by_symbol.get(symbol, {})
        drift = drift_by_symbol.get(symbol, {})
        market_ctx = market_context_by_symbol.get(symbol, {})
        focus = focus_by_symbol.get(symbol, {})

        raw_target_notional = _safe_float(intent.get("raw_target_notional_estimate"))
        projected_target_notional = _safe_float(intent.get("projected_target_notional_estimate"))
        execute_target_notional = (
            _safe_float(drift.get("execute_target_notional_estimate"))
            if drift
            else projected_target_notional
        )
        after_market_value = _safe_float(
            transition.get("after_market_value")
            or symbol_attr.get("after_market_value")
            or market_ctx.get("after_market_value")
        )
        after_raw_gap = after_market_value - raw_target_notional
        after_projected_gap = after_market_value - projected_target_notional
        after_execute_gap = after_market_value - execute_target_notional
        projection_gap = projected_target_notional - raw_target_notional
        projection_gap_abs = abs(projection_gap)
        target_drift = _safe_float(drift.get("target_notional_delta_estimate"))
        target_drift_abs = abs(target_drift)
        order_drift = _safe_float(drift.get("planned_delta_notional_change"))
        order_drift_abs = abs(order_drift)
        submitted_unfilled = _safe_float(order_gap.get("submitted_unfilled_notional"))
        skipped_notional = _safe_float(order_gap.get("skipped_notional"))
        gross_order_gap = _safe_float(order_gap.get("gross_order_gap_notional"))
        implementation_shortfall = _safe_float(symbol_attr.get("implementation_shortfall_notional"))
        snapshot_pnl = _safe_float(symbol_attr.get("snapshot_intraday_pnl") or focus.get("snapshot_intraday_pnl"))
        realized_pnl = _safe_float(symbol_attr.get("realized_pnl_estimate") or focus.get("realized_pnl_estimate"))
        total_pnl = snapshot_pnl + realized_pnl
        pnl_loss_abs = max(0.0, -total_pnl)
        material_residual = _truthy(transition.get("material_position_residual")) or _truthy(
            symbol_attr.get("material_position_residual")
        )
        primary_gap = _primary_gap_bucket(
            material_position_residual=material_residual,
            equity=equity,
            projection_gap_abs=projection_gap_abs,
            decision_execute_target_drift_abs=target_drift_abs,
            decision_execute_order_drift_abs=order_drift_abs,
            skipped_notional=skipped_notional,
            submitted_unfilled_notional=submitted_unfilled,
            after_projected_target_gap_abs=abs(after_projected_gap),
            ideal_actual_gap_abs=abs(after_raw_gap),
        )
        performance_drag = _performance_drag_bucket(
            snapshot_plus_realized_pnl=total_pnl,
            implementation_shortfall=implementation_shortfall,
            primary_gap_bucket=primary_gap,
            gross_order_gap_notional=gross_order_gap,
            after_projected_target_gap_abs=abs(after_projected_gap),
        )
        diagnostics: list[str] = []
        projection_reason = str(intent.get("projection_reason") or "")
        if projection_reason and projection_reason not in {"unchanged", "flat_or_absent"}:
            diagnostics.append(f"projection:{projection_reason}")
        diagnostics.extend(f"drift:{item}" for item in _split_tokens(drift.get("drift_reasons")))
        diagnostics.extend(f"skip:{item}" for item in _split_tokens(order_gap.get("skip_reasons")))
        diagnostics.extend(f"status:{item}" for item in _split_tokens(order_gap.get("status_latest_set")))
        if submitted_unfilled > 50.0:
            diagnostics.append("submitted_unfilled")
        if material_residual:
            diagnostics.append("position_residual")
        if total_pnl < -10.0:
            diagnostics.append("symbol_loss")

        gap_score = (
            abs(after_raw_gap)
            + projection_gap_abs * 0.50
            + target_drift_abs * 0.50
            + order_drift_abs * 0.35
            + gross_order_gap
            + max(0.0, implementation_shortfall) * 2.0
            + pnl_loss_abs * 3.0
        )
        rows.append(
            {
                "gap_rank": 0,
                "symbol": symbol,
                "primary_gap_bucket": primary_gap,
                "performance_drag_bucket": performance_drag,
                "gap_score": gap_score,
                "raw_target_signed_weight": _safe_float(intent.get("raw_target_signed_weight")),
                "projected_target_signed_weight": _safe_float(intent.get("projected_target_signed_weight")),
                "execute_projected_target_signed_weight": _safe_float(drift.get("execute_projected_target_signed_weight")),
                "raw_target_notional_estimate": raw_target_notional,
                "projected_target_notional_estimate": projected_target_notional,
                "execute_target_notional_estimate": execute_target_notional,
                "after_market_value": after_market_value,
                "ideal_actual_gap": after_raw_gap,
                "ideal_actual_gap_abs": abs(after_raw_gap),
                "after_projected_target_gap": after_projected_gap,
                "after_projected_target_gap_abs": abs(after_projected_gap),
                "after_execute_target_gap": after_execute_gap,
                "after_execute_target_gap_abs": abs(after_execute_gap),
                "projection_reason": projection_reason,
                "projection_gap_notional": projection_gap,
                "projection_gap_abs": projection_gap_abs,
                "order_intent_status": intent.get("order_intent_status", ""),
                "decision_execute_drift_reasons": drift.get("drift_reasons", ""),
                "decision_execute_order_presence": drift.get("order_presence", ""),
                "decision_execute_target_drift": target_drift,
                "decision_execute_target_drift_abs": target_drift_abs,
                "decision_execute_order_drift": order_drift,
                "decision_execute_order_drift_abs": order_drift_abs,
                "submitted_order_count": _safe_int(order_gap.get("submitted_order_count")),
                "skipped_order_count": _safe_int(order_gap.get("skipped_order_count")),
                "whole_share_required_count": _safe_int(order_gap.get("whole_share_required_count")),
                "submitted_planned_abs_notional": _safe_float(order_gap.get("submitted_planned_abs_notional")),
                "submitted_filled_notional": _safe_float(order_gap.get("submitted_filled_notional")),
                "submitted_unfilled_notional": submitted_unfilled,
                "skipped_notional": skipped_notional,
                "gross_order_gap_notional": gross_order_gap,
                "submitted_fill_rate_notional": order_gap.get("submitted_fill_rate_notional"),
                "order_action_classes": order_gap.get("action_classes", ""),
                "order_status_latest_set": order_gap.get("status_latest_set", ""),
                "skip_reasons": order_gap.get("skip_reasons", ""),
                "whole_share_reasons": order_gap.get("whole_share_reasons", ""),
                "target_intent": transition.get("intent", ""),
                "target_outcome": transition.get("outcome", ""),
                "target_confidence": transition.get("confidence", ""),
                "snapshot_intraday_pnl": snapshot_pnl,
                "realized_pnl_estimate": realized_pnl,
                "snapshot_plus_realized_pnl": total_pnl,
                "pnl_loss_abs": pnl_loss_abs,
                "implementation_shortfall_notional": implementation_shortfall,
                "position_unexplained_notional": _safe_float(
                    transition.get("position_unexplained_notional")
                    or symbol_attr.get("position_unexplained_notional")
                ),
                "material_position_residual": material_residual,
                "after_side": market_ctx.get("after_side") or symbol_attr.get("after_side", ""),
                "target_side": market_ctx.get("target_side") or symbol_attr.get("target_side", ""),
                "sic2_sector": market_ctx.get("sic2_sector", ""),
                "primary_factor": market_ctx.get("primary_factor", ""),
                "beta": _safe_float(market_ctx.get("beta")),
                "beta_bucket": market_ctx.get("beta_bucket", ""),
                "change_today": _safe_float(market_ctx.get("change_today")),
                "focus_rank": _safe_int(focus.get("focus_rank")),
                "focus_tags": focus.get("focus_tags", ""),
                "evidence_gap_count": _safe_int(focus.get("evidence_gap_count")),
                "evidence_gap_tags": focus.get("evidence_gap_tags", ""),
                "diagnostic_tags": ";".join(dict.fromkeys(diagnostics)),
                "supporting_artifacts": (
                    "42_decision_intent_trace.csv; 44_order_constraint_trace.csv; "
                    "46_decision_execute_drift.csv; 40_target_transition_trace.csv; "
                    "35_symbol_attribution_bridge.csv; 68_replay_focus_trace.csv"
                ),
            }
        )

    rows = sorted(rows, key=lambda row: _safe_float(row.get("gap_score")), reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["gap_rank"] = rank

    material_gap_rows = [
        row
        for row in rows
        if str(row.get("primary_gap_bucket") or "") != "near_target"
        or _safe_float(row.get("pnl_loss_abs")) > 10.0
    ]
    gross_ideal_actual_gap_abs = sum(_safe_float(row.get("ideal_actual_gap_abs")) for row in rows)
    gross_after_projected_target_gap_abs = sum(
        _safe_float(row.get("after_projected_target_gap_abs")) for row in rows
    )
    gross_projection_gap_abs = sum(_safe_float(row.get("projection_gap_abs")) for row in rows)
    safe_equity = max(float(equity), 1e-9)
    symbol_count = len(rows)
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "attention" if material_gap_rows else "pass",
        "session_date": context.get("session_date") or summary.get("decision_date"),
        "symbol_count": len(rows),
        "material_gap_symbol_count": len(material_gap_rows),
        "primary_gap_bucket_counts": dict(sorted(Counter(str(row.get("primary_gap_bucket") or "") for row in rows).items())),
        "performance_drag_bucket_counts": dict(
            sorted(Counter(str(row.get("performance_drag_bucket") or "") for row in rows).items())
        ),
        "strategy_to_actual_weight_error_l1": float(gross_ideal_actual_gap_abs / safe_equity),
        "strategy_to_actual_weight_error_l1_pct": float(gross_ideal_actual_gap_abs / safe_equity * 100.0),
        "strategy_to_executable_weight_error_l1": float(gross_projection_gap_abs / safe_equity),
        "strategy_to_executable_weight_error_l1_pct": float(gross_projection_gap_abs / safe_equity * 100.0),
        "executable_to_actual_weight_error_l1": float(gross_after_projected_target_gap_abs / safe_equity),
        "executable_to_actual_weight_error_l1_pct": float(
            gross_after_projected_target_gap_abs / safe_equity * 100.0
        ),
        "mean_symbol_strategy_to_actual_weight_error": float(
            gross_ideal_actual_gap_abs / safe_equity / symbol_count
        )
        if symbol_count
        else 0.0,
        "max_symbol_strategy_to_actual_weight_error": float(
            max((_safe_float(row.get("ideal_actual_gap_abs")) / safe_equity for row in rows), default=0.0)
        ),
        "gross_ideal_actual_gap_abs": gross_ideal_actual_gap_abs,
        "gross_after_projected_target_gap_abs": gross_after_projected_target_gap_abs,
        "gross_projection_gap_abs": gross_projection_gap_abs,
        "gross_decision_execute_target_drift_abs": sum(
            _safe_float(row.get("decision_execute_target_drift_abs")) for row in rows
        ),
        "gross_decision_execute_order_drift_abs": sum(
            _safe_float(row.get("decision_execute_order_drift_abs")) for row in rows
        ),
        "gross_submitted_unfilled_notional": sum(_safe_float(row.get("submitted_unfilled_notional")) for row in rows),
        "gross_skipped_notional": sum(_safe_float(row.get("skipped_notional")) for row in rows),
        "gross_order_gap_notional": sum(_safe_float(row.get("gross_order_gap_notional")) for row in rows),
        "snapshot_plus_realized_pnl": sum(_safe_float(row.get("snapshot_plus_realized_pnl")) for row in rows),
        "gross_pnl_loss_abs": sum(_safe_float(row.get("pnl_loss_abs")) for row in rows),
        "gross_implementation_shortfall_notional": sum(
            _safe_float(row.get("implementation_shortfall_notional")) for row in rows
        ),
        "top_drag_symbols": rows[:25],
        "top_ideal_actual_gaps": sorted(rows, key=lambda row: _safe_float(row.get("ideal_actual_gap_abs")), reverse=True)[:25],
        "top_projection_constraints": sorted(rows, key=lambda row: _safe_float(row.get("projection_gap_abs")), reverse=True)[:25],
        "top_decision_execute_target_drift": sorted(
            rows,
            key=lambda row: _safe_float(row.get("decision_execute_target_drift_abs")),
            reverse=True,
        )[:25],
        "top_decision_execute_order_drift": sorted(
            rows,
            key=lambda row: _safe_float(row.get("decision_execute_order_drift_abs")),
            reverse=True,
        )[:25],
        "top_order_gaps": sorted(rows, key=lambda row: _safe_float(row.get("gross_order_gap_notional")), reverse=True)[:25],
        "top_pnl_drag_symbols": sorted(rows, key=lambda row: _safe_float(row.get("snapshot_plus_realized_pnl")))[:25],
        "sector_buckets": _bucket_gap_rows(rows, "sic2_sector")[:20],
        "factor_buckets": _bucket_gap_rows(rows, "primary_factor")[:20],
        "side_buckets": _bucket_gap_rows(rows, "after_side")[:20],
        "note": (
            "Decomposes raw strategy targets versus actual after-position exposure. "
            "The buckets are diagnostic exposure gaps, not proof that a gap caused symbol PnL."
        ),
    }
    return rows, summary_payload


def _build_executable_target_projection_outputs(
    *,
    run_dir: Path,
    staged_rebuild_snapshots: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    initial = _read_json(run_dir / "executable_target_projection.json", {})
    projections: list[tuple[str, dict[str, Any]]] = []
    if isinstance(initial, dict) and initial.get("symbols"):
        projections.append(("initial", initial))

    snapshots = staged_rebuild_snapshots.get("snapshots") if isinstance(staged_rebuild_snapshots, dict) else []
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or str(snapshot.get("snapshot_type")) != "entry_rebuild":
                continue
            projection = snapshot.get("entry_executable_target_projection")
            if isinstance(projection, dict) and projection.get("symbols"):
                projections.append(("staged_entry", projection))

    rows: list[dict[str, Any]] = []
    for phase, projection in projections:
        for raw_row in projection.get("symbols") or []:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            reasons = row.get("constraint_reasons")
            if isinstance(reasons, list):
                row["constraint_reasons"] = ";".join(str(item) for item in reasons if str(item))
            row["projection_phase"] = phase
            row["buying_power"] = projection.get("buying_power")
            row["buying_power_buffer"] = projection.get("buying_power_buffer")
            row["buying_power_cap"] = projection.get("buying_power_cap")
            row["estimated_entry_buying_power_used_total"] = projection.get(
                "estimated_entry_buying_power_used"
            )
            rows.append(row)

    final_phase, final_projection = projections[-1] if projections else ("missing", {})
    initial_projection = projections[0][1] if projections else {}
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pass" if projections and bool(final_projection.get("solver", {}).get("success")) else "missing",
        "projection_count": len(projections),
        "row_count": len(rows),
        "final_projection_phase": final_phase,
        "solver": final_projection.get("solver", {}),
        "buying_power": final_projection.get("buying_power"),
        "buying_power_buffer": final_projection.get("buying_power_buffer"),
        "buying_power_cap": final_projection.get("buying_power_cap"),
        "estimated_entry_buying_power_used": final_projection.get("estimated_entry_buying_power_used"),
        "buying_power_cap_utilization": final_projection.get("buying_power_cap_utilization"),
        "tracking_error_l1_weight": final_projection.get("tracking_error_l1_weight"),
        "tracking_error_l2_weight": final_projection.get("tracking_error_l2_weight"),
        "tracking_error_l1_weight_pct": final_projection.get("tracking_error_l1_weight_pct"),
        "mean_abs_symbol_weight_error": final_projection.get("mean_abs_symbol_weight_error"),
        "mean_abs_symbol_weight_error_pct": final_projection.get("mean_abs_symbol_weight_error_pct"),
        "max_abs_symbol_weight_error": final_projection.get("max_abs_symbol_weight_error"),
        "max_abs_symbol_weight_error_pct": final_projection.get("max_abs_symbol_weight_error_pct"),
        "max_symbol_relative_target_error": final_projection.get("max_symbol_relative_target_error"),
        "integer_short_absolute_notional_gap": final_projection.get(
            "integer_short_absolute_notional_gap"
        ),
        "raw_long_gross_weight": final_projection.get("raw_long_gross_weight"),
        "raw_short_gross_weight": final_projection.get("raw_short_gross_weight"),
        "executable_long_gross_weight": final_projection.get("executable_long_gross_weight"),
        "executable_short_gross_weight": final_projection.get("executable_short_gross_weight"),
        "blocked_target_count": final_projection.get("blocked_target_count"),
        "integer_short_target_count": final_projection.get("integer_short_target_count"),
        "buying_power_buffer_scenarios": final_projection.get("buying_power_buffer_scenarios", []),
        "initial_tracking_error_l1_weight": initial_projection.get("tracking_error_l1_weight"),
        "initial_integer_short_absolute_notional_gap": initial_projection.get(
            "integer_short_absolute_notional_gap"
        ),
        "source_path": (run_dir / "executable_target_projection.json").as_posix(),
    }
    return rows, summary


def _build_position_capacity_summary(run_dir: Path) -> dict[str, Any]:
    account_after = _read_json(run_dir / "broker_account_after.json", {})
    long_market_value = _optional_float(account_after.get("long_market_value"))
    short_market_value = _optional_float(account_after.get("short_market_value"))
    regt_buying_power = _optional_float(account_after.get("regt_buying_power"))
    broker_position_market_value = _optional_float(account_after.get("position_market_value"))
    target_ratio = 0.90

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "missing",
        "source_account_snapshot": (run_dir / "broker_account_after.json").as_posix(),
        "configured_buying_power_target_ratio": target_ratio,
        "gross_long_market_value": None,
        "gross_short_market_value_abs": None,
        "gross_position_notional": None,
        "broker_position_market_value": broker_position_market_value,
        "broker_position_market_value_delta": None,
        "regt_buying_power_remaining": regt_buying_power,
        "total_regt_buying_power_capacity": None,
        "configured_gross_target_notional": None,
        "gross_utilization_of_total_bp": None,
        "gross_error_vs_target_notional": None,
        "gross_error_vs_target_pct_points": None,
        "gross_error_vs_total_notional": None,
        "gross_error_vs_total_pct_points": None,
        "note": (
            "Total RegT capacity is reconstructed as gross position notional plus remaining "
            "RegT buying power. The 90% portfolio-capacity target is an audit benchmark and is "
            "separate from the legacy incremental-entry buying-power cap."
        ),
    }
    if long_market_value is None or short_market_value is None or regt_buying_power is None:
        return payload

    gross_long = abs(float(long_market_value))
    gross_short = abs(float(short_market_value))
    gross_position = gross_long + gross_short
    total_capacity = gross_position + float(regt_buying_power)
    if total_capacity <= 0.0:
        return payload

    configured_target = target_ratio * total_capacity
    utilization = gross_position / total_capacity
    payload.update(
        {
            "status": "pass",
            "gross_long_market_value": gross_long,
            "gross_short_market_value_abs": gross_short,
            "gross_position_notional": gross_position,
            "broker_position_market_value_delta": (
                gross_position - float(broker_position_market_value)
                if broker_position_market_value is not None
                else None
            ),
            "total_regt_buying_power_capacity": total_capacity,
            "configured_gross_target_notional": configured_target,
            "gross_utilization_of_total_bp": utilization,
            "gross_error_vs_target_notional": gross_position - configured_target,
            "gross_error_vs_target_pct_points": (utilization - target_ratio) * 100.0,
            "gross_error_vs_total_notional": gross_position - total_capacity,
            "gross_error_vs_total_pct_points": (utilization - 1.0) * 100.0,
        }
    )
    return payload


def _fill_by_symbol(fill_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(float))
    times_by_symbol: dict[str, list[str]] = defaultdict(list)
    for fill in fill_rows:
        symbol = str(fill.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        qty = abs(_safe_float(fill.get("qty")))
        price = _safe_float(fill.get("price"))
        side = str(fill.get("side") or "").lower().strip()
        notional = qty * price
        bucket = out[symbol]
        bucket["fill_count"] += 1
        bucket["fill_abs_qty"] += qty
        bucket["fill_abs_notional"] += notional
        if side.startswith("buy"):
            bucket["buy_abs_notional"] += notional
            bucket["buy_count"] += 1
        elif side.startswith("sell"):
            bucket["sell_abs_notional"] += notional
            bucket["sell_count"] += 1
        timestamp = str(fill.get("transaction_time") or "")
        if timestamp:
            times_by_symbol[symbol].append(timestamp)
    normalized: dict[str, dict[str, Any]] = {}
    for symbol, bucket in out.items():
        fill_qty = _safe_float(bucket.get("fill_abs_qty"))
        buy_notional = _safe_float(bucket.get("buy_abs_notional"))
        sell_notional = _safe_float(bucket.get("sell_abs_notional"))
        if buy_notional > 0 and sell_notional > 0:
            primary_side = "mixed"
        elif buy_notional > 0:
            primary_side = "buy"
        elif sell_notional > 0:
            primary_side = "sell"
        else:
            primary_side = ""
        times = sorted(times_by_symbol.get(symbol, []))
        normalized[symbol] = {
            "fill_count": _safe_int(bucket.get("fill_count")),
            "fill_abs_qty": fill_qty,
            "fill_abs_notional": _safe_float(bucket.get("fill_abs_notional")),
            "fill_vwap": _safe_float(bucket.get("fill_abs_notional")) / fill_qty if fill_qty > 0 else 0.0,
            "primary_fill_side": primary_side,
            "buy_abs_notional": buy_notional,
            "sell_abs_notional": sell_notional,
            "first_fill_time": times[0] if times else "",
            "last_fill_time": times[-1] if times else "",
        }
    return normalized


def _build_intraday_bar_evidence(
    *,
    run_dir: Path,
    market_price_evidence_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_path = run_dir / "execution_intraday_bars_1min.json"
    after_path = run_dir / "execution_intraday_bars_1min_after.json"
    source_defs = [
        ("before_submit", before_path, _read_json(before_path, {})),
        ("after_execution", after_path, _read_json(after_path, {})),
    ]
    bars_by_source_symbol: dict[str, dict[str, list[dict[str, Any]]]] = {}
    source_summaries: dict[str, dict[str, Any]] = {}
    expected_symbols: set[str] = set()
    all_bar_symbols: set[str] = set()
    total_errors: list[dict[str, Any]] = []
    for source, path, raw in source_defs:
        source_rows = _intraday_bar_rows(raw)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in source_rows:
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            grouped[symbol].append(row)
        requested = _intraday_bar_requested_symbols(raw)
        errors = _intraday_bar_errors(raw)
        expected_symbols.update(requested)
        all_bar_symbols.update(grouped)
        total_errors.extend({"source": source, **error} for error in errors)
        payload = _intraday_bar_payload(raw)
        bars_by_source_symbol[source] = grouped
        source_summaries[source] = {
            "exists": path.exists(),
            "path": path.as_posix() if path.exists() else None,
            "ok": bool(payload.get("ok")) if "ok" in payload else None,
            "requested_symbol_count": len(requested),
            "bar_count": len(source_rows),
            "bar_symbol_count": len(grouped),
            "missing_bar_symbol_count": len(set(requested) - set(grouped)),
            "error_count": len(errors),
            "collected_at_utc": payload.get("collected_at_utc", ""),
            "label": payload.get("label") or source,
            "feed": payload.get("feed", ""),
        }

    market_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in market_price_evidence_rows
        if str(row.get("symbol") or "").strip()
    }
    fills = _fill_by_symbol(fill_rows)
    expected_symbols.update(market_by_symbol)
    expected_symbols.update(fills)
    symbols = sorted(expected_symbols | all_bar_symbols)

    before_stats = {
        symbol: _bar_stats("before_submit", symbol, bars)
        for symbol, bars in bars_by_source_symbol.get("before_submit", {}).items()
    }
    after_stats = {
        symbol: _bar_stats("after_execution", symbol, bars)
        for symbol, bars in bars_by_source_symbol.get("after_execution", {}).items()
    }

    rows: list[dict[str, Any]] = []
    any_raw_exists = before_path.exists() or after_path.exists()
    source_error_count = len(total_errors)
    for symbol in symbols:
        chosen = after_stats.get(symbol) or before_stats.get(symbol) or {}
        before = before_stats.get(symbol, {})
        after = after_stats.get(symbol, {})
        market = market_by_symbol.get(symbol, {})
        fill = fills.get(symbol, {})
        open_price = _safe_float(chosen.get("open"))
        high_price = _safe_float(chosen.get("high"))
        low_price = _safe_float(chosen.get("low"))
        close_price = _safe_float(chosen.get("close"))
        intraday_vwap = _safe_float(chosen.get("vwap"))
        execute_reference = _safe_float(market.get("execute_reference_price_used"))
        latest_trade = _safe_float(market.get("execute_latest_trade_price"))
        fill_vwap = _safe_float(fill.get("fill_vwap"))
        primary_side = str(fill.get("primary_fill_side") or "")
        hints: list[str] = []
        if not any_raw_exists:
            status = "historical_limited"
        elif _safe_int(chosen.get("bar_count")) <= 0:
            status = "missing_bars"
            hints.append("missing_intraday_bars")
        elif source_error_count:
            status = "source_errors"
        else:
            status = "pass"
        reference_position = _range_position_pct(execute_reference, low_price, high_price)
        fill_position = _range_position_pct(fill_vwap, low_price, high_price)
        latest_trade_position = _range_position_pct(latest_trade, low_price, high_price)
        if execute_reference > 0 and high_price > 0 and low_price > 0 and (execute_reference < low_price or execute_reference > high_price):
            hints.append("reference_outside_intraday_range")
        if _safe_float(chosen.get("range_bps")) > 300.0:
            hints.append("large_intraday_range")
        if fill_vwap > 0 and fill_position is not None:
            if primary_side == "buy" and fill_position >= 80.0:
                hints.append("buy_fill_near_intraday_high")
            elif primary_side == "sell" and fill_position <= 20.0:
                hints.append("sell_fill_near_intraday_low")
        fill_vs_vwap_adverse = _signed_adverse_bps(primary_side, fill_vwap, intraday_vwap)
        fill_vs_close_adverse = _signed_adverse_bps(primary_side, fill_vwap, close_price)
        if fill_vs_vwap_adverse > 25.0:
            hints.append("fill_worse_than_intraday_vwap")
        if fill_vs_close_adverse > 50.0:
            hints.append("fill_worse_than_close")
        rows.append(
            {
                "symbol": symbol,
                "status": status,
                "source_used": chosen.get("source", ""),
                "before_bar_count": _safe_int(before.get("bar_count")),
                "after_bar_count": _safe_int(after.get("bar_count")),
                "bar_count": _safe_int(chosen.get("bar_count")),
                "first_bar_time": chosen.get("first_bar_time", ""),
                "last_bar_time": chosen.get("last_bar_time", ""),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "vwap": intraday_vwap,
                "volume": _safe_float(chosen.get("volume")),
                "trade_count": _safe_float(chosen.get("trade_count")),
                "range_bps": _safe_float(chosen.get("range_bps")),
                "close_vs_open_bps": _safe_float(chosen.get("close_vs_open_bps")),
                "execute_reference_price": execute_reference,
                "reference_vs_open_bps": _bps_change(execute_reference, open_price),
                "reference_vs_vwap_bps": _bps_change(execute_reference, intraday_vwap),
                "reference_vs_close_bps": _bps_change(execute_reference, close_price),
                "reference_position_in_range_pct": reference_position,
                "execute_latest_trade_price": latest_trade,
                "latest_trade_position_in_range_pct": latest_trade_position,
                "fill_count": _safe_int(fill.get("fill_count")),
                "fill_abs_qty": _safe_float(fill.get("fill_abs_qty")),
                "fill_abs_notional": _safe_float(fill.get("fill_abs_notional")),
                "fill_vwap": fill_vwap,
                "primary_fill_side": primary_side,
                "first_fill_time": fill.get("first_fill_time", ""),
                "last_fill_time": fill.get("last_fill_time", ""),
                "fill_vwap_vs_open_bps": _signed_adverse_bps(primary_side, fill_vwap, open_price),
                "fill_vwap_vs_vwap_adverse_bps": fill_vs_vwap_adverse,
                "fill_vwap_vs_close_adverse_bps": fill_vs_close_adverse,
                "fill_position_in_range_pct": fill_position,
                "price_path_hints": ";".join(hints),
            }
        )

    missing_rows = [row for row in rows if row.get("status") == "missing_bars"]
    filled_rows = [row for row in rows if _safe_int(row.get("fill_count")) > 0]
    filled_missing = [row for row in filled_rows if row.get("status") == "missing_bars"]
    status_counts = Counter(str(row.get("status") or "") for row in rows)
    if not any_raw_exists:
        status = "historical_limited"
    elif total_errors or filled_missing:
        status = "attention"
    elif missing_rows:
        status = "partial"
    else:
        status = "pass"
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "raw_artifact_exists": bool(any_raw_exists),
        "sources": source_summaries,
        "symbol_count": len(rows),
        "bar_symbol_count": len(all_bar_symbols),
        "status_counts": dict(sorted(status_counts.items())),
        "missing_bar_symbol_count": len(missing_rows),
        "missing_bar_symbols": [row.get("symbol") for row in missing_rows][:100],
        "filled_symbol_count": len(filled_rows),
        "filled_symbols_missing_bars": [row.get("symbol") for row in filled_missing][:100],
        "filled_symbols_missing_bars_count": len(filled_missing),
        "error_count": len(total_errors),
        "errors": total_errors[:20],
        "max_intraday_range_bps": max((_safe_float(row.get("range_bps")) for row in rows), default=0.0),
        "worst_intraday_ranges": sorted(
            rows,
            key=lambda row: _safe_float(row.get("range_bps")),
            reverse=True,
        )[:25],
        "worst_fill_vs_vwap_adverse": sorted(
            [row for row in filled_rows if _safe_float(row.get("fill_vwap_vs_vwap_adverse_bps")) > 0],
            key=lambda row: _safe_float(row.get("fill_vwap_vs_vwap_adverse_bps")),
            reverse=True,
        )[:25],
        "worst_fill_vs_close_adverse": sorted(
            [row for row in filled_rows if _safe_float(row.get("fill_vwap_vs_close_adverse_bps")) > 0],
            key=lambda row: _safe_float(row.get("fill_vwap_vs_close_adverse_bps")),
            reverse=True,
        )[:25],
        "reference_outside_range_rows": [
            row for row in rows if "reference_outside_intraday_range" in str(row.get("price_path_hints") or "")
        ][:25],
        "note": (
            "Aggregates raw 1-minute bars for relevant symbols. The raw JSON keeps complete bars; this summary links "
            "reference prices and fills to intraday open/high/low/close/VWAP for execution-timing attribution."
        ),
    }
    return rows, summary_payload


def _build_quote_evidence(
    *,
    run_dir: Path,
    market_price_evidence_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_path = run_dir / "execution_latest_quotes_snapshot.json"
    after_path = run_dir / "execution_latest_quotes_snapshot_after.json"
    source_defs = [
        ("before_submit", before_path, _read_json(before_path, {})),
        ("after_execution", after_path, _read_json(after_path, {})),
    ]
    quotes_by_source: dict[str, dict[str, dict[str, Any]]] = {}
    source_summaries: dict[str, dict[str, Any]] = {}
    requested_symbols: set[str] = set()
    all_quote_symbols: set[str] = set()
    all_errors: list[dict[str, Any]] = []
    for source, path, raw in source_defs:
        quotes = _latest_quote_payload(raw)
        payload = _snapshot_payload(raw)
        requested = payload.get("requested_symbols") if isinstance(payload.get("requested_symbols"), list) else []
        requested_set = {str(symbol or "").upper().strip() for symbol in requested if str(symbol or "").strip()}
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        requested_symbols.update(requested_set)
        all_quote_symbols.update(quotes)
        all_errors.extend({"source": source, **error} for error in errors if isinstance(error, dict))
        quotes_by_source[source] = quotes
        source_summaries[source] = {
            "exists": path.exists(),
            "path": path.as_posix() if path.exists() else None,
            "ok": bool(raw.get("ok")) if isinstance(raw, dict) and "ok" in raw else None,
            "requested_symbol_count": len(requested_set),
            "quote_symbol_count": len(quotes),
            "missing_quote_symbol_count": len(requested_set - set(quotes)),
            "error_count": len(errors),
            "collected_at_utc": payload.get("collected_at_utc", "") or (raw.get("collected_at_utc", "") if isinstance(raw, dict) else ""),
            "feed": payload.get("feed", ""),
        }
    market_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in market_price_evidence_rows
        if str(row.get("symbol") or "").strip()
    }
    fills = _fill_by_symbol(fill_rows)
    symbols = sorted(set(market_by_symbol) | set(fills) | all_quote_symbols | requested_symbols)
    any_raw_exists = before_path.exists() or after_path.exists()
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        before_quote = quotes_by_source.get("before_submit", {}).get(symbol, {})
        after_quote = quotes_by_source.get("after_execution", {}).get(symbol, {})
        quote = after_quote or before_quote
        source_used = "after_execution" if after_quote else "before_submit" if before_quote else ""
        market = market_by_symbol.get(symbol, {})
        fill = fills.get(symbol, {})
        before_bid = _safe_float(before_quote.get("bp") or before_quote.get("bid_price"))
        before_ask = _safe_float(before_quote.get("ap") or before_quote.get("ask_price"))
        before_mid = (before_bid + before_ask) / 2.0 if before_bid > 0 and before_ask > 0 else 0.0
        before_spread_bps = (before_ask - before_bid) / before_mid * 10000.0 if before_mid > 0 and before_ask >= before_bid else 0.0
        after_bid = _safe_float(after_quote.get("bp") or after_quote.get("bid_price"))
        after_ask = _safe_float(after_quote.get("ap") or after_quote.get("ask_price"))
        after_mid = (after_bid + after_ask) / 2.0 if after_bid > 0 and after_ask > 0 else 0.0
        after_spread_bps = (after_ask - after_bid) / after_mid * 10000.0 if after_mid > 0 and after_ask >= after_bid else 0.0
        bid = _safe_float(quote.get("bp") or quote.get("bid_price"))
        ask = _safe_float(quote.get("ap") or quote.get("ask_price"))
        bid_size = _safe_float(quote.get("bs") or quote.get("bid_size"))
        ask_size = _safe_float(quote.get("as") or quote.get("ask_size"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        spread = ask - bid if ask > 0 and bid > 0 else 0.0
        spread_bps = spread / mid * 10000.0 if mid > 0 and spread >= 0 else 0.0
        reference = _safe_float(market.get("execute_reference_price_used"))
        latest_trade = _safe_float(market.get("execute_latest_trade_price"))
        fill_vwap = _safe_float(fill.get("fill_vwap"))
        primary_side = str(fill.get("primary_fill_side") or "")
        if not any_raw_exists:
            status = "historical_limited"
        elif not quote:
            status = "missing_quote"
        elif bid <= 0 or ask <= 0 or ask < bid:
            status = "invalid_quote"
        elif spread_bps > 100.0:
            status = "wide_spread"
        else:
            status = "pass"
        hints: list[str] = []
        if status == "wide_spread":
            hints.append("wide_spread")
        if fill_vwap > 0 and mid > 0:
            adverse_mid_bps = _signed_adverse_bps(primary_side, fill_vwap, mid)
            if adverse_mid_bps > 25.0:
                hints.append("fill_worse_than_quote_mid")
        else:
            adverse_mid_bps = 0.0
        rows.append(
            {
                "symbol": symbol,
                "status": status,
                "source_used": source_used,
                "quote_time": quote.get("t", ""),
                "before_quote_time": before_quote.get("t", ""),
                "after_quote_time": after_quote.get("t", ""),
                "before_mid_price": before_mid,
                "after_mid_price": after_mid,
                "before_spread_bps": before_spread_bps,
                "after_spread_bps": after_spread_bps,
                "bid_price": bid,
                "ask_price": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "mid_price": mid,
                "spread": spread,
                "spread_bps": spread_bps,
                "conditions": _json_cell(quote.get("c", "")),
                "tape": quote.get("z", ""),
                "execute_reference_price": reference,
                "reference_vs_mid_bps": _bps_change(reference, mid),
                "latest_trade_price": latest_trade,
                "latest_trade_vs_mid_bps": _bps_change(latest_trade, mid),
                "fill_count": _safe_int(fill.get("fill_count")),
                "fill_vwap": fill_vwap,
                "primary_fill_side": primary_side,
                "fill_vwap_vs_mid_adverse_bps": adverse_mid_bps,
                "price_microstructure_hints": ";".join(hints),
            }
        )

    missing_rows = [row for row in rows if row.get("status") == "missing_quote"]
    invalid_rows = [row for row in rows if row.get("status") == "invalid_quote"]
    wide_rows = [row for row in rows if row.get("status") == "wide_spread"]
    status_counts = Counter(str(row.get("status") or "") for row in rows)
    if not any_raw_exists:
        status = "historical_limited"
    elif all_errors or invalid_rows:
        status = "attention"
    elif missing_rows or wide_rows:
        status = "partial"
    else:
        status = "pass"
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "raw_artifact_exists": bool(any_raw_exists),
        "sources": source_summaries,
        "symbol_count": len(rows),
        "quote_symbol_count": len(all_quote_symbols),
        "status_counts": dict(sorted(status_counts.items())),
        "missing_quote_symbol_count": len(missing_rows),
        "invalid_quote_symbol_count": len(invalid_rows),
        "wide_spread_symbol_count": len(wide_rows),
        "max_spread_bps": max((_safe_float(row.get("spread_bps")) for row in rows), default=0.0),
        "error_count": len(all_errors),
        "errors": all_errors[:20],
        "widest_spreads": sorted(rows, key=lambda row: _safe_float(row.get("spread_bps")), reverse=True)[:25],
        "worst_fill_vs_mid_adverse": sorted(
            [row for row in rows if _safe_float(row.get("fill_vwap_vs_mid_adverse_bps")) > 0],
            key=lambda row: _safe_float(row.get("fill_vwap_vs_mid_adverse_bps")),
            reverse=True,
        )[:25],
        "note": (
            "Latest bid/ask quotes provide spread and quote-mid context for execution reference and fill prices. "
            "Historical runs before this capture are marked historical_limited."
        ),
    }
    return rows, summary_payload


def _corporate_action_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    actions = payload.get("actions") if isinstance(payload, dict) else None
    if isinstance(actions, list):
        return [dict(item) for item in actions if isinstance(item, dict)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return []


def _corporate_action_symbol(action: dict[str, Any]) -> str:
    for key in [
        "symbol",
        "new_symbol",
        "old_symbol",
        "target_symbol",
        "source_symbol",
        "payable_symbol",
        "cash_symbol",
    ]:
        value = str(action.get(key) or "").upper().strip()
        if value:
            return value
    return ""


def _corporate_action_relevant_dates(action: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in [
        "ex_date",
        "record_date",
        "payable_date",
        "payable_on",
        "process_date",
        "effective_date",
        "expiration_date",
        "declaration_date",
        "date",
    ]:
        value = str(action.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _date_in_session_window(value: str, session_date: str, *, before_days: int = 10, after_days: int = 3) -> bool:
    try:
        candidate = datetime.fromisoformat(str(value)[:10]).date()
        session = datetime.fromisoformat(str(session_date)[:10]).date()
    except Exception:
        return False
    return (session - timedelta(days=before_days)) <= candidate <= (session + timedelta(days=after_days))


def _build_corporate_action_trace(
    *,
    run_dir: Path,
    session_date: str,
    reconciliation_rows: list[dict[str, Any]],
    market_price_evidence_rows: list[dict[str, Any]],
    account_activity_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = run_dir / "broker_corporate_actions.json"
    raw = _read_json(path, {})
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw if isinstance(raw, dict) else {}
    actions = _corporate_action_items(raw)
    residual_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in reconciliation_rows
        if str(row.get("material_unexplained_qty")).lower() == "true" and str(row.get("symbol") or "").strip()
    }
    price_by_symbol = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in market_price_evidence_rows
        if str(row.get("symbol") or "").strip()
    }
    activity_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in account_activity_rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        activity_class = str(row.get("activity_class") or "")
        if symbol and activity_class in {"corporate_action", "dividend"}:
            activity_by_symbol[symbol].append(row)

    rows: list[dict[str, Any]] = []
    for idx, action in enumerate(actions, start=1):
        symbol = _corporate_action_symbol(action)
        dates = _corporate_action_relevant_dates(action)
        in_session_window = any(_date_in_session_window(value, session_date) for value in dates.values())
        residual = residual_by_symbol.get(symbol, {})
        price_row = price_by_symbol.get(symbol, {})
        related_activity = activity_by_symbol.get(symbol, [])
        amount = _safe_float(
            action.get("cash")
            or action.get("cash_amount")
            or action.get("rate")
            or action.get("payment")
            or action.get("price")
        )
        old_rate = _safe_float(action.get("old_rate") or action.get("from_factor") or action.get("old_qty"))
        new_rate = _safe_float(action.get("new_rate") or action.get("to_factor") or action.get("new_qty"))
        ratio = new_rate / old_rate if old_rate else 0.0
        action_type = str(action.get("action_type") or action.get("type") or "").strip()
        causal_hint = []
        if residual:
            causal_hint.append("position_residual_symbol")
        if related_activity:
            causal_hint.append("matching_account_activity_symbol")
        if abs(_safe_float(price_row.get("decision_execute_reference_change_bps"))) > 200.0:
            causal_hint.append("large_decision_execute_price_move")
        if in_session_window:
            causal_hint.append("date_in_session_window")
        rows.append(
            {
                "row_index": idx,
                "symbol": symbol,
                "action_type": action_type,
                "status": "matched_to_residual_or_activity" if causal_hint else "reference_only",
                "in_session_window": bool(in_session_window),
                "date_fields": _json_cell(dates),
                "ex_date": action.get("ex_date", ""),
                "record_date": action.get("record_date", ""),
                "payable_date": action.get("payable_date") or action.get("payable_on") or "",
                "process_date": action.get("process_date") or action.get("effective_date") or "",
                "cash_amount": amount,
                "old_rate": old_rate,
                "new_rate": new_rate,
                "ratio": ratio,
                "position_residual_flag": bool(residual),
                "position_residual_qty": _safe_float(residual.get("unexplained_qty")),
                "position_residual_notional": _safe_float(residual.get("unexplained_notional_at_snapshot_price")),
                "price_evidence_status": price_row.get("status", ""),
                "decision_execute_reference_change_bps": _safe_float(
                    price_row.get("decision_execute_reference_change_bps")
                ),
                "matching_account_activity_rows": len(related_activity),
                "matching_account_activity_net_amount": sum(_safe_float(item.get("net_amount")) for item in related_activity),
                "causal_hint": ";".join(causal_hint),
                "raw_action": _json_cell(action),
            }
        )

    action_symbol_set = {str(row.get("symbol") or "").upper().strip() for row in rows if str(row.get("symbol") or "").strip()}
    residual_symbols = set(residual_by_symbol)
    activity_symbols = set(activity_by_symbol)
    action_errors = payload.get("errors", []) if isinstance(payload, dict) and isinstance(payload.get("errors"), list) else []
    missing_residual_symbols = sorted(residual_symbols - action_symbol_set)
    missing_activity_symbols = sorted(activity_symbols - action_symbol_set)
    status = (
        "attention"
        if action_errors
        else "matched"
        if any(row.get("position_residual_flag") or _safe_int(row.get("matching_account_activity_rows")) > 0 for row in rows)
        else "pass"
        if path.exists()
        else "historical_limited"
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "raw_artifact_exists": path.exists(),
        "raw_artifact": path.as_posix() if path.exists() else None,
        "raw_ok": bool(payload.get("ok")) if isinstance(payload, dict) and "ok" in payload else None,
        "session_date": session_date,
        "window_start": payload.get("window_start") if isinstance(payload, dict) else None,
        "window_end": payload.get("window_end") if isinstance(payload, dict) else None,
        "requested_symbol_count": _safe_int(payload.get("requested_symbol_count")) if isinstance(payload, dict) else 0,
        "action_count": len(rows),
        "action_symbol_count": len(action_symbol_set),
        "action_type_counts": dict(sorted(Counter(str(row.get("action_type") or "__missing__") for row in rows).items())),
        "matched_position_residual_symbol_count": len(action_symbol_set & residual_symbols),
        "matched_account_activity_symbol_count": len(action_symbol_set & activity_symbols),
        "residual_symbols_without_corporate_action": missing_residual_symbols[:100],
        "residual_symbols_without_corporate_action_count": len(missing_residual_symbols),
        "account_activity_symbols_without_corporate_action": missing_activity_symbols[:100],
        "account_activity_symbols_without_corporate_action_count": len(missing_activity_symbols),
        "error_count": len(action_errors),
        "errors": action_errors[:20],
        "top_matches": sorted(
            [row for row in rows if row.get("position_residual_flag") or _safe_int(row.get("matching_account_activity_rows")) > 0],
            key=lambda row: abs(_safe_float(row.get("position_residual_notional")))
            + abs(_safe_float(row.get("matching_account_activity_net_amount"))),
            reverse=True,
        )[:25],
        "note": (
            "Corporate-action evidence is informational unless it matches residual symbols or dividend/corporate account activities. "
            "Historical runs before this artifact are marked historical_limited."
        ),
    }
    return rows, summary_payload


def _portfolio_history_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {})
    return dict(raw) if isinstance(raw, dict) else {}


def _portfolio_history_rows_from_payload(source: str, raw: Any) -> list[dict[str, Any]]:
    payload = _portfolio_history_payload(raw)
    timestamps = payload.get("timestamp") if isinstance(payload.get("timestamp"), list) else []
    equity = payload.get("equity") if isinstance(payload.get("equity"), list) else []
    profit_loss = payload.get("profit_loss") if isinstance(payload.get("profit_loss"), list) else []
    profit_loss_pct = payload.get("profit_loss_pct") if isinstance(payload.get("profit_loss_pct"), list) else []
    base_value = _safe_float(payload.get("base_value"))
    rows: list[dict[str, Any]] = []
    max_len = max(len(timestamps), len(equity), len(profit_loss), len(profit_loss_pct))
    for idx in range(max_len):
        ts_raw = timestamps[idx] if idx < len(timestamps) else ""
        try:
            timestamp_utc = datetime.fromtimestamp(float(ts_raw), timezone.utc).isoformat(timespec="seconds")
        except Exception:
            timestamp_utc = str(ts_raw or "")
        eq = _safe_float(equity[idx] if idx < len(equity) else None)
        pl = _safe_float(profit_loss[idx] if idx < len(profit_loss) else None)
        pl_pct = _safe_float(profit_loss_pct[idx] if idx < len(profit_loss_pct) else None)
        rows.append(
            {
                "source": source,
                "row_index": idx + 1,
                "timestamp_raw": ts_raw,
                "timestamp_utc": timestamp_utc,
                "equity": eq,
                "profit_loss": pl,
                "profit_loss_pct": pl_pct,
                "base_value": base_value,
                "equity_minus_base_value": eq - base_value if base_value else "",
            }
        )
    return rows


def _build_portfolio_history_trace(
    *,
    run_dir: Path,
    summary: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before_path = run_dir / "broker_portfolio_history_before.json"
    after_path = run_dir / "broker_portfolio_history_after.json"
    before_raw = _read_json(before_path, {})
    after_raw = _read_json(after_path, {})
    rows = _portfolio_history_rows_from_payload("before", before_raw) + _portfolio_history_rows_from_payload(
        "after",
        after_raw,
    )
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_source[str(row.get("source") or "")].append(row)

    def source_summary(source: str, path: Path, raw: Any) -> dict[str, Any]:
        source_rows = by_source.get(source, [])
        equities = [_safe_float(row.get("equity")) for row in source_rows if not _is_missing(row.get("equity"))]
        payload = _portfolio_history_payload(raw)
        ok = bool(raw.get("ok")) if isinstance(raw, dict) and "ok" in raw else None
        return {
            "exists": path.exists(),
            "ok": ok,
            "row_count": len(source_rows),
            "base_value": _safe_float(payload.get("base_value")),
            "first_timestamp_utc": source_rows[0].get("timestamp_utc") if source_rows else "",
            "last_timestamp_utc": source_rows[-1].get("timestamp_utc") if source_rows else "",
            "first_equity": equities[0] if equities else 0.0,
            "last_equity": equities[-1] if equities else 0.0,
            "min_equity": min(equities) if equities else 0.0,
            "max_equity": max(equities) if equities else 0.0,
            "equity_change": (equities[-1] - equities[0]) if len(equities) >= 2 else 0.0,
            "error": raw.get("error") if isinstance(raw, dict) else "",
        }

    before_summary = source_summary("before", before_path, before_raw)
    after_summary = source_summary("after", after_path, after_raw)
    summary_equity_before = _safe_float(summary.get("account_equity"))
    summary_equity_after = _safe_float(summary.get("account_equity_post_trade"))
    history_last_equity = _safe_float(after_summary.get("last_equity") or before_summary.get("last_equity"))
    summary_vs_history_after_delta = summary_equity_after - history_last_equity if history_last_equity else 0.0
    missing_future = not before_path.exists() and not after_path.exists()
    status = (
        "historical_limited"
        if missing_future
        else "attention"
        if (before_summary.get("ok") is False or after_summary.get("ok") is False)
        else "attention"
        if after_summary.get("row_count") == 0 and before_summary.get("row_count") == 0
        else "pass"
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "before": before_summary,
        "after": after_summary,
        "row_count": len(rows),
        "raw_artifacts": {
            "before": before_path.as_posix() if before_path.exists() else None,
            "after": after_path.as_posix() if after_path.exists() else None,
        },
        "summary_equity_before": summary_equity_before,
        "summary_equity_after": summary_equity_after,
        "history_last_equity": history_last_equity,
        "summary_vs_history_after_delta": summary_vs_history_after_delta,
        "largest_equity_drawdown_from_history": (
            max(_safe_float(row.get("equity")) for row in rows) - min(_safe_float(row.get("equity")) for row in rows)
            if rows
            else 0.0
        ),
        "note": (
            "Broker portfolio history is an official account-level time series used to locate when equity/PnL moved. "
            "Historical runs before this capture are marked historical_limited."
        ),
    }
    return rows, summary_payload


def _parse_calendar_date(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _normalize_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        if len(text) >= 8 and text[:8].isdigit():
            return datetime.strptime(text[:8], "%Y%m%d").date().isoformat()
    except Exception:
        return text
    return text


def _parse_clock_minutes(value: Any) -> int | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    if "T" in text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.hour * 60 + parsed.minute
        except Exception:
            return None
    pieces = text.split(":")
    if len(pieces) < 2:
        return None
    try:
        return int(pieces[0]) * 60 + int(pieces[1])
    except Exception:
        return None


def _calendar_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
        return dict(raw.get("payload") or {})
    if isinstance(raw, dict) and isinstance(raw.get("payload"), list):
        return {"rows": raw.get("payload")}
    return dict(raw) if isinstance(raw, dict) else {}


def _calendar_rows(raw: Any) -> list[dict[str, Any]]:
    payload = _calendar_payload(raw)
    for key in ("rows", "calendar", "sessions"):
        items = payload.get(key)
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, dict)]
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _build_calendar_trace(
    *,
    run_dir: Path,
    session_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = run_dir / "broker_calendar_window.json"
    raw = _read_json(path, {})
    payload = _calendar_payload(raw)
    source_rows = _calendar_rows(raw)
    session_dt = _parse_calendar_date(session_date)

    dated_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in source_rows:
        parsed = _parse_calendar_date(row.get("date"))
        if parsed is not None:
            dated_rows.append((parsed, row))
    dated_rows = sorted(dated_rows, key=lambda item: item[0])
    calendar_dates = [dt.date().isoformat() for dt, _row in dated_rows]
    session_key = session_dt.date().isoformat() if session_dt else str(session_date or "")
    session_row = next((row for dt, row in dated_rows if dt.date().isoformat() == session_key), None)
    previous_dates = [dt for dt, _row in dated_rows if session_dt is not None and dt.date() < session_dt.date()]
    next_dates = [dt for dt, _row in dated_rows if session_dt is not None and dt.date() > session_dt.date()]
    expected_previous = previous_dates[-1].date().isoformat() if previous_dates else ""
    expected_next = next_dates[0].date().isoformat() if next_dates else ""

    rows: list[dict[str, Any]] = []
    for idx, (row_dt, row) in enumerate(dated_rows, start=1):
        open_text = str(row.get("open") or "")
        close_text = str(row.get("close") or "")
        open_minutes = _parse_clock_minutes(open_text)
        close_minutes = _parse_clock_minutes(close_text)
        duration_minutes = (
            close_minutes - open_minutes
            if open_minutes is not None and close_minutes is not None and close_minutes >= open_minutes
            else None
        )
        is_session = row_dt.date().isoformat() == session_key
        rows.append(
            {
                "row_index": idx,
                "date": row_dt.date().isoformat(),
                "open": open_text,
                "close": close_text,
                "session_open": row.get("session_open", ""),
                "session_close": row.get("session_close", ""),
                "is_session_date": bool(is_session),
                "is_observed_execute_dir": bool(is_session),
                "is_half_day": bool(close_minutes is not None and close_minutes < 16 * 60),
                "regular_session_minutes": duration_minutes if duration_minutes is not None else "",
                "days_from_session": (row_dt.date() - session_dt.date()).days if session_dt is not None else "",
                "expected_previous_trading_date": expected_previous,
                "expected_next_trading_date": expected_next,
                "raw_row": _json_cell(row),
            }
        )

    raw_ok = bool(raw.get("ok")) if isinstance(raw, dict) and "ok" in raw else None
    any_raw_exists = path.exists()
    if not any_raw_exists:
        status = "historical_limited"
    elif raw_ok is False or not source_rows:
        status = "attention"
    elif session_row is None:
        status = "attention"
    else:
        status = "pass"

    session_open_minutes = _parse_clock_minutes(session_row.get("open")) if isinstance(session_row, dict) else None
    session_close_minutes = _parse_clock_minutes(session_row.get("close")) if isinstance(session_row, dict) else None
    session_duration_minutes = (
        session_close_minutes - session_open_minutes
        if session_open_minutes is not None
        and session_close_minutes is not None
        and session_close_minutes >= session_open_minutes
        else None
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "raw_artifact_exists": bool(any_raw_exists),
        "ok": raw_ok,
        "error": raw.get("error") if isinstance(raw, dict) else "",
        "error_type": raw.get("error_type") if isinstance(raw, dict) else "",
        "error_count": 1 if raw_ok is False else 0,
        "session_date": session_key,
        "window_start": payload.get("window_start") or (calendar_dates[0] if calendar_dates else ""),
        "window_end": payload.get("window_end") or (calendar_dates[-1] if calendar_dates else ""),
        "row_count": len(rows),
        "calendar_dates": calendar_dates,
        "session_date_in_calendar": bool(session_row is not None),
        "session_row": session_row or {},
        "session_open": session_row.get("open", "") if isinstance(session_row, dict) else "",
        "session_close": session_row.get("close", "") if isinstance(session_row, dict) else "",
        "session_regular_minutes": session_duration_minutes if session_duration_minutes is not None else "",
        "session_is_half_day": bool(session_close_minutes is not None and session_close_minutes < 16 * 60),
        "expected_previous_trading_date": expected_previous,
        "expected_next_trading_date": expected_next,
        "first_calendar_date": calendar_dates[0] if calendar_dates else "",
        "last_calendar_date": calendar_dates[-1] if calendar_dates else "",
        "raw_artifact": path.as_posix() if any_raw_exists else None,
        "note": (
            "Alpaca calendar evidence proves official trading sessions, holidays, and half days around the run. "
            "Historical runs before this artifact are marked historical_limited."
        ),
    }
    return rows, summary_payload


def _account_activity_class(row: dict[str, Any]) -> tuple[str, str, bool, str]:
    activity_type = str(row.get("activity_type") or row.get("type") or "").upper().strip()
    source = str(row.get("source") or "").lower()
    if activity_type == "FILL" or "fill_activities" in source:
        return (
            "trade_fill_cashflow",
            "trade_cashflow_not_strategy_pnl",
            False,
            "FILL cash flow changes cash and positions but should not be used by itself to explain equity PnL.",
        )
    if activity_type in {"DIV", "DIVCGL", "DIVCGS", "DIVFEE", "DIVFT", "DIVNRA", "DIVROC", "DIVTXEX"}:
        return ("dividend", "strategy_account_pnl", True, "Dividend or dividend-like broker activity.")
    if "FEE" in activity_type or activity_type in {"PTC", "PTR"}:
        return ("fee", "strategy_account_cost", True, "Broker fee or pass-through cost.")
    if activity_type in {"INT", "INTEREST"} or "INT" in activity_type:
        return ("interest", "strategy_account_pnl", True, "Interest or margin-interest broker activity.")
    if activity_type in {"TRANS", "ACH", "WIRE", "ACATC", "ACATS"}:
        return ("external_transfer", "non_strategy_cashflow", True, "External cash or position transfer.")
    if activity_type in {"JNLC", "JNLS", "JNL", "CSD", "CSW"}:
        return ("journal_or_cash_movement", "non_strategy_cashflow", True, "Broker journal/cash movement.")
    if activity_type in {"MA", "NC", "SC", "SSO", "SSP", "REORG", "SPIN", "CIL"}:
        return ("corporate_action", "strategy_or_broker_adjustment", True, "Corporate action or broker adjustment.")
    if not activity_type:
        return ("missing_activity_type", "unknown", False, "Activity type is missing.")
    return ("unknown_non_trade_activity", "unknown", False, "Unclassified account activity type; inspect raw broker payload.")


def _build_account_activity_attribution(
    *,
    broker_activity_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(broker_activity_rows, start=1):
        activity_class, pnl_bucket, equity_impact_used, note = _account_activity_class(row)
        net_amount = _safe_float(row.get("net_amount"))
        gross_amount = _safe_float(row.get("gross_amount"))
        rows.append(
            {
                "row_index": idx,
                "source": row.get("source", ""),
                "matched_scope": row.get("matched_scope", ""),
                "activity_id": row.get("activity_id", ""),
                "activity_type": row.get("activity_type", ""),
                "type": row.get("type", ""),
                "activity_class": activity_class,
                "strategy_pnl_bucket": pnl_bucket,
                "known_equity_impact_used_in_bridge": bool(equity_impact_used),
                "transaction_time": row.get("transaction_time", ""),
                "symbol": row.get("symbol", ""),
                "side": row.get("side", ""),
                "qty": _safe_float(row.get("qty")),
                "price": _safe_float(row.get("price")),
                "order_id": row.get("order_id", ""),
                "in_execution_records": bool(row.get("in_execution_records")),
                "net_amount": net_amount,
                "gross_amount": gross_amount,
                "expected_equity_impact_amount": net_amount if equity_impact_used else 0.0,
                "note": note,
            }
        )
    by_class = Counter(str(row.get("activity_class") or "") for row in rows)
    by_bucket = Counter(str(row.get("strategy_pnl_bucket") or "") for row in rows)
    net_by_class: dict[str, float] = defaultdict(float)
    gross_by_class: dict[str, float] = defaultdict(float)
    net_by_bucket: dict[str, float] = defaultdict(float)
    for row in rows:
        activity_class = str(row.get("activity_class") or "")
        bucket = str(row.get("strategy_pnl_bucket") or "")
        net_by_class[activity_class] += _safe_float(row.get("net_amount"))
        gross_by_class[activity_class] += _safe_float(row.get("gross_amount"))
        net_by_bucket[bucket] += _safe_float(row.get("net_amount"))
    known_impact = sum(_safe_float(row.get("expected_equity_impact_amount")) for row in rows)
    unknown_net = sum(
        _safe_float(row.get("net_amount"))
        for row in rows
        if str(row.get("strategy_pnl_bucket") or "") == "unknown"
    )
    trade_cashflow = _safe_float(net_by_class.get("trade_fill_cashflow"))
    non_strategy_cashflow = sum(
        _safe_float(net_by_class.get(key))
        for key in ("external_transfer", "journal_or_cash_movement")
    )
    fee_interest_dividend = sum(_safe_float(net_by_class.get(key)) for key in ("fee", "interest", "dividend"))
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "attention" if abs(unknown_net) > 1e-6 else "pass",
        "row_count": len(rows),
        "activity_class_counts": dict(sorted(by_class.items())),
        "strategy_pnl_bucket_counts": dict(sorted(by_bucket.items())),
        "net_amount_by_activity_class": dict(sorted(net_by_class.items())),
        "gross_amount_by_activity_class": dict(sorted(gross_by_class.items())),
        "net_amount_by_strategy_pnl_bucket": dict(sorted(net_by_bucket.items())),
        "known_non_trade_equity_impact_net_amount": known_impact,
        "trade_fill_cashflow_net_amount": trade_cashflow,
        "non_strategy_cashflow_net_amount": non_strategy_cashflow,
        "fee_interest_dividend_net_amount": fee_interest_dividend,
        "unknown_activity_net_amount": unknown_net,
        "rows_with_unknown_activity_class": [
            row for row in rows if str(row.get("strategy_pnl_bucket") or "") == "unknown"
        ][:50],
        "note": (
            "Classifies broker account activities so fills are separated from non-trade cash/equity impacts. "
            "Only known non-trade equity impacts are used by the equity bridge."
        ),
    }
    return rows, summary_payload


def _build_strict_attribution_checklist(
    *,
    context: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    artifacts = context.get("artifacts", {}) if isinstance(context.get("artifacts"), dict) else {}
    counts = context.get("artifact_counts", {}) if isinstance(context.get("artifact_counts"), dict) else {}

    def present(key: str) -> bool:
        return _path_exists(artifacts.get(key))

    future_capture_expected = any(
        present(key)
        for key in [
            "run_context",
            "alpaca_api_audit",
            "source_code_manifest",
            "run_evidence_digest",
            "broker_account_after",
            "broker_positions_after_raw",
            "broker_position_account_stability_after",
            "broker_position_account_stability_before",
            "execution_price_snapshot",
            "scheduler_task_result",
        ]
    )
    rows: list[dict[str, Any]] = []

    def add_item(
        area: str,
        item: str,
        status: str,
        *,
        severity: str,
        blocking: bool,
        present_value: Any,
        expected: Any,
        observed: Any,
        evidence_artifacts: str,
        detail: str,
        next_action: str,
    ) -> None:
        rows.append(
            {
                "area": area,
                "item": item,
                "status": status,
                "severity": severity,
                "blocking_strict_attribution": bool(blocking),
                "present": present_value,
                "expected": expected,
                "observed": observed,
                "evidence_artifacts": evidence_artifacts,
                "detail": detail,
                "next_action": next_action,
            }
        )

    future_status_missing = "missing" if future_capture_expected else "historical_gap"
    future_severity_missing = "warning" if future_capture_expected else "info"
    for area, keys, detail in [
        (
            "broker_state",
            [
                "broker_account_before",
                "broker_account_after",
                "broker_positions_before_raw",
                "broker_positions_after_raw",
                "broker_position_account_stability_before",
                "broker_position_account_stability_after",
            ],
            "Raw before/after broker account and position snapshots are required for strict replay.",
        ),
        (
            "execution_microstructure",
            ["broker_fill_activities", "broker_order_snapshots", "broker_orders_all_after", "order_poll_timeline"],
            "Order, fill, broker-order, and poll-timeline evidence are required for execution replay.",
        ),
        (
            "market_price",
            [
                "execution_price_snapshot",
                "execution_latest_trades_snapshot",
                "execution_latest_quotes_snapshot",
                "execution_latest_quotes_snapshot_after",
                "execution_intraday_bars_1min",
            ],
            "Reference-price, trade, quote, and intraday-bar snapshots are required to explain order sizing and execution drift.",
        ),
        (
            "portfolio_history",
            ["broker_portfolio_history_before", "broker_portfolio_history_after"],
            "Broker portfolio-history time series is required to locate account-level equity/PnL movement during the run.",
        ),
        (
            "market_calendar",
            ["broker_calendar_window"],
            "Official broker calendar is required to prove trading-day continuity, holidays, and half-day sessions.",
        ),
        (
            "corporate_actions",
            ["broker_corporate_actions"],
            "Corporate-action capture is required to explain splits, dividends, symbol changes, and broker adjustments.",
        ),
        (
            "source_and_scheduler",
            [
                "run_context",
                "run_evidence_digest",
                "source_code_manifest",
                "source_git_snapshot",
                "python_environment",
                "scheduler_task_context",
                "scheduler_task_result",
            ],
            "Code/runtime/scheduler context is required to reproduce the run exactly.",
        ),
    ]:
        for key in keys:
            is_present = present(key)
            add_item(
                area,
                key,
                "pass" if is_present else future_status_missing,
                severity="info" if is_present else future_severity_missing,
                blocking=bool(future_capture_expected and not is_present),
                present_value=is_present,
                expected=True,
                observed=artifacts.get(key),
                evidence_artifacts=str(artifacts.get(key) or ""),
                detail=detail,
                next_action="Verify the next real execute run writes this artifact." if not is_present else "No action.",
            )

    position_summary = summaries.get("position_snapshot_integrity", {})
    residual_summary = summaries.get("residual_diagnosis", {})
    evidence_summary = summaries.get("evidence_completeness", {})
    price_summary = summaries.get("market_price_evidence", {})
    activity_summary = summaries.get("account_activity_attribution", {})
    corporate_action_summary = summaries.get("corporate_action_trace", {})
    portfolio_history_summary = summaries.get("portfolio_history_trace", {})
    calendar_summary = summaries.get("calendar_trace", {})
    account_state_summary = summaries.get("account_state_bridge", {})
    intraday_bar_summary = summaries.get("intraday_bar_evidence", {})
    quote_summary = summaries.get("quote_evidence", {})
    decision_drift_summary = summaries.get("decision_execute_drift", {})
    run_evidence_digest_summary = summaries.get("run_evidence_digest", {})
    startup_binding_summary = summaries.get("startup_binding", {})
    run_failure_summary = summaries.get("run_failure_diagnosis", {})
    add_item(
        "run_health",
        "run_completed_without_executor_error",
        "pass" if run_failure_summary.get("status") == "pass" else "attention",
        severity="error" if run_failure_summary.get("status") == "fail" else "warning",
        blocking=run_failure_summary.get("status") != "pass",
        present_value=bool(run_failure_summary),
        expected="pass",
        observed={
            "status": run_failure_summary.get("status"),
            "task_status": run_failure_summary.get("task_status"),
            "failure_class": run_failure_summary.get("failure_class"),
            "error_type": run_failure_summary.get("error_type"),
            "error": run_failure_summary.get("error"),
            "missing_core_artifacts": run_failure_summary.get("missing_core_artifacts"),
        },
        evidence_artifacts="76_run_failure_diagnosis.csv; 77_run_failure_diagnosis_summary.json; scheduler_task_result.json; execution_summary.json",
        detail="Executor/scheduler health must be clean before interpreting a session as a valid performance attribution sample.",
        next_action="Fix the failure_class/error_type first, then rerun or wait for the scheduler retry.",
    )
    add_item(
        "source_and_scheduler",
        "run_evidence_digest_complete",
        "pass"
        if _safe_int(run_evidence_digest_summary.get("strict_missing_file_count")) <= 0
        and run_evidence_digest_summary.get("status") != "historical_limited"
        else "historical_gap"
        if not future_capture_expected
        else "attention",
        severity="warning" if future_capture_expected else "info",
        blocking=bool(
            future_capture_expected
            and (
                run_evidence_digest_summary.get("status") == "historical_limited"
                or _safe_int(run_evidence_digest_summary.get("strict_missing_file_count")) > 0
            )
        ),
        present_value=bool(run_evidence_digest_summary.get("digest_exists")),
        expected="digest_exists_and_strict_missing_file_count=0",
        observed={
            "status": run_evidence_digest_summary.get("status"),
            "digest_exists": run_evidence_digest_summary.get("digest_exists"),
            "strict_missing_file_count": run_evidence_digest_summary.get("strict_missing_file_count"),
            "missing_file_count": run_evidence_digest_summary.get("missing_file_count"),
        },
        evidence_artifacts="run_evidence_digest.json; 72_run_evidence_digest_summary.json; 73_run_evidence_digest_checks.csv",
        detail="Semantic run evidence digest should index raw broker, execution, market, API, source, and scheduler evidence.",
        next_action="Inspect strict_missing_files in 72_run_evidence_digest_summary.json and verify the next live execute run writes them.",
    )
    add_item(
        "broker_state",
        "position_snapshot_integrity_pass",
        "pass" if position_summary.get("status") == "pass" else "attention",
        severity="warning",
        blocking=position_summary.get("status") != "pass",
        present_value=bool(position_summary),
        expected="pass",
        observed=position_summary.get("status"),
        evidence_artifacts="37_position_snapshot_integrity.csv/json",
        detail="Before positions plus captured fills should reconcile to after positions.",
        next_action="Inspect raw/stability position snapshots and broker activities for residual symbols.",
    )
    add_item(
        "residuals",
        "residual_diagnosis_clear",
        "pass" if residual_summary.get("status") == "pass" else "attention",
        severity="warning",
        blocking=residual_summary.get("status") != "pass",
        present_value=bool(residual_summary),
        expected="pass",
        observed=residual_summary.get("status"),
        evidence_artifacts="38_residual_diagnosis.csv/json",
        detail="Residual diagnosis must be clear before treating daily attribution as strict.",
        next_action="Resolve position/equity residual buckets before concluding strategy causality.",
    )
    add_item(
        "market_price",
        "reference_prices_available",
        "pass" if _safe_int(price_summary.get("execute_missing_reference_symbol_count")) <= 0 else "attention",
        severity="warning",
        blocking=_safe_int(price_summary.get("execute_missing_reference_symbol_count")) > 0,
        present_value=bool(price_summary),
        expected=0,
        observed=price_summary.get("execute_missing_reference_symbol_count"),
        evidence_artifacts="48_market_price_evidence.csv; 49_market_price_evidence_summary.json",
        detail="Every target/position symbol should have a usable execution reference price.",
        next_action="Inspect missing price symbols and Alpaca latest-trade/fallback coverage.",
    )
    add_item(
        "portfolio_history",
        "portfolio_history_capture_ok",
        "pass"
        if portfolio_history_summary.get("status") in {"pass", "historical_limited"}
        else "attention",
        severity="warning" if portfolio_history_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and portfolio_history_summary.get("status") == "attention"),
        present_value=bool(portfolio_history_summary),
        expected="pass",
        observed=portfolio_history_summary.get("status"),
        evidence_artifacts="56_portfolio_history_trace.csv; 57_portfolio_history_summary.json",
        detail="Broker portfolio-history time series should be captured and parseable for account-level PnL timing.",
        next_action="Inspect broker_portfolio_history_before/after.json and compare to account snapshots.",
    )
    add_item(
        "market_calendar",
        "calendar_capture_ok",
        "pass" if calendar_summary.get("status") in {"pass", "historical_limited"} else "attention",
        severity="warning" if calendar_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and calendar_summary.get("status") == "attention"),
        present_value=bool(calendar_summary),
        expected="pass",
        observed=calendar_summary.get("status"),
        evidence_artifacts="62_calendar_trace.csv; 63_calendar_summary.json",
        detail="Official Alpaca calendar should be captured and include the execute session date.",
        next_action="Inspect broker_calendar_window.json for API errors or missing session-date rows.",
    )
    add_item(
        "broker_state",
        "account_state_bridge_ok",
        "pass" if account_state_summary.get("status") in {"pass", "historical_limited"} else "attention",
        severity="warning" if account_state_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and account_state_summary.get("status") == "attention"),
        present_value=bool(account_state_summary),
        expected="pass",
        observed=account_state_summary.get("status"),
        evidence_artifacts="64_account_state_bridge.csv; 65_account_state_bridge_summary.json",
        detail="Account-state bridge should reconcile raw account field deltas with summary and equity bridge values.",
        next_action="Inspect before/after account snapshots, cash/exposure deltas, and equity bridge residuals.",
    )
    add_item(
        "market_price",
        "intraday_bar_capture_ok",
        "pass"
        if intraday_bar_summary.get("status") in {"pass", "partial", "historical_limited"}
        else "attention",
        severity="warning" if intraday_bar_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and intraday_bar_summary.get("status") == "attention"),
        present_value=bool(intraday_bar_summary),
        expected="pass_or_partial",
        observed=intraday_bar_summary.get("status"),
        evidence_artifacts="58_intraday_bar_evidence.csv; 59_intraday_bar_summary.json",
        detail="Intraday 1-minute bars should be captured and parseable for relevant target/position/fill symbols.",
        next_action="Inspect execution_intraday_bars_1min*.json for API errors or missing filled symbols.",
    )
    add_item(
        "market_price",
        "quote_capture_ok",
        "pass" if quote_summary.get("status") in {"pass", "partial", "historical_limited"} else "attention",
        severity="warning" if quote_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and quote_summary.get("status") == "attention"),
        present_value=bool(quote_summary),
        expected="pass_or_partial",
        observed=quote_summary.get("status"),
        evidence_artifacts="60_quote_evidence.csv; 61_quote_summary.json",
        detail="Latest bid/ask quotes should be captured and parseable for relevant target/position/fill symbols.",
        next_action="Inspect execution_latest_quotes_snapshot.json for API errors, missing quotes, or invalid spreads.",
    )
    add_item(
        "account_activity",
        "account_activity_known_or_empty",
        "pass" if abs(_safe_float(activity_summary.get("unknown_activity_net_amount"))) <= 1e-6 else "attention",
        severity="warning",
        blocking=abs(_safe_float(activity_summary.get("unknown_activity_net_amount"))) > 1e-6,
        present_value=bool(activity_summary),
        expected=0.0,
        observed=activity_summary.get("unknown_activity_net_amount"),
        evidence_artifacts="50_account_activity_attribution.csv; 51_account_activity_attribution_summary.json",
        detail="Unclassified account activities can hide dividends, fees, transfers, or broker adjustments.",
        next_action="Classify unknown activity types and decide whether they are strategy PnL or external cashflow.",
    )
    add_item(
        "corporate_actions",
        "corporate_action_capture_ok",
        "pass"
        if corporate_action_summary.get("status") in {"pass", "matched", "historical_limited"}
        else "attention",
        severity="warning" if corporate_action_summary.get("status") == "attention" else "info",
        blocking=bool(future_capture_expected and corporate_action_summary.get("status") == "attention"),
        present_value=bool(corporate_action_summary),
        expected="pass_or_matched",
        observed=corporate_action_summary.get("status"),
        evidence_artifacts="54_corporate_action_trace.csv; 55_corporate_action_summary.json",
        detail="Corporate-action evidence should be captured and parseable for relevant target/position symbols.",
        next_action="Inspect broker_corporate_actions.json errors or action rows matching residual/account activity symbols.",
    )
    add_item(
        "decision_execute",
        "decision_execute_drift_recorded",
        "pass" if _safe_int(decision_drift_summary.get("symbol_count")) > 0 else "missing",
        severity="warning",
        blocking=_safe_int(decision_drift_summary.get("symbol_count")) <= 0,
        present_value=bool(decision_drift_summary),
        expected="symbol_count>0",
        observed=decision_drift_summary.get("symbol_count"),
        evidence_artifacts="46_decision_execute_drift.csv; 47_decision_execute_drift_summary.json",
        detail="Decision-time versus execute-time plan drift must be available for attribution.",
        next_action="Ensure both decision and execute order_plan.json files are present.",
    )
    add_item(
        "strict_replay",
        "strict_account_position_replay_ready",
        "pass"
        if evidence_summary.get("strict_account_position_replay_ready")
        else "attention"
        if future_capture_expected
        else "historical_gap",
        severity="warning" if future_capture_expected else "info",
        blocking=bool(future_capture_expected and not evidence_summary.get("strict_account_position_replay_ready")),
        present_value=bool(evidence_summary),
        expected=True,
        observed=evidence_summary.get("strict_account_position_replay_ready"),
        evidence_artifacts="39_evidence_completeness.csv/json",
        detail="Strict account/position replay requires raw before/after state and stable after-run evidence.",
        next_action="Wait for the next upgraded live execute run and verify raw/stability artifacts are present.",
    )
    startup_issue_count = _safe_int(startup_binding_summary.get("issue_count"))
    add_item(
        "operational_startup",
        "startup_binding_observable",
        "pass" if startup_binding_summary.get("status") == "pass" else "attention",
        severity="warning",
        blocking=startup_binding_summary.get("status") not in {"pass", None, ""},
        present_value=bool(startup_binding_summary),
        expected="pass",
        observed={
            "status": startup_binding_summary.get("status"),
            "issue_count": startup_issue_count,
            "autostart_registered": startup_binding_summary.get("autostart_registered"),
            "scheduler_due_latest_exists": startup_binding_summary.get("scheduler_due_latest_exists"),
            "scheduler_runtime_latest_exists": startup_binding_summary.get("scheduler_runtime_latest_exists"),
            "process_health_status": startup_binding_summary.get("process_health_status"),
        },
        evidence_artifacts="74_startup_binding_checks.csv; 75_startup_binding_summary.json; daemon/startup.bat.log; daemon/scheduler_due_latest.json; daemon/scheduler_runtime_latest.json",
        detail="Operational startup evidence should show autostart registration, visible Start.bat path, pid files, due check, and scheduler heartbeat.",
        next_action="Register the Windows logon task, restart with Start.bat, then refresh process health if startup binding is not pass.",
    )

    blocking_rows = [row for row in rows if bool(row.get("blocking_strict_attribution"))]
    status_counts = Counter(str(row.get("status") or "") for row in rows)
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "ready"
        if evidence_summary.get("strict_account_position_replay_ready") and not blocking_rows
        else "blocked"
        if blocking_rows
        else "historical_limited",
        "strict_attribution_ready": bool(evidence_summary.get("strict_account_position_replay_ready")) and not blocking_rows,
        "future_capture_expected": bool(future_capture_expected),
        "row_count": len(rows),
        "blocking_item_count": len(blocking_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "artifact_counts_context": counts,
        "top_blockers": blocking_rows[:25],
        "note": (
            "This checklist is intentionally stricter than core replay. "
            "Historical backfills can be useful while still failing strict attribution readiness."
        ),
    }
    return rows, summary_payload


def _raw_position_payload(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("payload"), list):
        return [dict(item) for item in raw.get("payload", []) if isinstance(item, dict)]
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _build_position_snapshot_integrity(
    *,
    run_dir: Path,
    summary: dict[str, Any],
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    reconciliation_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    outputs = summary.get("outputs", {}) if isinstance(summary.get("outputs"), dict) else {}
    raw_before_path = run_dir / "broker_positions_before_raw.json"
    raw_after_path = run_dir / "broker_positions_after_raw.json"
    stability_before_path = run_dir / "broker_position_account_stability_before.json"
    stability_path = run_dir / "broker_position_account_stability_after.json"
    raw_before = _raw_position_payload(_read_json(raw_before_path, []))
    raw_after = _raw_position_payload(_read_json(raw_after_path, []))
    stability_before = _read_json(stability_before_path, {})
    stability = _read_json(stability_path, {})
    stability_before_samples = stability_before.get("samples", []) if isinstance(stability_before, dict) else []
    stability_samples = stability.get("samples", []) if isinstance(stability, dict) else []
    material_rows = [row for row in reconciliation_rows if str(row.get("material_unexplained_qty")).lower() == "true"]
    disappeared_without_fill = [
        row
        for row in material_rows
        if str(row.get("residual_reason_hint") or "") == "position_changed_without_captured_fill"
    ]
    side_flip_residuals = [
        row
        for row in material_rows
        if row.get("before_side")
        and row.get("after_side")
        and str(row.get("before_side")) != str(row.get("after_side"))
    ]
    missing_after_symbols = sorted(set(before) - set(after))
    fill_symbols = {str(row.get("symbol") or "").upper().strip() for row in fill_rows if str(row.get("symbol") or "").strip()}
    missing_after_without_fill = [symbol for symbol in missing_after_symbols if symbol not in fill_symbols]
    raw_after_expected = bool(outputs.get("broker_positions_after_raw_json"))
    stability_before_expected = bool(outputs.get("broker_position_account_stability_before_json"))
    stability_expected = bool(outputs.get("broker_position_account_stability_after_json"))
    after_contract_ratio = (len(after) / len(before)) if before else 1.0
    snapshot_status = "pass"
    if material_rows:
        snapshot_status = "attention"
    elif before and after_contract_ratio < 0.75 and not raw_after_path.exists():
        snapshot_status = "attention"
    rows = [
        {
            "check": "before_position_csv",
            "status": "pass" if before else "warning",
            "severity": "error" if not before else "info",
            "observed": len(before),
            "expected": ">0",
            "detail": "Broker positions before execution CSV symbol count.",
            "examples": "",
        },
        {
            "check": "after_position_csv",
            "status": "pass" if after else "warning",
            "severity": "error" if not after else "info",
            "observed": len(after),
            "expected": ">0",
            "detail": "Broker positions after execution CSV symbol count.",
            "examples": "",
        },
        {
            "check": "raw_before_positions_available",
            "status": "pass" if raw_before_path.exists() else "not_applicable",
            "severity": "info",
            "observed": len(raw_before) if raw_before_path.exists() else "missing",
            "expected": "present for future strict replay runs",
            "detail": "Raw broker position payload before execution.",
            "examples": raw_before_path.as_posix(),
        },
        {
            "check": "raw_after_positions_available",
            "status": "pass" if raw_after_path.exists() else "warning" if raw_after_expected else "not_applicable",
            "severity": "warning" if raw_after_expected and not raw_after_path.exists() else "info",
            "observed": len(raw_after) if raw_after_path.exists() else "missing",
            "expected": "present for future strict replay runs",
            "detail": "Raw broker position payload after execution.",
            "examples": raw_after_path.as_posix(),
        },
        {
            "check": "before_stability_samples_available",
            "status": "pass"
            if stability_before_samples
            else "warning"
            if stability_before_expected
            else "not_applicable",
            "severity": "warning" if stability_before_expected and not stability_before_samples else "info",
            "observed": len(stability_before_samples) if isinstance(stability_before_samples, list) else 0,
            "expected": ">=1 for future strict replay runs",
            "detail": "Repeated before-run broker positions/account snapshots.",
            "examples": stability_before_path.as_posix(),
        },
        {
            "check": "after_stability_samples_available",
            "status": "pass"
            if stability_samples
            else "warning"
            if stability_expected
            else "not_applicable",
            "severity": "warning" if stability_expected and not stability_samples else "info",
            "observed": len(stability_samples) if isinstance(stability_samples, list) else 0,
            "expected": ">=1 for future strict replay runs",
            "detail": "Repeated after-run broker positions/account snapshots.",
            "examples": stability_path.as_posix(),
        },
        {
            "check": "position_symbols_not_unexplained_by_fills",
            "status": "pass" if not material_rows else "warning",
            "severity": "warning",
            "observed": len(material_rows),
            "expected": 0,
            "detail": "Before position plus captured fills should reconcile to after position.",
            "examples": _json_cell([row.get("symbol") for row in material_rows[:20]]),
        },
        {
            "check": "after_snapshot_symbol_count_vs_before",
            "status": "pass" if after_contract_ratio >= 0.75 or not material_rows else "warning",
            "severity": "warning",
            "observed": {"before": len(before), "after": len(after), "after_to_before_ratio": after_contract_ratio},
            "expected": "large contractions need fills/raw stability evidence",
            "detail": "Detects after snapshots that look like partial or transient broker position lists.",
            "examples": _json_cell(missing_after_without_fill[:20]),
        },
    ]
    reason_counts = Counter(str(row.get("residual_reason_hint") or "__missing__") for row in material_rows)
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": snapshot_status,
        "before_position_symbols": len(before),
        "after_position_symbols": len(after),
        "after_to_before_symbol_ratio": after_contract_ratio,
        "raw_before_exists": raw_before_path.exists(),
        "raw_after_exists": raw_after_path.exists(),
        "raw_before_position_count": len(raw_before),
        "raw_after_position_count": len(raw_after),
        "stability_before_exists": stability_before_path.exists(),
        "stability_before_sample_count": len(stability_before_samples) if isinstance(stability_before_samples, list) else 0,
        "stability_before_position_symbol_counts": stability_before.get("position_symbol_counts", [])
        if isinstance(stability_before, dict)
        else [],
        "stability_before_position_payload_stable": stability_before.get("position_payload_stable")
        if isinstance(stability_before, dict)
        else None,
        "stability_after_exists": stability_path.exists(),
        "stability_sample_count": len(stability_samples) if isinstance(stability_samples, list) else 0,
        "stability_position_symbol_counts": stability.get("position_symbol_counts", []) if isinstance(stability, dict) else [],
        "stability_position_payload_stable": stability.get("position_payload_stable") if isinstance(stability, dict) else None,
        "missing_after_symbols_from_before_count": len(missing_after_symbols),
        "missing_after_symbols_without_captured_fill_count": len(missing_after_without_fill),
        "material_residual_symbol_count": len(material_rows),
        "material_residual_reason_counts": dict(sorted(reason_counts.items())),
        "disappeared_without_captured_fill_count": len(disappeared_without_fill),
        "side_flip_residual_count": len(side_flip_residuals),
        "largest_material_residuals": sorted(
            material_rows,
            key=lambda row: _safe_float(row.get("unexplained_notional_at_snapshot_price")),
            reverse=True,
        )[:25],
        "interpretation": (
            "attention means the after position snapshot cannot be strictly reconciled from before positions and captured fills. "
            "Future runs include raw and repeated before/after snapshots to distinguish broker/API snapshot drift from real trades."
        ),
    }
    return rows, summary_payload


def _build_residual_diagnosis(
    *,
    reconciliation_rows: list[dict[str, Any]],
    equity_pnl_bridge: dict[str, Any],
    account_field_summary: dict[str, Any],
    account_state_bridge_summary: dict[str, Any] | None = None,
    position_snapshot_summary: dict[str, Any],
    corporate_action_summary: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    corp_summary = corporate_action_summary or {}
    account_state = account_state_bridge_summary or {}
    material_rows = [row for row in reconciliation_rows if str(row.get("material_unexplained_qty")).lower() == "true"]
    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in material_rows:
        by_reason[str(row.get("residual_reason_hint") or "__missing__")].append(row)
    for reason, reason_rows in sorted(by_reason.items()):
        notional = sum(_safe_float(row.get("unexplained_notional_at_snapshot_price")) for row in reason_rows)
        qty = sum(_safe_float(row.get("unexplained_abs_qty")) for row in reason_rows)
        rows.append(
            {
                "diagnosis_type": "position_reconciliation",
                "reason": reason,
                "status": "attention",
                "severity": "warning",
                "symbol_count": len(reason_rows),
                "amount": notional,
                "abs_amount": abs(notional),
                "evidence_artifacts": "16_position_reconciliation.csv; 17_position_reconciliation_summary.json; 37_position_snapshot_integrity.json",
                "examples": _json_cell(
                    [
                        {
                            "symbol": row.get("symbol"),
                            "unexplained_qty": row.get("unexplained_qty"),
                            "unexplained_notional": row.get("unexplained_notional_at_snapshot_price"),
                        }
                        for row in sorted(
                            reason_rows,
                            key=lambda item: _safe_float(item.get("unexplained_notional_at_snapshot_price")),
                            reverse=True,
                        )[:10]
                    ]
                ),
                "interpretation": "Captured fills do not mechanically explain before-to-after broker position movement.",
                "next_action": "Use raw/stability position snapshots and broker activity/order universe to decide whether this is a true trade gap or snapshot timing issue.",
            }
        )

    matched_residual_corp = _safe_int(corp_summary.get("matched_position_residual_symbol_count"))
    if matched_residual_corp > 0:
        rows.append(
            {
                "diagnosis_type": "corporate_action_evidence",
                "reason": "corporate_action_matches_position_residual_symbol",
                "status": "evidence_linked",
                "severity": "info",
                "symbol_count": matched_residual_corp,
                "amount": corp_summary.get("matched_account_activity_symbol_count"),
                "abs_amount": matched_residual_corp,
                "evidence_artifacts": "54_corporate_action_trace.csv; 55_corporate_action_summary.json",
                "examples": _json_cell(corp_summary.get("top_matches")),
                "interpretation": (
                    "One or more residual symbols have same-window corporate-action evidence; inspect action terms "
                    "before treating the residual as unexplained strategy execution drift."
                ),
                "next_action": "Compare the action's effective/ex/payable dates and ratios/cash amounts against position and account deltas.",
            }
        )

    component_amounts = equity_pnl_bridge.get("component_amounts", {}) if isinstance(equity_pnl_bridge, dict) else {}
    equity_residual = _safe_float(component_amounts.get("unexplained_after_snapshot_intraday_realized_activity"))
    equity_change = _safe_float(component_amounts.get("broker_equity_change"))
    strict_account_fields_available = bool(account_field_summary.get("exists_before")) and bool(
        account_field_summary.get("exists_after")
    )
    if abs(equity_residual) > max(100.0, abs(equity_change) * 0.10):
        equity_status = "attention" if strict_account_fields_available else "evidence_limited"
        rows.append(
            {
                "diagnosis_type": "equity_pnl_bridge",
                "reason": "large_unexplained_equity_bridge_residual",
                "status": equity_status,
                "severity": "warning" if equity_status == "attention" else "info",
                "symbol_count": "",
                "amount": equity_residual,
                "abs_amount": abs(equity_residual),
                "evidence_artifacts": "29_equity_pnl_bridge.csv; 30_equity_pnl_bridge.json; 31_account_field_diff.csv; 64_account_state_bridge.csv; 65_account_state_bridge_summary.json",
                "examples": _json_cell(component_amounts),
                "interpretation": (
                    "Broker equity changed more than captured snapshot PnL, realized estimate, and account activities explain."
                    if strict_account_fields_available
                    else "Historical run has an equity bridge residual, but raw account before/after fields are missing, so this is evidence-limited rather than a core audit failure."
                ),
                "next_action": (
                    "Inspect account field deltas, account activities, and future account snapshots for fees, transfers, marks, or missing activity types."
                    if strict_account_fields_available
                    else "Future executor runs persist raw account snapshots and account activities so this bridge can be made stricter."
                ),
            }
        )

    if account_state and account_state.get("status") == "attention":
        rows.append(
            {
                "diagnosis_type": "account_state_bridge",
                "reason": "account_state_equity_delta_mismatch",
                "status": "attention",
                "severity": "warning",
                "symbol_count": "",
                "amount": account_state.get("equity_delta_vs_summary_delta"),
                "abs_amount": abs(_safe_float(account_state.get("equity_delta_vs_summary_delta"))),
                "evidence_artifacts": "64_account_state_bridge.csv; 65_account_state_bridge_summary.json",
                "examples": _json_cell(
                    {
                        "source_before": account_state.get("source_before"),
                        "source_after": account_state.get("source_after"),
                        "equity_delta": account_state.get("equity_delta"),
                        "summary_equity_delta": account_state.get("summary_equity_delta"),
                        "equity_bridge_change": account_state.get("equity_bridge_change"),
                        "cash_delta": account_state.get("cash_delta"),
                        "gross_exposure_delta": account_state.get("gross_exposure_delta"),
                    }
                ),
                "interpretation": "Account snapshot field deltas disagree with summary or equity bridge values.",
                "next_action": "Inspect raw broker account snapshots and account-state bridge rows before relying on equity residual attribution.",
            }
        )

    if not account_field_summary.get("exists_before") or not account_field_summary.get("exists_after"):
        rows.append(
            {
                "diagnosis_type": "account_snapshot",
                "reason": "raw_account_before_after_missing",
                "status": "historical_gap",
                "severity": "info",
                "symbol_count": "",
                "amount": "",
                "abs_amount": "",
                "evidence_artifacts": "31_account_field_diff.csv; 32_account_field_diff_summary.json",
                "examples": _json_cell(
                    {
                        "exists_before": account_field_summary.get("exists_before"),
                        "exists_after": account_field_summary.get("exists_after"),
                        "missing_reason": account_field_summary.get("missing_reason"),
                    }
                ),
                "interpretation": "This run lacks raw account before/after snapshots, so strict account-field attribution is limited.",
                "next_action": "Future executor runs now persist raw account snapshots and repeated after-run account samples.",
            }
        )

    if position_snapshot_summary.get("status") != "pass":
        rows.append(
            {
                "diagnosis_type": "position_snapshot_integrity",
                "reason": "after_position_snapshot_not_strictly_reconciled",
                "status": "attention",
                "severity": "warning",
                "symbol_count": position_snapshot_summary.get("material_residual_symbol_count"),
                "amount": position_snapshot_summary.get("missing_after_symbols_without_captured_fill_count"),
                "abs_amount": position_snapshot_summary.get("missing_after_symbols_without_captured_fill_count"),
                "evidence_artifacts": "37_position_snapshot_integrity.csv; 37_position_snapshot_integrity.json",
                "examples": _json_cell(position_snapshot_summary.get("material_residual_reason_counts")),
                "interpretation": "After snapshot has position movements that are not explained by captured fills.",
                "next_action": "Treat PnL attribution for this day as evidence-limited until raw/stability snapshots exist or broker activities explain the residual.",
            }
        )

    status = "attention" if any(str(row.get("status")) == "attention" for row in rows) else "pass"
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "row_count": len(rows),
        "attention_count": sum(1 for row in rows if str(row.get("status")) == "attention"),
        "diagnosis_type_counts": dict(sorted(Counter(str(row.get("diagnosis_type") or "") for row in rows).items())),
        "position_residual_symbol_count": len(material_rows),
        "position_residual_notional": sum(
            _safe_float(row.get("unexplained_notional_at_snapshot_price")) for row in material_rows
        ),
        "equity_bridge_residual": equity_residual,
        "account_state_bridge_status": account_state.get("status"),
        "account_state_equity_delta": account_state.get("equity_delta"),
        "account_state_cash_delta": account_state.get("cash_delta"),
        "account_state_gross_exposure_delta": account_state.get("gross_exposure_delta"),
        "corporate_action_status": corp_summary.get("status"),
        "corporate_action_count": _safe_int(corp_summary.get("action_count")),
        "corporate_action_matched_position_residual_symbols": matched_residual_corp,
        "corporate_action_matched_account_activity_symbols": _safe_int(
            corp_summary.get("matched_account_activity_symbol_count")
        ),
        "top_rows": sorted(rows, key=lambda row: _safe_float(row.get("abs_amount")), reverse=True)[:20],
    }
    return rows, summary_payload


def _build_evidence_completeness(
    *,
    context: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    artifacts = context.get("artifacts", {}) if isinstance(context.get("artifacts"), dict) else {}
    counts = context.get("artifact_counts", {}) if isinstance(context.get("artifact_counts"), dict) else {}

    def present(key: str) -> bool:
        return _path_exists(artifacts.get(key))

    groups = [
        (
            "core_replay",
            ["execution_summary", "order_plan", "execution_records", "decision_targets", "broker_positions_before", "broker_positions_after"],
            "Minimum files needed to replay the run at a high level.",
        ),
        (
            "broker_raw_state",
            [
                "broker_account_before",
                "broker_account_for_sizing",
                "broker_account_after",
                "broker_account_configurations_before",
                "broker_account_configurations_after",
                "broker_positions_before_raw",
                "broker_positions_after_raw",
                "broker_position_account_stability_before",
                "broker_position_account_stability_after",
                "broker_clock_before",
                "broker_clock_after",
            ],
            "Raw broker state needed for strict account/position attribution.",
        ),
        (
            "portfolio_history",
            ["broker_portfolio_history_before", "broker_portfolio_history_after"],
            "Broker account equity/PnL time series needed to locate when account-level movement occurred.",
        ),
        (
            "market_calendar",
            ["broker_calendar_window"],
            "Official Alpaca trading calendar needed to prove sessions, holidays, half days, and cross-day gaps.",
        ),
        (
            "intraday_price_path",
            [
                "execution_intraday_bars_1min",
                "execution_intraday_bars_1min_after",
                "execution_latest_quotes_snapshot",
                "execution_latest_quotes_snapshot_after",
            ],
            "Raw 1-minute bars and latest quotes needed to attribute fills/reference prices against the day's price path and spread.",
        ),
        (
            "execution_microstructure",
            [
                "broker_fill_activities",
                "broker_account_activities",
                "broker_order_snapshots",
                "broker_orders_all_before",
                "broker_orders_all_after",
                "order_poll_timeline",
                "alpaca_api_audit",
            ],
            "Order, fill, activity, and API evidence for execution replay.",
        ),
        (
            "corporate_actions",
            ["broker_corporate_actions"],
            "Corporate-action evidence needed to explain dividends, splits, symbol changes, and broker adjustments.",
        ),
        (
            "source_reproducibility",
            [
                "run_context",
                "run_evidence_digest",
                "source_code_manifest",
                "source_git_snapshot",
                "source_git_diff",
                "source_code_snapshot",
                "python_environment",
                "input_file_manifest",
            ],
            "Code/config/runtime evidence needed to reproduce future runs.",
        ),
        (
            "scheduler_context",
            ["scheduler_task_context", "scheduler_task_result", "decision_scheduler_task_context", "decision_scheduler_task_result"],
            "Scheduler evidence linking decision and execute tasks to their command context.",
        ),
        (
            "operational_startup",
            [
                "startup_log",
                "tray_launcher_pid_file",
                "scheduler_pid_file",
                "scheduler_due_latest",
                "scheduler_runtime_latest",
            ],
            "Startup, tray, pid, due-check, and heartbeat evidence needed to diagnose missed runs or missing tray icon.",
        ),
    ]
    rows: list[dict[str, Any]] = []
    for area, keys, note in groups:
        present_keys = [key for key in keys if present(key)]
        missing_keys = [key for key in keys if key not in present_keys]
        coverage = len(present_keys) / len(keys) if keys else 1.0
        if area == "core_replay":
            status = "pass" if coverage >= 1.0 else "warning"
        elif coverage >= 0.85:
            status = "pass"
        elif coverage > 0:
            status = "partial"
        else:
            status = "not_available"
        rows.append(
            {
                "area": area,
                "status": status,
                "present_count": len(present_keys),
                "expected_count": len(keys),
                "coverage_ratio": coverage,
                "present_artifacts": _json_cell(present_keys),
                "missing_artifacts": _json_cell(missing_keys),
                "row_count_context": _json_cell(counts),
                "note": note,
            }
        )

    strict_inputs = {
        "position_snapshot_integrity": summaries.get("position_snapshot_integrity", {}).get("status"),
        "residual_diagnosis": summaries.get("residual_diagnosis", {}).get("status"),
        "corporate_action_trace": summaries.get("corporate_action_trace", {}).get("status"),
        "portfolio_history_trace": summaries.get("portfolio_history_trace", {}).get("status"),
        "calendar_trace": summaries.get("calendar_trace", {}).get("status"),
        "intraday_bar_evidence": summaries.get("intraday_bar_evidence", {}).get("status"),
        "quote_evidence": summaries.get("quote_evidence", {}).get("status"),
        "account_field_diff_has_raw": bool(summaries.get("account_field_diff", {}).get("exists_before"))
        and bool(summaries.get("account_field_diff", {}).get("exists_after")),
        "account_state_bridge": summaries.get("account_state_bridge", {}).get("status"),
        "run_evidence_digest": summaries.get("run_evidence_digest", {}).get("status"),
        "run_evidence_digest_strict_missing_files": summaries.get("run_evidence_digest", {}).get(
            "strict_missing_file_count"
        ),
        "startup_binding": summaries.get("startup_binding", {}).get("status"),
        "run_failure_diagnosis": summaries.get("run_failure_diagnosis", {}).get("status"),
        "run_failure_class": summaries.get("run_failure_diagnosis", {}).get("failure_class"),
    }
    strict_ready = (
        present("broker_positions_before_raw")
        and present("broker_positions_after_raw")
        and present("broker_account_before")
        and present("broker_account_after")
        and present("broker_position_account_stability_before")
        and present("broker_position_account_stability_after")
        and (
            summaries.get("run_evidence_digest", {}).get("strict_missing_file_count") in (0, None)
            or not present("run_evidence_digest")
        )
        and summaries.get("position_snapshot_integrity", {}).get("status") == "pass"
    )
    summary_payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "area_count": len(rows),
        "status_counts": dict(sorted(Counter(str(row.get("status") or "") for row in rows).items())),
        "core_replay_ready": next((row.get("status") == "pass" for row in rows if row.get("area") == "core_replay"), False),
        "strict_account_position_replay_ready": bool(strict_ready),
        "strict_inputs": strict_inputs,
        "lowest_coverage_areas": sorted(rows, key=lambda row: _safe_float(row.get("coverage_ratio")))[:5],
        "note": "Historical runs can be core-replay ready but not strict account/position replay ready until future raw/stability artifacts exist.",
    }
    return rows, summary_payload


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
    except Exception:
        return rows
    return rows


def _build_api_audit_outputs(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_rows = _read_jsonl_rows(run_dir / "alpaca_api_audit.jsonl")
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        request = item.get("request") if isinstance(item.get("request"), dict) else {}
        legacy_payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        url_shape = request.get("url_shape") if isinstance(request.get("url_shape"), dict) else {}
        payload_shape = request.get("payload_shape") if isinstance(request.get("payload_shape"), dict) else {}
        payload_body = request.get("payload_body") if isinstance(request.get("payload_body"), dict) else {}
        payload_preview = request.get("payload_preview")
        if not payload_shape and legacy_payload is not None:
            payload_shape = {
                "type": "dict",
                "keys": sorted(str(key) for key in legacy_payload.keys()),
                "key_count": len(legacy_payload),
            }
            payload_preview = legacy_payload
        response_shape = item.get("response_shape") if isinstance(item.get("response_shape"), dict) else {}
        response_body = item.get("response_body") if isinstance(item.get("response_body"), dict) else {}
        rows.append(
            {
                "seq": _safe_int(item.get("seq")),
                "attempt": _safe_int(item.get("attempt")),
                "max_retries": _safe_int(item.get("max_retries")),
                "started_at_utc": item.get("started_at_utc", ""),
                "method": item.get("method", ""),
                "url": item.get("url", ""),
                "url_host": url_shape.get("host", ""),
                "url_path": url_shape.get("path", ""),
                "url_query_count": _safe_int(url_shape.get("query_count")),
                "url_query_keys": ",".join(url_shape.get("query_keys", []))
                if isinstance(url_shape.get("query_keys"), list)
                else "",
                "request_payload_type": payload_shape.get("type", ""),
                "request_payload_key_count": payload_shape.get("key_count", ""),
                "request_payload_keys": ",".join(payload_shape.get("keys", []))
                if isinstance(payload_shape.get("keys"), list)
                else "",
                "request_payload_bytes": _safe_int(payload_body.get("bytes")),
                "request_payload_sha256": payload_body.get("sha256", ""),
                "request_payload_preview": _json_cell(payload_preview),
                "request_payload_preview_truncated": bool(payload_body.get("preview_truncated")),
                "ok": bool(item.get("ok")),
                "elapsed_ms": _safe_float(item.get("elapsed_ms")),
                "status_code": _safe_int(item.get("status_code")),
                "response_body_bytes": _safe_int(response_body.get("bytes") or item.get("response_body_bytes")),
                "response_body_sha256": response_body.get("sha256") or item.get("response_body_sha256", ""),
                "response_type": response_shape.get("type", ""),
                "response_key_count": response_shape.get("key_count", ""),
                "response_keys": ",".join(response_shape.get("keys", [])) if isinstance(response_shape.get("keys"), list) else "",
                "error_type": item.get("error_type", ""),
                "error": item.get("error", ""),
            }
        )
    by_method = Counter(str(row.get("method") or "") for row in rows)
    by_status = Counter(str(row.get("status_code") or "__missing__") for row in rows)
    by_path = Counter(str(row.get("url_path") or "__missing__") for row in rows)
    errors = [row for row in rows if not row.get("ok")]
    payload_rows = [row for row in rows if str(row.get("request_payload_type") or "") not in {"", "NoneType"}]
    url_shape_rows = [row for row in rows if str(row.get("url_path") or "").strip()]
    elapsed_values = [_safe_float(row.get("elapsed_ms")) for row in rows if not _is_missing(row.get("elapsed_ms"))]
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": (run_dir / "alpaca_api_audit.jsonl").as_posix(),
        "exists": (run_dir / "alpaca_api_audit.jsonl").exists(),
        "request_rows": len(rows),
        "ok_rows": sum(1 for row in rows if row.get("ok")),
        "error_rows": len(errors),
        "request_payload_rows": len(payload_rows),
        "request_url_shape_rows": len(url_shape_rows),
        "request_payload_key_counts": dict(
            sorted(Counter(str(row.get("request_payload_key_count") or "__missing__") for row in payload_rows).items())
        ),
        "method_counts": dict(sorted(by_method.items())),
        "status_code_counts": dict(sorted(by_status.items())),
        "path_counts": dict(sorted(by_path.items())),
        "elapsed_ms": {
            "count": len(elapsed_values),
            "min": min(elapsed_values) if elapsed_values else None,
            "max": max(elapsed_values) if elapsed_values else None,
            "mean": _safe_mean(elapsed_values),
        },
        "slowest_requests": sorted(rows, key=lambda row: _safe_float(row.get("elapsed_ms")), reverse=True)[:20],
        "errors": errors[:50],
    }
    return rows, summary


def _build_audit_checks(
    *,
    context: dict[str, Any],
    summary: dict[str, Any],
    records: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    realized_rows: list[dict[str, Any]],
    order_poll_timeline: dict[str, Any],
    order_poll_rows: list[dict[str, Any]],
    broker_order_snapshots: dict[str, Any],
    position_reconciliation_summary: dict[str, Any] | None = None,
    order_attempt_rows: list[dict[str, Any]] | None = None,
    broker_activity_summary: dict[str, Any] | None = None,
    api_audit_summary: dict[str, Any] | None = None,
    broker_order_universe_summary: dict[str, Any] | None = None,
    staged_rebuild_summary: dict[str, Any] | None = None,
    execution_attribution_summary: dict[str, Any] | None = None,
    equity_pnl_bridge: dict[str, Any] | None = None,
    account_field_summary: dict[str, Any] | None = None,
    account_state_bridge_summary: dict[str, Any] | None = None,
    event_timeline_summary: dict[str, Any] | None = None,
    symbol_attribution_summary: dict[str, Any] | None = None,
    target_transition_summary: dict[str, Any] | None = None,
    decision_intent_summary: dict[str, Any] | None = None,
    order_constraint_summary: dict[str, Any] | None = None,
    decision_execute_drift_summary: dict[str, Any] | None = None,
    market_price_evidence_summary: dict[str, Any] | None = None,
    intraday_bar_summary: dict[str, Any] | None = None,
    quote_summary: dict[str, Any] | None = None,
    account_activity_attribution_summary: dict[str, Any] | None = None,
    corporate_action_summary: dict[str, Any] | None = None,
    portfolio_history_summary: dict[str, Any] | None = None,
    calendar_summary: dict[str, Any] | None = None,
    position_snapshot_integrity_summary: dict[str, Any] | None = None,
    residual_diagnosis_summary: dict[str, Any] | None = None,
    evidence_completeness_summary: dict[str, Any] | None = None,
    strict_attribution_checklist_summary: dict[str, Any] | None = None,
    attribution_dossier_summary: dict[str, Any] | None = None,
    startup_binding_summary: dict[str, Any] | None = None,
    run_failure_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def add_check(
        name: str,
        status: str,
        *,
        severity: str = "info",
        detail: str = "",
        expected: Any = None,
        observed: Any = None,
    ) -> None:
        item = {
            "name": name,
            "status": status,
            "severity": severity,
            "detail": detail,
            "expected": expected,
            "observed": observed,
        }
        checks.append(item)
        if status not in {"pass", "not_applicable"}:
            issues.append(item)

    artifacts = context.get("artifacts", {}) if isinstance(context.get("artifacts"), dict) else {}
    required_artifacts = [
        "execution_summary",
        "order_plan",
        "execution_records",
        "decision_targets",
        "broker_positions_before",
        "broker_positions_after",
    ]
    for key in required_artifacts:
        add_check(
            f"required_artifact_exists:{key}",
            "pass" if _path_exists(artifacts.get(key)) else "fail",
            severity="error",
            detail="Core replay input must exist in the execute/decision artifact set.",
            expected=True,
            observed=bool(_path_exists(artifacts.get(key))),
        )

    decision_dir_files = context.get("decision_dir_files", [])
    add_check(
        "decision_dir_inventory",
        "pass" if isinstance(decision_dir_files, list) and len(decision_dir_files) > 0 else "warning",
        severity="warning",
        detail="Decision directory inventory is needed to replay the signal-generation side of the run.",
        expected="non_empty_when_decision_dir_exists",
        observed=len(decision_dir_files) if isinstance(decision_dir_files, list) else None,
    )

    submitted_orders = _safe_int(summary.get("submitted_orders"), default=-1) if isinstance(summary, dict) else -1
    if submitted_orders >= 0:
        add_check(
            "summary_submitted_orders_matches_execution_records",
            "pass" if submitted_orders == len(records) else "fail",
            severity="error",
            detail="execution_summary.submitted_orders should match execution_records rows.",
            expected=submitted_orders,
            observed=len(records),
        )

    add_check(
        "order_trace_rows_present",
        "pass" if len(order_rows) > 0 or not summary.get("submitted") else "fail",
        severity="error",
        detail="Submitted sessions should have order trace rows.",
        expected=">0 when submitted",
        observed=len(order_rows),
    )
    filled_records = [
        record
        for record in records
        if _safe_float(record.get("filled_qty")) > 0
        or any(
            isinstance(attempt, dict) and _safe_float(attempt.get("filled_qty")) > 0
            for attempt in (record.get("attempts") or [])
            if isinstance(record.get("attempts"), list)
        )
    ]
    add_check(
        "filled_records_have_fill_trace",
        "pass" if not filled_records or len(fill_rows) > 0 else "warning",
        severity="warning",
        detail="If execution records show fills, audit fill_trace should contain broker fills or fallback fills.",
        expected="fill_rows>0 when filled_records>0",
        observed={"filled_record_count": len(filled_records), "fill_rows": len(fill_rows)},
    )
    add_check(
        "realized_rows_do_not_exceed_empty_fill_trace",
        "pass" if fill_rows or not realized_rows else "warning",
        severity="warning",
        detail="Realized ledger rows without fill trace rows would make attribution hard to verify.",
        expected="no realized rows without fill rows",
        observed={"fill_rows": len(fill_rows), "realized_rows": len(realized_rows)},
    )

    timeline_event_count = (
        _safe_int(order_poll_timeline.get("event_count"))
        if isinstance(order_poll_timeline, dict) and order_poll_timeline
        else 0
    )
    if order_poll_timeline:
        add_check(
            "order_poll_timeline_event_count_matches_rows",
            "pass" if timeline_event_count == len(order_poll_rows) else "fail",
            severity="error",
            detail="order_poll_timeline.event_count should equal flattened CSV rows.",
            expected=timeline_event_count,
            observed=len(order_poll_rows),
        )
        summary_poll_count = _safe_int(summary.get("order_poll_event_count"), default=-1)
        if summary_poll_count >= 0:
            add_check(
                "summary_order_poll_event_count_matches_timeline",
                "pass" if summary_poll_count == timeline_event_count else "fail",
                severity="error",
                detail="execution_summary.order_poll_event_count should match order_poll_timeline.",
                expected=summary_poll_count,
                observed=timeline_event_count,
            )
    else:
        outputs = summary.get("outputs", {}) if isinstance(summary.get("outputs"), dict) else {}
        poll_expected = "order_poll_event_count" in summary or bool(outputs.get("order_poll_timeline_json"))
        add_check(
            "order_poll_timeline_available",
            "warning" if poll_expected else "not_applicable",
            severity="warning" if poll_expected else "info",
            detail=(
                "execution_summary indicates order_poll_timeline should exist."
                if poll_expected
                else "Historical runs before the poll-timeline upgrade may not have order_poll_timeline.json."
            ),
            expected="present for future execute runs",
            observed=False,
        )

    order_ids = _record_order_ids(records)
    snapshot_ids: set[str] = set()
    for snapshot in broker_order_snapshots.get("snapshots", []) if isinstance(broker_order_snapshots, dict) else []:
        if isinstance(snapshot, dict) and str(snapshot.get("id") or "").strip():
            snapshot_ids.add(str(snapshot.get("id")).strip())
    missing_snapshots = sorted(order_id for order_id in order_ids if snapshot_ids and order_id not in snapshot_ids)
    add_check(
        "broker_order_snapshots_cover_recorded_order_ids",
        "pass" if not snapshot_ids or not missing_snapshots else "warning",
        severity="warning",
        detail="Final broker order snapshots should cover all submitted order ids when snapshots are available.",
        expected=len(order_ids),
        observed={"snapshot_ids": len(snapshot_ids), "missing_order_ids": missing_snapshots[:20]},
    )

    attempt_rows = order_attempt_rows or []
    if records:
        add_check(
            "order_attempt_trace_present",
            "pass" if len(attempt_rows) >= len(records) else "warning",
            severity="warning",
            detail="Attempt-level order trace should have at least one row per execution record.",
            expected=f">={len(records)}",
            observed=len(attempt_rows),
        )

    activity_summary = broker_activity_summary or {}
    if activity_summary:
        activity_expected = bool(artifacts.get("broker_fill_activities") or artifacts.get("broker_account_activities"))
        add_check(
            "broker_activity_trace_present",
            "pass" if _safe_int(activity_summary.get("row_count")) > 0 else "warning" if activity_expected else "not_applicable",
            severity="warning" if activity_expected else "info",
            detail=(
                "Broker activity trace should contain captured account/FILL activities when available."
                if activity_expected
                else "Historical runs before broker activity capture may not have raw activity artifacts."
            ),
            expected=">0 when broker activity artifacts exist",
            observed=activity_summary.get("row_count"),
        )

    api_summary = api_audit_summary or {}
    api_expected = bool(artifacts.get("alpaca_api_audit"))
    if api_expected or api_summary.get("exists"):
        add_check(
            "alpaca_api_audit_parseable",
            "pass" if _safe_int(api_summary.get("request_rows")) > 0 else "warning",
            severity="warning",
            detail="Per-request Alpaca API audit should parse into request summary rows for future runs.",
            expected="request_rows>0 when alpaca_api_audit exists",
            observed=api_summary.get("request_rows"),
        )
        method_counts = api_summary.get("method_counts", {}) if isinstance(api_summary.get("method_counts"), dict) else {}
        post_rows = _safe_int(method_counts.get("POST"))
        mutating_rows = post_rows + _safe_int(method_counts.get("DELETE"))
        payload_rows = _safe_int(api_summary.get("request_payload_rows"))
        add_check(
            "alpaca_api_request_payload_audit",
            "pass" if post_rows <= 0 or payload_rows >= post_rows else "warning",
            severity="warning",
            detail="POST Alpaca API calls should have a redacted request payload digest for replay.",
            expected="request_payload_rows>=POST rows when POST calls exist",
            observed={
                "post_rows": post_rows,
                "request_payload_rows": payload_rows,
                "method_counts": method_counts,
            },
        )
        add_check(
            "alpaca_api_url_shape_audit",
            "pass"
            if _safe_int(api_summary.get("request_url_shape_rows")) >= _safe_int(api_summary.get("request_rows"))
            else "warning",
            severity="warning",
            detail="Every Alpaca API audit row should include URL host/path/query-key shape for replay without parsing raw URLs.",
            expected="request_url_shape_rows==request_rows",
            observed={
                "request_rows": api_summary.get("request_rows"),
                "request_url_shape_rows": api_summary.get("request_url_shape_rows"),
                "mutating_rows": mutating_rows,
            },
        )

    startup_binding = startup_binding_summary or {}
    if startup_binding:
        add_check(
            "startup_binding_evidence_present",
            "pass" if startup_binding.get("status") == "pass" else "warning",
            severity="warning",
            detail="Startup binding audit should prove Start.bat, autostart, pid files, due check, and runtime heartbeat are observable.",
            expected="pass",
            observed={
                "status": startup_binding.get("status"),
                "issue_count": startup_binding.get("issue_count"),
                "autostart_registered": startup_binding.get("autostart_registered"),
                "scheduler_due_latest_exists": startup_binding.get("scheduler_due_latest_exists"),
                "scheduler_runtime_latest_exists": startup_binding.get("scheduler_runtime_latest_exists"),
                "process_health_status": startup_binding.get("process_health_status"),
            },
        )
        add_check(
            "startup_autostart_registered",
            "pass" if startup_binding.get("autostart_registered") else "warning",
            severity="warning",
            detail="The Windows logon task should be registered so reboot/login starts the visible tray-bound program.",
            expected=True,
            observed=startup_binding.get("autostart_registered"),
        )

    run_failure = run_failure_summary or {}
    if run_failure:
        add_check(
            "run_failure_diagnosis_clear",
            "pass" if run_failure.get("status") == "pass" else "fail" if run_failure.get("status") == "fail" else "warning",
            severity="error" if run_failure.get("status") == "fail" else "warning",
            detail="Scheduler/executor failure diagnosis should be clear before the run is treated as a complete trading sample.",
            expected="pass",
            observed={
                "status": run_failure.get("status"),
                "task_status": run_failure.get("task_status"),
                "failure_class": run_failure.get("failure_class"),
                "error_type": run_failure.get("error_type"),
                "error": run_failure.get("error"),
                "missing_core_artifacts": run_failure.get("missing_core_artifacts"),
            },
        )

    order_universe = broker_order_universe_summary or {}
    if order_universe:
        missing_count = _safe_int(order_universe.get("execution_order_ids_missing_from_universe_count"))
        truncated_sources = order_universe.get("paged_order_capture_truncated_sources", [])
        order_universe_expected = bool(
            artifacts.get("broker_order_snapshots")
            or artifacts.get("broker_orders_all_before")
            or artifacts.get("broker_orders_all_before_submit")
            or artifacts.get("broker_orders_all_after_cancel")
            or artifacts.get("broker_orders_all_after")
        )
        add_check(
            "broker_order_universe_covers_execution_order_ids",
            "pass" if missing_count <= 0 else "warning" if order_universe_expected else "not_applicable",
            severity="warning" if order_universe_expected else "info",
            detail=(
                "Combined broker order universe should include every order id recorded by the executor."
                if order_universe_expected
                else "Historical runs before broker order snapshot capture may not have order-universe artifacts."
            ),
            expected=0,
            observed=missing_count,
        )
        if truncated_sources:
            add_check(
                "broker_order_universe_paged_capture_not_truncated",
                "warning",
                severity="warning",
                detail="Paged broker all-orders capture hit its configured max page boundary.",
                expected=[],
                observed=truncated_sources,
            )

    staged_summary = staged_rebuild_summary or {}
    outputs = summary.get("outputs", {}) if isinstance(summary.get("outputs"), dict) else {}
    if str(summary.get("execution_mode") or "") == "staged_regt":
        staged_expected = bool(artifacts.get("staged_rebuild_snapshots") or outputs.get("staged_rebuild_snapshots_json"))
        if staged_expected or staged_summary.get("exists"):
            add_check(
                "staged_rebuild_snapshots_parseable",
                "pass" if _safe_int(staged_summary.get("snapshot_count")) > 0 or not summary.get("submitted") else "warning",
                severity="warning",
                detail="Staged RegT runs should preserve release/rebuild/buying-power-cap snapshots for replay.",
                expected="snapshot_count>0 when staged submitted orders were evaluated",
                observed=staged_summary.get("snapshot_count"),
            )
        else:
            add_check(
                "staged_rebuild_snapshots_available",
                "not_applicable",
                severity="info",
                detail="Historical staged runs before this upgrade may not have staged_rebuild_snapshots.json.",
                expected="present for future staged_regt execute runs",
                observed=False,
            )

    execution_attr = execution_attribution_summary or {}
    if records:
        add_check(
            "execution_attribution_trace_present",
            "pass" if _safe_int(execution_attr.get("attempt_row_count")) >= len(records) else "warning",
            severity="warning",
            detail="Execution attribution should include at least one attempt row per execution record.",
            expected=f">={len(records)}",
            observed=execution_attr.get("attempt_row_count"),
        )

    bridge = equity_pnl_bridge or {}
    if summary:
        add_check(
            "equity_pnl_bridge_present",
            "pass" if bridge.get("components") else "warning",
            severity="warning",
            detail="Daily audit should include a diagnostic bridge from broker equity change to PnL/activity components.",
            expected="non_empty components",
            observed=len(bridge.get("components", [])) if isinstance(bridge.get("components"), list) else 0,
        )

    account_diff = account_field_summary or {}
    if artifacts.get("broker_account_before") or artifacts.get("broker_account_after"):
        add_check(
            "account_field_diff_parseable",
            "pass"
            if bool(account_diff.get("exists_before")) and bool(account_diff.get("exists_after"))
            else "warning",
            severity="warning",
            detail="Raw account before/after snapshots should produce account field deltas.",
            expected={"exists_before": True, "exists_after": True},
            observed={
                "exists_before": account_diff.get("exists_before"),
                "exists_after": account_diff.get("exists_after"),
                "row_count": account_diff.get("row_count"),
            },
        )

    account_state_bridge = account_state_bridge_summary or {}
    account_state_expected = bool(
        artifacts.get("broker_account_before")
        or artifacts.get("broker_account_for_sizing")
        or artifacts.get("broker_account_after")
    )
    add_check(
        "account_state_bridge_present",
        "pass"
        if "row_count" in account_state_bridge
        else "warning"
        if account_state_expected
        else "not_applicable",
        severity="warning" if account_state_expected else "info",
        detail=(
            "Account-state bridge should parse broker account snapshots into equity/cash/exposure/margin deltas."
            if account_state_expected
            else "Historical runs before raw account snapshot capture may not have this artifact."
        ),
        expected="summary present when account snapshots exist",
        observed=account_state_bridge.get("row_count"),
    )
    if account_state_bridge:
        add_check(
            "account_state_bridge_consistent",
            "pass"
            if account_state_bridge.get("status") in {"pass", "historical_limited"}
            else "warning",
            severity="warning" if account_state_bridge.get("status") == "attention" else "info",
            detail="Account-state equity delta should agree with execution summary and equity bridge when snapshots exist.",
            expected="pass or historical_limited",
            observed={
                "status": account_state_bridge.get("status"),
                "equity_delta": account_state_bridge.get("equity_delta"),
                "summary_equity_delta": account_state_bridge.get("summary_equity_delta"),
                "equity_delta_vs_summary_delta": account_state_bridge.get("equity_delta_vs_summary_delta"),
                "equity_delta_vs_equity_bridge_change": account_state_bridge.get(
                    "equity_delta_vs_equity_bridge_change"
                ),
            },
        )

    timeline = event_timeline_summary or {}
    add_check(
        "event_timeline_present",
        "pass" if _safe_int(timeline.get("event_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should include a unified event timeline for replay.",
        expected="event_count>0",
        observed=timeline.get("event_count"),
    )

    symbol_attr = symbol_attribution_summary or {}
    add_check(
        "symbol_attribution_bridge_present",
        "pass" if _safe_int(symbol_attr.get("symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should include symbol-level PnL/execution/residual bridge rows.",
        expected="symbol_count>0",
        observed=symbol_attr.get("symbol_count"),
    )

    target_transition = target_transition_summary or {}
    add_check(
        "target_transition_trace_present",
        "pass" if _safe_int(target_transition.get("symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should compare intended before-to-target transition against the after-position snapshot.",
        expected="symbol_count>0",
        observed=target_transition.get("symbol_count"),
    )
    if target_transition:
        add_check(
            "target_transition_verified",
            "pass" if target_transition.get("status") == "pass" else "warning",
            severity="warning",
            detail="Target-transition attribution is strict only when material position residuals are absent; target gaps remain performance diagnostics.",
            expected="pass",
            observed={
                "status": target_transition.get("status"),
                "attention_symbol_count": target_transition.get("attention_symbol_count"),
                "material_position_residual_symbols": target_transition.get("material_position_residual_symbols"),
                "target_gap_symbol_count_without_position_residual": target_transition.get(
                    "target_gap_symbol_count_without_position_residual"
                ),
            },
        )

    decision_intent = decision_intent_summary or {}
    add_check(
        "decision_intent_trace_present",
        "pass" if _safe_int(decision_intent.get("symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should explain raw target weights, executable projected weights, and no-order reasons.",
        expected="symbol_count>0",
        observed=decision_intent.get("symbol_count"),
    )
    if decision_intent:
        add_check(
            "decision_intent_unexplained_no_order_clear",
            "pass" if _safe_int(decision_intent.get("unexplained_no_order_symbol_count")) <= 0 else "warning",
            severity="warning",
            detail="Symbols with material target delta and no planned/skipped order need an explicit explanation.",
            expected=0,
            observed=decision_intent.get("unexplained_no_order_symbol_count"),
        )

    order_constraints = order_constraint_summary or {}
    add_check(
        "order_constraint_trace_present",
        "pass" if _safe_int(order_constraints.get("row_count")) > 0 or not summary.get("submitted") else "warning",
        severity="warning",
        detail="Audit package should explain order-builder constraints, skips, quantity rounding, and fill coverage.",
        expected="row_count>0 when submitted",
        observed=order_constraints.get("row_count"),
    )

    decision_execute_drift = decision_execute_drift_summary or {}
    add_check(
        "decision_execute_drift_trace_present",
        "pass" if _safe_int(decision_execute_drift.get("symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should compare decision-time plan against execute-time rebuilt plan.",
        expected="symbol_count>0",
        observed=decision_execute_drift.get("symbol_count"),
    )

    market_price = market_price_evidence_summary or {}
    add_check(
        "market_price_evidence_present",
        "pass" if _safe_int(market_price.get("symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should expand reference-price/latest-trade evidence by symbol.",
        expected="symbol_count>0",
        observed=market_price.get("symbol_count"),
    )
    if market_price:
        add_check(
            "market_price_reference_coverage",
            "pass" if _safe_int(market_price.get("execute_missing_reference_symbol_count")) <= 0 else "warning",
            severity="warning",
            detail="Execution reference prices should be available for every target/position symbol.",
            expected=0,
            observed=market_price.get("execute_missing_reference_symbol_count"),
        )

    intraday_bars = intraday_bar_summary or {}
    intraday_expected = bool(artifacts.get("execution_intraday_bars_1min") or artifacts.get("execution_intraday_bars_1min_after"))
    add_check(
        "intraday_bar_evidence_present",
        "pass"
        if "symbol_count" in intraday_bars
        else "warning"
        if intraday_expected
        else "not_applicable",
        severity="warning" if intraday_expected else "info",
        detail=(
            "Intraday bar evidence should parse execution_intraday_bars_1min*.json when raw files exist."
            if intraday_expected
            else "Historical runs before intraday-bar capture may not have this artifact."
        ),
        expected="summary present when raw artifact exists",
        observed=intraday_bars.get("symbol_count"),
    )
    if intraday_bars:
        add_check(
            "intraday_bar_capture_ok",
            "pass" if intraday_bars.get("status") in {"pass", "partial", "historical_limited"} else "warning",
            severity="warning" if intraday_bars.get("status") == "attention" else "info",
            detail="Relevant-symbol 1-minute bars should be captured without missing filled symbols or source errors.",
            expected="pass, partial, or historical_limited",
            observed={
                "status": intraday_bars.get("status"),
                "symbol_count": intraday_bars.get("symbol_count"),
                "filled_symbols_missing_bars_count": intraday_bars.get("filled_symbols_missing_bars_count"),
                "error_count": intraday_bars.get("error_count"),
            },
        )

    quotes = quote_summary or {}
    quote_expected = bool(artifacts.get("execution_latest_quotes_snapshot") or artifacts.get("execution_latest_quotes_snapshot_after"))
    add_check(
        "quote_evidence_present",
        "pass"
        if "symbol_count" in quotes
        else "warning"
        if quote_expected
        else "not_applicable",
        severity="warning" if quote_expected else "info",
        detail=(
            "Quote evidence should parse execution_latest_quotes_snapshot.json when raw files exist."
            if quote_expected
            else "Historical runs before quote capture may not have this artifact."
        ),
        expected="summary present when raw artifact exists",
        observed=quotes.get("symbol_count"),
    )
    if quotes:
        add_check(
            "quote_capture_ok",
            "pass" if quotes.get("status") in {"pass", "partial", "historical_limited"} else "warning",
            severity="warning" if quotes.get("status") == "attention" else "info",
            detail="Latest quote capture should provide parseable bid/ask context without invalid quotes or source errors.",
            expected="pass, partial, or historical_limited",
            observed={
                "status": quotes.get("status"),
                "quote_symbol_count": quotes.get("quote_symbol_count"),
                "missing_quote_symbol_count": quotes.get("missing_quote_symbol_count"),
                "invalid_quote_symbol_count": quotes.get("invalid_quote_symbol_count"),
                "error_count": quotes.get("error_count"),
            },
        )

    portfolio_history = portfolio_history_summary or {}
    portfolio_history_expected = bool(
        artifacts.get("broker_portfolio_history_before") or artifacts.get("broker_portfolio_history_after")
    )
    add_check(
        "portfolio_history_trace_present",
        "pass"
        if "row_count" in portfolio_history
        else "warning"
        if portfolio_history_expected
        else "not_applicable",
        severity="warning" if portfolio_history_expected else "info",
        detail=(
            "Portfolio history trace should parse broker portfolio-history artifacts when raw files exist."
            if portfolio_history_expected
            else "Historical runs before portfolio-history capture may not have this artifact."
        ),
        expected="summary present when raw artifact exists",
        observed=portfolio_history.get("row_count"),
    )
    if portfolio_history:
        add_check(
            "portfolio_history_capture_ok",
            "pass" if portfolio_history.get("status") in {"pass", "historical_limited"} else "warning",
            severity="warning" if portfolio_history.get("status") == "attention" else "info",
            detail="Broker portfolio-history capture should produce a parseable account equity/PnL time series.",
            expected="pass or historical_limited",
            observed={
                "status": portfolio_history.get("status"),
                "row_count": portfolio_history.get("row_count"),
                "summary_vs_history_after_delta": portfolio_history.get("summary_vs_history_after_delta"),
            },
        )

    calendar = calendar_summary or {}
    calendar_expected = bool(artifacts.get("broker_calendar_window"))
    add_check(
        "calendar_trace_present",
        "pass"
        if "row_count" in calendar
        else "warning"
        if calendar_expected
        else "not_applicable",
        severity="warning" if calendar_expected else "info",
        detail=(
            "Calendar trace should parse broker_calendar_window.json when the raw artifact exists."
            if calendar_expected
            else "Historical runs before calendar capture may not have this artifact."
        ),
        expected="summary present when raw artifact exists",
        observed=calendar.get("row_count"),
    )
    if calendar:
        add_check(
            "calendar_session_confirmed",
            "pass"
            if calendar.get("status") in {"pass", "historical_limited"}
            else "warning",
            severity="warning" if calendar.get("status") == "attention" else "info",
            detail="Official Alpaca calendar should include the execute session date and expose expected adjacent sessions.",
            expected="pass or historical_limited",
            observed={
                "status": calendar.get("status"),
                "session_date_in_calendar": calendar.get("session_date_in_calendar"),
                "expected_previous_trading_date": calendar.get("expected_previous_trading_date"),
                "expected_next_trading_date": calendar.get("expected_next_trading_date"),
                "session_is_half_day": calendar.get("session_is_half_day"),
            },
        )

    account_activity_attr = account_activity_attribution_summary or {}
    add_check(
        "account_activity_attribution_present",
        "pass" if "row_count" in account_activity_attr else "warning",
        severity="warning",
        detail="Audit package should classify broker account activities into fills, fees, dividends, transfers, and unknowns.",
        expected="summary present",
        observed=account_activity_attr.get("row_count"),
    )
    if account_activity_attr:
        add_check(
            "account_activity_unknown_net_clear",
            "pass" if abs(_safe_float(account_activity_attr.get("unknown_activity_net_amount"))) <= 1e-6 else "warning",
            severity="warning",
            detail="Unknown broker account activities with non-zero net amount can hide non-strategy cashflow or costs.",
            expected=0.0,
            observed=account_activity_attr.get("unknown_activity_net_amount"),
        )

    corp = corporate_action_summary or {}
    corp_expected = bool(artifacts.get("broker_corporate_actions"))
    add_check(
        "corporate_action_trace_present",
        "pass" if "action_count" in corp else "warning" if corp_expected else "not_applicable",
        severity="warning" if corp_expected else "info",
        detail=(
            "Corporate-action trace should parse broker_corporate_actions.json when the raw artifact exists."
            if corp_expected
            else "Historical runs before corporate-action capture may not have this artifact."
        ),
        expected="summary present when raw artifact exists",
        observed=corp.get("action_count"),
    )
    if corp:
        add_check(
            "corporate_action_raw_capture_ok",
            "pass" if _safe_int(corp.get("error_count")) <= 0 else "warning",
            severity="warning",
            detail="Alpaca corporate-action capture should complete without per-chunk API errors.",
            expected=0,
            observed=corp.get("error_count"),
        )
        residual_without_action = _safe_int(corp.get("residual_symbols_without_corporate_action_count"))
        residual_total = _safe_int((position_reconciliation_summary or {}).get("symbols_with_material_unexplained_qty"))
        add_check(
            "corporate_action_residual_symbol_coverage_recorded",
            "pass" if residual_total <= 0 or residual_without_action <= residual_total else "warning",
            severity="info",
            detail="Records whether residual symbols had same-window corporate-action evidence; absence is a useful attribution fact, not by itself a failure.",
            expected="coverage recorded",
            observed={
                "material_residual_symbols": residual_total,
                "matched_position_residual_symbol_count": corp.get("matched_position_residual_symbol_count"),
                "residual_symbols_without_corporate_action_count": corp.get(
                    "residual_symbols_without_corporate_action_count"
                ),
            },
        )

    snapshot_integrity = position_snapshot_integrity_summary or {}
    add_check(
        "position_snapshot_integrity",
        "pass" if snapshot_integrity.get("status") == "pass" else "warning",
        severity="warning",
        detail="After position snapshot should be reconcilable from before positions and captured fills, or supported by raw/stability evidence.",
        expected="pass",
        observed={
            "status": snapshot_integrity.get("status"),
            "before_position_symbols": snapshot_integrity.get("before_position_symbols"),
            "after_position_symbols": snapshot_integrity.get("after_position_symbols"),
            "material_residual_symbol_count": snapshot_integrity.get("material_residual_symbol_count"),
        },
    )

    residual_diagnosis = residual_diagnosis_summary or {}
    add_check(
        "residual_diagnosis_clear",
        "pass" if residual_diagnosis.get("status") == "pass" else "warning",
        severity="warning",
        detail="Residual diagnosis should have no attention items for strict attribution.",
        expected="pass",
        observed={
            "status": residual_diagnosis.get("status"),
            "attention_count": residual_diagnosis.get("attention_count"),
            "position_residual_symbol_count": residual_diagnosis.get("position_residual_symbol_count"),
            "equity_bridge_residual": residual_diagnosis.get("equity_bridge_residual"),
        },
    )

    evidence_completeness = evidence_completeness_summary or {}
    strict_replay_expected = bool(
        evidence_completeness.get("strict_account_position_replay_ready")
        or artifacts.get("broker_position_account_stability_before")
        or artifacts.get("broker_position_account_stability_after")
        or artifacts.get("broker_positions_after_raw")
        or artifacts.get("broker_account_after")
        or snapshot_integrity.get("status") != "pass"
    )
    add_check(
        "strict_account_position_replay_ready",
        "pass"
        if evidence_completeness.get("strict_account_position_replay_ready")
        else "warning"
        if strict_replay_expected
        else "not_applicable",
        severity="warning" if strict_replay_expected else "info",
        detail=(
            "Strict replay needs raw before/after account+position snapshots and stable after-run position/account evidence."
            if strict_replay_expected
            else "Historical runs before raw/stability capture are core-replayable but not strict account/position replayable."
        ),
        expected=True,
        observed=evidence_completeness.get("strict_account_position_replay_ready"),
    )

    strict_checklist = strict_attribution_checklist_summary or {}
    strict_ready = bool(strict_checklist.get("strict_attribution_ready"))
    strict_status = str(strict_checklist.get("status") or "")
    strict_blockers = _safe_int(strict_checklist.get("blocking_item_count"))
    add_check(
        "strict_attribution_checklist_ready",
        "pass"
        if strict_ready or (strict_status == "historical_limited" and strict_blockers <= 0)
        else "warning",
        severity="warning" if strict_blockers > 0 else "info",
        detail="Strict attribution checklist should have no blocking items before treating performance attribution as complete.",
        expected="ready or historical_limited_without_blockers",
        observed={
            "status": strict_status,
            "strict_attribution_ready": strict_ready,
            "blocking_item_count": strict_blockers,
        },
    )

    attribution_dossier = attribution_dossier_summary or {}
    add_check(
        "attribution_dossier_present",
        "pass" if _safe_int(attribution_dossier.get("focus_symbol_count")) > 0 else "warning",
        severity="warning",
        detail="Audit package should include a ranked review index that ties PnL, residuals, drift, and evidence gaps together by symbol.",
        expected="focus_symbol_count>0",
        observed=attribution_dossier.get("focus_symbol_count"),
    )

    reconciliation = position_reconciliation_summary or {}
    if reconciliation:
        add_check(
            "position_reconciliation_unexplained_qty",
            "pass" if _safe_float(reconciliation.get("symbols_with_material_unexplained_qty")) <= 0 else "warning",
            severity="warning",
            detail="Before position plus signed fills should reconcile to after position except for rounding/snapshot timing.",
            expected=0,
            observed=reconciliation.get("symbols_with_material_unexplained_qty"),
        )

    severity_counts = Counter(str(item.get("severity") or "info") for item in issues)
    status = "fail" if severity_counts.get("error") else "attention" if issues else "pass"
    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "issue_count": len(issues),
        "issue_count_by_severity": dict(sorted(severity_counts.items())),
        "checks": checks,
        "issues": issues,
    }


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return True
    return False


def _safe_mean(values: list[float]) -> float | None:
    return float(sum(values) / len(values)) if values else None


def _numeric_summary(rows: list[dict[str, Any]], column: str) -> dict[str, Any]:
    values = [_safe_float(row.get(column)) for row in rows if not _is_missing(row.get(column))]
    return {
        "column": column,
        "non_null": len(values),
        "missing": max(0, len(rows) - len(values)),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": _safe_mean(values),
        "zero_count": sum(1 for value in values if abs(value) <= 1e-12),
    }


def _date_age_days(session_date: str, raw_date: Any) -> int | None:
    if _is_missing(session_date) or _is_missing(raw_date):
        return None
    try:
        session_dt = datetime.fromisoformat(str(session_date)[:10])
        raw_dt = datetime.fromisoformat(str(raw_date)[:10])
    except Exception:
        return None
    return int((session_dt.date() - raw_dt.date()).days)


def _counter_from_rows(rows: list[dict[str, Any]], column: str, *, top_n: int = 20) -> dict[str, int]:
    counts = Counter(str(row.get(column) or "__missing__") for row in rows)
    return dict(counts.most_common(top_n))


def _build_data_quality_snapshot(
    *,
    alpha_rows: list[dict[str, Any]],
    targets: dict[str, dict[str, Any]],
    plan: dict[str, Any],
    session_date: str,
    alpha_path: Path | None,
) -> dict[str, Any]:
    alpha_symbols = {str(row.get("symbol") or "").upper().strip() for row in alpha_rows if str(row.get("symbol") or "").strip()}
    target_symbols = set(targets)
    target_missing_alpha = sorted(target_symbols - alpha_symbols)
    alpha_not_target = sorted(alpha_symbols - target_symbols)
    target_weights = [_safe_float(row.get("signed_weight") or row.get("target_signed_weight")) for row in targets.values()]
    long_weights = [value for value in target_weights if value > 0]
    short_weights = [value for value in target_weights if value < 0]
    important_columns = [
        "symbol",
        "price_asof_session_date",
        "close",
        "lagged_raw_close",
        "return_5d",
        "momentum_l120_s20",
        "beta",
        "beta_obs",
        "sec_status",
        "sec_payload_source",
        "shares_outstanding",
        "share_source",
        "last_fundamental_filed_date",
        "assets",
        "liabilities",
        "cash",
        "market_cap",
        "cash_to_assets",
        *FACTOR_COLUMNS,
        "composite_score",
        "composite_rank",
    ]
    missing_by_column = {
        column: sum(1 for row in alpha_rows if _is_missing(row.get(column)))
        for column in important_columns
        if alpha_rows
    }
    price_ages = [
        age
        for age in (_date_age_days(session_date, row.get("price_asof_session_date")) for row in alpha_rows)
        if age is not None
    ]
    fundamental_ages = [
        age
        for age in (_date_age_days(session_date, row.get("last_fundamental_filed_date")) for row in alpha_rows)
        if age is not None
    ]
    stale_price_rows = [
        {
            "symbol": str(row.get("symbol") or "").upper(),
            "price_asof_session_date": row.get("price_asof_session_date"),
            "age_days": _date_age_days(session_date, row.get("price_asof_session_date")),
        }
        for row in alpha_rows
        if (_date_age_days(session_date, row.get("price_asof_session_date")) or 0) > 5
    ]
    score_columns = [
        "return_5d",
        "momentum_l120_s20",
        "beta",
        "beta_obs",
        "market_cap",
        "cash_to_assets",
        *FACTOR_COLUMNS,
        "composite_score",
        "composite_rank",
    ]
    return {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": session_date,
        "alpha_path": alpha_path.as_posix() if alpha_path else None,
        "coverage": {
            "alpha_rows": len(alpha_rows),
            "alpha_symbols": len(alpha_symbols),
            "target_symbols": len(target_symbols),
            "target_symbols_with_alpha": len(target_symbols & alpha_symbols),
            "target_symbols_missing_alpha": len(target_missing_alpha),
            "alpha_symbols_not_targeted": len(alpha_not_target),
            "target_missing_alpha_symbols": target_missing_alpha[:100],
            "alpha_not_target_symbols_sample": alpha_not_target[:100],
        },
        "target_weights": {
            "long_count": len(long_weights),
            "short_count": len(short_weights),
            "gross_long_weight": float(sum(long_weights)),
            "gross_short_weight_abs": float(abs(sum(short_weights))),
            "net_weight": float(sum(target_weights)),
            "gross_weight": float(sum(abs(value) for value in target_weights)),
            "max_abs_weight": max((abs(value) for value in target_weights), default=0.0),
        },
        "plan_controls": {
            "execution_mode": plan.get("execution_mode"),
            "execution_order_style": plan.get("execution_order_style"),
            "decision_status": plan.get("decision_status"),
            "decision_skip_reason": plan.get("decision_skip_reason"),
            "target_short_floor_diagnostics": plan.get("target_short_floor_diagnostics"),
            "lot_sync_before_decision": plan.get("lot_sync_before_decision"),
        },
        "missing_by_column": missing_by_column,
        "sec_status_counts": _counter_from_rows(alpha_rows, "sec_status"),
        "sec_payload_source_counts": _counter_from_rows(alpha_rows, "sec_payload_source"),
        "share_source_counts": _counter_from_rows(alpha_rows, "share_source"),
        "market_cap_price_source_counts": _counter_from_rows(alpha_rows, "market_cap_price_source"),
        "price_asof_session_date_counts": _counter_from_rows(alpha_rows, "price_asof_session_date"),
        "price_staleness_days": {
            "count": len(price_ages),
            "min": min(price_ages) if price_ages else None,
            "max": max(price_ages) if price_ages else None,
            "mean": _safe_mean([float(value) for value in price_ages]),
            "symbols_over_5_calendar_days": stale_price_rows[:100],
            "symbols_over_5_calendar_days_count": len(stale_price_rows),
        },
        "fundamental_filed_age_days": {
            "count": len(fundamental_ages),
            "min": min(fundamental_ages) if fundamental_ages else None,
            "max": max(fundamental_ages) if fundamental_ages else None,
            "mean": _safe_mean([float(value) for value in fundamental_ages]),
        },
        "numeric_summaries": {column: _numeric_summary(alpha_rows, column) for column in score_columns},
    }


def _signed_position_qty(row: dict[str, Any]) -> float:
    if "signed_qty" in row and not _is_missing(row.get("signed_qty")):
        return _safe_float(row.get("signed_qty"))
    qty = _safe_float(row.get("qty"))
    side = str(row.get("side") or "").lower()
    if side == "short" and qty > 0:
        return -abs(qty)
    return qty


def _signed_trade_qty(side: str, qty: float) -> float:
    side_l = str(side or "").lower().strip()
    if side_l.startswith("buy"):
        return float(qty)
    if side_l.startswith("sell"):
        return -float(qty)
    return 0.0


def _build_position_reconciliation(
    *,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    fill_rows: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fill_net_qty: dict[str, float] = defaultdict(float)
    fill_abs_qty: dict[str, float] = defaultdict(float)
    fill_count: dict[str, int] = defaultdict(int)
    fill_notional: dict[str, float] = defaultdict(float)
    for fill in fill_rows:
        symbol = str(fill.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        side = str(fill.get("side") or "").lower()
        qty = _safe_float(fill.get("qty"))
        price = _safe_float(fill.get("price"))
        signed = _signed_trade_qty(side, qty)
        fill_net_qty[symbol] += signed
        fill_abs_qty[symbol] += abs(qty)
        fill_count[symbol] += 1
        fill_notional[symbol] += abs(qty * price)

    planned_net_qty: dict[str, float] = defaultdict(float)
    planned_abs_qty: dict[str, float] = defaultdict(float)
    planned_notional: dict[str, float] = defaultdict(float)
    for order in order_rows:
        symbol = str(order.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        side = str(order.get("side") or "").lower()
        qty = _safe_float(order.get("planned_qty"))
        signed = _signed_trade_qty(side, qty)
        planned_net_qty[symbol] += signed
        planned_abs_qty[symbol] += abs(qty)
        planned_notional[symbol] += abs(_safe_float(order.get("planned_delta_notional")))

    rows: list[dict[str, Any]] = []
    for symbol in sorted(set(before) | set(after) | set(fill_net_qty) | set(planned_net_qty)):
        before_row = before.get(symbol, {})
        after_row = after.get(symbol, {})
        before_qty = _signed_position_qty(before_row)
        after_qty = _signed_position_qty(after_row)
        expected_after_qty = before_qty + fill_net_qty.get(symbol, 0.0)
        unexplained_qty = after_qty - expected_after_qty
        price = _safe_float(after_row.get("current_price")) or _safe_float(before_row.get("current_price"))
        material = abs(unexplained_qty) > max(0.01, 0.001 * max(abs(before_qty), abs(after_qty), abs(fill_abs_qty.get(symbol, 0.0)), 1.0))
        observed_delta = after_qty - before_qty
        fill_qty_abs = fill_abs_qty.get(symbol, 0.0)
        if not material:
            reason_hint = "reconciled"
        elif fill_qty_abs <= 1e-12 and abs(observed_delta) > 1e-12:
            reason_hint = "position_changed_without_captured_fill"
        elif abs(fill_net_qty.get(symbol, 0.0)) <= 1e-12 and fill_qty_abs > 0:
            reason_hint = "fills_captured_but_side_not_directional"
        else:
            reason_hint = "captured_fills_do_not_match_position_delta"
        rows.append(
            {
                "symbol": symbol,
                "before_signed_qty": before_qty,
                "after_signed_qty": after_qty,
                "observed_delta_qty": observed_delta,
                "fill_net_signed_qty": fill_net_qty.get(symbol, 0.0),
                "expected_after_qty_from_fills": expected_after_qty,
                "unexplained_qty": unexplained_qty,
                "unexplained_abs_qty": abs(unexplained_qty),
                "unexplained_notional_at_snapshot_price": abs(unexplained_qty * price),
                "snapshot_price_used": price,
                "fill_abs_qty": fill_abs_qty.get(symbol, 0.0),
                "fill_count": fill_count.get(symbol, 0),
                "fill_notional_abs": fill_notional.get(symbol, 0.0),
                "planned_net_signed_qty": planned_net_qty.get(symbol, 0.0),
                "planned_abs_qty": planned_abs_qty.get(symbol, 0.0),
                "planned_notional_abs": planned_notional.get(symbol, 0.0),
                "material_unexplained_qty": bool(material),
                "residual_reason_hint": reason_hint,
                "before_side": before_row.get("side", ""),
                "after_side": after_row.get("side", ""),
            }
        )

    material_rows = [row for row in rows if row.get("material_unexplained_qty")]
    reason_counts = Counter(str(row.get("residual_reason_hint") or "") for row in rows)
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol_count": len(rows),
        "symbols_with_fills": sum(1 for row in rows if _safe_float(row.get("fill_abs_qty")) > 0),
        "symbols_with_planned_orders": sum(1 for row in rows if _safe_float(row.get("planned_abs_qty")) > 0),
        "symbols_with_position_change": sum(1 for row in rows if abs(_safe_float(row.get("observed_delta_qty"))) > 0),
        "symbols_with_material_unexplained_qty": len(material_rows),
        "material_unexplained_by_reason": dict(
            Counter(str(row.get("residual_reason_hint") or "") for row in material_rows)
        ),
        "all_rows_by_reason": dict(sorted(reason_counts.items())),
        "total_unexplained_abs_qty": sum(_safe_float(row.get("unexplained_abs_qty")) for row in rows),
        "total_unexplained_notional_at_snapshot_price": sum(
            _safe_float(row.get("unexplained_notional_at_snapshot_price")) for row in rows
        ),
        "largest_unexplained": sorted(
            material_rows,
            key=lambda row: _safe_float(row.get("unexplained_notional_at_snapshot_price")),
            reverse=True,
        )[:25],
        "note": (
            "Reconciles signed broker position qty before + signed fill qty to broker position qty after. "
            "Small residuals can occur from fractional rounding, snapshot timing, or broker-side adjustments."
        ),
    }
    return rows, summary


def _parse_raw_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _infer_decision_dir(run_dir: Path) -> Path | None:
    name = run_dir.name
    if name.endswith("_execute"):
        candidate = run_dir.with_name(name[: -len("_execute")] + "_decision")
        if candidate.exists():
            return candidate
    return None


def _find_alpha_path(run_dir: Path, decision_dir: Path | None, summary: dict[str, Any]) -> Path | None:
    outputs = summary.get("outputs", {}) if isinstance(summary, dict) else {}
    for raw in [outputs.get("alpha_panel_csv") if isinstance(outputs, dict) else None]:
        if raw:
            p = Path(str(raw))
            if p.exists():
                return p
    search_dirs = [p for p in [decision_dir, run_dir] if p]
    for base in search_dirs:
        matches = sorted(base.glob("alpha_core_panel_*.csv"))
        if matches:
            return matches[-1]
    return None


def _load_alpha_by_symbol(alpha_path: Path | None) -> dict[str, dict[str, Any]]:
    if not alpha_path or not alpha_path.exists():
        return {}
    rows = _read_csv_rows(alpha_path)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if sym:
            out[sym] = row
    return out


def _load_targets(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in _read_csv_rows(path):
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out[sym] = row
    return out


def _position_maps(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("symbol") or "").upper().strip(): r for r in rows if str(r.get("symbol") or "").strip()}


def _extract_intraday_pl(row: dict[str, Any]) -> tuple[float, float, float]:
    raw = _parse_raw_dict(row.get("raw"))
    return (
        _safe_float(raw.get("unrealized_intraday_pl")),
        _safe_float(raw.get("unrealized_pl")),
        _safe_float(raw.get("change_today")),
    )


def _build_decision_trace(
    targets: dict[str, dict[str, Any]],
    alpha: dict[str, dict[str, Any]],
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    lot_weights: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    symbols = sorted(set(targets) | set(before) | set(after) | set(lot_weights))
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        target = targets.get(sym, {})
        a = alpha.get(sym, {})
        b = before.get(sym, {})
        aft = after.get(sym, {})
        before_mv = _safe_float(b.get("market_value"))
        after_mv = _safe_float(aft.get("market_value"))
        intraday_pl, unrealized_pl, change_today = _extract_intraday_pl(aft)
        row: dict[str, Any] = {
            "symbol": sym,
            "target_signed_weight": _safe_float(target.get("signed_weight")),
            "target_side": target.get("side") or ("long" if _safe_float(target.get("signed_weight")) > 0 else "short" if _safe_float(target.get("signed_weight")) < 0 else "flat"),
            "before_side": b.get("side", ""),
            "after_side": aft.get("side", ""),
            "before_qty": _safe_float(b.get("qty")),
            "after_qty": _safe_float(aft.get("qty")),
            "before_market_value": before_mv,
            "after_market_value": after_mv,
            "delta_market_value": after_mv - before_mv,
            "current_price": _safe_float(aft.get("current_price")),
            "avg_entry_price": _safe_float(aft.get("avg_entry_price")),
            "unrealized_intraday_pl_snapshot": intraday_pl,
            "unrealized_pl_since_entry": unrealized_pl,
            "change_today": change_today,
            "sic2_sector": a.get("sic2_sector", ""),
            "beta": _safe_float(a.get("beta")),
            "composite_score": _safe_float(a.get("composite_score")),
            "composite_rank": _safe_int(a.get("composite_rank")),
            "lot_total_weight": sum(lot_weights.get(sym, {}).values()),
        }
        for col in FACTOR_COLUMNS:
            row[col] = _safe_float(a.get(col))
            row[f"lot_weight_{col}"] = _safe_float(lot_weights.get(sym, {}).get(col))
        rows.append(row)
    return rows


def _build_lot_trace(lot_snapshot: dict[str, Any], session_idx: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    ledger = lot_snapshot.get("ledger", {}) if isinstance(lot_snapshot, dict) else {}
    rows: list[dict[str, Any]] = []
    lot_weights: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for side in ["long", "short"]:
        for lot in ledger.get(side, []) if isinstance(ledger, dict) else []:
            sym = str(lot.get("symbol") or "").upper().strip()
            factor = str(lot.get("factor") or "")
            weight = _safe_float(lot.get("weight"))
            birth_idx = _safe_int(lot.get("birth_idx"))
            min_hold = _safe_int(lot.get("min_hold"))
            age = max(0, session_idx - birth_idx)
            locked = age < min_hold
            if sym and factor:
                lot_weights[sym][factor] += weight
            rows.append(
                {
                    "side": side,
                    "symbol": sym,
                    "factor": factor,
                    "weight": weight,
                    "birth_idx": birth_idx,
                    "session_idx": session_idx,
                    "age_sessions": age,
                    "min_hold": min_hold,
                    "locked": locked,
                    "remaining_lock_sessions": max(0, min_hold - age),
                    "entry_session_date": lot.get("entry_session_date", ""),
                    "entry_time_utc": lot.get("entry_time_utc", ""),
                }
            )
    return rows, {s: dict(v) for s, v in lot_weights.items()}


def _build_order_trace(
    plan: dict[str, Any],
    records: list[dict[str, Any]],
    quality: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    per_order_q = {}
    for item in quality.get("per_order", []) if isinstance(quality, dict) else []:
        if isinstance(item, dict):
            key = (str(item.get("symbol") or "").upper(), str(item.get("side") or "").lower(), round(_safe_float(item.get("planned_notional") or item.get("delta_notional")), 4))
            per_order_q[key] = item
    records_by_symbol = defaultdict(list)
    for rec in records if isinstance(records, list) else []:
        records_by_symbol[str(rec.get("symbol") or "").upper()].append(rec)
    staged = summary.get("staged_diagnostics", {}) if isinstance(summary, dict) else {}
    release_unfilled_symbols = {
        str(symbol or "").upper()
        for symbol in staged.get("release_unfilled_symbols", [])
        if str(symbol or "").strip()
    } if isinstance(staged, dict) else set()
    release_unfilled_stage = str(staged.get("release_unfilled_stage") or "") if isinstance(staged, dict) else ""
    entry_abort_reason = str(staged.get("entry_abort_reason") or "") if isinstance(staged, dict) else ""
    rows: list[dict[str, Any]] = []
    for order in plan.get("orders", []) if isinstance(plan, dict) else []:
        sym = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        delta_notional = _safe_float(order.get("delta_notional"))
        planned_qty = _safe_float(order.get("qty"))
        planned_stage = _execution_stage_for_order(order)
        candidates = [
            r
            for r in records_by_symbol.get(sym, [])
            if str(r.get("side") or "").lower() == side
            and (not planned_stage or str(r.get("stage") or "") == planned_stage)
        ]
        if not candidates:
            candidates = [r for r in records_by_symbol.get(sym, []) if str(r.get("side") or "").lower() == side]
        rec = _aggregate_order_records_for_plan(order=order, records=candidates)
        qkey = (sym, side, round(delta_notional, 4))
        q = per_order_q.get(qkey, {})
        error_payload = _broker_error_payload(rec)
        status_latest = rec.get("status_latest", "not_submitted")
        submit_error_class = rec.get("submit_error_class") or _submit_error_class_from_payload(error_payload, rec.get("error"))
        not_submitted_reason = ""
        if str(status_latest or "").lower() == "not_submitted":
            if sym in release_unfilled_symbols:
                not_submitted_reason = f"{release_unfilled_stage}_not_reached_or_unfilled"
            elif entry_abort_reason:
                not_submitted_reason = f"entry_aborted:{entry_abort_reason}"
            else:
                not_submitted_reason = "no_execution_record"
        elif str(status_latest or "").lower() == "submit_error":
            not_submitted_reason = f"submit_error:{submit_error_class or 'unknown'}"
        rows.append(
            {
                "symbol": sym,
                "side": side,
                "stage": rec.get("stage", ""),
                "planned_qty": planned_qty,
                "planned_delta_notional": delta_notional,
                "reference_price": _safe_float(order.get("reference_price")),
                "sizing_price": _safe_float(order.get("sizing_price")),
                "target_notional": _safe_float(order.get("target_notional")),
                "current_notional": _safe_float(order.get("current_notional")),
                "opening_short": bool(order.get("opening_short")),
                "client_order_id": rec.get("client_order_id", ""),
                "order_id": rec.get("order_id", ""),
                "status_latest": status_latest,
                "not_submitted_reason": not_submitted_reason,
                "filled_qty": _safe_float(rec.get("filled_qty")),
                "remaining_qty": _safe_float(rec.get("remaining_qty")),
                "filled_avg_price": _safe_float(rec.get("filled_avg_price")),
                "attempt_count": _safe_int(rec.get("attempt_count")),
                "submitted_at_utc": rec.get("submitted_at_utc", ""),
                "updated_at": rec.get("updated_at", ""),
                "slippage_bps": q.get("slippage_bps"),
                "filled_notional": q.get("filled_notional"),
                "requested_qty": _safe_float(rec.get("requested_qty") or rec.get("qty")),
                "submit_error_class": submit_error_class,
                "broker_error_code": rec.get("broker_error_code") or error_payload.get("code") or "",
                "broker_error_message": rec.get("broker_error_message") or error_payload.get("message") or "",
                "broker_available_qty": _optional_float(rec.get("broker_available_qty") or error_payload.get("available")),
                "broker_existing_qty": _optional_float(rec.get("broker_existing_qty") or error_payload.get("existing_qty")),
                "broker_held_for_orders_qty": _optional_float(
                    rec.get("broker_held_for_orders_qty") or error_payload.get("held_for_orders")
                ),
                "abort_remaining_orders": bool(rec.get("abort_remaining_orders")),
                "error_type": rec.get("error_type", ""),
                "error": rec.get("error", ""),
            }
        )
    return rows


def _execution_stage_for_order(order: dict[str, Any]) -> str:
    side = str(order.get("side") or "").lower()
    action_class = _action_class_for_order(
        side,
        _safe_float(order.get("current_notional")),
        _safe_float(order.get("target_notional")),
    )
    if action_class in {"release_sell_long", "release_buy_to_cover"}:
        return action_class
    return "entry"


def _aggregate_order_records_for_plan(*, order: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    planned_qty = _safe_float(order.get("qty"))
    if planned_qty <= 0:
        planned_qty = max((_safe_float(record.get("qty")) for record in records), default=0.0)
    records_sorted = sorted(
        records,
        key=lambda record: (
            _safe_int(record.get("release_round")),
            _safe_int(record.get("attempt_count")),
            str(record.get("updated_at") or record.get("submitted_at_utc") or ""),
        ),
    )
    total_filled_qty = sum(_safe_float(record.get("filled_qty")) for record in records_sorted)
    remaining_qty = max(0.0, planned_qty - total_filled_qty)
    filled_records = [record for record in records_sorted if _safe_float(record.get("filled_qty")) > 0]
    error_records = [record for record in records_sorted if str(record.get("status_latest") or "").lower() == "submit_error"]
    latest_record = records_sorted[-1]
    status_latest = str(latest_record.get("status_latest") or "")
    if planned_qty > 0 and remaining_qty <= max(1e-6, planned_qty * 1e-6):
        status_latest = "filled"
        remaining_qty = 0.0
    elif total_filled_qty > 0:
        status_latest = "partial_fill"
    elif error_records:
        status_latest = "submit_error"
        latest_record = error_records[-1]
    reference_price = _safe_float(latest_record.get("reference_price") or order.get("reference_price"))
    filled_notional = sum(
        _safe_float(record.get("filled_qty"))
        * (
            _safe_float(record.get("filled_avg_price"))
            if _safe_float(record.get("filled_avg_price")) > 0
            else _safe_float(record.get("reference_price") or reference_price)
        )
        for record in records_sorted
    )
    filled_avg_price = filled_notional / total_filled_qty if total_filled_qty > 0 else _safe_float(latest_record.get("filled_avg_price"))
    chosen_record = filled_records[-1] if filled_records else latest_record
    error_payload = _broker_error_payload(latest_record)
    return {
        **latest_record,
        "stage": latest_record.get("stage") or _execution_stage_for_order(order),
        "client_order_id": chosen_record.get("client_order_id", latest_record.get("client_order_id", "")),
        "order_id": chosen_record.get("order_id", latest_record.get("order_id", "")),
        "status_latest": status_latest,
        "filled_qty": total_filled_qty,
        "remaining_qty": remaining_qty,
        "filled_avg_price": filled_avg_price,
        "attempt_count": sum(_safe_int(record.get("attempt_count")) for record in records_sorted),
        "submitted_at_utc": next((record.get("submitted_at_utc", "") for record in records_sorted if record.get("submitted_at_utc")), ""),
        "updated_at": latest_record.get("updated_at", ""),
        "requested_qty": planned_qty,
        "qty": planned_qty,
        "delta_notional": _safe_float(order.get("delta_notional")),
        "reference_price": reference_price,
        "submit_error_class": latest_record.get("submit_error_class")
        or _submit_error_class_from_payload(error_payload, latest_record.get("error")),
        "broker_error_code": latest_record.get("broker_error_code") or error_payload.get("code") or "",
        "broker_error_message": latest_record.get("broker_error_message") or error_payload.get("message") or "",
        "broker_available_qty": latest_record.get("broker_available_qty") or error_payload.get("available"),
        "broker_existing_qty": latest_record.get("broker_existing_qty") or error_payload.get("existing_qty"),
        "broker_held_for_orders_qty": latest_record.get("broker_held_for_orders_qty") or error_payload.get("held_for_orders"),
        "abort_remaining_orders": any(bool(record.get("abort_remaining_orders")) for record in records_sorted),
        "error_type": latest_record.get("error_type", ""),
        "error": latest_record.get("error", ""),
    }


def _build_fill_trace(records: list[dict[str, Any]], broker_fills: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a fill-level trace.

    Prefer raw broker FILL activities when the live executor has captured them.
    Historical runs are backfilled from execution_records/attempts, which is
    less granular and is labeled via source=execution_record_attempt.
    """
    rows: list[dict[str, Any]] = []
    matched = broker_fills.get("matched_activities", []) if isinstance(broker_fills, dict) else []
    if matched:
        for idx, fill in enumerate(matched, start=1):
            if not isinstance(fill, dict):
                continue
            rows.append(
                {
                    "source": "broker_fill_activity",
                    "fill_seq": idx,
                    "symbol": str(fill.get("symbol") or "").upper(),
                    "side": str(fill.get("side") or "").lower(),
                    "order_id": fill.get("order_id", ""),
                    "client_order_id": fill.get("client_order_id", ""),
                    "transaction_time": fill.get("transaction_time") or fill.get("date") or "",
                    "qty": _safe_float(fill.get("qty")),
                    "price": _safe_float(fill.get("price")),
                    "gross_amount": _safe_float(fill.get("gross_amount")),
                    "net_amount": _safe_float(fill.get("net_amount")),
                    "raw_activity_id": fill.get("id", ""),
                }
            )
        return rows

    seq = 0
    for rec in records if isinstance(records, list) else []:
        attempts = rec.get("attempts")
        if isinstance(attempts, list) and attempts:
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                qty = _safe_float(attempt.get("filled_qty"))
                if qty <= 0:
                    continue
                seq += 1
                rows.append(
                    {
                        "source": "execution_record_attempt",
                        "fill_seq": seq,
                        "symbol": str(rec.get("symbol") or "").upper(),
                        "side": str(rec.get("side") or "").lower(),
                        "order_id": attempt.get("order_id", ""),
                        "client_order_id": attempt.get("client_order_id", ""),
                        "transaction_time": attempt.get("updated_at", ""),
                        "qty": qty,
                        "price": _safe_float(attempt.get("filled_avg_price")),
                        "gross_amount": qty * _safe_float(attempt.get("filled_avg_price")),
                        "net_amount": "",
                        "raw_activity_id": "",
                    }
                )
        else:
            qty = _safe_float(rec.get("filled_qty"))
            if qty <= 0:
                continue
            seq += 1
            rows.append(
                {
                    "source": "execution_record",
                    "fill_seq": seq,
                    "symbol": str(rec.get("symbol") or "").upper(),
                    "side": str(rec.get("side") or "").lower(),
                    "order_id": rec.get("order_id", ""),
                    "client_order_id": rec.get("client_order_id", ""),
                    "transaction_time": rec.get("updated_at", ""),
                    "qty": qty,
                    "price": _safe_float(rec.get("filled_avg_price")),
                    "gross_amount": qty * _safe_float(rec.get("filled_avg_price")),
                    "net_amount": "",
                    "raw_activity_id": "",
                }
            )
    return rows


def _build_position_pnl(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sym in sorted(set(before) | set(after)):
        b = before.get(sym, {})
        a = after.get(sym, {})
        intraday_pl, unrealized_pl, change_today = _extract_intraday_pl(a)
        rows.append(
            {
                "symbol": sym,
                "side": a.get("side") or b.get("side") or "",
                "qty_before": _safe_float(b.get("qty")),
                "qty_after": _safe_float(a.get("qty")),
                "market_value_before": _safe_float(b.get("market_value")),
                "market_value_after": _safe_float(a.get("market_value")),
                "delta_market_value": _safe_float(a.get("market_value")) - _safe_float(b.get("market_value")),
                "current_price_before": _safe_float(b.get("current_price")),
                "current_price_after": _safe_float(a.get("current_price")),
                "unrealized_intraday_pl_snapshot": intraday_pl,
                "unrealized_pl_since_entry": unrealized_pl,
                "change_today": change_today,
            }
        )
    return rows


def _build_factor_attribution(
    decision_rows: list[dict[str, Any]],
    lot_weights: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """Approximate factor-level PnL by splitting symbol snapshot PnL by lot weights."""
    rows: list[dict[str, Any]] = []
    by_factor: dict[str, dict[str, Any]] = defaultdict(lambda: {"factor": "", "lot_weight": 0.0, "approx_intraday_pnl": 0.0, "symbols": set()})
    by_symbol = {str(row.get("symbol") or "").upper(): row for row in decision_rows}
    for symbol, weights in lot_weights.items():
        row = by_symbol.get(symbol, {})
        pnl = _safe_float(row.get("unrealized_intraday_pl_snapshot"))
        total = sum(_safe_float(v) for v in weights.values())
        if total <= 0:
            continue
        for factor, weight in weights.items():
            share = _safe_float(weight) / total if total else 0.0
            approx = pnl * share
            bucket = by_factor[factor]
            bucket["factor"] = factor
            bucket["lot_weight"] += _safe_float(weight)
            bucket["approx_intraday_pnl"] += approx
            bucket["symbols"].add(symbol)
            rows.append(
                {
                    "level": "symbol_factor",
                    "factor": factor,
                    "symbol": symbol,
                    "symbol_side": row.get("after_side") or row.get("target_side") or "",
                    "symbol_intraday_pnl_snapshot": pnl,
                    "symbol_lot_total_weight": total,
                    "factor_lot_weight": _safe_float(weight),
                    "factor_weight_share_in_symbol": share,
                    "approx_intraday_pnl": approx,
                    "note": "snapshot approximation: symbol unrealized_intraday_pl allocated by factor-lot weight share",
                }
            )
    summary_rows = []
    for factor, bucket in by_factor.items():
        summary_rows.append(
            {
                "level": "factor_total",
                "factor": factor,
                "symbol": "__TOTAL__",
                "symbol_side": "",
                "symbol_intraday_pnl_snapshot": "",
                "symbol_lot_total_weight": "",
                "factor_lot_weight": bucket["lot_weight"],
                "factor_weight_share_in_symbol": "",
                "approx_intraday_pnl": bucket["approx_intraday_pnl"],
                "note": f"{len(bucket['symbols'])} symbols",
            }
        )
    return sorted(summary_rows, key=lambda r: _safe_float(r.get("approx_intraday_pnl"))) + rows


def _find_pre_trade_lot_snapshot(
    run_dir: Path,
    decision_dir: Path | None,
    session_key: str,
) -> tuple[dict[str, Any], Path | None, str]:
    candidates: list[tuple[Path, str]] = [
        (run_dir / f"lot_snapshot_before_execution_{session_key}.json", "execute_pre_execution"),
        (run_dir / f"lot_snapshot_before_decision_{session_key}.json", "run_pre_decision"),
    ]
    if decision_dir:
        candidates.extend(
            [
                (decision_dir / f"lot_snapshot_before_decision_{session_key}.json", "decision_pre_decision"),
                (decision_dir / f"lot_snapshot_{session_key}.json", "decision_post_decision_fallback"),
            ]
        )
    candidates.append((run_dir / f"lot_snapshot_{session_key}.json", "execute_post_execution_fallback"))
    for path, source in candidates:
        if path.exists():
            return _read_json(path, {}), path, source
    return {}, None, "missing"


def _order_lookup(records: list[dict[str, Any]], order_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in order_rows:
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            lookup[order_id] = row
    for rec in records if isinstance(records, list) else []:
        base = {
            "symbol": rec.get("symbol", ""),
            "side": rec.get("side", ""),
            "stage": rec.get("stage", ""),
            "reference_price": rec.get("reference_price", ""),
            "planned_delta_notional": rec.get("delta_notional", ""),
            "client_order_id": rec.get("client_order_id", ""),
        }
        order_id = str(rec.get("order_id") or "").strip()
        if order_id and order_id not in lookup:
            lookup[order_id] = dict(base, order_id=order_id)
        attempts = rec.get("attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                attempt_order_id = str(attempt.get("order_id") or "").strip()
                if not attempt_order_id or attempt_order_id in lookup:
                    continue
                lookup[attempt_order_id] = dict(
                    base,
                    order_id=attempt_order_id,
                    client_order_id=attempt.get("client_order_id", base.get("client_order_id", "")),
                    reference_price=base.get("reference_price", ""),
                )
    return lookup


def _pre_trade_lot_states(
    lot_snapshot: dict[str, Any],
    before_positions: dict[str, dict[str, Any]],
    session_idx: int,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    ledger = lot_snapshot.get("ledger", {}) if isinstance(lot_snapshot, dict) else {}
    states: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    ordinals: Counter[tuple[str, str, str, int]] = Counter()
    for side in ("long", "short"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for lot in ledger.get(side, []) if isinstance(ledger, dict) else []:
            if not isinstance(lot, dict):
                continue
            sym = str(lot.get("symbol") or "").upper().strip()
            if not sym:
                continue
            grouped[sym].append(lot)
        for sym, lots in grouped.items():
            pos = before_positions.get(sym, {})
            pos_side = str(pos.get("side") or "").lower()
            pos_qty = _safe_float(pos.get("qty"))
            if pos_side != side or pos_qty <= 0:
                continue
            total_weight = sum(_safe_float(lot.get("weight")) for lot in lots)
            if total_weight <= 0:
                continue
            for lot in lots:
                factor = str(lot.get("factor") or "unknown")
                birth_idx = _safe_int(lot.get("birth_idx"))
                min_hold = _safe_int(lot.get("min_hold"))
                key = (side, sym, factor, birth_idx)
                ordinals[key] += 1
                lot_weight = _safe_float(lot.get("weight"))
                remaining_qty = pos_qty * lot_weight / total_weight
                states[(side, sym)].append(
                    {
                        "lot_id": f"{side}:{sym}:{factor}:{birth_idx}:{ordinals[key]}",
                        "side": side,
                        "symbol": sym,
                        "factor": factor,
                        "weight": lot_weight,
                        "remaining_qty": remaining_qty,
                        "birth_idx": birth_idx,
                        "min_hold": min_hold,
                        "locked": int(session_idx) - birth_idx < min_hold,
                        "entry_session_date": lot.get("entry_session_date", ""),
                        "entry_time_utc": lot.get("entry_time_utc", ""),
                    }
                )
            states[(side, sym)].sort(
                key=lambda item: (
                    bool(item.get("locked")),
                    -_safe_int(item.get("birth_idx")),
                    -_safe_float(item.get("weight")),
                    str(item.get("factor") or ""),
                )
            )
    return states


def _signed_slippage_bps(side: str, reference_price: float, fill_price: float) -> float | None:
    if reference_price <= 0 or fill_price <= 0:
        return None
    if str(side).lower() == "buy":
        return (fill_price - reference_price) / reference_price * 10_000.0
    return (reference_price - fill_price) / reference_price * 10_000.0


def _build_realized_pnl_ledger(
    *,
    fill_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
    order_rows: list[dict[str, Any]],
    before_positions: dict[str, dict[str, Any]],
    pre_trade_lot_snapshot: dict[str, Any],
    session_date: str,
    session_idx: int,
    lot_source: str,
) -> list[dict[str, Any]]:
    lot_states = _pre_trade_lot_states(pre_trade_lot_snapshot, before_positions, session_idx)
    remaining_position_qty: dict[tuple[str, str], float] = {}
    for sym, pos in before_positions.items():
        side = str(pos.get("side") or "").lower()
        if side in ("long", "short"):
            remaining_position_qty[(side, sym)] = _safe_float(pos.get("qty"))

    order_by_id = _order_lookup(records, order_rows)
    sorted_fills = sorted(
        fill_rows,
        key=lambda row: (str(row.get("transaction_time") or ""), _safe_int(row.get("fill_seq")), str(row.get("raw_activity_id") or "")),
    )
    ledger_rows: list[dict[str, Any]] = []
    seq = 0
    for fill in sorted_fills:
        symbol = str(fill.get("symbol") or "").upper().strip()
        fill_side = str(fill.get("side") or "").lower().strip()
        fill_qty = _safe_float(fill.get("qty"))
        fill_price = _safe_float(fill.get("price"))
        if not symbol or fill_qty <= 0 or fill_price <= 0 or not (fill_side.startswith("buy") or fill_side.startswith("sell")):
            continue
        closing_side = "short" if fill_side.startswith("buy") else "long" if fill_side == "sell" else ""
        pos = before_positions.get(symbol, {})
        avg_entry = _safe_float(pos.get("avg_entry_price"))
        available = remaining_position_qty.get((closing_side, symbol), 0.0) if closing_side else 0.0
        close_qty = min(fill_qty, max(0.0, available))
        opening_qty = max(0.0, fill_qty - close_qty)
        order_id = str(fill.get("order_id") or "").strip()
        order = order_by_id.get(order_id, {})
        reference_price = _safe_float(order.get("reference_price"))
        slippage_bps = _signed_slippage_bps(fill_side, reference_price, fill_price)

        if closing_side and close_qty > 1e-9 and avg_entry > 0:
            remaining_position_qty[(closing_side, symbol)] = max(0.0, available - close_qty)
            remaining_to_allocate = close_qty
            lots = lot_states.get((closing_side, symbol), [])
            for lot in lots:
                if remaining_to_allocate <= 1e-9:
                    break
                lot_remaining = _safe_float(lot.get("remaining_qty"))
                if lot_remaining <= 1e-9:
                    continue
                qty = min(lot_remaining, remaining_to_allocate)
                lot["remaining_qty"] = max(0.0, lot_remaining - qty)
                remaining_to_allocate -= qty
                pnl_per_share = (fill_price - avg_entry) if closing_side == "long" else (avg_entry - fill_price)
                seq += 1
                ledger_rows.append(
                    {
                        "ledger_seq": seq,
                        "session_date": session_date,
                        "session_idx": session_idx,
                        "symbol": symbol,
                        "fill_side": fill_side,
                        "closed_position_side": closing_side,
                        "action": "close",
                        "fill_id": fill.get("raw_activity_id", ""),
                        "order_id": order_id,
                        "client_order_id": fill.get("client_order_id") or order.get("client_order_id", ""),
                        "stage": order.get("stage", ""),
                        "transaction_time": fill.get("transaction_time", ""),
                        "fill_qty": fill_qty,
                        "closed_qty": qty,
                        "opening_qty": 0.0,
                        "fill_price": fill_price,
                        "avg_entry_price_before": avg_entry,
                        "cost_basis_source": "broker_avg_entry_price_before",
                        "realized_pnl": pnl_per_share * qty,
                        "pnl_per_share": pnl_per_share,
                        "gross_exit_notional": qty * fill_price,
                        "reference_price": reference_price,
                        "slippage_bps": slippage_bps,
                        "lot_id": lot.get("lot_id", ""),
                        "factor": lot.get("factor", ""),
                        "lot_weight": lot.get("weight", ""),
                        "lot_birth_idx": lot.get("birth_idx", ""),
                        "lot_min_hold": lot.get("min_hold", ""),
                        "lot_locked_at_close": lot.get("locked", ""),
                        "lot_entry_session_date": lot.get("entry_session_date", ""),
                        "lot_entry_time_utc": lot.get("entry_time_utc", ""),
                        "pre_trade_lot_source": lot_source,
                        "strictness": "fill_level_with_broker_avg_cost_and_strategy_lot_allocation",
                    }
                )
            if remaining_to_allocate > 1e-6:
                pnl_per_share = (fill_price - avg_entry) if closing_side == "long" else (avg_entry - fill_price)
                seq += 1
                ledger_rows.append(
                    {
                        "ledger_seq": seq,
                        "session_date": session_date,
                        "session_idx": session_idx,
                        "symbol": symbol,
                        "fill_side": fill_side,
                        "closed_position_side": closing_side,
                        "action": "close_unattributed",
                        "fill_id": fill.get("raw_activity_id", ""),
                        "order_id": order_id,
                        "client_order_id": fill.get("client_order_id") or order.get("client_order_id", ""),
                        "stage": order.get("stage", ""),
                        "transaction_time": fill.get("transaction_time", ""),
                        "fill_qty": fill_qty,
                        "closed_qty": remaining_to_allocate,
                        "opening_qty": 0.0,
                        "fill_price": fill_price,
                        "avg_entry_price_before": avg_entry,
                        "cost_basis_source": "broker_avg_entry_price_before",
                        "realized_pnl": pnl_per_share * remaining_to_allocate,
                        "pnl_per_share": pnl_per_share,
                        "gross_exit_notional": remaining_to_allocate * fill_price,
                        "reference_price": reference_price,
                        "slippage_bps": slippage_bps,
                        "lot_id": "",
                        "factor": "unattributed_pre_trade_lot",
                        "lot_weight": "",
                        "lot_birth_idx": "",
                        "lot_min_hold": "",
                        "lot_locked_at_close": "",
                        "lot_entry_session_date": "",
                        "lot_entry_time_utc": "",
                        "pre_trade_lot_source": lot_source,
                        "strictness": "fill_level_with_broker_avg_cost_unattributed_lot",
                    }
                )
        if opening_qty > 1e-9:
            seq += 1
            ledger_rows.append(
                {
                    "ledger_seq": seq,
                    "session_date": session_date,
                    "session_idx": session_idx,
                    "symbol": symbol,
                    "fill_side": fill_side,
                    "closed_position_side": "",
                    "action": "open_or_increase",
                    "fill_id": fill.get("raw_activity_id", ""),
                    "order_id": order_id,
                    "client_order_id": fill.get("client_order_id") or order.get("client_order_id", ""),
                    "stage": order.get("stage", ""),
                    "transaction_time": fill.get("transaction_time", ""),
                    "fill_qty": fill_qty,
                    "closed_qty": 0.0,
                    "opening_qty": opening_qty,
                    "fill_price": fill_price,
                    "avg_entry_price_before": "",
                    "cost_basis_source": "",
                    "realized_pnl": 0.0,
                    "pnl_per_share": "",
                    "gross_exit_notional": 0.0,
                    "reference_price": reference_price,
                    "slippage_bps": slippage_bps,
                    "lot_id": "",
                    "factor": "new_position_not_realized",
                    "lot_weight": "",
                    "lot_birth_idx": "",
                    "lot_min_hold": "",
                    "lot_locked_at_close": "",
                    "lot_entry_session_date": "",
                    "lot_entry_time_utc": "",
                    "pre_trade_lot_source": lot_source,
                    "strictness": "opening_fill_no_realized_pnl_yet",
                }
            )
    return ledger_rows


def _group_realized(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row.get(k, "") for k in keys)
        bucket = grouped.setdefault(
            key,
            {k: row.get(k, "") for k in keys}
            | {
                "realized_pnl": 0.0,
                "closed_qty": 0.0,
                "opening_qty": 0.0,
                "gross_exit_notional": 0.0,
                "fill_row_count": 0,
                "close_row_count": 0,
            },
        )
        bucket["realized_pnl"] += _safe_float(row.get("realized_pnl"))
        bucket["closed_qty"] += _safe_float(row.get("closed_qty"))
        bucket["opening_qty"] += _safe_float(row.get("opening_qty"))
        bucket["gross_exit_notional"] += _safe_float(row.get("gross_exit_notional"))
        bucket["fill_row_count"] += 1
        if _safe_float(row.get("closed_qty")) > 0:
            bucket["close_row_count"] += 1
    return sorted(grouped.values(), key=lambda item: _safe_float(item.get("realized_pnl")))


def _realized_summary(rows: list[dict[str, Any]], lot_source_path: Path | None, lot_source: str) -> dict[str, Any]:
    close_rows = [row for row in rows if _safe_float(row.get("closed_qty")) > 0]
    opening_rows = [row for row in rows if _safe_float(row.get("opening_qty")) > 0]
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "realized_pnl_total": _sum(close_rows, "realized_pnl"),
        "closed_qty_total": _sum(close_rows, "closed_qty"),
        "gross_exit_notional": _sum(close_rows, "gross_exit_notional"),
        "close_row_count": len(close_rows),
        "opening_row_count": len(opening_rows),
        "ledger_row_count": len(rows),
        "pre_trade_lot_source": lot_source,
        "pre_trade_lot_source_path": lot_source_path.as_posix() if lot_source_path else None,
        "cost_basis_source": "broker_positions_before.avg_entry_price",
        "strictness": (
            "Fill-level realized PnL using broker average entry cost before execution; "
            "factor ownership allocated by pre-trade strategy lot reduction policy."
        ),
    }


def _sum(rows: Iterable[dict[str, Any]], key: str) -> float:
    return sum(_safe_float(r.get(key)) for r in rows)


def _top(rows: list[dict[str, Any]], key: str, n: int = 8, reverse: bool = True) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: _safe_float(r.get(key)), reverse=reverse)[:n]


def _risk_snapshot(summary: dict[str, Any], quality: dict[str, Any], decision_diag: dict[str, Any], decision_rows: list[dict[str, Any]], lot_rows: list[dict[str, Any]]) -> dict[str, Any]:
    long_mv = sum(max(0.0, _safe_float(r.get("after_market_value"))) for r in decision_rows)
    short_mv_abs = sum(abs(min(0.0, _safe_float(r.get("after_market_value")))) for r in decision_rows)
    equity = _safe_float(summary.get("account_equity_post_trade") or summary.get("account_equity"), 0.0)
    side_pnl = defaultdict(float)
    sector_pnl = defaultdict(float)
    factor_weight = defaultdict(float)
    for r in decision_rows:
        side = r.get("after_side") or r.get("target_side") or "unknown"
        side_pnl[str(side)] += _safe_float(r.get("unrealized_intraday_pl_snapshot"))
        sector_pnl[str(r.get("sic2_sector") or "unknown")] += _safe_float(r.get("unrealized_intraday_pl_snapshot"))
    for r in lot_rows:
        factor_weight[str(r.get("factor") or "unknown")] += _safe_float(r.get("weight"))
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": summary.get("decision_date") or quality.get("session_date"),
        "session_idx": summary.get("session_idx"),
        "account_equity_before": summary.get("account_equity"),
        "account_equity_after": summary.get("account_equity_post_trade"),
        "equity_change_during_run": _safe_float(summary.get("account_equity_post_trade")) - _safe_float(summary.get("account_equity")),
        "gross_long_market_value_after": long_mv,
        "gross_short_market_value_abs_after": short_mv_abs,
        "gross_exposure_after": long_mv + short_mv_abs,
        "net_exposure_after": long_mv - short_mv_abs,
        "gross_exposure_to_equity": (long_mv + short_mv_abs) / equity if equity else None,
        "net_exposure_to_equity": (long_mv - short_mv_abs) / equity if equity else None,
        "decision_diagnostics": decision_diag,
        "execution_quality": {
            "fill_rate_count": quality.get("fill_rate_count"),
            "fill_rate_notional": quality.get("fill_rate_notional"),
            "planned_notional": quality.get("planned_notional"),
            "filled_notional": quality.get("filled_notional"),
            "unfilled_notional": quality.get("unfilled_notional"),
            "slippage_bps": quality.get("slippage_bps"),
            "counts": quality.get("counts"),
        },
        "snapshot_intraday_pnl_by_side": dict(sorted(side_pnl.items())),
        "snapshot_intraday_pnl_by_sector": dict(sorted(sector_pnl.items(), key=lambda kv: kv[1])),
        "lot_weight_by_factor": dict(sorted(factor_weight.items())),
        "locked_lot_count": sum(1 for r in lot_rows if str(r.get("locked")).lower() == "true"),
        "lot_count": len(lot_rows),
    }


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for r in rows:
        vals = []
        for c in columns:
            v = r.get(c, "")
            if v is None:
                vals.append("")
            elif isinstance(v, float):
                vals.append(f"{v:,.2f}")
            else:
                vals.append(str(v))
        out.append("| " + " | ".join(vals) + " |")
    return "\n".join(out) + "\n"


def _write_review(
    path: Path,
    context: dict[str, Any],
    risk: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    realized_summary: dict[str, Any] | None = None,
    audit_checks: dict[str, Any] | None = None,
    equity_pnl_bridge: dict[str, Any] | None = None,
    account_state_bridge_summary: dict[str, Any] | None = None,
    market_context_summary: dict[str, Any] | None = None,
    attribution_dossier_summary: dict[str, Any] | None = None,
    ideal_actual_gap_summary: dict[str, Any] | None = None,
    target_transition_summary: dict[str, Any] | None = None,
    decision_intent_summary: dict[str, Any] | None = None,
    order_constraint_summary: dict[str, Any] | None = None,
    decision_execute_drift_summary: dict[str, Any] | None = None,
    market_price_evidence_summary: dict[str, Any] | None = None,
    intraday_bar_summary: dict[str, Any] | None = None,
    quote_summary: dict[str, Any] | None = None,
    account_activity_attribution_summary: dict[str, Any] | None = None,
    corporate_action_summary: dict[str, Any] | None = None,
    portfolio_history_summary: dict[str, Any] | None = None,
    calendar_summary: dict[str, Any] | None = None,
    position_snapshot_integrity: dict[str, Any] | None = None,
    residual_diagnosis: dict[str, Any] | None = None,
    evidence_completeness: dict[str, Any] | None = None,
    strict_attribution_checklist_summary: dict[str, Any] | None = None,
    execution_attribution_summary: dict[str, Any] | None = None,
) -> None:
    worst = _top(positions, "unrealized_intraday_pl_snapshot", 8, reverse=False)
    best = _top(positions, "unrealized_intraday_pl_snapshot", 8, reverse=True)
    unfilled = [r for r in orders if str(r.get("status_latest") or "").lower() != "filled"]
    checks = audit_checks or {}
    check_issues = checks.get("issues", []) if isinstance(checks, dict) else []
    bridge_amounts = (
        equity_pnl_bridge.get("component_amounts", {})
        if isinstance(equity_pnl_bridge, dict) and isinstance(equity_pnl_bridge.get("component_amounts"), dict)
        else {}
    )
    account_state = account_state_bridge_summary or {}
    market_context = market_context_summary or {}
    attribution_dossier = attribution_dossier_summary or {}
    ideal_actual_gap = ideal_actual_gap_summary or {}
    transition = target_transition_summary or {}
    decision_intent = decision_intent_summary or {}
    order_constraints = order_constraint_summary or {}
    decision_execute_drift = decision_execute_drift_summary or {}
    market_price = market_price_evidence_summary or {}
    intraday_bars = intraday_bar_summary or {}
    quotes = quote_summary or {}
    account_activity_attr = account_activity_attribution_summary or {}
    corporate_actions = corporate_action_summary or {}
    portfolio_history = portfolio_history_summary or {}
    calendar = calendar_summary or {}
    snapshot_integrity = position_snapshot_integrity or {}
    residual_diag = residual_diagnosis or {}
    evidence = evidence_completeness or {}
    strict_checklist = strict_attribution_checklist_summary or {}
    execution_attr = execution_attribution_summary or {}
    top_check_issues = [
        {
            "severity": item.get("severity", ""),
            "name": item.get("name", ""),
            "status": item.get("status", ""),
            "detail": item.get("detail", ""),
        }
        for item in (check_issues if isinstance(check_issues, list) else [])[:10]
        if isinstance(item, dict)
    ]
    lines = [
        f"# Daily Review — {context.get('session_date') or context.get('run_dir')}\n",
        "> PnL fields are broker snapshot approximations based on `unrealized_intraday_pl`; they are not a full realized PnL ledger.\n",
        "## Run Context\n",
        f"- Run dir: `{context.get('run_dir')}`\n",
        f"- Decision dir: `{context.get('decision_dir')}`\n",
        f"- Session idx: `{context.get('session_idx')}`\n",
        f"- Equity before/after: `{risk.get('account_equity_before')}` → `{risk.get('account_equity_after')}`\n",
        f"- Execute fill rate: `{risk.get('execution_quality', {}).get('fill_rate_count')}` count / `{risk.get('execution_quality', {}).get('fill_rate_notional')}` notional\n",
        f"- Avg notional-weighted slippage bps: `{(risk.get('execution_quality', {}).get('slippage_bps') or {}).get('avg_notional_weighted')}`\n",
        "\n## Exposure Snapshot\n",
        f"- Gross exposure/equity: `{risk.get('gross_exposure_to_equity')}`\n",
        f"- Net exposure/equity: `{risk.get('net_exposure_to_equity')}`\n",
        f"- Long MV: `{risk.get('gross_long_market_value_after')}`; short abs MV: `{risk.get('gross_short_market_value_abs_after')}`\n",
        "\n## Realized PnL Ledger\n",
        f"- Realized PnL total: `{(realized_summary or {}).get('realized_pnl_total')}`\n",
        f"- Closed qty total: `{(realized_summary or {}).get('closed_qty_total')}`\n",
        f"- Close rows: `{(realized_summary or {}).get('close_row_count')}`; opening rows: `{(realized_summary or {}).get('opening_row_count')}`\n",
        f"- Cost basis: `{(realized_summary or {}).get('cost_basis_source')}`\n",
        f"- Lot source: `{(realized_summary or {}).get('pre_trade_lot_source')}`\n",
        "\n## Equity / PnL Bridge\n",
        f"- Broker equity change: `{bridge_amounts.get('broker_equity_change')}`\n",
        f"- Snapshot intraday PnL: `{bridge_amounts.get('snapshot_unrealized_intraday_pnl')}`\n",
        f"- Realized PnL estimate: `{bridge_amounts.get('realized_pnl_estimate')}`\n",
        f"- Execution shortfall cost estimate: `{bridge_amounts.get('execution_shortfall_cost_estimate')}`\n",
        f"- Non-trade account activity net: `{bridge_amounts.get('non_trade_account_activity_net_amount')}`; FILL cashflow excluded: `{bridge_amounts.get('trade_fill_cashflow_net_amount_not_equity_pnl')}`\n",
        f"- Unexplained bridge residual: `{bridge_amounts.get('unexplained_after_snapshot_intraday_realized_activity')}`\n",
        f"- Account activity classes: `{account_activity_attr.get('activity_class_counts')}`\n",
        f"- Account state bridge: `{account_state.get('status')}`; equity delta `{account_state.get('equity_delta')}`; cash delta `{account_state.get('cash_delta')}`; gross exposure delta `{account_state.get('gross_exposure_delta')}`\n",
        f"- Account bridge vs summary/bridge deltas: `{account_state.get('equity_delta_vs_summary_delta')}` / `{account_state.get('equity_delta_vs_equity_bridge_change')}`\n",
        f"- Market/factor context: `{market_context.get('status')}`; snapshot+realized `{market_context.get('snapshot_plus_realized_pnl')}`; net/gross `{market_context.get('net_to_gross_after')}`; beta/gross `{market_context.get('net_beta_exposure_to_gross')}`\n",
        f"- Attribution dossier: `{attribution_dossier.get('status')}`; focus symbols `{attribution_dossier.get('focus_symbol_count')}`; buckets `{attribution_dossier.get('primary_bucket_counts')}`\n",
        f"- Corporate actions: `{corporate_actions.get('status')}`; rows `{corporate_actions.get('action_count')}`; residual-symbol matches `{corporate_actions.get('matched_position_residual_symbol_count')}`; errors `{corporate_actions.get('error_count')}`\n",
        f"- Portfolio history: `{portfolio_history.get('status')}`; rows `{portfolio_history.get('row_count')}`; summary-vs-history after delta `{portfolio_history.get('summary_vs_history_after_delta')}`\n",
        f"- Calendar: `{calendar.get('status')}`; session in official calendar `{calendar.get('session_date_in_calendar')}`; previous/next `{calendar.get('expected_previous_trading_date')}` / `{calendar.get('expected_next_trading_date')}`; half day `{calendar.get('session_is_half_day')}`\n",
        "\n## Market / Factor Context\n",
        f"- Benchmark symbols: `{market_context.get('benchmark_symbols')}`; with bars `{market_context.get('benchmark_symbols_with_bars')}`\n",
        f"- Signed beta exposure: `{market_context.get('signed_beta_exposure')}`; gross after MV `{market_context.get('gross_after_market_value')}`\n",
        "\n### Worst Market Context Symbols\n",
        _markdown_table(
            (
                market_context.get("worst_symbols", [])
                if isinstance(market_context.get("worst_symbols"), list)
                else []
            )[:10],
            ["symbol", "after_side", "sic2_sector", "primary_factor", "beta", "snapshot_plus_realized_pnl", "pnl_bps_of_gross_after_market_value"],
        ),
        "\n### Worst Sector Buckets\n",
        _markdown_table(
            (
                market_context.get("worst_sectors", [])
                if isinstance(market_context.get("worst_sectors"), list)
                else []
            )[:10],
            ["bucket", "symbol_count", "snapshot_plus_realized_pnl", "gross_after_market_value", "pnl_bps_of_gross_after_market_value"],
        ),
        "\n### Worst Factor Buckets\n",
        _markdown_table(
            (
                market_context.get("worst_factors", [])
                if isinstance(market_context.get("worst_factors"), list)
                else []
            )[:10],
            ["bucket", "symbol_count", "snapshot_plus_realized_pnl", "gross_after_market_value", "pnl_bps_of_gross_after_market_value"],
        ),
        "\n## Replay Focus Index\n",
        f"- Status: `{attribution_dossier.get('status')}`; strict ready `{attribution_dossier.get('strict_attribution_ready')}`; strict blockers `{attribution_dossier.get('strict_attribution_blocking_items')}`\n",
        f"- Primary buckets: `{attribution_dossier.get('primary_bucket_counts')}`\n",
        f"- Focus tags: `{attribution_dossier.get('focus_tag_counts')}`\n",
        "\n### Top Focus Symbols\n",
        _markdown_table(
            (
                attribution_dossier.get("top_focus_symbols", [])
                if isinstance(attribution_dossier.get("top_focus_symbols"), list)
                else []
            )[:12],
            ["focus_rank", "symbol", "primary_attribution_bucket", "focus_score", "snapshot_plus_realized_pnl", "position_unexplained_notional", "target_error_abs", "decision_execute_planned_delta_change"],
        ),
        "\n### Top Evidence Gap Symbols\n",
        _markdown_table(
            (
                attribution_dossier.get("top_evidence_gap_symbols", [])
                if isinstance(attribution_dossier.get("top_evidence_gap_symbols"), list)
                else []
            )[:10],
            ["focus_rank", "symbol", "evidence_gap_tags", "primary_attribution_bucket", "next_review_action"],
        ),
        "\n## Ideal vs Actual Gap\n",
        f"- Status: `{ideal_actual_gap.get('status')}`; material symbols: `{ideal_actual_gap.get('material_gap_symbol_count')}` / `{ideal_actual_gap.get('symbol_count')}`\n",
        f"- Primary weight error L1, strategy -> actual: `{ideal_actual_gap.get('strategy_to_actual_weight_error_l1_pct')}%`; strategy -> executable: `{ideal_actual_gap.get('strategy_to_executable_weight_error_l1_pct')}%`; executable -> actual: `{ideal_actual_gap.get('executable_to_actual_weight_error_l1_pct')}%`\n",
        f"- Mean / max symbol strategy -> actual weight error: `{_safe_float(ideal_actual_gap.get('mean_symbol_strategy_to_actual_weight_error')) * 100.0}%` / `{_safe_float(ideal_actual_gap.get('max_symbol_strategy_to_actual_weight_error')) * 100.0}%`\n",
        f"- Auxiliary notional translation, ideal/actual: `{ideal_actual_gap.get('gross_ideal_actual_gap_abs')}`; after projected target: `{ideal_actual_gap.get('gross_after_projected_target_gap_abs')}`; projection: `{ideal_actual_gap.get('gross_projection_gap_abs')}`\n",
        f"- Decision-target drift / order-drift notional: `{ideal_actual_gap.get('gross_decision_execute_target_drift_abs')}` / `{ideal_actual_gap.get('gross_decision_execute_order_drift_abs')}`\n",
        f"- Submitted-unfilled / skipped notional: `{ideal_actual_gap.get('gross_submitted_unfilled_notional')}` / `{ideal_actual_gap.get('gross_skipped_notional')}`; PnL loss abs `{ideal_actual_gap.get('gross_pnl_loss_abs')}`\n",
        f"- Gap buckets: `{ideal_actual_gap.get('primary_gap_bucket_counts')}`\n",
        f"- Performance drag buckets: `{ideal_actual_gap.get('performance_drag_bucket_counts')}`\n",
        "\n### Top Gap Drag Symbols\n",
        _markdown_table(
            (
                ideal_actual_gap.get("top_drag_symbols", [])
                if isinstance(ideal_actual_gap.get("top_drag_symbols"), list)
                else []
            )[:12],
            ["gap_rank", "symbol", "primary_gap_bucket", "performance_drag_bucket", "gap_score", "snapshot_plus_realized_pnl", "ideal_actual_gap_abs", "gross_order_gap_notional"],
        ),
        "\n### Top Ideal/Actual Exposure Gaps\n",
        _markdown_table(
            (
                ideal_actual_gap.get("top_ideal_actual_gaps", [])
                if isinstance(ideal_actual_gap.get("top_ideal_actual_gaps"), list)
                else []
            )[:10],
            ["symbol", "primary_gap_bucket", "raw_target_notional_estimate", "projected_target_notional_estimate", "after_market_value", "ideal_actual_gap_abs"],
        ),
        "\n### Top Order Gaps\n",
        _markdown_table(
            (
                ideal_actual_gap.get("top_order_gaps", [])
                if isinstance(ideal_actual_gap.get("top_order_gaps"), list)
                else []
            )[:10],
            ["symbol", "primary_gap_bucket", "submitted_unfilled_notional", "skipped_notional", "gross_order_gap_notional", "order_status_latest_set", "skip_reasons"],
        ),
        "\n## Target Transition\n",
        f"- Status: `{transition.get('status')}`; symbols: `{transition.get('symbol_count')}`; attention symbols: `{transition.get('attention_symbol_count')}`\n",
        f"- Material residual symbols: `{transition.get('material_position_residual_symbols')}`; target-gap symbols without residual: `{transition.get('target_gap_symbol_count_without_position_residual')}`\n",
        f"- Gross target error without residual: `{transition.get('gross_target_error_abs_without_position_residual')}`\n",
        "\n### Largest Target Errors\n",
        _markdown_table(
            (transition.get("largest_target_errors", []) if isinstance(transition.get("largest_target_errors"), list) else [])[:10],
            ["symbol", "intent", "outcome", "confidence", "target_error_market_value", "planned_order_count", "fill_count"],
        ),
        "\n### Largest Unverified Transitions\n",
        _markdown_table(
            (
                transition.get("largest_unverified_transitions", [])
                if isinstance(transition.get("largest_unverified_transitions"), list)
                else []
            )[:10],
            ["symbol", "intent", "outcome", "position_unexplained_qty", "position_unexplained_notional", "position_residual_reason_hint"],
        ),
        "\n## Decision / Order Constraints\n",
        f"- Decision intent status: `{decision_intent.get('status')}`; projection-changed symbols: `{decision_intent.get('projection_changed_symbol_count')}`; skipped symbols: `{decision_intent.get('skipped_symbol_count')}`\n",
        f"- Short-floor zeroed/reduced: `{decision_intent.get('short_floor_zeroed_symbol_count')}` / `{decision_intent.get('short_floor_reduced_symbol_count')}`; gross projection delta: `{decision_intent.get('gross_projection_delta_notional_abs')}`\n",
        f"- Order constraints: planned `{order_constraints.get('planned_order_count')}`, skipped `{order_constraints.get('skipped_order_count')}`, whole-share-required `{order_constraints.get('whole_share_required_count')}`\n",
        f"- Filled/unfilled notional estimate: `{order_constraints.get('gross_filled_notional_estimate')}` / `{order_constraints.get('gross_unfilled_notional_estimate')}`\n",
        "\n### Largest Projection Changes\n",
        _markdown_table(
            (
                decision_intent.get("largest_projection_changes", [])
                if isinstance(decision_intent.get("largest_projection_changes"), list)
                else []
            )[:10],
            ["symbol", "projection_reason", "raw_target_signed_weight", "projected_target_signed_weight", "projection_delta_notional_estimate"],
        ),
        "\n### Largest Unfilled Orders\n",
        _markdown_table(
            (
                order_constraints.get("largest_unfilled_orders", [])
                if isinstance(order_constraints.get("largest_unfilled_orders"), list)
                else []
            )[:10],
            ["symbol", "side", "action_class", "planned_abs_notional", "filled_notional_estimate", "unfilled_notional_estimate", "status_latest_set"],
        ),
        "\n## Execution Attempt Diagnostics\n",
        f"- Attempt rows / filled attempts / records: `{execution_attr.get('attempt_row_count')}` / `{execution_attr.get('filled_attempt_row_count')}` / `{execution_attr.get('record_count')}`\n",
        f"- Multi-attempt records: `{execution_attr.get('multi_attempt_record_count')}`; max attempt count `{execution_attr.get('max_attempt_count')}`\n",
        f"- Max actual/configured offset bps: `{execution_attr.get('max_attempt_offset_bps')}` / `{execution_attr.get('max_configured_offset_bps')}`; max requote cycle `{execution_attr.get('max_requote_cycle')}`\n",
        f"- Records hitting max offset: `{execution_attr.get('records_hitting_max_offset_count')}`; unfilled at max offset `{execution_attr.get('unfilled_records_hitting_max_offset_count')}`; remaining notional `{execution_attr.get('unfilled_records_hitting_max_offset_remaining_notional')}`\n",
        "\n### Top Requoted Orders\n",
        _markdown_table(
            (
                execution_attr.get("top_requote_records", [])
                if isinstance(execution_attr.get("top_requote_records"), list)
                else []
            )[:10],
            ["symbol", "side", "stage", "status_latest", "attempt_count", "max_attempt_offset_bps", "max_configured_offset_bps", "remaining_notional_at_reference"],
        ),
        "\n### Unfilled Orders At Max Offset\n",
        _markdown_table(
            (
                execution_attr.get("top_unfilled_records_hitting_max_offset", [])
                if isinstance(execution_attr.get("top_unfilled_records_hitting_max_offset"), list)
                else []
            )[:10],
            ["symbol", "side", "stage", "status_latest", "attempt_count", "max_attempt_offset_bps", "max_configured_offset_bps", "remaining_notional_at_reference"],
        ),
        "\n## Decision vs Execute Drift\n",
        f"- Changed/material symbols: `{decision_execute_drift.get('changed_symbol_count')}` / `{decision_execute_drift.get('material_changed_symbol_count')}`\n",
        f"- Decision/execute order count: `{decision_execute_drift.get('decision_order_count')}` / `{decision_execute_drift.get('execute_order_count')}`\n",
        f"- Gross target/order delta drift: `{decision_execute_drift.get('gross_abs_target_notional_delta_estimate')}` / `{decision_execute_drift.get('gross_abs_planned_delta_notional_change')}`\n",
        "\n### Largest Decision/Execute Order Drift\n",
        _markdown_table(
            (
                decision_execute_drift.get("largest_planned_delta_changes", [])
                if isinstance(decision_execute_drift.get("largest_planned_delta_changes"), list)
                else []
            )[:10],
            ["symbol", "order_presence", "drift_reasons", "decision_planned_delta_notional", "execute_planned_delta_notional", "planned_delta_notional_change"],
        ),
        "\n## Market Price Evidence\n",
        f"- Status: `{market_price.get('status')}`; symbols: `{market_price.get('symbol_count')}`; execute price snapshot exists: `{market_price.get('execute_price_snapshot_exists')}`\n",
        f"- Missing reference symbols: `{market_price.get('execute_missing_reference_symbol_count')}`; fallback-only symbols: `{market_price.get('fallback_only_symbol_count')}`; large decision/execute reference moves: `{market_price.get('large_decision_execute_reference_move_count')}`\n",
        "\n### Largest Reference Price Moves\n",
        _markdown_table(
            (
                market_price.get("largest_decision_execute_reference_moves", [])
                if isinstance(market_price.get("largest_decision_execute_reference_moves"), list)
                else []
            )[:10],
            ["symbol", "status", "decision_reference_price_used", "execute_reference_price_used", "decision_execute_reference_change_bps", "execute_reference_source_inferred"],
        ),
        "\n## Intraday Price Path Evidence\n",
        f"- Status: `{intraday_bars.get('status')}`; symbols: `{intraday_bars.get('symbol_count')}`; bar symbols: `{intraday_bars.get('bar_symbol_count')}`\n",
        f"- Missing bar symbols: `{intraday_bars.get('missing_bar_symbol_count')}`; filled symbols missing bars: `{intraday_bars.get('filled_symbols_missing_bars_count')}`; capture errors: `{intraday_bars.get('error_count')}`\n",
        f"- Max intraday range bps: `{intraday_bars.get('max_intraday_range_bps')}`\n",
        "\n### Worst Fill vs Intraday VWAP\n",
        _markdown_table(
            (
                intraday_bars.get("worst_fill_vs_vwap_adverse", [])
                if isinstance(intraday_bars.get("worst_fill_vs_vwap_adverse"), list)
                else []
            )[:10],
            ["symbol", "primary_fill_side", "fill_vwap", "vwap", "fill_vwap_vs_vwap_adverse_bps", "fill_position_in_range_pct", "price_path_hints"],
        ),
        "\n### Largest Intraday Ranges\n",
        _markdown_table(
            (
                intraday_bars.get("worst_intraday_ranges", [])
                if isinstance(intraday_bars.get("worst_intraday_ranges"), list)
                else []
            )[:10],
            ["symbol", "bar_count", "open", "high", "low", "close", "range_bps", "close_vs_open_bps"],
        ),
        "\n## Quote / Spread Evidence\n",
        f"- Status: `{quotes.get('status')}`; symbols: `{quotes.get('symbol_count')}`; quote symbols: `{quotes.get('quote_symbol_count')}`\n",
        f"- Missing quotes: `{quotes.get('missing_quote_symbol_count')}`; invalid quotes: `{quotes.get('invalid_quote_symbol_count')}`; wide spreads: `{quotes.get('wide_spread_symbol_count')}`; errors: `{quotes.get('error_count')}`\n",
        f"- Max spread bps: `{quotes.get('max_spread_bps')}`\n",
        "\n### Widest Quote Spreads\n",
        _markdown_table(
            (
                quotes.get("widest_spreads", [])
                if isinstance(quotes.get("widest_spreads"), list)
                else []
            )[:10],
            ["symbol", "bid_price", "ask_price", "mid_price", "spread_bps", "reference_vs_mid_bps", "latest_trade_vs_mid_bps"],
        ),
        "\n### Worst Fill vs Quote Mid\n",
        _markdown_table(
            (
                quotes.get("worst_fill_vs_mid_adverse", [])
                if isinstance(quotes.get("worst_fill_vs_mid_adverse"), list)
                else []
            )[:10],
            ["symbol", "primary_fill_side", "fill_vwap", "mid_price", "fill_vwap_vs_mid_adverse_bps", "spread_bps", "price_microstructure_hints"],
        ),
        "\n## Corporate Action Evidence\n",
        f"- Status: `{corporate_actions.get('status')}`; raw exists: `{corporate_actions.get('raw_artifact_exists')}`; action rows: `{corporate_actions.get('action_count')}`\n",
        f"- Matched residual symbols: `{corporate_actions.get('matched_position_residual_symbol_count')}`; matched account-activity symbols: `{corporate_actions.get('matched_account_activity_symbol_count')}`\n",
        f"- Residual symbols without corporate action: `{corporate_actions.get('residual_symbols_without_corporate_action_count')}`; capture errors: `{corporate_actions.get('error_count')}`\n",
        "\n### Corporate Action Matches\n",
        _markdown_table(
            (
                corporate_actions.get("top_matches", [])
                if isinstance(corporate_actions.get("top_matches"), list)
                else []
            )[:10],
            ["symbol", "action_type", "status", "position_residual_notional", "matching_account_activity_net_amount", "causal_hint"],
        ),
        "\n## Portfolio History Evidence\n",
        f"- Status: `{portfolio_history.get('status')}`; rows: `{portfolio_history.get('row_count')}`\n",
        f"- Before rows: `{(portfolio_history.get('before') or {}).get('row_count') if isinstance(portfolio_history.get('before'), dict) else None}`; after rows: `{(portfolio_history.get('after') or {}).get('row_count') if isinstance(portfolio_history.get('after'), dict) else None}`\n",
        f"- History last equity: `{portfolio_history.get('history_last_equity')}`; summary after equity: `{portfolio_history.get('summary_equity_after')}`; delta: `{portfolio_history.get('summary_vs_history_after_delta')}`\n",
        f"- Largest equity range in captured history: `{portfolio_history.get('largest_equity_drawdown_from_history')}`\n",
        "\n## Market Calendar Evidence\n",
        f"- Status: `{calendar.get('status')}`; rows: `{calendar.get('row_count')}`; raw exists: `{calendar.get('raw_artifact_exists')}`\n",
        f"- Session in calendar: `{calendar.get('session_date_in_calendar')}`; open/close: `{calendar.get('session_open')}` / `{calendar.get('session_close')}`; regular minutes: `{calendar.get('session_regular_minutes')}`\n",
        f"- Expected previous/next trading date: `{calendar.get('expected_previous_trading_date')}` / `{calendar.get('expected_next_trading_date')}`; half day: `{calendar.get('session_is_half_day')}`\n",
        "\n## Strict Replay Readiness\n",
        f"- Strict account/position replay ready: `{evidence.get('strict_account_position_replay_ready')}`\n",
        f"- Strict attribution checklist: `{strict_checklist.get('status')}`; blocking items: `{strict_checklist.get('blocking_item_count')}`\n",
        f"- Position snapshot integrity: `{snapshot_integrity.get('status')}`; material residual symbols: `{snapshot_integrity.get('material_residual_symbol_count')}`\n",
        f"- Missing after symbols without captured fill: `{snapshot_integrity.get('missing_after_symbols_without_captured_fill_count')}`\n",
        f"- Residual diagnosis: `{residual_diag.get('status')}`; attention rows: `{residual_diag.get('attention_count')}`\n",
        "\n### Strict Attribution Blockers\n",
        _markdown_table(
            (
                strict_checklist.get("top_blockers", [])
                if isinstance(strict_checklist.get("top_blockers"), list)
                else []
            )[:12],
            ["area", "item", "status", "severity", "observed", "next_action"],
        ),
        "\n## Audit Checks\n",
        f"- Status: `{checks.get('status') if isinstance(checks, dict) else None}`\n",
        f"- Issues: `{checks.get('issue_count') if isinstance(checks, dict) else None}` "
        f"{checks.get('issue_count_by_severity') if isinstance(checks, dict) else ''}\n",
        _markdown_table(top_check_issues, ["severity", "status", "name", "detail"]),
        "\n## Worst Snapshot Intraday PnL\n",
        _markdown_table(worst, ["symbol", "side", "market_value_after", "unrealized_intraday_pl_snapshot", "change_today"]),
        "\n## Best Snapshot Intraday PnL\n",
        _markdown_table(best, ["symbol", "side", "market_value_after", "unrealized_intraday_pl_snapshot", "change_today"]),
        "\n## Unfilled / Non-filled Orders\n",
        _markdown_table(
            unfilled[:20],
            [
                "symbol", "side", "stage", "planned_delta_notional", "status_latest",
                "not_submitted_reason", "filled_qty", "remaining_qty", "submit_error_class", "requested_qty",
                "broker_available_qty", "broker_existing_qty", "slippage_bps",
            ],
        ),
        "\n## Files in this audit package\n",
        "- `00_run_context.json` — run inputs/paths and artifact availability\n",
        "- Source artifacts in run dir — `source_code_snapshot.zip`, `source_git_diff.patch`, and manifests capture exact runnable code for future runs\n",
        "- `01_decision_trace.csv` — symbol-level target, alpha, lot, position and PnL snapshot\n",
        "- `02_lot_trace.csv` — factor-lot age/lock trace\n",
        "- `03_order_trace.csv` — plan vs submit/fill trace\n",
        "- `04_position_pnl_snapshot.csv` — broker position PnL snapshot\n",
        "- `05_fill_trace.csv` — fill-level trace; broker FILL activities when available, execution-record fallback otherwise\n",
        "- `06_factor_attribution_snapshot.csv` — approximate factor PnL allocation by factor-lot weight share\n",
        "- `07_risk_snapshot.json` — aggregate exposure, quality, side/sector/factor summaries\n",
        "- `08_realized_pnl_ledger.csv` — fill x lot realized PnL attribution ledger\n",
        "- `09_factor_realized_pnl.csv` — realized PnL grouped by factor and closed side\n",
        "- `10_symbol_realized_pnl.csv` — realized PnL grouped by symbol and closed side\n",
        "- `11_realized_pnl_summary.json` — realized PnL ledger metadata and totals\n",
        "- `12_audit_manifest.json` — file inventory with size, mtime, and sha256 hashes\n",
        "- `13_order_poll_timeline.csv` — flattened order status polling/cancel timeline when available\n",
        "- `14_audit_checks.json` — consistency checks across summary, records, fills, manifests, and timeline\n",
        "- `15_data_quality_snapshot.json` — signal-input coverage, freshness, missingness, and factor score summaries\n",
        "- `16_position_reconciliation.csv` — before/after signed position qty reconciled against fill qty by symbol\n",
        "- `17_position_reconciliation_summary.json` — aggregate reconciliation diagnostics and largest residuals\n",
        "- `18_order_attempt_trace.csv` — attempt-level quote, requote, status, fill, and broker fill match trace\n",
        "- `19_broker_activity_trace.csv` — flattened broker account/FILL activities captured for the run\n",
        "- `20_broker_activity_summary.json` — activity counts by source, type, side, symbol, and order-id coverage\n",
        "- `21_api_audit_requests.csv` — parsed per-request Alpaca API audit rows when available\n",
        "- `22_api_audit_summary.json` — API request counts, status codes, latency, and error summary\n",
        "- `23_broker_order_universe.csv` — combined broker order snapshots/all-order captures by source\n",
        "- `24_broker_order_universe_summary.json` — unique broker order coverage and status/side totals\n",
        "- `25_staged_rebuild_trace.csv` — Staged RegT release/rebuild/buying-power-cap snapshot index\n",
        "- `26_staged_rebuild_summary.json` — staged execution replay metadata and aggregate rebuild effects\n",
        "- `27_execution_attribution_trace.csv` — attempt-level reference/limit/requote/fill implementation-shortfall trace\n",
        "- `28_execution_attribution_summary.json` — execution shortfall, requote, max-offset, and worst-row totals\n",
        "- `29_equity_pnl_bridge.csv` — diagnostic bridge from broker equity change to PnL/activity components\n",
        "- `30_equity_pnl_bridge.json` — equity bridge metadata, component amounts, and residual notes\n",
        "- `31_account_field_diff.csv` — before/after broker account field diff when raw account snapshots exist\n",
        "- `32_account_field_diff_summary.json` — tracked account field deltas and missing-snapshot status\n",
        "- Source run dir `broker_account_configurations_before/after.json` — Alpaca account rule/config snapshots for constraint attribution\n",
        "- `64_account_state_bridge.csv` — grouped broker account equity/cash/exposure/buying-power/margin deltas\n",
        "- `65_account_state_bridge_summary.json` — account-state bridge status and largest field deltas for attribution\n",
        "- `66_market_context_attribution.csv` — symbol, side, sector, beta, factor, benchmark context for PnL attribution\n",
        "- `67_market_context_summary.json` — market/factor context summary and worst buckets\n",
        "- `68_replay_focus_trace.csv` — ranked symbol-level review focus tying PnL, residuals, drift, constraints, price, and evidence gaps\n",
        "- `69_attribution_dossier.json` — daily attribution dossier with top losses, residuals, drifts, strict blockers, and coverage gaps\n",
        "- `78_ideal_vs_actual_gap.csv` — raw strategy target to actual after-position gap decomposition by symbol\n",
        "- `79_ideal_vs_actual_gap_summary.json` — ideal-vs-actual gap totals, buckets, and top drag symbols\n",
        "- `80_executable_target_projection.csv` — raw weights, executable target shares/weights, per-symbol gaps, and binding constraints\n",
        "- `81_executable_target_projection_summary.json` — optimizer priority, buying-power use, integer-short gap, and 85/90/95% scenarios\n",
        "- `82_position_capacity_summary.json` — gross-position use versus the 90% target and total RegT capacity\n",
        "- `70_account_config_diff.csv` — Alpaca account trading configuration before/after diff\n",
        "- `71_account_config_summary.json` — account configuration change summary for constraint attribution\n",
        "- Source run dir `run_evidence_digest.json` — semantic digest of raw broker, execution, market, API, source, and scheduler evidence\n",
        "- `72_run_evidence_digest_summary.json` — digest coverage summary and strict replay missing-file list\n",
        "- `73_run_evidence_digest_checks.csv` — file-level digest presence/hash/count checks\n",
        "- `76_run_failure_diagnosis.csv` — scheduler/executor failure diagnosis rows for missed or partial runs\n",
        "- `77_run_failure_diagnosis_summary.json` — run-health status, failure class, exception signature, and scheduler context\n",
        "- `33_event_timeline.csv` — unified scheduler/executor/API/order/fill event timeline\n",
        "- `34_event_timeline_summary.json` — timeline source/type counts and first/last timestamps\n",
        "- `35_symbol_attribution_bridge.csv` — symbol-level snapshot PnL, realized PnL, execution shortfall, and position residuals\n",
        "- `36_symbol_attribution_summary.json` — symbol attribution totals and worst contributors\n",
        "- `37_position_snapshot_integrity.csv/json` — before/after position snapshot completeness, raw/stability evidence, and residual flags\n",
        "- `38_residual_diagnosis.csv/json` — account, equity bridge, and position residual diagnosis buckets\n",
        "- `39_evidence_completeness.csv/json` — evidence coverage by replay area and strict replay readiness\n",
        "- `40_target_transition_trace.csv` — intended before-to-target transition vs after snapshot by symbol\n",
        "- `41_target_transition_summary.json` — target-transition status, counts, largest gaps, and unverified transitions\n",
        "- `42_decision_intent_trace.csv` — raw target weights, projected executable weights, signal/lot context, and no-order reasons\n",
        "- `43_decision_intent_summary.json` — target projection, short-floor, skipped-symbol, and no-order diagnostics\n",
        "- `44_order_constraint_trace.csv` — order-builder constraints, quantity rounding, skipped rows, and fill coverage\n",
        "- `45_order_constraint_summary.json` — order constraint totals, skipped reasons, whole-share counts, and largest unfilled orders\n",
        "- `46_decision_execute_drift.csv` — decision-time plan vs execute-time rebuilt plan by symbol\n",
        "- `47_decision_execute_drift_summary.json` — plan drift counts, reasons, and largest decision/execute order deltas\n",
        "- `48_market_price_evidence.csv` — reference price, latest trade, fallback, fill VWAP, and decision/execute price drift by symbol\n",
        "- `49_market_price_evidence_summary.json` — price evidence coverage, missing references, fallback-only symbols, and largest reference moves\n",
        "- `58_intraday_bar_evidence.csv` — symbol-level 1-minute bar OHLC/VWAP/range evidence linked to fills and reference prices\n",
        "- `59_intraday_bar_summary.json` — intraday price-path coverage, missing symbols, source errors, and worst fill-vs-VWAP rows\n",
        "- `60_quote_evidence.csv` — latest bid/ask/mid/spread evidence linked to references and fills\n",
        "- `61_quote_summary.json` — quote coverage, invalid/wide spreads, and worst fill-vs-quote-mid rows\n",
        "- `50_account_activity_attribution.csv` — broker account activity classification into fills, dividends, fees, transfers, corporate actions, and unknowns\n",
        "- `51_account_activity_attribution_summary.json` — activity class totals and non-trade equity-impact amounts used by the equity bridge\n",
        "- `52_strict_attribution_checklist.csv` — strict attribution readiness checklist and blockers by replay area\n",
        "- `53_strict_attribution_checklist_summary.json` — strict attribution readiness summary for future upgraded runs\n",
        "- `54_corporate_action_trace.csv` — broker corporate-action evidence by symbol, linked to residual/account/price clues\n",
        "- `55_corporate_action_summary.json` — corporate-action capture status, matches, missing residual coverage, and API errors\n",
        "- `56_portfolio_history_trace.csv` — broker account equity/PnL time series rows from portfolio history\n",
        "- `57_portfolio_history_summary.json` — portfolio-history coverage and summary-vs-history equity consistency checks\n",
        "- `62_calendar_trace.csv` — Alpaca official trading-calendar rows around the execute session, including half-day signals\n",
        "- `63_calendar_summary.json` — calendar capture status plus expected previous/next official trading dates\n",
    ]
    path.write_text("".join(lines), encoding="utf-8")


def generate_audit(run_dir: Path, decision_dir: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    decision_dir = decision_dir.resolve() if decision_dir else _infer_decision_dir(run_dir)
    audit_dir = run_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in [
        "00_run_context.json",
        "01_decision_trace.csv",
        "02_lot_trace.csv",
        "03_order_trace.csv",
        "04_position_pnl_snapshot.csv",
        "05_risk_snapshot.json",
        "05_fill_trace.csv",
        "06_factor_attribution_snapshot.csv",
        "07_risk_snapshot.json",
        "08_realized_pnl_ledger.csv",
        "09_factor_realized_pnl.csv",
        "10_symbol_realized_pnl.csv",
        "11_realized_pnl_summary.json",
        "12_audit_manifest.json",
        "13_order_poll_timeline.csv",
        "14_audit_checks.json",
        "15_data_quality_snapshot.json",
        "16_position_reconciliation.csv",
        "17_position_reconciliation_summary.json",
        "18_order_attempt_trace.csv",
        "19_broker_activity_trace.csv",
        "20_broker_activity_summary.json",
        "21_api_audit_requests.csv",
        "22_api_audit_summary.json",
        "23_broker_order_universe.csv",
        "24_broker_order_universe_summary.json",
        "25_staged_rebuild_trace.csv",
        "26_staged_rebuild_summary.json",
        "27_execution_attribution_trace.csv",
        "28_execution_attribution_summary.json",
        "29_equity_pnl_bridge.csv",
        "30_equity_pnl_bridge.json",
        "31_account_field_diff.csv",
        "32_account_field_diff_summary.json",
        "33_event_timeline.csv",
        "34_event_timeline_summary.json",
        "35_symbol_attribution_bridge.csv",
        "36_symbol_attribution_summary.json",
        "37_position_snapshot_integrity.csv",
        "37_position_snapshot_integrity.json",
        "38_residual_diagnosis.csv",
        "38_residual_diagnosis.json",
        "39_evidence_completeness.csv",
        "39_evidence_completeness.json",
        "40_target_transition_trace.csv",
        "41_target_transition_summary.json",
        "42_decision_intent_trace.csv",
        "43_decision_intent_summary.json",
        "44_order_constraint_trace.csv",
        "45_order_constraint_summary.json",
        "46_decision_execute_drift.csv",
        "47_decision_execute_drift_summary.json",
        "48_market_price_evidence.csv",
        "49_market_price_evidence_summary.json",
        "50_account_activity_attribution.csv",
        "51_account_activity_attribution_summary.json",
        "52_strict_attribution_checklist.csv",
        "53_strict_attribution_checklist_summary.json",
        "54_corporate_action_trace.csv",
        "55_corporate_action_summary.json",
        "56_portfolio_history_trace.csv",
        "57_portfolio_history_summary.json",
        "58_intraday_bar_evidence.csv",
        "59_intraday_bar_summary.json",
        "60_quote_evidence.csv",
        "61_quote_summary.json",
        "62_calendar_trace.csv",
        "63_calendar_summary.json",
        "64_account_state_bridge.csv",
        "65_account_state_bridge_summary.json",
        "66_market_context_attribution.csv",
        "67_market_context_summary.json",
        "68_replay_focus_trace.csv",
        "69_attribution_dossier.json",
        "70_account_config_diff.csv",
        "71_account_config_summary.json",
        "72_run_evidence_digest_summary.json",
        "73_run_evidence_digest_checks.csv",
        "74_startup_binding_checks.csv",
        "75_startup_binding_summary.json",
        "76_run_failure_diagnosis.csv",
        "77_run_failure_diagnosis_summary.json",
        "78_ideal_vs_actual_gap.csv",
        "79_ideal_vs_actual_gap_summary.json",
        "80_executable_target_projection.csv",
        "81_executable_target_projection_summary.json",
        "82_position_capacity_summary.json",
        "daily_review.md",
    ]:
        try:
            (audit_dir / stale_name).unlink(missing_ok=True)
        except Exception:
            pass

    summary = _read_json(run_dir / "execution_summary.json", {})
    quality = _read_json(run_dir / "execution_quality.json", {})
    plan = _read_json(run_dir / "order_plan.json", {})
    records = _read_json(run_dir / "execution_records.json", [])
    broker_fills = _read_json(run_dir / "broker_fill_activities.json", {})
    broker_order_snapshots = _read_json(run_dir / "broker_order_snapshots.json", {})
    broker_account_activities = _read_json(run_dir / "broker_account_activities.json", {})
    order_poll_timeline = _read_json(run_dir / "order_poll_timeline.json", {})
    staged_rebuild_snapshots = _read_json(run_dir / "staged_rebuild_snapshots.json", {})
    decision_plan = _read_json((decision_dir / "order_plan.json") if decision_dir else Path("__missing__"), {})
    startup_binding_rows, startup_binding_summary = _build_startup_binding_audit(run_dir.parent)

    session_date = str(summary.get("decision_date") or plan.get("decision_date") or "")
    session_key = session_date.replace("-", "") if session_date else run_dir.name.split("_")[0]
    lot_path = run_dir / f"lot_snapshot_{session_key}.json"
    if not lot_path.exists() and decision_dir:
        lot_path = decision_dir / f"lot_snapshot_{session_key}.json"
    lot_snapshot = _read_json(lot_path, {})
    pre_trade_lot_snapshot, pre_trade_lot_path, pre_trade_lot_source = _find_pre_trade_lot_snapshot(
        run_dir,
        decision_dir,
        session_key,
    )
    alpha_path = _find_alpha_path(run_dir, decision_dir, summary)

    targets = _load_targets(run_dir / "decision_targets.csv")
    if not targets and decision_dir:
        targets = _load_targets(decision_dir / "decision_targets.csv")
    before = _position_maps(_read_csv_rows(run_dir / "broker_positions_before.csv"))
    after = _position_maps(_read_csv_rows(run_dir / "broker_positions_after.csv"))
    alpha_rows = _read_csv_rows(alpha_path) if alpha_path and alpha_path.exists() else []
    alpha = {
        str(row.get("symbol") or "").upper().strip(): row
        for row in alpha_rows
        if str(row.get("symbol") or "").strip()
    }
    if not alpha:
        alpha = _load_alpha_by_symbol(alpha_path)
    session_idx = _safe_int(summary.get("session_idx") or plan.get("session_idx"))
    lot_rows, lot_weights = _build_lot_trace(lot_snapshot, session_idx)
    decision_rows = _build_decision_trace(targets, alpha, before, after, lot_weights)
    order_rows = _build_order_trace(
        plan,
        records if isinstance(records, list) else [],
        quality,
        summary if isinstance(summary, dict) else {},
    )
    fill_rows = _build_fill_trace(records if isinstance(records, list) else [], broker_fills if isinstance(broker_fills, dict) else {})
    position_rows = _build_position_pnl(before, after)
    factor_rows = _build_factor_attribution(decision_rows, lot_weights)
    realized_rows = _build_realized_pnl_ledger(
        fill_rows=fill_rows,
        records=records if isinstance(records, list) else [],
        order_rows=order_rows,
        before_positions=before,
        pre_trade_lot_snapshot=pre_trade_lot_snapshot,
        session_date=session_date,
        session_idx=session_idx,
        lot_source=pre_trade_lot_source,
    )
    factor_realized_rows = _group_realized(realized_rows, ["factor", "closed_position_side"])
    symbol_realized_rows = _group_realized(realized_rows, ["symbol", "closed_position_side"])
    realized_summary = _realized_summary(realized_rows, pre_trade_lot_path, pre_trade_lot_source)
    order_poll_rows = _build_order_poll_rows(order_poll_timeline if isinstance(order_poll_timeline, dict) else {})
    data_quality = _build_data_quality_snapshot(
        alpha_rows=alpha_rows,
        targets=targets,
        plan=plan if isinstance(plan, dict) else {},
        session_date=session_date,
        alpha_path=alpha_path,
    )
    order_attempt_rows = _build_order_attempt_rows(records if isinstance(records, list) else [], fill_rows)
    broker_activity_rows, broker_activity_summary = _build_broker_activity_trace(
        broker_fills=broker_fills if isinstance(broker_fills, dict) else {},
        broker_account_activities=broker_account_activities if isinstance(broker_account_activities, dict) else {},
        records=records if isinstance(records, list) else [],
    )
    account_activity_attribution_rows, account_activity_attribution_summary = _build_account_activity_attribution(
        broker_activity_rows=broker_activity_rows,
    )
    broker_order_universe_rows, broker_order_universe_summary = _build_broker_order_universe(
        run_dir=run_dir,
        broker_order_snapshots=broker_order_snapshots if isinstance(broker_order_snapshots, dict) else {},
        records=records if isinstance(records, list) else [],
    )
    api_audit_rows, api_audit_summary = _build_api_audit_outputs(run_dir)
    staged_rebuild_rows, staged_rebuild_summary = _build_staged_rebuild_outputs(
        run_dir,
        staged_rebuild_snapshots if isinstance(staged_rebuild_snapshots, dict) else {},
    )
    executable_projection_rows, executable_projection_summary = _build_executable_target_projection_outputs(
        run_dir=run_dir,
        staged_rebuild_snapshots=staged_rebuild_snapshots
        if isinstance(staged_rebuild_snapshots, dict)
        else {},
    )
    position_capacity_summary = _build_position_capacity_summary(run_dir)
    execution_attribution_rows, execution_attribution_summary = _build_execution_attribution_outputs(
        records=records if isinstance(records, list) else [],
        fill_rows=fill_rows,
    )
    account_field_rows, account_field_summary = _build_account_field_diff(
        run_dir,
        summary if isinstance(summary, dict) else {},
    )
    account_config_rows, account_config_summary = _build_account_config_diff(run_dir)
    run_evidence_digest_rows, run_evidence_digest_summary = _build_run_evidence_digest_audit(run_dir)
    event_timeline_rows, event_timeline_summary = _build_event_timeline(
        run_dir=run_dir,
        records=records if isinstance(records, list) else [],
        fill_rows=fill_rows,
        order_attempt_rows=order_attempt_rows,
        order_poll_rows=order_poll_rows,
        api_audit_rows=api_audit_rows,
        staged_rebuild_rows=staged_rebuild_rows,
    )
    reconciliation_rows, reconciliation_summary = _build_position_reconciliation(
        before=before,
        after=after,
        fill_rows=fill_rows,
        order_rows=order_rows,
    )
    symbol_attribution_rows, symbol_attribution_summary = _build_symbol_attribution_bridge(
        decision_rows=decision_rows,
        realized_rows=realized_rows,
        execution_attribution_rows=execution_attribution_rows,
        reconciliation_rows=reconciliation_rows,
        fill_rows=fill_rows,
        order_rows=order_rows,
    )
    position_snapshot_integrity_rows, position_snapshot_integrity_summary = _build_position_snapshot_integrity(
        run_dir=run_dir,
        summary=summary if isinstance(summary, dict) else {},
        before=before,
        after=after,
        reconciliation_rows=reconciliation_rows,
        fill_rows=fill_rows,
    )
    target_transition_rows, target_transition_summary = _build_target_transition_trace(
        decision_rows=decision_rows,
        order_rows=order_rows,
        fill_rows=fill_rows,
        reconciliation_rows=reconciliation_rows,
        summary=summary if isinstance(summary, dict) else {},
    )
    decision_intent_rows, decision_intent_summary = _build_decision_intent_trace(
        plan=plan if isinstance(plan, dict) else {},
        decision_rows=decision_rows,
        order_rows=order_rows,
        summary=summary if isinstance(summary, dict) else {},
    )
    order_constraint_rows, order_constraint_summary = _build_order_constraint_trace(
        plan=plan if isinstance(plan, dict) else {},
        order_rows=order_rows,
        fill_rows=fill_rows,
        decision_rows=decision_rows,
        summary=summary if isinstance(summary, dict) else {},
    )
    decision_execute_drift_rows, decision_execute_drift_summary = _build_decision_execute_drift(
        decision_plan=decision_plan if isinstance(decision_plan, dict) else {},
        execute_plan=plan if isinstance(plan, dict) else {},
    )
    market_price_evidence_rows, market_price_evidence_summary = _build_market_price_evidence(
        run_dir=run_dir,
        decision_dir=decision_dir,
        decision_plan=decision_plan if isinstance(decision_plan, dict) else {},
        execute_plan=plan if isinstance(plan, dict) else {},
        decision_rows=decision_rows,
        fill_rows=fill_rows,
    )
    market_context_rows, market_context_summary = _build_market_context_attribution(
        run_dir=run_dir,
        decision_rows=decision_rows,
        lot_rows=lot_rows,
        symbol_attribution_rows=symbol_attribution_rows,
        market_price_evidence_rows=market_price_evidence_rows,
    )
    intraday_bar_rows, intraday_bar_summary = _build_intraday_bar_evidence(
        run_dir=run_dir,
        market_price_evidence_rows=market_price_evidence_rows,
        fill_rows=fill_rows,
    )
    quote_rows, quote_summary = _build_quote_evidence(
        run_dir=run_dir,
        market_price_evidence_rows=market_price_evidence_rows,
        fill_rows=fill_rows,
    )
    corporate_action_rows, corporate_action_summary = _build_corporate_action_trace(
        run_dir=run_dir,
        session_date=session_date,
        reconciliation_rows=reconciliation_rows,
        market_price_evidence_rows=market_price_evidence_rows,
        account_activity_rows=account_activity_attribution_rows,
    )
    portfolio_history_rows, portfolio_history_summary = _build_portfolio_history_trace(
        run_dir=run_dir,
        summary=summary if isinstance(summary, dict) else {},
    )
    calendar_rows, calendar_summary = _build_calendar_trace(
        run_dir=run_dir,
        session_date=session_date,
    )

    decision_diag = {}
    for candidate in [decision_plan, plan, summary]:
        raw = candidate.get("decision_diagnostics") if isinstance(candidate, dict) else None
        if isinstance(raw, dict) and raw:
            decision_diag = raw
            break

    context = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_dir": run_dir.as_posix(),
        "decision_dir": decision_dir.as_posix() if decision_dir else None,
        "session_date": session_date,
        "session_idx": session_idx,
        "artifacts": {
            "run_context": (run_dir / "run_context.json").as_posix()
            if (run_dir / "run_context.json").exists()
            else None,
            "alpaca_api_audit": (run_dir / "alpaca_api_audit.jsonl").as_posix()
            if (run_dir / "alpaca_api_audit.jsonl").exists()
            else None,
            "source_code_manifest": (run_dir / "source_code_manifest.json").as_posix()
            if (run_dir / "source_code_manifest.json").exists()
            else None,
            "source_git_snapshot": (run_dir / "source_git_snapshot.json").as_posix()
            if (run_dir / "source_git_snapshot.json").exists()
            else None,
            "source_git_diff": (run_dir / "source_git_diff.patch").as_posix()
            if (run_dir / "source_git_diff.patch").exists()
            else None,
            "source_code_snapshot": (run_dir / "source_code_snapshot.zip").as_posix()
            if (run_dir / "source_code_snapshot.zip").exists()
            else None,
            "source_code_snapshot_manifest": (run_dir / "source_code_snapshot_manifest.json").as_posix()
            if (run_dir / "source_code_snapshot_manifest.json").exists()
            else None,
            "python_environment": (run_dir / "python_environment.json").as_posix()
            if (run_dir / "python_environment.json").exists()
            else None,
            "input_file_manifest": (run_dir / "input_file_manifest.json").as_posix()
            if (run_dir / "input_file_manifest.json").exists()
            else None,
            "execution_summary": (run_dir / "execution_summary.json").as_posix(),
            "execution_quality": (run_dir / "execution_quality.json").as_posix(),
            "order_plan": (run_dir / "order_plan.json").as_posix(),
            "execution_records": (run_dir / "execution_records.json").as_posix(),
            "order_poll_timeline": (run_dir / "order_poll_timeline.json").as_posix()
            if (run_dir / "order_poll_timeline.json").exists()
            else None,
            "run_artifact_manifest": (run_dir / "run_artifact_manifest.json").as_posix()
            if (run_dir / "run_artifact_manifest.json").exists()
            else None,
            "run_evidence_digest": (run_dir / "run_evidence_digest.json").as_posix()
            if (run_dir / "run_evidence_digest.json").exists()
            else None,
            "scheduler_task_context": (run_dir / "scheduler_task_context.json").as_posix()
            if (run_dir / "scheduler_task_context.json").exists()
            else None,
            "scheduler_task_result": (run_dir / "scheduler_task_result.json").as_posix()
            if (run_dir / "scheduler_task_result.json").exists()
            else None,
            "startup_log": (run_dir.parent / "daemon" / "startup.bat.log").as_posix()
            if (run_dir.parent / "daemon" / "startup.bat.log").exists()
            else None,
            "tray_launcher_pid_file": (run_dir.parent / "daemon" / "tray_launcher.pid").as_posix()
            if (run_dir.parent / "daemon" / "tray_launcher.pid").exists()
            else None,
            "scheduler_pid_file": (run_dir.parent / "daemon" / "scheduler.pid").as_posix()
            if (run_dir.parent / "daemon" / "scheduler.pid").exists()
            else None,
            "scheduler_due_latest": (run_dir.parent / "daemon" / "scheduler_due_latest.json").as_posix()
            if (run_dir.parent / "daemon" / "scheduler_due_latest.json").exists()
            else None,
            "scheduler_runtime_latest": (run_dir.parent / "daemon" / "scheduler_runtime_latest.json").as_posix()
            if (run_dir.parent / "daemon" / "scheduler_runtime_latest.json").exists()
            else None,
            "process_health_latest": (run_dir.parent / "process_health_latest.json").as_posix()
            if (run_dir.parent / "process_health_latest.json").exists()
            else None,
            "decision_scheduler_task_context": (decision_dir / "scheduler_task_context.json").as_posix()
            if decision_dir and (decision_dir / "scheduler_task_context.json").exists()
            else None,
            "decision_scheduler_task_result": (decision_dir / "scheduler_task_result.json").as_posix()
            if decision_dir and (decision_dir / "scheduler_task_result.json").exists()
            else None,
            "decision_execution_price_snapshot": (decision_dir / "execution_price_snapshot.json").as_posix()
            if decision_dir and (decision_dir / "execution_price_snapshot.json").exists()
            else None,
            "decision_execution_latest_trades_snapshot": (
                decision_dir / "execution_latest_trades_snapshot.json"
            ).as_posix()
            if decision_dir and (decision_dir / "execution_latest_trades_snapshot.json").exists()
            else None,
            "broker_fill_activities": (run_dir / "broker_fill_activities.json").as_posix()
            if (run_dir / "broker_fill_activities.json").exists()
            else None,
            "broker_account_activities": (run_dir / "broker_account_activities.json").as_posix()
            if (run_dir / "broker_account_activities.json").exists()
            else None,
            "broker_corporate_actions": (run_dir / "broker_corporate_actions.json").as_posix()
            if (run_dir / "broker_corporate_actions.json").exists()
            else None,
            "broker_order_snapshots": (run_dir / "broker_order_snapshots.json").as_posix()
            if (run_dir / "broker_order_snapshots.json").exists()
            else None,
            "broker_orders_all_before": (run_dir / "broker_orders_all_before.json").as_posix()
            if (run_dir / "broker_orders_all_before.json").exists()
            else None,
            "broker_orders_all_before_submit": (run_dir / "broker_orders_all_before_submit.json").as_posix()
            if (run_dir / "broker_orders_all_before_submit.json").exists()
            else None,
            "broker_orders_all_after_cancel": (run_dir / "broker_orders_all_after_cancel.json").as_posix()
            if (run_dir / "broker_orders_all_after_cancel.json").exists()
            else None,
            "broker_orders_all_after": (run_dir / "broker_orders_all_after.json").as_posix()
            if (run_dir / "broker_orders_all_after.json").exists()
            else None,
            "staged_rebuild_snapshots": (run_dir / "staged_rebuild_snapshots.json").as_posix()
            if (run_dir / "staged_rebuild_snapshots.json").exists()
            else None,
            "broker_cancel_all_orders_response": (run_dir / "broker_cancel_all_orders_response.json").as_posix()
            if (run_dir / "broker_cancel_all_orders_response.json").exists()
            else None,
            "broker_account_before": (run_dir / "broker_account_before.json").as_posix()
            if (run_dir / "broker_account_before.json").exists()
            else None,
            "broker_account_for_sizing": (run_dir / "broker_account_for_sizing.json").as_posix()
            if (run_dir / "broker_account_for_sizing.json").exists()
            else None,
            "broker_account_after": (run_dir / "broker_account_after.json").as_posix()
            if (run_dir / "broker_account_after.json").exists()
            else None,
            "broker_account_configurations_before": (
                run_dir / "broker_account_configurations_before.json"
            ).as_posix()
            if (run_dir / "broker_account_configurations_before.json").exists()
            else None,
            "broker_account_configurations_after": (
                run_dir / "broker_account_configurations_after.json"
            ).as_posix()
            if (run_dir / "broker_account_configurations_after.json").exists()
            else None,
            "broker_calendar_window": (run_dir / "broker_calendar_window.json").as_posix()
            if (run_dir / "broker_calendar_window.json").exists()
            else None,
            "broker_clock_before": (run_dir / "broker_clock_before.json").as_posix()
            if (run_dir / "broker_clock_before.json").exists()
            else None,
            "broker_clock_after": (run_dir / "broker_clock_after.json").as_posix()
            if (run_dir / "broker_clock_after.json").exists()
            else None,
            "broker_portfolio_history_before": (run_dir / "broker_portfolio_history_before.json").as_posix()
            if (run_dir / "broker_portfolio_history_before.json").exists()
            else None,
            "broker_portfolio_history_after": (run_dir / "broker_portfolio_history_after.json").as_posix()
            if (run_dir / "broker_portfolio_history_after.json").exists()
            else None,
            "broker_positions_before_raw": (run_dir / "broker_positions_before_raw.json").as_posix()
            if (run_dir / "broker_positions_before_raw.json").exists()
            else None,
            "broker_positions_after_raw": (run_dir / "broker_positions_after_raw.json").as_posix()
            if (run_dir / "broker_positions_after_raw.json").exists()
            else None,
            "broker_assets_active_us_equity": (run_dir / "broker_assets_active_us_equity.json").as_posix()
            if (run_dir / "broker_assets_active_us_equity.json").exists()
            else None,
            "broker_assets_relevant": (run_dir / "broker_assets_relevant.json").as_posix()
            if (run_dir / "broker_assets_relevant.json").exists()
            else None,
            "execution_price_snapshot": (run_dir / "execution_price_snapshot.json").as_posix()
            if (run_dir / "execution_price_snapshot.json").exists()
            else None,
            "execution_latest_trades_snapshot": (run_dir / "execution_latest_trades_snapshot.json").as_posix()
            if (run_dir / "execution_latest_trades_snapshot.json").exists()
            else None,
            "execution_latest_quotes_snapshot": (run_dir / "execution_latest_quotes_snapshot.json").as_posix()
            if (run_dir / "execution_latest_quotes_snapshot.json").exists()
            else None,
            "execution_latest_quotes_snapshot_after": (run_dir / "execution_latest_quotes_snapshot_after.json").as_posix()
            if (run_dir / "execution_latest_quotes_snapshot_after.json").exists()
            else None,
            "execution_intraday_bars_1min": (run_dir / "execution_intraday_bars_1min.json").as_posix()
            if (run_dir / "execution_intraday_bars_1min.json").exists()
            else None,
            "execution_intraday_bars_1min_after": (run_dir / "execution_intraday_bars_1min_after.json").as_posix()
            if (run_dir / "execution_intraday_bars_1min_after.json").exists()
            else None,
            "target_weights_snapshot": (run_dir / "target_weights_snapshot.json").as_posix()
            if (run_dir / "target_weights_snapshot.json").exists()
            else None,
            "executable_target_projection": (run_dir / "executable_target_projection.json").as_posix()
            if (run_dir / "executable_target_projection.json").exists()
            else None,
            "portfolio_weights_snapshot": (run_dir / "portfolio_weights_snapshot.json").as_posix()
            if (run_dir / "portfolio_weights_snapshot.json").exists()
            else None,
            "portfolio_weights_after_snapshot": (run_dir / "portfolio_weights_after_snapshot.json").as_posix()
            if (run_dir / "portfolio_weights_after_snapshot.json").exists()
            else None,
            "broker_position_account_stability_before": (
                run_dir / "broker_position_account_stability_before.json"
            ).as_posix()
            if (run_dir / "broker_position_account_stability_before.json").exists()
            else None,
            "broker_position_account_stability_after": (
                run_dir / "broker_position_account_stability_after.json"
            ).as_posix()
            if (run_dir / "broker_position_account_stability_after.json").exists()
            else None,
            "decision_targets": (run_dir / "decision_targets.csv").as_posix(),
            "alpha_panel": alpha_path.as_posix() if alpha_path else None,
            "lot_snapshot": lot_path.as_posix() if lot_path.exists() else None,
            "pre_trade_lot_snapshot": pre_trade_lot_path.as_posix() if pre_trade_lot_path else None,
            "pre_trade_lot_source": pre_trade_lot_source,
            "broker_positions_before": (run_dir / "broker_positions_before.csv").as_posix(),
            "broker_positions_after": (run_dir / "broker_positions_after.csv").as_posix(),
        },
        "run_dir_files": _inventory_files(run_dir, exclude_dirs={"audit"}),
        "decision_dir_files": _inventory_files(decision_dir, exclude_dirs={"audit"}) if decision_dir else [],
        "artifact_counts": {
            "target_symbols": len(targets),
            "alpha_symbols": len(alpha),
            "positions_before": len(before),
            "positions_after": len(after),
            "lot_rows": len(lot_rows),
            "order_rows": len(order_rows),
            "fill_rows": len(fill_rows),
            "realized_pnl_rows": len(realized_rows),
            "broker_order_snapshots": len(broker_order_snapshots.get("snapshots", []))
            if isinstance(broker_order_snapshots, dict)
            else 0,
            "order_poll_events": int(order_poll_timeline.get("event_count") or 0)
            if isinstance(order_poll_timeline, dict)
            else 0,
            "order_poll_rows": len(order_poll_rows),
            "decision_dir_files": len(_inventory_files(decision_dir, exclude_dirs={"audit"})) if decision_dir else 0,
            "data_quality_target_missing_alpha": data_quality.get("coverage", {}).get("target_symbols_missing_alpha")
            if isinstance(data_quality.get("coverage"), dict)
            else None,
            "position_reconciliation_material_residuals": reconciliation_summary.get(
                "symbols_with_material_unexplained_qty"
            ),
            "order_attempt_rows": len(order_attempt_rows),
            "broker_activity_rows": len(broker_activity_rows),
            "account_activity_attribution_rows": len(account_activity_attribution_rows),
            "broker_order_universe_rows": len(broker_order_universe_rows),
            "broker_order_universe_unique_orders": broker_order_universe_summary.get("unique_order_id_count"),
            "api_audit_request_rows": len(api_audit_rows),
            "staged_rebuild_rows": len(staged_rebuild_rows),
            "staged_rebuild_snapshots": staged_rebuild_summary.get("snapshot_count"),
            "execution_attribution_rows": len(execution_attribution_rows),
            "account_field_diff_rows": len(account_field_rows),
            "account_field_diff_key_deltas": len(account_field_summary.get("key_deltas", {}))
            if isinstance(account_field_summary.get("key_deltas"), dict)
            else 0,
            "account_config_diff_rows": len(account_config_rows),
            "account_config_changed_fields": account_config_summary.get("changed_field_count"),
            "run_evidence_digest_rows": len(run_evidence_digest_rows),
            "run_evidence_digest_missing_files": run_evidence_digest_summary.get("missing_file_count"),
            "run_evidence_digest_strict_missing_files": run_evidence_digest_summary.get("strict_missing_file_count"),
            "startup_binding_rows": len(startup_binding_rows),
            "startup_binding_issue_count": startup_binding_summary.get("issue_count"),
            "event_timeline_rows": len(event_timeline_rows),
            "symbol_attribution_rows": len(symbol_attribution_rows),
            "position_snapshot_integrity_rows": len(position_snapshot_integrity_rows),
            "target_transition_rows": len(target_transition_rows),
            "decision_intent_rows": len(decision_intent_rows),
            "order_constraint_rows": len(order_constraint_rows),
            "decision_execute_drift_rows": len(decision_execute_drift_rows),
            "market_price_evidence_rows": len(market_price_evidence_rows),
            "market_context_rows": len(market_context_rows),
            "intraday_bar_evidence_rows": len(intraday_bar_rows),
            "quote_evidence_rows": len(quote_rows),
            "corporate_action_rows": len(corporate_action_rows),
            "portfolio_history_rows": len(portfolio_history_rows),
            "calendar_rows": len(calendar_rows),
            "equity_pnl_bridge_components": len(equity_pnl_bridge.get("components", []))
            if "equity_pnl_bridge" in locals() and isinstance(equity_pnl_bridge.get("components"), list)
            else None,
        },
    }
    run_failure_rows, run_failure_summary = _build_run_failure_diagnosis(
        run_dir=run_dir,
        decision_dir=decision_dir,
        context=context,
        summary=summary if isinstance(summary, dict) else {},
        plan=plan if isinstance(plan, dict) else {},
        records=records if isinstance(records, list) else [],
        startup_binding_summary=startup_binding_summary,
        run_evidence_digest_summary=run_evidence_digest_summary,
    )
    context["artifact_counts"]["run_failure_diagnosis_rows"] = len(run_failure_rows)
    context["artifact_counts"]["run_failure_diagnosis_issues"] = run_failure_summary.get("issue_count")
    context["artifact_counts"]["run_failure_class"] = run_failure_summary.get("failure_class")
    risk = _risk_snapshot(summary, quality, decision_diag, decision_rows, lot_rows)
    equity_pnl_bridge = _build_equity_pnl_bridge(
        summary=summary if isinstance(summary, dict) else {},
        risk=risk,
        position_rows=position_rows,
        realized_summary=realized_summary,
        execution_attribution_summary=execution_attribution_summary,
        broker_activity_summary=broker_activity_summary,
        account_activity_attribution_summary=account_activity_attribution_summary,
    )
    account_state_bridge_rows, account_state_bridge_summary = _build_account_state_bridge(
        run_dir=run_dir,
        summary=summary if isinstance(summary, dict) else {},
        account_field_rows=account_field_rows,
        equity_pnl_bridge=equity_pnl_bridge,
    )
    residual_diagnosis_rows, residual_diagnosis_summary = _build_residual_diagnosis(
        reconciliation_rows=reconciliation_rows,
        equity_pnl_bridge=equity_pnl_bridge,
        account_field_summary=account_field_summary,
        account_state_bridge_summary=account_state_bridge_summary,
        position_snapshot_summary=position_snapshot_integrity_summary,
        corporate_action_summary=corporate_action_summary,
    )
    evidence_completeness_rows, evidence_completeness_summary = _build_evidence_completeness(
        context=context,
        summaries={
            "position_snapshot_integrity": position_snapshot_integrity_summary,
            "residual_diagnosis": residual_diagnosis_summary,
            "account_field_diff": account_field_summary,
            "account_config_diff": account_config_summary,
            "account_state_bridge": account_state_bridge_summary,
            "run_evidence_digest": run_evidence_digest_summary,
            "startup_binding": startup_binding_summary,
            "run_failure_diagnosis": run_failure_summary,
            "calendar_trace": calendar_summary,
        },
    )
    strict_attribution_checklist_rows, strict_attribution_checklist_summary = _build_strict_attribution_checklist(
        context=context,
        summaries={
            "position_snapshot_integrity": position_snapshot_integrity_summary,
            "residual_diagnosis": residual_diagnosis_summary,
            "account_field_diff": account_field_summary,
            "evidence_completeness": evidence_completeness_summary,
            "market_price_evidence": market_price_evidence_summary,
            "intraday_bar_evidence": intraday_bar_summary,
            "quote_evidence": quote_summary,
            "account_activity_attribution": account_activity_attribution_summary,
            "account_state_bridge": account_state_bridge_summary,
            "run_evidence_digest": run_evidence_digest_summary,
            "startup_binding": startup_binding_summary,
            "run_failure_diagnosis": run_failure_summary,
            "corporate_action_trace": corporate_action_summary,
            "portfolio_history_trace": portfolio_history_summary,
            "calendar_trace": calendar_summary,
            "decision_execute_drift": decision_execute_drift_summary,
        },
    )
    replay_focus_rows, attribution_dossier_summary = _build_attribution_dossier(
        context=context,
        summary=summary if isinstance(summary, dict) else {},
        equity_pnl_bridge=equity_pnl_bridge,
        symbol_attribution_rows=symbol_attribution_rows,
        target_transition_rows=target_transition_rows,
        decision_intent_rows=decision_intent_rows,
        order_constraint_rows=order_constraint_rows,
        decision_execute_drift_rows=decision_execute_drift_rows,
        market_price_evidence_rows=market_price_evidence_rows,
        intraday_bar_rows=intraday_bar_rows,
        quote_rows=quote_rows,
        market_context_rows=market_context_rows,
        market_context_summary=market_context_summary,
        residual_diagnosis_summary=residual_diagnosis_summary,
        evidence_completeness_summary=evidence_completeness_summary,
        strict_attribution_checklist_summary=strict_attribution_checklist_summary,
    )
    ideal_actual_gap_rows, ideal_actual_gap_summary = _build_ideal_vs_actual_gap(
        context=context,
        summary=summary if isinstance(summary, dict) else {},
        symbol_attribution_rows=symbol_attribution_rows,
        target_transition_rows=target_transition_rows,
        decision_intent_rows=decision_intent_rows,
        order_constraint_rows=order_constraint_rows,
        decision_execute_drift_rows=decision_execute_drift_rows,
        market_context_rows=market_context_rows,
        replay_focus_rows=replay_focus_rows,
    )
    if isinstance(context.get("artifact_counts"), dict):
        context["artifact_counts"]["equity_pnl_bridge_components"] = len(equity_pnl_bridge.get("components", []))
        context["artifact_counts"]["account_activity_attribution_rows"] = len(account_activity_attribution_rows)
        context["artifact_counts"]["account_field_diff_rows"] = len(account_field_rows)
        context["artifact_counts"]["account_state_bridge_rows"] = len(account_state_bridge_rows)
        context["artifact_counts"]["run_evidence_digest_rows"] = len(run_evidence_digest_rows)
        context["artifact_counts"]["event_timeline_rows"] = len(event_timeline_rows)
        context["artifact_counts"]["symbol_attribution_rows"] = len(symbol_attribution_rows)
        context["artifact_counts"]["position_snapshot_integrity_rows"] = len(position_snapshot_integrity_rows)
        context["artifact_counts"]["target_transition_rows"] = len(target_transition_rows)
        context["artifact_counts"]["decision_intent_rows"] = len(decision_intent_rows)
        context["artifact_counts"]["order_constraint_rows"] = len(order_constraint_rows)
        context["artifact_counts"]["decision_execute_drift_rows"] = len(decision_execute_drift_rows)
        context["artifact_counts"]["market_price_evidence_rows"] = len(market_price_evidence_rows)
        context["artifact_counts"]["market_context_rows"] = len(market_context_rows)
        context["artifact_counts"]["intraday_bar_evidence_rows"] = len(intraday_bar_rows)
        context["artifact_counts"]["quote_evidence_rows"] = len(quote_rows)
        context["artifact_counts"]["corporate_action_rows"] = len(corporate_action_rows)
        context["artifact_counts"]["portfolio_history_rows"] = len(portfolio_history_rows)
        context["artifact_counts"]["calendar_rows"] = len(calendar_rows)
        context["artifact_counts"]["residual_diagnosis_rows"] = len(residual_diagnosis_rows)
        context["artifact_counts"]["evidence_completeness_rows"] = len(evidence_completeness_rows)
        context["artifact_counts"]["strict_attribution_checklist_rows"] = len(strict_attribution_checklist_rows)
        context["artifact_counts"]["replay_focus_rows"] = len(replay_focus_rows)
        context["artifact_counts"]["ideal_actual_gap_rows"] = len(ideal_actual_gap_rows)
        context["artifact_counts"]["executable_target_projection_rows"] = len(executable_projection_rows)
    audit_checks = _build_audit_checks(
        context=context,
        summary=summary if isinstance(summary, dict) else {},
        records=records if isinstance(records, list) else [],
        order_rows=order_rows,
        fill_rows=fill_rows,
        realized_rows=realized_rows,
        order_poll_timeline=order_poll_timeline if isinstance(order_poll_timeline, dict) else {},
        order_poll_rows=order_poll_rows,
        broker_order_snapshots=broker_order_snapshots if isinstance(broker_order_snapshots, dict) else {},
        position_reconciliation_summary=reconciliation_summary,
        order_attempt_rows=order_attempt_rows,
        broker_activity_summary=broker_activity_summary,
        api_audit_summary=api_audit_summary,
        broker_order_universe_summary=broker_order_universe_summary,
        staged_rebuild_summary=staged_rebuild_summary,
        execution_attribution_summary=execution_attribution_summary,
        equity_pnl_bridge=equity_pnl_bridge,
        account_field_summary=account_field_summary,
        account_state_bridge_summary=account_state_bridge_summary,
        event_timeline_summary=event_timeline_summary,
        symbol_attribution_summary=symbol_attribution_summary,
        target_transition_summary=target_transition_summary,
        decision_intent_summary=decision_intent_summary,
        order_constraint_summary=order_constraint_summary,
        decision_execute_drift_summary=decision_execute_drift_summary,
        market_price_evidence_summary=market_price_evidence_summary,
        intraday_bar_summary=intraday_bar_summary,
        quote_summary=quote_summary,
        account_activity_attribution_summary=account_activity_attribution_summary,
        corporate_action_summary=corporate_action_summary,
        portfolio_history_summary=portfolio_history_summary,
        calendar_summary=calendar_summary,
        position_snapshot_integrity_summary=position_snapshot_integrity_summary,
        residual_diagnosis_summary=residual_diagnosis_summary,
        evidence_completeness_summary=evidence_completeness_summary,
        strict_attribution_checklist_summary=strict_attribution_checklist_summary,
        attribution_dossier_summary=attribution_dossier_summary,
        startup_binding_summary=startup_binding_summary,
        run_failure_summary=run_failure_summary,
    )

    _write_json(audit_dir / "00_run_context.json", context)
    _write_csv(audit_dir / "01_decision_trace.csv", decision_rows, [
        "symbol", "target_signed_weight", "target_side", "before_side", "after_side", "before_qty", "after_qty",
        "before_market_value", "after_market_value", "delta_market_value", "current_price", "avg_entry_price",
        "unrealized_intraday_pl_snapshot", "unrealized_pl_since_entry", "change_today", "sic2_sector", "beta",
        "composite_score", "composite_rank", "reversal_score", "momentum_score", "small_size_score", "low_beta_score",
        "cash_quality_score", "lot_total_weight", "lot_weight_reversal_score", "lot_weight_momentum_score",
        "lot_weight_small_size_score", "lot_weight_low_beta_score", "lot_weight_cash_quality_score",
    ])
    _write_csv(audit_dir / "02_lot_trace.csv", lot_rows, [
        "side", "symbol", "factor", "weight", "birth_idx", "session_idx", "age_sessions", "min_hold", "locked",
        "remaining_lock_sessions", "entry_session_date", "entry_time_utc",
    ])
    _write_csv(audit_dir / "03_order_trace.csv", order_rows, [
        "symbol", "side", "stage", "planned_qty", "planned_delta_notional", "reference_price", "sizing_price",
        "target_notional", "current_notional", "opening_short", "client_order_id", "order_id", "status_latest",
        "not_submitted_reason", "filled_qty", "remaining_qty", "filled_avg_price", "attempt_count",
        "submitted_at_utc", "updated_at", "slippage_bps", "filled_notional", "requested_qty",
        "submit_error_class", "broker_error_code", "broker_error_message", "broker_available_qty",
        "broker_existing_qty", "broker_held_for_orders_qty", "abort_remaining_orders", "error_type", "error",
    ])
    _write_csv(audit_dir / "04_position_pnl_snapshot.csv", position_rows, [
        "symbol", "side", "qty_before", "qty_after", "market_value_before", "market_value_after", "delta_market_value",
        "current_price_before", "current_price_after", "unrealized_intraday_pl_snapshot", "unrealized_pl_since_entry",
        "change_today",
    ])
    _write_csv(audit_dir / "05_fill_trace.csv", fill_rows, [
        "source", "fill_seq", "symbol", "side", "order_id", "client_order_id", "transaction_time", "qty", "price",
        "gross_amount", "net_amount", "raw_activity_id",
    ])
    _write_csv(audit_dir / "06_factor_attribution_snapshot.csv", factor_rows, [
        "level", "factor", "symbol", "symbol_side", "symbol_intraday_pnl_snapshot", "symbol_lot_total_weight",
        "factor_lot_weight", "factor_weight_share_in_symbol", "approx_intraday_pnl", "note",
    ])
    _write_json(audit_dir / "07_risk_snapshot.json", risk)
    realized_fields = [
        "ledger_seq", "session_date", "session_idx", "symbol", "fill_side", "closed_position_side", "action",
        "fill_id", "order_id", "client_order_id", "stage", "transaction_time", "fill_qty", "closed_qty",
        "opening_qty", "fill_price", "avg_entry_price_before", "cost_basis_source", "realized_pnl",
        "pnl_per_share", "gross_exit_notional", "reference_price", "slippage_bps", "lot_id", "factor",
        "lot_weight", "lot_birth_idx", "lot_min_hold", "lot_locked_at_close", "lot_entry_session_date",
        "lot_entry_time_utc", "pre_trade_lot_source", "strictness",
    ]
    _write_csv(audit_dir / "08_realized_pnl_ledger.csv", realized_rows, realized_fields)
    realized_summary_fields = [
        "factor", "symbol", "closed_position_side", "realized_pnl", "closed_qty", "opening_qty",
        "gross_exit_notional", "fill_row_count", "close_row_count",
    ]
    _write_csv(audit_dir / "09_factor_realized_pnl.csv", factor_realized_rows, realized_summary_fields)
    _write_csv(audit_dir / "10_symbol_realized_pnl.csv", symbol_realized_rows, realized_summary_fields)
    _write_json(audit_dir / "11_realized_pnl_summary.json", realized_summary)
    order_poll_fields = [
        "timeline_row", "record_index", "record_symbol", "record_side", "record_stage",
        "record_execution_order_style", "record_client_order_id", "record_order_id", "attempt_index",
        "attempt_no", "attempt_client_order_id", "attempt_order_id", "attempt_limit_price",
        "attempt_offset_bps", "seq", "event", "at_utc", "elapsed_ms", "request_elapsed_ms",
        "order_id", "client_order_id", "symbol", "side", "order_type", "time_in_force", "status",
        "qty", "filled_qty", "remaining_qty", "filled_avg_price", "limit_price", "submitted_at",
        "updated_at", "filled_at", "canceled_at", "expired_at", "failed_at", "terminal_status",
        "deadline_reached", "seconds_to_deadline", "cancel_requested_at_utc", "error_type", "error",
    ]
    _write_csv(audit_dir / "13_order_poll_timeline.csv", order_poll_rows, order_poll_fields)
    _write_json(audit_dir / "14_audit_checks.json", audit_checks)
    _write_json(audit_dir / "15_data_quality_snapshot.json", data_quality)
    reconciliation_fields = [
        "symbol", "before_signed_qty", "after_signed_qty", "observed_delta_qty", "fill_net_signed_qty",
        "expected_after_qty_from_fills", "unexplained_qty", "unexplained_abs_qty",
        "unexplained_notional_at_snapshot_price", "snapshot_price_used", "fill_abs_qty", "fill_count",
        "fill_notional_abs", "planned_net_signed_qty", "planned_abs_qty", "planned_notional_abs",
        "material_unexplained_qty", "residual_reason_hint", "before_side", "after_side",
    ]
    _write_csv(audit_dir / "16_position_reconciliation.csv", reconciliation_rows, reconciliation_fields)
    _write_json(audit_dir / "17_position_reconciliation_summary.json", reconciliation_summary)
    attempt_fields = [
        "record_index", "symbol", "side", "stage", "release_round", "execution_order_style",
        "record_client_order_id", "record_order_id", "record_status_latest", "record_qty",
        "record_filled_qty", "record_remaining_qty", "record_reference_price", "record_delta_notional",
        "record_submitted_at_utc", "record_updated_at", "record_attempt_count", "attempt_index",
        "attempt_no", "attempt_client_order_id", "attempt_order_id", "qty_submitted", "limit_price",
        "offset_bps", "requote_step_index", "requote_cycle", "max_offset_bps", "status_latest",
        "filled_qty", "remaining_qty_estimate", "filled_avg_price", "updated_at", "broker_fill_count",
        "broker_fill_qty", "broker_fill_vwap", "broker_fill_notional", "poll_event_count",
        "requested_qty", "submit_error_class", "broker_error_code", "broker_error_message",
        "broker_available_qty", "broker_existing_qty", "broker_held_for_orders_qty",
        "abort_remaining_orders", "error_type", "error",
    ]
    _write_csv(audit_dir / "18_order_attempt_trace.csv", order_attempt_rows, attempt_fields)
    broker_activity_fields = [
        "source", "matched_scope", "activity_id", "activity_type", "type", "transaction_time", "symbol",
        "side", "qty", "price", "order_id", "order_status", "leaves_qty", "cum_qty", "net_amount",
        "gross_amount", "in_execution_records",
    ]
    _write_csv(audit_dir / "19_broker_activity_trace.csv", broker_activity_rows, broker_activity_fields)
    _write_json(audit_dir / "20_broker_activity_summary.json", broker_activity_summary)
    api_request_fields = [
        "seq", "attempt", "max_retries", "started_at_utc", "method", "url", "ok", "elapsed_ms",
        "status_code", "url_host", "url_path", "url_query_count", "url_query_keys",
        "request_payload_type", "request_payload_key_count", "request_payload_keys",
        "request_payload_bytes", "request_payload_sha256", "request_payload_preview",
        "request_payload_preview_truncated", "response_body_bytes", "response_body_sha256",
        "response_type", "response_key_count", "response_keys", "error_type", "error",
    ]
    _write_csv(audit_dir / "21_api_audit_requests.csv", api_audit_rows, api_request_fields)
    _write_json(audit_dir / "22_api_audit_summary.json", api_audit_summary)
    broker_order_universe_fields = [
        "source", "source_index", "order_id", "client_order_id", "symbol", "side", "order_type",
        "time_in_force", "status", "qty", "filled_qty", "filled_avg_price", "limit_price", "notional",
        "created_at", "submitted_at", "updated_at", "filled_at", "canceled_at", "expired_at",
        "failed_at", "in_execution_records",
    ]
    _write_csv(audit_dir / "23_broker_order_universe.csv", broker_order_universe_rows, broker_order_universe_fields)
    _write_json(audit_dir / "24_broker_order_universe_summary.json", broker_order_universe_summary)
    staged_rebuild_fields = [
        "snapshot_index", "snapshot_type", "captured_at_utc", "stage", "round", "session_token",
        "limit_base_offset_bps", "input_order_count", "input_symbols", "submitted_record_count",
        "submitted_status_counts", "submitted_filled_record_count", "submitted_filled_qty",
        "submitted_filled_notional", "refreshed_position_count", "refreshed_signed_notional_symbols",
        "refreshed_price_count", "buying_power", "buying_power_source", "account_equity",
        "account_equity_source", "rebuilt_all_order_count", "rebuilt_release_order_count",
        "rebuilt_stage_order_count", "rebuilt_release_residual_count", "entry_order_count_before_cap",
        "final_entry_order_count", "rebuilt_skipped_count", "cap_scaled_count", "cap_skipped_count",
        "cap_estimated_used", "cap", "remaining_order_count", "remaining_symbols", "fully_filled",
        "entry_abort_reason", "entry_submission_skipped_reason",
    ]
    _write_csv(audit_dir / "25_staged_rebuild_trace.csv", staged_rebuild_rows, staged_rebuild_fields)
    _write_json(audit_dir / "26_staged_rebuild_summary.json", staged_rebuild_summary)
    execution_attribution_fields = [
        "record_index", "attempt_index", "symbol", "side", "stage", "release_round",
        "client_order_id", "order_id", "status_latest", "outcome", "reference_price",
        "sizing_price", "limit_price", "limit_aggressiveness_bps", "attempt_offset_bps",
        "requote_step_index", "requote_cycle", "max_offset_bps", "submitted_qty", "filled_qty",
        "filled_avg_price", "broker_fill_count",
        "filled_notional_at_reference", "filled_notional_actual", "implementation_shortfall_bps",
        "implementation_shortfall_notional", "poll_event_count", "updated_at",
    ]
    _write_csv(
        audit_dir / "27_execution_attribution_trace.csv",
        execution_attribution_rows,
        execution_attribution_fields,
    )
    _write_json(audit_dir / "28_execution_attribution_summary.json", execution_attribution_summary)
    equity_bridge_fields = ["component", "amount", "method", "strictness"]
    _write_csv(audit_dir / "29_equity_pnl_bridge.csv", _equity_pnl_bridge_rows(equity_pnl_bridge), equity_bridge_fields)
    _write_json(audit_dir / "30_equity_pnl_bridge.json", equity_pnl_bridge)
    account_field_fields = [
        "field", "before", "after", "before_num", "after_num", "delta",
        "source_before", "source_after", "tracked_field",
    ]
    _write_csv(audit_dir / "31_account_field_diff.csv", account_field_rows, account_field_fields)
    _write_json(audit_dir / "32_account_field_diff_summary.json", account_field_summary)
    event_timeline_fields = [
        "timeline_seq", "at_utc", "source", "event_type", "symbol", "order_id",
        "client_order_id", "stage", "severity", "detail", "payload",
    ]
    _write_csv(audit_dir / "33_event_timeline.csv", event_timeline_rows, event_timeline_fields)
    _write_json(audit_dir / "34_event_timeline_summary.json", event_timeline_summary)
    symbol_attribution_fields = [
        "symbol", "target_signed_weight", "target_side", "before_side", "after_side",
        "before_market_value", "after_market_value", "delta_market_value", "snapshot_intraday_pnl",
        "realized_pnl_estimate", "closed_qty", "opening_qty", "realized_ledger_rows",
        "implementation_shortfall_notional", "implementation_shortfall_bps_weighted",
        "filled_notional_at_reference", "execution_attempt_rows", "fill_count", "fill_abs_qty",
        "fill_notional_abs", "planned_abs_notional", "order_rows", "position_unexplained_qty",
        "position_unexplained_abs_qty", "position_unexplained_notional", "position_residual_reason_hint",
    ]
    _write_csv(audit_dir / "35_symbol_attribution_bridge.csv", symbol_attribution_rows, symbol_attribution_fields)
    _write_json(audit_dir / "36_symbol_attribution_summary.json", symbol_attribution_summary)
    position_snapshot_integrity_fields = [
        "check", "status", "severity", "observed", "expected", "detail", "examples",
    ]
    _write_csv(
        audit_dir / "37_position_snapshot_integrity.csv",
        position_snapshot_integrity_rows,
        position_snapshot_integrity_fields,
    )
    _write_json(audit_dir / "37_position_snapshot_integrity.json", position_snapshot_integrity_summary)
    residual_diagnosis_fields = [
        "diagnosis_type", "reason", "status", "severity", "symbol_count", "amount",
        "abs_amount", "evidence_artifacts", "examples", "interpretation", "next_action",
    ]
    _write_csv(audit_dir / "38_residual_diagnosis.csv", residual_diagnosis_rows, residual_diagnosis_fields)
    _write_json(audit_dir / "38_residual_diagnosis.json", residual_diagnosis_summary)
    evidence_completeness_fields = [
        "area", "status", "present_count", "expected_count", "coverage_ratio",
        "present_artifacts", "missing_artifacts", "row_count_context", "note",
    ]
    _write_csv(audit_dir / "39_evidence_completeness.csv", evidence_completeness_rows, evidence_completeness_fields)
    _write_json(audit_dir / "39_evidence_completeness.json", evidence_completeness_summary)
    target_transition_fields = [
        "symbol", "intent", "outcome", "confidence", "target_side", "before_side", "after_side",
        "before_market_value", "target_market_value_estimate", "after_market_value",
        "desired_delta_market_value", "planned_delta_notional", "observed_delta_market_value",
        "target_error_market_value", "target_error_abs", "target_error_bps_of_equity",
        "planned_order_count", "fill_count", "fill_net_signed_qty", "fill_abs_qty",
        "fill_abs_notional", "position_residual_reason_hint", "material_position_residual",
        "position_unexplained_qty", "position_unexplained_notional",
    ]
    _write_csv(audit_dir / "40_target_transition_trace.csv", target_transition_rows, target_transition_fields)
    _write_json(audit_dir / "41_target_transition_summary.json", target_transition_summary)
    decision_intent_fields = [
        "symbol", "raw_target_signed_weight", "projected_target_signed_weight", "projection_delta_weight",
        "raw_target_notional_estimate", "projected_target_notional_estimate",
        "projection_delta_notional_estimate", "projection_reason", "raw_target_side",
        "projected_target_side", "before_side", "after_side", "before_market_value",
        "after_market_value", "desired_delta_notional_estimate", "planned_delta_notional",
        "planned_abs_notional", "planned_order_count", "filled_qty_from_order_trace",
        "remaining_qty_from_order_trace", "order_intent_status", "skip_reason", "skip_count",
        "min_trade_notional", "composite_score", "reversal_score", "momentum_score",
        "small_size_score", "low_beta_score", "cash_quality_score", "beta", "sic2_sector",
        "lot_total_weight", "lot_weight_reversal_score", "lot_weight_momentum_score",
        "lot_weight_small_size_score", "lot_weight_low_beta_score", "lot_weight_cash_quality_score",
    ]
    _write_csv(audit_dir / "42_decision_intent_trace.csv", decision_intent_rows, decision_intent_fields)
    _write_json(audit_dir / "43_decision_intent_summary.json", decision_intent_summary)
    order_constraint_fields = [
        "row_type", "plan_order_index", "symbol", "side", "action_class", "current_notional",
        "target_notional", "delta_notional", "planned_abs_notional", "reference_price",
        "sizing_price", "sizing_offset_bps_estimate", "raw_qty_estimate", "planned_qty",
        "qty_rounding_loss", "qty_decimals", "min_trade_notional", "estimated_notional_at_reference",
        "estimated_notional_at_sizing", "whole_share_required", "whole_share_reason",
        "opening_short", "short_sale_estimate", "skipped", "skip_reason", "execution_trace_rows",
        "execution_stages", "status_latest_set", "filled_qty", "remaining_qty",
        "filled_notional_estimate", "unfilled_notional_estimate", "fill_count", "fill_abs_qty",
        "fill_abs_notional", "composite_score", "constraint_notes",
    ]
    _write_csv(audit_dir / "44_order_constraint_trace.csv", order_constraint_rows, order_constraint_fields)
    _write_json(audit_dir / "45_order_constraint_summary.json", order_constraint_summary)
    decision_execute_drift_fields = [
        "symbol", "order_presence", "skip_presence", "drift_reasons",
        "decision_raw_target_signed_weight", "execute_raw_target_signed_weight",
        "raw_target_weight_delta", "decision_projected_target_signed_weight",
        "execute_projected_target_signed_weight", "projected_target_weight_delta",
        "decision_target_notional_estimate", "execute_target_notional_estimate",
        "target_notional_delta_estimate", "decision_order_count", "execute_order_count",
        "decision_planned_delta_notional", "execute_planned_delta_notional",
        "planned_delta_notional_change", "decision_planned_abs_notional",
        "execute_planned_abs_notional", "decision_current_notional", "execute_current_notional",
        "current_notional_change", "decision_reference_price", "execute_reference_price",
        "reference_price_change", "reference_price_change_bps", "decision_qty",
        "execute_qty", "qty_change", "decision_skip_count", "execute_skip_count",
        "decision_skip_reasons", "execute_skip_reasons",
    ]
    _write_csv(audit_dir / "46_decision_execute_drift.csv", decision_execute_drift_rows, decision_execute_drift_fields)
    _write_json(audit_dir / "47_decision_execute_drift_summary.json", decision_execute_drift_summary)
    market_price_evidence_fields = [
        "symbol", "status", "in_decision_target_or_position", "in_execute_target_symbols",
        "in_execute_broker_position_before", "decision_feed", "execute_feed",
        "decision_price_snapshot_exists", "execute_price_snapshot_exists",
        "decision_snapshot_collected_at_utc", "execute_snapshot_collected_at_utc",
        "decision_plan_reference_price", "execute_plan_reference_price",
        "decision_snapshot_reference_price", "execute_snapshot_reference_price",
        "decision_reference_price_used", "execute_reference_price_used",
        "decision_fallback_price", "execute_fallback_price", "execute_reference_source_inferred",
        "decision_latest_trade_price", "execute_latest_trade_price", "decision_latest_trade_time",
        "execute_latest_trade_time", "execute_latest_trade_exchange", "execute_latest_trade_size",
        "execute_latest_trade_conditions", "reference_vs_fallback_bps",
        "latest_trade_vs_reference_bps", "decision_execute_reference_change_bps",
        "execute_planned_delta_notional", "execute_planned_abs_notional",
        "fill_count", "fill_abs_notional", "fill_vwap", "fill_vwap_vs_reference_bps",
        "missing_reference_flag",
    ]
    _write_csv(audit_dir / "48_market_price_evidence.csv", market_price_evidence_rows, market_price_evidence_fields)
    _write_json(audit_dir / "49_market_price_evidence_summary.json", market_price_evidence_summary)
    market_context_fields = [
        "row_type", "bucket", "symbol", "target_side", "after_side", "sic2_sector", "primary_factor",
        "factor_weights", "beta", "beta_bucket", "after_market_value", "gross_after_market_value",
        "net_after_market_value", "signed_beta_exposure", "change_today", "snapshot_intraday_pnl",
        "realized_pnl_estimate", "snapshot_plus_realized_pnl", "pnl_bps_of_gross_after_market_value",
        "implementation_shortfall_notional", "position_unexplained_notional", "filled_notional_at_reference",
        "execute_reference_price", "decision_execute_reference_change_bps", "symbol_count", "long_symbol_count",
        "short_symbol_count", "largest_losses", "bar_count", "first_bar_time", "last_bar_time", "open",
        "high", "low", "close", "vwap", "range_bps", "close_vs_open_bps", "context_note",
    ]
    _write_csv(audit_dir / "66_market_context_attribution.csv", market_context_rows, market_context_fields)
    _write_json(audit_dir / "67_market_context_summary.json", market_context_summary)
    replay_focus_fields = [
        "focus_rank", "symbol", "focus_score", "focus_tags", "primary_attribution_bucket",
        "next_review_action", "snapshot_plus_realized_pnl", "snapshot_intraday_pnl",
        "realized_pnl_estimate", "implementation_shortfall_notional",
        "position_unexplained_notional", "position_residual_reason_hint",
        "material_position_residual", "target_intent", "target_outcome",
        "target_confidence", "target_error_market_value", "target_error_abs",
        "planned_order_count", "fill_count", "planned_abs_notional",
        "unfilled_notional_estimate", "skipped_order_count",
        "whole_share_required_count", "order_status_latest_set", "skip_reasons",
        "action_classes", "projection_reason", "raw_target_signed_weight",
        "projected_target_signed_weight", "projection_delta_notional_estimate",
        "decision_execute_order_presence", "decision_execute_drift_reasons",
        "decision_execute_planned_delta_change", "decision_execute_target_notional_delta",
        "market_price_status", "decision_execute_reference_change_bps",
        "fill_vwap_vs_reference_bps", "missing_reference_flag",
        "intraday_bar_status", "intraday_range_bps", "fill_vwap_vs_vwap_adverse_bps",
        "quote_status", "spread_bps", "fill_vwap_vs_mid_adverse_bps",
        "after_side", "target_side", "sic2_sector", "primary_factor", "beta",
        "beta_bucket", "change_today", "after_market_value", "evidence_gap_count",
        "evidence_gap_tags", "supporting_artifacts",
    ]
    _write_csv(audit_dir / "68_replay_focus_trace.csv", replay_focus_rows, replay_focus_fields)
    _write_json(audit_dir / "69_attribution_dossier.json", attribution_dossier_summary)
    ideal_actual_gap_fields = [
        "gap_rank", "symbol", "primary_gap_bucket", "performance_drag_bucket", "gap_score",
        "raw_target_signed_weight", "projected_target_signed_weight", "execute_projected_target_signed_weight",
        "raw_target_notional_estimate", "projected_target_notional_estimate",
        "execute_target_notional_estimate", "after_market_value",
        "ideal_actual_gap", "ideal_actual_gap_abs", "after_projected_target_gap",
        "after_projected_target_gap_abs", "after_execute_target_gap", "after_execute_target_gap_abs",
        "projection_reason", "projection_gap_notional", "projection_gap_abs",
        "order_intent_status", "decision_execute_drift_reasons", "decision_execute_order_presence",
        "decision_execute_target_drift", "decision_execute_target_drift_abs",
        "decision_execute_order_drift", "decision_execute_order_drift_abs",
        "submitted_order_count", "skipped_order_count", "whole_share_required_count",
        "submitted_planned_abs_notional", "submitted_filled_notional", "submitted_unfilled_notional",
        "skipped_notional", "gross_order_gap_notional", "submitted_fill_rate_notional",
        "order_action_classes", "order_status_latest_set", "skip_reasons", "whole_share_reasons",
        "target_intent", "target_outcome", "target_confidence",
        "snapshot_intraday_pnl", "realized_pnl_estimate", "snapshot_plus_realized_pnl", "pnl_loss_abs",
        "implementation_shortfall_notional", "position_unexplained_notional", "material_position_residual",
        "after_side", "target_side", "sic2_sector", "primary_factor", "beta", "beta_bucket", "change_today",
        "focus_rank", "focus_tags", "evidence_gap_count", "evidence_gap_tags", "diagnostic_tags",
        "supporting_artifacts",
    ]
    _write_csv(audit_dir / "78_ideal_vs_actual_gap.csv", ideal_actual_gap_rows, ideal_actual_gap_fields)
    _write_json(audit_dir / "79_ideal_vs_actual_gap_summary.json", ideal_actual_gap_summary)
    executable_projection_fields = [
        "projection_phase", "symbol", "target_side", "raw_target_signed_weight",
        "raw_target_notional", "reference_price", "current_signed_qty", "current_signed_notional",
        "raw_target_abs_qty", "target_lattice_abs_qty", "target_lattice_signed_qty",
        "short_position_residual_qty", "expected_final_signed_qty",
        "executable_expected_signed_weight", "projection_weight_gap", "projection_notional_gap",
        "estimated_entry_qty", "estimated_entry_buying_power", "buying_power_price",
        "integer_target_required", "constraint_reasons", "buying_power", "buying_power_buffer",
        "buying_power_cap", "estimated_entry_buying_power_used_total",
    ]
    _write_csv(
        audit_dir / "80_executable_target_projection.csv",
        executable_projection_rows,
        executable_projection_fields,
    )
    _write_json(
        audit_dir / "81_executable_target_projection_summary.json",
        executable_projection_summary,
    )
    _write_json(audit_dir / "82_position_capacity_summary.json", position_capacity_summary)
    intraday_bar_fields = [
        "symbol", "status", "source_used", "before_bar_count", "after_bar_count", "bar_count",
        "first_bar_time", "last_bar_time", "open", "high", "low", "close", "vwap",
        "volume", "trade_count", "range_bps", "close_vs_open_bps",
        "execute_reference_price", "reference_vs_open_bps", "reference_vs_vwap_bps",
        "reference_vs_close_bps", "reference_position_in_range_pct",
        "execute_latest_trade_price", "latest_trade_position_in_range_pct",
        "fill_count", "fill_abs_qty", "fill_abs_notional", "fill_vwap",
        "primary_fill_side", "first_fill_time", "last_fill_time",
        "fill_vwap_vs_open_bps", "fill_vwap_vs_vwap_adverse_bps",
        "fill_vwap_vs_close_adverse_bps", "fill_position_in_range_pct",
        "price_path_hints",
    ]
    _write_csv(audit_dir / "58_intraday_bar_evidence.csv", intraday_bar_rows, intraday_bar_fields)
    _write_json(audit_dir / "59_intraday_bar_summary.json", intraday_bar_summary)
    quote_fields = [
        "symbol", "status", "source_used", "quote_time", "before_quote_time", "after_quote_time",
        "before_mid_price", "after_mid_price", "before_spread_bps", "after_spread_bps",
        "bid_price", "ask_price", "bid_size", "ask_size",
        "mid_price", "spread", "spread_bps", "conditions", "tape",
        "execute_reference_price", "reference_vs_mid_bps", "latest_trade_price",
        "latest_trade_vs_mid_bps", "fill_count", "fill_vwap", "primary_fill_side",
        "fill_vwap_vs_mid_adverse_bps", "price_microstructure_hints",
    ]
    _write_csv(audit_dir / "60_quote_evidence.csv", quote_rows, quote_fields)
    _write_json(audit_dir / "61_quote_summary.json", quote_summary)
    account_activity_attribution_fields = [
        "row_index", "source", "matched_scope", "activity_id", "activity_type", "type",
        "activity_class", "strategy_pnl_bucket", "known_equity_impact_used_in_bridge",
        "transaction_time", "symbol", "side", "qty", "price", "order_id",
        "in_execution_records", "net_amount", "gross_amount", "expected_equity_impact_amount", "note",
    ]
    _write_csv(
        audit_dir / "50_account_activity_attribution.csv",
        account_activity_attribution_rows,
        account_activity_attribution_fields,
    )
    _write_json(audit_dir / "51_account_activity_attribution_summary.json", account_activity_attribution_summary)
    strict_attribution_checklist_fields = [
        "area", "item", "status", "severity", "blocking_strict_attribution", "present",
        "expected", "observed", "evidence_artifacts", "detail", "next_action",
    ]
    _write_csv(
        audit_dir / "52_strict_attribution_checklist.csv",
        strict_attribution_checklist_rows,
        strict_attribution_checklist_fields,
    )
    _write_json(audit_dir / "53_strict_attribution_checklist_summary.json", strict_attribution_checklist_summary)
    corporate_action_fields = [
        "row_index", "symbol", "action_type", "status", "in_session_window", "date_fields",
        "ex_date", "record_date", "payable_date", "process_date", "cash_amount",
        "old_rate", "new_rate", "ratio", "position_residual_flag", "position_residual_qty",
        "position_residual_notional", "price_evidence_status", "decision_execute_reference_change_bps",
        "matching_account_activity_rows", "matching_account_activity_net_amount", "causal_hint", "raw_action",
    ]
    _write_csv(audit_dir / "54_corporate_action_trace.csv", corporate_action_rows, corporate_action_fields)
    _write_json(audit_dir / "55_corporate_action_summary.json", corporate_action_summary)
    portfolio_history_fields = [
        "source", "row_index", "timestamp_raw", "timestamp_utc", "equity",
        "profit_loss", "profit_loss_pct", "base_value", "equity_minus_base_value",
    ]
    _write_csv(audit_dir / "56_portfolio_history_trace.csv", portfolio_history_rows, portfolio_history_fields)
    _write_json(audit_dir / "57_portfolio_history_summary.json", portfolio_history_summary)
    calendar_fields = [
        "row_index", "date", "open", "close", "session_open", "session_close",
        "is_session_date", "is_observed_execute_dir", "is_half_day", "regular_session_minutes",
        "days_from_session", "expected_previous_trading_date", "expected_next_trading_date", "raw_row",
    ]
    _write_csv(audit_dir / "62_calendar_trace.csv", calendar_rows, calendar_fields)
    _write_json(audit_dir / "63_calendar_summary.json", calendar_summary)
    account_state_bridge_fields = [
        "group", "field", "before", "after", "before_num", "after_num", "delta", "abs_delta",
        "direction", "source_before", "source_after", "used_in_equity_bridge", "interpretation",
    ]
    _write_csv(audit_dir / "64_account_state_bridge.csv", account_state_bridge_rows, account_state_bridge_fields)
    _write_json(audit_dir / "65_account_state_bridge_summary.json", account_state_bridge_summary)
    account_config_fields = ["field", "before", "after", "changed", "source_before", "source_after"]
    _write_csv(audit_dir / "70_account_config_diff.csv", account_config_rows, account_config_fields)
    _write_json(audit_dir / "71_account_config_summary.json", account_config_summary)
    run_evidence_digest_fields = [
        "artifact", "exists", "strict_replay_input", "bytes", "sha256", "payload_count",
        "line_count", "parse_error_count", "status", "path",
    ]
    _write_csv(audit_dir / "73_run_evidence_digest_checks.csv", run_evidence_digest_rows, run_evidence_digest_fields)
    _write_json(audit_dir / "72_run_evidence_digest_summary.json", run_evidence_digest_summary)
    startup_binding_fields = [
        "area", "item", "status", "severity", "observed", "expected", "evidence_path", "detail",
    ]
    _write_csv(audit_dir / "74_startup_binding_checks.csv", startup_binding_rows, startup_binding_fields)
    _write_json(audit_dir / "75_startup_binding_summary.json", startup_binding_summary)
    run_failure_fields = [
        "area", "item", "status", "severity", "observed", "expected",
        "evidence_path", "detail", "next_action",
    ]
    _write_csv(audit_dir / "76_run_failure_diagnosis.csv", run_failure_rows, run_failure_fields)
    _write_json(audit_dir / "77_run_failure_diagnosis_summary.json", run_failure_summary)
    _write_review(
        audit_dir / "daily_review.md",
        context,
        risk,
        position_rows,
        order_rows,
        realized_summary,
        audit_checks,
        equity_pnl_bridge,
        account_state_bridge_summary,
        market_context_summary,
        attribution_dossier_summary,
        ideal_actual_gap_summary,
        target_transition_summary,
        decision_intent_summary,
        order_constraint_summary,
        decision_execute_drift_summary,
        market_price_evidence_summary,
        intraday_bar_summary,
        quote_summary,
        account_activity_attribution_summary,
        corporate_action_summary,
        portfolio_history_summary,
        calendar_summary,
        position_snapshot_integrity_summary,
        residual_diagnosis_summary,
        evidence_completeness_summary,
        strict_attribution_checklist_summary,
        execution_attribution_summary=execution_attribution_summary,
    )
    audit_manifest = _write_audit_manifest(audit_dir, run_dir, context, decision_dir)

    return {
        "run_dir": run_dir.as_posix(),
        "audit_dir": audit_dir.as_posix(),
        "session_date": session_date,
        "decision_rows": len(decision_rows),
        "lot_rows": len(lot_rows),
        "order_rows": len(order_rows),
        "fill_rows": len(fill_rows),
        "realized_pnl_rows": len(realized_rows),
        "position_rows": len(position_rows),
        "order_poll_rows": len(order_poll_rows),
        "data_quality_target_missing_alpha": data_quality.get("coverage", {}).get("target_symbols_missing_alpha")
        if isinstance(data_quality.get("coverage"), dict)
        else None,
        "position_reconciliation_material_residuals": reconciliation_summary.get(
            "symbols_with_material_unexplained_qty"
        ),
        "order_attempt_rows": len(order_attempt_rows),
        "broker_activity_rows": len(broker_activity_rows),
        "broker_order_universe_rows": len(broker_order_universe_rows),
        "broker_order_universe_unique_orders": broker_order_universe_summary.get("unique_order_id_count"),
        "api_audit_request_rows": len(api_audit_rows),
        "staged_rebuild_rows": len(staged_rebuild_rows),
        "staged_rebuild_snapshots": staged_rebuild_summary.get("snapshot_count"),
        "execution_attribution_rows": len(execution_attribution_rows),
        "equity_pnl_bridge_components": len(equity_pnl_bridge.get("components", [])),
        "account_field_diff_rows": len(account_field_rows),
        "account_state_bridge_rows": len(account_state_bridge_rows),
        "account_state_bridge_status": account_state_bridge_summary.get("status"),
        "account_state_equity_delta": account_state_bridge_summary.get("equity_delta"),
        "account_state_cash_delta": account_state_bridge_summary.get("cash_delta"),
        "account_state_gross_exposure_delta": account_state_bridge_summary.get("gross_exposure_delta"),
        "account_state_equity_delta_vs_summary_delta": account_state_bridge_summary.get("equity_delta_vs_summary_delta"),
        "account_field_diff_key_deltas": len(account_field_summary.get("key_deltas", {}))
        if isinstance(account_field_summary.get("key_deltas"), dict)
        else 0,
        "account_config_status": account_config_summary.get("status"),
        "account_config_changed_fields": account_config_summary.get("changed_field_count"),
        "run_evidence_digest_status": run_evidence_digest_summary.get("status"),
        "run_evidence_digest_digest_exists": run_evidence_digest_summary.get("digest_exists"),
        "run_evidence_digest_coverage_ratio": run_evidence_digest_summary.get("coverage_ratio"),
        "run_evidence_digest_missing_files": run_evidence_digest_summary.get("missing_file_count"),
        "run_evidence_digest_strict_missing_files": run_evidence_digest_summary.get("strict_missing_file_count"),
        "run_evidence_digest_api_audit_line_count": run_evidence_digest_summary.get("api_audit_line_count"),
        "run_evidence_digest_run_event_count": run_evidence_digest_summary.get("run_event_count"),
        "run_evidence_digest_hash_manifest_file_count": run_evidence_digest_summary.get(
            "file_hash_manifest_file_count"
        ),
        "run_evidence_digest_artifact_completeness_status": run_evidence_digest_summary.get(
            "artifact_completeness_status"
        ),
        "run_evidence_digest_artifact_completeness_partial_category_count": run_evidence_digest_summary.get(
            "artifact_completeness_partial_category_count"
        ),
        "startup_binding_status": startup_binding_summary.get("status"),
        "startup_binding_issue_count": startup_binding_summary.get("issue_count"),
        "startup_autostart_registered": startup_binding_summary.get("autostart_registered"),
        "startup_process_health_status": startup_binding_summary.get("process_health_status"),
        "startup_scheduler_due_latest_exists": startup_binding_summary.get("scheduler_due_latest_exists"),
        "startup_scheduler_runtime_latest_exists": startup_binding_summary.get("scheduler_runtime_latest_exists"),
        "event_timeline_rows": len(event_timeline_rows),
        "symbol_attribution_rows": len(symbol_attribution_rows),
        "target_transition_rows": len(target_transition_rows),
        "target_transition_status": target_transition_summary.get("status"),
        "target_transition_attention_symbols": target_transition_summary.get("attention_symbol_count"),
        "decision_intent_rows": len(decision_intent_rows),
        "decision_intent_status": decision_intent_summary.get("status"),
        "order_constraint_rows": len(order_constraint_rows),
        "order_constraint_skipped_orders": order_constraint_summary.get("skipped_order_count"),
        "decision_execute_drift_rows": len(decision_execute_drift_rows),
        "decision_execute_drift_material_symbols": decision_execute_drift_summary.get("material_changed_symbol_count"),
        "market_price_evidence_rows": len(market_price_evidence_rows),
        "market_price_missing_reference_symbols": market_price_evidence_summary.get(
            "execute_missing_reference_symbol_count"
        ),
        "market_price_large_reference_moves": market_price_evidence_summary.get(
            "large_decision_execute_reference_move_count"
        ),
        "market_context_rows": len(market_context_rows),
        "market_context_status": market_context_summary.get("status"),
        "market_context_snapshot_plus_realized_pnl": market_context_summary.get("snapshot_plus_realized_pnl"),
        "market_context_net_beta_exposure_to_gross": market_context_summary.get("net_beta_exposure_to_gross"),
        "market_context_benchmark_symbols_with_bars": len(
            market_context_summary.get("benchmark_symbols_with_bars", [])
        )
        if isinstance(market_context_summary.get("benchmark_symbols_with_bars"), list)
        else 0,
        "replay_focus_rows": len(replay_focus_rows),
        "attribution_dossier_status": attribution_dossier_summary.get("status"),
        "attribution_dossier_top_focus_symbols": len(
            attribution_dossier_summary.get("top_focus_symbols", [])
        )
        if isinstance(attribution_dossier_summary.get("top_focus_symbols"), list)
        else 0,
        "attribution_dossier_primary_bucket_counts": attribution_dossier_summary.get("primary_bucket_counts"),
        "ideal_actual_gap_rows": len(ideal_actual_gap_rows),
        "ideal_actual_gap_status": ideal_actual_gap_summary.get("status"),
        "ideal_actual_gap_material_symbols": ideal_actual_gap_summary.get("material_gap_symbol_count"),
        "ideal_actual_gap_gross_abs": ideal_actual_gap_summary.get("gross_ideal_actual_gap_abs"),
        "ideal_actual_gap_order_gap_notional": ideal_actual_gap_summary.get("gross_order_gap_notional"),
        "ideal_actual_gap_primary_bucket_counts": ideal_actual_gap_summary.get("primary_gap_bucket_counts"),
        "executable_target_projection_rows": len(executable_projection_rows),
        "executable_target_projection_status": executable_projection_summary.get("status"),
        "executable_target_projection_weight_error_l1": executable_projection_summary.get(
            "tracking_error_l1_weight"
        ),
        "executable_target_projection_buying_power_utilization": executable_projection_summary.get(
            "buying_power_cap_utilization"
        ),
        "ideal_actual_gap_performance_drag_bucket_counts": ideal_actual_gap_summary.get(
            "performance_drag_bucket_counts"
        ),
        "intraday_bar_evidence_rows": len(intraday_bar_rows),
        "intraday_bar_status": intraday_bar_summary.get("status"),
        "intraday_bar_missing_symbols": intraday_bar_summary.get("missing_bar_symbol_count"),
        "intraday_bar_filled_symbols_missing": intraday_bar_summary.get("filled_symbols_missing_bars_count"),
        "intraday_bar_error_count": intraday_bar_summary.get("error_count"),
        "intraday_bar_max_range_bps": intraday_bar_summary.get("max_intraday_range_bps"),
        "quote_evidence_rows": len(quote_rows),
        "quote_status": quote_summary.get("status"),
        "quote_missing_symbols": quote_summary.get("missing_quote_symbol_count"),
        "quote_invalid_symbols": quote_summary.get("invalid_quote_symbol_count"),
        "quote_wide_spread_symbols": quote_summary.get("wide_spread_symbol_count"),
        "quote_error_count": quote_summary.get("error_count"),
        "quote_max_spread_bps": quote_summary.get("max_spread_bps"),
        "calendar_rows": len(calendar_rows),
        "calendar_status": calendar_summary.get("status"),
        "calendar_session_date_in_calendar": calendar_summary.get("session_date_in_calendar"),
        "calendar_expected_previous_trading_date": calendar_summary.get("expected_previous_trading_date"),
        "calendar_expected_next_trading_date": calendar_summary.get("expected_next_trading_date"),
        "calendar_session_is_half_day": calendar_summary.get("session_is_half_day"),
        "calendar_error_count": calendar_summary.get("error_count"),
        "account_activity_attribution_rows": len(account_activity_attribution_rows),
        "account_activity_unknown_net_amount": account_activity_attribution_summary.get("unknown_activity_net_amount"),
        "corporate_action_rows": len(corporate_action_rows),
        "corporate_action_status": corporate_action_summary.get("status"),
        "corporate_action_matched_position_residual_symbols": corporate_action_summary.get(
            "matched_position_residual_symbol_count"
        ),
        "corporate_action_error_count": corporate_action_summary.get("error_count"),
        "portfolio_history_rows": len(portfolio_history_rows),
        "portfolio_history_status": portfolio_history_summary.get("status"),
        "portfolio_history_summary_vs_after_delta": portfolio_history_summary.get("summary_vs_history_after_delta"),
        "strict_attribution_blocking_items": strict_attribution_checklist_summary.get("blocking_item_count"),
        "strict_attribution_ready": strict_attribution_checklist_summary.get("strict_attribution_ready"),
        "position_snapshot_integrity_status": position_snapshot_integrity_summary.get("status"),
        "residual_diagnosis_status": residual_diagnosis_summary.get("status"),
        "evidence_strict_account_position_replay_ready": evidence_completeness_summary.get(
            "strict_account_position_replay_ready"
        ),
        "audit_check_status": audit_checks.get("status"),
        "audit_check_issues": audit_checks.get("issue_count"),
        "audit_manifest_files": audit_manifest.get("audit_file_count"),
    }


def generate_task_health_audit(task_dir: Path) -> dict[str, Any]:
    task_dir = task_dir.resolve()
    audit_dir = task_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    startup_binding_rows, startup_binding_summary = _build_startup_binding_audit(task_dir.parent)
    summary = _read_json(task_dir / "execution_summary.json", {})
    plan = _read_json(task_dir / "order_plan.json", {})
    records = _read_json(task_dir / "execution_records.json", [])
    run_evidence_digest_rows, run_evidence_digest_summary = _build_run_evidence_digest_audit(task_dir)
    context = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_dir": task_dir.as_posix(),
        "decision_dir": task_dir.as_posix() if task_dir.name.endswith("_decision") else None,
        "session_date": str(summary.get("decision_date") or plan.get("decision_date") or task_dir.name.split("_")[0]),
        "artifacts": {
            "execution_summary": (task_dir / "execution_summary.json").as_posix()
            if (task_dir / "execution_summary.json").exists()
            else None,
            "order_plan": (task_dir / "order_plan.json").as_posix()
            if (task_dir / "order_plan.json").exists()
            else None,
            "execution_records": (task_dir / "execution_records.json").as_posix()
            if (task_dir / "execution_records.json").exists()
            else None,
            "decision_targets": (task_dir / "decision_targets.csv").as_posix()
            if (task_dir / "decision_targets.csv").exists()
            else None,
            "run_context": (task_dir / "run_context.json").as_posix()
            if (task_dir / "run_context.json").exists()
            else None,
            "scheduler_task_context": (task_dir / "scheduler_task_context.json").as_posix()
            if (task_dir / "scheduler_task_context.json").exists()
            else None,
            "scheduler_task_result": (task_dir / "scheduler_task_result.json").as_posix()
            if (task_dir / "scheduler_task_result.json").exists()
            else None,
            "run_evidence_digest": (task_dir / "run_evidence_digest.json").as_posix()
            if (task_dir / "run_evidence_digest.json").exists()
            else None,
        },
        "artifact_counts": {
            "execution_records": len(records) if isinstance(records, list) else 0,
            "run_evidence_digest_rows": len(run_evidence_digest_rows),
            "startup_binding_rows": len(startup_binding_rows),
        },
    }
    run_failure_rows, run_failure_summary = _build_run_failure_diagnosis(
        run_dir=task_dir,
        decision_dir=task_dir if task_dir.name.endswith("_decision") else _infer_decision_dir(task_dir),
        context=context,
        summary=summary if isinstance(summary, dict) else {},
        plan=plan if isinstance(plan, dict) else {},
        records=records if isinstance(records, list) else [],
        startup_binding_summary=startup_binding_summary,
        run_evidence_digest_summary=run_evidence_digest_summary,
    )
    startup_binding_fields = [
        "area", "item", "status", "severity", "observed", "expected", "evidence_path", "detail",
    ]
    run_failure_fields = [
        "area", "item", "status", "severity", "observed", "expected",
        "evidence_path", "detail", "next_action",
    ]
    _write_json(audit_dir / "00_run_context.json", context)
    _write_csv(audit_dir / "73_run_evidence_digest_checks.csv", run_evidence_digest_rows, [
        "artifact", "exists", "strict_replay_input", "bytes", "sha256", "payload_count",
        "line_count", "parse_error_count", "status", "path",
    ])
    _write_json(audit_dir / "72_run_evidence_digest_summary.json", run_evidence_digest_summary)
    _write_csv(audit_dir / "74_startup_binding_checks.csv", startup_binding_rows, startup_binding_fields)
    _write_json(audit_dir / "75_startup_binding_summary.json", startup_binding_summary)
    _write_csv(audit_dir / "76_run_failure_diagnosis.csv", run_failure_rows, run_failure_fields)
    _write_json(audit_dir / "77_run_failure_diagnosis_summary.json", run_failure_summary)
    return {
        "run_dir": task_dir.as_posix(),
        "audit_dir": audit_dir.as_posix(),
        "session_date": context.get("session_date"),
        "run_failure_status": run_failure_summary.get("status"),
        "run_failure_class": run_failure_summary.get("failure_class"),
        "run_failure_error_type": run_failure_summary.get("error_type"),
        "startup_binding_status": startup_binding_summary.get("status"),
    }


def _execute_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*_execute") if p.is_dir() and _is_valid_execute_dir(p))


def _task_dirs(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.glob("*_*")
        if p.is_dir() and (p.name.endswith("_decision") or p.name.endswith("_execute"))
    )


def _needs_task_health_audit(task_dir: Path) -> bool:
    result = _read_json(task_dir / "scheduler_task_result.json", {})
    summary = _read_json(task_dir / "execution_summary.json", {})
    status = _task_payload_status(result) or _task_payload_status(summary)
    if status in {"failed", "error", "started", "running"}:
        return True
    if isinstance(summary, dict) and summary.get("ok") is False:
        return True
    if task_dir.name.endswith("_decision") and ((task_dir / "scheduler_task_result.json").exists() or summary):
        return True
    return False


def _is_valid_execute_dir(run_dir: Path) -> bool:
    if not run_dir.is_dir() or not run_dir.name.endswith("_execute"):
        return False
    indicators = [
        "execution_summary.json",
        "scheduler_task_result.json",
        "scheduler_task_context.json",
        "run_context.json",
        "order_plan.json",
        "execution_records.json",
        "broker_account_before.json",
        "broker_positions_before.csv",
    ]
    return any((run_dir / name).exists() for name in indicators)


def _signed_position_qty_from_row(row: dict[str, Any]) -> float:
    side = str(row.get("side") or "").lower()
    qty = _safe_float(row.get("qty"))
    return -abs(qty) if side == "short" else abs(qty)


def _signed_position_mv_from_row(row: dict[str, Any]) -> float:
    raw_value = row.get("market_value")
    value = _safe_float(raw_value)
    side = str(row.get("side") or "").lower()
    if side == "short" and value > 0:
        return -abs(value)
    return value


def _position_continuity_maps(path: Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv_rows(path)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        out[symbol] = {
            "symbol": symbol,
            "side": str(row.get("side") or "").lower(),
            "signed_qty": _signed_position_qty_from_row(row),
            "signed_market_value": _signed_position_mv_from_row(row),
            "current_price": _safe_float(row.get("current_price")),
        }
    return out


def _build_cross_day_continuity(root: Path, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    continuity_rows: list[dict[str, Any]] = []
    for prev_row, next_row in zip(rows, rows[1:]):
        prev_run = Path(str(prev_row.get("run_dir") or ""))
        next_run = Path(str(next_row.get("run_dir") or ""))
        prev_after = _position_continuity_maps(prev_run / "broker_positions_after.csv")
        next_before = _position_continuity_maps(next_run / "broker_positions_before.csv")
        symbols = sorted(set(prev_after) | set(next_before))
        qty_abs_gap = 0.0
        mv_abs_gap = 0.0
        symbol_gap_count = 0
        largest_symbol_gaps: list[dict[str, Any]] = []
        for symbol in symbols:
            prev_pos = prev_after.get(symbol, {})
            next_pos = next_before.get(symbol, {})
            qty_gap = _safe_float(next_pos.get("signed_qty")) - _safe_float(prev_pos.get("signed_qty"))
            mv_gap = _safe_float(next_pos.get("signed_market_value")) - _safe_float(prev_pos.get("signed_market_value"))
            qty_abs_gap += abs(qty_gap)
            mv_abs_gap += abs(mv_gap)
            if abs(qty_gap) > 1e-6:
                symbol_gap_count += 1
                largest_symbol_gaps.append(
                    {
                        "symbol": symbol,
                        "prev_after_signed_qty": _safe_float(prev_pos.get("signed_qty")),
                        "next_before_signed_qty": _safe_float(next_pos.get("signed_qty")),
                        "qty_gap": qty_gap,
                        "prev_after_signed_market_value": _safe_float(prev_pos.get("signed_market_value")),
                        "next_before_signed_market_value": _safe_float(next_pos.get("signed_market_value")),
                        "market_value_gap": mv_gap,
                    }
                )
        calendar_gap_days = 0
        try:
            calendar_gap_days = (
                datetime.strptime(str(next_row.get("session_date")), "%Y-%m-%d").date()
                - datetime.strptime(str(prev_row.get("session_date")), "%Y-%m-%d").date()
            ).days
        except Exception:
            pass
        equity_gap = _safe_float(next_row.get("equity_before")) - _safe_float(prev_row.get("equity_after"))
        continuity_rows.append(
            {
                "previous_session_date": prev_row.get("session_date", ""),
                "next_session_date": next_row.get("session_date", ""),
                "calendar_gap_days": calendar_gap_days,
                "previous_after_equity": _safe_float(prev_row.get("equity_after")),
                "next_before_equity": _safe_float(next_row.get("equity_before")),
                "overnight_equity_gap": equity_gap,
                "previous_after_position_symbols": len(prev_after),
                "next_before_position_symbols": len(next_before),
                "position_symbol_union_count": len(symbols),
                "symbols_with_qty_gap": symbol_gap_count,
                "total_abs_qty_gap": qty_abs_gap,
                "total_abs_market_value_gap": mv_abs_gap,
                "largest_symbol_qty_gaps": _json_cell(
                    sorted(largest_symbol_gaps, key=lambda item: abs(_safe_float(item.get("qty_gap"))), reverse=True)[:20]
                ),
                "status": "attention" if symbol_gap_count else "pass",
                "note": (
                    "Compares previous execute after-snapshot to next execute before-snapshot. "
                    "Equity/market-value gaps are informational market-mark drift; status is based on signed quantity gaps."
                ),
            }
        )

    issue_rows = [row for row in continuity_rows if row.get("status") != "pass"]
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": root.as_posix(),
        "pair_count": len(continuity_rows),
        "issue_pair_count": len(issue_rows),
        "status_counts": dict(sorted(Counter(str(row.get("status") or "__missing__") for row in continuity_rows).items())),
        "largest_overnight_equity_gaps": sorted(
            continuity_rows,
            key=lambda row: abs(_safe_float(row.get("overnight_equity_gap"))),
            reverse=True,
        )[:10],
        "largest_position_qty_gaps": sorted(
            continuity_rows,
            key=lambda row: _safe_float(row.get("total_abs_qty_gap")),
            reverse=True,
        )[:10],
        "issues": issue_rows,
        "note": (
            "Equity gaps across days can include market movement, fees, transfers, dividends, and broker marks outside "
            "execute windows. Continuity status is based on signed quantity gaps, not mark-to-market movement."
        ),
    }
    return continuity_rows, summary


def _build_official_calendar_gaps(rows: list[dict[str, Any]]) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    limited_pairs: list[dict[str, Any]] = []
    for prev_row, next_row in zip(rows, rows[1:]):
        prev_date = _normalize_date_text(prev_row.get("session_date"))
        next_date = _normalize_date_text(next_row.get("session_date"))
        expected_next = _normalize_date_text(prev_row.get("calendar_expected_next_trading_date"))
        prev_status = str(prev_row.get("calendar_status") or "")
        next_status = str(next_row.get("calendar_status") or "")
        if not expected_next or prev_status == "historical_limited" or next_status == "historical_limited":
            limited_pairs.append(
                {
                    "previous_session_date": prev_date,
                    "next_session_date": next_date,
                    "calendar_status_previous": prev_status,
                    "calendar_status_next": next_status,
                    "reason": "official_calendar_evidence_missing_or_historical_limited",
                }
            )
            continue
        if expected_next != next_date:
            calendar_dates = []
            prev_run = Path(str(prev_row.get("run_dir") or ""))
            raw_summary = _read_json(prev_run / "audit" / "63_calendar_summary.json", {})
            if isinstance(raw_summary, dict) and isinstance(raw_summary.get("calendar_dates"), list):
                calendar_dates = [str(item) for item in raw_summary.get("calendar_dates") if str(item)]
            missing = [date for date in calendar_dates if expected_next <= date < next_date]
            gaps.append(
                {
                    "previous_session_date": prev_date,
                    "next_session_date": next_date,
                    "expected_next_trading_date": expected_next,
                    "official_missing_trading_dates": missing,
                    "official_missing_trading_day_count": len(missing) if missing else 1,
                    "calendar_status_previous": prev_status,
                    "calendar_status_next": next_status,
                    "note": "Alpaca official calendar expected at least one trading session before the next observed execute dir.",
                }
            )
    return {
        "schema_version": "1.0",
        "status": "attention" if gaps else "historical_limited" if limited_pairs else "pass",
        "gap_count": len(gaps),
        "limited_pair_count": len(limited_pairs),
        "gaps": gaps,
        "limited_pairs": limited_pairs[:50],
        "note": "Uses captured Alpaca calendar windows. Historical pairs without raw calendar evidence remain limited.",
    }


def generate_rollup(root: Path = SCHED_ROOT) -> dict[str, Any]:
    root = root.resolve()
    rows: list[dict[str, Any]] = []
    for run_dir in _execute_dirs(root):
        audit_dir = run_dir / "audit"
        summary = _read_json(run_dir / "execution_summary.json", {})
        checks = _read_json(audit_dir / "14_audit_checks.json", {})
        bridge = _read_json(audit_dir / "30_equity_pnl_bridge.json", {})
        exec_attr = _read_json(audit_dir / "28_execution_attribution_summary.json", {})
        data_quality = _read_json(audit_dir / "15_data_quality_snapshot.json", {})
        recon = _read_json(audit_dir / "17_position_reconciliation_summary.json", {})
        activity = _read_json(audit_dir / "20_broker_activity_summary.json", {})
        symbol_attr = _read_json(audit_dir / "36_symbol_attribution_summary.json", {})
        position_integrity = _read_json(audit_dir / "37_position_snapshot_integrity.json", {})
        residual_diag = _read_json(audit_dir / "38_residual_diagnosis.json", {})
        evidence = _read_json(audit_dir / "39_evidence_completeness.json", {})
        target_transition = _read_json(audit_dir / "41_target_transition_summary.json", {})
        decision_intent = _read_json(audit_dir / "43_decision_intent_summary.json", {})
        order_constraints = _read_json(audit_dir / "45_order_constraint_summary.json", {})
        decision_execute_drift = _read_json(audit_dir / "47_decision_execute_drift_summary.json", {})
        market_price = _read_json(audit_dir / "49_market_price_evidence_summary.json", {})
        intraday_bars = _read_json(audit_dir / "59_intraday_bar_summary.json", {})
        quotes = _read_json(audit_dir / "61_quote_summary.json", {})
        account_activity_attr = _read_json(audit_dir / "51_account_activity_attribution_summary.json", {})
        strict_checklist = _read_json(audit_dir / "53_strict_attribution_checklist_summary.json", {})
        corporate_action = _read_json(audit_dir / "55_corporate_action_summary.json", {})
        portfolio_history = _read_json(audit_dir / "57_portfolio_history_summary.json", {})
        calendar = _read_json(audit_dir / "63_calendar_summary.json", {})
        account_state = _read_json(audit_dir / "65_account_state_bridge_summary.json", {})
        market_context = _read_json(audit_dir / "67_market_context_summary.json", {})
        attribution_dossier = _read_json(audit_dir / "69_attribution_dossier.json", {})
        ideal_actual_gap = _read_json(audit_dir / "79_ideal_vs_actual_gap_summary.json", {})
        executable_projection = _read_json(
            audit_dir / "81_executable_target_projection_summary.json",
            {},
        )
        position_capacity = _read_json(audit_dir / "82_position_capacity_summary.json", {})
        account_config = _read_json(audit_dir / "71_account_config_summary.json", {})
        run_evidence_digest = _read_json(audit_dir / "72_run_evidence_digest_summary.json", {})
        startup_binding = _read_json(audit_dir / "75_startup_binding_summary.json", {})
        run_failure = _read_json(audit_dir / "77_run_failure_diagnosis_summary.json", {})
        component_amounts = bridge.get("component_amounts", {}) if isinstance(bridge, dict) else {}
        coverage = data_quality.get("coverage", {}) if isinstance(data_quality, dict) else {}
        symbol_totals = symbol_attr.get("totals", {}) if isinstance(symbol_attr, dict) else {}
        rows.append(
            {
                "run_dir": run_dir.as_posix(),
                "session_date": _normalize_date_text(summary.get("decision_date") or bridge.get("session_date") or run_dir.name[:8]),
                "session_idx": _safe_int(summary.get("session_idx")),
                "audit_status": checks.get("status") if isinstance(checks, dict) else "",
                "audit_issues": _safe_int(checks.get("issue_count")) if isinstance(checks, dict) else 0,
                "equity_before": _safe_float(summary.get("account_equity")),
                "equity_after": _safe_float(summary.get("account_equity_post_trade")),
                "equity_change": _safe_float(component_amounts.get("broker_equity_change")),
                "snapshot_intraday_pnl": _safe_float(component_amounts.get("snapshot_unrealized_intraday_pnl")),
                "realized_pnl_estimate": _safe_float(component_amounts.get("realized_pnl_estimate")),
                "execution_shortfall_cost_estimate": _safe_float(component_amounts.get("execution_shortfall_cost_estimate")),
                "equity_bridge_residual": _safe_float(
                    component_amounts.get("unexplained_after_snapshot_intraday_realized_activity")
                ),
                "account_state_bridge_status": account_state.get("status")
                if isinstance(account_state, dict)
                else "",
                "account_state_equity_delta": _optional_float(account_state.get("equity_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_cash_delta": _optional_float(account_state.get("cash_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_gross_exposure_delta": _optional_float(account_state.get("gross_exposure_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_net_exposure_delta": _optional_float(account_state.get("net_exposure_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_buying_power_delta": _optional_float(account_state.get("buying_power_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_maintenance_margin_delta": _optional_float(account_state.get("maintenance_margin_delta"))
                if isinstance(account_state, dict)
                else None,
                "account_state_equity_delta_vs_summary_delta": _optional_float(
                    account_state.get("equity_delta_vs_summary_delta")
                )
                if isinstance(account_state, dict)
                else None,
                "account_state_equity_delta_vs_equity_bridge": _optional_float(
                    account_state.get("equity_delta_vs_equity_bridge_change")
                )
                if isinstance(account_state, dict)
                else None,
                "account_config_status": account_config.get("status")
                if isinstance(account_config, dict)
                else "",
                "account_config_changed_fields": _safe_int(account_config.get("changed_field_count"))
                if isinstance(account_config, dict)
                else 0,
                "run_evidence_digest_status": run_evidence_digest.get("status")
                if isinstance(run_evidence_digest, dict)
                else "",
                "run_evidence_digest_digest_exists": bool(run_evidence_digest.get("digest_exists"))
                if isinstance(run_evidence_digest, dict)
                else False,
                "run_evidence_digest_coverage_ratio": _optional_float(run_evidence_digest.get("coverage_ratio"))
                if isinstance(run_evidence_digest, dict)
                else None,
                "run_evidence_digest_missing_files": _safe_int(run_evidence_digest.get("missing_file_count"))
                if isinstance(run_evidence_digest, dict)
                else 0,
                "run_evidence_digest_strict_missing_files": _safe_int(
                    run_evidence_digest.get("strict_missing_file_count")
                )
                if isinstance(run_evidence_digest, dict)
                else 0,
                "run_evidence_digest_api_audit_line_count": _safe_int(run_evidence_digest.get("api_audit_line_count"))
                if isinstance(run_evidence_digest, dict)
                else 0,
                "run_evidence_digest_run_event_count": _safe_int(run_evidence_digest.get("run_event_count"))
                if isinstance(run_evidence_digest, dict)
                else 0,
                "run_evidence_digest_hash_manifest_file_count": _safe_int(
                    run_evidence_digest.get("file_hash_manifest_file_count")
                )
                if isinstance(run_evidence_digest, dict)
                else 0,
                "run_evidence_digest_artifact_completeness_status": run_evidence_digest.get(
                    "artifact_completeness_status"
                )
                if isinstance(run_evidence_digest, dict)
                else "",
                "run_evidence_digest_artifact_completeness_partial_category_count": _safe_int(
                    run_evidence_digest.get("artifact_completeness_partial_category_count")
                )
                if isinstance(run_evidence_digest, dict)
                else 0,
                "startup_binding_status": startup_binding.get("status")
                if isinstance(startup_binding, dict)
                else "",
                "startup_binding_issue_count": _safe_int(startup_binding.get("issue_count"))
                if isinstance(startup_binding, dict)
                else 0,
                "startup_autostart_registered": bool(startup_binding.get("autostart_registered"))
                if isinstance(startup_binding, dict)
                else False,
                "startup_process_health_status": startup_binding.get("process_health_status")
                if isinstance(startup_binding, dict)
                else "",
                "startup_scheduler_due_latest_exists": bool(startup_binding.get("scheduler_due_latest_exists"))
                if isinstance(startup_binding, dict)
                else False,
                "startup_scheduler_runtime_latest_exists": bool(startup_binding.get("scheduler_runtime_latest_exists"))
                if isinstance(startup_binding, dict)
                else False,
                "startup_due_session_date": (
                    startup_binding.get("due_latest", {}).get("due_session_date")
                    if isinstance(startup_binding.get("due_latest"), dict)
                    else ""
                )
                if isinstance(startup_binding, dict)
                else "",
                "startup_due_decision_status": (
                    startup_binding.get("due_latest", {}).get("due_decision_status_before")
                    if isinstance(startup_binding.get("due_latest"), dict)
                    else ""
                )
                if isinstance(startup_binding, dict)
                else "",
                "startup_due_execute_status": (
                    startup_binding.get("due_latest", {}).get("due_execute_status_before")
                    if isinstance(startup_binding.get("due_latest"), dict)
                    else ""
                )
                if isinstance(startup_binding, dict)
                else "",
                "run_failure_status": run_failure.get("status")
                if isinstance(run_failure, dict)
                else "",
                "run_failure_task_status": run_failure.get("task_status")
                if isinstance(run_failure, dict)
                else "",
                "run_failure_class": run_failure.get("failure_class")
                if isinstance(run_failure, dict)
                else "",
                "run_failure_error_type": run_failure.get("error_type")
                if isinstance(run_failure, dict)
                else "",
                "run_failure_error": run_failure.get("error")
                if isinstance(run_failure, dict)
                else "",
                "run_failure_missing_core_artifacts": _json_cell(run_failure.get("missing_core_artifacts"))
                if isinstance(run_failure, dict)
                else "",
                "market_context_status": market_context.get("status")
                if isinstance(market_context, dict)
                else "",
                "market_context_snapshot_plus_realized_pnl": _safe_float(
                    market_context.get("snapshot_plus_realized_pnl")
                )
                if isinstance(market_context, dict)
                else 0.0,
                "market_context_net_beta_exposure_to_gross": _optional_float(
                    market_context.get("net_beta_exposure_to_gross")
                )
                if isinstance(market_context, dict)
                else None,
                "market_context_benchmark_symbols_with_bars": len(
                    market_context.get("benchmark_symbols_with_bars", [])
                )
                if isinstance(market_context, dict) and isinstance(market_context.get("benchmark_symbols_with_bars"), list)
                else 0,
                "attribution_dossier_status": attribution_dossier.get("status")
                if isinstance(attribution_dossier, dict)
                else "",
                "attribution_focus_symbol_count": _safe_int(attribution_dossier.get("focus_symbol_count"))
                if isinstance(attribution_dossier, dict)
                else 0,
                "attribution_top_focus_symbol_count": len(attribution_dossier.get("top_focus_symbols", []))
                if isinstance(attribution_dossier, dict) and isinstance(attribution_dossier.get("top_focus_symbols"), list)
                else 0,
                "attribution_evidence_gap_count": _safe_int(attribution_dossier.get("evidence_gap_count"))
                if isinstance(attribution_dossier, dict)
                else 0,
                "attribution_primary_bucket_counts": _json_cell(attribution_dossier.get("primary_bucket_counts"))
                if isinstance(attribution_dossier, dict)
                else "",
                "ideal_actual_gap_status": ideal_actual_gap.get("status")
                if isinstance(ideal_actual_gap, dict)
                else "",
                "ideal_actual_gap_material_symbols": _safe_int(ideal_actual_gap.get("material_gap_symbol_count"))
                if isinstance(ideal_actual_gap, dict)
                else 0,
                "ideal_actual_gap_gross_abs": _safe_float(ideal_actual_gap.get("gross_ideal_actual_gap_abs"))
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_after_projected_abs": _safe_float(
                    ideal_actual_gap.get("gross_after_projected_target_gap_abs")
                )
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_projection_abs": _safe_float(ideal_actual_gap.get("gross_projection_gap_abs"))
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_decision_target_drift_abs": _safe_float(
                    ideal_actual_gap.get("gross_decision_execute_target_drift_abs")
                )
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_decision_order_drift_abs": _safe_float(
                    ideal_actual_gap.get("gross_decision_execute_order_drift_abs")
                )
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_submitted_unfilled_notional": _safe_float(
                    ideal_actual_gap.get("gross_submitted_unfilled_notional")
                )
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_skipped_notional": _safe_float(ideal_actual_gap.get("gross_skipped_notional"))
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_order_gap_notional": _safe_float(ideal_actual_gap.get("gross_order_gap_notional"))
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_pnl_loss_abs": _safe_float(ideal_actual_gap.get("gross_pnl_loss_abs"))
                if isinstance(ideal_actual_gap, dict)
                else 0.0,
                "ideal_actual_gap_primary_bucket_counts": _json_cell(ideal_actual_gap.get("primary_gap_bucket_counts"))
                if isinstance(ideal_actual_gap, dict)
                else "",
                "ideal_actual_gap_performance_drag_bucket_counts": _json_cell(
                    ideal_actual_gap.get("performance_drag_bucket_counts")
                )
                if isinstance(ideal_actual_gap, dict)
                else "",
                "strategy_to_actual_weight_error_l1": _optional_float(
                    ideal_actual_gap.get("strategy_to_actual_weight_error_l1")
                )
                if isinstance(ideal_actual_gap, dict)
                else None,
                "strategy_to_executable_weight_error_l1": _optional_float(
                    ideal_actual_gap.get("strategy_to_executable_weight_error_l1")
                )
                if isinstance(ideal_actual_gap, dict)
                else None,
                "executable_to_actual_weight_error_l1": _optional_float(
                    ideal_actual_gap.get("executable_to_actual_weight_error_l1")
                )
                if isinstance(ideal_actual_gap, dict)
                else None,
                "mean_symbol_strategy_to_actual_weight_error": _optional_float(
                    ideal_actual_gap.get("mean_symbol_strategy_to_actual_weight_error")
                )
                if isinstance(ideal_actual_gap, dict)
                else None,
                "max_symbol_strategy_to_actual_weight_error": _optional_float(
                    ideal_actual_gap.get("max_symbol_strategy_to_actual_weight_error")
                )
                if isinstance(ideal_actual_gap, dict)
                else None,
                "executable_projection_status": executable_projection.get("status")
                if isinstance(executable_projection, dict)
                else "",
                "optimizer_tracking_error_l1_weight": _optional_float(
                    executable_projection.get("tracking_error_l1_weight")
                )
                if isinstance(executable_projection, dict)
                else None,
                "optimizer_mean_abs_symbol_weight_error": _optional_float(
                    executable_projection.get("mean_abs_symbol_weight_error")
                )
                if isinstance(executable_projection, dict)
                else None,
                "optimizer_max_abs_symbol_weight_error": _optional_float(
                    executable_projection.get("max_abs_symbol_weight_error")
                )
                if isinstance(executable_projection, dict)
                else None,
                "optimizer_buying_power_cap_utilization": _optional_float(
                    executable_projection.get("buying_power_cap_utilization")
                )
                if isinstance(executable_projection, dict)
                else None,
                "position_capacity_status": position_capacity.get("status")
                if isinstance(position_capacity, dict)
                else "",
                "gross_position_notional": _optional_float(
                    position_capacity.get("gross_position_notional")
                )
                if isinstance(position_capacity, dict)
                else None,
                "regt_buying_power_remaining": _optional_float(
                    position_capacity.get("regt_buying_power_remaining")
                )
                if isinstance(position_capacity, dict)
                else None,
                "total_regt_buying_power_capacity": _optional_float(
                    position_capacity.get("total_regt_buying_power_capacity")
                )
                if isinstance(position_capacity, dict)
                else None,
                "configured_buying_power_target_ratio": _optional_float(
                    position_capacity.get("configured_buying_power_target_ratio")
                )
                if isinstance(position_capacity, dict)
                else None,
                "configured_gross_target_notional": _optional_float(
                    position_capacity.get("configured_gross_target_notional")
                )
                if isinstance(position_capacity, dict)
                else None,
                "gross_utilization_of_total_bp": _optional_float(
                    position_capacity.get("gross_utilization_of_total_bp")
                )
                if isinstance(position_capacity, dict)
                else None,
                "gross_error_vs_target_notional": _optional_float(
                    position_capacity.get("gross_error_vs_target_notional")
                )
                if isinstance(position_capacity, dict)
                else None,
                "gross_error_vs_target_pct_points": _optional_float(
                    position_capacity.get("gross_error_vs_target_pct_points")
                )
                if isinstance(position_capacity, dict)
                else None,
                "gross_error_vs_total_notional": _optional_float(
                    position_capacity.get("gross_error_vs_total_notional")
                )
                if isinstance(position_capacity, dict)
                else None,
                "gross_error_vs_total_pct_points": _optional_float(
                    position_capacity.get("gross_error_vs_total_pct_points")
                )
                if isinstance(position_capacity, dict)
                else None,
                "submitted_orders": _safe_int(summary.get("submitted_orders")),
                "order_plan_count": _safe_int(summary.get("order_plan_count")),
                "staged_rebuild_snapshot_count": _safe_int(summary.get("staged_rebuild_snapshot_count")),
                "execution_attempt_rows": _safe_int(exec_attr.get("attempt_row_count")) if isinstance(exec_attr, dict) else 0,
                "fill_attempt_rows": _safe_int(exec_attr.get("filled_attempt_row_count")) if isinstance(exec_attr, dict) else 0,
                "implementation_shortfall_bps_weighted": _safe_float(
                    exec_attr.get("implementation_shortfall_bps_weighted")
                )
                if isinstance(exec_attr, dict)
                else 0.0,
                "execution_multi_attempt_records": _safe_int(exec_attr.get("multi_attempt_record_count"))
                if isinstance(exec_attr, dict)
                else 0,
                "execution_max_attempt_count": _safe_int(exec_attr.get("max_attempt_count"))
                if isinstance(exec_attr, dict)
                else 0,
                "execution_max_attempt_offset_bps": _safe_float(exec_attr.get("max_attempt_offset_bps"))
                if isinstance(exec_attr, dict)
                else 0.0,
                "execution_max_configured_offset_bps": _safe_float(exec_attr.get("max_configured_offset_bps"))
                if isinstance(exec_attr, dict)
                else 0.0,
                "execution_records_hitting_max_offset": _safe_int(exec_attr.get("records_hitting_max_offset_count"))
                if isinstance(exec_attr, dict)
                else 0,
                "execution_unfilled_records_hitting_max_offset": _safe_int(
                    exec_attr.get("unfilled_records_hitting_max_offset_count")
                )
                if isinstance(exec_attr, dict)
                else 0,
                "execution_unfilled_at_max_offset_remaining_notional": _safe_float(
                    exec_attr.get("unfilled_records_hitting_max_offset_remaining_notional")
                )
                if isinstance(exec_attr, dict)
                else 0.0,
                "target_symbols_missing_alpha": _safe_int(coverage.get("target_symbols_missing_alpha"))
                if isinstance(coverage, dict)
                else 0,
                "position_reconciliation_material_residuals": _safe_int(
                    recon.get("symbols_with_material_unexplained_qty")
                )
                if isinstance(recon, dict)
                else 0,
                "broker_activity_rows": _safe_int(activity.get("row_count")) if isinstance(activity, dict) else 0,
                "symbol_attribution_symbols": _safe_int(symbol_attr.get("symbol_count")) if isinstance(symbol_attr, dict) else 0,
                "symbol_attr_snapshot_intraday_pnl": _safe_float(symbol_totals.get("snapshot_intraday_pnl"))
                if isinstance(symbol_totals, dict)
                else 0.0,
                "symbol_attr_realized_pnl_estimate": _safe_float(symbol_totals.get("realized_pnl_estimate"))
                if isinstance(symbol_totals, dict)
                else 0.0,
                "symbol_attr_execution_shortfall": _safe_float(symbol_totals.get("implementation_shortfall_notional"))
                if isinstance(symbol_totals, dict)
                else 0.0,
                "symbol_attr_position_residual_notional": _safe_float(symbol_totals.get("position_unexplained_notional"))
                if isinstance(symbol_totals, dict)
                else 0.0,
                "position_snapshot_integrity_status": position_integrity.get("status")
                if isinstance(position_integrity, dict)
                else "",
                "position_snapshot_material_residual_symbols": _safe_int(
                    position_integrity.get("material_residual_symbol_count")
                )
                if isinstance(position_integrity, dict)
                else 0,
                "residual_diagnosis_status": residual_diag.get("status") if isinstance(residual_diag, dict) else "",
                "residual_diagnosis_attention_count": _safe_int(residual_diag.get("attention_count"))
                if isinstance(residual_diag, dict)
                else 0,
                "strict_account_position_replay_ready": bool(evidence.get("strict_account_position_replay_ready"))
                if isinstance(evidence, dict)
                else False,
                "target_transition_status": target_transition.get("status")
                if isinstance(target_transition, dict)
                else "",
                "target_transition_attention_symbols": _safe_int(target_transition.get("attention_symbol_count"))
                if isinstance(target_transition, dict)
                else 0,
                "target_transition_material_residual_symbols": _safe_int(
                    target_transition.get("material_position_residual_symbols")
                )
                if isinstance(target_transition, dict)
                else 0,
                "target_gap_symbol_count_without_position_residual": _safe_int(
                    target_transition.get("target_gap_symbol_count_without_position_residual")
                )
                if isinstance(target_transition, dict)
                else 0,
                "gross_target_error_abs_without_position_residual": _safe_float(
                    target_transition.get("gross_target_error_abs_without_position_residual")
                )
                if isinstance(target_transition, dict)
                else 0.0,
                "decision_intent_status": decision_intent.get("status")
                if isinstance(decision_intent, dict)
                else "",
                "decision_projection_changed_symbols": _safe_int(decision_intent.get("projection_changed_symbol_count"))
                if isinstance(decision_intent, dict)
                else 0,
                "decision_short_floor_zeroed_symbols": _safe_int(decision_intent.get("short_floor_zeroed_symbol_count"))
                if isinstance(decision_intent, dict)
                else 0,
                "decision_short_floor_reduced_symbols": _safe_int(decision_intent.get("short_floor_reduced_symbol_count"))
                if isinstance(decision_intent, dict)
                else 0,
                "decision_gross_projection_delta_notional_abs": _safe_float(
                    decision_intent.get("gross_projection_delta_notional_abs")
                )
                if isinstance(decision_intent, dict)
                else 0.0,
                "decision_skipped_symbol_count": _safe_int(decision_intent.get("skipped_symbol_count"))
                if isinstance(decision_intent, dict)
                else 0,
                "order_constraint_planned_orders": _safe_int(order_constraints.get("planned_order_count"))
                if isinstance(order_constraints, dict)
                else 0,
                "order_constraint_skipped_orders": _safe_int(order_constraints.get("skipped_order_count"))
                if isinstance(order_constraints, dict)
                else 0,
                "order_constraint_whole_share_required": _safe_int(order_constraints.get("whole_share_required_count"))
                if isinstance(order_constraints, dict)
                else 0,
                "order_constraint_unfilled_notional": _safe_float(order_constraints.get("gross_unfilled_notional_estimate"))
                if isinstance(order_constraints, dict)
                else 0.0,
                "order_constraint_skipped_notional": _safe_float(order_constraints.get("gross_skipped_abs_notional"))
                if isinstance(order_constraints, dict)
                else 0.0,
                "decision_execute_changed_symbols": _safe_int(decision_execute_drift.get("changed_symbol_count"))
                if isinstance(decision_execute_drift, dict)
                else 0,
                "decision_execute_material_changed_symbols": _safe_int(
                    decision_execute_drift.get("material_changed_symbol_count")
                )
                if isinstance(decision_execute_drift, dict)
                else 0,
                "decision_execute_gross_target_drift": _safe_float(
                    decision_execute_drift.get("gross_abs_target_notional_delta_estimate")
                )
                if isinstance(decision_execute_drift, dict)
                else 0.0,
                "decision_execute_gross_order_delta_drift": _safe_float(
                    decision_execute_drift.get("gross_abs_planned_delta_notional_change")
                )
                if isinstance(decision_execute_drift, dict)
                else 0.0,
                "market_price_status": market_price.get("status") if isinstance(market_price, dict) else "",
                "market_price_snapshot_exists": bool(market_price.get("execute_price_snapshot_exists"))
                if isinstance(market_price, dict)
                else False,
                "market_price_missing_reference_symbols": _safe_int(
                    market_price.get("execute_missing_reference_symbol_count")
                )
                if isinstance(market_price, dict)
                else 0,
                "market_price_fallback_only_symbols": _safe_int(market_price.get("fallback_only_symbol_count"))
                if isinstance(market_price, dict)
                else 0,
                "market_price_large_reference_moves": _safe_int(
                    market_price.get("large_decision_execute_reference_move_count")
                )
                if isinstance(market_price, dict)
                else 0,
                "market_price_max_abs_reference_move_bps": _safe_float(
                    market_price.get("max_abs_decision_execute_reference_change_bps")
                )
                if isinstance(market_price, dict)
                else 0.0,
                "intraday_bar_status": intraday_bars.get("status")
                if isinstance(intraday_bars, dict)
                else "",
                "intraday_bar_symbols": _safe_int(intraday_bars.get("symbol_count"))
                if isinstance(intraday_bars, dict)
                else 0,
                "intraday_bar_bar_symbols": _safe_int(intraday_bars.get("bar_symbol_count"))
                if isinstance(intraday_bars, dict)
                else 0,
                "intraday_bar_missing_symbols": _safe_int(intraday_bars.get("missing_bar_symbol_count"))
                if isinstance(intraday_bars, dict)
                else 0,
                "intraday_bar_filled_symbols_missing": _safe_int(
                    intraday_bars.get("filled_symbols_missing_bars_count")
                )
                if isinstance(intraday_bars, dict)
                else 0,
                "intraday_bar_error_count": _safe_int(intraday_bars.get("error_count"))
                if isinstance(intraday_bars, dict)
                else 0,
                "intraday_bar_max_range_bps": _safe_float(intraday_bars.get("max_intraday_range_bps"))
                if isinstance(intraday_bars, dict)
                else 0.0,
                "quote_status": quotes.get("status")
                if isinstance(quotes, dict)
                else "",
                "quote_symbols": _safe_int(quotes.get("symbol_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_quote_symbols": _safe_int(quotes.get("quote_symbol_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_missing_symbols": _safe_int(quotes.get("missing_quote_symbol_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_invalid_symbols": _safe_int(quotes.get("invalid_quote_symbol_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_wide_spread_symbols": _safe_int(quotes.get("wide_spread_symbol_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_error_count": _safe_int(quotes.get("error_count"))
                if isinstance(quotes, dict)
                else 0,
                "quote_max_spread_bps": _safe_float(quotes.get("max_spread_bps"))
                if isinstance(quotes, dict)
                else 0.0,
                "account_activity_rows": _safe_int(account_activity_attr.get("row_count"))
                if isinstance(account_activity_attr, dict)
                else 0,
                "account_activity_unknown_net_amount": _safe_float(
                    account_activity_attr.get("unknown_activity_net_amount")
                )
                if isinstance(account_activity_attr, dict)
                else 0.0,
                "account_activity_non_trade_equity_impact": _safe_float(
                    account_activity_attr.get("known_non_trade_equity_impact_net_amount")
                )
                if isinstance(account_activity_attr, dict)
                else 0.0,
                "account_activity_trade_fill_cashflow": _safe_float(
                    account_activity_attr.get("trade_fill_cashflow_net_amount")
                )
                if isinstance(account_activity_attr, dict)
                else 0.0,
                "corporate_action_status": corporate_action.get("status")
                if isinstance(corporate_action, dict)
                else "",
                "corporate_action_rows": _safe_int(corporate_action.get("action_count"))
                if isinstance(corporate_action, dict)
                else 0,
                "corporate_action_matched_position_residual_symbols": _safe_int(
                    corporate_action.get("matched_position_residual_symbol_count")
                )
                if isinstance(corporate_action, dict)
                else 0,
                "corporate_action_matched_account_activity_symbols": _safe_int(
                    corporate_action.get("matched_account_activity_symbol_count")
                )
                if isinstance(corporate_action, dict)
                else 0,
                "corporate_action_error_count": _safe_int(corporate_action.get("error_count"))
                if isinstance(corporate_action, dict)
                else 0,
                "corporate_action_residual_symbols_without_action": _safe_int(
                    corporate_action.get("residual_symbols_without_corporate_action_count")
                )
                if isinstance(corporate_action, dict)
                else 0,
                "portfolio_history_status": portfolio_history.get("status")
                if isinstance(portfolio_history, dict)
                else "",
                "portfolio_history_rows": _safe_int(portfolio_history.get("row_count"))
                if isinstance(portfolio_history, dict)
                else 0,
                "portfolio_history_summary_vs_after_delta": _safe_float(
                    portfolio_history.get("summary_vs_history_after_delta")
                )
                if isinstance(portfolio_history, dict)
                else 0.0,
                "portfolio_history_largest_equity_range": _safe_float(
                    portfolio_history.get("largest_equity_drawdown_from_history")
                )
                if isinstance(portfolio_history, dict)
                else 0.0,
                "calendar_status": calendar.get("status")
                if isinstance(calendar, dict)
                else "",
                "calendar_rows": _safe_int(calendar.get("row_count"))
                if isinstance(calendar, dict)
                else 0,
                "calendar_session_date_in_calendar": bool(calendar.get("session_date_in_calendar"))
                if isinstance(calendar, dict)
                else False,
                "calendar_expected_previous_trading_date": calendar.get("expected_previous_trading_date")
                if isinstance(calendar, dict)
                else "",
                "calendar_expected_next_trading_date": calendar.get("expected_next_trading_date")
                if isinstance(calendar, dict)
                else "",
                "calendar_session_is_half_day": bool(calendar.get("session_is_half_day"))
                if isinstance(calendar, dict)
                else False,
                "calendar_error_count": _safe_int(calendar.get("error_count"))
                if isinstance(calendar, dict)
                else 0,
                "strict_attribution_status": strict_checklist.get("status")
                if isinstance(strict_checklist, dict)
                else "",
                "strict_attribution_ready": bool(strict_checklist.get("strict_attribution_ready"))
                if isinstance(strict_checklist, dict)
                else False,
                "strict_attribution_blocking_items": _safe_int(strict_checklist.get("blocking_item_count"))
                if isinstance(strict_checklist, dict)
                else 0,
            }
        )

    rows = sorted(rows, key=lambda row: str(row.get("session_date") or ""))
    dates = [str(row.get("session_date") or "") for row in rows if str(row.get("session_date") or "")]
    duplicate_dates = sorted([date for date, count in Counter(dates).items() if count > 1])
    parsed_dates: list[datetime] = []
    for raw in dates:
        try:
            parsed_dates.append(datetime.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            try:
                parsed_dates.append(datetime.strptime(raw, "%Y%m%d"))
            except ValueError:
                pass
    large_calendar_gaps: list[dict[str, Any]] = []
    for prev, curr in zip(parsed_dates, parsed_dates[1:]):
        gap_days = (curr.date() - prev.date()).days
        if gap_days > 4:
            large_calendar_gaps.append(
                {
                    "previous_session_date": prev.date().isoformat(),
                    "next_session_date": curr.date().isoformat(),
                    "calendar_gap_days": gap_days,
                    "note": "Calendar gap greater than long weekend/holiday heuristic; verify market calendar.",
                }
            )
    official_calendar_gaps = _build_official_calendar_gaps(rows)
    continuity_rows, continuity_summary = _build_cross_day_continuity(root, rows)
    issue_rows = [row for row in rows if str(row.get("audit_status") or "") != "pass"]
    summary = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": root.as_posix(),
        "trading_day_count": len(rows),
        "first_session_date": dates[0] if dates else None,
        "last_session_date": dates[-1] if dates else None,
        "duplicate_session_dates": duplicate_dates,
        "large_calendar_gaps": large_calendar_gaps,
        "official_calendar_gaps": official_calendar_gaps,
        "cross_day_continuity": {
            "pair_count": continuity_summary.get("pair_count"),
            "issue_pair_count": continuity_summary.get("issue_pair_count"),
            "status_counts": continuity_summary.get("status_counts"),
            "largest_overnight_equity_gaps": continuity_summary.get("largest_overnight_equity_gaps"),
            "largest_position_qty_gaps": continuity_summary.get("largest_position_qty_gaps"),
        },
        "audit_status_counts": dict(sorted(Counter(str(row.get("audit_status") or "__missing__") for row in rows).items())),
        "audit_issue_days": [
            {
                "session_date": row.get("session_date"),
                "audit_status": row.get("audit_status"),
                "audit_issues": row.get("audit_issues"),
                "position_reconciliation_material_residuals": row.get("position_reconciliation_material_residuals"),
                "position_snapshot_integrity_status": row.get("position_snapshot_integrity_status"),
                "residual_diagnosis_status": row.get("residual_diagnosis_status"),
                "target_transition_status": row.get("target_transition_status"),
                "target_transition_attention_symbols": row.get("target_transition_attention_symbols"),
                "decision_intent_status": row.get("decision_intent_status"),
                "order_constraint_skipped_orders": row.get("order_constraint_skipped_orders"),
                "decision_execute_material_changed_symbols": row.get("decision_execute_material_changed_symbols"),
                "market_price_missing_reference_symbols": row.get("market_price_missing_reference_symbols"),
                "market_price_large_reference_moves": row.get("market_price_large_reference_moves"),
                "intraday_bar_status": row.get("intraday_bar_status"),
                "intraday_bar_missing_symbols": row.get("intraday_bar_missing_symbols"),
                "intraday_bar_filled_symbols_missing": row.get("intraday_bar_filled_symbols_missing"),
                "intraday_bar_error_count": row.get("intraday_bar_error_count"),
                "quote_status": row.get("quote_status"),
                "quote_missing_symbols": row.get("quote_missing_symbols"),
                "quote_invalid_symbols": row.get("quote_invalid_symbols"),
                "quote_wide_spread_symbols": row.get("quote_wide_spread_symbols"),
                "quote_error_count": row.get("quote_error_count"),
                "calendar_status": row.get("calendar_status"),
                "calendar_rows": row.get("calendar_rows"),
                "calendar_session_date_in_calendar": row.get("calendar_session_date_in_calendar"),
                "calendar_expected_previous_trading_date": row.get("calendar_expected_previous_trading_date"),
                "calendar_expected_next_trading_date": row.get("calendar_expected_next_trading_date"),
                "calendar_session_is_half_day": row.get("calendar_session_is_half_day"),
                "calendar_error_count": row.get("calendar_error_count"),
                "account_state_bridge_status": row.get("account_state_bridge_status"),
                "account_state_equity_delta": row.get("account_state_equity_delta"),
                "account_state_cash_delta": row.get("account_state_cash_delta"),
                "account_state_gross_exposure_delta": row.get("account_state_gross_exposure_delta"),
                "account_state_equity_delta_vs_summary_delta": row.get(
                    "account_state_equity_delta_vs_summary_delta"
                ),
                "market_context_status": row.get("market_context_status"),
                "market_context_snapshot_plus_realized_pnl": row.get("market_context_snapshot_plus_realized_pnl"),
                "market_context_net_beta_exposure_to_gross": row.get("market_context_net_beta_exposure_to_gross"),
                "market_context_benchmark_symbols_with_bars": row.get(
                    "market_context_benchmark_symbols_with_bars"
                ),
                "attribution_dossier_status": row.get("attribution_dossier_status"),
                "attribution_focus_symbol_count": row.get("attribution_focus_symbol_count"),
                "attribution_evidence_gap_count": row.get("attribution_evidence_gap_count"),
                "attribution_primary_bucket_counts": row.get("attribution_primary_bucket_counts"),
                "ideal_actual_gap_status": row.get("ideal_actual_gap_status"),
                "ideal_actual_gap_material_symbols": row.get("ideal_actual_gap_material_symbols"),
                "ideal_actual_gap_gross_abs": row.get("ideal_actual_gap_gross_abs"),
                "ideal_actual_gap_order_gap_notional": row.get("ideal_actual_gap_order_gap_notional"),
                "ideal_actual_gap_primary_bucket_counts": row.get("ideal_actual_gap_primary_bucket_counts"),
                "ideal_actual_gap_performance_drag_bucket_counts": row.get(
                    "ideal_actual_gap_performance_drag_bucket_counts"
                ),
                "account_activity_unknown_net_amount": row.get("account_activity_unknown_net_amount"),
                "corporate_action_status": row.get("corporate_action_status"),
                "corporate_action_rows": row.get("corporate_action_rows"),
                "corporate_action_matched_position_residual_symbols": row.get(
                    "corporate_action_matched_position_residual_symbols"
                ),
                "corporate_action_error_count": row.get("corporate_action_error_count"),
                "portfolio_history_status": row.get("portfolio_history_status"),
                "portfolio_history_rows": row.get("portfolio_history_rows"),
                "portfolio_history_summary_vs_after_delta": row.get("portfolio_history_summary_vs_after_delta"),
                "strict_attribution_status": row.get("strict_attribution_status"),
                "strict_attribution_blocking_items": row.get("strict_attribution_blocking_items"),
                "strict_account_position_replay_ready": row.get("strict_account_position_replay_ready"),
                "startup_binding_status": row.get("startup_binding_status"),
                "startup_binding_issue_count": row.get("startup_binding_issue_count"),
                "startup_autostart_registered": row.get("startup_autostart_registered"),
                "startup_process_health_status": row.get("startup_process_health_status"),
                "startup_due_session_date": row.get("startup_due_session_date"),
                "startup_due_decision_status": row.get("startup_due_decision_status"),
                "startup_due_execute_status": row.get("startup_due_execute_status"),
                "run_failure_status": row.get("run_failure_status"),
                "run_failure_task_status": row.get("run_failure_task_status"),
                "run_failure_class": row.get("run_failure_class"),
                "run_failure_error_type": row.get("run_failure_error_type"),
                "run_failure_error": row.get("run_failure_error"),
                "run_failure_missing_core_artifacts": row.get("run_failure_missing_core_artifacts"),
            }
            for row in issue_rows
        ],
        "strict_account_position_replay_ready_days": sum(
            1 for row in rows if bool(row.get("strict_account_position_replay_ready"))
        ),
        "strict_attribution_ready_days": sum(1 for row in rows if bool(row.get("strict_attribution_ready"))),
        "totals": {
            "equity_change": sum(_safe_float(row.get("equity_change")) for row in rows),
            "snapshot_intraday_pnl": sum(_safe_float(row.get("snapshot_intraday_pnl")) for row in rows),
            "realized_pnl_estimate": sum(_safe_float(row.get("realized_pnl_estimate")) for row in rows),
            "execution_shortfall_cost_estimate": sum(_safe_float(row.get("execution_shortfall_cost_estimate")) for row in rows),
            "equity_bridge_residual": sum(_safe_float(row.get("equity_bridge_residual")) for row in rows),
            "submitted_orders": sum(_safe_int(row.get("submitted_orders")) for row in rows),
            "position_reconciliation_material_residuals": sum(
                _safe_int(row.get("position_reconciliation_material_residuals")) for row in rows
            ),
            "symbol_attr_position_residual_notional": sum(
                _safe_float(row.get("symbol_attr_position_residual_notional")) for row in rows
            ),
            "residual_diagnosis_attention_count": sum(
                _safe_int(row.get("residual_diagnosis_attention_count")) for row in rows
            ),
            "target_transition_attention_symbols": sum(
                _safe_int(row.get("target_transition_attention_symbols")) for row in rows
            ),
            "target_transition_material_residual_symbols": sum(
                _safe_int(row.get("target_transition_material_residual_symbols")) for row in rows
            ),
            "target_gap_symbol_count_without_position_residual": sum(
                _safe_int(row.get("target_gap_symbol_count_without_position_residual")) for row in rows
            ),
            "gross_target_error_abs_without_position_residual": sum(
                _safe_float(row.get("gross_target_error_abs_without_position_residual")) for row in rows
            ),
            "decision_projection_changed_symbols": sum(
                _safe_int(row.get("decision_projection_changed_symbols")) for row in rows
            ),
            "decision_short_floor_zeroed_symbols": sum(
                _safe_int(row.get("decision_short_floor_zeroed_symbols")) for row in rows
            ),
            "decision_short_floor_reduced_symbols": sum(
                _safe_int(row.get("decision_short_floor_reduced_symbols")) for row in rows
            ),
            "decision_gross_projection_delta_notional_abs": sum(
                _safe_float(row.get("decision_gross_projection_delta_notional_abs")) for row in rows
            ),
            "decision_skipped_symbol_count": sum(
                _safe_int(row.get("decision_skipped_symbol_count")) for row in rows
            ),
            "order_constraint_planned_orders": sum(
                _safe_int(row.get("order_constraint_planned_orders")) for row in rows
            ),
            "order_constraint_skipped_orders": sum(
                _safe_int(row.get("order_constraint_skipped_orders")) for row in rows
            ),
            "order_constraint_whole_share_required": sum(
                _safe_int(row.get("order_constraint_whole_share_required")) for row in rows
            ),
            "order_constraint_unfilled_notional": sum(
                _safe_float(row.get("order_constraint_unfilled_notional")) for row in rows
            ),
            "order_constraint_skipped_notional": sum(
                _safe_float(row.get("order_constraint_skipped_notional")) for row in rows
            ),
            "decision_execute_changed_symbols": sum(
                _safe_int(row.get("decision_execute_changed_symbols")) for row in rows
            ),
            "decision_execute_material_changed_symbols": sum(
                _safe_int(row.get("decision_execute_material_changed_symbols")) for row in rows
            ),
            "decision_execute_gross_target_drift": sum(
                _safe_float(row.get("decision_execute_gross_target_drift")) for row in rows
            ),
            "decision_execute_gross_order_delta_drift": sum(
                _safe_float(row.get("decision_execute_gross_order_delta_drift")) for row in rows
            ),
            "market_price_missing_reference_symbols": sum(
                _safe_int(row.get("market_price_missing_reference_symbols")) for row in rows
            ),
            "market_price_fallback_only_symbols": sum(
                _safe_int(row.get("market_price_fallback_only_symbols")) for row in rows
            ),
            "market_price_large_reference_moves": sum(
                _safe_int(row.get("market_price_large_reference_moves")) for row in rows
            ),
            "intraday_bar_missing_symbols": sum(
                _safe_int(row.get("intraday_bar_missing_symbols")) for row in rows
            ),
            "intraday_bar_filled_symbols_missing": sum(
                _safe_int(row.get("intraday_bar_filled_symbols_missing")) for row in rows
            ),
            "intraday_bar_error_count": sum(_safe_int(row.get("intraday_bar_error_count")) for row in rows),
            "quote_missing_symbols": sum(_safe_int(row.get("quote_missing_symbols")) for row in rows),
            "quote_invalid_symbols": sum(_safe_int(row.get("quote_invalid_symbols")) for row in rows),
            "quote_wide_spread_symbols": sum(_safe_int(row.get("quote_wide_spread_symbols")) for row in rows),
            "quote_error_count": sum(_safe_int(row.get("quote_error_count")) for row in rows),
            "calendar_rows": sum(_safe_int(row.get("calendar_rows")) for row in rows),
            "calendar_error_count": sum(_safe_int(row.get("calendar_error_count")) for row in rows),
            "calendar_official_gap_count": _safe_int(official_calendar_gaps.get("gap_count")),
            "calendar_limited_pair_count": _safe_int(official_calendar_gaps.get("limited_pair_count")),
            "account_state_equity_delta": sum(_safe_float(row.get("account_state_equity_delta")) for row in rows),
            "account_state_cash_delta": sum(_safe_float(row.get("account_state_cash_delta")) for row in rows),
            "account_state_abs_gross_exposure_delta": sum(
                abs(_safe_float(row.get("account_state_gross_exposure_delta"))) for row in rows
            ),
            "account_state_abs_equity_delta_vs_summary_delta": sum(
                abs(_safe_float(row.get("account_state_equity_delta_vs_summary_delta"))) for row in rows
            ),
            "account_config_changed_fields": sum(
                _safe_int(row.get("account_config_changed_fields")) for row in rows
            ),
            "run_evidence_digest_missing_files": sum(
                _safe_int(row.get("run_evidence_digest_missing_files")) for row in rows
            ),
            "run_evidence_digest_strict_missing_files": sum(
                _safe_int(row.get("run_evidence_digest_strict_missing_files")) for row in rows
            ),
            "run_evidence_digest_api_audit_line_count": sum(
                _safe_int(row.get("run_evidence_digest_api_audit_line_count")) for row in rows
            ),
            "run_evidence_digest_run_event_count": sum(
                _safe_int(row.get("run_evidence_digest_run_event_count")) for row in rows
            ),
            "run_evidence_digest_hash_manifest_file_count": sum(
                _safe_int(row.get("run_evidence_digest_hash_manifest_file_count")) for row in rows
            ),
            "run_evidence_digest_artifact_completeness_partial_category_count": sum(
                _safe_int(row.get("run_evidence_digest_artifact_completeness_partial_category_count"))
                for row in rows
            ),
            "startup_binding_issue_count": sum(
                _safe_int(row.get("startup_binding_issue_count")) for row in rows
            ),
            "startup_autostart_registered_days": sum(
                1 for row in rows if bool(row.get("startup_autostart_registered"))
            ),
            "run_failure_issue_days": sum(
                1 for row in rows if str(row.get("run_failure_status") or "") not in {"", "pass"}
            ),
            "market_context_snapshot_plus_realized_pnl": sum(
                _safe_float(row.get("market_context_snapshot_plus_realized_pnl")) for row in rows
            ),
            "market_context_benchmark_symbols_with_bars": sum(
                _safe_int(row.get("market_context_benchmark_symbols_with_bars")) for row in rows
            ),
            "attribution_focus_symbol_count": sum(
                _safe_int(row.get("attribution_focus_symbol_count")) for row in rows
            ),
            "attribution_evidence_gap_count": sum(
                _safe_int(row.get("attribution_evidence_gap_count")) for row in rows
            ),
            "ideal_actual_gap_material_symbols": sum(
                _safe_int(row.get("ideal_actual_gap_material_symbols")) for row in rows
            ),
            "ideal_actual_gap_gross_abs": sum(
                _safe_float(row.get("ideal_actual_gap_gross_abs")) for row in rows
            ),
            "ideal_actual_gap_after_projected_abs": sum(
                _safe_float(row.get("ideal_actual_gap_after_projected_abs")) for row in rows
            ),
            "ideal_actual_gap_projection_abs": sum(
                _safe_float(row.get("ideal_actual_gap_projection_abs")) for row in rows
            ),
            "ideal_actual_gap_decision_target_drift_abs": sum(
                _safe_float(row.get("ideal_actual_gap_decision_target_drift_abs")) for row in rows
            ),
            "ideal_actual_gap_decision_order_drift_abs": sum(
                _safe_float(row.get("ideal_actual_gap_decision_order_drift_abs")) for row in rows
            ),
            "ideal_actual_gap_submitted_unfilled_notional": sum(
                _safe_float(row.get("ideal_actual_gap_submitted_unfilled_notional")) for row in rows
            ),
            "ideal_actual_gap_skipped_notional": sum(
                _safe_float(row.get("ideal_actual_gap_skipped_notional")) for row in rows
            ),
            "ideal_actual_gap_order_gap_notional": sum(
                _safe_float(row.get("ideal_actual_gap_order_gap_notional")) for row in rows
            ),
            "ideal_actual_gap_pnl_loss_abs": sum(
                _safe_float(row.get("ideal_actual_gap_pnl_loss_abs")) for row in rows
            ),
            "execution_attempt_rows": sum(_safe_int(row.get("execution_attempt_rows")) for row in rows),
            "execution_multi_attempt_records": sum(
                _safe_int(row.get("execution_multi_attempt_records")) for row in rows
            ),
            "execution_records_hitting_max_offset": sum(
                _safe_int(row.get("execution_records_hitting_max_offset")) for row in rows
            ),
            "execution_unfilled_records_hitting_max_offset": sum(
                _safe_int(row.get("execution_unfilled_records_hitting_max_offset")) for row in rows
            ),
            "execution_unfilled_at_max_offset_remaining_notional": sum(
                _safe_float(row.get("execution_unfilled_at_max_offset_remaining_notional")) for row in rows
            ),
            "account_activity_unknown_net_amount": sum(
                _safe_float(row.get("account_activity_unknown_net_amount")) for row in rows
            ),
            "account_activity_non_trade_equity_impact": sum(
                _safe_float(row.get("account_activity_non_trade_equity_impact")) for row in rows
            ),
            "account_activity_trade_fill_cashflow": sum(
                _safe_float(row.get("account_activity_trade_fill_cashflow")) for row in rows
            ),
            "corporate_action_rows": sum(_safe_int(row.get("corporate_action_rows")) for row in rows),
            "corporate_action_matched_position_residual_symbols": sum(
                _safe_int(row.get("corporate_action_matched_position_residual_symbols")) for row in rows
            ),
            "corporate_action_matched_account_activity_symbols": sum(
                _safe_int(row.get("corporate_action_matched_account_activity_symbols")) for row in rows
            ),
            "corporate_action_error_count": sum(_safe_int(row.get("corporate_action_error_count")) for row in rows),
            "corporate_action_residual_symbols_without_action": sum(
                _safe_int(row.get("corporate_action_residual_symbols_without_action")) for row in rows
            ),
            "portfolio_history_rows": sum(_safe_int(row.get("portfolio_history_rows")) for row in rows),
            "portfolio_history_abs_summary_vs_after_delta": sum(
                abs(_safe_float(row.get("portfolio_history_summary_vs_after_delta"))) for row in rows
            ),
            "strict_attribution_blocking_items": sum(
                _safe_int(row.get("strict_attribution_blocking_items")) for row in rows
            ),
        },
        "worst_equity_change_days": sorted(rows, key=lambda row: _safe_float(row.get("equity_change")))[:10],
        "largest_bridge_residual_days": sorted(
            rows,
            key=lambda row: abs(_safe_float(row.get("equity_bridge_residual"))),
            reverse=True,
        )[:10],
        "largest_account_state_delta_days": sorted(
            rows,
            key=lambda row: abs(_safe_float(row.get("account_state_gross_exposure_delta"))),
            reverse=True,
        )[:10],
        "rows": rows,
        "notes": [
            "official_calendar_gaps uses captured Alpaca calendar windows when available.",
            "large_calendar_gaps remains a legacy heuristic for historical runs without official calendar evidence.",
            "Equity bridge components are diagnostic and inherit each daily audit file's strictness caveats.",
        ],
    }
    _write_csv(
        root / "audit_rollup.csv",
        rows,
        [
            "session_date", "session_idx", "audit_status", "audit_issues", "equity_before",
            "equity_after", "equity_change", "snapshot_intraday_pnl", "realized_pnl_estimate",
            "execution_shortfall_cost_estimate", "equity_bridge_residual",
            "account_state_bridge_status", "account_state_equity_delta",
            "account_state_cash_delta", "account_state_gross_exposure_delta",
            "account_state_net_exposure_delta", "account_state_buying_power_delta",
            "account_state_maintenance_margin_delta", "account_state_equity_delta_vs_summary_delta",
            "account_state_equity_delta_vs_equity_bridge",
            "account_config_status", "account_config_changed_fields",
            "run_evidence_digest_status", "run_evidence_digest_digest_exists",
            "run_evidence_digest_coverage_ratio", "run_evidence_digest_missing_files",
            "run_evidence_digest_strict_missing_files", "run_evidence_digest_api_audit_line_count",
            "run_evidence_digest_run_event_count", "run_evidence_digest_hash_manifest_file_count",
            "run_evidence_digest_artifact_completeness_status",
            "run_evidence_digest_artifact_completeness_partial_category_count",
            "startup_binding_status", "startup_binding_issue_count", "startup_autostart_registered",
            "startup_process_health_status", "startup_scheduler_due_latest_exists",
            "startup_scheduler_runtime_latest_exists", "startup_due_session_date",
            "startup_due_decision_status", "startup_due_execute_status",
            "run_failure_status", "run_failure_task_status", "run_failure_class",
            "run_failure_error_type", "run_failure_error", "run_failure_missing_core_artifacts",
            "market_context_status", "market_context_snapshot_plus_realized_pnl",
            "market_context_net_beta_exposure_to_gross", "market_context_benchmark_symbols_with_bars",
            "attribution_dossier_status", "attribution_focus_symbol_count",
            "attribution_top_focus_symbol_count", "attribution_evidence_gap_count",
            "attribution_primary_bucket_counts",
            "ideal_actual_gap_status", "ideal_actual_gap_material_symbols",
            "ideal_actual_gap_gross_abs", "ideal_actual_gap_after_projected_abs",
            "ideal_actual_gap_projection_abs", "ideal_actual_gap_decision_target_drift_abs",
            "ideal_actual_gap_decision_order_drift_abs",
            "ideal_actual_gap_submitted_unfilled_notional", "ideal_actual_gap_skipped_notional",
            "ideal_actual_gap_order_gap_notional", "ideal_actual_gap_pnl_loss_abs",
            "ideal_actual_gap_primary_bucket_counts",
            "ideal_actual_gap_performance_drag_bucket_counts",
            "strategy_to_actual_weight_error_l1", "strategy_to_executable_weight_error_l1",
            "executable_to_actual_weight_error_l1", "mean_symbol_strategy_to_actual_weight_error",
            "max_symbol_strategy_to_actual_weight_error", "executable_projection_status",
            "optimizer_tracking_error_l1_weight", "optimizer_mean_abs_symbol_weight_error",
            "optimizer_max_abs_symbol_weight_error", "optimizer_buying_power_cap_utilization",
            "position_capacity_status", "gross_position_notional", "regt_buying_power_remaining",
            "total_regt_buying_power_capacity", "configured_buying_power_target_ratio",
            "configured_gross_target_notional", "gross_utilization_of_total_bp",
            "gross_error_vs_target_notional", "gross_error_vs_target_pct_points",
            "gross_error_vs_total_notional", "gross_error_vs_total_pct_points",
            "submitted_orders",
            "order_plan_count", "staged_rebuild_snapshot_count", "execution_attempt_rows",
            "fill_attempt_rows", "implementation_shortfall_bps_weighted",
            "execution_multi_attempt_records", "execution_max_attempt_count",
            "execution_max_attempt_offset_bps", "execution_max_configured_offset_bps",
            "execution_records_hitting_max_offset", "execution_unfilled_records_hitting_max_offset",
            "execution_unfilled_at_max_offset_remaining_notional", "target_symbols_missing_alpha",
            "position_reconciliation_material_residuals", "broker_activity_rows", "run_dir",
            "symbol_attribution_symbols", "symbol_attr_snapshot_intraday_pnl",
            "symbol_attr_realized_pnl_estimate", "symbol_attr_execution_shortfall",
            "symbol_attr_position_residual_notional", "position_snapshot_integrity_status",
            "position_snapshot_material_residual_symbols", "residual_diagnosis_status",
            "residual_diagnosis_attention_count", "target_transition_status",
            "target_transition_attention_symbols", "target_transition_material_residual_symbols",
            "target_gap_symbol_count_without_position_residual",
            "gross_target_error_abs_without_position_residual", "decision_intent_status",
            "decision_projection_changed_symbols", "decision_short_floor_zeroed_symbols",
            "decision_short_floor_reduced_symbols", "decision_gross_projection_delta_notional_abs",
            "decision_skipped_symbol_count", "order_constraint_planned_orders",
            "order_constraint_skipped_orders", "order_constraint_whole_share_required",
            "order_constraint_unfilled_notional", "order_constraint_skipped_notional",
            "decision_execute_changed_symbols", "decision_execute_material_changed_symbols",
            "decision_execute_gross_target_drift", "decision_execute_gross_order_delta_drift",
            "market_price_status", "market_price_snapshot_exists",
            "market_price_missing_reference_symbols", "market_price_fallback_only_symbols",
            "market_price_large_reference_moves", "market_price_max_abs_reference_move_bps",
            "intraday_bar_status", "intraday_bar_symbols", "intraday_bar_bar_symbols",
            "intraday_bar_missing_symbols", "intraday_bar_filled_symbols_missing",
            "intraday_bar_error_count", "intraday_bar_max_range_bps",
            "quote_status", "quote_symbols", "quote_quote_symbols", "quote_missing_symbols",
            "quote_invalid_symbols", "quote_wide_spread_symbols", "quote_error_count",
            "quote_max_spread_bps",
            "calendar_status", "calendar_rows", "calendar_session_date_in_calendar",
            "calendar_expected_previous_trading_date", "calendar_expected_next_trading_date",
            "calendar_session_is_half_day", "calendar_error_count",
            "account_activity_rows", "account_activity_unknown_net_amount",
            "account_activity_non_trade_equity_impact", "account_activity_trade_fill_cashflow",
            "corporate_action_status", "corporate_action_rows",
            "corporate_action_matched_position_residual_symbols",
            "corporate_action_matched_account_activity_symbols", "corporate_action_error_count",
            "corporate_action_residual_symbols_without_action",
            "portfolio_history_status", "portfolio_history_rows",
            "portfolio_history_summary_vs_after_delta", "portfolio_history_largest_equity_range",
            "strict_attribution_status", "strict_attribution_ready", "strict_attribution_blocking_items",
            "strict_account_position_replay_ready",
        ],
    )
    _write_json(root / "audit_rollup.json", summary)
    _write_csv(
        root / "audit_continuity.csv",
        continuity_rows,
        [
            "previous_session_date", "next_session_date", "calendar_gap_days",
            "previous_after_equity", "next_before_equity", "overnight_equity_gap",
            "previous_after_position_symbols", "next_before_position_symbols",
            "position_symbol_union_count", "symbols_with_qty_gap", "total_abs_qty_gap",
            "total_abs_market_value_gap", "largest_symbol_qty_gaps", "status", "note",
        ],
    )
    _write_json(root / "audit_continuity.json", continuity_summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate daily live-trading audit package(s).")
    parser.add_argument("--run-dir", type=Path, help="One *_execute run directory")
    parser.add_argument("--decision-dir", type=Path, default=None, help="Matching *_decision directory")
    parser.add_argument("--all", action="store_true", help="Generate for every *_execute dir under scheduler artifacts")
    parser.add_argument("--rollup", action="store_true", help="Generate cross-day audit_rollup.csv/json under --root")
    parser.add_argument("--root", type=Path, default=SCHED_ROOT)
    args = parser.parse_args(argv)

    if not args.all and not args.run_dir and not args.rollup:
        parser.error("provide --run-dir, --all, or --rollup")

    results = []
    if args.all:
        for run_dir in _execute_dirs(args.root):
            results.append(generate_audit(run_dir))
        for task_dir in _task_dirs(args.root):
            if task_dir.name.endswith("_execute") and _is_valid_execute_dir(task_dir):
                continue
            if _needs_task_health_audit(task_dir):
                results.append(generate_task_health_audit(task_dir))
    elif args.run_dir:
        results.append(generate_audit(args.run_dir, args.decision_dir))
    if args.rollup or args.all:
        results.append({"rollup": generate_rollup(args.root)})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
