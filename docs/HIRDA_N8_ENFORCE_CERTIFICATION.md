# HIRDA N8 Controlled Enforce Certification

This milestone certifies N8 enforce behavior without switching production out of shadow mode.

## Safety model

Production remains:

`cognitive_node_routing_mode: shadow`

The certification process creates in-process registry/router clones from live runtime and node observations. It does not mutate production policy, node health, runtime bindings, or cognitive mode.

No prompt is dispatched by the certification harness.

## Enforce gates

Controlled enforce certification proves four boundaries:

1. Healthy enforce preserves the same semantic lane and runtime order as an explicit off-mode baseline.
2. A simulated unhealthy primary backend is filtered by N7 before prompt dispatch.
3. Remaining fallback runtimes preserve the original semantic order after the unavailable runtime is removed.
4. A simulated machine policy with `execute=false` filters every cognitive runtime and proves machine policy remains the hard ceiling.

Identity continuity and semantic authority must remain unchanged in every scenario.

## Live inputs

The harness reads the current production runtime/pane state from:

`GET /api/cognition/status`

and uses the production shadow plan only for current lane-state observations.

N3-N6 node/backend/capability/health observations are rebuilt from the current production configuration in an isolated in-process Node Registry.

## Backend-unavailable simulation

For each of the six certified cognitive capabilities, the current semantic primary runtime is identified from the off-mode baseline.

The bound backend for that primary runtime is then marked unavailable only inside the cloned health observation.

Enforce planning must:

- remove that runtime;
- report an N7 `backend_unavailable` rejection;
- never include the filtered runtime in the executable candidate set;
- preserve the relative order of all surviving semantic candidates.

This expected infrastructure filtering is not semantic drift.

## Machine-policy simulation

A cloned StudioConfig is created with the local machine's `execute` permission disabled.

The real N7/N8 code paths are then evaluated in enforce mode.

Certification passes only when:

- zero executable cognitive candidates remain;
- N7 reports `permission_denied`;
- no prompt dispatch occurs;
- identity continuity remains true;
- semantic authority remains unchanged.

Production machine policy is never modified.

## Semantic drift definition

Semantic drift remains zero when:

- semantic lane is unchanged; and
- healthy enforce preserves the baseline runtime order; or
- in a simulated infrastructure failure, the enforce order equals the baseline order with only the ineligible runtime removed.

Infrastructure eligibility is allowed to filter candidates. It is not allowed to reorder the surviving semantic candidates.

## Certification command

```bash
./.venv/bin/python scripts/certify_cognitive_enforce_routing.py
```

The machine-readable report is written to:

`data/certification/n8-enforce-latest.json`

## Cutover rule

Passing controlled enforce certification does not itself change production to enforce.

After certification passes, production stays in shadow until explicit operator approval to change:

```yaml
cognitive_node_routing_mode: enforce
```

followed by a controlled service restart and a post-cutover live certification.

## Rollback

The rollback remains configuration-only:

```yaml
cognitive_node_routing_mode: shadow
```

or, for full N8 rollback:

```yaml
cognitive_node_routing_mode: off
```

No Node Registry or cognitive state migration is required.
