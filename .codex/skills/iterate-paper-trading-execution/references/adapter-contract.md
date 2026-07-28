# Broker Adapter Contract

Use this contract to make execution experiments portable across brokers. Keep platform-specific payloads in raw evidence files and map only stable semantics into canonical artifacts.

## Required Capabilities

Implement or identify equivalents for:

| Capability | Required behavior |
| --- | --- |
| Account | Return immutable account ID, status, equity, buying power, RegT/day-trading fields, trading blocks, and shorting capability. |
| Clock/calendar | Return authoritative open/closed state and next open/close timestamps. |
| Assets | Return tradable, fractionable, shortable, easy-to-borrow/borrow status, and exchange/class. |
| Positions | Return signed quantity, available quantity, market value, current price, and average entry price. |
| Orders | Submit, query, list, and cancel by immutable order ID and client order ID. Preserve partial fills. |
| Activities/fills | Return fill quantity, price, side, symbol, order linkage, and timestamp. |
| Quotes | Return bid, ask, sizes, exchange/source, provider timestamp, and local receipt timestamp. |
| Error parsing | Expose HTTP/platform code, retryability, rate-limit class, available quantity/buying power, and a redacted message. |

Use a proven official SDK when it is reliable and observable. Wrap it when raw request timing, response status, or payload evidence would otherwise be lost.

## Canonical Run Directory

Emit these files when applicable:

```text
run/
  run_context.json
  decision_phase_timings.json
  run_events.jsonl
  target_weights_snapshot.json
  executable_target_projection.json
  order_plan.json
  execution_records.json
  broker_positions_before_raw.json
  broker_positions_after_raw.json
  broker_account_before.json
  broker_account_after.json
  broker_api_audit.jsonl
  execution_summary.json
```

The bundled analyzer also recognizes the legacy name `alpaca_api_audit.jsonl`.

## Canonical Execution Record

Each `execution_records.json` item should contain:

```json
{
  "symbol": "XYZ",
  "side": "buy",
  "qty": 12.5,
  "stage": "entry",
  "status_latest": "filled",
  "filled_qty": 12.5,
  "remaining_qty": 0.0,
  "batch_started_at_utc": "2026-01-01T15:30:00.000Z",
  "queue_wait_ms": 1200.0,
  "order_wall_time_seconds": 4.2,
  "batch_requested_workers": 10,
  "batch_worker_safety_cap": 10,
  "batch_effective_workers": 10,
  "attempt_count": 1,
  "submit_error_class": "",
  "attempts": []
}
```

Each attempt should preserve quote observation, broker submit/acknowledgment, polling, cancel/requote, fill, and terminal timestamps. Preserve the quote source, age, bid/ask, spread, limit offset, submitted quantity, filled quantity, broker status, and redacted error class.

## Canonical API Audit Row

Write one JSONL row per request:

```json
{
  "seq": 1,
  "started_at_utc": "2026-01-01T15:30:00.000Z",
  "method": "POST",
  "operation": "submit_order",
  "elapsed_ms": 742.1,
  "ok": true,
  "status_code": 200,
  "error_type": ""
}
```

Do not store API keys, authorization headers, complete account IDs, full URLs containing secrets, or unrestricted request/response bodies. Hash or summarize sensitive payloads.

## Stable Capacity Mapping

Map broker fields to one stable post-trade gross-capacity target. For a RegT-style account, prefer:

```text
total_regt_capacity = current_gross_position + remaining_regt_buying_power
target_gross = configured_ratio * total_regt_capacity
```

Record the source fields and formula. Do not compare a post-trade position directly with a remaining-buying-power value whose denominator changes after every fill.

## Adapter Acceptance Tests

Before broad experiments, prove:

1. Test and production immutable IDs differ.
2. Paper/sandbox endpoint is enforced.
3. One long fractional buy and exact close reconcile.
4. One whole-share short open and cover reconcile.
5. Partial fill, cancel, and already-filled cancel responses are classified correctly.
6. Rate limits are surfaced and counted.
7. Position quantity and available quantity are not conflated.
8. Client order IDs are unique and bounded to platform rules.
9. Raw and canonical evidence can trace every submitted order.

