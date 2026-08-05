# Alpaca Paper Position Disappearance And Restoration Recurrence

- Incident dates: 2026-08-04 and 2026-08-05
- Status: Escalated to Alpaca; broker investigation pending
- Severity: High for execution integrity and performance attribution
- Account: `PA3Q...NMX8` (paper)
- Assets: `VISN`, then `PRAX`
- VISN asset ID: `b4344ff5-3ab3-4c75-b7a0-be67746efc97`

## Summary

Alpaca's Positions API stopped returning an existing `VISN` long position of
`202.0043` shares without a corresponding order, fill, transfer, or corporate
action. Account cash was unchanged except for `-0.08 USD` of documented fees,
while account equity lost approximately the market value of the missing
position.

The executor correctly treated `GET /v2/positions` as the authoritative broker
state. Because the broker omitted `VISN`, the target reconciliation classified
the position as missing and bought `182.577` shares at approximately `12.22 USD`
to restore the strategy target. This creates a material duplicate-exposure risk
if Alpaca later restores the original shares server-side.

On 2026-08-05 that exact restoration occurred: Alpaca returned `VISN`
`384.5813 = 182.577 + 202.0043` shares, while `PRAX` `10.1725` shares
simultaneously disappeared without an explanatory event. The executor therefore
sold the restored VISN excess and repurchased PRAX. This establishes a recurring
broker position-ledger integrity problem rather than a one-time attribution
artifact.

## Impact

- Missing original position: `202.0043` shares of `VISN`.
- Last confirmed pre-incident market value: `2,391.730912 USD` at `11.84 USD`.
- Holding attribution residual: `-2,399.67581953 USD`.
- Unexplained residual after known fees: `-2,399.59581953 USD`.
- Reopened position: `182.577` shares filled at `12.22 USD`.
- Current recorded post-execution position: only the reopened `182.577` shares.
- The daily long/short attribution for this cycle is not strict-ready because
  position continuity is broken for `VISN`.

## Timeline

All timestamps below are UTC.

| Time | Event |
| --- | --- |
| 2026-08-03 14:02:27 | Previous execution completed. Broker snapshot contained `VISN` long `202.0043`, market value `2,375.570568 USD`, average entry `11.818538 USD`. Account equity was `104,996.77 USD`; cash was `105,330.05 USD`. |
| 2026-08-04 03:37 | Alpaca incident `r7qj8308gddb` began. Alpaca reported order-processing irregularities affecting Orders API and Positions API. |
| 2026-08-04 04:41 | Decision-stage broker snapshot still contained `VISN` long `202.0043`, market value `2,391.730912 USD`. |
| 2026-08-04 06:48 | Alpaca marked incident `r7qj8308gddb` resolved and asked users with discrepancies to contact support. |
| 2026-08-04 14:01:09 | Execute preflight `GET /v2/positions` returned HTTP 200 with 91 positions and omitted `VISN`; request ID `c8082213edee9acd40ccbf01ceda6271`. |
| 2026-08-04 14:01:12 | Second independent preflight call again returned 91 positions without `VISN`; request ID `f7b1d3af44000887b982c114b1923a5f`. |
| 2026-08-04 14:01:15 | Third independent preflight call again returned 91 positions without `VISN`; request ID `5a58db0f1bc8d24609c3fd41943c2bb6`. |
| 2026-08-04 14:01:18 | Stability check completed: all three samples had 91 symbols and omitted `VISN`. |
| 2026-08-04 14:02:28 | Executor submitted a target-restoration buy for `VISN`, order ID `ea50236a-53df-4ac0-a9e6-a18a3871468a`. |
| 2026-08-04 14:02:32 | Buy completed for `182.577` shares at average price `12.22 USD`. |
| 2026-08-04 14:02:43 | Execution cycle completed with equity `102,924.69 USD`. |
| 2026-08-04 14:04:39 | Daily attribution identified `VISN` as the sole symbol missing from the current pre-trade snapshot and measured a `-2,399.67581953 USD` holding residual. |
| 2026-08-05 04:44:10 | Decision-stage broker snapshot contained `PRAX` `10.1725` shares and replacement `VISN` `182.577` shares. This is `12:44:10` China Standard Time. |
| 2026-08-05 14:00:49 | First execution preflight call returned 94 positions, omitted `PRAX`, and returned `VISN` `384.5813`; request ID `17e1a8ba9a3a074832b6db12cb80f5f2`. |
| 2026-08-05 14:00:51 | Second call repeated the same symbol state; request ID `b09c5847e3221cb287403bb2004839ab`. |
| 2026-08-05 14:00:55 | Third call repeated the same symbol state; request ID `f54e1f4f2e0dc4f1da645a29fe67fa0b`. |
| 2026-08-05 14:01:23 | Executor sold `167.2184` restored excess VISN shares, order ID `8f66c93c-9894-41ef-a7b1-8ef94b2a69c4`. |
| 2026-08-05 14:02:01 | Executor bought `9.3256` PRAX shares to restore the target, order ID `e9b08e29-c694-4749-b7b4-b720835de1e4`. |
| 2026-08-05 14:03:59 | Attribution reported missing `PRAX`, mismatched `VISN`, `-629.65030513 USD` net holding residual, and approximately `5,645.91 USD` gross continuity break. |

