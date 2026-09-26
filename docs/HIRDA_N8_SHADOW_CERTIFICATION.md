# HIRDA N8 Shadow Certification

This milestone certifies the production shadow rollout of N8 Cognitive Router Integration against the existing semantic routing behavior.

## Production state

Production remains in `shadow` mode.

- N8 infrastructure routing is enabled for observation only.
- N7 does not filter or reorder cognitive runtime candidates in shadow mode.
- No prompt execution is performed by the certification harness.
- Existing semantic lane/runtime policy remains authoritative.

The production cognitive status exposes:

- `node_routing.mode = shadow`
- six cognitive capability mappings
- three runtime bindings

## Capability mapping

All current cognitive semantic capabilities map to the provider-neutral infrastructure capability:

`cognitive-execution`

The semantic meaning of the cognitive request is still interpreted by Cognitive Router policy. N7 only verifies that the bound infrastructure route is eligible.

## Explicit runtime bindings

Current production bindings are:

- Claude -> `openclaw / claude-runtime`
- Codex -> `openclaw / codex-runtime`
- Hermes -> `openclaw / hermes-runtime`

No model binding is asserted yet because current live inventory does not provide a certified stable model identity for every cognitive runtime.

## Certification method

`scripts/certify_cognitive_node_routing.py` performs plan-only certification.

For each cognitive capability it:

1. reads live runtime/pane state from the running control plane;
2. builds an explicit `off`-mode baseline using that same live runtime state;
3. builds an N8 `shadow` plan using the same state;
4. compares semantic lane and runtime candidate order;
5. verifies every shadow candidate has an eligible N7 route;
6. verifies the live production shadow planner matches the locally reproduced shadow plan.

Separately, every configured runtime binding is resolved through N7 to prove the bound node/backend route is eligible.

## Certified cognitive cases

The suite covers:

- social_dialogue
- reflection
- fast_utility
- reasoning_high
- coding
- research

The fast cases remain in the fast lane and the deep cases remain in the deep lane.

## Required pass gates

Shadow certification passes only when all of the following are true:

- production mode is shadow;
- every configured runtime binding is N7-eligible;
- all six cognitive cases pass;
- candidate order is unchanged from the explicit off-mode baseline;
- semantic lane is unchanged;
- live service shadow output matches the reproduced shadow output;
- identity continuity remains true;
- semantic authority remains unchanged;
- no prompt execution occurs;
- semantic drift equals zero.

Any failed gate keeps production in shadow.

## Latest certified result

The certification completed with:

- 3/3 runtime bindings eligible;
- 6/6 cognitive cases passing;
- candidate-order continuity passing;
- lane continuity passing;
- identity continuity passing;
- semantic authority unchanged;
- prompt execution: none;
- semantic drift: 0.

The generated machine-readable report is written to:

`data/certification/n8-shadow-latest.json`

The report is operational evidence and is not required to be committed.

## Rollback

Rollback is configuration-only:

```yaml
cognitive_node_routing_mode: off
```

Then restart `mcp-studio.service`.

No Node Registry, machine policy, runtime binding, or semantic-routing data migration is required to return to the pre-N8 execution behavior.

## Next gate

Keep production in shadow while preparing controlled enforce certification.

Enforce must not be enabled until a dedicated certification proves that:

- an eligible primary runtime remains executable;
- an unavailable bound runtime is filtered before prompt dispatch;
- fallback order still follows Cognitive Router semantic policy;
- machine execute policy remains the hard ceiling;
- identity continuity remains true;
- semantic authority remains external/unchanged;
- semantic drift remains zero.
