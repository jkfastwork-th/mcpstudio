# HIRDA Cognitive Live Soak / Observability

This milestone adds reporting-only observability for production cognitive routing after N8 enforce cutover.

## Goals

The soak layer answers:

- which runtime actually succeeds or fails;
- when fallback is used;
- which N7 rejection reasons filter candidates;
- selected node/backend load and probe latency;
- plan and execution latency;
- whether identity continuity or semantic authority ever changes;
- whether a runtime that looks healthy at the runtime layer is nevertheless filtered by N7.

Observability never changes route eligibility, policy, runtime order, or semantic authority.

## Event ledger

Plan and execution observations are written to HIRDA's existing append-only event ledger.

Event kinds:

- `cognition.plan.observed`
- `cognition.execute.observed`
- `cognition.endpoint.error`

The observer is fail-open. A ledger failure is reported as `last_observer_error` and must not fail a cognitive request.

## Privacy

The observer intentionally does not persist:

- prompts;
- model outputs;
- attempt detail text.

It records only routing and operational metadata such as request ID, capability, lane, runtime IDs, status/error codes, filter reasons, latency, load, and backend probe latency.

## Suspected false rejection

A `suspected_false_rejection` is a review signal, not a verdict.

It is emitted when:

1. production routing is in enforce mode;
2. N7 filters a runtime; and
3. the runtime inventory itself still reports that runtime as ready.

The infrastructure rejection may still be correct because machine policy, backend health, freshness, or lineage can fail independently of runtime readiness.

## Soak report

Read-only endpoint:

`GET /api/cognition/observability?window_minutes=1440`

The report includes:

- plan/execution counts;
- success/failure rate;
- fallback/degraded counts;
- filtered runtime counts;
- N7 rejection reasons;
- runtime successes/failures;
- attempt error codes;
- average/P95 plan and execution latency;
- selected route load/backend latency;
- suspected false rejection count;
- identity/semantic-integrity counters.

## Reporting states

The reporting state is informational only:

- `warming`: fewer than the configured minimum execution samples;
- `healthy`: enough samples and no configured warning threshold exceeded;
- `degraded`: failure/fallback warning threshold exceeded or false-rejection review signal exists;
- `down`: identity continuity or semantic-authority integrity failure was observed.

Defaults:

- window: 24 hours (1440 minutes);
- minimum executions: 20 within that rolling 24-hour window;
- execution failure warning: 5%;
- fallback warning: 25%.

These thresholds do not affect routing.

## Certification

Activation check:

```bash
./.venv/bin/python scripts/certify_cognitive_soak.py
```

After enough organic/live executions:

```bash
./.venv/bin/python scripts/certify_cognitive_soak.py --require-min-executions
```

Machine-readable evidence is written to:

`data/certification/cognitive-soak-latest.json`

## Next step

Keep production enforce running and accumulate real execution samples. Investigate any suspected false rejection or integrity failure before expanding to multi-machine cognitive routing.
