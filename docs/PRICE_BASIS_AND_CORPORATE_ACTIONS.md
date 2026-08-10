# Price Basis and Corporate Actions

## Invariants

The system uses separate price bases for separate economic meanings:

| Workflow | Price basis | Reason |
| --- | --- | --- |
| Return-based Alpha (`reversal`, `momentum`, `beta`) | Alpaca `split` or `all`; default `all` | Splits must never appear as investment returns. `all` also includes cash-dividend total return. |
| Absolute price filters and liquidity | Alpaca `raw` | These features describe prices and traded dollars that were actually observable at the time. |
| Market capitalization | Raw historical price multiplied by split-aligned point-in-time shares | Price and share count must be expressed in the same split units. |
| Live sizing and execution | Current unadjusted broker or quote-provider price | Order quantities are submitted in current shares and current currency units. |
| Portfolio backtest | Raw OHLC, explicit split quantity changes, and explicit dividend cash | This preserves executable share units and prevents corporate actions from being counted twice. |
| Backtest benchmark | Alpaca `all` | Benchmark curves are total-return curves independent of the Alpha experiment setting. |

`raw` and `dividend` are invalid Alpha return adjustments because neither protects return factors from split jumps. `split` is valid for price-return research; `all` is valid for total-return research and remains the production default.

## Alpha Panel Contract

Every newly generated Alpha panel carries explicit evidence fields:

- `raw_close`, `adjusted_close`, `lagged_raw_close`, and `lagged_adjusted_close`
- `alpha_price_adjustment`, `alpha_return_price_source`, and `absolute_price_source`
- `adjustment_factor = adjusted_close / raw_close`
- `market_cap_price_asof_session_date`
- reported shares, split factor, price-basis shares, adjustment window, and split dates

`close` remains a compatibility alias for `raw_close`. It must not be used to calculate return-based Alpha.

The executor writes `alpha_price_basis.json` and rejects panels when required fields are absent, configured and observed adjustments disagree, raw/adjusted bar coverage differs, or reported shares cannot be aligned to the raw market-cap price date. Cached panels created before this contract must be regenerated.

## Share Alignment

SEC shares are point-in-time facts and may remain in pre-split units until the next filing. For spot shares, split actions after `share_period_end` and through `market_cap_price_asof_session_date` are multiplied into the reported value. For weighted-average fallback shares, the adjustment window starts no earlier than the filing date because filed per-share data may already be retrospectively restated.

The Alpha panel preserves both `shares_outstanding_reported` and `shares_outstanding_price_basis`; `shares_outstanding` is the aligned value used by market capitalization.

## Backtest Timing

For each `session_open -> next_session_open` interval:

1. Value current holdings at the raw session open.
2. Rebalance at raw session-open prices.
3. Apply corporate actions whose effective date is `next_session_date`.
4. Value the resulting holdings at the raw next-session open.

Applying actions at step 3 is essential. Applying them at the next loop's start would record a false split/dividend loss in one daily return and a false recovery in the next.

Cash dividends are booked as economic ex-date cash. Payable-date settlement timing is intentionally not modeled. Splits and cash dividends are supported. Any other corporate action affecting a held position fails the backtest explicitly instead of silently producing an invalid return.

## Evidence

- Live/decision runs: `alpha_price_basis.json`
- Alpha CLI: `<alpha-panel-stem>_price_basis.json`
- Backtest: `price_basis.json`, `corporate_actions.json`, corporate-action columns in `daily_backtest_results.csv`, and `price_basis` / `corporate_actions` sections in `backtest_summary.json`
- Daily audit: `15_data_quality_snapshot.json` and the `price_basis` evidence-completeness group