## Evidence And Exclusions

### Broker state was the actual-state source

Prepare, reconciliation, and final position snapshots all came directly from
Alpaca `GET /v2/positions`. No local lot ledger, cached holdings, inferred
position, or Longbridge quote was used as actual position state.

The execute run recorded ten successful Positions API calls from `14:01:09` to
`14:02:40` UTC. The first three independent preflight samples consistently
omitted `VISN`, ruling out a single transient response as the immediate trigger.

### No disposition event explains the disappearance

The captured account activity contains only `FILL` and `FEE` activity types.
Before the executor's replacement buy, there was no `VISN` fill. No sale,
liquidation, transfer, ACAT event, journal, reorganization, merger, symbol
change, cash distribution, or stock distribution explains removal of the
original shares.

The relevant corporate-action query covered 2026-07-25 through 2026-08-07 and
returned actions only for `EIX`, `FCX`, `JPM`, and `MA`, not `VISN`. `VISN`
remained active and tradable under the same Alpaca asset ID.

Cash moved from `105,330.05 USD` after the prior execution to `105,329.97 USD`
before the current execution. The exact `-0.08 USD` change is accounted for by
one `-0.07 USD` TAF fee and one `-0.01 USD` CAT fee; there were no sale proceeds.

## Root-Cause Assessment

The strongest current hypothesis is a broker-side paper-position synchronization
or state-repair failure. Confidence is high that the disappearance occurred at
the broker/API state layer, but Alpaca must confirm the exact internal cause.

Supporting evidence:

1. The position was present during Alpaca incident `r7qj8308gddb` and absent in
   the first later execution snapshot after Alpaca's remediation window.
2. Alpaca's incident report states that duplicate executions were triggered
   during an OMS reset and lists Orders API and Positions API as affected.
3. A nearly identical 2026-07-07 paper incident was reported publicly: positions
   vanished, equity fell by their exact market value, cash did not receive sale
   proceeds, and account activity contained no disposition. Alpaca support called
   that event a paper-environment outage; users later reported server-side
   restoration.

The status incident page labels the affected component as Live Trading API, so
association with this paper account remains a hypothesis rather than a confirmed
fact until Alpaca responds.

## External References

- Alpaca incident: https://status.alpaca.markets/incidents/r7qj8308gddb
- Similar paper-account report: https://forum.alpaca.markets/t/alpaca-paper-account-positions-wiped/19225

## Response And Follow-Up

Completed:

- Preserved the source snapshots, account activities, corporate-action response,
  Positions API audit log, and attribution output in a local ignored evidence
  bundle with SHA-256 hashes.
- Submitted a support request to `support@alpaca.markets` at
  `2026-08-04T15:01:36Z`, asking Alpaca to investigate and repair the account,
  identify whether incident `r7qj8308gddb` affected it, and coordinate any
  restoration because replacement shares have already been bought. SMTP
  message ID: `<178585565850.49080.17486067313665924025@qq.com>`.
- Alpaca acknowledged the request as support ticket `#332101` and instructed
  urgent cases to use dashboard live chat while adding evidence by replying to
  the ticket email.
- Replied within ticket `#332101` on 2026-08-05 with the PRAX disappearance,
  exact VISN restoration identity, three new Positions API request IDs, resulting
  corrective orders, and a 255,362-byte evidence ZIP. QQ SMTP accepted the
  message with no refused recipients. SMTP message ID:
  `<178594065684.129888.11108367217710177458@qq.com>`.
- Sent a timestamp correction in the same ticket thread: the final decision
  snapshot was `2026-08-05T04:44:10Z` (`12:44:10` China Standard Time), so the
  broker state changed between that snapshot and `2026-08-05T14:00:49.568Z`.
  SMTP message ID: `<178595572919.82748.125915625444468282@qq.com>`.
- Did not reset the paper account, preserving broker-side evidence.

Pending:

- Alpaca confirmation of the root cause and affected records.
- Broker confirmation that the restored original VISN quantity and disappearing
  PRAX quantity were internal ledger repair/state failures.
- Broker validation of current equity, cash, and all open positions after the
  executor sold restored VISN excess and repurchased PRAX.
- Reconciliation after repair and annotation of any compensating broker event.

Operational caution until resolution:

- Treat any unannounced reappearance of the original `202.0043` shares as a
  broker repair, not alpha-generated exposure.
- Do not reset this paper account before Alpaca has reviewed the evidence.
- Preserve the current evidence bundle unchanged; append support correspondence
  and repair snapshots as new files.

## Evidence Location

The full account number and raw broker payloads are intentionally excluded from
Git. They are stored locally under:

- `artifacts/incidents/20260804_alpaca_visn_position_disappearance/`
- `artifacts/incidents/20260805_alpaca_prax_disappearance_visn_restoration/`
