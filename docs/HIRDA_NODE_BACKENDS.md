# HIRDA Node Runtime / Backend Discovery

N3 adds vendor-neutral runtime/backend discovery to the N1 Node Registry and N2 observation pipeline.

## Contract

A backend probe is configuration, not a hardcoded product integration. Each probe declares:

- `id`
- `kind`
- executable `command`
- optional `version_args`
- optional capability labels
- bounded probe timeout

HIRDA emits a versioned `hirda-node-backends-v1` observation containing backend availability, resolved binary path, version text when requested, capability labels, and observation time.

## Sources

The same observation contract is used by:

- the local control-plane node via `studio.node_backend_probes`;
- the legacy HIRDA Machine Agent via its `backend_probes` config;
- the Shared Device Runtime via `backend_probes` and authenticated outbound WSS.

The Shared Device Runtime sends `node_observations` only after Device Broker authentication succeeds. The control plane accepts only known observation facets and ignores unknown facets fail-closed.

## Node semantics

Runtime/backend discovery enriches:

`metadata.node_registry.enrichment.facets.runtime.backends`

It does not replace or mutate:

- node identity;
- lifecycle authority;
- per-machine policy;
- configured providers;
- capability ownership;
- workspace mapping.

Backend observations are refreshable state, not durable identity truth.

## Vendor neutrality

Node Registry and N3 discovery core do not contain product-specific runtime rules. Provider presets or adapters may later supply generic probe specs without changing the core schema.

The existing Claude/Codex/Hermes lane inventory remains a separate operator/UI concern and is not used as Node Registry runtime truth.

## N4 model discovery

Backend-scoped model probes now populate the versioned `runtime.models` facet. The control plane stamps `node_id` and rejects inventories whose `backend_id` is not present in that node's discovered backend set. See `docs/HIRDA_NODE_MODELS.md`.

## N5 capability advertisement

Backend and model capability claims now reference the N3/N4 lineage and are stored separately from backend identity. See `docs/HIRDA_NODE_CAPABILITIES.md`.

## N6 health integration

Backend specs may now declare generic bounded health probes, and their current availability is reported separately from backend identity. See `docs/HIRDA_NODE_HEALTH.md`.

## N7 router integration

Backend capability routes now require both current N3 availability and fresh N6 backend health before becoming eligible. See `docs/HIRDA_CAPABILITY_ROUTER.md`.

## N8 integration

Cognitive runtime bindings can explicitly reference N3 backend identity; no provider/model-name inference is performed. See `docs/HIRDA_COGNITIVE_ROUTER.md`.

## Roadmap status

N1-N8 are complete.
