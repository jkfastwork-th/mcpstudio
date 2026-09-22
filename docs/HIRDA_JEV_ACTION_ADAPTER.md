# HIRDA JEV Action Adapter v0.1

Status: prototype contract implemented
Runtime authority: none
Execution authority: none

## Purpose

The JEV Action Adapter is the common action-selection boundary for HIRDA providers.

A provider converts its environment into:

```text
structured state
+ bounded candidate actions
+ explicit risk/consequence metadata
```

JEV may then judge which candidate best fits the state. The adapter never executes the candidate and never grants permission to execute it.

This lets Browser, WebMCP, Oriverse, Earth-616, finance systems, and later providers share one decision contract without sharing executor code.

## Core rule

```text
Provider computes state and legal actions
            ↓
      HIRDA ActionEnvelope
            ↓
           JEV
       advisory judgment
            ↓
 HIRDA runtime/policy/approval
            ↓
      provider executor
```

The current prototype stops at the JEV judgment.

## Envelope

Schema: `hirda-action-envelope-v1`

Canonical JSON Schema:

`docs/schemas/hirda-action-envelope-v1.schema.json`

Required top-level fields:

- `request_id`: unique provider request identifier;
- `provider`: adapter/provider identity such as `browser`, `oriverse`, or `navinvestor`;
- `domain`: broad domain such as `browser`, `world`, or `finance`;
- `goal`: bounded decision objective;
- `state`: structured current state computed by provider code;
- `actions`: 1-32 legal candidate actions.

Every action carries:

- stable `action_id`;
- generic `operation`;
- title/description;
- HIRDA-compatible `permission_class`;
- provider arguments and constraints;
- explicit risk flags.

Risk flags are deliberately generic:

`consequential`, `reversible`, `external_side_effect`, `destructive`, `financial`, `requires_human_approval`.

For example, a future finance provider can remain `permission_class=execute` while declaring `financial=true`, `consequential=true`, and `requires_human_approval=true`.

## JEV output

Schema: `hirda-jev-action-judgment-v1`

Canonical output JSON Schema:

`docs/schemas/hirda-jev-action-judgment-v1.schema.json`

The adapter asks JEV for:

- one selected candidate;
- candidate probabilities when returned by JEV;
- `needs_human_review`;
- `needs_more_information`;
- `consequence_risk`;
- `uncertainty`.

Candidate IDs are not sent as raw Choice keys. They are mapped to local tokens `a0..a31`, then translated back after the response. This keeps provider identifiers independent from JEV Choice-key constraints.

The result always carries:

```text
advisory_only=true
runtime_authorization_required=true
may_execute=false
external_action_authority=false
```

## Security boundary

The adapter reuses HIRDA JEV sanitization before provider-controlled state, goal/title/description text, arguments, or constraints enter the JEV request. Secret-looking fields and inline bearer tokens are redacted.

A provider's declared permission/risk metadata is descriptive input. It cannot grant permission. HIRDA runtime policy remains authoritative.

The v0.1 adapter intentionally has no executor hook.

## Quick test

Validate the built-in browser-like sample without calling JEV:

```bash
.venv/bin/python scripts/jev-action-demo.py
```

Print the normalized sample:

```bash
.venv/bin/python scripts/jev-action-demo.py --json
```

Call the configured JEV endpoint:

```bash
.venv/bin/python scripts/jev-action-demo.py --live
```

`--live` reads the same HIRDA JEV settings and `TYPESAFE_API_KEY` configuration used by Reflex. It still performs no action execution.

## JAA-1 test providers and playground

HIRDA includes three deterministic test providers that all emit the same `hirda-action-envelope-v1` contract:

- `browser`: wait / submit / clear-query;
- `world`: observe / respond / defer;
- `finance`: hold / refresh-data / prepare-rebalance.

The finance fixture deliberately carries `financial`, `consequential`, and `requires_human_approval` metadata while also declaring `orders_forbidden=true`. It cannot place a broker order.

Playground API:

```text
GET  /api/action-adapter/providers
GET  /api/action-adapter/providers/{provider_id}
POST /api/action-adapter/providers/{provider_id}/evaluate
```

The HIRDA **Actions** page consumes these endpoints. Evaluation returns the normalized envelope plus `hirda-jev-action-judgment-v1`, with:

```text
executor_attached=false
execution_performed=false
```

The page is therefore suitable for schema/JEV testing before any real Browser, WebMCP, Oriverse, Earth-616, or NavInvestor executor is connected.

## JAA-2 provider contract and registry

Providers now implement one envelope-producer contract instead of being wired directly into FastAPI routes:

```python
class ActionProvider(Protocol):
    provider_id: str
    domain: str
    title: str
    description: str
    executor_attached: bool

    def build_envelope(self) -> ActionEnvelope | dict | Awaitable[ActionEnvelope | dict]: ...
```

`build_envelope()` may be synchronous or asynchronous, so a future Browser or NavInvestor provider can fetch live DOM/API state without changing the HIRDA route contract.

`ActionProviderRegistry` validates these invariants every time an envelope is built:

- `envelope.provider` must match the registered `provider_id`;
- `envelope.domain` must match the registered domain;
- `executor_attached` cannot silently change;
- duplicate provider IDs fail closed;
- JAA-2 rejects providers that declare an attached executor.

Registry status is available at:

```text
GET /api/action-adapter/registry
```

Canonical registry snapshot schema:

`docs/schemas/hirda-action-provider-registry-v1.schema.json`

The existing `/api/action-adapter/providers...` routes now resolve providers through this registry, so API/UI code no longer knows about Browser, World, Finance, or future providers individually.

### External provider plugins

Trusted installed Python packages can publish an entry point in the `hirda.action_providers` group. For example:

```toml
[project.entry-points."hirda.action_providers"]
navinvestor = "navinvestor.hirda:provider"
```

External entry-point discovery is **disabled by default** because loading a Python entry point executes installed package code. An operator must explicitly enable it:

```yaml
studio:
  action_provider_entrypoints_enabled: true
  action_provider_entrypoint_group: hirda.action_providers
```

Discovery failures are recorded in the registry snapshot and do not grant execution authority. The registry remains envelope-only; provider execution is a later, separately certified stage.
