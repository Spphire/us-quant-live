"""Run an isolated paper-order fault-injection test for entry repair."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.alpaca_executor import (  # noqa: E402
    OrderInstruction,
    _execution_attempt_outcome_summary,
    _submit_and_track_orders,
)
from src.dynamic_symbol_pool import _resolve_alpaca_credentials  # noqa: E402
from src.vendors.alpaca import AlpacaHttpClient  # noqa: E402
from src.vendors.longbridge import (  # noqa: E402
    LongbridgeCredentials,
    LongbridgeQuoteClient,
)


class _ShiftedQuoteClient:
    def __init__(self, delegate: LongbridgeQuoteClient, shift_bps: float) -> None:
        self.delegate = delegate
        self.shift_ratio = float(shift_bps) / 10_000.0

    def get_latest_quotes(
        self,
        *,
        symbols: list[str],
        feed: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        quotes = self.delegate.get_latest_quotes(
            symbols=symbols,
            feed=feed,
            require_fresh=True,
        )
        shifted: dict[str, dict[str, Any]] = {}
        for symbol, raw in quotes.items():
            quote = dict(raw)
            for field in ("bp", "ap", "last_trade_price"):
                value = quote.get(field)
                if value is not None:
                    quote[field] = float(value) * (1.0 + self.shift_ratio)
            quote["provider"] = "longbridge_fault_injection"
            quote["fault_injection_shift_bps"] = self.shift_ratio * 10_000.0
            shifted[symbol] = quote
        return shifted


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paper", action="store_true")
    parser.add_argument("--account-name", required=True)
    parser.add_argument("--symbol", default="SEZL")
    parser.add_argument("--qty", type=int, default=3)
    parser.add_argument("--fault-shift-bps", type=float, default=200.0)
    parser.add_argument("--repair-offset-bps", type=float, default=150.0)
    parser.add_argument("--longbridge-max-quote-age-seconds", type=float, default=5.0)
    parser.add_argument(
        "--output-root",
        default="artifacts/0727_execution_tests/experiment_20260728_entry_repair_live",
    )
    return parser.parse_args()


def _position_for(client: AlpacaHttpClient, symbol: str) -> dict[str, Any] | None:
    return next(
        (
            dict(row)
            for row in client.list_positions()
            if str(row.get("symbol") or "").upper() == symbol
        ),
        None,
    )


def _wait_terminal(
    client: AlpacaHttpClient,
    order_id: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    latest = client.get_order(order_id)
    while (
        str(latest.get("status") or "").lower()
        not in {"filled", "canceled", "expired", "rejected", "failed"}
        and time.monotonic() < deadline
    ):
        time.sleep(0.5)
        latest = client.get_order(order_id)
    return latest


def _cleanup_test_state(
    *,
    client: AlpacaHttpClient,
    symbol: str,
    prefix: str,
    expect_position: bool,
) -> dict[str, Any]:
    canceled_order_ids: list[str] = []
    for order in client.list_orders(status="open", limit=500):
        if str(order.get("client_order_id") or "").startswith(prefix):
            client.cancel_order(str(order["id"]))
            canceled_order_ids.append(str(order["id"]))

    position = _position_for(client, symbol)
    if expect_position and position is None:
        position_deadline = time.monotonic() + 20.0
        while position is None and time.monotonic() < position_deadline:
            time.sleep(1.0)
            position = _position_for(client, symbol)
    cleanup_order: dict[str, Any] | None = None
    cleanup_latest: dict[str, Any] | None = None
    if position is not None:
        side = "buy" if str(position.get("side") or "").lower() == "short" else "sell"
        cleanup_order = client.submit_order(
            symbol=symbol,
            side=side,
            type="market",
            time_in_force="day",
            qty=abs(float(position.get("qty") or 0.0)),
            client_order_id=f"{prefix}_market_cleanup",
        )
        cleanup_latest = _wait_terminal(client, str(cleanup_order["id"]))
        position_deadline = time.monotonic() + 20.0
        stable_absent_samples = 0
        while time.monotonic() < position_deadline:
            if _position_for(client, symbol) is None:
                stable_absent_samples += 1
                if stable_absent_samples >= 3:
                    break
            else:
                stable_absent_samples = 0
            time.sleep(1.0)
    else:
        stable_absent_samples = 1

    open_orders_after = [
        dict(order)
        for order in client.list_orders(status="open", limit=500)
        if str(order.get("client_order_id") or "").startswith(prefix)
    ]
    return {
        "canceled_order_ids": canceled_order_ids,
        "cleanup_order": cleanup_order,
        "cleanup_latest": cleanup_latest,
        "stable_absent_position_samples": int(stable_absent_samples),
        "position_after_cleanup": _position_for(client, symbol),
        "test_open_orders_after_cleanup": open_orders_after,
    }


def main() -> int:
    args = _parse_args()
    if not args.confirm_paper:
        raise SystemExit("Refusing to trade without --confirm-paper")

    symbol = str(args.symbol).strip().upper()
    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "controlled_entry_repair_result.json"
    prefix = f"repairlive{int(time.time()) % 1_000_000:06d}"
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_alias": str(args.account_name),
        "symbol": symbol,
        "qty": int(args.qty),
        "fault_injection_shift_bps": float(args.fault_shift_bps),
        "repair_offset_bps": float(args.repair_offset_bps),
        "longbridge_max_quote_age_seconds": float(
            args.longbridge_max_quote_age_seconds
        ),
        "records": [],
    }

    credentials = _resolve_alpaca_credentials(
        accounts_json_path="configs/alpaca_acounts/alpaca_accounts.local.json",
        account_name=str(args.account_name),
        data_base_url="https://data.alpaca.markets",
        request_timeout_seconds=30.0,
        max_retries=3,
    )
    if "paper-api.alpaca.markets" not in credentials.trading_base_url:
        raise SystemExit("Refusing to run against a non-paper Alpaca endpoint")

    alpaca = AlpacaHttpClient(credentials)
    longbridge = LongbridgeQuoteClient(
        LongbridgeCredentials.from_sources("configs/longbridge.local.json"),
        max_quote_age_seconds=float(args.longbridge_max_quote_age_seconds),
    )
    records: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {}
    try:
        if not bool(alpaca.get_clock().get("is_open")):
            raise RuntimeError("US market is not open")
        if _position_for(alpaca, symbol) is not None:
            raise RuntimeError(f"{symbol} position already exists")
        if alpaca.list_orders(status="open", limit=500):
            raise RuntimeError("Open orders exist; refusing isolated test")

        result["longbridge_start_health"] = longbridge.start([symbol])
        quote = longbridge.get_marketable_quote(symbol)
        result["real_quote_before"] = quote
        reference = float(quote["bp"])
        instruction = OrderInstruction(
            symbol=symbol,
            side="sell",
            qty=float(args.qty),
            reference_price=reference,
            sizing_price=reference,
            current_notional=0.0,
            target_notional=-float(args.qty) * reference,
            delta_notional=-float(args.qty) * reference,
            opening_short=True,
            current_signed_qty=0.0,
            target_signed_qty=-float(args.qty),
        )

        initial_records = _submit_and_track_orders(
            client=alpaca,
            instructions=[instruction],
            session_token=f"{prefix}_ent",
            timeout_seconds=20.0,
            poll_seconds=0.5,
            execution_order_style="marketable_limit",
            marketable_limit_base_offset_bps=0.0,
            marketable_limit_max_offset_bps=0.0,
            marketable_limit_requote_steps_bps=[0.0],
            marketable_limit_requote_wait_seconds=3.0,
            marketable_limit_max_attempts=1,
            max_workers=1,
            execution_price_feed="iex",
            execution_quote_client=_ShiftedQuoteClient(
                longbridge,
                float(args.fault_shift_bps),
            ),
        )
        for record in initial_records:
            record["stage"] = "entry"
        records.extend(initial_records)

        if not initial_records or float(initial_records[0].get("filled_qty") or 0.0) > 0.0:
            raise RuntimeError("Fault-injected initial order unexpectedly filled")

        repair_records = _submit_and_track_orders(
            client=alpaca,
            instructions=[instruction],
            session_token=f"{prefix}_erp_r01",
            timeout_seconds=30.0,
            poll_seconds=0.5,
            execution_order_style="marketable_limit",
            marketable_limit_base_offset_bps=float(args.repair_offset_bps),
            marketable_limit_max_offset_bps=float(args.repair_offset_bps),
            marketable_limit_requote_steps_bps=[float(args.repair_offset_bps)],
            marketable_limit_requote_wait_seconds=10.0,
            marketable_limit_max_attempts=1,
            max_workers=1,
            execution_price_feed="iex",
            execution_quote_client=longbridge,
        )
        for record in repair_records:
            record["stage"] = "entry_repair"
            record["entry_repair_round"] = 1
        records.extend(repair_records)
        time.sleep(1.0)
        result["position_after_repair"] = _position_for(alpaca, symbol)
        result["records"] = records
        result["attempt_outcome_summary"] = _execution_attempt_outcome_summary(
            records
        )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        try:
            cleanup = _cleanup_test_state(
                client=alpaca,
                symbol=symbol,
                prefix=prefix,
                expect_position=any(
                    float(record.get("filled_qty") or 0.0) > 0.0
                    for record in records
                ),
            )
        except Exception as cleanup_exc:
            cleanup = {
                "error_type": type(cleanup_exc).__name__,
                "error": str(cleanup_exc),
            }
        result["cleanup"] = cleanup
        result["completed_at_utc"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        longbridge.close()

    initial_record = records[0] if records else {}
    repair_record = next(
        (record for record in records if record.get("stage") == "entry_repair"),
        {},
    )
    result["ok"] = bool(
        str(initial_record.get("status_latest") or "") == "canceled"
        and float(repair_record.get("filled_qty") or 0.0) > 0.0
        and cleanup.get("position_after_cleanup") is None
        and not cleanup.get("test_open_orders_after_cleanup")
        and not cleanup.get("error")
        and int(cleanup.get("stable_absent_position_samples") or 0) >= 3
        and not result.get("error")
    )
    output_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": result["ok"],
                "initial_status": initial_record.get("status_latest"),
                "repair_status": repair_record.get("status_latest"),
                "repair_filled_qty": repair_record.get("filled_qty"),
                "repair_fill_price": repair_record.get("filled_avg_price"),
                "repaired_entry_symbols": (
                    result.get("attempt_outcome_summary") or {}
                ).get("repaired_entry_symbols"),
                "position_after_cleanup": cleanup.get("position_after_cleanup"),
                "test_open_order_count_after_cleanup": len(
                    cleanup.get("test_open_orders_after_cleanup") or []
                ),
                "error": result.get("error"),
                "artifact": output_path.as_posix(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
