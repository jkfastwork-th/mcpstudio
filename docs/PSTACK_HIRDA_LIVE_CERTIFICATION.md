# Pstack → HIRDA Live Certification

Date: 2026-09-28
Branch: `feature/pstack-hirda`
Adapter commit under test: `08a5779 feat(pstack): add HIRDA methodology adapter`

## Scope

This certification proves the integration boundary in two parts:

1. The Pstack plugin adapter in the isolated worktree loads the pinned upstream methodology and emits runtime-neutral `hirda-cognitive-request-v1` requests.
2. Those requests execute through the live HIRDA cognitive plane on port 8100.

The Pstack plugin was not installed into the production working tree for this run. Plugin trust/lifecycle activation had already been certified locally through `IntegrationManager`.

## Preconditions

- Pstack upstream: `michael-denyer/pstack-claude`
- Pinned revision: `c02fd4922b25ee005f42042463d741d236c2c35e` (`v0.9.45`)
- Semantic routing: `canary`
- Supervisor policy: `enforce`, preferred supervisor `claude`
- Hermes team delegation: `canary`, explicit request opt-in
- Team topology: `claude>hermes>bots>hermes>claude`
- Workers: `coder`, `tester`, `reviewer`

Claude and Codex had stale HIRDA runtime backoff entries when certification resumed. Direct probes succeeded for both runtimes, so the stale backoff lane states were cleared through HIRDA's public lane-state API before certification.

## Plan-only certification

All three workflows produced the same authority split:

- semantic preference: `codex`
- semantic execution runtime: `codex`
- strategic supervisor: `claude`
- Hermes team delegation: applied
- runtime pin from Pstack: none
- candidates: `codex`, `claude`, `hermes`

This confirms that Pstack supplies methodology/role intent while HIRDA retains runtime and supervisor authority.

## Live execution results

| Workflow | Request | Result | Elapsed | Team events | Recovery | Degraded | Fallback |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| bug-fix | `PSTACK-091D1E840F17` | success | 125.18 s | 16 | 0 | no | no |
| feature | `PSTACK-1BE8F3699AE2` | success | 292.68 s | 17 | 0 | no | no |
| review | `PSTACK-9828E3FF4EB8` | success | 209.64 s | 18 | 0 | no | no |

Every live run completed these stages successfully:

1. `supervisor_plan` on Claude
2. `team_execution` through Hermes group chat with coder/tester/reviewer
3. `supervisor_review` on Claude

No certification task modified repository files.

## Quality evidence

### Bug-fix

The team correctly diagnosed the inverted clamp expression:

`min(0, max(10, x))`

and produced the corrected clamp:

`min(10, max(0, x))`

The final review explicitly distinguished manual tracing from runtime testing.

### Feature

The team caught a subtle specification mismatch that was not in the initial obvious path: Python `str.lower()` lowercases non-ASCII letters as well, so it does not satisfy an ASCII-only lowercase contract when non-ASCII characters must remain unchanged.

### Review

The team covered empty input, booleans, non-numeric values, non-finite values, and sized-vs-unsized input. The final Claude review additionally caught a large-number overflow edge case missed by all three worker reports.

## Production-load observation

The feature run overlapped with live Nova/social-dialogue traffic. Hermes was actively handling other production work during the certification window. The feature run became slower but still completed without degradation, fallback, or recovery rounds.

This is useful evidence that the team path can complete under real runtime contention.

## Finding: semantic telemetry mismatch

A material observability inconsistency was exposed.

The plan reports:

- `semantic_preference = codex`
- `semantic_execution_runtime = codex`
- `semantic_routing.applied = true`

But when team delegation is applied, the actual execution path is:

`Claude supervisor → Hermes/Bots → Claude supervisor review`

Codex is not invoked in the recorded attempts.

At the same time, cognitive observability records `semantic_authority_changed=true` from the canary ranking and therefore reports health state `down` with `semantic_or_identity_integrity_failure`, even though all three controlled-team executions completed successfully and the supervisor policy itself reports `semantic_authority_changed=false`.

This is not a Pstack adapter failure. It is a HIRDA cognitive-observability / execution-semantics mismatch exposed by the Pstack certification.

## Certification result

Pstack methodology → HIRDA request translation → Supervisor Policy → Hermes Bots team execution → supervisor review is live-certified for bug-fix, feature, and review workflows.

Promotion into the production plugin set should remain separate from fixing or explicitly accepting the semantic telemetry mismatch above.
