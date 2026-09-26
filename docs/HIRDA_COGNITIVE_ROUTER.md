# HIRDA Cognitive Router Integration

N8 connects the existing HIRDA Cognitive Router to the N7 Capability Router without transferring semantic authority to infrastructure routing.

## Separation of authority

HIRDA keeps two routing layers distinct:

1. Cognitive policy decides the semantic compute lane and ordered runtime candidates.
2. N7 decides whether each explicitly bound infrastructure route is currently eligible.

N7 may remove an unavailable, stale, policy-denied, or lineage-invalid runtime candidate. It does not reorder semantic candidates or choose a different compute depth.

JEV remains advisory evidence only. Identity continuity remains preserved and `semantic_authority_changed` remains false.

## Explicit mapping

HIRDA never infers a node/backend/model binding from runtime, provider, or model names.

Two explicit configuration maps connect the layers:

- `cognitive_capability_routes`: cognitive request capability -> advertised N5 capability
- `cognitive_runtime_bindings`: runtime ID -> node/backend/model lineage

A runtime binding requires `machine_id`. A model binding also requires `backend_id`.

Example shape:

```yaml
cognitive_capability_routes:
  fast_utility: chat

cognitive_runtime_bindings:
  runtime-a:
    machine_id: node-a
    backend_id: local-inference
    model_id: model-a
```

These are deployment mappings, not provider rules in HIRDA core.

## Rollout modes

`cognitive_node_routing_mode` supports:

- `off`: preserve the pre-N8 cognitive path and do not call N7.
- `shadow`: evaluate N7 for every runtime binding, expose decisions, but do not filter candidates.
- `enforce`: a runtime candidate must have a mapped capability, explicit binding, an available N7 resolver, and an eligible N7 route.

Enforce mode fails closed when mapping, binding, lineage, policy, health, or availability requirements are missing.

## N7 request contract

For every cognitive runtime candidate, N8 calls N7 with:

- the mapped advertised capability;
- permission class `execute`;
- explicit machine ID;
- optional backend ID;
- optional model ID.

This means existing per-machine execute policy remains the hard ceiling for cognitive execution.

## Planning and execution

`/api/cognition/plan` includes an `infrastructure_routing` report containing:

- rollout mode;
- mapped advertised capability;
- candidate counts before/after filtering;
- per-runtime binding;
- N7 selected route or rejection summary;
- eligible runtime IDs;
- filtered runtime IDs;
- `routing_authority: infrastructure_eligibility_only`;
- `semantic_authority_changed: false`.

Each surviving candidate may include its explicit `infrastructure_route`.

`/api/cognition/execute` uses the filtered plan in enforce mode and returns the same infrastructure routing report in the cognitive result.

The legacy `attempts` schema is intentionally unchanged. `fallback_used` continues to describe runtime execution failover, not infrastructure pre-filtering.

## Failure behavior

In enforce mode these conditions remove the runtime candidate:

- cognitive capability has no N5 mapping;
- runtime has no explicit node binding;
- N7 resolver is unavailable;
- N7 rejects the bound route;
- route evaluation errors.

If all candidates are removed, execution returns the existing `no_healthy_cognitive_runtime` failure without dispatching a prompt.

## Backward compatibility

The default mode is `off`, so existing deployments do not change behavior until the operator explicitly starts shadow/enforce rollout.

The existing runtime pane selection, target-pane pinning, fallback rules, result framing, identity continuity, and external semantic authority contracts remain unchanged.

## Roadmap status

N8 completes the Node Registry integration roadmap N1-N8.

The next step is end-to-end certification and controlled cutover: certify N7/N8 shadow decisions against live node/backend/model observations, then enable enforce mode only for explicitly bound and certified cognitive runtimes.
