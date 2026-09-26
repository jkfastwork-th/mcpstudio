# HIRDA Node Health / Load / Availability

N6 adds refreshable health, load, and availability observations to the Node Registry without granting routing authority.

## Observation schema

The versioned schema is `hirda-node-health-v1` and is stored at:

`metadata.node_registry.enrichment.facets.health.availability`

The node section reports:

- `available`
- `state`
- logical CPU count
- 1/5/15 minute load averages when the platform exposes them
- normalized 1 minute load ratio
- physical memory total / available / available ratio when available
- storage total / free / free ratio for the observed volume

Backend entries report:

- `backend_id`
- `available`
- `state`
- health probe latency when a health probe is configured

## Generic backend health probes

An N3 backend probe may optionally declare:

`health_probe.command`

and a bounded `health_probe.timeout_seconds`.

Exit status zero means the backend health probe is ready. A nonzero exit status, missing probe executable, timeout, or execution failure is reported as unavailable with an explicit state.

HIRDA core contains no provider-specific health check.

## Refresh paths

N6 refreshes health through all supported node transports:

- the local control-plane node uses a periodic `NodeHealthMonitor`;
- legacy Machine Agent health is refreshed by the existing discovery loop;
- Shared Device Runtime sends health on initial authenticated observation and again on heartbeat without rerunning model discovery.

When a Device Broker WSS connection closes and no newer connection exists, the control plane marks that node offline immediately.

## Freshness

Health is ephemeral observation state.

`NodeRegistry.availability()` evaluates the observation against `node_health_stale_after_seconds`. Stale health is treated as unavailable even if the last observed payload said ready.

Defaults:

- health refresh interval: 15 seconds
- stale after: 45 seconds

The stale threshold must be greater than the refresh interval.

## Authority boundary

Health and availability do not mutate:

- node identity;
- configured machine capabilities;
- backend/model capability claims;
- per-machine policy;
- provider configuration;
- workspace mapping;
- routing decisions.

N6 reports current facts. N7 decides whether those facts make a route eligible.

## Next milestone

N7 — Capability Router Integration: combine capability lineage, policy, health, freshness, and availability to select eligible node/backend/model routes without weakening existing machine policy.
