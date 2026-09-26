# HIRDA N8 Production Enforce Cutover

Production N8 cognitive infrastructure routing is now running in `enforce` mode.

## Cutover

The production configuration is:

```yaml
cognitive_node_routing_mode: enforce
```

The service restarted through the normal production preflight path and returned active/running.

## Post-cutover certification

`scripts/certify_cognitive_enforce_cutover.py` performs plan-only certification against the live production enforce service.

It compares each live enforce plan with an explicit off-mode baseline built from the same live runtime/pane state.

The certified cognitive cases are:

- social_dialogue
- reflection
- fast_utility
- reasoning_high
- coding
- research

## Required gates

The cutover remains accepted only when:

- config mode is enforce;
- live service mode is enforce;
- all three runtime bindings are N7-eligible;
- all six cognitive cases pass;
- semantic lane is unchanged;
- runtime candidate order is unchanged;
- no unexpected runtime filtering occurs while all current routes are healthy;
- identity continuity remains true;
- semantic authority remains unchanged;
- no prompt is executed by certification;
- semantic drift equals zero.

## Certified runtime bindings

Current live bindings:

- Claude -> `openclaw / claude-runtime`
- Codex -> `openclaw / codex-runtime`
- Hermes -> `openclaw / hermes-runtime`

Each binding resolved through N7 with machine execute policy, current N6 health, and N3 backend lineage.

## Latest result

Post-cutover certification passed with:

- runtime bindings: 3/3 eligible;
- cognitive cases: 6/6 passed;
- candidate-order continuity: pass;
- lane continuity: pass;
- unexpected filtering: none;
- identity continuity: pass;
- semantic authority: unchanged;
- prompt execution: none;
- semantic drift: 0.

Machine-readable evidence:

`data/certification/n8-enforce-cutover-latest.json`

## Rollback

If a live execution gate fails, rollback is configuration-only:

```yaml
cognitive_node_routing_mode: shadow
```

Then restart `mcp-studio.service` and run the shadow certification again.

No Node Registry, capability, model, machine-policy, or cognitive-state migration is required.
