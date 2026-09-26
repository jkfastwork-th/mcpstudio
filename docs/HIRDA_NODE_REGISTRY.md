# HIRDA Node Registry

N1 establishes Node Registry as HIRDA's canonical internal node abstraction while keeping the existing Machine UI and Machine API contracts compatible.

## Architectural contract

HIRDA is a universal capability and cognitive orchestration layer. A node is the identity and lifecycle container for a compute endpoint. Hardware observations, local AI runtimes, models, Desktop Commander, browsers, and other backends are capabilities or providers of that node; none of them is the node itself.

The control plane instantiates NodeRegistry. It subclasses the existing MachineRegistry contract so /api/machines, per-machine policy, session binding, capability routing, and existing MachineRegistry consumers continue to work unchanged.

## Lifecycle

Every node receives vendor-neutral lifecycle metadata under metadata.node_registry:

- schema
- node_id
- state
- source
- registered_at
- enrolled_at when applicable
- updated_at
- monotonic in-process revision
- generic enrichment state and facets

Enrollment and device pairing create an enrolled node record immediately. They do not wait for hardware, runtime, model, or capability discovery. Later discovery stages enrich that exact record.

## Generic enrichment

NodeRegistry.enrich_node() attaches an arbitrary named facet with source, observation timestamp, and data. N1 intentionally does not interpret vendor or runtime-specific payloads. N2-N6 can populate hardware, resources, runtimes, models, capabilities, health, load, and availability through this generic foundation.

No product, model runtime, or hardware vendor is privileged by Node Registry core.

## Compatibility

The product surface remains Machines for now. Existing machine IDs, machine APIs, per-machine policies, session bindings, provider contracts, and workspace behavior remain valid.

## N2 hardware/resource discovery

Hardware/resource observations now enrich the existing node through the versioned `hardware.resources` facet. They remain observational cache rather than durable identity or policy. See `docs/HIRDA_NODE_HARDWARE.md`.

## N3 runtime/backend discovery

Configured runtime/backend probes now enrich the existing node through the versioned `runtime.backends` facet. Local control plane, legacy Machine Agent, and Shared Device Runtime use the same observation contract. See `docs/HIRDA_NODE_BACKENDS.md`.

## N4 model discovery

Models are discovered beneath existing backends and stored in the versioned `runtime.models` facet with explicit node/backend lineage. See `docs/HIRDA_NODE_MODELS.md`.

## N5 capability advertisement

Node/backend/model capability claims now use the versioned `capabilities.advertised` facet with lineage enforced against N3/N4 inventories. Advertisement remains observational and does not grant permission or routing authority. See `docs/HIRDA_NODE_CAPABILITIES.md`.

## Next milestone

N6 — Health / Load / Availability.
