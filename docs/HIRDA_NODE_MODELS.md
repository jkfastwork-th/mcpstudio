# HIRDA Node Model Discovery

N4 adds vendor-neutral model discovery beneath the N3 runtime/backend layer.

## Lineage

A discovered model is never a standalone node object. Its identity is scoped by:

`node_id + backend_id + model_id`

The node identity comes from the authenticated/enrolled Node Registry record. The backend identity must already exist in that node's `runtime.backends` observation. Model observations that reference an unknown backend are discarded fail-closed.

## Probe contract

Model discovery is configured as an optional `model_probe` nested under a generic backend probe. A model probe declares:

- executable `command`
- output `format`: `lines` or `json`
- optional JSON path
- configurable model ID/name fields
- bounded timeout

HIRDA core contains no provider-specific model parser.

## Observation

The versioned `hirda-node-models-v1` observation contains backend-scoped inventories and discovery status.

The control plane stamps the authoritative `node_id` during ingestion. Device runtimes and machine agents do not get to self-assert that identity in trusted registry state.

The resulting facet lives at:

`metadata.node_registry.enrichment.facets.runtime.models`

## Transport

The same model observation contract is used by:

- local control-plane probes;
- legacy HIRDA Machine Agent discovery;
- Shared Device Runtime over authenticated outbound WSS.

Shared Device Runtime sends model observations inside the existing `node_observations` message after Device Broker authentication.

## Authority boundary

N4 does not mutate:

- node identity;
- per-machine policy;
- configured providers;
- node capabilities;
- workspace mapping;
- backend identity.

Model inventory remains refreshable observation state, not durable node identity truth.

## N5 capability advertisement

Model capability claims now live in the separate `capabilities.advertised` facet and are accepted only when their backend/model lineage exists in N3/N4 observations. See `docs/HIRDA_NODE_CAPABILITIES.md`.

## N6 integration

Model identity remains separate from health; N6 contributes node/backend availability that N7 can combine with this model lineage. See `docs/HIRDA_NODE_HEALTH.md`.

## N7 router integration

Model capability routes now require ready N4 model lineage, an eligible backend, machine policy, and fresh N6 availability. See `docs/HIRDA_CAPABILITY_ROUTER.md`.

## N8 integration

Cognitive runtime bindings may explicitly reference N4 model lineage beneath a backend, and N7 validates that lineage before the runtime remains eligible. See `docs/HIRDA_COGNITIVE_ROUTER.md`.

## Roadmap status

N1-N8 are complete.
