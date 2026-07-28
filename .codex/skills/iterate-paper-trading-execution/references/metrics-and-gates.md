# Metrics And Acceptance Gates

Apply safety and quality gates before speed metrics. Tune thresholds to the platform and strategy, but make them explicit before the experiment.

## Primary Portfolio Metrics

### Strategy-to-actual L1

```text
sum over symbols(abs(raw_strategy_weight - actual_signed_weight))
```

This includes unavoidable projection loss from whole-share shorts, asset restrictions, and buying-power constraints.

### Executable-to-actual L1

```text
sum over symbols(abs(executable_expected_weight - actual_signed_weight))
```

Use this as the primary execution-quality metric. It isolates order execution and reconciliation from the target projector.

### Gross utilization

```text
actual_gross_notional / stable_total_capacity
```

Compare it with the configured target, currently 0.95 in this project. Keep capacity reconstruction stable across before/after snapshots.

## Execution Timeline Metrics

- Execution-stage elapsed seconds.
- Stage elapsed seconds for prepare, release, reconcile/reproject, entry, repair, and finalize.
- Aggregate order work seconds.
- Parallel order wall seconds.
- Parallel speedup ratio: aggregate work / parallel wall.
- Queue wait mean, median, P95, and max.
- Requested, safety-capped, and effective worker counts.
- Attempt, cancel/requote, and terminal-order counts.
- API call count, latency percentiles, max concurrency, 429 count, and 5xx count.
- Quote age and spread distributions at each attempt.

## Default Hard Gates

Use these defaults unless the user or platform contract specifies stricter values:

| Gate | Default |
| --- | --- |
| Account isolation | Test ID must differ from production ID; paper/sandbox endpoint required. |
| Logical fill rate | 100% for representative runs. |
| Submit errors | 0. |
| Terminal unfilled/canceled/rejected | 0. |
| HTTP/platform rate limits | 0. |
| Uncontrolled side crossing | 0. |
| Buying-power or gross-capacity breach | 0. |
| Executable-to-actual L1 regression | No material regression; default tolerance 0.0005 absolute L1. |
| Gross utilization | Within 0.5 percentage points of target unless integer lattice makes this infeasible. |
| Evidence completeness | Every submitted client order ID maps to attempts and final broker evidence. |

Do not waive a hard gate because average latency improved.

## Stop Conditions During A Run

Stop new submissions and reconcile the test account when any occurs:

- Production/test identity ambiguity.
- Repeated rate limiting or authentication errors.
- Buying-power rejection indicating unsafe projection.
- Side inversion, fractional short rejection, or unexpected shortability change.
- Position reconciliation timeout after a release leg.
- Quote provider coverage/staleness failure above the configured threshold.
- Source code changes while a multi-round subprocess remains active.

Preserve the partial run and classify its failure phase. Do not fold it into successful latency statistics.

## Comparison Rules

1. Compare only runs with equivalent target construction and market regime when possible.
2. Report successful and failed runs separately.
3. Use medians and P95 across repeated rounds; do not rely only on means.
4. Treat a one-round result as a boundary probe, not broad performance proof.
5. Keep failed high-concurrency runs as evidence for a defensive cap.
6. Distinguish trading completion from post-run audit completion.
7. Explain projection error separately from execution error.

## Promotion Decision

Promote a candidate only when:

```text
all hard gates pass
and executable_to_actual_l1 <= baseline + tolerance
and gross utilization is in band
and execution elapsed or another declared objective improves materially
```

After promotion, verify the production scheduler actually passes the winning setting and the executor enforces the defensive bound independently.

