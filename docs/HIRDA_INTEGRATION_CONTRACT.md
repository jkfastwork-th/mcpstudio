# HIRDA Integration Contract

HIRDA integrations use one control-plane lifecycle instead of wiring each subsystem
independently into routing, permissions, health, telemetry, and certification.

## Lifecycle

Every integration moves through the same stages:

```text
DISCOVERED -> VALIDATING -> REGISTERED -> CERTIFYING -> READY
```

A failed integration stays at the exact stage that failed and reports
`blocked=true`, `failed_stage`, and `error`. Integration failures do not prevent
the HIRDA control plane from starting.

HIRDA automatically reconciles registered integrations during application startup.

## Manifest contract

Schema: `hirda-integration-manifest-v1`

Example:

```yaml
schema: hirda-integration-manifest-v1
id: example
name: Example
capabilities:
  - context_search

runtime:
  type: local-cli

tools:
  - example_search

permissions:
  example_search: read

health:
  type: adapter_probe

routing:
  mode: context-plane
  preferred_lanes:
    - hermes

metadata:
  write_authority: false
```

Every declared tool must have exactly one permission class:

- `read`
- `write`
- `execute`
- `destructive`

Unknown or undeclared permission mappings fail validation.

## Adapter contract

An integration adapter exposes one validated manifest and three read-only lifecycle
probes:

```python
class ExampleIntegrationAdapter:
    manifest = IntegrationManifest.from_mapping({...})

    async def validate(self) -> dict:
        return {"ok": True}

    async def certify(self) -> dict:
        return {"ok": True}

    async def status(self) -> dict:
        return {"ok": True}
```

`validate()` checks runtime/config prerequisites. `certify()` checks authority and
contract invariants. `status()` reports live state without mutating the subsystem.

The Integration Manager owns orchestration only. Existing subsystem managers remain
authoritative for their own execution and state.

## External discovery

External packages may register an adapter without editing HIRDA core by publishing a
Python entry point in the `hirda.integrations` group.

Example `pyproject.toml`:

```toml
[project.entry-points."hirda.integrations"]
example = "example_hirda:ExampleIntegrationAdapter"
```

External entry-point loading executes installed package code, so discovery is
disabled by default. Enable it only for trusted installed packages:

```yaml
studio:
  integration_entrypoints_enabled: true
  integration_entrypoint_group: hirda.integrations
```

After that global opt-in, newly installed integration packages can be discovered,
validated, certified, and surfaced through the common lifecycle without adding
per-plugin wiring to `mcp_studio/main.py`.

## API

- `GET /api/integrations`
- `GET /api/integrations/{integration_id}`
- `POST /api/integrations/{integration_id}/reconcile`

The main `/api/status` payload also contains the current integration registry
snapshot.

## First migrated integration: Graft

Graft is registered as `builtin:graft`.

The migration does not grant new authority. Its external tools remain semantic
read operations, while its existing internal permission profile still enforces:

```text
read=true
write=false
execute=true
destructive=false
scope=workspace
fail_closed_unknown=true
```

Graft workspace activation, rollout, circuit-breaker behavior, and Serena write
authority remain owned by the existing `GraftManager`.

## Second migrated integration: TypeSafe JEV

JEV is registered as `builtin:jev` without adding new wiring to `main.py`, the gateway,
or Studio settings. It reuses the existing `studio` configuration already passed into
the Integration Manager builder.

JEV intentionally declares no direct integration tools because it is invoked internally
by HIRDA after deterministic permission checks. Its manifest certifies the existing
authority boundary:

```text
advisory_only=true
grant_authority=false
block_authority=false
execution_authority=false
action_creation_authority=false
runtime_policy=hirda-reflex
```

Validation is local and side-effect free. It checks whether JEV is enabled, the mode is
recognized, the endpoint is HTTPS (or loopback HTTP), the model and timeout are valid,
and the configured API-key environment variable is present. The credential value is
never returned in integration telemetry.

Startup reconciliation deliberately does not make a network request to JEV. External
request health remains observable through the existing JEV/Reflex telemetry path, while
the integration lifecycle answers the separate question: "is this integration correctly
configured and certified to participate?"

The JEV migration demonstrates the intended steady state: adding a built-in integration
required an adapter plus tests/documentation, not another parallel set of startup,
permission, routing, or status wiring.

## Design rule

Adding an integration must not create another parallel control plane.

Prefer:

```text
system-specific adapter
        |
        v
HIRDA Integration Manager
        |
        +-- permission contract
        +-- health/status
        +-- routing metadata
        +-- certification
        +-- lifecycle/telemetry surface
```

over adding new one-off startup, status, permission, and dashboard wiring for each
system.
