---
name: iterate-paper-trading-execution
description: Build, benchmark, and iteratively optimize a broker or exchange execution workflow using isolated paper/sandbox accounts, real submitted test orders, execution-stage timelines, canonical audit artifacts, and quality-gated A/B comparisons. Use when Codex needs to migrate this trading system to another broker, develop a new execution adapter, investigate slow or inaccurate execution, tune order concurrency/retries/quoting, reproduce ideal-vs-actual position gaps, or prove an execution change before production deployment.
---

# Iterate Paper Trading Execution

Optimize the actual execution path, not a mocked approximation. Keep broker-specific code behind an adapter and emit one canonical evidence set so the same experiment and acceptance gates work on every platform.

## Read The Relevant References

- Read [references/adapter-contract.md](references/adapter-contract.md) before adding or replacing a broker adapter.
- Read [references/metrics-and-gates.md](references/metrics-and-gates.md) before designing an experiment or deciding whether a candidate wins.
- Read [references/project-map.md](references/project-map.md) when working in this repository or using the Alpaca implementation as a migration example.

## Follow The Workflow

### 1. Freeze Scope And Invariants

Write down before testing:

- The ideal portfolio semantics, including signed weights, integer/fractional rules, and buying-power target.
- The execution stage boundary. Start at submission preparation and end after trading reconciliation; exclude decision downloads and post-run reporting from execution latency.
- The one variable being tested, such as worker count, quote age, retry wait, order style, or repair policy.
- The acceptance and stop gates from `metrics-and-gates.md`.

Do not change alpha, target construction, and execution mechanics in one comparison.

### 2. Prove Account Isolation

Fail closed unless all checks pass:

1. Resolve test and production profiles independently.
2. Verify their immutable account IDs differ.
3. Verify the test endpoint is paper/sandbox, the account is active, trading is allowed, and required capabilities such as shorting are enabled.
4. Verify the market clock and asset capabilities from the broker, not local assumptions.
5. Check that no production scheduler or executor uses the test profile.
6. Redact credentials and complete account IDs from logs and reports.

Never infer isolation from profile names alone. Never submit experimental orders to production.

### 3. Establish The Adapter Contract

Implement the minimum broker operations and canonical artifacts in `adapter-contract.md`. Preserve raw broker responses separately, but normalize every platform into the same order, attempt, API-call, position, and target fields.

Make all lifecycle transitions observable: queued, quote acquired, submitted, acknowledged, partially filled, canceled, requoted, filled, reconciled, or failed.

### 4. Capture A Baseline

Use real paper/sandbox submissions with deterministic targets:

- Prefer the same eligible universe, target count, long/short balance, random seed, buying-power target, and quote-age rule across candidates.
- Use at least three successful baseline rounds when the market window permits. Treat one round as directional evidence only.
- Begin with the current safe configuration.
- Persist each run in a separate immutable directory.
- Do not edit imported execution source while an experiment subprocess is running. Stop the run first or wait for completion; a new child process can otherwise load a different source revision between rounds.

Record both strategy-to-actual and executable-to-actual errors. The latter isolates execution quality from unavoidable integer-share and buying-power projection error.

### 5. Read The Execution Timeline

Attribute elapsed time using overlapping tracks:

- Stage spans: preparation, release/reduction, reconciliation/reprojection, entry, repair, final trading reconciliation.
- One lane per overlapping logical order.
- Child spans for quote acquisition, submit, polls, cancel, and requote attempts.
- Broker API lanes with status code and latency, excluding secrets and payload bodies.

Calculate aggregate order work, parallel wall time, speedup, queue mean/P95/max, and effective concurrency. A long queue with low API failure rate supports increasing concurrency. Rising 429/5xx, submit errors, or fill loss identifies the safe boundary.

### 6. Form One Falsifiable Hypothesis

State the expected mechanism and measurable result. Examples:

- "Increase workers from 6 to 10 to reduce queue P95 without any 429 or fill-rate regression."
- "Fetch quotes in parallel before sizing to reduce preparation latency while keeping quote age below 10 seconds."
- "Retry an exact fractional long close one minimum unit lower only after the broker reports a zero-boundary precision rejection."

Change one causal dimension per run. Add audit fields before the experiment when existing evidence cannot prove or disprove the hypothesis.

### 7. Run Guarded A/B Tests

Run the baseline and candidate on the isolated account. Keep inputs comparable and preserve every artifact. Monitor live stop conditions during submission; do not wait for the final report to notice rate limiting or unsafe exposure.

Use the bundled analyzer after adapters emit canonical artifacts:

```powershell
python .codex/skills/iterate-paper-trading-execution/scripts/analyze_execution_ab.py `
  --run baseline=artifacts/experiment/baseline/execution `
  --run candidate=artifacts/experiment/candidate/execution `
  --output artifacts/experiment/ab_report
```

Read the generated JSON for machine decisions and Markdown for review.

### 8. Apply Quality Gates Before Speed

Reject a faster candidate when any hard gate fails. In priority order:

1. No production-account exposure or account isolation failure.
2. No uncontrolled gross exposure, side inversion, or buying-power breach.
3. No submit errors, terminal unfilled orders, or rate-limit responses unless the experiment explicitly studies recovery and returns the account to a verified state.
4. No executable-to-actual weight-error regression beyond tolerance.
5. Gross utilization remains within the target band.
6. Only then compare execution elapsed time and queue latency.

When a higher setting crosses a hard gate, preserve the failed run as boundary evidence and add a runtime safety cap rather than merely changing a default.

### 9. Repair Newly Exposed Edge Cases

Use the exact failed order and broker response to add the narrowest safe fallback. Verify three layers:

- Unit test reproducing the broker condition.
- Focused real paper/sandbox order that forces the condition when safe.
- Full representative run when the market window permits.

Record fallback count, original request, adjusted request, residual exposure, and broker response class. Do not silently convert rejected orders into apparent fills.

### 10. Deploy And Verify

After tests pass:

1. Update the platform default and a defensive runtime cap or invariant when evidence supports one.
2. Run focused execution tests, timeline tests, compilation/static checks, and any platform adapter tests.
3. Confirm no experiment process is active.
4. Restart through the project's supported restart entry point.
5. Verify process ownership, scheduler/tray/dashboard health, loaded defaults, and timeline API output.
6. Commit and push code. Leave raw experiment artifacts uncommitted unless repository policy says otherwise.

Report the baseline, winning candidate, rejected boundary, residual risks, exact verification evidence, and whether a full post-fix representative run was completed.

## Preserve These Boundaries

- Do not optimize post-run audit duration when the user asks specifically about execution latency; report it separately.
- Do not call a run successful only because orders were submitted. Require final fills, reconciliation, target error, and exposure checks.
- Do not average failed/no-order runs into latency or execution-error statistics as if they were valid observations. Classify failure phase first.
- Do not use changing remaining buying power as the portfolio target denominator. Use the platform's stable total capacity definition and record how it was reconstructed.
- Do not weaken weight tracking to fill more buying power. Optimize target-weight error first, gross utilization second.
- Do not erase failed boundary experiments; they define the safe operating envelope.
