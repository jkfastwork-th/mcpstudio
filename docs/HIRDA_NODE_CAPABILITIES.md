# HIRDA Node Capability Advertisement

N5 adds vendor-neutral capability advertisement on top of the N1-N4 node lineage.

## Capability scopes

A capability claim may be scoped to:

- the node;
- a discovered backend;
- a discovered model beneath a discovered backend.

The versioned observation schema is `hirda-node-capabilities-v1` and is stored at:

`metadata.node_registry.enrichment.facets.capabilities.advertised`

Backend claims reference `backend_id`. Model claims reference both `backend_id` and `model_id`.

## Lineage enforcement

The control plane is authoritative for lineage:

- `node_id` is stamped from the authenticated/enrolled Node Registry record;
- backend capability claims survive only when the backend exists in that node's N3 `runtime.backends` inventory;
- model capability claims survive only when the backend/model pair exists in that node's N4 `runtime.models` inventory.

Unknown backend/model references are discarded fail-closed.

## Advertisement is not authority

Capability advertisement is observation state only.

It does not mutate:

- `MachineDescriptor.capabilities`;
- per-machine policy;
- provider configuration;
- workspace mappings;
- session authority;
- routing decisions.

An authenticated node may describe what it can do, but that claim does not grant permission to do it. Existing machine policy remains the hard ceiling.

N7 Capability Router Integration will consume capability advertisements only together with policy, availability, health, and other routing constraints.

## Sources

The same capability observation contract is produced by:

- the local control-plane node;
- the legacy HIRDA Machine Agent;
- the Shared Device Runtime over authenticated outbound WSS.

Node capabilities originate from the node's configured/runtime declaration. Backend capabilities come from the generic N3 backend probe. Optional `model_capabilities` on a backend probe advertise capabilities for models discovered beneath that backend without adding provider-specific rules to HIRDA core.

## Vendor neutrality

HIRDA core does not contain provider, runtime, or model-family capability tables. Provider-specific adapters may translate their native metadata into this generic observation contract.

## N6 integration

Capability claims now have a separate refreshable health/load/availability signal with freshness semantics. See `docs/HIRDA_NODE_HEALTH.md`.

## N7 router integration

Capability claims are now evaluated by the Machine Capability Router together with policy, N3/N4 lineage, and N6 freshness/availability. Route resolution remains non-executing and does not grant authority. See `docs/HIRDA_CAPABILITY_ROUTER.md`.

## Next milestone

N8 — Cognitive Router Integration.
