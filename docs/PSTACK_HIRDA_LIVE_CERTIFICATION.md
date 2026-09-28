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

The live run exposed a HIRDA observability inconsistency.

The plan correctly records:

- `semantic_preference = codex`
- `semantic_execution_runtime = codex`
- `semantic_routing.applied = true`

When controlled team delegation takes over, however, the actual execution path is:

`Claude supervisor → Hermes/Bots → Claude supervisor review`

Codex is a semantic specialist hint in this topology and is not invoked in the recorded attempts.

The mismatch was fixed on this feature branch by `fdfcd26 fix(cognition): align team execution telemetry`.

Controlled-team results now preserve the semantic preference while recording:

- `execution_applied = false`
- `execution_authority = controlled_team`
- `execution_runtime = claude`
- `specialist_runtime = codex`
- `semantic_authority_changed = false`

Observability also separates plan-only semantic changes from realized execution authority changes. `semantic_plan_change_count` records canary planning activity, while `semantic_authority_change_count` remains an execution-integrity signal. A direct canary that actually changes the executed runtime still reports an integrity change and can drive health to `down`.

Post-fix verification:

- cognitive router + observability: `95 passed`
- Pstack + plugin lifecycle + cognitive suites: `124 passed`
- broad suite excluding the two pre-existing OAuth collection failures: `683 passed, 5 skipped`

## Certification result

Pstack methodology → HIRDA request translation → Supervisor Policy → Hermes Bots team execution → supervisor review is live-certified for bug-fix, feature, and review workflows.

The semantic telemetry mismatch found during certification is fixed on this branch. The branch is ready as a merge candidate once the dirty production `master` working tree is reconciled.
