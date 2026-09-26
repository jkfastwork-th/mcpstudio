# HIRDA Capability Router Integration

N7 integrates N5 capability advertisement and N6 health/availability into the existing Machine Capability Router without weakening the machine-policy ceiling.

## Route request

The resolver accepts:

- capability
- permission class
- optional machine filter
- optional backend filter
- optional model filter

The external API remains on the Machines surface:

`POST /api/machines/routes/resolve`

The response schema is `hirda-capability-route-v1`.

## Eligibility gates

A route is eligible only when all applicable gates pass:

1. the machine/node is enabled;
2. the requested capability is present in `capabilities.advertised`;
3. backend/model claims still match the current N3/N4 lineage;
4. the requested permission is allowed by existing machine policy;
5. N6 node health is fresh and currently available;
6. backend/model routes have fresh backend health and the backend is currently available.

Unknown, stale, unavailable, policy-denied, or lineage-invalid routes fail closed.

## Lineage defense in depth

N7 does not trust capability advertisement alone.

- backend capability claims are cross-checked against an N3 backend that is currently marked available;
- model capability claims are cross-checked against an N4 inventory whose status is ready;
- model filters require an explicit backend ID;
- node ID comes from the registry, not from an external claim.

## Selection order

Eligible routes are sorted deterministically using current observations:

1. lower 1-minute node load ratio;
2. lower backend health-probe latency;
3. higher available-memory ratio;
4. more specific scope: model, backend, node;
5. stable node/backend/model IDs.

Missing load/latency data never makes a route more attractive than known lower-load data.

The router does not impose provider-specific load thresholds in N7. Threshold policy can evolve independently while the raw N6 observations remain provider-neutral.

## Explainability

The resolver returns:

- selected route;
- all eligible routes in deterministic order;
- rejected routes;
- rejection reason and policy/health detail where relevant.

Rejection reasons include capability not advertised, lineage mismatch, machine disabled, permission denied, stale health, node unavailable, missing backend health, and backend unavailable.

## Authority boundary

Route resolution does not:

- execute a backend;
- bind or move a managed session;
- mutate node identity;
- mutate configured machine capabilities;
- change policy;
- grant provider authority.

Existing machine policy remains the hard ceiling. N7 only determines eligibility and preference among routes already allowed by that policy.

## Machines UI compatibility

`GET /api/machines` remains backward compatible and now adds two routing-observability fields per machine:

- `routing_availability`
- `advertised_capabilities`

The existing `online` field and provider-health surface remain unchanged.

## N8 cognitive router integration

The Cognitive Router now consumes N7 as an infrastructure eligibility gate through explicit capability mappings and runtime-to-node/backend/model bindings. Semantic lane/runtime policy remains authoritative. See `docs/HIRDA_COGNITIVE_ROUTER.md`.

## Roadmap status

N1-N8 are complete. Next: end-to-end certification and controlled shadow/enforce cutover.
