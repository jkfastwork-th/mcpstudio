# HIRDA Node Hardware & Resource Discovery

N2 adds vendor-neutral hardware/resource observation on top of the N1 Node Registry lifecycle.

## Scope

N2 discovers resource facts about a node without changing node identity, authority, policy, capability ownership, or provider configuration.

The observation schema is `hirda-node-hardware-v1` and currently records:

- CPU logical count
- machine architecture
- processor string when the operating system exposes one
- physical memory total
- storage total / used / free for the probed filesystem volume
- observation timestamp

## Local nodes

The control plane probes the local node during boot and writes the observation into the existing node record at:

`metadata.node_registry.enrichment.facets.hardware.resources`

The observation source is `local_probe`.

## Remote nodes

HIRDA Machine Agent includes the same versioned hardware observation under its authenticated `/identity` response.

Enrollment creates the node record first. Only after that record exists does HIRDA enrich it with the hardware observation.

For already registered nodes, each discovery pass refreshes the same `hardware.resources` facet. It does not replace the node record, provider set, policy, capabilities, or workspace mapping.

## Persistence semantics

Hardware/resource data is observational cache, not durable identity truth.

- Configured local nodes are re-probed when the control plane boots.
- Remote nodes are refreshed from their Machine Agent during discovery.
- Approved enrollment sidecar records persist identity/policy/provider configuration, but do not persist hardware observations as authoritative configuration.
- If a remote node is offline after restart, its current hardware observation remains unavailable until it is observed again.

This keeps stale telemetry separate from durable node authority.

## Compatibility

The external product surface remains Machines. Existing machine APIs, UI, policies, session binding, and capability routing are unchanged.

No product, AI runtime, model family, hardware vendor, or provider is privileged by N2 core.

## N3 integration

Runtime/backend observations now travel through the same node observation pipeline as hardware/resource observations. See `docs/HIRDA_NODE_BACKENDS.md`.

## N4 integration

Model inventories now use the same node observation pipeline with backend lineage enforced by the control plane. See `docs/HIRDA_NODE_MODELS.md`.

## N5 integration

Capability advertisements now share the same node observation pipeline and remain separate from policy/authority. See `docs/HIRDA_NODE_CAPABILITIES.md`.

## N6 integration

Hardware resource facts now complement refreshable health/load observations and stale-aware availability. See `docs/HIRDA_NODE_HEALTH.md`.

## Next milestone

N7 — Capability Router Integration.
