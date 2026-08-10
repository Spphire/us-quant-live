from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping, MutableMapping, Sequence


def index_corporate_actions(actions: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Index Alpaca corporate-action rows by ex-date and remove duplicates."""

    indexed: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for raw in actions:
        if not isinstance(raw, Mapping):
            continue
        date_source = next(
            (
                key
                for key in ("ex_date", "exDate", "effective_date", "process_date")
                if str(raw.get(key) or "").strip()
            ),
            "",
        )
        ex_date = str(raw.get(date_source) or "")[:10] if date_source else ""
        symbol = str(
            raw.get("symbol")
            or raw.get("old_symbol")
            or raw.get("target_symbol")
            or raw.get("new_symbol")
            or ""
        ).strip().upper()
        if len(ex_date) != 10 or not symbol:
            continue
        action_type = str(raw.get("action_type") or raw.get("type") or "").strip().lower()
        action = dict(raw)
        action["symbol"] = symbol
        action["ex_date"] = ex_date
        action["effective_date_source"] = date_source
        action["action_type"] = action_type
        identity = str(raw.get("id") or "").strip() or _action_identity(action)
        indexed[ex_date][identity] = action
    return {date: list(rows.values()) for date, rows in sorted(indexed.items())}


def apply_corporate_actions(
    *,
    shares: MutableMapping[str, float],
    cash: float,
    actions: Sequence[Mapping[str, Any]],
) -> tuple[float, dict[str, Any]]:
    """Apply ex-date split and cash-dividend effects to a raw-price portfolio.

    Split quantities are changed before the session open. Cash dividends are
    booked on ex-date as economic total-return accounting; this keeps the
    backtest independent of a later payable-date cash timing convention.
    """

    cash_value = float(cash)
    split_events: list[dict[str, Any]] = []
    dividend_events: list[dict[str, Any]] = []
    unsupported_events: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw in actions:
        symbol = str(
            raw.get("symbol")
            or raw.get("old_symbol")
            or raw.get("target_symbol")
            or raw.get("new_symbol")
            or ""
        ).strip().upper()
        if not symbol:
            continue
        action_type = str(raw.get("action_type") or raw.get("type") or "").strip().lower()
        if "split" in action_type:
            factor = resolve_split_factor(raw)
            if factor is None or factor <= 0.0 or not math.isfinite(factor):
                errors.append({"symbol": symbol, "action_type": action_type, "reason": "invalid_split_ratio"})
                continue
            previous_qty = float(shares.get(symbol, 0.0))
            if abs(previous_qty) > 1e-12:
                shares[symbol] = float(previous_qty * factor)
            split_events.append(
                {
                    "symbol": symbol,
                    "factor": float(factor),
                    "old_qty": previous_qty,
                    "new_qty": float(previous_qty * factor),
                    "action_id": str(raw.get("id") or ""),
                }
            )
            continue
        if action_type in {"cash_dividend", "cash_dividends"}:
            rate = _rate(raw.get("rate"))
            if rate is None or rate < 0.0:
                errors.append({"symbol": symbol, "action_type": action_type, "reason": "invalid_dividend_rate"})
                continue
            qty = float(shares.get(symbol, 0.0))
            cash_delta = qty * rate
            cash_value += cash_delta
            dividend_events.append(
                {
                    "symbol": symbol,
                    "rate": float(rate),
                    "qty": qty,
                    "cash_delta": float(cash_delta),
                    "action_id": str(raw.get("id") or ""),
                }
            )
            continue

        qty = float(shares.get(symbol, 0.0))
        unsupported = {
            "symbol": symbol,
            "action_type": action_type,
            "qty": qty,
            "action_id": str(raw.get("id") or ""),
        }
        unsupported_events.append(unsupported)
        if abs(qty) > 1e-12:
            errors.append(
                {
                    **unsupported,
                    "reason": "unsupported_corporate_action_for_held_position",
                }
            )

    return cash_value, {
        "schema_version": "1.0",
        "action_count": len(actions),
        "split_event_count": len(split_events),
        "dividend_event_count": len(dividend_events),
        "split_events": split_events,
        "dividend_events": dividend_events,
        "unsupported_event_count": len(unsupported_events),
        "unsupported_events": unsupported_events,
        "cash_delta": float(cash_value - float(cash)),
        "errors": errors,
        "status": "error" if errors else "attention" if unsupported_events else "pass",
    }


def resolve_split_factor(action: Mapping[str, Any]) -> float | None:
    old_rate = _rate(action.get("old_rate"))
    new_rate = _rate(action.get("new_rate"))
    factor = _rate(action.get("split_factor"))
    if factor is None and old_rate is not None and new_rate is not None and old_rate > 0.0:
        factor = new_rate / old_rate
    if factor is None or factor <= 0.0 or not math.isfinite(factor):
        return None
    return float(factor)


def _action_identity(action: Mapping[str, Any]) -> str:
    return "|".join(
        str(action.get(key) or "")
        for key in ("symbol", "action_type", "ex_date", "rate", "old_rate", "new_rate")
    )


def _rate(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and ":" in value:
        left, right = value.split(":", 1)
        try:
            numerator = float(left)
            denominator = float(right)
        except ValueError:
            return None
        if denominator == 0.0:
            return None
        return numerator / denominator
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
